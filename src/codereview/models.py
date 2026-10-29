"""Shared records crossing the review, repair, persistence and publication interfaces."""

from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codereview import __version__


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Snapshot(Record):
    id: str
    root: str
    head_sha: str
    base_sha: str
    mode: Literal["working", "staged", "branch", "audit"]
    files: dict[str, str]
    changed_paths: list[str]
    diff: str = ""
    excluded: list[str] = Field(default_factory=list)
    # Exact changed right-side line numbers; deleted files may have no current content.
    changed_lines: dict[str, list[int]] = Field(default_factory=dict)
    deleted_paths: list[str] = Field(default_factory=list)
    # Base64 bytes for changed non-secret files excluded from model context, e.g. lockfiles.
    overlay_files: dict[str, str] = Field(default_factory=dict)
    unavailable_changes: list[str] = Field(default_factory=list)
    file_modes: dict[str, int] = Field(default_factory=dict)


class Evidence(Record):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> "Evidence":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be at or after start_line")
        return self


class Finding(Record):
    id: str = ""
    category: Literal["maintainability", "correctness", "security", "regression", "framework"]
    kind: Literal["defect", "suggestion"] = "suggestion"
    severity: Literal["high", "medium", "low"] = "low"
    title: str = Field(min_length=1, max_length=200)
    observation: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    alternative: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    verification: Literal["candidate", "supported", "reproduced"] = "candidate"
    uncertainty: str = ""
    origin: Literal["introduced", "existing", "unknown"] = "unknown"
    snapshot_id: str = ""


class ReviewTask(Record):
    id: str
    lens: Literal["maintainability", "defects", "framework"]
    question: str
    paths: list[str]
    evidence_required: str = "Exact source quotes and line locations; examine counterexamples."


class Usage(Record):
    worker_cost: float = 0
    coordinator_cost: float = 0
    worker_reserved: float = 0
    coordinator_reserved: float = 0
    calls: int = 0


class ReviewResult(Record):
    schema_version: int = 1
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    operation: Literal["review", "audit"] = "review"
    snapshot: Snapshot
    status: Literal["complete", "partial", "failed"] = "complete"
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    completed_tasks: list[str] = Field(default_factory=list)
    tasks: list[ReviewTask] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    config_digest: str = ""
    engine_version: str = __version__
    models: dict[str, str] = Field(default_factory=dict)
    github: dict[str, Any] = Field(default_factory=dict)
    call_records: list[dict[str, Any]] = Field(default_factory=list)


class FileEdit(Record):
    path: str
    # Whole-file replacements make the contract explicit; hashes prevent stale edits.
    expected_sha256: str | None = None
    content: str | None = None


class CheckResult(Record):
    name: str
    status: Literal["passed", "failed", "unavailable", "timed_out"]
    exit_code: int | None = None
    output: str = ""


class FixResult(Record):
    schema_version: int = 1
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    review_run_id: str
    snapshot_id: str
    selected_ids: list[str]
    status: Literal["validated", "draft", "failed"] = "draft"
    branch: str | None = None
    worktree: str | None = None
    commit_sha: str | None = None
    baseline_commit_sha: str | None = None
    patch: str = ""
    baseline_checks: list[CheckResult] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    call_records: list[dict[str, Any]] = Field(default_factory=list)
    config_digest: str = ""
    engine_version: str = __version__
    models: dict[str, str] = Field(default_factory=dict)


class CompletionClient(Protocol):
    async def complete(
        self,
        *,
        role: Literal["coordinator", "worker"],
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]: ...

    def usage(self) -> Usage: ...
