"""Exercise the complete review boundary; only the external HTTP service is simulated."""

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from codereview.config import ModelPolicy, Settings
from codereview.engine import review
from codereview.models import Finding
from codereview.providers import OpenRouterClient
from codereview.reporting import render_review
from codereview.runs import RunStore
from codereview.snapshots import capture_snapshot

SOURCE = "def total(items):\n    result = 0\n    for item in items:\n        result += item\n    return result\n"


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "reviewed project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Integration fixture")
    git(root, "config", "user.email", "fixture@example.test")
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (root / ".gitignore").write_text("ignored.py\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    (root / "app.py").write_text("def total(items):\n    return sum(items) + 0\n")
    git(root, "add", "app.py")
    (root / "app.py").write_text(SOURCE)
    (root / "helper.ts").write_text("export const label = 'total';\n")
    (root / "ignored.py").write_text("ignored = True\n")
    return root


def settings() -> Settings:
    return Settings(
        models=ModelPolicy(
            coordinator="fixture/coordinator",
            worker="fixture/worker",
            allowed_models=["fixture/coordinator", "fixture/worker"],
            allowed_providers=["fixture-provider"],
        )
    )


def candidate() -> dict[str, Any]:
    return Finding(
        id="untrusted-model-id",
        snapshot_id="untrusted-model-snapshot",
        category="maintainability",
        kind="suggestion",
        severity="low",
        title="Use the built-in sum for numeric accumulation",
        observation="This loop duplicates Python's standard numeric accumulation operation.",
        rationale="Using the standard operation makes the intent explicit and removes mutable bookkeeping.",
        alternative="Return sum(items), retaining the current behavior for the numeric inputs expected here.",
        evidence=[
            {
                "path": "app.py",
                "start_line": 2,
                "end_line": 5,
                "quote": "    result = 0\n    for item in items:\n        result += item\n    return result",
            }
        ],
        # Model assertions do not prove that tests were run or determine finding identity.
        verification="reproduced",
    ).model_dump()


class OpenRouterService:
    """A real HTTP boundary with catalog, key policy, schemas, costs and an outage."""

    def __init__(self, *, unavailable_worker: bool = False):
        self.unavailable_worker = unavailable_worker
        self.calls: list[dict[str, Any]] = []
        self.verified_ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"
        if request.url.path.endswith("/key"):
            assert request.headers["Authorization"] == "Bearer fixture-coordinator-key"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit": 100,
                        "limit_remaining": 95,
                        "limit_reset": "monthly",
                        "include_byok_in_limit": True,
                        "is_management_key": False,
                    }
                },
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": model,
                            "context_length": 200000,
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
                        for model in ("fixture/coordinator", "fixture/worker")
                    ]
                },
            )
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]
        stage = schema["title"]
        payload = json.loads(body["messages"][-1]["content"])
        role = "coordinator" if stage in {"TaskPlan", "Consolidation"} else "worker"
        assert request.headers["Authorization"] == f"Bearer fixture-{role}-key"
        assert body["model"] == f"fixture/{role}"
        assert body["provider"]["only"] == ["fixture-provider"]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["require_parameters"] is True
        assert body["response_format"]["json_schema"]["strict"] is True
        # Real Pydantic schemas include nested defaults. Strict providers require every field.
        for definition in [schema, *schema.get("$defs", {}).values()]:
            if definition.get("type") == "object":
                assert set(definition["required"]) == set(definition.get("properties", {}))
                assert definition["additionalProperties"] is False
                assert all("default" not in field for field in definition["properties"].values())
        self.calls.append({"stage": stage, "role": role, "payload": payload})

        if stage == "TaskPlan":
            assert payload["scope"] == ["app.py", "helper.ts"]
            assert "result += item" in payload["context"]
            output = {
                "tasks": [
                    {
                        "id": f"plan-{lens}",
                        "lens": lens,
                        "question": f"Inspect {lens}",
                        "paths": ["app.py", "helper.ts"],
                        "evidence_required": "Check exact source quotes and counterexamples.",
                    }
                    for lens in ("maintainability", "defects", "framework")
                ]
            }
        elif stage == "Analysis":
            if self.unavailable_worker and payload["task"]["lens"] == "defects":
                # An unavailable endpoint can omit billing details. Retrying cannot erase that risk.
                return httpx.Response(
                    503, json={"error": {"message": "unavailable"}}, headers={"retry-after": "0"}
                )
            output = {
                "findings": [candidate()] if payload["task"]["lens"] == "maintainability" else [],
                "limitations": [],
            }
        elif stage == "Verification":
            finding = payload["findings"][0]
            assert finding["id"] != "untrusted-model-id"
            assert finding["snapshot_id"] != "untrusted-model-snapshot"
            assert finding["verification"] == "candidate"
            assert "result += item" in payload["context"]
            self.verified_ids.append(finding["id"])
            output = {
                "decisions": [
                    {
                        "finding_id": finding["id"],
                        "supported": True,
                        "rationale": "The quoted loop is exactly a numeric sum; the alternative preserves its purpose.",
                    }
                ],
                "limitations": [],
            }
        else:
            assert stage == "Consolidation"
            assert all(item["verification"] == "supported" for item in payload["findings"])
            output = {"ordered_ids": [item["id"] for item in payload["findings"]]}

        Draft202012Validator(schema).validate(output)
        return httpx.Response(
            200,
            json={
                "id": f"generation-{len(self.calls)}",
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
                "usage": {"cost": 0.02 if role == "coordinator" else 0.01},
            },
        )


@pytest.mark.parametrize(
    "unavailable_worker", [False, True], ids=["complete", "partial-worker-outage"]
)
async def test_snapshot_to_saved_report_through_real_review_and_provider(
    repository: Path,
    tmp_path: Path,
    unavailable_worker: bool,
):
    policy = settings()
    before = git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    snapshot = capture_snapshot(repository, settings=policy)
    assert snapshot.files["app.py"] == SOURCE
    assert "helper.ts" in snapshot.files
    assert "ignored.py" not in snapshot.files
    service = OpenRouterService(unavailable_worker=unavailable_worker)
    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as http:
        async with OpenRouterClient(
            policy,
            coordinator_key="fixture-coordinator-key",
            worker_key="fixture-worker-key",
            http_client=http,
        ) as client:
            result = await review(snapshot, policy, client)

    assert result.status == ("partial" if unavailable_worker else "complete")
    assert len(result.completed_tasks) == (2 if unavailable_worker else 3)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.verification == "supported"
    assert finding.snapshot_id == snapshot.id
    assert finding.id == service.verified_ids[0]
    assert finding.evidence[0].quote == "\n".join(snapshot.files["app.py"].splitlines()[1:5])
    stages = Counter(call["stage"] for call in service.calls)
    assert stages == {
        "TaskPlan": 1,
        "Analysis": 4 if unavailable_worker else 3,
        "Verification": 1,
        "Consolidation": 1,
    }
    assert result.usage.coordinator_cost == 0.04
    assert result.usage.worker_cost == (0.03 if unavailable_worker else 0.04)
    assert result.usage.calls == (7 if unavailable_worker else 6)
    assert result.usage.coordinator_reserved == 0
    assert len(result.call_records) == result.usage.calls
    assert all("key" not in record and "messages" not in record for record in result.call_records)

    store = RunStore(tmp_path / "saved runs")
    saved = store.save_review(result)
    loaded = store.load_review(result.run_id)
    assert saved.is_file()
    assert loaded == result
    report = render_review(loaded)
    assert finding.id in report
    assert "Improvement suggestions" in report
    assert "Use the built-in sum" in report
    assert "app.py:2–5" in report
    assert "coordinator **$0.0400**" in report
    assert store.list_reviews()[0]["status"] == result.status
    if unavailable_worker:
        assert result.usage.worker_reserved > 0
        assert (
            len([record for record in result.call_records if record["status"] == "usage_unknown"])
            == 2
        )
        assert any("defects analysis unavailable" in gap for gap in loaded.gaps)
        assert "Incomplete coverage" in report
        assert "not a clean review" in report
        assert "Unsettled reservations" in report
    else:
        assert not result.gaps
        assert result.usage.worker_reserved == 0
        assert "Incomplete coverage" not in report
    assert git(repository, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert (repository / "app.py").read_text() == SOURCE
