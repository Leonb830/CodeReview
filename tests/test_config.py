import pytest

from codereview.config import Check, Settings


def test_ambiguous_check_names_cannot_hide_baseline_failures():
    with pytest.raises(ValueError, match="unique"):
        Settings(
            checks=[
                Check(name="tests", command=["pytest"], image="project-checks"),
                Check(name="tests", command=["tsc"], image="project-checks"),
            ]
        )
