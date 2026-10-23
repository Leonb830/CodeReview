"""Bounded planning, independent specialist analysis and evidence verification."""

import asyncio
import json
import time
from collections import Counter
from typing import Any, Literal, TypeVar

from pydantic import Field

from codereview import prompts
from codereview.config import Settings
from codereview.context import TRUNCATION_MARKER, affected_by_deletions, build_context
from codereview.findings import prepare_findings
from codereview.models import (
    CompletionClient,
    Finding,
    Record,
    ReviewResult,
    ReviewTask,
    Snapshot,
)


class TaskPlan(Record):
    tasks: list[ReviewTask]


class Analysis(Record):
    findings: list[Finding]
    limitations: list[str] = Field(default_factory=list)


class VerificationDecision(Record):
    finding_id: str
    supported: bool
    rationale: str = Field(min_length=1)


class Verification(Record):
    decisions: list[VerificationDecision]
    limitations: list[str] = Field(default_factory=list)


class Consolidation(Record):
    ordered_ids: list[str]


_Response = TypeVar("_Response", bound=Record)
_LENSES: tuple[Literal["maintainability", "defects", "framework"], ...] = (
    "maintainability",
    "defects",
    "framework",
)
_QUESTIONS = {
    "maintainability": "Find useful design improvements, including small suggestions; preserve cohesive code.",
    "defects": "Investigate correctness, security and regressions; check safeguards and relevant callers.",
    "framework": "Inspect installed framework/runtime assumptions and affected Python/TypeScript behavior.",
}
_DECLARED_EXCLUSIONS = {
    "repository exclusion policy",
    "generated or dependency directory",
    "potential secret file",
    "unsupported source type",
    "binary file excluded",
    "non-UTF-8 file excluded",
    "symlink excluded",
    "submodule excluded",
    "unsafe or unsupported path",
}


async def review(
    snapshot: Snapshot,
    settings: Settings,
    client: CompletionClient,
    *,
    operation: Literal["review", "audit"] = "review",
) -> ReviewResult:
    """Review one captured revision and preserve explicit coverage failures.

    The client owns paid-call accounting. This layer enforces the overall operation deadline,
    required review lenses and source-bound output regardless of model behavior.
    """
    result = ReviewResult(
        snapshot=snapshot,
        operation=operation,
        config_digest=settings.digest(),
        models={"coordinator": settings.models.coordinator, "worker": settings.models.worker},
    )
    remaining = float(settings.limit(operation).seconds)
    budget = getattr(client, "budget", None)
    if budget is not None:
        remaining = min(remaining, budget.remaining_seconds())
    deadline = time.monotonic() + remaining
    # Keep a bounded finalization window; reports still have a deterministic fallback.
    work_deadline = deadline - min(15.0, settings.limit(operation).seconds * 0.05)
    scope = sorted(
        snapshot.files
        if operation == "audit"
        else set(snapshot.changed_paths) & snapshot.files.keys()
    )
    declared = {
        message.split(": ", 1)[0]
        for message in snapshot.excluded
        if message.split(": ", 1)[-1] in _DECLARED_EXCLUSIONS
    }
    result.gaps.extend(
        message
        for message in snapshot.excluded
        if message.split(": ", 1)[-1] not in _DECLARED_EXCLUSIONS
    )
    if operation == "review" and snapshot.deleted_paths:
        affected, deletion_gaps = affected_by_deletions(snapshot, max_paths=settings.max_tasks * 4)
        scope = sorted(set(scope) | set(affected))
        result.gaps.extend(deletion_gaps)
    unavailable = (
        set(snapshot.changed_paths) - snapshot.files.keys() - declared - set(snapshot.deleted_paths)
    )
    if operation == "review" and unavailable:
        result.gaps.append(
            "Changed files without captured current source: " + ", ".join(sorted(unavailable))
        )
    if not scope:
        result.summary = "No reviewable source in this snapshot."
        result.status = "partial" if result.gaps else "complete"
        result.usage = client.usage()
        result.call_records = list(getattr(client, "call_records", []))
        return result

    try:
        plan = await _call(
            client,
            "coordinator",
            prompts.PLAN,
            {
                "operation": operation,
                "snapshot_id": snapshot.id,
                "scope": scope,
                "deleted_paths": snapshot.deleted_paths,
                "available_paths": sorted(snapshot.files),
                "max_tasks": settings.max_tasks,
                "trusted_policy": settings.instructions,
                "context": build_context(
                    snapshot, paths=scope, max_chars=settings.max_context_chars
                ),
            },
            TaskPlan,
            work_deadline,
            settings,
        )
        tasks = _tasks(plan, scope, snapshot, settings.max_tasks)
        result.tasks = tasks
    except Exception as exc:
        # No worker-only fallback can silently bypass an exhausted coordinator allowance.
        result.gaps.append(_failure("Coordinator planning unavailable", exc))
        result.status = "failed"
        result.summary = "Review could not be planned; no complete assessment is available."
        result.usage = client.usage()
        result.call_records = list(getattr(client, "call_records", []))
        return result

    semaphore = asyncio.Semaphore(settings.concurrency)
    outcomes = await asyncio.gather(
        *(
            _investigate(task, snapshot, settings, client, work_deadline, semaphore)
            for task in tasks
        )
    )
    candidates: list[Finding] = []
    for task, (findings, gaps, completed) in zip(tasks, outcomes, strict=True):
        candidates.extend(findings)
        result.gaps.extend(gaps)
        if completed:
            result.completed_tasks.append(task.id)

    # Workers may reach the same claim independently. Identity is already bound to this snapshot.
    unique = {finding.id: finding for finding in candidates}
    result.findings = list(unique.values())
    if result.findings:
        try:
            order = await _call(
                client,
                "coordinator",
                prompts.CONSOLIDATE,
                {
                    "findings": [finding.model_dump() for finding in result.findings],
                    "completed_tasks": result.completed_tasks,
                    "gaps": result.gaps,
                },
                Consolidation,
                deadline,
                settings,
            )
            result.findings = _ordered(result.findings, order.ordered_ids)
        except Exception as exc:
            result.gaps.append(
                _failure("Coordinator consolidation unavailable; retained verified findings", exc)
            )

    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = "partial" if result.gaps else "complete"
    if not result.completed_tasks:
        result.status = "failed"
    count = len(result.findings)
    result.summary = (
        f"{count} supported finding{'s' if count != 1 else ''}; "
        f"{len(result.completed_tasks)}/{len(tasks)} review tasks completed."
    )
    if result.gaps:
        result.summary += " Coverage is incomplete; this is not a clean review."
    result.usage = client.usage()
    result.call_records = list(getattr(client, "call_records", []))
    return result


def _tasks(plan: TaskPlan, scope: list[str], snapshot: Snapshot, maximum: int) -> list[ReviewTask]:
    """Required lenses cannot disappear through model omission or task-count truncation."""
    normalized: list[ReviewTask] = []
    for lens in _LENSES:
        matching = [task for task in plan.tasks if task.lens == lens]
        first = matching[0] if matching else None
        # A baseline pass covers the full focus, even if the model only names one file.
        support = [path for path in (first.paths if first else []) if path in snapshot.files]
        normalized.append(
            ReviewTask(
                id=f"T{len(normalized) + 1}",
                lens=lens,
                question=first.question if first else _QUESTIONS[lens],
                paths=list(dict.fromkeys(scope + support)),
                evidence_required=first.evidence_required
                if first
                else "Exact code quotes, callers and counterexamples.",
            )
        )
    # Extra tasks preserve the coordinator's narrower deep dives after mandatory coverage.
    for lens in _LENSES:
        for task in [item for item in plan.tasks if item.lens == lens][1:]:
            if len(normalized) >= maximum:
                break
            paths = [path for path in task.paths if path in snapshot.files]
            if paths:
                normalized.append(
                    task.model_copy(update={"id": f"T{len(normalized) + 1}", "paths": paths})
                )
    return normalized


async def _investigate(
    task: ReviewTask,
    snapshot: Snapshot,
    settings: Settings,
    client: CompletionClient,
    deadline: float,
    semaphore: asyncio.Semaphore,
) -> tuple[list[Finding], list[str], bool]:
    gaps: list[str] = []
    async with semaphore:
        context = build_context(snapshot, paths=task.paths, max_chars=settings.max_context_chars)
        if TRUNCATION_MARKER in context:
            gaps.append(
                f"{task.id}: context truncated; assigned files were not completely inspected"
            )
        payload = {
            "task": task.model_dump(),
            "snapshot_id": snapshot.id,
            "deleted_paths": snapshot.deleted_paths,
            "trusted_policy": settings.instructions,
            "context": context,
        }
        try:
            analysis = await _call(
                client, "worker", prompts.ANALYZE, payload, Analysis, deadline, settings
            )
        except Exception as exc:
            gaps.append(_failure(f"{task.id} {task.lens} analysis unavailable", exc))
            return [], gaps, False
        gaps.extend(f"{task.id}: {limitation}" for limitation in analysis.limitations)
        candidates, rejected = prepare_findings(
            snapshot, analysis.findings, allowed_paths=task.paths
        )
        gaps.extend(f"{task.id}: {reason}" for reason in rejected)
        if not candidates:
            return [], gaps, True
        try:
            verification = await _call(
                client,
                "worker",
                prompts.VERIFY,
                {
                    "findings": [candidate.model_dump() for candidate in candidates],
                    "trusted_policy": settings.instructions,
                    "context": context,
                },
                Verification,
                deadline,
                settings,
            )
        except Exception as exc:
            gaps.append(_failure(f"{task.id}: candidate findings could not be verified", exc))
            return [], gaps, False
        gaps.extend(
            f"{task.id} verification: {limitation}" for limitation in verification.limitations
        )
        decisions = {decision.finding_id: decision for decision in verification.decisions}
        decision_counts = Counter(decision.finding_id for decision in verification.decisions)
        accepted: list[Finding] = []
        for candidate in candidates:
            decision = decisions.get(candidate.id)
            if decision is None:
                gaps.append(f"{task.id}: verifier omitted {candidate.id}")
            elif decision_counts[candidate.id] != 1:
                gaps.append(
                    f"{task.id}: duplicate or conflicting verifier decisions for {candidate.id}"
                )
            elif decision.supported:
                accepted.append(candidate.model_copy(update={"verification": "supported"}))
        return accepted, gaps, True


async def _call(
    client: CompletionClient,
    role: Literal["coordinator", "worker"],
    instruction: str,
    payload: dict[str, Any],
    response: type[_Response],
    deadline: float,
    settings: Settings,
) -> _Response:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("operation deadline reached")
    raw = await asyncio.wait_for(
        client.complete(
            role=role,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            schema=response.model_json_schema(),
            max_tokens=settings.models.max_output_tokens,
        ),
        timeout=remaining,
    )
    return response.model_validate(raw)


def _ordered(findings: list[Finding], requested: list[str]) -> list[Finding]:
    by_id = {finding.id: finding for finding in findings}
    ids = list(dict.fromkeys([item for item in requested if item in by_id] + list(by_id)))
    return [by_id[item] for item in ids]


def _failure(prefix: str, error: Exception) -> str:
    return f"{prefix} ({type(error).__name__}: {str(error)[:240]})"
