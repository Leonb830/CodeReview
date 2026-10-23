"""Source-bound finding identities and deterministic evidence checks."""

import hashlib
import json
from collections.abc import Iterable
from pathlib import PurePosixPath

from codereview.models import Finding, Snapshot


def prepare_findings(
    snapshot: Snapshot,
    candidates: Iterable[Finding],
    *,
    allowed_paths: Iterable[str] | None = None,
) -> tuple[list[Finding], list[str]]:
    """Validate source evidence, bind identity, and remove equivalent duplicate claims.

    This proves the quoted source exists, not that the model's interpretation is correct.
    The independent verifier is responsible for challenging that interpretation.
    """
    allowed = set(allowed_paths) if allowed_paths is not None else set(snapshot.files)
    accepted: dict[str, Finding] = {}
    gaps: list[str] = []
    for candidate in candidates:
        error = _evidence_error(snapshot, candidate, allowed)
        if error:
            gaps.append(f"Rejected finding {candidate.title!r}: {error}")
            continue
        identity = _identity(snapshot, candidate)
        # The worker cannot assert that tests were run: this path only inspects source.
        bound = candidate.model_copy(
            update={
                "id": identity,
                "snapshot_id": snapshot.id,
                "verification": "candidate",
            }
        )
        accepted.setdefault(identity, bound)
    return list(accepted.values()), gaps


def _evidence_error(snapshot: Snapshot, finding: Finding, allowed: set[str]) -> str | None:
    if not any(evidence.path in allowed for evidence in finding.evidence):
        return "no evidence is in the assigned review scope"
    for evidence in finding.evidence:
        path = PurePosixPath(evidence.path)
        if path.is_absolute() or ".." in path.parts or "\\" in evidence.path:
            return "unsafe evidence path"
        if evidence.path not in snapshot.files:
            return f"{evidence.path} was not captured in the reviewed snapshot"
        lines = snapshot.files[evidence.path].splitlines()
        if evidence.end_line > len(lines):
            return f"{evidence.path}:{evidence.end_line} is outside the captured source"
        actual = "\n".join(lines[evidence.start_line - 1 : evidence.end_line])
        if evidence.quote not in (actual, actual + "\n", actual + "\r\n"):
            return f"quote does not match {evidence.path}:{evidence.start_line}-{evidence.end_line}"
    return None


def _identity(snapshot: Snapshot, finding: Finding) -> str:
    claim = {
        "snapshot": snapshot.id,
        "category": finding.category,
        "kind": finding.kind,
        "observation": " ".join(finding.observation.lower().split()),
        "evidence": sorted(
            (e.path, e.start_line, e.end_line, e.quote.rstrip("\r\n")) for e in finding.evidence
        ),
    }
    digest = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()
    return f"F{digest[:12]}"
