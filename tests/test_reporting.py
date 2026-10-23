from codereview.models import Evidence, Finding, ReviewResult, Snapshot
from codereview.reporting import render_review


def test_small_suggestions_and_incomplete_coverage_remain_visible():
    snapshot = Snapshot(
        id="snapshot",
        root="repo",
        head_sha="abc",
        base_sha="abc",
        mode="working",
        files={"app.py": "value = 1\n"},
        changed_paths=["app.py"],
    )
    finding = Finding(
        id="F12",
        category="maintainability",
        title="Name the unit",
        observation="The unit is implicit",
        rationale="A reader cannot tell whether this is seconds",
        alternative="Use timeout_seconds",
        evidence=[Evidence(path="app.py", start_line=1, end_line=1, quote="value = 1")],
        verification="supported",
    )
    result = ReviewResult(
        snapshot=snapshot, findings=[finding], status="partial", gaps=["Framework review timed out"]
    )
    rendered = render_review(result)
    assert "F12" in rendered
    assert "suggestion" in rendered
    assert "Framework review timed out" in rendered
    assert "partial" in rendered.lower()
    assert "No issues" not in rendered
