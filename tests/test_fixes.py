import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from codereview.config import Check, Settings
from codereview.fixes import fix
from codereview.models import CheckResult, Evidence, Finding, ReviewResult, Snapshot, Usage
from codereview.snapshots import capture_snapshot


class ScriptedModels:
    def __init__(self, answers):
        self.answers = iter(answers)

    async def complete(self, **kwargs):
        assert kwargs["role"] == "worker"
        return next(self.answers)

    def usage(self):
        return Usage(worker_cost=0.01)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("result = 1 + 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def review_of(snapshot: Snapshot) -> ReviewResult:
    return ReviewResult(
        snapshot=snapshot,
        findings=[
            Finding(
                id="F1",
                category="maintainability",
                title="Use a named constant",
                observation="The total is unnamed",
                rationale="The name communicates the domain",
                alternative="Name the constant",
                evidence=[
                    Evidence(
                        path="app.py",
                        start_line=1,
                        end_line=1,
                        quote=snapshot.files["app.py"].splitlines()[0],
                    )
                ],
                snapshot_id=snapshot.id,
                verification="supported",
            )
        ],
    )


@pytest.mark.asyncio
async def test_unknown_finding_is_rejected_before_model_work(tmp_path):
    snapshot = Snapshot(
        id="snapshot",
        root=str(tmp_path),
        head_sha="a" * 40,
        base_sha="a" * 40,
        mode="working",
        files={"app.py": "a = 1\n"},
        changed_paths=["app.py"],
    )
    with pytest.raises(ValueError, match="Unknown finding"):
        await fix(review_of(snapshot), ["F99"], Settings(), ScriptedModels([]))


def answers(content, replacement, path="app.py"):
    return [
        {
            "edits": [
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "content": replacement,
                }
            ],
            "notes": [],
        },
        {
            "approved": True,
            "reason": "Requested naming improvement preserves the computation.",
            "unaddressed_ids": [],
        },
    ]


def passing_checks(root, checks, deadline):
    return [CheckResult(name=check.name, status="passed", exit_code=0) for check in checks]


@pytest.mark.asyncio
async def test_local_fix_commits_only_agent_changes_and_preserves_checkout_and_index(tmp_path):
    root = repo(tmp_path)
    (root / "app.py").write_text("result = 2 + 2\n")
    git(root, "add", "app.py")
    (root / "note.py").write_text("# user-owned untracked work\n")
    before_status = git(root, "status", "--porcelain")
    before_index = git(root, "diff", "--cached")
    snapshot = capture_snapshot(root, mode="working")
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels(answers("result = 2 + 2\n", "TOTAL = 2 + 2\n")),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "draft"  # An empty required-check set cannot establish validation.
    assert result.commit_sha
    assert result.baseline_commit_sha != snapshot.head_sha
    assert "-result = 2 + 2" in result.patch
    assert "+TOTAL = 2 + 2" in result.patch
    assert "note.py" not in result.patch
    assert (root / "app.py").read_text() == "result = 2 + 2\n"
    assert (tmp_path / "fixed" / "note.py").read_text() == "# user-owned untracked work\n"
    assert git(root, "status", "--porcelain") == before_status
    assert git(root, "diff", "--cached") == before_index
    assert git(root, "rev-parse", "HEAD") == snapshot.head_sha


@pytest.mark.asyncio
async def test_clean_pr_fix_descends_directly_from_reviewed_head(tmp_path):
    root = repo(tmp_path)
    base = git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("result = 2 + 2\n")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "PR changes")
    snapshot = capture_snapshot(root, mode="branch", base=base)
    settings = Settings(
        checks=[Check(name="tests", command=["python", "-m", "pytest"], image="python:3.12")]
    )
    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        ScriptedModels(answers("result = 2 + 2\n", "TOTAL = 2 + 2\n")),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "validated"
    assert result.baseline_commit_sha == snapshot.head_sha
    assert git(tmp_path / "fixed", "rev-parse", "HEAD^") == snapshot.head_sha


@pytest.mark.asyncio
async def test_stale_working_source_rejected_without_a_model_call(tmp_path):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    (root / "app.py").write_text("changed = True\n")
    with pytest.raises(ValueError, match="source changed"):
        await fix(review_of(snapshot), ["F1"], Settings(), ScriptedModels([]))


@pytest.mark.asyncio
async def test_invalid_edit_hash_or_path_cannot_write_outside_fix(tmp_path):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    malicious = {
        "edits": [{"path": "../escaped.py", "expected_sha256": None, "content": "escaped"}],
        "notes": [],
    }
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels([malicious]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "failed"
    assert result.commit_sha is None
    assert not (tmp_path / "escaped.py").exists()
    assert "Unsafe edit path" in result.notes[-1]


@pytest.mark.asyncio
async def test_failed_validation_retains_draft_and_compares_baseline(tmp_path):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    settings = Settings(checks=[Check(name="tests", command=["test"], image="python:3.12")])

    def failed(root, checks, deadline):
        return [
            CheckResult(name="tests", status="failed", exit_code=1, output="existing broken test")
        ]

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=failed,
    )
    assert result.status == "draft"
    assert result.commit_sha
    assert "also present on baseline" in " ".join(result.notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs", [("test_original failed", "test_regression failed"), ("", "")])
async def test_different_or_unidentified_failures_are_not_existing_baseline_failures(
    tmp_path, outputs
):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    settings = Settings(checks=[Check(name="tests", command=["test"], image="python:3.12")])
    remaining = iter(outputs)

    def failed(root, checks, deadline):
        return [CheckResult(name="tests", status="failed", exit_code=1, output=next(remaining))]

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=failed,
    )
    assert result.status == "draft"
    assert "new or unverified regression" in " ".join(result.notes)


@pytest.mark.asyncio
async def test_dirty_lockfile_and_binary_survive_isolated_baseline(tmp_path):
    root = repo(tmp_path)
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (root / "image.png").write_bytes(b"\x89PNG\x00\x01")
    snapshot = capture_snapshot(root, mode="audit")
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.commit_sha
    assert (tmp_path / "fixed" / "package-lock.json").read_text() == '{"lockfileVersion": 3}'
    assert (tmp_path / "fixed" / "image.png").read_bytes() == b"\x89PNG\x00\x01"
    assert "package-lock" not in result.patch


@pytest.mark.asyncio
async def test_all_edit_hashes_are_checked_before_any_edit_is_applied(tmp_path):
    root = repo(tmp_path)
    (root / "other.py").write_text("untouched = True\n")
    git(root, "add", "other.py")
    git(root, "commit", "-m", "another module")
    snapshot = capture_snapshot(root, mode="audit")
    plan = answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")[0]
    plan["edits"].append(
        {"path": "other.py", "expected_sha256": "0" * 64, "content": "overwritten = True\n"}
    )
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels([plan]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "failed"
    assert (tmp_path / "fixed" / "app.py").read_text() == "result = 1 + 1\n"
    assert (tmp_path / "fixed" / "other.py").read_text() == "untouched = True\n"


@pytest.mark.asyncio
async def test_requested_fix_can_add_a_suitable_dependency_manifest_without_extra_approval(
    tmp_path,
):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    plan = answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")
    plan[0]["edits"].append(
        {"path": "requirements.txt", "expected_sha256": None, "content": "httpx==0.28.1\n"}
    )
    result = await fix(
        review_of(snapshot),
        [],
        Settings(),
        ScriptedModels(plan),
        all_findings=True,
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.commit_sha
    assert "+httpx==0.28.1" in result.patch
    assert result.selected_ids == ["F1"]


@pytest.mark.asyncio
async def test_preserves_executable_bit_in_dirty_local_baseline(tmp_path):
    root = repo(tmp_path)
    script = root / "runner.py"
    script.write_text("#!/usr/bin/env python3\nprint('run')\n")
    script.chmod(0o755)
    snapshot = capture_snapshot(root, mode="audit")
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.commit_sha
    assert (tmp_path / "fixed" / "runner.py").stat().st_mode & 0o777 == 0o755
    assert "runner.py" not in result.patch


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["clean", "smudge", "process"])
@pytest.mark.parametrize("conditional", [False, True])
async def test_repository_git_filters_never_execute_on_host(tmp_path, driver, conditional):
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("*.py filter=probe\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-m", "attribute policy")
    marker = tmp_path / "filter-executed"
    script = tmp_path / "filter.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        f"Path({str(root / 'app.py')!r}).write_text('corrupted by filter')\n"
        + ("" if driver == "process" else "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    )
    arguments = []
    if conditional:
        included = tmp_path / "branch-filters.config"
        git(root, "config", "includeIf.onbranch:codereview/**.path", str(included))
        arguments = ["--file", str(included)]
    git(
        root,
        "config",
        *arguments,
        f"filter.probe.{driver}",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
    )
    git(root, "config", *arguments, "filter.probe.required", "true")
    snapshot = capture_snapshot(root, mode="audit")
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert not marker.exists()
    assert (root / "app.py").read_text() == "result = 1 + 1\n"
    assert result.commit_sha


@pytest.mark.asyncio
async def test_repair_patch_does_not_execute_repository_textconv(tmp_path):
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("*.py diff=probe\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-m", "diff attribute")
    marker = tmp_path / "textconv-executed"
    script = tmp_path / "textconv.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "sys.stdout.buffer.write(Path(sys.argv[-1]).read_bytes())\n"
    )
    git(
        root,
        "config",
        "diff.probe.textconv",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
    )
    snapshot = capture_snapshot(root, mode="audit")
    workspace = tmp_path / "fixed"
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=workspace,
        runner=passing_checks,
    )
    assert result.commit_sha
    assert not marker.exists()
    assert "+TOTAL = 1 + 1" in result.patch
    # Verify the real driver would execute without the explicit protection:
    # --binary and --no-ext-diff alone do not suppress Git text conversion.
    git(workspace, "diff", "--cached", "--no-ext-diff", "--binary", result.baseline_commit_sha)
    assert marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_fix_receives_bounded_lock_content_from_its_baseline(tmp_path, dirty):
    root = repo(tmp_path)
    lock = root / "package-lock.json"
    lock.write_text('{"lockfileVersion": 2}\n')
    git(root, "add", "package-lock.json")
    git(root, "commit", "-m", "dependency lock")
    if dirty:
        lock.write_text('{"lockfileVersion": 3}\n')
    captured = lock.read_text()
    snapshot = capture_snapshot(root, mode="audit")
    assert "package-lock.json" not in snapshot.files

    class LockRepair(ScriptedModels):
        async def complete(self, **kwargs):
            if kwargs["schema"]["title"] == "RepairPlan":
                context = json.loads(kwargs["messages"][-1]["content"])
                supplied = context["files"]["package-lock.json"]
                assert supplied["content"] == captured
                assert supplied["sha256"] == hashlib.sha256(captured.encode()).hexdigest()
                return {
                    "edits": [
                        {
                            "path": "package-lock.json",
                            "expected_sha256": supplied["sha256"],
                            "content": '{"lockfileVersion": 3, "packages": {}}\n',
                        }
                    ]
                }
            return {
                "approved": True,
                "reason": "Selected dependency change includes its lock",
                "unaddressed_ids": [],
            }

    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        LockRepair([]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.commit_sha
    assert "package-lock.json" in result.patch
    assert lock.read_text() == captured


@pytest.mark.asyncio
async def test_additional_exclusion_policy_applies_to_fix_lock_context(tmp_path):
    root = repo(tmp_path)
    (root / "private").mkdir()
    (root / "private" / "uv.lock").write_text("private-lock-contents")
    settings = Settings(exclude=[*Settings().exclude, "private/**"])
    snapshot = capture_snapshot(root, mode="audit", settings=settings)

    class InspectContext(ScriptedModels):
        async def complete(self, **kwargs):
            context = json.loads(kwargs["messages"][-1]["content"])
            assert "private/uv.lock" not in context["files"]
            assert "private/uv.lock" in context["omitted_paths"]
            return {"edits": []}

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        InspectContext([]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "draft"
    assert "No applicable repairs were produced." in result.notes


@pytest.mark.asyncio
@pytest.mark.parametrize("file_limit,content_length", [(100, 200), (5000, 1500)])
async def test_oversized_lock_context_is_explicitly_omitted(tmp_path, file_limit, content_length):
    root = repo(tmp_path)
    (root / "uv.lock").write_text("x" * content_length)
    git(root, "add", "uv.lock")
    git(root, "commit", "-m", "dependency lock")
    settings = Settings(max_file_bytes=file_limit, max_context_chars=1000)
    snapshot = capture_snapshot(root, mode="audit", settings=settings)

    class InspectContext(ScriptedModels):
        async def complete(self, **kwargs):
            context = json.loads(kwargs["messages"][-1]["content"])
            assert "uv.lock" not in context["files"]
            assert "uv.lock" in context["omitted_paths"]
            assert "limit" in context["omitted_reasons"]["uv.lock"]
            return {"edits": []}

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        InspectContext([]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "draft"
    assert "No applicable repairs were produced." in result.notes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", [".npmrc", ".pypirc", "credentials.json", "secret.p12", "node_modules/.npmrc"]
)
async def test_model_cannot_create_credential_files(tmp_path, path):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    plan = {"edits": [{"path": path, "expected_sha256": None, "content": "secret=123"}]}
    result = await fix(
        review_of(snapshot),
        ["F1"],
        Settings(),
        ScriptedModels([plan]),
        destination=tmp_path / "fixed",
        runner=passing_checks,
    )
    assert result.status == "failed"
    assert not (tmp_path / "fixed" / path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_status", ["unavailable", "timed_out", "missing", "wrong_name"])
async def test_passing_fixed_checks_require_a_complete_baseline_comparison(
    tmp_path, baseline_status
):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    settings = Settings(checks=[Check(name="tests", command=["test"], image="python:3.12")])
    baseline = (
        []
        if baseline_status == "missing"
        else [
            CheckResult(
                name="other" if baseline_status == "wrong_name" else "tests",
                status="passed" if baseline_status == "wrong_name" else baseline_status,
                output="baseline could not run",
            )
        ]
    )
    runs = iter([baseline, [CheckResult(name="tests", status="passed", exit_code=0)]])

    def checks(root, checks, deadline):
        return next(runs)

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=checks,
    )
    assert result.status == "draft"
    assert "baseline" in " ".join(result.notes).lower()


@pytest.mark.asyncio
async def test_resolved_baseline_failure_can_be_validated_with_clear_diagnostics(tmp_path):
    root = repo(tmp_path)
    snapshot = capture_snapshot(root, mode="audit")
    settings = Settings(checks=[Check(name="tests", command=["test"], image="python:3.12")])
    runs = iter(
        [
            [CheckResult(name="tests", status="failed", exit_code=1, output="original failure")],
            [CheckResult(name="tests", status="passed", exit_code=0)],
        ]
    )

    def checks(root, checks, deadline):
        return next(runs)

    result = await fix(
        review_of(snapshot),
        ["F1"],
        settings,
        ScriptedModels(answers("result = 1 + 1\n", "TOTAL = 1 + 1\n")),
        destination=tmp_path / "fixed",
        runner=checks,
    )
    assert result.status == "validated"
    assert "tests: passed (baseline failed)" in result.notes
