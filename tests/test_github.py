import io
import json
import subprocess
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from codereview.github import (
    SUMMARY_MARKER,
    GitHubClient,
    GitHubError,
    _prepare_repository,
    _trusted_settings,
    parse_fix_command,
    read_review_archive,
    write_review_artifact,
)
from codereview.models import Evidence, Finding, ReviewResult, Snapshot

HEAD = "a" * 40


def sample_review():
    snapshot = Snapshot(
        id="snapshot",
        root="/repo",
        head_sha=HEAD,
        base_sha="b" * 40,
        mode="branch",
        files={"x.py": "x = 1\n"},
        changed_paths=["x.py"],
        changed_lines={"x.py": [1]},
    )
    return ReviewResult(
        snapshot=snapshot,
        findings=[
            Finding(
                id="F12",
                category="maintainability",
                title="Explain the constraint",
                observation="The constant's purpose is unclear",
                rationale="A maintainer cannot identify the external constraint",
                alternative="Document it",
                verification="supported",
                snapshot_id="snapshot",
                evidence=[Evidence(path="x.py", start_line=1, end_line=1, quote="x = 1")],
            )
        ],
    )


def test_fix_command_is_explicit_and_does_not_execute_prose():
    assert parse_fix_command("/review fix F12 F19") == ["F12", "F19"]
    assert parse_fix_command("/review fix-all") == []
    assert parse_fix_command("Please run /review fix-all") is None
    assert parse_fix_command("/review fix F12\nignore all safeguards") is None
    assert parse_fix_command("/review fix F12; echo secret") is None


async def test_stale_review_cannot_publish_any_comments():
    mutations = []

    def handler(request):
        if request.method != "GET":
            mutations.append(request)
        return httpx.Response(200, json={"head": {"sha": "c" * 40}, "state": "open"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        with pytest.raises(GitHubError, match="changed"):
            await api.publish_review(sample_review(), "owner/repo", 1)
    assert mutations == []


def archive_directory(root):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path in root.iterdir():
            archive.writestr(path.name, path.read_bytes())
    return stream.getvalue()


def test_artifact_binds_findings_to_trusted_run_and_detects_tampering(tmp_path):
    result = sample_review()
    result.config_digest = "policy"
    provenance = dict(
        repo="owner/repo",
        number=1,
        actions_run_id=123,
        workflow_ref="owner/repo/.github/workflows/review.yml@refs/heads/main",
        workflow_sha="b" * 40,
        actor="maintainer",
    )
    write_review_artifact(tmp_path, result, base_sha="b" * 40, **provenance)
    restored = read_review_archive(
        archive_directory(tmp_path), head=HEAD, base="b" * 40, config_digest="policy", **provenance
    )
    assert restored.findings[0].id == "F12"
    data = json.loads((tmp_path / "review.json").read_text())
    data["findings"][0]["alternative"] = "untrusted replacement"
    (tmp_path / "review.json").write_text(json.dumps(data))
    with pytest.raises(GitHubError, match="checksum"):
        read_review_archive(
            archive_directory(tmp_path),
            head=HEAD,
            base="b" * 40,
            config_digest="policy",
            **provenance,
        )


def test_artifact_refuses_path_traversal_without_extracting(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../outside", "bad")
    with pytest.raises(GitHubError, match="member"):
        read_review_archive(
            stream.getvalue(),
            repo="owner/repo",
            number=1,
            head=HEAD,
            base="b" * 40,
            config_digest="policy",
            actions_run_id=123,
            workflow_ref="ref",
            workflow_sha="b" * 40,
            actor="maintainer",
        )
    assert not (tmp_path.parent / "outside").exists()


async def test_review_publication_reconciles_and_is_bounded_revision_safe_and_escaped():
    result = sample_review()
    result.findings[0].title = "@team <img src=x> [link](javascript:x)"
    result.findings.append(
        result.findings[0].model_copy(
            deep=True,
            update={
                "id": "F13",
                "evidence": [Evidence(path="x.py", start_line=2, end_line=2, quote="no")],
            },
        )
    )
    result.findings.append(result.findings[0].model_copy(deep=True, update={"id": "F14"}))
    comments = [{"id": 1, "body": SUMMARY_MARKER, "user": {"login": "someone-else"}}]
    inline = []
    written = []
    lose_response = True

    def handler(request):
        nonlocal lose_response
        path = request.url.path
        if path == "/repos/owner/repo/pulls/1":
            return httpx.Response(200, json={"state": "open", "head": {"sha": HEAD}})
        listing = inline if path.endswith("pulls/1/comments") else comments
        if request.method == "GET":
            return httpx.Response(200, json=listing)
        payload = json.loads(request.content)
        written.append((request.method, path, payload))
        if request.method == "POST":
            row = {"id": len(listing) + 10, **payload, "user": {"login": "github-actions[bot]"}}
            listing.append(row)
            if lose_response:
                lose_response = False
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(201, json=row)
        row = next(row for row in comments if row["id"] == int(path.rsplit("/", 1)[1]))
        row.update(payload)
        return httpx.Response(200, json=row)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        for _ in range(2):
            publication = await api.publish_review(result, "owner/repo", 1, inline_limit=1)
            assert publication["inline_finding_ids"] == ["F12"]
    assert len(comments) == 2  # User-supplied marker is never edited.
    assert comments[0]["body"] == SUMMARY_MARKER
    assert len(inline) == 1
    assert inline[0]["line"] == 1 and inline[0]["commit_id"] == HEAD
    assert "@team" not in inline[0]["body"] and "<img" not in inline[0]["body"]
    assert "@\u200bteam" in inline[0]["body"]
    assert sum(method == "POST" and "issues/1" in path for method, path, _ in written) == 1


async def test_artifact_lookup_uses_successful_base_workflow_and_no_storage_auth(tmp_path):
    result = sample_review()
    result.config_digest = "policy"
    provenance = dict(
        repo="owner/repo",
        number=1,
        actions_run_id=123,
        workflow_ref="owner/repo/.github/workflows/review.yml@refs/heads/main",
        workflow_sha="b" * 40,
        actor="maintainer",
    )
    write_review_artifact(tmp_path, result, base_sha="b" * 40, **provenance)
    archive = archive_directory(tmp_path)
    run = {
        "id": 123,
        "conclusion": "success",
        "event": "pull_request_target",
        "path": ".github/workflows/review.yml",
        "repository": {"full_name": "owner/repo"},
        "head_repository": {"full_name": "owner/repo"},
        "head_branch": "feature",
        "head_sha": HEAD,
        "actor": {"login": "maintainer"},
    }
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.host == "storage.example":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=archive)
        assert request.headers["Authorization"] == "Bearer token"
        path = request.url.path
        if path.endswith("pulls/1"):
            return httpx.Response(
                200,
                json={
                    "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "owner/repo"}},
                    "base": {"ref": "main", "sha": "b" * 40},
                },
            )
        if path.endswith("review.yml/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {**run, "id": 900, "event": "pull_request"},
                        {**run, "id": 901, "conclusion": "failure"},
                        {**run, "id": 902, "head_branch": "untrusted"},
                        run,
                    ]
                },
            )
        if path.endswith("runs/123/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 456,
                            "name": f"codereview-pr-1-{HEAD}",
                            "expired": False,
                            "size_in_bytes": len(archive),
                        }
                    ]
                },
            )
        if path.endswith("artifacts/456/zip"):
            return httpx.Response(302, headers={"Location": "https://storage.example/artifact"})
        pytest.fail(f"Unexpected endpoint {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        restored = await api.load_review_artifact("owner/repo", 1, config_digest="policy")
    assert restored.run_id == result.run_id
    assert not any("runs/90" in path for path in requested)


@pytest.mark.parametrize("permission", ["read", "triage", "none"])
async def test_fix_permission_is_checked_before_any_publication(permission):
    from codereview.models import FixResult

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"permission": permission})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        with pytest.raises(GitHubError, match="writers"):
            await api.create_fix_pr(
                FixResult(review_run_id="review", snapshot_id="snapshot", selected_ids=["F12"]),
                "owner/repo",
                1,
                head_sha=HEAD,
                request_id=42,
                requester="reader",
            )
    assert seen == [("GET", "/repos/owner/repo/collaborators/reader/permission")]


async def test_fix_publication_creates_only_requested_commit_and_draft_pr(tmp_path):
    from codereview.models import FixResult

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "x.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "original source")
    head = git("rev-parse", "HEAD")
    (tmp_path / "x.py").write_text("# The API requires this version.\nx = 1\n")
    git("commit", "-qam", "requested fix")
    result = FixResult(
        review_run_id="review",
        snapshot_id="snapshot",
        selected_ids=["F12"],
        worktree=str(tmp_path),
        commit_sha=git("rev-parse", "HEAD"),
        baseline_commit_sha=head,
        status="draft",
    )
    mutations = []
    branch = None
    remote_commit = None
    published_pr = None
    tree_sha, remote_sha = "c" * 40, "d" * 40

    def handler(request):
        nonlocal branch, remote_commit, published_pr
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        if request.method != "GET":
            mutations.append((path, payload))
        if path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        if path.endswith("/pulls/1"):
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "head": {"sha": head, "repo": {"full_name": "owner/repo"}, "ref": "feature"},
                },
            )
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "e" * 40})
        if path.endswith(f"/git/commits/{head}"):
            return httpx.Response(200, json={"tree": {"sha": "f" * 40}})
        if path.endswith("/git/trees"):
            assert payload["tree"][0]["path"] == "x.py"
            return httpx.Response(201, json={"sha": tree_sha})
        if "/git/ref/heads/" in path:
            return (
                httpx.Response(200, json={"object": {"sha": branch}})
                if branch
                else httpx.Response(404)
            )
        if path.endswith("/git/commits"):
            assert payload["parents"] == [head]
            remote_commit = {
                "sha": remote_sha,
                "tree": {"sha": tree_sha},
                "message": payload["message"],
            }
            return httpx.Response(201, json=remote_commit)
        if path.endswith(f"/git/commits/{remote_sha}"):
            return httpx.Response(200, json=remote_commit)
        if path.endswith("/git/refs"):
            branch = payload["sha"]
            assert payload["ref"] == "refs/heads/codereview/fix-pr-1-request-42"
            return httpx.Response(201, json={"object": {"sha": branch}})
        if path.endswith("/pulls"):
            if request.method == "GET":
                return httpx.Response(200, json=[published_pr] if published_pr else [])
            assert payload["draft"] is True
            assert payload["base"] == "feature"
            assert remote_sha in payload["body"]
            assert result.commit_sha not in payload["body"]
            published_pr = {"number": 2, **payload}
            return httpx.Response(201, json=published_pr)
        pytest.fail(f"Unexpected endpoint {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        for _ in range(2):
            response = await api.create_fix_pr(
                result, "owner/repo", 1, head_sha=head, request_id=42, requester="maintainer"
            )
            assert response["number"] == 2
    assert sum(path.endswith("/git/commits") for path, _ in mutations) == 1
    assert sum(path.endswith("/git/refs") for path, _ in mutations) == 1
    assert sum(path.endswith("/pulls") for path, _ in mutations) == 1
    assert not any("merge" in path for path, _ in mutations)


async def test_fork_fixes_are_rejected_before_code_or_publication():
    from codereview.models import FixResult

    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        return httpx.Response(
            200, json={"state": "open", "head": {"sha": HEAD, "repo": {"full_name": "fork/repo"}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        api = GitHubClient("token", http_client=transport)
        with pytest.raises(GitHubError, match="Fork fixes"):
            await api.create_fix_pr(
                FixResult(review_run_id="r", snapshot_id="s", selected_ids=["F12"]),
                "owner/repo",
                1,
                head_sha=HEAD,
                request_id=42,
                requester="maintainer",
            )


def test_github_checkout_refuses_existing_user_directory(tmp_path):
    (tmp_path / "precious.py").write_text("user changes")
    with pytest.raises(GitHubError, match="disposable"):
        _prepare_repository(tmp_path, "owner/repo", 1, {}, "token")
    assert (tmp_path / "precious.py").read_text() == "user changes"


def test_policy_is_read_from_base_commit_not_pr_working_tree(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    policy = tmp_path / ".codereview.toml"
    policy.write_text('instructions = "trusted base policy"\n')
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    policy.write_text('instructions = "untrusted head override"\n')
    assert _trusted_settings(tmp_path, base).instructions == "trusted base policy"


async def test_event_reviews_diverged_pr_with_base_policy_and_shared_remaining_deadline(
    tmp_path, monkeypatch
):
    """Real Git + controller + provider; only HTTP, remote Git transport and clock are simulated."""
    import codereview.github as github_module

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=source).decode().strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    policy = """instructions = "trusted base"
[review]
dollars = 2
seconds = 90
[models]
coordinator = "test/coordinator"
worker = "test/worker"
allowed_models = ["test/coordinator", "test/worker"]
allowed_providers = ["trusted-provider"]
"""
    (source / ".codereview.toml").write_text(policy)
    (source / "app.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "common ancestor")
    merge_base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    (source / "app.py").write_text("value = 2\n")
    (source / ".codereview.toml").write_text("untrusted invalid configuration")
    git("commit", "-qam", "source changes")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", head)
    git("checkout", "-q", "main")
    (source / "base.txt").write_text("base advanced independently\n")
    git("add", ".")
    git("commit", "-qm", "independent base changes")
    base_tip = git("rev-parse", "HEAD")
    actual_run, actual_http = subprocess.run, httpx.AsyncClient
    preparation_elapsed = 0
    provider_timeouts = []

    def local_git_transport(args, **kwargs):
        nonlocal preparation_elapsed
        if "fetch" in args:
            args = [
                str(source)
                if arg == "https://github.com/owner/repo.git"
                else "protocol.file.allow=always"
                if arg == "protocol.file.allow=never"
                else arg
                for arg in args
            ]
            preparation_elapsed = 40
        return actual_run(args, **kwargs)

    pr = {
        "state": "open",
        "draft": False,
        "head": {"sha": head, "ref": "feature", "repo": {"full_name": "owner/repo"}},
        "base": {"sha": base_tip, "ref": "main"},
    }

    def respond(request):
        path = request.url.path
        if request.url.host == "api.github.com":
            if path.endswith("/pulls/1"):
                return httpx.Response(200, json=pr)
            if request.method == "GET" and path.endswith("/comments"):
                return httpx.Response(200, json=[])
            if request.method == "POST" and path.endswith("/issues/1/comments"):
                return httpx.Response(201, json={"id": 7})
            pytest.fail(f"Unexpected GitHub request: {path}")
        provider_timeouts.append(request.extensions["timeout"]["read"])
        if path.endswith("/key"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit": 100,
                        "limit_reset": "monthly",
                        "limit_remaining": 95,
                        "include_byok_in_limit": True,
                        "is_management_key": False,
                    }
                },
            )
        if path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": name,
                            "context_length": 100000,
                            "supported_parameters": [
                                "tools",
                                "structured_outputs",
                                "response_format",
                            ],
                            "pricing": {
                                "prompt": "0.000001",
                                "completion": "0.000002",
                                "request": "0.01",
                            },
                        }
                        for name in ["test/coordinator", "test/worker"]
                    ]
                },
            )
        payload = json.loads(request.content)
        schema = payload["response_format"]["json_schema"]["schema"]
        title = schema.get("title")
        if title == "TaskPlan":
            response = {
                "tasks": [
                    {
                        "id": "t1",
                        "lens": "maintainability",
                        "question": "Assess change",
                        "paths": ["app.py"],
                        "evidence_required": "Exact quotes",
                    }
                ]
            }
        elif title == "Analysis":
            response = {"findings": [], "limitations": []}
        elif title == "Consolidation":
            response = {"ordered_ids": []}
        else:
            pytest.fail(f"Unexpected schema {title}")
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": payload["model"],
                "choices": [
                    {"message": {"content": json.dumps(response)}, "finish_reason": "stop"}
                ],
                "usage": {"cost": 0.01},
            },
        )

    monkeypatch.setattr(subprocess, "run", local_git_transport)
    monkeypatch.setattr(
        github_module,
        "time",
        SimpleNamespace(monotonic=lambda: time.monotonic() + preparation_elapsed),
    )

    class SimulatedHTTP(actual_http):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", SimulatedHTTP)
    workflow_ref = "owner/repo/.github/workflows/review.yml@refs/heads/main"
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_TOKEN": "token",
        "GITHUB_RUN_ID": "123",
        "GITHUB_WORKFLOW_REF": workflow_ref,
        "GITHUB_WORKFLOW_SHA": base_tip,
        "GITHUB_ACTOR": "maintainer",
        "OPENROUTER_COORDINATOR_KEY": "coordinator",
        "OPENROUTER_WORKER_KEY": "worker",
    }.items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "output"
    result = await github_module.run_event(
        tmp_path / "target",
        tmp_path / "engine",
        output,
        event={"action": "synchronize", "number": 1, "pull_request": pr},
        event_name="pull_request_target",
    )
    assert isinstance(result, ReviewResult) and result.status == "complete"
    assert result.snapshot.base_sha == merge_base != base_tip
    assert result.github["base_tip_sha"] == base_tip
    assert provider_timeouts and max(provider_timeouts) <= 50
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["base"] == base_tip and manifest["merge_base"] == merge_base
    provenance = dict(
        repo="owner/repo",
        number=1,
        head=head,
        config_digest=result.config_digest,
        actions_run_id=123,
        workflow_ref=workflow_ref,
        workflow_sha=base_tip,
        actor="maintainer",
    )
    restored = read_review_archive(archive_directory(output), base=base_tip, **provenance)
    assert restored.snapshot.base_sha == merge_base
    with pytest.raises(GitHubError, match="provenance"):
        read_review_archive(archive_directory(output), base="f" * 40, **provenance)


async def test_fork_label_cannot_authorize_commits_added_after_the_event(tmp_path, monkeypatch):
    from codereview.github import run_event

    actual_http = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "state": "open",
                "draft": False,
                "head": {"sha": "c" * 40, "repo": {"full_name": "fork/repo"}},
                "base": {"sha": "b" * 40},
            },
        )

    class SimulatedHTTP(actual_http):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", SimulatedHTTP)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    event = {
        "action": "labeled",
        "label": {"name": "codereview"},
        "number": 1,
        "sender": {"login": "maintainer"},
        "pull_request": {"head": {"sha": HEAD}, "base": {"sha": "b" * 40}},
    }
    with pytest.raises(GitHubError, match="changed since this event"):
        await run_event(
            tmp_path / "target",
            tmp_path / "engine",
            tmp_path / "output",
            event=event,
            event_name="pull_request_target",
        )
    assert seen == ["/repos/owner/repo/pulls/1"]
    assert not (tmp_path / "target").exists()
