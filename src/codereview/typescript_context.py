"""Lexical TypeScript/JavaScript relationships within captured source only.

These candidates are useful review context, not a compiler module graph. Package
exports, path aliases, conditional resolution and computed imports are not resolved.
"""

import json
import re
from pathlib import PurePosixPath

SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_SUBSTITUTIONS = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".jsx": (".tsx", ".d.ts", ".jsx"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
}
_CONFIG = re.compile(r"(?:tsconfig|jsconfig)(?:\.[^/]+)?\.jsonc?$")
# Keep strings as single tokens so commented imports and import-like text inside
# quoted strings do not become edges. Template expressions remain deliberately opaque.
_TOKENS = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
    r"|//[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_$][\w$]*|[^\s]"
)
_JSONC_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
_JSONC_TRAILING_COMMAS = re.compile(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])')


def _tokens(text: str) -> list[str]:
    return [
        match.group()
        for match in _TOKENS.finditer(text)
        if not match.group().startswith(("//", "/*"))
    ]


def _specifier(token: str) -> str | None:
    if len(token) >= 2 and token[0] in {"'", '"'} and "\\" not in token:
        return token[1:-1]
    return None


def _imports(text: str) -> set[str]:
    tokens = _tokens(text)
    result: set[str] = set()
    for index, token in enumerate(tokens):
        if token not in {"import", "export", "require"}:
            continue
        if index and tokens[index - 1] in {".", "?."}:
            continue
        following = tokens[index + 1 : index + 3]
        if not following:
            continue
        candidate = None
        if token in {"import", "require"} and len(following) == 2 and following[0] == "(":
            if index + 3 < len(tokens) and tokens[index + 3] in {")", ","}:
                candidate = _specifier(following[1])
        elif token == "import" and _specifier(following[0]) is not None:
            candidate = _specifier(following[0])
        elif token == "import" or (token == "export" and following[0] in {"*", "{", "type"}):
            for cursor in range(index + 1, len(tokens) - 1):
                if tokens[cursor] in {";", "=", "import", "export"}:
                    break
                if tokens[cursor] == "from":
                    candidate = _specifier(tokens[cursor + 1])
                    break
        if candidate and candidate.startswith(("./", "../")):
            result.add(candidate)
    return result


def _relative(path: str, specifier: str) -> str | None:
    """Normalize a repository-relative path without filesystem access or escape."""
    if not specifier or specifier.startswith("/") or "\\" in specifier or ":" in specifier:
        return None
    parts = list(PurePosixPath(path).parent.parts)
    for part in specifier.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _resolve_module(files: dict[str, str], path: str, specifier: str) -> str | None:
    target = _relative(path, specifier)
    if target is None:
        return None
    suffix = PurePosixPath(target).suffix
    if suffix in _SUBSTITUTIONS:
        candidates = [target[: -len(suffix)] + ext for ext in _SUBSTITUTIONS[suffix]]
    elif suffix in SOURCE_SUFFIXES or suffix == ".json":
        candidates = [target]
    else:
        candidates = [target + ext for ext in _EXTENSIONS]
        index = str(PurePosixPath(target) / "index")
        candidates += [index + ext for ext in _EXTENSIONS]
    return next((candidate for candidate in candidates if candidate in files), None)


def is_project_config(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        bool(_CONFIG.fullmatch(name))
        or name == "package.json"
        or name
        in {
            "next.config.js",
            "next.config.mjs",
            "next.config.ts",
            "next.config.cjs",
        }
    )


def _config_links(files: dict[str, str], path: str) -> set[str]:
    # JSONC permits comments and trailing commas, but remains data, never code.
    content = _JSONC_COMMENTS.sub(
        lambda match: match.group() if match.group().startswith('"') else " ", files[path]
    )
    content = _JSONC_TRAILING_COMMAS.sub(
        lambda match: match.group() if match.group().startswith('"') else "", content
    )
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return set()
    if not isinstance(data, dict):
        return set()
    extends = data.get("extends", [])
    if isinstance(extends, str):
        extends = [extends]
    links: list[tuple[str, bool]] = []
    if isinstance(extends, list):
        links.extend(
            (item, False)
            for item in extends
            if isinstance(item, str) and item.startswith(("./", "../"))
        )
    references = data.get("references", [])
    if isinstance(references, list):
        links.extend(
            (item["path"], True)
            for item in references
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )
    result = set()
    for specifier, directory in links:
        target = _relative(path, specifier)
        if target is None:
            continue
        candidates = [target, target + ".json"]
        if directory:
            candidates.append(str(PurePosixPath(target) / "tsconfig.json"))
        resolved = next((item for item in candidates if item in files), None)
        if resolved:
            result.add(resolved)
    return result


def related_context(files: dict[str, str], paths: list[str]) -> dict[str, int]:
    """Rank local config, direct dependencies, and direct importers deterministically.

    All loops are bounded by the captured snapshot. Configuration links are followed
    with cycle detection; source module edges deliberately stop after one hop.
    """
    requested = set(paths)
    if not any(
        PurePosixPath(path).suffix in SOURCE_SUFFIXES or _CONFIG.fullmatch(PurePosixPath(path).name)
        for path in paths
    ):
        return {}
    related: dict[str, int] = {}
    for path, text in sorted(files.items()):
        if PurePosixPath(path).suffix not in SOURCE_SUFFIXES:
            continue
        targets = {
            resolved
            for specifier in _imports(text)
            if (resolved := _resolve_module(files, path, specifier)) is not None
        }
        if path in requested:
            related.update((target, 1) for target in targets)
        elif targets & requested:
            related[path] = min(related.get(path, 2), 2)
    ancestors = {
        parent.as_posix()
        for path in requested | related.keys()
        for parent in PurePosixPath(path).parents
    }
    pending = sorted(
        path
        for path in files
        if is_project_config(path) and PurePosixPath(path).parent.as_posix() in ancestors
    )
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        related[path] = 0
        if PurePosixPath(path).name != "package.json" and path.endswith((".json", ".jsonc")):
            pending.extend(sorted(_config_links(files, path) - visited))
    return related
