"""Review TypeScript repositories through real snapshot, review and reporting boundaries.

Model responses are scripted: these tests verify orchestration and evidence handling,
not a model's ability to discover bugs or the result of a TypeScript compiler run.
"""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codereview.cli import app
from codereview.config import Settings
from codereview.engine import review
from codereview.models import Usage
from codereview.reporting import render_review
from codereview.runs import RunStore
from codereview.snapshots import capture_snapshot

CARD = """import type { Profile } from "./contracts.js";
export function ProfileCard({ payload }: { payload: unknown }) {
  const profile = payload as Profile;
  return <span>{profile.name.toUpperCase()}</span>;
}
"""
SOURCES = {
    "src/ProfileCard.tsx": CARD,
    "src/api.ts": "export async function load(): Promise<unknown> { return {}; }\n",
    "src/loader.mts": "export const load = async (): Promise<unknown> => ({});\n",
    "src/legacy.cts": "const label: string = 'legacy';\nexport = label;\n",
    "src/public.d.ts": "export interface PublicProfile { name: string }\n",
    "src/public.d.mts": "export declare const version: string;\n",
    "src/public.d.cts": "declare const version: string;\nexport = version;\n",
}


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def typescript_repo(tmp_path: Path) -> Path:
    root = tmp_path / "typescript project"
    (root / "src").mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "TypeScript fixture")
    git(root, "config", "user.email", "typescript@example.test")
    (root / "src/ProfileCard.tsx").write_text(
        "export function ProfileCard() { return <span>Guest</span>; }\n"
    )
    (root / "src/contracts.ts").write_text("export interface Profile { name: string }\n")
    (root / "tsconfig.json").write_text('{"extends":"./tsconfig.base.json"}\n')
    (root / "tsconfig.base.json").write_text(
        '{"compilerOptions":{"strict":true,"jsx":"react-jsx","module":"NodeNext"}}\n'
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "typescript-fixture",
                "type": "module",
                "devDependencies": {"typescript": "5.7.3"},
                "dependencies": {"react": "19.0.0"},
                # Capturing and reviewing source must never execute package scripts.
                "scripts": {"prepare": "touch UNEXPECTED_EXECUTION"},
            }
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    for path, text in SOURCES.items():
        (root / path).write_text(text)
    return root


@pytest.mark.parametrize("mode", ["working", "staged", "branch", "audit"])
def test_all_typescript_extensions_are_reviewable_in_each_scope(typescript_repo, mode):
    root = typescript_repo
    base = git(root, "rev-parse", "HEAD")
    if mode in {"staged", "branch"}:
        git(root, "add", ".")
    if mode == "branch":
        git(root, "commit", "-qm", "TypeScript changes")
    if mode in {"staged", "branch"}:
        (root / "src/ProfileCard.tsx").write_text("// uncaptured working-tree edit\n")
    before = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    snapshot = capture_snapshot(root, mode=mode, base=base if mode == "branch" else None)
    assert set(SOURCES) <= set(snapshot.changed_paths)
    for path, text in SOURCES.items():
        assert snapshot.files[path] == text
        assert snapshot.changed_lines[path]
    assert "tsconfig.base.json" in snapshot.files
    assert not snapshot.excluded
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert not (root / "UNEXPECTED_EXECUTION").exists()


class TypeScriptModel:
    def __init__(self, *, fabricated_quote: bool = False):
        self.fabricated_quote = fabricated_quote
        self.requests: list[tuple[str, dict]] = []

    async def complete(self, *, role, messages, schema, max_tokens=4096):
        stage = schema["title"]
        payload = json.loads(messages[-1]["content"])
        self.requests.append((stage, payload))
        if stage == "TaskPlan":
            return {"tasks": []}  # The engine must retain its mandatory review lenses.
        if stage == "Analysis":
            if payload["task"]["lens"] != "defects":
                return {"findings": []}
            return {
                "findings": [
                    {
                        "category": "correctness",
                        "kind": "defect",
                        "severity": "medium",
                        "title": "Validate the unknown profile before rendering",
                        "observation": "A type assertion allows unchecked input to reach a string method.",
                        "rationale": "A payload with a non-string name throws while rendering.",
                        "alternative": "Validate the payload and name before rendering the profile.",
                        "origin": "introduced",
                        "verification": "reproduced",
                        "snapshot_id": "model-cannot-rebind-evidence",
                        "evidence": [
                            {
                                "path": "src/ProfileCard.tsx",
                                "start_line": 3,
                                "end_line": 4,
                                "quote": "not actual TypeScript source"
                                if self.fabricated_quote
                                else "\n".join(CARD.splitlines()[2:4]),
                            }
                        ],
                    }
                ]
            }
        if stage == "Verification":
            return {
                "decisions": [
                    {
                        "finding_id": item["id"],
                        "supported": True,
                        "rationale": "The exact source assertion provides no runtime validation.",
                    }
                    for item in payload["findings"]
                ]
            }
        assert stage == "Consolidation"
        return {"ordered_ids": [item["id"] for item in payload["findings"]]}

    def usage(self):
        return Usage(calls=len(self.requests))


@pytest.mark.parametrize("fabricated_quote", [False, True], ids=["supported", "fabricated"])
async def test_typescript_review_preserves_source_evidence_and_related_context(
    typescript_repo, tmp_path, fabricated_quote
):
    before = git(typescript_repo, "status", "--porcelain=v1", "--untracked-files=all")
    snapshot = capture_snapshot(typescript_repo)
    client = TypeScriptModel(fabricated_quote=fabricated_quote)
    result = await review(snapshot, Settings(), client)
    assert {task.lens for task in result.tasks} == {"maintainability", "defects", "framework"}
    for stage, payload in client.requests:
        if stage in {"TaskPlan", "Analysis", "Verification"}:
            assert "export interface Profile { name: string }" in payload["context"]
            assert '"strict":true' in payload["context"]
            assert '"typescript": "5.7.3"' in payload["context"]
    store = RunStore(tmp_path / "saved reviews")
    store.save_review(result)
    loaded = store.load_review(result.run_id)
    assert loaded == result
    report = render_review(loaded)
    if fabricated_quote:
        assert result.status == "partial"
        assert not result.findings
        assert any("quote does not match" in gap for gap in result.gaps)
        assert "not a clean review" in report
        assert all(stage != "Verification" for stage, _ in client.requests)
    else:
        assert result.status == "complete"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.verification == "supported"
        assert finding.snapshot_id == snapshot.id
        assert finding.evidence[0].quote == "\n".join(CARD.splitlines()[2:4])
        assert "src/ProfileCard.tsx:3–4" in report
        assert "Validate the unknown profile" in report
        assert "&lt;span&gt;" in report
    assert git(typescript_repo, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert not (typescript_repo / "UNEXPECTED_EXECUTION").exists()


def test_cli_can_inspect_typescript_without_model_credentials(typescript_repo, tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "review",
            "--root",
            str(typescript_repo),
            "--offline",
            "--json",
            "--state-dir",
            str(tmp_path / "offline runs"),
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "partial"
    assert payload["snapshot"]["files"]["src/ProfileCard.tsx"] == CARD
    assert set(SOURCES) <= set(payload["snapshot"]["changed_paths"])
    assert payload["findings"] == []
    assert payload["usage"]["calls"] == 0
    assert any("no model review" in gap for gap in payload["gaps"])
