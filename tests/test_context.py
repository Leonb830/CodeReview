from codereview.context import TRUNCATION_MARKER, affected_by_deletions, build_context
from codereview.models import Snapshot
from codereview.typescript_context import related_context


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


def typescript_snapshot(files: dict[str, str], path: str = "src/main.ts") -> Snapshot:
    return Snapshot(
        id="typescript",
        root="/definitely/not/a/repository",
        head_sha="head",
        base_sha="base",
        mode="working",
        files=files,
        changed_paths=[path],
    )


def test_typescript_relative_imports_resolve_runtime_extensions_indexes_and_type_reexports():
    record = typescript_snapshot(
        {
            "src/main.ts": (
                'import value from "./a.js";\n'
                'import type { B } from "./b";\n'
                'export type { C } from "./c.mjs";\n'
                'export * from "./d.cjs";\n'
                'import "./e";\n'
                'const promise = import("./f");\n'
                'const legacy = require("./g");\n'
            ),
            "src/a.ts": "export default 1;\n",
            "src/a.js": "export default 2;\n",
            "src/b/index.ts": "export type B = number;\n",
            "src/c.mts": "export type C = number;\n",
            "src/d.cts": "export const D = 4;\n",
            "src/e.tsx": "export default <div />;\n",
            "src/f.ts": "export default 6;\n",
            "src/g/index.js": "module.exports = 7;\n",
        }
    )
    related = related_context(record.files, record.changed_paths)
    assert set(related) == {
        "src/a.ts",
        "src/b/index.ts",
        "src/c.mts",
        "src/d.cts",
        "src/e.tsx",
        "src/f.ts",
        "src/g/index.js",
    }
    context = build_context(record)
    for path in related:
        assert f"## Related source: {path}\n" in context


def test_typescript_reverse_imports_find_default_and_namespace_consumers():
    files = {
        "src/x.ts": "export default 1;\n",
        "src/consumer.ts": 'import selected from "./x.js";\n',
        "tests/check.ts": 'import * as subject from "../src/x";\n',
        "src/unrelated.ts": 'import selected from "./elsewhere";\n',
    }
    record = typescript_snapshot(files, "src/x.ts")
    assert set(related_context(files, record.changed_paths)) == {
        "src/consumer.ts",
        "tests/check.ts",
    }
    context = build_context(record)
    assert "## Related source: src/consumer.ts" in context
    assert "## Related source: tests/check.ts" in context
    assert "## Related source: src/unrelated.ts" not in context


def test_typescript_context_follows_local_jsonc_configs_and_bounds_cycles():
    record = typescript_snapshot(
        {
            "apps/web/src/main.ts": "export default 1;\n",
            "apps/web/package.json": '{"type":"module"}',
            "apps/web/tsconfig.app.json": (
                '{ // local compiler policy\n"extends": ["../../config/base", "@shared/tsconfig"],'
                '"references": [{"path":"../../packages/contracts"}],}'
            ),
            "config/base.json": '{"compilerOptions":{"strict":true}, "extends":"./cycle.json"}',
            "config/cycle.json": '{"extends":"./base.json"}',
            "packages/contracts/tsconfig.json": '{"compilerOptions":{"composite":true}}',
            "package.json": '{"workspaces":["apps/*", "packages/*"]}',
            "apps/unrelated/tsconfig.json": '{"compilerOptions":{"strict":false}}',
            "apps/unrelated/package.json": '{"name":"other"}',
        },
        "apps/web/src/main.ts",
    )
    related = related_context(record.files, record.changed_paths)
    assert set(related) == {
        "apps/web/package.json",
        "apps/web/tsconfig.app.json",
        "config/base.json",
        "config/cycle.json",
        "packages/contracts/tsconfig.json",
        "package.json",
    }
    context = build_context(record)
    assert '"strict":true' in context
    assert '"strict":false' not in context
    assert "apps/unrelated/package.json" not in context
    assert context.index("apps/web/package.json") < context.index("## Related source: package.json")
    assert context.count("## Related source: config/base.json") == 1


def test_typescript_import_context_ignores_comments_strings_aliases_and_computed_paths():
    files = {
        "src/main.ts": (
            '// import "./a";\n/* export * from "./b"; */\n'
            "const note = \"import './c'\";\n"
            'const template = `import "./d"`;\n'
            'import ignored from "@/e";\n'
            'const lazy = import("./f" + suffix);\n'
            'import outside from "../../g";\n'
        ),
        **{f"src/{letter}.ts": "export default 1;" for letter in "abcdef"},
        "g.ts": "export default 7;",
        "other/tsconfig.json": "{}",
    }
    assert related_context(files, ["src/main.ts"]) == {}
    assert "## Related source: other/tsconfig.json" not in build_context(typescript_snapshot(files))


def test_typescript_config_context_is_deterministic_and_keeps_truncation_visible():
    files = {
        "src/main.ts": 'import value from "./x";\n',
        "src/x.ts": "export default 1;\n",
        "tsconfig.json": '{"extends":"../../outside", "references":[{"path":"../outside"}]}',
        "tsconfig.broken.json": '{"extends": invalid}',
        "apps/unrelated/package.json": "x" * 10_000,
    }
    record = typescript_snapshot(files)
    context = build_context(record)
    assert context == build_context(
        record.model_copy(update={"files": dict(reversed(list(files.items())))})
    )
    assert "## Related source: src/x.ts" in context
    bounded = build_context(record, max_chars=220)
    assert len(bounded) == 220
    assert bounded.endswith(TRUNCATION_MARKER)


def test_typescript_context_resolves_root_directory_and_jsx_runtime_imports():
    files = {
        "main.ts": 'import value from "./";\nimport view from "./view.js";\n',
        "index.ts": "export default 1;\n",
        "view.jsx": "export default <div />;\n",
        "tsconfig.json": '{"references":[{"path":"."}]}',
    }
    assert set(related_context(files, ["main.ts"])) == {
        "index.ts",
        "view.jsx",
        "tsconfig.json",
    }


def test_typescript_malformed_jsonc_is_retained_without_following_invented_config_links():
    files = {
        "main.ts": "export default 1;\n",
        "tsconfig.json": '{"extends":"./config/base.json", "invalid": 1 2}',
        "config/base.json": '{"compilerOptions":{"strict":true}}',
    }
    assert set(related_context(files, ["main.ts"])) == {"tsconfig.json"}
