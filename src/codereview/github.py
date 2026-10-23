"""Trusted GitHub event handling, revision-bound publication and artifact recovery."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from codereview.config import Settings
from codereview.models import FixResult, ReviewResult

SUMMARY_MARKER = "<!-- codereview-agent:summary:v1 -->"
WORKFLOW_PATH = ".github/workflows/review.yml"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


class GitHubError(RuntimeError):
    """A permission, revision, persistence or publication invariant was not satisfied."""


def _repo(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise GitHubError("Invalid repository name")
    return value


def _sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise GitHubError("Invalid Git revision")
    return value


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        bot_login: str = "github-actions[bot]",
    ):
        if not token:
            raise GitHubError("GITHUB_TOKEN is required")
        self._token = token
        self._client = http_client or httpx.AsyncClient(timeout=30)
        self._owned = http_client is None
        self.bot_login = bot_login

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._owned:
            await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, accepted: tuple[int, ...] = (), **kwargs: Any
    ) -> httpx.Response:
        response = await self._client.request(
            method,
            f"https://api.github.com{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            **kwargs,
        )
        if response.status_code >= 400 and response.status_code not in accepted:
            raise GitHubError(f"GitHub {method} {path} returned HTTP {response.status_code}")
        return response

    async def _pages(
        self, path: str, *, field: str | None = None, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, 21):
            response = await self._request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            data = response.json()
            batch = data[field] if field else data
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubError(
            "GitHub listing exceeded pagination limit; refusing ambiguous publication"
        )

    async def get_pull_request(self, repo: str, number: int) -> dict[str, Any]:
        if number < 1:
            raise GitHubError("Invalid pull request number")
        return (await self._request("GET", f"/repos/{_repo(repo)}/pulls/{number}")).json()

    async def require_writer(self, repo: str, login: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\[\]-]+", login):
            raise GitHubError("Invalid requesting user")
        response = await self._request(
            "GET", f"/repos/{_repo(repo)}/collaborators/{login}/permission"
        )
        if response.json().get("permission") not in {"write", "maintain", "admin"}:
            raise GitHubError("Only repository writers may request this operation")

    async def require_current(self, repo: str, number: int, head: str) -> dict[str, Any]:
        pr = await self.get_pull_request(repo, number)
        if pr.get("state") != "open" or pr["head"]["sha"] != _sha(head):
            raise GitHubError("Pull request changed or closed; refresh the review")
        return pr

    def _own_marker(self, row: dict[str, Any], marker: str) -> bool:
        return row.get("user", {}).get("login") == self.bot_login and marker in row.get("body", "")

    async def _upsert_summary(self, repo: str, number: int, head: str, body: str) -> int:
        path = f"/repos/{repo}/issues/{number}/comments"
        rows = await self._pages(path)
        existing = next((r for r in rows if self._own_marker(r, SUMMARY_MARKER)), None)
        await self.require_current(repo, number, head)
        if existing:
            response = await self._request(
                "PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", json={"body": body}
            )
            return int(response.json()["id"])
        try:
            response = await self._request("POST", path, json={"body": body})
            return int(response.json()["id"])
        except (httpx.TransportError, GitHubError):
            # A failed response does not establish that GitHub rejected the write.
            rows = await self._pages(path)
            recovered = next((r for r in rows if self._own_marker(r, SUMMARY_MARKER)), None)
            if recovered:
                return int(recovered["id"])
            raise

    async def publish_review(
        self,
        result: ReviewResult,
        repo: str,
        number: int,
        *,
        inline_limit: int = 5,
        report_url: str = "",
    ) -> dict[str, Any]:
        repo = _repo(repo)
        head = result.snapshot.head_sha
        await self.require_current(repo, number, head)
        from codereview.reporting import plain, render_review

        report = render_review(result)
        footer = f"\n\n[Complete report and run artifacts]({report_url})" if report_url else ""
        if len(report) > 58_000:
            report = (
                report[:58_000] + "\n\nSummary truncated; the run artifact contains every finding."
            )
        body = f"{SUMMARY_MARKER}\nReviewed commit `{head}`\n\n{report}{footer}"
        summary_id = await self._upsert_summary(repo, number, head, body)
        path = f"/repos/{repo}/pulls/{number}/comments"
        existing = await self._pages(path)
        posted: list[str] = []
        priority = {"high": 0, "medium": 1, "low": 2}
        for finding in sorted(result.findings, key=lambda f: priority[f.severity]):
            if len(posted) >= inline_limit:
                break
            anchor = next(
                (
                    e
                    for e in finding.evidence
                    if e.start_line in result.snapshot.changed_lines.get(e.path, [])
                ),
                None,
            )
            if not anchor or finding.verification == "candidate":
                continue
            marker = f"<!-- codereview-agent:finding:{finding.id}:{head} -->"
            if any(self._own_marker(row, marker) for row in existing):
                posted.append(finding.id)
                continue
            await self.require_current(repo, number, head)
            comment = (
                f"{marker}\n**{finding.id} · {finding.kind} · {finding.severity}** — "
                f"{plain(finding.title)}\n\n{plain(finding.observation)}\n\n"
                f"{plain(finding.rationale)}\n\n"
                f"Suggested change: {plain(finding.alternative)}"
            )[:60_000]
            try:
                await self._request(
                    "POST",
                    path,
                    json={
                        "commit_id": head,
                        "path": anchor.path,
                        "line": anchor.start_line,
                        "side": "RIGHT",
                        "body": comment,
                    },
                )
            except (httpx.TransportError, GitHubError):
                recovered = await self._pages(path)
                if not any(self._own_marker(row, marker) for row in recovered):
                    raise
            posted.append(finding.id)
        return {"summary_comment_id": summary_id, "inline_finding_ids": posted}

    async def _download_archive(self, repo: str, artifact_id: int) -> bytes:
        response = await self._request("GET", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip")
        if response.is_redirect:
            location = response.headers.get("location", "")
            if not location.startswith("https://"):
                raise GitHubError("GitHub returned an invalid artifact download location")
            # The signed storage URL is authoritative only because it came from GitHub.
            # Authentication headers are intentionally not sent to object storage.
            chunks: list[bytes] = []
            total = 0
            async with self._client.stream("GET", location, follow_redirects=True) as download:
                download.raise_for_status()
                async for chunk in download.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise GitHubError("Review archive exceeds size limit")
                    chunks.append(chunk)
            return b"".join(chunks)
        if len(response.content) > MAX_ARCHIVE_BYTES:
            raise GitHubError("Review archive exceeds size limit")
        return response.content

    async def load_review_artifact(
        self, repo: str, number: int, *, config_digest: str
    ) -> ReviewResult:
        repo = _repo(repo)
        pr = await self.get_pull_request(repo, number)
        head = _sha(pr["head"]["sha"])
        base_branch = pr["base"]["ref"]
        runs = await self._pages(
            f"/repos/{repo}/actions/workflows/review.yml/runs",
            field="workflow_runs",
            params={"event": "pull_request_target", "status": "success"},
        )
        name = f"codereview-pr-{number}-{head}"
        for run in runs:
            if (
                run.get("conclusion") != "success"
                or run.get("event") != "pull_request_target"
                or run.get("path", "").split("@")[0] != WORKFLOW_PATH
                or run.get("repository", {}).get("full_name") != repo
                or run.get("head_sha") != head
                or run.get("head_repository", {}).get("full_name")
                != pr["head"]["repo"]["full_name"]
                or run.get("head_branch") != pr["head"]["ref"]
            ):
                continue
            actor = run.get("actor", {}).get("login")
            if not actor:
                continue
            artifacts = await self._pages(
                f"/repos/{repo}/actions/runs/{run['id']}/artifacts", field="artifacts"
            )
            for artifact in artifacts:
                if artifact.get("name") != name or artifact.get("expired"):
                    continue
                if artifact.get("size_in_bytes", 0) > MAX_ARCHIVE_BYTES:
                    raise GitHubError("Review archive exceeds size limit")
                payload = await self._download_archive(repo, int(artifact["id"]))
                result = read_review_archive(
                    payload,
                    repo=repo,
                    number=number,
                    head=head,
                    base=_sha(pr["base"]["sha"]),
                    config_digest=config_digest,
                    actions_run_id=int(run["id"]),
                    workflow_ref=f"{repo}/{WORKFLOW_PATH}@refs/heads/{base_branch}",
                    workflow_sha=pr["base"]["sha"],
                    actor=actor,
                )
                if result.status == "failed":
                    raise GitHubError(
                        "The review failed; run a fresh review before requesting fixes"
                    )
                return result
        raise GitHubError("No trusted current review artifact found; run a fresh review")

    async def create_fix_pr(
        self,
        result: FixResult,
        repo: str,
        number: int,
        *,
        head_sha: str,
        request_id: int,
        requester: str,
    ) -> dict[str, Any]:
        """Publish an agent-only tree as one commit, then a draft PR targeting the source branch."""
        repo = _repo(repo)
        await self.require_writer(repo, requester)
        pr = await self.require_current(repo, number, head_sha)
        if pr["head"].get("repo", {}).get("full_name") != repo:
            raise GitHubError("Fork fixes are not supported in version one")
        if (
            result.status == "failed"
            or not result.worktree
            or not result.commit_sha
            or result.baseline_commit_sha != head_sha
        ):
            raise GitHubError(
                "Fix cannot be published: missing commit or a synthetic/obsolete baseline"
            )
        worktree = Path(result.worktree)
        _sha(result.commit_sha)
        if _git(worktree, "rev-parse", "HEAD").decode().strip() != result.commit_sha:
            raise GitHubError("Fix worktree no longer matches its recorded commit")
        _git(worktree, "merge-base", "--is-ancestor", head_sha, result.commit_sha)
        records = (
            _git(
                worktree, "diff", "--name-status", "-z", "--no-renames", head_sha, result.commit_sha
            )
            .decode()
            .split("\0")
        )
        if len(records) < 2:
            raise GitHubError("Fix has no changes to publish")
        tree_entries: list[dict[str, Any]] = []
        for index in range(0, len(records) - 1, 2):
            change, path = records[index : index + 2]
            parts = PurePosixPath(path).parts
            if not parts or any(p in {"..", ".git"} for p in parts) or path.startswith("/"):
                raise GitHubError("Fix contains an invalid path")
            if change == "D":
                tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            listing = _git(worktree, "ls-tree", result.commit_sha, "--", path).decode()
            mode = listing.split()[0]
            if mode not in {"100644", "100755"}:
                raise GitHubError("Fix contains unsupported symlink or submodule changes")
            content = _git(worktree, "show", f"{result.commit_sha}:{path}")
            blob = await self._request(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            tree_entries.append(
                {"path": path, "mode": mode, "type": "blob", "sha": blob.json()["sha"]}
            )
        parent = (await self._request("GET", f"/repos/{repo}/git/commits/{head_sha}")).json()
        tree = (
            await self._request(
                "POST",
                f"/repos/{repo}/git/trees",
                json={"base_tree": parent["tree"]["sha"], "tree": tree_entries},
            )
        ).json()
        branch = f"codereview/fix-pr-{number}-request-{request_id}"
        marker = f"<!-- codereview-agent:fix:{number}:{request_id} -->"
        ref_path = f"/repos/{repo}/git/ref/heads/{branch}"
        existing = await self._request("GET", ref_path, accepted=(404,))
        if existing.status_code == 404:
            await self.require_current(repo, number, head_sha)
            commit = (
                await self._request(
                    "POST",
                    f"/repos/{repo}/git/commits",
                    json={
                        "message": f"Apply requested review fixes\n\n{marker}",
                        "tree": tree["sha"],
                        "parents": [head_sha],
                    },
                )
            ).json()
            await self._request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        else:
            commit = (
                await self._request(
                    "GET", f"/repos/{repo}/git/commits/{existing.json()['object']['sha']}"
                )
            ).json()
            if marker not in commit["message"] or commit["tree"]["sha"] != tree["sha"]:
                raise GitHubError(
                    "This fix request already has different published changes; inspect its branch"
                )
        await self.require_current(repo, number, head_sha)
        owner = repo.split("/")[0]
        pull_requests = await self._pages(
            f"/repos/{repo}/pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "base": pr["head"]["ref"]},
        )
        previous = next((p for p in pull_requests if marker in p.get("body", "")), None)
        if previous:
            return previous
        from codereview.reporting import render_fix

        published_result = result.model_copy(update={"commit_sha": commit["sha"], "branch": branch})
        body = (
            f"{marker}\nRequested by @{requester} for #{number} at `{head_sha}`.\n\n"
            + render_fix(published_result)
        )[:60_000]
        try:
            return (
                await self._request(
                    "POST",
                    f"/repos/{repo}/pulls",
                    json={
                        "title": f"Review fixes for #{number}",
                        "head": branch,
                        "base": pr["head"]["ref"],
                        "body": body,
                        "draft": True,
                    },
                )
            ).json()
        except (httpx.TransportError, GitHubError):
            pull_requests = await self._pages(
                f"/repos/{repo}/pulls",
                params={"state": "all", "head": f"{owner}:{branch}", "base": pr["head"]["ref"]},
            )
            previous = next((p for p in pull_requests if marker in p.get("body", "")), None)
            if previous:
                return previous
            raise


def parse_fix_command(body: str) -> list[str] | None:
    """Return selected IDs, an empty list for fix-all, or None for non-commands."""
    command = body.strip()
    if command == "/review fix-all":
        return []
    if not re.fullmatch(r"/review fix(?: F[A-Za-z0-9_-]+)+", command):
        return None
    return list(dict.fromkeys(command.split()[2:]))


def write_review_artifact(
    output: Path,
    result: ReviewResult,
    *,
    repo: str,
    number: int,
    base_sha: str,
    actions_run_id: int,
    workflow_ref: str,
    workflow_sha: str,
    actor: str,
) -> None:
    """Write the trusted manifest alongside the revision-bound result, never credentials."""
    output.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump_json(indent=2).encode()
    manifest = {
        "schema_version": 1,
        "repo": _repo(repo),
        "number": number,
        "head": _sha(result.snapshot.head_sha),
        "config_digest": result.config_digest,
        "base": _sha(base_sha),
        "merge_base": _sha(result.snapshot.base_sha),
        "actions_run_id": actions_run_id,
        "workflow_ref": workflow_ref,
        "workflow_sha": _sha(workflow_sha),
        "actor": actor,
        "result_sha256": hashlib.sha256(payload).hexdigest(),
        "review_run_id": result.run_id,
    }
    (output / "review.json").write_bytes(payload)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))


def read_review_archive(
    data: bytes,
    *,
    repo: str,
    number: int,
    head: str,
    base: str,
    config_digest: str,
    actions_run_id: int,
    workflow_ref: str,
    workflow_sha: str,
    actor: str,
) -> ReviewResult:
    """Read only expected bounded members, then verify API-derived provenance and checksum."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise GitHubError("Review archive exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) - {
                "review.json",
                "manifest.json",
                "review.md",
            }:
                raise GitHubError("Unexpected or duplicate archive member")
            if not {"review.json", "manifest.json"}.issubset(names):
                raise GitHubError("Review archive is incomplete")
            if sum(entry.file_size for entry in archive.infolist()) > MAX_ARCHIVE_BYTES:
                raise GitHubError("Expanded review archive exceeds size limit")
            for entry in archive.infolist():
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise GitHubError("Symlink archive member is not allowed")
            manifest = json.loads(archive.read("manifest.json"))
            payload = archive.read("review.json")
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        raise GitHubError("Invalid review artifact") from exc
    expected = dict(
        schema_version=1,
        repo=_repo(repo),
        number=number,
        head=_sha(head),
        base=_sha(base),
        config_digest=config_digest,
        actions_run_id=actions_run_id,
        workflow_ref=workflow_ref,
        workflow_sha=_sha(workflow_sha),
        actor=actor,
    )
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise GitHubError("Review artifact provenance does not match the trusted run or revision")
    if manifest.get("result_sha256") != hashlib.sha256(payload).hexdigest():
        raise GitHubError("Review artifact checksum mismatch")
    try:
        result = ReviewResult.model_validate_json(payload)
    except ValueError as exc:
        raise GitHubError("Review artifact has an invalid result schema") from exc
    if (
        result.snapshot.head_sha != head
        or result.snapshot.base_sha != manifest.get("merge_base")
        or result.config_digest != config_digest
        or result.run_id != manifest.get("review_run_id")
    ):
        raise GitHubError("Review artifact result identity does not match its manifest")
    return result


def _git(root: Path, *args: str, token: str | None = None, timeout: float = 120) -> bytes:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "OPENROUTER_", "GITHUB_", "CODE_REVIEW_"))
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
    )
    if token:
        # Use a child-only configuration entry; no token in a URL, argument or persisted config.
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {credential}",
        )
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "core.pager=cat",
                *args,
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=max(1, timeout),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"Git {args[0] if args else 'operation'} failed") from exc
    return completed.stdout


def _prepare_repository(root: Path, repo: str, number: int, pr: dict[str, Any], token: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".codereview-github-workspace"
    if root.is_symlink() or marker.is_symlink():
        raise GitHubError("GitHub workspace must not be a symlink")
    if any(root.iterdir()) and (not marker.is_file() or marker.read_text() != repo):
        raise GitHubError(
            "GitHub target must be an empty disposable directory, not a user's checkout"
        )
    marker.write_text(repo)
    if not (root / ".git").exists():
        _git(root, "init", "--quiet")
    url = f"https://github.com/{_repo(repo)}.git"
    _git(root, "fetch", "--no-tags", url, _sha(pr["base"]["sha"]), token=token)
    _git(root, "fetch", "--no-tags", url, f"refs/pull/{number}/head", token=token)
    if _git(root, "rev-parse", "FETCH_HEAD").decode().strip() != pr["head"]["sha"]:
        raise GitHubError("Pull request changed during fetch; retry with a fresh revision")
    _git(root, "checkout", "--quiet", "--detach", "--force", _sha(pr["head"]["sha"]))


def _trusted_settings(root: Path, base_sha: str) -> Settings:
    from codereview.config import load_settings

    paths = _git(root, "ls-tree", "--name-only", _sha(base_sha), "--", ".codereview.toml").decode()
    text = _git(root, "show", f"{base_sha}:.codereview.toml").decode() if paths.strip() else ""
    return load_settings(root, trusted_text=text)


async def run_event(
    repo_root: Path,
    engine_root: Path,
    output: Path,
    event: dict[str, Any] | None = None,
    event_name: str | None = None,
) -> ReviewResult | FixResult | None:
    """Execute a trusted Actions event; model/GitHub secrets never reach repository tests."""
    from codereview.budgets import Budget
    from codereview.engine import review
    from codereview.fixes import fix
    from codereview.providers import OpenRouterClient
    from codereview.reporting import render_fix, render_review
    from codereview.snapshots import capture_snapshot

    started = time.monotonic()
    if repo_root.resolve() == engine_root.resolve():
        raise GitHubError("Use separate directories for trusted engine and reviewed repository")
    if event is None:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            raise GitHubError("GITHUB_EVENT_PATH is required")
        event = json.loads(Path(event_path).read_text())
    event_name = event_name or os.environ.get("GITHUB_EVENT_NAME", "")
    repo = _repo(os.environ.get("GITHUB_REPOSITORY", ""))
    token = os.environ.get("GITHUB_TOKEN", "")
    output.mkdir(parents=True, exist_ok=True)
    async with GitHubClient(token) as github:
        if event_name == "pull_request_target":
            if event.get("action") not in {
                "opened",
                "synchronize",
                "reopened",
                "ready_for_review",
                "labeled",
            }:
                return None
            if (
                event.get("action") == "labeled"
                and event.get("label", {}).get("name") != "codereview"
            ):
                return None
            number = int(event["number"])
            pr = await github.get_pull_request(repo, number)
            if pr.get("draft") or pr.get("state") != "open":
                return None
            triggered = event.get("pull_request", {})
            if (
                triggered.get("head", {}).get("sha") != pr["head"]["sha"]
                or triggered.get("base", {}).get("sha") != pr["base"]["sha"]
            ):
                raise GitHubError("Pull request changed since this event; trigger a fresh review")
            fork = pr["head"].get("repo", {}).get("full_name") != repo
            if fork:
                if (
                    event.get("action") != "labeled"
                    or event.get("label", {}).get("name") != "codereview"
                ):
                    return None
                await github.require_writer(repo, event.get("sender", {}).get("login", ""))
            await asyncio.to_thread(_prepare_repository, repo_root, repo, number, pr, token)
            settings = _trusted_settings(repo_root, pr["base"]["sha"])
            remaining = settings.review.seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise GitHubError("Review deadline exhausted while acquiring the repository")
            budget = Budget(
                settings.review.dollars, remaining, settings.models.coordinator_monthly_dollars
            )
            snapshot = capture_snapshot(
                repo_root,
                mode="branch",
                base=pr["base"]["sha"],
                head=pr["head"]["sha"],
                settings=settings,
            )
            await github.require_current(repo, number, snapshot.head_sha)
            model = OpenRouterClient(settings, operation="review", budget=budget)
            try:
                # The engine owns the deadline and preserves gathered findings. This
                # guard only catches a stuck provider/finalizer after its own limit.
                async with asyncio.timeout(budget.remaining_seconds() + 1):
                    async with model:
                        result = await review(snapshot, settings, model)
            except TimeoutError:
                result = ReviewResult(
                    snapshot=snapshot,
                    status="partial",
                    config_digest=settings.digest(),
                    summary="Review deadline reached before all work completed.",
                    gaps=["End-to-end deadline exhausted"],
                    usage=model.usage(),
                    call_records=model.call_records,
                )
            actions_run_id = int(os.environ["GITHUB_RUN_ID"])
            result.github = {
                "repository": repo,
                "pull_request": number,
                "actions_run_id": actions_run_id,
                "base_branch": pr["base"]["ref"],
                "base_tip_sha": pr["base"]["sha"],
            }
            write_review_artifact(
                output,
                result,
                repo=repo,
                number=number,
                base_sha=pr["base"]["sha"],
                actions_run_id=actions_run_id,
                workflow_ref=os.environ["GITHUB_WORKFLOW_REF"],
                workflow_sha=os.environ["GITHUB_WORKFLOW_SHA"],
                actor=os.environ["GITHUB_ACTOR"],
            )
            (output / "review.md").write_text(render_review(result))
            action_output = os.environ.get("GITHUB_OUTPUT")
            if action_output:
                with Path(action_output).open("a") as handle:
                    handle.write(f"artifact_name=codereview-pr-{number}-{snapshot.head_sha}\n")
            await github.publish_review(
                result,
                repo,
                number,
                inline_limit=settings.inline_limit,
                report_url=f"https://github.com/{repo}/actions/runs/{actions_run_id}",
            )
            return result
        if event_name != "issue_comment" or event.get("action") != "created":
            return None
        if "pull_request" not in event.get("issue", {}):
            return None
        number = int(event["issue"]["number"])
        comment_id = int(event["comment"]["id"])
        comment = (
            await github._request("GET", f"/repos/{repo}/issues/comments/{comment_id}")
        ).json()
        if comment.get("issue_url") != f"https://api.github.com/repos/{repo}/issues/{number}":
            raise GitHubError("Fix command does not belong to this pull request")
        selected = parse_fix_command(comment.get("body", ""))
        if selected is None:
            return None
        requester = comment.get("user", {}).get("login", "")
        await github.require_writer(repo, requester)
        pr = await github.get_pull_request(repo, number)
        if pr["head"].get("repo", {}).get("full_name") != repo:
            raise GitHubError("Fork fixes are not supported in version one")
        if pr.get("state") != "open":
            raise GitHubError("Pull request is closed")
        await asyncio.to_thread(_prepare_repository, repo_root, repo, number, pr, token)
        settings = _trusted_settings(repo_root, pr["base"]["sha"])
        original = await github.load_review_artifact(repo, number, config_digest=settings.digest())
        original.snapshot.root = str(repo_root.resolve())
        await github.require_current(repo, number, original.snapshot.head_sha)
        remaining = settings.fix.seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise GitHubError("Fix deadline exhausted while acquiring its review")
        budget = Budget(
            settings.fix.dollars, remaining, settings.models.coordinator_monthly_dollars
        )
        model = OpenRouterClient(settings, operation="fix", budget=budget)
        try:
            async with asyncio.timeout(budget.remaining_seconds() + 1):
                async with model:
                    repaired = await fix(
                        original, selected, settings, model, all_findings=not selected
                    )
        except TimeoutError:
            repaired = FixResult(
                review_run_id=original.run_id,
                snapshot_id=original.snapshot.id,
                selected_ids=selected or [f.id for f in original.findings],
                status="draft",
                notes=["End-to-end deadline exhausted; no fix was published"],
                usage=model.usage(),
                call_records=model.call_records,
            )
        (output / "fix.json").write_text(repaired.model_dump_json(indent=2))
        (output / "fix.md").write_text(render_fix(repaired))
        if repaired.commit_sha and repaired.status != "failed":
            publication_token = os.environ.get("CODE_REVIEW_APP_TOKEN") or token
            async with GitHubClient(publication_token) as publisher:
                published = await publisher.create_fix_pr(
                    repaired,
                    repo,
                    number,
                    head_sha=original.snapshot.head_sha,
                    request_id=comment_id,
                    requester=requester,
                )
            (output / "pull-request.json").write_text(json.dumps(published, indent=2))
        return repaired
