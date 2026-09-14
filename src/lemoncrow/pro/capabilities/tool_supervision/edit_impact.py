"""Evidence-only post-edit discovery for contract literals removed by an edit.

Renames and deprecations often span independent consumers with no call-graph edge
to the edited site: configuration keys, wire fields, and dict literals are plain
strings, invisible to symbol-level callers/callees/usages. When an edit removes a
quoted literal, surface the remaining occurrences in *other* files so the agent can
inspect parallel consumers while its implementation hypothesis is still revisable.

Detection combines two layers for precision *and* recall: ast-grep (the engine
behind the ``codemod`` tool) matches the literal as a string *node*, so it is precise
for code files -- it never matches the same text inside a larger string, a docstring,
or a comment. A language-agnostic text search (guarded by a structural heuristic)
adds the non-code consumers ast-grep can't parse (config, templates, docs). ast-grep
is authoritative for any code file it covers; text contributes the rest, and is the
sole path when the ast-grep binary is unavailable.

This module extracts the removed literals and module symbols and shapes the evidence.
It never blocks or rolls back an edit, and fails open (returns ``None``) on any error.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.core.foundation.redaction import redact_tool_output

# Decorators whose removal silently strips attributes/methods callers may use, with
# no call-graph edge to surface the breakage. ``functools.lru_cache``/``cache`` add
# ``.cache_clear``/``.cache_info``/``.cache_parameters`` to the wrapped function;
# removing the decorator makes every ``fn.cache_clear()`` elsewhere an AttributeError.
# Generic stdlib contract -- not project-specific. Extend this map (e.g. cached_property)
# as other attribute-providing decorators prove worth surfacing.
_DECORATOR_PROVIDED_ATTRS: dict[str, tuple[str, ...]] = {
    "lru_cache": ("cache_clear", "cache_info", "cache_parameters"),
    "cache": ("cache_clear", "cache_info", "cache_parameters"),
}
# A cache decorator immediately above a (possibly async) def -- captures both.
_CACHE_DECORATED_DEF_RE = re.compile(
    r"@(?:\w+\.)*(?P<deco>" + "|".join(_DECORATOR_PROVIDED_ATTRS) + r")\b[^\n]*\n\s*(?:async\s+)?def\s+(?P<name>\w+)"
)
_NOISY_LITERALS = frozenset(
    {
        "",
        "0",
        "1",
        "false",
        "true",
        "none",
        "null",
        # Common bool synonyms -- too short and ubiquitous to be meaningful contract
        # literals (e.g. env-var coercions like `in {"1", "true", "yes", "on"}`
        # appear in dozens of unrelated places and generate FIXME noise).
        "yes",
        "no",
        "on",
        "off",
    }
)
# A literal found in this many distinct files is ambient vocabulary (e.g. common
# bool synonyms, status words) rather than a contract identifier. Skip it.
_MAX_LITERAL_FILE_SPREAD = 4

_QUOTES = ("'", '"', "`")
_DELIMITERS = frozenset("[]{}():,=")
_MAX_FILE_BYTES = 1_000_000

# Recorded when a blob could not be parsed, so no literal was extracted from it.
# The name rides out of ``contract_literal_impact`` and into the review packet's
# ``degraded`` list: an absent signal is honest, a guessed one is not.
LITERAL_SCAN_UNPARSED = "contract_literal_unparsed"

# Recorded when the batched ast-grep scan found more matches than one pass will
# materialise. Same contract as the name above: a candidate missing from a capped
# scan may be missing for a reason that has nothing to do with the code, so the
# packet says "this pass was partial" instead of presenting it as exhaustive.
LITERAL_SCAN_TRUNCATED = "contract_literal_scan_truncated"

# The same claim, for the two detectors whose ast-grep pass shares that ceiling:
# a candidate with no rows may be missing because the cap cut the scan short, not
# because no site survives. Distinct names because a reviewer reading ``degraded``
# is owed which pass went partial, not just that one did.
SYMBOL_SCAN_TRUNCATED = "symbol_contract_scan_truncated"
SIGNATURE_SCAN_TRUNCATED = "signature_change_scan_truncated"


class DetectorSites(list[dict[str, Any]]):
    """A detector's flat site list plus the names of anything partial about it.

    ``symbol_contract_impact`` and ``signature_change_impact`` are consumed two
    ways: the edit hook does ``sites.extend(detector(...))`` and wants a plain
    list, while the review packet harvests a ``degraded`` channel so a capped or
    fallen-back pass is never presented as an exhaustive one. Subclassing ``list``
    serves both -- the first caller needs no change at all, and the second reads
    ``.degraded`` -- where a mapping return would have silently turned the hook's
    ``extend`` into a list of key strings.

    Empty ``degraded`` is the normal case and means what it says: this pass looked
    everywhere it claims to have looked.
    """

    def __init__(self, sites: Iterable[dict[str, Any]] = (), *, degraded: Sequence[str] = ()) -> None:
        super().__init__(sites)
        self.degraded: tuple[str, ...] = tuple(degraded)


# Tree-sitter node kinds that are string literals but whose kind name does not
# contain "string" (YAML quoted scalars, C-family chars, PHP/HTML attributes).
_EXTRA_STRING_NODE_KINDS = frozenset(
    {
        "char_literal",
        "character_literal",
        "single_quote_scalar",
        "double_quote_scalar",
        "quoted_attribute_value",
        "encapsed_string",
    }
)
# Kind-name fragments that mark a string node as an interpolation template
# (`${x}`, `#{x}`) rather than a literal. Its source text is not a value any
# consumer can hold verbatim, so searching for it can only mislead.
_INTERPOLATION_MARKERS = ("interpolation", "substitution")
# Suffixes of tree-sitter kinds naming the *inside* of a string node; the walk
# stops at the outermost string node, so these are belt-and-braces.
_STRING_PART_SUFFIXES = ("_content", "_fragment")
# Rich-edit target suffixes ("f.py:L3-L9", "f.py:full", "f.py:head=40"). Only the
# file extension matters here, so they are stripped before language detection.
_TARGET_SUFFIX_RE = re.compile(
    r":(?:L?\d+(?:-L?\d*)?|full|minified|summary|outline|head=\d+|tail=\d+)(?:=(?:true|1))?$",
    re.IGNORECASE,
)

# Edited-file extension -> ast-grep language name. ast-grep matches per language,
# so detection covers the language(s) of the files actually edited.
_EXT_TO_ASTGREP_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".kt": "kotlin",
    ".swift": "swift",
}


if TYPE_CHECKING:
    from lemoncrow.core.capabilities.tool_supervision_contract import _TextSearcher


@dataclass(frozen=True)
class _LiteralScan:
    """The quoted literals a parser found in one blob -- or why it found none.

    ``reason`` non-empty means the blob was never parsed, so ``literals`` is
    *unknown*, not *empty*. The distinction is the whole point of this type: a
    caller that treats an unparsed side's empty set as fact and subtracts it
    reports every literal on the other side as removed, which is precisely the
    fabrication this detector exists to avoid.
    """

    literals: frozenset[str] = frozenset()
    # 1-based source line -> the literals whose token starts on it. Carried out
    # of the same parse so rename detection never re-scans a line in isolation
    # (a single line of a multi-line expression rarely parses on its own).
    by_line: dict[int, frozenset[str]] = field(default_factory=dict)
    reason: str = ""


def _accept_literal(literal: str) -> bool:
    """Keep only literals that can be searched for verbatim and mean something.

    Escaped values can't be searched literally without language-specific
    decoding; one-character and very long prose strings are noisy.
    """
    return (
        2 <= len(literal) <= 80
        and "\\" not in literal
        and "\n" not in literal
        and literal.strip().lower() not in _NOISY_LITERALS
    )


def _string_token_body(token: str) -> str | None:
    """Inner *source* text of a Python string token, or ``None`` when unsearchable.

    Source form, not decoded value: every downstream consumer (engine text search,
    ast-grep pattern) matches file bytes, so handing them a decoded escape would
    search for text that exists in no file. f-strings are dropped outright -- their
    source is a template, and neither the template nor its fixed slices are a value
    a parallel consumer can hold.
    """
    index = 0
    while index < len(token) and token[index].isalpha():
        index += 1
    if "f" in token[:index].lower():
        return None
    rest = token[index:]
    for quote in ('"""', "'''", '"', "'"):
        if len(rest) >= 2 * len(quote) and rest.startswith(quote) and rest.endswith(quote):
            return rest[len(quote) : -len(quote)]
    return None


def _python_literal_tokens(text: str) -> list[tuple[int, str]] | None:
    """``(line, inner source)`` per Python string literal; ``None`` if untokenizable.

    ``tokenize`` is the entire reason this is not a regex: it knows that a ``#``
    inside a string opens no comment, that a quote inside a comment opens no
    string, and that ``'daemon\\'s'`` is one token rather than the start of a
    desynchronised scan that mis-slices every quote in the rest of the file.

    Bare string statements -- docstrings -- are skipped. A string that *is* a
    statement is prose: no consumer holds it, so its removal breaks no contract,
    and reporting one as a "contract literal still used elsewhere" is noise
    dressed as a finding.

    Unless the blob is nothing *but* strings. Edit hooks hand over fragments, and
    ``old_string='"passwd"'`` is a caller renaming that literal and nothing else --
    the one case where a bare string statement is the subject of the edit rather
    than documentation around it.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:  # unparsable blob: report nothing, never guess
        return None
    # COMMENT/NL carry no literals and would break statement-boundary tracking.
    coded = [tok for tok in tokens if tok.type not in (tokenize.COMMENT, tokenize.NL)]
    breaks = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING)
    structural = (*breaks, tokenize.ENDMARKER)
    out: list[tuple[int, str]] = []
    bare: list[tuple[int, str]] = []
    has_other_code = False
    at_statement_start = True
    for position, tok in enumerate(coded):
        if tok.type in breaks or (tok.type == tokenize.OP and tok.string == ";"):
            at_statement_start = True
            continue
        opened_statement = at_statement_start
        at_statement_start = False
        if tok.type != tokenize.STRING:
            has_other_code = has_other_code or tok.type not in structural
            continue
        body = _string_token_body(tok.string)
        if body is None:
            continue
        if opened_statement:
            following = coded[position + 1] if position + 1 < len(coded) else None
            ends_statement = (
                following is None
                or following.type in (tokenize.NEWLINE, tokenize.ENDMARKER)
                or (following.type == tokenize.OP and following.string == ";")
            )
            if ends_statement:
                bare.append((tok.start[0], body))  # docstring, pending the check below
                continue
        out.append((tok.start[0], body))
    return out if has_other_code else out + bare


def _is_string_node(kind: str) -> bool:
    if kind in _EXTRA_STRING_NODE_KINDS:
        return True
    return "string" in kind and not kind.endswith(_STRING_PART_SUFFIXES)


def _tree_sitter_string_body(node: Any, source: bytes) -> str | None:
    """Inner source text of one tree-sitter string node, or ``None`` to skip it.

    Skips anything whose delimiters this function cannot name with certainty --
    interpolated templates, Rust-style ``r#".."#``, YAML plain scalars. Skipping
    costs one candidate; guessing at the delimiters is how a scanner starts
    inventing them.
    """
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if any(marker in str(getattr(current, "type", "")) for marker in _INTERPOLATION_MARKERS):
            return None
        stack.extend(getattr(current, "children", ()) or ())
    try:
        raw = source[int(node.start_byte) : int(node.end_byte)].decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    index = 0
    while index < len(raw) and raw[index].isalpha():
        index += 1
    rest = raw[index:]
    if len(rest) < 2 or rest[0] not in _QUOTES or rest[-1] != rest[0]:
        return None
    return rest[1:-1]


def _tree_sitter_literal_tokens(text: str, language: str) -> list[tuple[int, str]] | None:
    """``(line, inner source)`` per string node; ``None`` when the blob won't parse.

    A grammar's string nodes are the non-Python equivalent of ``tokenize``: they
    already exclude comments and already own their escapes. ``has_error`` is
    treated as unparsed rather than partial, because a tree with an ERROR node is
    exactly where the parser's own idea of "inside a string" went wrong -- and a
    syntax error mid-edit is normal in a diff, not exceptional.
    """
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import tree_sitter_parser

    try:
        parser = tree_sitter_parser(language)
        if parser is None:
            return None
        source = text.encode("utf-8")
        root = parser.parse(source).root_node
    except Exception:  # grammar missing or wedged: report nothing
        return None
    if bool(getattr(root, "has_error", False)):
        return None
    out: list[tuple[int, str]] = []
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if _is_string_node(str(getattr(node, "type", ""))):
            body = _tree_sitter_string_body(node, source)
            if body is not None:
                out.append((int(node.start_point[0]) + 1, body))
            continue  # outermost string node only -- its children are its own parts
        stack.extend(getattr(node, "children", ()) or ())
    return out


def _blob_path(edit: Mapping[str, Any]) -> str:
    """The file path an edit descriptor targets, stripped of range/symbol suffixes."""
    raw = edit.get("path") or edit.get("file_path") or ""
    if not isinstance(raw, str):
        return ""
    path = raw.split("#", 1)[0]
    while True:
        trimmed = _TARGET_SUFFIX_RE.sub("", path)
        if trimmed == path:
            return trimmed
        path = trimmed


def _blob_language(path: str) -> str | None:
    """Parser language for an edited blob, or ``None`` when nothing can parse it.

    A blob handed over without a path is a fragment from the edit hook, whose
    supervised surface is Python; assuming Python there is safe because assuming
    wrong costs a failed tokenize and a degraded reason, never a made-up literal.
    """
    if not path:
        return "python"
    from lemoncrow.infra.tree_sitter.tags import detect_language
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import supported_tree_sitter_languages

    try:
        language = detect_language(Path(path))
    except Exception:  # unknown extension registry state: report nothing
        return None
    if language == "python":
        return language
    if language is not None and language in supported_tree_sitter_languages():
        return language
    return None


def _scan_literals(text: str, path: str = "") -> _LiteralScan:
    """Extract quoted literals from one blob with a real parser, or report failure.

    There is no regex fallback on purpose. A regex has no notion of comments,
    apostrophes or escapes, so one ``daemon's`` in a docstring desynchronises it
    for the rest of the file and it starts emitting captured *code* fragments
    (``' / session_id / '``) as contract literals. Those fabrications are rare by
    construction, so the rarity gate downstream keeps them -- the most
    credible-looking findings in the report are the invented ones. Emitting
    nothing plus a degraded reason is the only honest failure mode.
    """
    language = _blob_language(path)
    if language is None:
        # Nothing here can parse this file. Only say a signal was lost if the
        # blob has quotes at all -- a file with none had no literals to lose.
        lost = any(quote in text for quote in _QUOTES)
        return _LiteralScan(reason=LITERAL_SCAN_UNPARSED if lost else "")
    found = _python_literal_tokens(text) if language == "python" else _tree_sitter_literal_tokens(text, language)
    if found is None:
        return _LiteralScan(reason=LITERAL_SCAN_UNPARSED)
    literals: set[str] = set()
    lines: dict[int, set[str]] = {}
    for line, body in found:
        if not _accept_literal(body):
            continue
        literals.add(body)
        lines.setdefault(line, set()).add(body)
    return _LiteralScan(
        literals=frozenset(literals),
        by_line={line: frozenset(values) for line, values in lines.items()},
    )


def _literal_replacements(edits: list[dict[str, Any]], *, limit: int) -> tuple[dict[str, str | None], bool]:
    """``literal_replacements`` plus whether any edit's blob went unparsed.

    An edit whose old *or* new side failed to parse is skipped entirely. Half a
    comparison is worse than none: subtracting an unknown set from a known one
    marks every literal on the known side as removed.
    """
    replacements: dict[str, str | None] = {}
    unparsed = False
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        path = _blob_path(edit)
        old_scan = _scan_literals(old, path)
        new_scan = _scan_literals(new, path)
        if old_scan.reason or new_scan.reason:
            unparsed = True
            continue
        removed = old_scan.literals - new_scan.literals
        added = new_scan.literals - old_scan.literals
        for literal in removed:
            replacements.setdefault(literal, None)
        if not removed or not added:
            continue
        new_line_set = set(new.splitlines())
        old_lines = old.splitlines()
        sorted_added = sorted(added)
        for line_no, on_line in sorted(old_scan.by_line.items()):
            present = on_line & removed
            if len(present) != 1 or not 1 <= line_no <= len(old_lines):
                continue
            literal = next(iter(present))
            old_line = old_lines[line_no - 1]
            for quote in _QUOTES:
                token = f"{quote}{literal}{quote}"
                if token not in old_line:
                    continue
                target = next(
                    (r for r in sorted_added if old_line.replace(token, f"{quote}{r}{quote}") in new_line_set),
                    None,
                )
                if target is not None:
                    replacements[literal] = target
                    break
    # Prefer longer literals: more contract-specific, less noisy.
    ordered = sorted(replacements, key=lambda value: (-len(value), value))[:limit]
    return {literal: replacements[literal] for literal in ordered}, unparsed


def literal_replacements(edits: list[dict[str, Any]], *, limit: int = 6) -> dict[str, str | None]:
    """Map each removed quoted literal to its replacement, when a rename is identifiable.

    A literal present in ``old_string`` but not ``new_string`` was removed. A rename is
    only claimed when substituting a removed literal for an added one turns some old
    line into a line that appears verbatim in ``new`` (e.g. ``config['db']`` ->
    ``config['database']``); otherwise the value is ``None`` (removed, no confident
    replacement).

    The test is by substitution, deliberately NOT by positional line pairing: an edit
    that inserts or deletes lines shifts every line below it, so ``zip``-aligning
    old/new lines -- even when the line COUNT is coincidentally equal -- pairs
    unrelated lines and manufactures phantom renames. Requiring the literal to be
    genuinely absent from ``new`` likewise stops a still-present key (unchanged, merely
    shifted next to the churn) from ever being reported as removed or renamed.

    Both sides are read by a parser (``tokenize`` for Python, the tree-sitter grammar
    otherwise), so comments, apostrophes and escapes are the parser's problem rather
    than a scanner's guess. A blob that will not parse contributes nothing; see
    ``_scan_literals`` for why there is deliberately no regex fallback.
    """
    return _literal_replacements(edits, limit=limit)[0]


def removed_literals(edits: list[dict[str, Any]], *, limit: int = 6) -> list[str]:
    """Return quoted literals removed, rather than merely moved, by *edits*."""
    return list(literal_replacements(edits, limit=limit))


def _removed_cache_decorated_symbols(edits: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(decorator, symbol) pairs whose cache decorator this edit removed.

    Flags only a decorator present above ``def NAME`` in *old* and absent above the
    same ``def NAME`` in *new* -- i.e. genuinely stripped, not merely relocated to a
    new helper (the helper keeps the decorator, so its own name is never flagged).
    """
    out: list[tuple[str, str]] = []
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        for match in _CACHE_DECORATED_DEF_RE.finditer(old):
            deco, name = match.group("deco"), match.group("name")
            still = re.search(
                r"@(?:\w+\.)*" + re.escape(deco) + r"\b[^\n]*\n\s*(?:async\s+)?def\s+" + re.escape(name) + r"\b",
                new,
            )
            if not still and (deco, name) not in out:
                out.append((deco, name))
    return out


def decorator_contract_impact(
    edits: list[dict[str, Any]],
    *,
    engine: _TextSearcher | None,
    touched_paths: list[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Sites in untouched files that call a decorator-provided method this edit removed.

    e.g. removing ``@lru_cache`` from ``get_resolver`` breaks ``get_resolver.cache_clear()``
    in another file -- a semantic dependency invisible to literal matching. Deterministic
    (decorator removal is textual; the method names are a fixed stdlib vocabulary) and
    low-noise (fires only when the decorator is removed AND its method is used elsewhere).
    """
    pairs = _removed_cache_decorated_symbols(edits)
    if not pairs or engine is None:
        return []
    touched = set(touched_paths)
    sites: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for deco, name in pairs:
        for attr in _DECORATOR_PROVIDED_ATTRS.get(deco, ()):
            access = f"{name}.{attr}"
            try:
                hits = engine.search_text(access, path=".", limit=20, ignore_case=False)
            except Exception:
                hits = []
            for hit in hits:
                path = getattr(hit, "file_path", None)
                line = getattr(hit, "line", None)
                text = getattr(hit, "text", "") or ""
                if not isinstance(path, str) or path in touched:
                    continue
                if access not in text:  # precise: the actual attribute access, not a name collision
                    continue
                key = (path, int(line) if isinstance(line, int) else -1)
                if key in seen:
                    continue
                seen.add(key)
                sites.append(
                    {
                        "path": f"{path}:L{line}" if isinstance(line, int) else path,
                        "old": f"@{deco} on {name}()",
                        "new": f"{access} no longer exists",
                        # Snippet is raw file content -- mask secrets before it
                        # rides into agent-facing FIXME evidence (same masking
                        # applied to every other live tool output).
                        "snippet": redact_tool_output(text.strip())[:80],
                    }
                )
                if len(sites) >= limit:
                    return sites
    return sites


def _is_test_path(path: str) -> bool:
    name = Path(path).name.lower()
    parts = {part.lower() for part in Path(path).parts}
    return (
        bool(parts & {"test", "tests", "spec", "specs", "__tests__"})
        or name.startswith(("test_", "spec_"))
        or name.endswith(("_test.py", "_spec.rb"))
    )


def _is_structural_occurrence(line: str, literal: str) -> bool:
    """Text-fallback heuristic: True when the quoted literal is used as code, not prose.

    A real contract key sits next to a delimiter (``['db']``, ``.get('db'``,
    ``{'db':``, ``'db':``). The same token in prose sits between words
    (``the 'db' cache backend``) and is dropped as noise. ast-grep does this
    exactly; this approximates it when ast-grep is unavailable.
    """
    for quote in _QUOTES:
        token = f"{quote}{literal}{quote}"
        idx = line.find(token)
        while idx >= 0:
            before = line[idx - 1] if idx > 0 else ""
            after_index = idx + len(token)
            after = line[after_index] if after_index < len(line) else ""
            if before in _DELIMITERS or after in _DELIMITERS:
                return True
            idx = line.find(token, idx + 1)
    return False


def _astgrep_languages(touched_paths: list[str]) -> list[str]:
    languages: list[str] = []
    for path in touched_paths:
        language = _EXT_TO_ASTGREP_LANG.get(Path(path).suffix.lower())
        if language and language not in languages:
            languages.append(language)
    return languages


def _astgrep_patterns(literal: str) -> list[str]:
    # ast-grep string-literal patterns are quote-sensitive: 'x' matches only
    # single-quoted nodes, "x" only double-quoted. Try both and merge.
    return [f"{quote}{literal}{quote}" for quote in ("'", '"') if quote not in literal]


def _literal_queries(literal: str) -> list[str]:
    return [f"{quote}{literal}{quote}" for quote in _QUOTES]


def _line_lookup(repo_root: Path, cache: dict[str, list[str]], path: str, line: int) -> str:
    lines = cache.get(path)
    if lines is None:
        try:
            target = repo_root / path
            lines = (
                []
                if target.stat().st_size > _MAX_FILE_BYTES
                else target.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            lines = []
        cache[path] = lines
    return lines[line - 1].strip() if 1 <= line <= len(lines) else ""


def _astgrep_repo_path(repo_root: Path, raw_path: str) -> str:
    """Normalize ast-grep run/scan output to the detector's repo-relative contract."""

    path = Path(raw_path)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return raw_path


# Ceiling on how many matches one batched ast-grep scan will materialise. Every
# candidate is budgeted separately in the accumulation loop below, so this exists
# only to bound the objects a pathological repo can build -- never to decide which
# candidates are represented at all. When it does bind, the result says so.
_ASTGREP_SCAN_MATCH_CEILING = 20_000


@dataclass(frozen=True)
class _AstGrepDetection:
    """Per-candidate ast-grep sites, plus whether the scan itself was capped.

    ``truncated`` is not cosmetic. It means ast-grep matched more than one pass
    materialises, so a candidate with no rows may be absent because the cap cut
    the output short rather than because the code has no such site. Carrying it
    out is what lets the caller degrade instead of reporting a capped scan as an
    exhaustive one.
    """

    by_literal: dict[str, list[tuple[str, int, str]]]
    truncated: bool = False


def _astgrep_detect(
    literals: list[str],
    repo_root: Path,
    touched: set[str],
    *,
    languages: list[str],
    limit: int,
    pattern_builder: Callable[[str], list[str]] = _astgrep_patterns,
) -> _AstGrepDetection | None:
    """Structural detection via ast-grep. ``None`` means it could not run.

    The normal path batches every candidate/language/pattern into one ast-grep
    ``scan`` invocation. Large reviews used to spawn one process for every
    combination (124 children on LemonCrow's 444-file dogfood range), making
    process startup a double-digit-second part of review capture. If rule mode
    rejects any generated pattern, fall back to the legacy per-pattern calls so
    batching can never reduce detector coverage.
    """
    if not languages:
        return None
    try:
        from lemoncrow.infra.code_intel.astgrep import AstGrepAdapter, AstGrepToolUnavailable
    except Exception:
        return None

    adapter = AstGrepAdapter(repo_root)
    line_cache: dict[str, list[str]] = {}
    out: dict[str, list[tuple[str, int, str]]] = {literal: [] for literal in literals}
    seen_by_literal: dict[str, set[tuple[str, int]]] = {literal: set() for literal in literals}

    rules: list[dict[str, Any]] = []
    literal_by_rule: dict[str, str] = {}
    rule_index = 0
    for literal in literals:
        for language in languages:
            for pattern in pattern_builder(literal):
                rule_id = f"lc-impact-{rule_index}"
                rule_index += 1
                literal_by_rule[rule_id] = literal
                rules.append(
                    {
                        "id": rule_id,
                        "language": language,
                        "severity": "info",
                        "message": literal,
                        "rule": {"pattern": pattern},
                    }
                )
    if not rules:
        return None

    try:
        result = adapter.scan(
            rules=rules,
            no_ignore=False,
            # ``scan`` truncates with one global slice over file-ordered output,
            # so an aggregate allowance is not a per-rule budget: a candidate that
            # matches in hundreds of early files eats the whole slice and a rare
            # candidate whose one file sorts late gets zero rows -- reported to the
            # reviewer as "no remaining sites". Materialise far past what the
            # per-candidate budgets below can spend and let those budgets, not
            # file order, decide what each candidate keeps.
            limit=_ASTGREP_SCAN_MATCH_CEILING,
        )
    except AstGrepToolUnavailable:
        return None
    except Exception:
        return _astgrep_detect_unbatched(
            literals,
            repo_root,
            touched,
            languages=languages,
            limit=limit,
            pattern_builder=pattern_builder,
        )

    for match in result.matches:
        matched_literal = literal_by_rule.get(match.rule_id)
        if matched_literal is None:
            continue
        path = _astgrep_repo_path(repo_root, match.file_path)
        if not path or path in touched:
            continue
        # Each candidate gets its own ``limit``, counted after the ``touched``
        # filter: an in-patch hit is never evidence, so it must not spend the
        # budget that decides whether an out-of-patch consumer is reported.
        if len(out[matched_literal]) >= limit:
            continue
        line = match.line + 1  # ast-grep JSON ranges are 0-based; report 1-based
        key = (path, line)
        if key in seen_by_literal[matched_literal]:
            continue
        seen_by_literal[matched_literal].add(key)
        snippet = _line_lookup(repo_root, line_cache, path, line) or (match.snippet or "").strip()
        out[matched_literal].append((path, line, snippet))
    return _AstGrepDetection(by_literal=out, truncated=result.truncated)


def _astgrep_detect_unbatched(
    literals: list[str],
    repo_root: Path,
    touched: set[str],
    *,
    languages: list[str],
    limit: int,
    pattern_builder: Callable[[str], list[str]],
) -> _AstGrepDetection | None:
    """Compatibility fallback for ast-grep builds/patterns that cannot batch."""

    try:
        from lemoncrow.infra.code_intel.astgrep import AstGrepAdapter, AstGrepToolUnavailable
    except Exception:
        return None
    adapter = AstGrepAdapter(repo_root)
    line_cache: dict[str, list[str]] = {}
    out: dict[str, list[tuple[str, int, str]]] = {literal: [] for literal in literals}
    ran = False
    for literal in literals:
        seen: set[tuple[str, int]] = set()
        for language in languages:
            for pattern in pattern_builder(literal):
                try:
                    result = adapter.search(pattern=pattern, language=language, limit=limit)
                except AstGrepToolUnavailable:
                    return None
                except Exception:
                    continue
                ran = True
                for match in result.matches:
                    path = _astgrep_repo_path(repo_root, match.file_path)
                    if not path or path in touched:
                        continue
                    line = match.line + 1
                    key = (path, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    snippet = _line_lookup(repo_root, line_cache, path, line) or (match.snippet or "").strip()
                    out[literal].append((path, line, snippet))
    # ``search`` already applies ``limit`` per pattern, so this path has no
    # aggregate slice that one candidate could spend on another's behalf.
    return _AstGrepDetection(by_literal=out) if ran else None


def _text_detect(
    literals: list[str],
    engine: _TextSearcher | None,
    touched: set[str],
    *,
    limit: int,
    query_builder: Callable[[str], list[str]] = _literal_queries,
    gate: Callable[[str, str], bool] = _is_structural_occurrence,
) -> dict[str, list[tuple[str, int, str]]]:
    """Language-agnostic fallback: engine text search + a used-as-code heuristic.

    ``query_builder`` yields the raw search strings for a candidate and ``gate`` keeps
    only hits that use it structurally (as code, not prose) -- quoted-literal by
    default; a symbol detector swaps in identifier variants.
    """
    out: dict[str, list[tuple[str, int, str]]] = {literal: [] for literal in literals}
    if engine is None:
        return out
    for literal in literals:
        seen: set[tuple[str, int]] = set()
        for query in query_builder(literal):
            try:
                hits = engine.search_text(query, limit=limit)
            except Exception:
                continue
            for hit in hits:
                path = getattr(hit, "file_path", None)
                line = getattr(hit, "line", None)
                text = getattr(hit, "text", "") or ""
                if not isinstance(path, str) or not isinstance(line, int) or path in touched:
                    continue
                if not gate(text, literal):
                    continue
                key = (path, line)
                if key in seen:
                    continue
                seen.add(key)
                out[literal].append((path, line, text.strip()))
    return out


def _combine_matches(
    astgrep_matches: list[tuple[str, int, str]] | None,
    text_matches: list[tuple[str, int, str]],
    *,
    astgrep_authoritative: bool = True,
) -> list[tuple[str, int, str]]:
    """Best of both: ast-grep is authoritative for code files; text adds only the
    non-code files (config, templates, docs) ast-grep can't parse.

    ``astgrep_authoritative=False`` keeps the code-file text matches too. It exists
    for the attribute-call pattern ``$OBJ.name($$$)``: this module cannot assert that
    every ast-grep grammar spells an attribute call that way, and an ast-grep pass
    that runs but matches nothing would silently erase the text layer's hits -- which
    is exactly the invisibility that pattern was added to fix. The same gate still
    filters the text hits, so the union costs recall's worth of precision, not a
    fabricated site.
    """
    if astgrep_matches is None:
        return list(text_matches)  # ast-grep unavailable -> pure text recall
    seen = {(path, line) for path, line, _ in astgrep_matches}
    combined = list(astgrep_matches)
    for path, line, snippet in text_matches:
        if astgrep_authoritative and Path(path).suffix.lower() in _EXT_TO_ASTGREP_LANG:
            continue  # code file -> ast-grep already covered it precisely
        if (path, line) in seen:
            continue
        seen.add((path, line))
        combined.append((path, line, snippet))
    return combined


def contract_literal_impact(
    edits: list[dict[str, Any]],
    *,
    engine: _TextSearcher | None,
    repo_root: Path,
    touched_paths: list[str],
    max_matches_per_literal: int = 2,
    search_limit: int = 30,
) -> dict[str, Any] | None:
    """Return remaining occurrences of literals removed by *edits*, in untouched files.

    Detection prefers ast-grep (structural, precise); it degrades to *engine* text
    search when ast-grep can't run. Matches inside *touched_paths* are excluded --
    only parallel consumers the agent may have missed are evidence. Returns ``None``
    when no literal was removed and every blob parsed.

    When some blob would not parse, the return carries ``degraded:
    [LITERAL_SCAN_UNPARSED]`` even with an empty ``sites`` list, so a caller can
    say "this pass was partial" instead of implying it looked everywhere.
    """
    replacements, unparsed = _literal_replacements(edits, limit=6)
    partial: list[str] = [LITERAL_SCAN_UNPARSED] if unparsed else []
    if not replacements:
        return {"sites": [], "degraded": partial} if partial else None
    touched = set(touched_paths)
    literals = list(replacements)

    # Recall layer: language-agnostic text search (heuristic-filtered) finds
    # candidates anywhere, including non-code config/templates ast-grep can't parse.
    text_by_literal = _text_detect(literals, engine, touched, limit=search_limit)
    # Precision layer: ast-grep over every code language that actually appears
    # (edited files + text candidates). It is authoritative for code files --
    # matching string *nodes* drops the docstring/comment false positives that the
    # text heuristic only approximates away.
    candidate_paths = [match[0] for matches in text_by_literal.values() for match in matches]
    languages = _astgrep_languages(list(touched) + candidate_paths)
    detection = _astgrep_detect(literals, repo_root, touched, languages=languages, limit=search_limit)
    if detection is not None and detection.truncated:
        # A capped scan may simply never have reached a candidate's files, so an
        # empty result below means "not looked at everywhere", not "not present".
        partial.append(LITERAL_SCAN_TRUNCATED)
    astgrep_by_literal = detection.by_literal if detection is not None else None

    sites: list[dict[str, Any]] = []
    for literal in literals:
        astgrep_found = astgrep_by_literal.get(literal) if astgrep_by_literal is not None else None
        found = _combine_matches(astgrep_found, text_by_literal.get(literal) or [])
        if not found:
            continue
        # Rarity gate: a literal found in many files is ambient vocabulary (common
        # bool synonyms, generic status words), not a contract -- skip it.
        if len({match[0] for match in found}) > _MAX_LITERAL_FILE_SPREAD:
            continue
        # Production consumers before tests, then stable by location.
        found.sort(key=lambda match: (_is_test_path(match[0]), match[0], match[1]))
        for path, line, snippet in found[:max_matches_per_literal]:
            entry: dict[str, Any] = {
                "path": f"{path}:L{line}",
                "old": literal,
                # Snippet is raw file content -- mask secrets before it rides
                # into agent-facing FIXME evidence (same masking applied to
                # every other live tool output).
                "snippet": redact_tool_output(snippet)[:80],
            }
            if replacements[literal] is not None:
                entry["new"] = replacements[literal]
            sites.append(entry)
    if not sites:
        return {"sites": [], "degraded": partial} if partial else None
    out: dict[str, Any] = {
        "reason": ("These sites still use the old form you just changed -- update each or say why not."),
        "sites": sites,
    }
    if partial:
        out["degraded"] = partial
    return out


# Column-0 (module-level) definitions whose removal breaks importers in other files:
# a function, a class, or an UPPER_SNAKE constant. Indented defs/classes (methods,
# nested helpers) are excluded -- their references are attribute-qualified and far
# noisier than a bare module name.
_MODULE_SYMBOL_DEF_RE = re.compile(
    r"^(?:async\s+)?def\s+(?P<fn>\w+)"
    r"|^class\s+(?P<cls>\w+)"
    r"|^(?P<const>[A-Z_][A-Z0-9_]{2,})\s*(?::[^=\n]+)?=(?!=)",
    re.MULTILINE,
)
# Shorter module names (<4 chars) are too common to reference-match without noise;
# the file-spread gate already drops ambient names -- this just skips their lookups.
_MIN_SYMBOL_LEN = 4


def _defined_module_symbols(text: str) -> set[str]:
    out: set[str] = set()
    for match in _MODULE_SYMBOL_DEF_RE.finditer(text):
        name = match.group("fn") or match.group("cls") or match.group("const")
        if name:
            out.add(name)
    return out


def _removed_module_symbol_sources(edits: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(name, defining path)`` for module-level symbols removed by *edits*.

    Keeping the defining path is load-bearing for Python private names: a bare
    search for ``_helper`` can otherwise merge an unrelated private namesake in
    another package and manufacture a removed-symbol finding. Empty paths are
    preserved for hook payloads that genuinely do not carry one; those retain
    the legacy conservative behaviour rather than pretending we know scope.
    """
    removed: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        source_path = _blob_path(edit)
        for name in _defined_module_symbols(old) - _defined_module_symbols(new):
            item = (name, source_path)
            if len(name) >= _MIN_SYMBOL_LEN and item not in seen:
                seen.add(item)
                removed.append(item)
    return removed


def _removed_module_symbols(edits: list[dict[str, Any]]) -> list[str]:
    """Names only, kept for the detector's public/tested helper contract."""
    out: list[str] = []
    seen: set[str] = set()
    for name, _source_path in _removed_module_symbol_sources(edits):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _path_defines_module_symbol(repo_root: Path, path: str, name: str) -> bool:
    """Whether *path* owns a module-level definition of *name* itself.

    A repository-wide bare-name search returns both imports/references and
    unrelated same-named definitions. If a candidate file defines the name, its
    bare uses belong to that local definition; treating them as surviving
    references to a symbol removed somewhere else is a namesake false positive.
    Read failures deliberately return False so this remains a conservative
    enrichment rather than a precondition.
    """

    try:
        root = repo_root.expanduser().resolve()
        target = (root / path).resolve()
        target.relative_to(root)
        if not target.is_file() or target.stat().st_size > _MAX_FILE_BYTES:
            return False
        return name in _defined_module_symbols(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False


def _private_symbol_out_of_scope(name: str, source_path: str, candidate_path: str) -> bool:
    """Whether a Python private-name hit cannot belong to *source_path*.

    One leading underscore is treated package-private (same directory allowed),
    matching LemonCrow's review call-graph gate. Double-leading names are
    file-private/name-mangled unless they are public dunder protocol. Other
    languages and pathless hook payloads are left untouched.
    """
    if not source_path or Path(source_path).suffix.lower() not in {".py", ".pyi"}:
        return False
    if not name.startswith("_") or (name.startswith("__") and name.endswith("__")):
        return False
    if candidate_path == source_path:
        return False
    if name.startswith("__"):
        return True
    return Path(candidate_path).parent != Path(source_path).parent


def _symbol_tokens(name: str) -> list[str]:
    # A bare identifier: the ast-grep pattern and the text query are the name itself.
    return [name]


def _is_identifier_occurrence(line: str, name: str) -> bool:
    """Text-fallback gate: *name* appears as a bare identifier reference -- not a
    substring of a longer name, an attribute of an unrelated object, or in a comment."""
    if line.lstrip().startswith("#"):
        return False
    return re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", line) is not None


def symbol_contract_impact(
    edits: list[dict[str, Any]],
    *,
    engine: _TextSearcher | None,
    repo_root: Path,
    touched_paths: list[str],
    max_matches_per_symbol: int = 2,
    search_limit: int = 30,
) -> DetectorSites:
    """Sites in untouched files still referencing a module-level symbol this edit
    removed or renamed (a def / class / constant).

    The symbol counterpart of ``contract_literal_impact``: a deleted or renamed name
    breaks importers with no surviving definition for the call graph to resolve, so
    detection is structural -- ast-grep matches the identifier as an AST node (not a
    string or comment), with the engine's indexed text search as the language-agnostic
    fallback. Same discipline: exclude touched files, drop ambient names by file
    spread, fail-open. Returns a flat site list (empty when nothing remains elsewhere),
    carrying ``degraded`` when the ast-grep pass behind it was capped -- an empty list
    from a truncated scan means "not looked at everywhere", not "nothing remains".
    """
    symbol_sources = _removed_module_symbol_sources(edits)
    names = list(dict.fromkeys(name for name, _source_path in symbol_sources))
    if not names:
        return DetectorSites()
    sources_by_name: dict[str, tuple[str, ...]] = {}
    for name, source_path in symbol_sources:
        sources_by_name[name] = (*sources_by_name.get(name, ()), source_path)
    touched = set(touched_paths)
    text_by_name = _text_detect(
        names,
        engine,
        touched,
        limit=search_limit,
        query_builder=_symbol_tokens,
        gate=_is_identifier_occurrence,
    )
    candidate_paths = [match[0] for matches in text_by_name.values() for match in matches]
    languages = _astgrep_languages(list(touched) + candidate_paths)
    detection = _astgrep_detect(
        names, repo_root, touched, languages=languages, limit=search_limit, pattern_builder=_symbol_tokens
    )
    # A capped scan may never have reached a name's files, so a name that ends up
    # with no sites below is unproven, not clean. Say so rather than let the
    # caller read silence as an exhaustive "nothing remains".
    partial = [SYMBOL_SCAN_TRUNCATED] if detection is not None and detection.truncated else []
    astgrep_by_name = detection.by_literal if detection is not None else None
    sites: list[dict[str, Any]] = []
    for name in names:
        astgrep_found = astgrep_by_name.get(name) if astgrep_by_name is not None else None
        found = _combine_matches(astgrep_found, text_by_name.get(name) or [])
        source_paths = sources_by_name.get(name, ())
        # A file that owns its own definition of this spelling is a namesake,
        # not an importer of the removed definition. Drop the whole file before
        # scope checks so its local uses cannot masquerade as references.
        namesake_paths = {path for path, _line, _snippet in found if _path_defines_module_symbol(repo_root, path, name)}
        if namesake_paths:
            found = [match for match in found if match[0] not in namesake_paths]
        if source_paths:
            found = [
                match
                for match in found
                if any(not _private_symbol_out_of_scope(name, source_path, match[0]) for source_path in source_paths)
            ]
        if not found:
            continue
        # Rarity gate: a name referenced across many files is ambient, not a contract.
        if len({match[0] for match in found}) > _MAX_LITERAL_FILE_SPREAD:
            continue
        found.sort(key=lambda match: (_is_test_path(match[0]), match[0], match[1]))
        for path, line, snippet in found[:max_matches_per_symbol]:
            sites.append(
                {
                    "path": f"{path}:L{line}",
                    "old": name,
                    "new": f"{name} no longer defined here",
                    "snippet": redact_tool_output(snippet)[:80],
                }
            )
    return DetectorSites(sites, degraded=partial)


def _split_top_level(param_str: str) -> list[str]:
    """Split a parameter list on top-level commas (commas inside (), [], {} stay put)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in param_str:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _has_top_level_default(param: str) -> bool:
    # A default is a top-level '=' -- not one nested in an annotation like
    # ``Callable[[int], int]`` (no '=') or a call default ``field(default=1)``.
    depth = 0
    for char in param:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "=" and depth == 0:
            return True
    return False


def _required_param_names(param_str: str) -> set[str]:
    """Names of parameters a caller MUST pass: positional-or-keyword / keyword-only
    with no default. ``self``/``cls``, ``*args``/``**kwargs`` and the ``*``/``/``
    markers are excluded -- they never impose a new required argument on callers."""
    required: set[str] = set()
    for raw in _split_top_level(param_str):
        param = raw.strip()
        if not param or param.startswith("*") or param == "/":
            continue
        name = re.split(r"[:=]", param, maxsplit=1)[0].strip()
        if name in ("self", "cls") or not name.isidentifier():
            continue
        if not _has_top_level_default(param):
            required.add(name)
    return required


_DEF_SIGNATURE_RE = re.compile(r"(?:^|\n)(?P<indent>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<name>\w+)[ \t]*\(")
# Indentation is the only class signal a raw edit hunk carries, and it is enough:
# Python's own scoping rule says the nearest preceding ``class`` indented *less*
# than a ``def`` owns it.
_CLASS_HEADER_RE = re.compile(r"(?:^|\n)(?P<indent>[ \t]*)class[ \t]+(?P<name>\w+)")


def _owner_class(headers: list[tuple[int, int, str]], indent: int, offset: int) -> str:
    """The class enclosing a def at *offset* with *indent*, or ``""`` for module level.

    *headers* is ``(offset, indent, name)`` for every ``class`` in the same text, in
    source order. The last header that both precedes the def and is indented less
    than it is the innermost enclosing class; a class at the same indentation is a
    sibling, not a parent, which is what keeps a module-level function that happens
    to follow a class body unqualified.
    """
    owner = ""
    for start, header_indent, name in headers:
        if start >= offset:
            break
        if header_indent < indent:
            owner = name
    return owner


def _def_signatures(text: str) -> dict[str, str]:
    """Map each def to its raw parameter string, balanced across newlines.

    The key is ``Class.name`` for a method and the bare ``name`` for a module-level
    function. That qualifier is load-bearing rather than cosmetic: a method is only
    ever reached as ``obj.name(...)``, so a detector holding nothing but the bare
    name cannot build a call-site query for it and cannot say which class a call it
    does find belongs to. Python code is method-heavy, so a bare-name-only map made
    the common case of this detector structurally undetectable.

    A def whose parameter list is not closed within *text* (a hunk that cuts the
    signature mid-way) is skipped -- never guessed at."""
    headers = [
        (match.start(), len(match.group("indent")), match.group("name")) for match in _CLASS_HEADER_RE.finditer(text)
    ]
    out: dict[str, str] = {}
    for match in _DEF_SIGNATURE_RE.finditer(text):
        idx = match.end()  # just past the '('
        depth = 1
        while idx < len(text) and depth > 0:
            char = text[idx]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            idx += 1
        if depth != 0:
            continue
        owner = _owner_class(headers, len(match.group("indent")), match.start())
        name = match.group("name")
        out[f"{owner}.{name}" if owner else name] = text[match.end() : idx - 1]
    return out


def _split_owner(key: str) -> tuple[str, str]:
    """Split a ``_def_signatures`` key into ``(class_name_or_empty, bare_name)``."""
    owner, _, name = key.rpartition(".")
    return owner, name


def _signature_change_params(edits: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each def present in BOTH old and new to the parameters that BECAME required
    -- newly added without a default, or an existing param that lost its default.
    Either breaks callers that don't pass it. Renamed/removed defs are the job of
    ``symbol_contract_impact``; a benign change (a new *optional* param) yields nothing.

    Keys are ``_def_signatures`` keys, so a method is reported as ``Class.name``. Old
    and new are matched on that same qualified key: moving ``refresh`` from one class
    to another is a removal plus an addition, not a signature change, and matching on
    the bare name would have called it a signature change on whichever class won."""
    out: dict[str, list[str]] = {}
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        old_sigs = _def_signatures(old)
        for key, new_params in _def_signatures(new).items():
            if key not in old_sigs or len(_split_owner(key)[1]) < _MIN_SYMBOL_LEN:
                continue
            newly_required = _required_param_names(new_params) - _required_param_names(old_sigs[key])
            if newly_required:
                out.setdefault(key, []).extend(sorted(newly_required))
    return out


def _call_patterns(key: str) -> list[str]:
    """ast-grep patterns for a call to *key*.

    A module-level function is called by its bare identifier. A method is not: it is
    reached through a receiver, so the pattern has to be the attribute call. Matching
    a method with the bare-identifier pattern is what made ``SessionManager.refresh``
    invisible while module-level ``refresh_session`` was found.
    """
    owner, name = _split_owner(key)
    return [f"$OBJ.{name}($$$)"] if owner else [f"{name}($$$)"]


def _call_queries(key: str) -> list[str]:
    """Text-search queries for a call to *key* -- ``.name(`` for a method, the bare name
    for a module-level function. The literal ``.name(`` is both precise and cheap, so
    the search budget is not spent on every unrelated mention of the bare name."""
    owner, name = _split_owner(key)
    return [f".{name}("] if owner else [name]


def _is_call_occurrence(line: str, key: str) -> bool:
    """Text-fallback gate: *key* is invoked as a call on this line -- not a substring
    and not a comment.

    A module-level name must be a *bare* call (``name(``), never an attribute of some
    other object. A method is the mirror image: it must be an *attribute* call
    (``obj.name(``), because that is the only way a receiver reaches it.
    """
    if line.lstrip().startswith("#"):
        return False
    owner, name = _split_owner(key)
    if owner:
        return re.search(rf"\.[ \t]*{re.escape(name)}[ \t]*\(", line) is not None
    return re.search(rf"(?<![\w.]){re.escape(name)}[ \t]*\(", line) is not None


# How many definition sites the ownership census may look at. It only ever answers
# "one, or more than one", so a small bound is enough and keeps the extra query cheap.
_DEFINITION_CENSUS_LIMIT = 12


def _definition_spread(name: str, engine: _TextSearcher | None, *, limit: int) -> int | None:
    """How many places in the repo write ``def name(``; ``None`` when unknowable.

    A literal text search, deliberately: it is the one ownership census that needs no
    index, no ast-grep and no type inference, so it answers on a fresh clone. It is
    only ever used to *lower* a claim -- an over-count downgrades a true finding to
    uncertain, which is the safe direction -- and ``None`` (engine absent, or the
    search failed) is treated the same way rather than as "unique".

    Zero hits means the search could not even see the definition this edit just made,
    so the census is broken rather than empty; that also answers ``None``.
    """
    if engine is None:
        return None
    try:
        hits = engine.search_text(f"def {name}(", path=".", limit=limit, ignore_case=False)
    except Exception:
        # Evidence-only, and a census that did not run must never read as "unique".
        return None
    seen: set[tuple[str, int]] = set()
    for hit in hits:
        path = getattr(hit, "file_path", None)
        line = getattr(hit, "line", None)
        if isinstance(path, str) and path:
            seen.add((path, line if isinstance(line, int) else -1))
    return len(seen) or None


def _method_uncertainty(owner: str, name: str, spread: int | None) -> str:
    """Why an attribute call site may not belong to *owner*; ``""`` when it must.

    ``obj.refresh(...)`` names a method, not a class. Nothing in a text or ast-grep
    match says what ``obj`` is, so the only honest way to attribute the call is to
    show that no other definition of that name exists to attribute it to. When one
    does -- or when the census could not run -- the finding still prints, carrying
    this sentence, because a reviewer who is told "this may be another class" can
    check in seconds while a silently dropped finding is a defect nobody sees.
    """
    if spread is None:
        return (
            f"unverified: could not count the definitions of {name}() in this repo, "
            f"so these call sites are not confirmed to be {owner}.{name}"
        )
    if spread > 1:
        # "at least", not a bare count: the census is a bounded search, so it can
        # under-report and never over-report. A lower bound is true either way, and
        # a finding whose whole point is refusing to over-claim must not open with
        # an exact number it cannot stand behind.
        return (
            f"unverified: at least {spread} definitions named {name}() in this repo -- "
            f"a call site here may belong to another class, not {owner}.{name}"
        )
    return ""


def signature_change_impact(
    edits: list[dict[str, Any]],
    *,
    engine: _TextSearcher | None,
    repo_root: Path,
    touched_paths: list[str],
    max_matches_per_symbol: int = 2,
    search_limit: int = 30,
) -> DetectorSites:
    """Call sites in untouched files of a def whose signature gained a required
    parameter -- callers that don't pass it now break.

    Detection is textual and structural, never graph-based: ast-grep matches the call
    expression and the engine's text search backs it up. That is a deliberate choice
    (it works on a stale or absent index) and it is also the honest description --
    an earlier version of this docstring claimed the call graph could see this change
    because the symbol survives the edit, which was never what the code did.

    Two shapes, because Python has two: a module-level ``name(...)`` call, and a
    method reached only as ``obj.name(...)``. The second is the common case and used
    to be structurally invisible -- both the ast-grep pattern and the text gate
    required a bare identifier, so ``SessionManager.refresh`` gaining a parameter
    produced nothing at all while module-level ``refresh_session`` produced the right
    finding. Methods now carry their class (``Class.name``) and are matched as
    attribute calls.

    A method finding is qualified, not asserted: a receiver's type is unknowable here,
    so when any other definition of the bare name exists -- or the census that would
    prove otherwise could not run -- the site ships with an ``uncertainty`` sentence
    instead of being either dropped or printed as fact.

    Only *required* additions fire (a new optional param is non-breaking), so the
    common signature tweak stays silent. Fail-open; touched files excluded. The
    return carries ``degraded`` when the ast-grep pass behind it was capped, so a
    caller can tell "no surviving call sites" from "the scan stopped early"."""
    changed = _signature_change_params(edits)
    if not changed:
        return DetectorSites()
    keys = list(changed)
    touched = set(touched_paths)
    text_by_key = _text_detect(
        keys, engine, touched, limit=search_limit, query_builder=_call_queries, gate=_is_call_occurrence
    )
    candidate_paths = [match[0] for matches in text_by_key.values() for match in matches]
    languages = _astgrep_languages(list(touched) + candidate_paths)
    detection = _astgrep_detect(
        keys, repo_root, touched, languages=languages, limit=search_limit, pattern_builder=_call_patterns
    )
    # Same disclosure as the symbol pass: above the materialisation ceiling a key
    # can lose every row to file order alone, and an unreported cap would present
    # that as "no caller is broken".
    partial = [SIGNATURE_SCAN_TRUNCATED] if detection is not None and detection.truncated else []
    astgrep_by_key = detection.by_literal if detection is not None else None
    sites: list[dict[str, Any]] = []
    for key in keys:
        owner, name = _split_owner(key)
        astgrep_found = astgrep_by_key.get(key) if astgrep_by_key is not None else None
        found = _combine_matches(astgrep_found, text_by_key.get(key) or [], astgrep_authoritative=not owner)
        if not found:
            continue
        # Rarity gate: a name called across many files is ambient, not a contract.
        if len({match[0] for match in found}) > _MAX_LITERAL_FILE_SPREAD:
            continue
        # One census per changed method, after the gates -- a finding that will not
        # be reported never pays for the query.
        uncertainty = (
            _method_uncertainty(owner, name, _definition_spread(name, engine, limit=_DEFINITION_CENSUS_LIMIT))
            if owner
            else ""
        )
        found.sort(key=lambda match: (_is_test_path(match[0]), match[0], match[1]))
        required = ", ".join(changed[key])
        for path, line, snippet in found[:max_matches_per_symbol]:
            site: dict[str, Any] = {
                "path": f"{path}:L{line}",
                "old": f"{key}(...)",
                "new": f"now requires: {required}",
                "snippet": redact_tool_output(snippet)[:80],
            }
            if uncertainty:
                site["uncertainty"] = uncertainty
            sites.append(site)
    return DetectorSites(sites, degraded=partial)


__all__ = [
    "DetectorSites",
    "contract_literal_impact",
    "decorator_contract_impact",
    "literal_replacements",
    "removed_literals",
    "signature_change_impact",
    "symbol_contract_impact",
]
