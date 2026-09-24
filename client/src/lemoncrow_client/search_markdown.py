"""Compact, agent-facing Markdown for ``code_search``.

Retrieval and ranking stay server-owned.  This module is deliberately a pure
presentation layer over ranked ``QueryHit`` pointers plus a source hydrator, so
a same-host thin client can later hydrate from its checkout while hosted clients
hydrate from the content store and both produce byte-for-byte equivalent output.
"""

from __future__ import annotations

import ast
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Lock

from .manifest import sha256_hex

SearchHit = Mapping[str, object]
SearchAnswer = Mapping[str, object]
SourceLoader = Callable[[SearchHit], bytes | None]
NavigationExtent = Callable[[SearchHit, bytes | None], int | None]

# Only verified navigation extents, never source bytes. Bounded for long sessions
# and discarded on process exit; digest keys make source changes cache misses.
_LOCAL_EXTENTS: OrderedDict[tuple[str, str, str, str, int], int] = OrderedDict()
_LOCAL_EXTENTS_LOCK = Lock()
_MAX_LOCAL_EXTENTS = 4096

_MAX_INLINE_CHARS = 8_000
_FUZZY_BEFORE = 2
_FUZZY_AFTER = 5
_SYMBOL_QUERY_RE = re.compile(r"^[A-Za-z_$][\w.$:-]*$")
_DEFINITION_QUERY_RE = re.compile(
    r"^(?:def|class|function|func|method|type|interface|struct|enum|const|var)\s+([A-Za-z_$][\w.$:-]*)$",
    re.IGNORECASE,
)


def render_code_search_markdown(
    query: str,
    answer: SearchAnswer,
    *,
    load_source: SourceLoader,
    navigation_extent: NavigationExtent | None = None,
) -> str:
    """Render ranked search pointers as compact, edit-oriented Markdown.

    The renderer intentionally omits implementation telemetry (scores, sizes,
    digests, searched-path counts, view revisions) from normal model context.
    Those remain available in ``structured``/diagnostics.  Actionable state --
    confidence, source, ranges, navigation and truncation -- remains visible.
    """
    hits = _hits(answer)
    if not hits:
        return "no matches"

    exact = _is_exact(query, hits)
    blocks: list[str] = ["= exact" if exact else "~ ranked"]
    hydrated: set[str] = set()
    source_cache: dict[str, bytes | None] = {}
    inline_slots = 2 if exact else 1

    def source_for(hit: SearchHit) -> bytes | None:
        if _path(hit) not in source_cache:
            source_cache[_path(hit)] = load_source(hit)
        return source_cache[_path(hit)]

    for hit in hits:
        if len(hydrated) >= inline_slots:
            break
        source = source_for(hit)
        if source is None:
            continue
        block = _source_block(query, hit, source, exact=exact)
        if block is None:
            continue
        blocks.append(block)
        hydrated.add(_path(hit))

    nav: list[tuple[str, str, str]] = []
    for hit in hits:
        if _path(hit) in hydrated:
            continue
        end_line = navigation_extent(hit, None) if navigation_extent is not None else None
        data = source_for(hit) if _definitions(hit) and end_line is None else None
        if end_line is None and data is not None and navigation_extent is not None:
            end_line = navigation_extent(hit, data)
        pointer, symbol = _pointer(hit, data, end_line=end_line)
        nav.append((_path(hit), pointer, symbol))

    if nav:
        blocks.append(_render_navigation(nav))

    remaining = max(0, _int(answer.get("remaining_hits"), 0))
    if remaining:
        blocks.append(f"+{remaining} more")
    elif bool(answer.get("truncated")):
        blocks.append("+more")

    if bool(answer.get("degraded")):
        reason = str(answer.get("degraded_reason") or "").strip()
        blocks.append(f"! degraded{': ' + reason if reason else ''}")

    return "\n\n".join(blocks)


def render_local_code_search(query: str, answer: SearchAnswer, *, repo_root: Path) -> str | None:
    """Hydrate only bytes that exactly match the server's content pointer.

    ``None`` means the caller must retry with server hydration. Missing hydration
    pointers are legitimate path-only hits and do not trigger fallback; a pointer
    that exists but cannot be verified does.
    """
    raw_hydration = answer.get("_hydration")
    hydration = raw_hydration if isinstance(raw_hydration, Mapping) else {}
    failed = False
    resolved_root = repo_root.resolve()

    def navigation_extent(hit: SearchHit, data: bytes | None) -> int | None:
        definitions = _definitions(hit)
        expected = hydration.get(_path(hit))
        if not definitions or not isinstance(expected, str) or not expected:
            return None
        definition = definitions[0]
        name = str(definition.get("name") or "")
        line = _positive_int(definition.get("line"), 1)
        key = (str(resolved_root), _path(hit), expected, name, line)
        with _LOCAL_EXTENTS_LOCK:
            cached = _LOCAL_EXTENTS.get(key)
            if cached is not None:
                _LOCAL_EXTENTS.move_to_end(key)
                return cached
        if data is None:
            return None
        # data came exclusively from load_source after digest/path verification.
        end_line = _definition_end_line(_path(hit), name, data, line)
        with _LOCAL_EXTENTS_LOCK:
            _LOCAL_EXTENTS[key] = end_line
            _LOCAL_EXTENTS.move_to_end(key)
            while len(_LOCAL_EXTENTS) > _MAX_LOCAL_EXTENTS:
                _LOCAL_EXTENTS.popitem(last=False)
        return end_line

    def load_source(hit: SearchHit) -> bytes | None:
        nonlocal failed
        path = _path(hit)
        expected = hydration.get(path)
        if not isinstance(expected, str) or not expected:
            return None
        target = (repo_root / path).resolve()
        try:
            target.relative_to(resolved_root)
            data = target.read_bytes()
        except (OSError, ValueError):
            failed = True
            return None
        if sha256_hex(data) != expected:
            failed = True
            return None
        return data

    rendered = render_code_search_markdown(query, answer, load_source=load_source, navigation_extent=navigation_extent)
    return None if failed else rendered


def _hits(answer: SearchAnswer) -> list[SearchHit]:
    raw = answer.get("hits")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _path(hit: SearchHit) -> str:
    return str(hit.get("path") or "?")


def _detail(hit: SearchHit) -> Mapping[str, object]:
    raw = hit.get("detail")
    return raw if isinstance(raw, Mapping) else {}


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _is_exact(query: str, hits: Sequence[SearchHit]) -> bool:
    stripped = query.strip()
    if not stripped:
        return False
    if hits and (_path(hits[0]) == stripped or _path(hits[0]).rsplit("/", 1)[-1] == stripped):
        return True
    names = _exact_symbol_names(stripped)
    if not names:
        return False
    for hit in hits:
        for definition in _definitions(hit):
            name = str(definition.get("name") or "")
            if name in names:
                return True
    return False


def _exact_symbol_names(query: str) -> frozenset[str]:
    names: set[str] = set()
    for alternative in query.split("|"):
        value = alternative.strip()
        if not value:
            continue
        match = _DEFINITION_QUERY_RE.fullmatch(value)
        if match:
            names.add(match.group(1))
        elif _SYMBOL_QUERY_RE.fullmatch(value):
            names.add(value)
    return frozenset(names)


def _definitions(hit: SearchHit) -> list[Mapping[str, object]]:
    raw = _detail(hit).get("definitions")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _source_block(query: str, hit: SearchHit, data: bytes, *, exact: bool) -> str | None:
    definitions = _definitions(hit)
    definition = _preferred_definition(query, definitions)
    if definition is not None:
        return _definition_block(_path(hit), definition, data)

    # An exact path lookup of a small non-symbol file is already a read.  Keep
    # that useful old LemonCrow behavior; large files stay bounded and point to
    # ``:full`` rather than poisoning the remaining context window.
    path = _path(hit)
    if exact and (query.strip() == path or query.strip() == path.rsplit("/", 1)[-1]):
        return _whole_file_block(path, data)

    return _lexical_block(query, hit, data)


def _preferred_definition(
    query: str,
    definitions: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    if not definitions:
        return None
    names = _exact_symbol_names(query.strip())
    for definition in definitions:
        if str(definition.get("name") or "") in names:
            return definition
    return definitions[0]


def _definition_block(path: str, definition: Mapping[str, object], data: bytes) -> str:
    name = str(definition.get("name") or "")
    start_line = _positive_int(definition.get("line"), 1)
    end_line = _definition_end_line(path, name, data, start_line)
    body = _line_window(data, start_line, end_line).rstrip("\n")
    if not body:
        body = _line_window(data, start_line, start_line)
    body, tail = _bound_source(body, f"read {path}:L{start_line}-L{end_line}")
    header = f"## {path}:L{start_line}-L{end_line}"
    if name:
        header += f" · {name}"
    rendered = f"{header}\n{_sparse_gutter(body, start_line)}"
    return rendered + tail


def _whole_file_block(path: str, data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").rstrip("\n")
    total_lines = max(1, data.count(b"\n") + (0 if data.endswith(b"\n") else 1))
    text, tail = _bound_source(text, f"read {path}:full")
    return f"## {path}:L1-L{total_lines}\n{_sparse_gutter(text, 1)}" + tail


def _lexical_block(query: str, hit: SearchHit, data: bytes) -> str | None:
    source = data.decode("utf-8", errors="replace").splitlines()
    if not source:
        return f"## {_path(hit)} · empty"
    line = _positive_int(_detail(hit).get("line"), 0, allow_zero=True)
    if line <= 0:
        needles = [token.lower() for token in re.findall(r"[A-Za-z_$][\w$.-]*", query) if len(token) > 1]
        line = next(
            (
                number
                for number, text in enumerate(source, 1)
                if query in text or any(needle in text.lower() for needle in needles)
            ),
            1,
        )
    first, last = max(1, line - _FUZZY_BEFORE), min(len(source), line + _FUZZY_AFTER)
    body = "\n".join(source[first - 1 : last])
    path = _path(hit)
    body, tail = _bound_source(body, f"read {path}:L{first}-L{last}")
    return f"## {path}:L{first}-L{last}\n{_sparse_gutter(body, first)}" + tail


def _pointer(hit: SearchHit, data: bytes | None = None, *, end_line: int | None = None) -> tuple[str, str]:
    definitions = _definitions(hit)
    if definitions:
        definition = definitions[0]
        line = _positive_int(definition.get("line"), 1)
        name = str(definition.get("name") or "")
        pointer = f"{_path(hit)}:L{line}"
        if end_line is None and data is not None:
            end_line = _definition_end_line(_path(hit), name, data, line)
        if end_line is not None and end_line > line:
            pointer = f"{_path(hit)}:L{line}-L{end_line}"
        return pointer, name
    detail = _detail(hit)
    line = _positive_int(detail.get("line"), 0, allow_zero=True)
    symbol = str(detail.get("related_symbol") or "")
    path = _path(hit)
    return (f"{path}:L{line}" if line > 0 else path), symbol


def _render_navigation(items: Sequence[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    index = 0
    while index < len(items):
        path, pointer, symbol = items[index]
        if symbol or pointer != path:
            lines.append(f"→ {pointer}" + (f" · {symbol}" if symbol else ""))
            index += 1
            continue

        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        run = [path]
        cursor = index + 1
        while cursor < len(items):
            other_path, other_pointer, other_symbol = items[cursor]
            other_parent = other_path.rsplit("/", 1)[0] if "/" in other_path else ""
            if other_symbol or other_pointer != other_path or other_parent != parent:
                break
            run.append(other_path)
            cursor += 1
        if len(run) == 1:
            lines.append(f"→ {path}")
        else:
            names = ",".join(item.rsplit("/", 1)[-1] for item in run)
            prefix = f"{parent}/" if parent else ""
            lines.append(f"→ {prefix}{{{names}}}")
        index = cursor
    return "\n".join(lines)


def _bound_source(text: str, pointer: str) -> tuple[str, str]:
    if len(text) <= _MAX_INLINE_CHARS:
        return text, ""
    return text[:_MAX_INLINE_CHARS].rstrip(), f"\n… {pointer}"


def _sparse_gutter(text: str, start_line: int) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    if len(lines) == 1:
        return f"{start_line} {lines[0]}"
    width = len(str(start_line))
    continuation = " " * (width + 1)
    return "\n".join([f"{start_line} {lines[0]}", *(continuation + line for line in lines[1:])])


def _definition_end_line(path: str, name: str, data: bytes, start_line: int) -> int:
    """Return a useful source extent for a symbol definition.

    Index ``SymbolDef.start/end`` offsets intentionally identify only the symbol
    name, not its body.  Model-facing search therefore derives the body extent
    from source structure instead of mistaking identifier offsets for a source
    span. Python gets exact AST ``end_lineno``; brace languages get a bounded
    balanced-block scan; unknown syntax falls back to the normal snippet window.
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return start_line
    start_line = min(max(1, start_line), len(lines))

    if Path(path).suffix.lower() in {".py", ".pyi"}:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            candidates = (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name
                and node.lineno == start_line
            )
            node = next(candidates, None)
            end_line = getattr(node, "end_lineno", None)
            if isinstance(end_line, int):
                return max(start_line, min(len(lines), end_line))

    # For C/JS/TS/Go/Rust/Java-style definitions, locate the first opening brace
    # at or shortly after the declaration and stop when its block balances.
    balance = 0
    saw_open = False
    scan_limit = min(len(lines), start_line - 1 + 200)
    for index in range(start_line - 1, scan_limit):
        line = lines[index]
        opens = line.count("{")
        closes = line.count("}")
        if opens:
            saw_open = True
        if saw_open:
            balance += opens - closes
            if balance <= 0:
                return index + 1

    return min(len(lines), start_line + _FUZZY_AFTER)


def _line_offset(data: bytes, line: int) -> int:
    if line <= 1:
        return 0
    offset = 0
    for _ in range(line - 1):
        newline = data.find(b"\n", offset)
        if newline < 0:
            return len(data)
        offset = newline + 1
    return offset


def _line_window(data: bytes, first: int, last: int) -> str:
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[max(0, first - 1) : max(first, last)])


def _positive_int(value: object, default: int, *, allow_zero: bool = False) -> int:
    try:
        result = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    if result < 0 or (result == 0 and not allow_zero):
        return default
    return result
