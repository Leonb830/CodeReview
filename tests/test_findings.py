from codereview.findings import prepare_findings
from codereview.models import Evidence, Finding, Snapshot


def snapshot() -> Snapshot:
    return Snapshot(
        id="snapshot-one",
        root="/repo",
        head_sha="head",
        base_sha="base",
        mode="working",
        files={"app.py": "def get_user(user_id):\n    return db.execute(user_id)\n"},
        changed_paths=["app.py"],
        changed_lines={"app.py": [2]},
    )


def candidate(**changes) -> Finding:
    values = dict(
        category="maintainability",
        kind="suggestion",
        title="Name the database operation",
        observation="The database operation has no domain name.",
        rationale="The caller must understand the raw database operation.",
        alternative="Use a named repository operation if this behavior is reused.",
        evidence=[
            Evidence(
                path="app.py", start_line=2, end_line=2, quote="    return db.execute(user_id)"
            )
        ],
    )
    values.update(changes)
    return Finding(**values)


def test_small_suggestions_get_revision_bound_ids_and_exact_evidence():
    findings, gaps = prepare_findings(snapshot(), [candidate()])
    assert gaps == []
    assert len(findings) == 1
    assert findings[0].kind == "suggestion"
    assert findings[0].snapshot_id == "snapshot-one"
    assert findings[0].id.startswith("F")
    assert findings[0].verification == "candidate"
    repeated, _ = prepare_findings(snapshot(), [candidate()])
    assert repeated[0].id == findings[0].id


def test_mismatched_source_and_unrelated_scope_are_not_publishable_findings():
    fabricated = candidate(
        evidence=[Evidence(path="app.py", start_line=2, end_line=2, quote="    db.delete_all()")]
    )
    findings, gaps = prepare_findings(snapshot(), [fabricated])
    assert findings == []
    assert "quote does not match" in gaps[0]
    unrelated, gaps = prepare_findings(snapshot(), [candidate()], allowed_paths=["other.py"])
    assert unrelated == []
    assert "assigned review scope" in gaps[0]


def test_repeated_claims_are_deduplicated_without_promoting_worker_execution_claims():
    findings, gaps = prepare_findings(
        snapshot(),
        [
            candidate(),
            candidate(
                title="Different wording",
                verification="reproduced",
                id="malicious-supplied-id",
            ),
        ],
    )
    assert gaps == []
    assert len(findings) == 1
    assert findings[0].verification == "candidate"
    other_snapshot = snapshot().model_copy(update={"id": "snapshot-two"})
    other, _ = prepare_findings(other_snapshot, [candidate()])
    assert findings[0].id != other[0].id


def test_exact_multiline_evidence_can_include_a_blank_last_line():
    source = snapshot().model_copy(update={"files": {"app.py": "one\n\nthree\n"}})
    item = candidate(evidence=[Evidence(path="app.py", start_line=1, end_line=2, quote="one\n")])
    findings, gaps = prepare_findings(source, [item])
    assert gaps == []
    assert len(findings) == 1
