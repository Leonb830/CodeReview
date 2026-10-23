"""Read-only, content-addressed Git inputs. Repository programs are never executed.

``files`` contains bounded reviewable text, not a runnable repository archive.
Repairs must materialize the pinned Git baseline before applying this overlay.
Every omitted path is reported; consumers must not claim coverage for it.
"""

import base64
import difflib
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from codereview.config import Settings
from codereview.models import Snapshot

_TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".prisma",
    ".json",
    ".jsonc",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".rst",
    ".txt",
    ".ini",
    ".cfg",
    ".sql",
    ".graphql",
    ".gql",
    ".html",
    ".css",
    ".scss",
    ".sh",
}
_TEXT_NAMES = {
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".editorconfig",
    ".nvmrc",
    ".python-version",
    "Dockerfile",
    "Makefile",
    "Procfile",
    "LICENSE",
    "NOTICE",
}
_GENERATED = {
    ".git",
    ".codereview",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    "coverage",
    ".coverage",
    "vendor",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
_SECRET_NAMES = {".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class _Entry:
    mode: str
    oid: str | None = None


def _worktree_entry(root: Path, path: str, tracked: _Entry | None) -> _Entry:
    if tracked and tracked.mode == "160000":
        return _Entry("160000")
    try:
        mode = (root / path).lstat().st_mode
    except OSError:
        return _Entry("100644")  # A later read reports the concurrent disappearance.
    if stat.S_ISLNK(mode):
        return _Entry("120000")
    return _Entry("100755" if mode & 0o111 else "100644")


def _git(root: Path, *args: str, allowed_failure: bool = False) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        }
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            *args,
        ],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode and not allowed_failure:
        raise ValueError(
            f"Git snapshot read failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return b"" if result.returncode else result.stdout


def _commit(root: Path, ref: str, *, optional: bool = False) -> str:
    return (
        _git(
            root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
            allowed_failure=optional,
        )
        .decode()
        .strip()
    )


def _tree(root: Path, revision: str) -> dict[str, _Entry]:
    if not revision:
        return {}
    entries = {}
    for record in _git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            mode, _, oid = meta.decode().split()
            entries[path.decode("utf-8", errors="backslashreplace")] = _Entry(mode, oid)
    return entries


def _index(root: Path) -> tuple[dict[str, _Entry], list[str]]:
    entries: dict[str, _Entry] = {}
    conflicts: set[str] = set()
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            meta, raw_path = record.split(b"\t", 1)
            mode, oid, stage = meta.decode().split()
            path = raw_path.decode("utf-8", errors="backslashreplace")
            if stage != "0":
                conflicts.add(path)
            else:
                entries[path] = _Entry(mode, oid)
    return entries, sorted(conflicts)


def _skip_worktree(root: Path) -> set[str]:
    return {
        row[2:].decode("utf-8", errors="backslashreplace")
        for row in _git(root, "ls-files", "-v", "-z").split(b"\0")
        if row[:1].upper() == b"S"
    }


def _safe_path(path: str) -> PurePosixPath:
    item = PurePosixPath(path)
    if (
        not path
        or item.is_absolute()
        or item.as_posix() != path
        or any(part in {".", ".."} or part.casefold() == ".git" for part in item.parts)
        or any(c in path for c in "\\:\x00\r\n")
    ):
        raise ValueError(f"Unsafe snapshot path: {path!r}")
    return item


def _safety_reason(path: str) -> str | None:
    try:
        item = _safe_path(path)
    except ValueError:
        return "unsafe or unsupported path"
    if any(part in _GENERATED for part in item.parts[:-1]):
        return "generated or dependency directory"
    if (
        item.name in _SECRET_NAMES
        or item.name == ".env"
        or item.name.startswith(".env.")
        or item.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    ):
        return "potential secret file"
    return None


def _policy_reason(path: str, settings: Settings) -> str | None:
    reason = _safety_reason(path)
    if reason:
        return reason
    item = PurePosixPath(path)
    if any(
        fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(item.name, pattern)
        for pattern in settings.exclude
    ):
        return "repository exclusion policy"
    if item.suffix.lower() not in _TEXT_SUFFIXES and item.name not in _TEXT_NAMES:
        return "unsupported source type"
    return None


def _no_symlink_parents(root: Path, path: PurePosixPath) -> None:
    current = root
    for component in path.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("symlink excluded")


def _read_bytes(
    root: Path, path: str, entry: _Entry, limit: int
) -> tuple[bytes | None, str | None]:
    if entry.mode == "120000":
        return None, "symlink excluded"
    if entry.mode == "160000":
        return None, "submodule excluded"
    try:
        if entry.oid:
            size = int(_git(root, "cat-file", "-s", entry.oid))
            if size > limit:
                return None, "file byte limit exceeded"
            raw = _git(root, "cat-file", "blob", entry.oid)
        else:
            item = _safe_path(path)
            _no_symlink_parents(root, item)
            descriptor = os.open(root / path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                details = os.fstat(source.fileno())
                if not stat.S_ISREG(details.st_mode):
                    return None, "nonregular file excluded"
                if details.st_size > limit:
                    return None, "file byte limit exceeded"
                raw = source.read(limit + 1)
        if len(raw) > limit:
            return None, "file byte limit exceeded"
        return raw, None
    except (OSError, ValueError) as exc:
        return None, f"unreadable source ({type(exc).__name__})"


def _read(root: Path, path: str, entry: _Entry, limit: int) -> tuple[str | None, str | None]:
    raw, reason = _read_bytes(root, path, entry, limit)
    if reason or raw is None:
        return None, reason
    if b"\0" in raw:
        return None, "binary file excluded"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "non-UTF-8 file excluded"


def _working_oid(
    root: Path,
    path: str,
    entry: _Entry,
    limit: int,
    algorithm: str,
) -> tuple[str | None, str | None]:
    if entry.oid:
        return entry.oid, None
    raw: bytes | None
    if entry.mode == "120000":
        try:
            _no_symlink_parents(root, PurePosixPath(path).parent)
            raw = os.fsencode(os.readlink(root / path))
        except (OSError, ValueError):
            return None, "symlink target unavailable"
    else:
        raw, reason = _read_bytes(root, path, entry, limit)
        if reason or raw is None:
            return None, reason
    # Hash the raw Git blob representation ourselves. Git's worktree diff and
    # hash-object filters can execute repository-configured clean/process tools.
    digest = hashlib.new(algorithm)
    digest.update(f"blob {len(raw)}\0".encode())
    digest.update(raw)
    return digest.hexdigest(), None


def _right_lines(diff: str) -> list[int]:
    result: list[int] = []
    line = 0
    in_hunk = False
    for text in diff.splitlines():
        match = _HUNK.match(text)
        if match:
            line = int(match[1])
            in_hunk = True
        elif in_hunk and text.startswith("+"):
            result.append(line)
            line += 1
        elif in_hunk and text.startswith(" "):
            line += 1
    return result


def capture_snapshot(
    root: Path,
    *,
    mode: Literal["working", "staged", "branch", "audit"] = "working",
    base: str | None = None,
    head: str | None = None,
    settings: Settings | None = None,
) -> Snapshot:
    """Capture Git objects/index or local bytes without touching checkout or index.

    Branch mode requires a base and compares its merge-base to the resolved head.
    Working/audit modes capture the worktree including nonignored untracked files.
    Staged mode reads blobs directly from the index, even if the worktree differs.
    """
    if mode not in {"working", "staged", "branch", "audit"}:
        raise ValueError(f"Unknown snapshot mode: {mode}")
    root = root.resolve()
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != root:
        raise ValueError("Snapshot root must be the Git repository root")
    settings = settings or Settings()
    head_sha = _commit(root, head or "HEAD", optional=head is None and mode != "branch")
    if mode != "branch" and (base is not None or head is not None):
        raise ValueError("base/head revisions are only supported in branch mode")
    base_sha = head_sha
    if mode == "branch":
        if base is None:
            raise ValueError("Branch mode requires an explicit base revision")
        base_sha = _git(root, "merge-base", _commit(root, base), head_sha).decode().strip()
    before = _tree(root, base_sha)
    excluded: list[str] = []
    conflicts: list[str] = []
    if mode == "branch":
        current = _tree(root, head_sha)
    elif mode == "staged":
        current, conflicts = _index(root)
    else:
        tracked, conflicts = _index(root)
        skipped = _skip_worktree(root)
        names = set(tracked) | set(conflicts)
        names.update(
            x.decode("utf-8", errors="backslashreplace")
            for x in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
            if x
        )
        current = {
            path: _worktree_entry(root, path, tracked.get(path))
            for path in names
            if os.path.lexists(root / path)
        }
        # Sparse checkout absence is not a deletion. Its indexed bytes are the
        # current source; present skipped files are still compared by raw bytes.
        current.update(
            {
                path: tracked[path]
                for path in skipped & tracked.keys()
                if not os.path.lexists(root / path)
            }
        )
    excluded.extend(f"{path}: unresolved merge conflict" for path in conflicts)
    algorithm = _git(root, "rev-parse", "--show-object-format").decode().strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("Unsupported Git object format")
    changed_set = set(before) ^ set(current) | set(conflicts)
    comparison_gaps: dict[str, str] = {}
    for path in current.keys() & before.keys():
        entry = current[path]
        oid, reason = _working_oid(
            root,
            path,
            entry,
            settings.max_snapshot_bytes,
            algorithm,
        )
        if oid != before[path].oid or entry.mode != before[path].mode:
            changed_set.add(path)
        if reason:
            comparison_gaps[path] = reason
    changed = sorted(changed_set)
    actual_changed = changed
    if mode == "audit":
        changed = sorted(current)
    files: dict[str, str] = {}
    reasons: dict[str, str] = {}
    total = 0
    # Prioritize changed files when a repository exceeds its snapshot budget.
    ordered = sorted(current, key=lambda path: (path not in changed, path))
    for path in ordered:
        reason = (
            "unresolved merge conflict" if path in conflicts else _policy_reason(path, settings)
        )
        text = None
        if reason is None:
            text, reason = _read(root, path, current[path], settings.max_file_bytes)
        size = len(text.encode()) if text is not None else 0
        if reason is None and total + size > settings.max_snapshot_bytes:
            reason = "snapshot byte limit exceeded"
        if reason:
            reasons[path] = reason
            excluded.append(f"{path}: {reason}")
        elif text is not None:
            files[path] = text
            total += size
    overlays: dict[str, str] = {}
    unavailable: list[str] = []
    # The pinned head already contains branch-mode bytes. Local modes need to
    # preserve dirty omitted bytes separately without exposing them to models.
    if mode != "branch":
        for path in actual_changed:
            reason = (
                "unresolved merge conflict"
                if path in conflicts
                else _safety_reason(path) or comparison_gaps.get(path)
            )
            if reason:
                unavailable.append(f"{path}: {reason}")
                continue
            if path not in current or path in files:
                continue
            raw, reason = _read_bytes(root, path, current[path], settings.max_file_bytes)
            if (
                reason is None
                and raw is not None
                and total + len(raw) > settings.max_snapshot_bytes
            ):
                reason = "snapshot byte limit exceeded"
            if reason:
                unavailable.append(f"{path}: {reason}")
            elif raw is not None:
                overlays[path] = base64.b64encode(raw).decode("ascii")
                total += len(raw)
    diffs: list[str] = []
    changed_lines: dict[str, list[int]] = {}
    diff_bytes = 0
    for path in changed:
        changed_lines[path] = []
        reason = reasons.get(path) or _policy_reason(path, settings)
        if reason:
            if path not in reasons:
                excluded.append(f"{path}: {reason}")
            continue
        old = ""
        if path in before:
            old_text, reason = _read(root, path, before[path], settings.max_file_bytes)
            if reason:
                excluded.append(f"{path}: baseline {reason}; diff unavailable")
                continue
            old = old_text or ""
        new = files.get(path, "")
        patch = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in current else "/dev/null",
            )
        )
        if diff_bytes + len(patch.encode()) > settings.max_snapshot_bytes:
            excluded.append(f"{path}: diff byte limit exceeded")
            continue
        diffs.append(patch)
        diff_bytes += len(patch.encode())
        changed_lines[path] = (
            list(range(1, len(new.splitlines()) + 1)) if mode == "audit" else _right_lines(patch)
        )
    payload = {
        "head_sha": head_sha,
        "base_sha": base_sha,
        "mode": mode,
        "files": dict(sorted(files.items())),
        "changed_paths": changed,
        "diff": "".join(diffs),
        "excluded": sorted(set(excluded)),
        "changed_lines": changed_lines,
        "deleted_paths": sorted(set(before) - set(current) - set(conflicts)),
        "overlay_files": overlays,
        "unavailable_changes": sorted(set(unavailable)),
        "file_modes": {
            path: 0o755 if current[path].mode == "100755" else 0o644
            for path in sorted(set(files) | set(overlays))
            if current[path].mode in {"100644", "100755"}
        },
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return Snapshot.model_validate({"id": identity, "root": str(root), **payload})


def export_snapshot(snapshot: Snapshot, destination: Path) -> None:
    """Write captured text only; reject traversal/symlinks before writing anything.

    This does not reconstruct excluded files or Git metadata, or remove existing
    files. A repair materializer must handle deletions against its pinned baseline.
    """
    destination = destination.absolute()
    for parent in [*destination.parents, destination]:
        if parent.is_symlink():
            raise ValueError("Snapshot destination must not contain symlinks")
    entries = [(_safe_path(path), content) for path, content in snapshot.files.items()]
    if any(mode not in {0o644, 0o755} for mode in snapshot.file_modes.values()):
        raise ValueError("Snapshot file modes must be 0644 or 0755")
    names = {path for path, _ in entries}
    for path, _ in entries:
        if any(parent in names for parent in path.parents):
            raise ValueError("Snapshot contains conflicting file and directory paths")
        _no_symlink_parents(destination, path)
        target = destination / path
        if target.exists() and not target.is_file():
            raise ValueError(f"Snapshot destination is not a regular file: {path}")
    destination.mkdir(parents=True, exist_ok=True)
    for path, content in entries:
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            # Replacing the directory entry also breaks an existing hardlink;
            # truncating its inode could otherwise modify the original checkout.
            os.chmod(temporary, snapshot.file_modes.get(path.as_posix(), 0o644))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
