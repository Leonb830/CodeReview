import json

import pytest

from codereview.models import ReviewResult, Snapshot
from codereview.runs import RunStore


def test_saved_review_can_be_loaded_for_selected_fixes(tmp_path):
    review = ReviewResult(
        snapshot=Snapshot(
            id="source",
            root="/repo",
            head_sha="abc",
            base_sha="abc",
            mode="working",
            files={"app.py": "print(1)\n"},
            changed_paths=["app.py"],
        )
    )
    store = RunStore(tmp_path / "runs")
    store.save_review(review)
    assert store.load_review(review.run_id) == review


def test_changed_record_is_rejected_before_it_can_authorize_a_fix(tmp_path):
    review = ReviewResult(
        snapshot=Snapshot(
            id="source",
            root="/repo",
            head_sha="abc",
            base_sha="abc",
            mode="audit",
            files={},
            changed_paths=[],
        )
    )
    store = RunStore(tmp_path)
    path = store.save_review(review)
    envelope = json.loads(path.read_text())
    envelope["payload"]["snapshot"]["head_sha"] = "other"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        store.load_review(review.run_id)


def test_run_identifiers_cannot_escape_store(tmp_path):
    with pytest.raises(ValueError, match="identifier"):
        RunStore(tmp_path).load_review("../../credentials")
