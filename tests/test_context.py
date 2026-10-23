from codereview.context import TRUNCATION_MARKER, affected_by_deletions, build_context
from codereview.models import Snapshot


def snapshot() -> Snapshot:
    return Snapshot(
        id="snapshot",
        root="/unused",
        head_sha="head",
        base_sha="base",
        mode="working",
        files={
            "service.py": "def calculate_total(values):\n    return sum(values)\n",
            "caller.py": "from service import calculate_total\nresult = calculate_total([1, 2])\n",
            "unrelated.py": "unrelated = True\n",
            "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n',
        },
        changed_paths=["service.py"],
        diff="--- a/service.py\n+++ b/service.py\n@@ -1 +1 @@\n-old\n+new\n",
    )


def test_context_prioritizes_requested_numbered_sources_and_actual_callers():
    result = build_context(snapshot(), paths=["service.py"])
    assert "1 | def calculate_total(values):" in result
    assert "2 |     return sum(values)" in result
    assert result.index("service.py") < result.index("caller.py")
    assert "result = calculate_total([1, 2])" in result
    assert 'requires-python = ">=3.11"' in result
    assert "unrelated = True" not in result
    assert "--- a/service.py" in result
    assert TRUNCATION_MARKER not in result


def test_context_reports_omissions_and_truncation_with_hard_size_bound():
    record = snapshot().model_copy(update={"excluded": ["secret.py: exclusion policy"]})
    result = build_context(record, paths=["secret.py", "service.py"], max_chars=260)
    assert "secret.py" in result
    assert result.endswith(TRUNCATION_MARKER)
    assert len(result) <= 260


def test_context_without_paths_prefers_changed_files_and_is_deterministic():
    first = build_context(snapshot())
    assert first == build_context(snapshot())
    assert "service.py" in first


def test_context_does_not_read_the_live_filesystem():
    record = snapshot().model_copy(update={"root": "/definitely/not/a/repository"})
    assert "calculate_total" in build_context(record)


def test_deleted_index_module_and_removed_exports_find_only_bounded_current_callers():
    record = Snapshot(
        id="deleted-index",
        root="/unused",
        head_sha="head",
        base_sha="base",
        mode="working",
        files={
            "app.ts": 'import { normalizeUser } from "./lib";\nnormalizeUser(user);\n',
            "test.ts": "expect(normalizeUser(user)).toEqual(expected);\n",
            "unrelated.ts": "export const count = 1;\n",
            "package.json": '{"name":"unrelated"}\n',
        },
        changed_paths=["lib/index.ts"],
        deleted_paths=["lib/index.ts"],
        diff="--- a/lib/index.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-export const normalizeUser = user => user;\n",
    )
    paths, gaps = affected_by_deletions(record, max_paths=1)
    assert paths == ["app.ts"]
    assert gaps == ["Deletion caller scope capped at 1 files; 1 candidate files omitted"]
    assert "lib/index.ts" not in record.files
