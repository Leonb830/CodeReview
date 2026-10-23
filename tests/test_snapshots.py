import base64
import hashlib
import subprocess
from pathlib import Path

import pytest

from codereview.config import Settings
from codereview.snapshots import capture_snapshot, export_snapshot


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo with spaces"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    (root / "app.py").write_text("value = 1\n")
    (root / ".gitignore").write_text("ignored.py\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


def test_working_and_staged_capture_distinct_content_without_mutating_checkout(repo: Path):
    (repo / "app.py").write_text("value = 2\n")
    git(repo, "add", "app.py")
    (repo / "app.py").write_text("value = 3\n")
    (repo / "new.ts").write_text("export const answer = 42;\n")
    (repo / "ignored.py").write_text("hidden = True\n")
    status_before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    index_before = hashlib.sha256((repo / ".git/index").read_bytes()).hexdigest()

    working = capture_snapshot(repo)
    staged = capture_snapshot(repo, mode="staged")

    assert working.files["app.py"] == "value = 3\n"
    assert staged.files["app.py"] == "value = 2\n"
    assert "new.ts" in working.files and "new.ts" not in staged.files
    assert "ignored.py" not in working.files
    assert working.changed_lines["app.py"] == [1]
    assert working.changed_lines["new.ts"] == [1]
    assert working.id != staged.id
    assert capture_snapshot(repo).id == working.id
    assert hashlib.sha256((repo / ".git/index").read_bytes()).hexdigest() == index_before
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == status_before


def test_branch_uses_merge_base_and_ignores_dirty_worktree(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "feature")
    (repo / "app.py").write_text("value = 4\n")
    git(repo, "commit", "-qam", "feature")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "other", base)
    (repo / "base_only.py").write_text("other = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "other base advanced")
    other = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("dirty = True\n")

    snapshot = capture_snapshot(repo, mode="branch", base=other, head=head)

    assert snapshot.base_sha == base
    assert snapshot.head_sha == head
    assert snapshot.files["app.py"] == "value = 4\n"
    assert snapshot.changed_paths == ["app.py"]
    assert "base_only.py" not in snapshot.files
    assert (repo / "app.py").read_text() == "dirty = True\n"


def test_deleted_files_are_explicit_in_scope_and_diff(repo: Path):
    (repo / "app.py").unlink()
    snapshot = capture_snapshot(repo)
    assert "app.py" in snapshot.changed_paths
    assert "app.py" not in snapshot.files
    assert snapshot.deleted_paths == ["app.py"]
    assert snapshot.changed_lines["app.py"] == []
    assert "+++ /dev/null" in snapshot.diff
    assert "-value = 1" in snapshot.diff


def test_exclusions_are_reported_and_never_leak_into_diff(repo: Path):
    (repo / ".env").write_text("TOP_SECRET=very-private\n")
    (repo / "binary.py").write_bytes(b"binary\x00data")
    (repo / "huge.py").write_text("x" * 101)
    (repo / "linked.py").symlink_to(repo / ".env")
    (repo / "node_modules").mkdir()
    (repo / "node_modules/generated.ts").write_text("generated = true")
    snapshot = capture_snapshot(repo, settings=Settings(max_file_bytes=100))
    assert set(snapshot.files) == {"app.py", ".gitignore"}
    assert all(
        any(path in gap for gap in snapshot.excluded)
        for path in [".env", "binary.py", "huge.py", "linked.py", "node_modules/generated.ts"]
    )
    assert "very-private" not in snapshot.diff


def test_audit_includes_unchanged_source_and_snapshot_cap_is_explicit(repo: Path):
    (repo / "other.py").write_text("b = 2\n")
    snapshot = capture_snapshot(repo, mode="audit")
    assert snapshot.changed_paths == sorted(snapshot.files)
    assert snapshot.changed_lines["app.py"] == [1]
    (repo / "large.py").write_text("x" * 1000)
    capped = capture_snapshot(repo, mode="audit", settings=Settings(max_snapshot_bytes=1000))
    assert any("snapshot byte limit" in gap for gap in capped.excluded)


def test_export_rejects_traversal_and_symlinks_before_writing(repo: Path, tmp_path: Path):
    snapshot = capture_snapshot(repo)
    destination = tmp_path / "export"
    export_snapshot(snapshot, destination)
    assert (destination / "app.py").read_text() == "value = 1\n"
    bad = snapshot.model_copy(update={"files": {"app.py": "bad", "../escape.py": "bad"}})
    with pytest.raises(ValueError):
        export_snapshot(bad, destination)
    assert (destination / "app.py").read_text() == "value = 1\n"
    (destination / "link").symlink_to(tmp_path, target_is_directory=True)
    bad = snapshot.model_copy(update={"files": {"link/escape.py": "bad"}})
    with pytest.raises(ValueError):
        export_snapshot(bad, destination)
    assert not (tmp_path / "escape.py").exists()


def test_no_final_newline_and_disjoint_additions_have_exact_right_lines(repo: Path):
    (repo / "app.py").write_text("one = 1\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n")
    git(repo, "commit", "-qam", "lines")
    (repo / "app.py").write_text("one = 9\ntwo = 2\nthree = 3\nfour = 4\nfive = 8")
    snapshot = capture_snapshot(repo)
    assert snapshot.changed_lines["app.py"] == [1, 5]
    assert "\\ No newline at end of file" in snapshot.diff


def test_capture_never_executes_repo_diff_or_fsmonitor_hooks(repo: Path):
    marker = repo / "executed"
    program = repo / "hook.sh"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    program.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(program))
    git(repo, "config", "diff.external", str(program))
    (repo / "app.py").write_text("value = 5\n")
    snapshot = capture_snapshot(repo)
    assert snapshot.files["app.py"] == "value = 5\n"
    assert not marker.exists()


def test_unborn_repository_can_capture_staged_and_working(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "app.py").write_text("first = True\n")
    assert capture_snapshot(tmp_path).changed_paths == ["app.py"]
    git(tmp_path, "add", "app.py")
    snapshot = capture_snapshot(tmp_path, mode="staged")
    assert snapshot.base_sha == snapshot.head_sha == ""
    assert snapshot.changed_lines["app.py"] == [1]


def test_dirty_lockfiles_and_binary_bytes_are_preserved_outside_model_context(repo: Path):
    (repo / "package-lock.json").write_text('{"version": 1}\n')
    (repo / "asset.bin").write_bytes(b"\x00\xffdata")
    snapshot = capture_snapshot(repo)
    assert "package-lock.json" not in snapshot.files
    assert "asset.bin" not in snapshot.files
    assert base64.b64decode(snapshot.overlay_files["package-lock.json"]) == b'{"version": 1}\n'
    assert base64.b64decode(snapshot.overlay_files["asset.bin"]) == b"\x00\xffdata"
    assert not snapshot.unavailable_changes
    (repo / "asset.bin").write_bytes(b"\x00\xffdifferent")
    assert capture_snapshot(repo).id != snapshot.id


def test_unavailable_dirty_secrets_symlinks_and_large_files_are_explicit(repo: Path):
    (repo / ".env").write_text("PRIVATE_KEY=never-capture\n")
    (repo / "linked.py").symlink_to(repo / ".env")
    (repo / "large.bin").write_bytes(b"\x00" * 101)
    snapshot = capture_snapshot(repo, settings=Settings(max_file_bytes=100))
    assert all(
        any(gap.startswith(path + ":") for gap in snapshot.unavailable_changes)
        for path in [".env", "linked.py", "large.bin"]
    )
    assert not snapshot.overlay_files
    assert "never-capture" not in snapshot.model_dump_json()


def test_audit_unavailable_changes_only_reports_dirty_files(repo: Path):
    (repo / "asset.bin").write_bytes(b"\x00" * 101)
    git(repo, "add", "asset.bin")
    git(repo, "commit", "-qm", "binary asset")
    snapshot = capture_snapshot(repo, mode="audit", settings=Settings(max_file_bytes=100))
    assert any(gap.startswith("asset.bin:") for gap in snapshot.excluded)
    assert not snapshot.unavailable_changes


def test_local_file_mode_changes_are_preserved_for_repairs(repo: Path):
    (repo / "app.py").chmod(0o755)
    snapshot = capture_snapshot(repo)
    assert snapshot.file_modes["app.py"] == 0o755
    assert not snapshot.unavailable_changes


def test_export_replaces_hardlinks_without_modifying_their_other_name(repo: Path, tmp_path: Path):
    source = tmp_path / "original.py"
    source.write_text("original")
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "app.py").hardlink_to(source)
    export_snapshot(capture_snapshot(repo), destination)
    assert source.read_text() == "original"
    assert (destination / "app.py").read_text() == "value = 1\n"


def test_worktree_capture_never_executes_clean_filters(repo: Path):
    marker = repo / "executed"
    program = repo / "filter.sh"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    program.chmod(0o755)
    (repo / ".gitattributes").write_text("app.py filter=unsafe\n")
    git(repo, "config", "filter.unsafe.clean", str(program))
    (repo / "app.py").write_text("value = 7\n")
    snapshot = capture_snapshot(repo)
    assert snapshot.files["app.py"] == "value = 7\n"
    assert not marker.exists()


def test_assume_unchanged_cannot_hide_dirty_binary_overlay(repo: Path):
    (repo / "asset.bin").write_bytes(b"\x00old")
    git(repo, "add", "asset.bin")
    git(repo, "commit", "-qm", "asset")
    git(repo, "update-index", "--assume-unchanged", "asset.bin")
    (repo / "asset.bin").write_bytes(b"\x00new")
    snapshot = capture_snapshot(repo)
    assert "asset.bin" in snapshot.changed_paths
    assert base64.b64decode(snapshot.overlay_files["asset.bin"]) == b"\x00new"


def test_sparse_missing_worktree_file_is_not_deleted(repo: Path):
    git(repo, "update-index", "--skip-worktree", "app.py")
    (repo / "app.py").unlink()
    snapshot = capture_snapshot(repo)
    assert "app.py" not in snapshot.deleted_paths
    assert "app.py" not in snapshot.changed_paths
    assert snapshot.files["app.py"] == "value = 1\n"
