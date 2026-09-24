"""Semantic change impact for `lc review`: what the diff reaches beyond the patch.

Why this module exists: a diff answers "what changed"; a reviewer needs "what
else now has to change". Two substrates already hold parts of that answer and
neither is usable raw. ``tool_supervision.edit_impact``'s four detectors find
consumers that have no call-graph edge at all -- contract literals, removed
module symbols, newly required parameters, stripped cache decorators -- but they
are best-effort, Python-shaped, and depend on an ast-grep binary that may not
exist. ``CodeContextEngine`` holds the persisted call graph that answers fan-out
and centrality, but its index may be absent on a fresh clone, drifted against
HEAD, or expensive to touch. This adapter is the one seam that turns both into
the packet's ``ChangedSymbol[]`` / ``ImpactSite[]``, so no caller ever learns
which signals happened to be available.

Two rules keep it honest. Every failure returns a value plus a name in
``degraded`` and never raises -- a packet must still build on a machine with no
index, no ast-grep and no network. And index availability is a three-state fact
(``fresh`` / ``stale`` / ``absent``) rather than a boolean, because "the index
answered but its line ranges have drifted" is a different warning from "there is
no index", and collapsing the two would let a stale answer read as authoritative.

Deliberately not consulted: ``semantic_file_memory`` / ``GraphAnalytics`` (one
global 2000-entry LRU shared across every project, whose ``change_impact``
returns empty lists for real files) and ``cross_lang_edges`` (query API complete,
table empty, no writer). Either would return nothing while leaving the packet
looking complete. Every graph signal here comes from ``CodeContextEngine``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from lemoncrow.infra.tree_sitter.tags import Tag, extract_tags_from_text
from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
    contract_literal_impact,
    decorator_contract_impact,
    signature_change_impact,
    symbol_contract_impact,
)

from .gitdiff import MAX_BLOB_BYTES, _anchor_line
from .models import ChangedFile, ChangedSymbol, DiffHunk, ImpactKind, ImpactSite, IndexStatus, SymbolChange

# One outline query per changed file replaces a per-hunk point lookup; 500 is
# well above any single file's symbol count and keeps the query bounded.
_OUTLINE_LIMIT = 500
# Centrality is one whole-repo ranking (~1s cold, cached afterwards). 1000 is
# deep enough that a changed symbol of any real importance is in it.
_CENTRALITY_LIMIT = 1000
# Caller expansion is one graph traversal per symbol, so it is capped twice:
# how many symbols get expanded at all, and how many callers each may return.
_MAX_CALLER_QUERIES = 20
_CALLERS_PER_SYMBOL = 20

# Degradation names. The first two are what `render.py::_INDEX_SIGNALS` looks
# for when deciding to print the ``run `lc code index``` hint, so the spelling
# is load-bearing.
_SIGNAL_RELATIONS = "symbol_relations"
_SIGNAL_CENTRALITY = "centrality"
_SIGNAL_LINE_RANGES = "symbol_line_ranges"
_SIGNAL_ASTGREP = "astgrep_unavailable"
# Raised when at least one symbol had a non-zero index count that could not be
# proved to belong to the definition this diff touched, and was therefore
# dropped. The reviewer is told the packet is under-claiming here, which is the
# whole point: the alternative is a number borrowed from a namesake.
_SIGNAL_AMBIGUOUS = "ambiguous_symbol_counts"
# Raised when a line number the index supplied for a site *outside* the patch
# did not describe the reviewed revision: a caller re-anchored to that
# revision's own blob, or a detector site whose file the workspace has since
# renumbered and which is therefore cited without a line at all.
# `_outline_drifted` only ever looked at the files the diff touched, so
# out-of-patch geometry -- where most of ATTENTION's clickable surface lives --
# sat under an unqualified `index: fresh`. Distinct from `symbol_line_ranges`:
# that one is about the changed file, this one about everything it reaches.
_SIGNAL_CALLER_LINES = "caller_line_ranges"
_SIGNAL_RESOLUTION_TRUNCATED = "symbol_resolution_truncated"
_SIGNAL_CALLER_QUERIES_TRUNCATED = "caller_queries_truncated"
# Raised when the index's outline had no row for a definition the reviewed blob
# plainly contains. Such definitions used to be dropped in silence, which cost a
# whole file's symbols on a commit whose outline the index had only half-walked.
_SIGNAL_OUTLINE_INCOMPLETE = "index_outline_incomplete"
# Raised when a changed file carried tags the extractor calls "definition" that
# do not define anything a reviewer can attest to -- an imported binding, a data
# key, a call expression, a string literal. They are dropped rather than
# rendered, and this says the file's symbol list is therefore shorter than the
# extractor's tag list. See :data:`_DEFINITION_NODE_KINDS`.
_SIGNAL_NON_DEFINITIONS = "non_definition_symbols"
# Raised when an impact site named a path that belongs to a *different*
# repository -- a submodule, or a nested clone. The site is dropped.
_SIGNAL_FOREIGN_SITES = "cross_repository_sites"
# Raised when a detector site named a file, or a use of a symbol, that the
# revision under review did not contain. The detectors read the workspace on
# disk; reviewing a commit a few revisions back therefore cited a file added
# since. The site is dropped, and the reviewer is told the pass under-claims.
_SIGNAL_ABSENT_SITES = "sites_absent_from_revision"
# Raised when `limit_sites` actually cut the site list. The cap used to be a
# silent head slice reported as if it were the total, so a diff with 54 sites
# and a limit of 40 was indistinguishable from one that had 40.
_SIGNAL_SITE_CAP = "impact_sites_truncated"

# How many of the `limit_sites` slots are held for the untouched-caller pass.
# Sites are ordered by kind and `untouched_caller` sorts last, so the cap used
# to delete that class whole: a refactor removing 25 constants filled the
# budget with `removed_symbol` sites, the caller pass was handed a budget of
# `limit_sites - len(detector_sites)` == 0, and four real untouched callers
# never reached the packet -- indistinguishable from a caller pass that found
# nothing. Small enough that a caller-poor diff loses no detector findings.
_CALLER_SITE_RESERVE = 8

# Categories that carry no reviewable definitions. The index happily models a
# markdown heading as a symbol, so a docs-only commit would otherwise return a
# hundred "changed symbols" with no callers and no impact -- burying the handful
# of code definitions the reviewer is actually looking for. The detectors still
# run over these files; only the symbol list skips them.
_NON_CODE_CATEGORIES = frozenset({"docs", "generated", "vendor"})

# A symbol name the index resolves to more than one definition. `tool_callers`
# answers such a query by *merging* the caller sets of every same-named
# definition in the repo (it says so in `ambiguity.merged_target_count`), so the
# result cannot be attributed to the definition this diff touched.
_MAX_MERGED_TARGETS = 1

# Python's leading-underscore convention is the one cross-file scope the index
# does not model, and the gap is not theoretical: `_chip` is defined four times
# in this repository, and `tool_callers` answers a query for it with a single
# target, no `ambiguity` block at all, and the union of all four definitions'
# callers. The merge is invisible in the payload, so it has to be detected from
# where the reported callers live instead.
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

# How many bare names the qualification pass may resolve. Every count the index
# returns is keyed on the bare name, so each one needs its own resolution query
# (~20 ms against this repo's 116k-symbol index) before it may be attributed.
# The budget is what stops a 500-file refactor from spending a minute on graph
# lookups: candidates are resolved strongest-signal-first, and a name the budget
# never reached reports no signal rather than an unattributed one.
_MAX_RESOLVE_QUERIES = 200

# How far above a definition its own decorators may reach before the scan gives
# up. Deep enough for any real stack of decorators, shallow enough that a file
# of pathological shape cannot make window computation quadratic.
_MAX_PRELUDE_LINES = 60

# How far below a definition the indentation scan may look for the end of its
# body. Only reached where no parse is available; longer than any hand-written
# function, and short enough that a generated one-body file stays linear.
_MAX_BODY_LINES = 2000

# A line that only opens or closes a block does not end the body above it. Brace
# languages put both at the *header's* own indentation, which the indentation
# rule alone would read as "the body is empty".
_BLOCK_DELIMITERS = ("{", "}", ")", "]", "(", "[")

# Line-comment markers across the languages this pass parses definitions for.
# Only ever used to decide that a line is *not* part of the body above it.
_COMMENT_PREFIXES = ("#", "//")

_IDENTIFIER_RE = re.compile(r"\w+")

# Keywords that introduce a definition across the languages the detectors run
# over. Used only to break an attribution tie -- never to *find* definitions --
# so a language whose keyword is missing degrades to the previous answer rather
# than to a wrong one.
_DEFINITION_KEYWORDS = (
    "def",
    "class",
    "func",
    "fn",
    "function",
    "interface",
    "struct",
    "trait",
    "enum",
    "type",
)
_DEFINITION_MODIFIERS = ("export", "default", "public", "private", "protected", "static", "async", "pub", "final")

# Tree-sitter node kinds that genuinely *define* a symbol, across every language
# the tag extractor covers. An allowlist rather than a denylist on purpose: this
# gate decides what may earn a caller count, a centrality rank, an
# ``untouched_caller`` site and a markable review unit, and the safe direction
# for an unclassified kind is out. ``tests/gateway/test_review_impact.py`` pins
# the partition against ``definition_node_kinds()`` for every supported language,
# so adding a language to the outliner fails a test rather than quietly minting
# review units for its data keys.
_DEFINITION_NODE_KINDS = frozenset(
    {
        # C / C++
        "alias_declaration",
        "class_specifier",
        "declaration",
        "enum_specifier",
        "field_declaration",
        "function_definition",
        "namespace_alias_definition",
        "namespace_definition",
        "preproc_def",
        "preproc_function_def",
        "struct_specifier",
        "template_declaration",
        "type_definition",
        "union_specifier",
        # C# / Java / Kotlin / Swift / Scala / PHP
        "annotation_type_declaration",
        "class_declaration",
        "class_definition",
        "companion_object",
        "const_declaration",
        "constructor_declaration",
        "delegate_declaration",
        "enum_declaration",
        "event_declaration",
        "extension_declaration",
        "file_scoped_namespace_declaration",
        "indexer_declaration",
        "init_declaration",
        "interface_declaration",
        "method_declaration",
        "namespace_declaration",
        "object_declaration",
        "object_definition",
        "primary_constructor",
        "property_declaration",
        "protocol_declaration",
        "protocol_property_declaration",
        "record_declaration",
        "secondary_constructor",
        "struct_declaration",
        "trait_declaration",
        "trait_definition",
        "type_alias",
        "typealias_declaration",
        "val_definition",
        "var_definition",
        # Go (`function_declaration` and `method_declaration` are shared above)
        "type_declaration",
        "var_declaration",
        # JavaScript / TypeScript
        "field_definition",
        "function_declaration",
        "function_signature",
        "generator_function_declaration",
        "lexical_declaration",
        "method_definition",
        "method_signature",
        "property_signature",
        "public_field_definition",
        "type_alias_declaration",
        "variable_declaration",
        # Rust
        "const_item",
        "enum_item",
        "foreign_mod_item",
        "function_item",
        "function_signature_item",
        "impl_item",
        "macro_definition",
        "mod_item",
        "static_item",
        "struct_item",
        "trait_item",
        "type_item",
        # Ruby -- `call` is deliberately absent; it is a call expression.
        "assignment",
        "class",
        "method",
        "module",
        "singleton_method",
        # Shell / Make
        "declaration_command",
        "rule",
        "variable_assignment",
        # SQL -- `alter_table` is deliberately absent; it names an existing table.
        "create_function",
        "create_index",
        "create_table",
        "create_view",
    }
)

# Node kinds the tag extractor labels "definition" that introduce a *name*
# without defining a symbol. Kept as an explicit set (rather than "everything not
# in the allowlist") so the partition test can tell "classified as not-a-symbol"
# apart from "nobody has looked at this kind yet".
_NON_DEFINITION_NODE_KINDS = frozenset(
    {
        # An imported binding is a use of someone else's definition.
        "import_statement",
        # Data keys: JSON / TOML / YAML.
        "block_mapping_pair",
        "pair",
        "table",
        "table_array_element",
        # Prose.
        "atx_heading",
        "fenced_code_block",
        "setext_heading",
        # Markup.
        "doctype",
        "self_closing_tag",
        "start_tag",
        # Stylesheets: selectors and at-rules, not symbols.
        "charset_statement",
        "keyframes_statement",
        "media_statement",
        "namespace_statement",
        "rule_set",
        "supports_statement",
        # A call expression (Ruby `attr_accessor :x`).
        "call",
        # C# top-level statements and assembly attributes.
        "global_attribute",
        "global_statement",
        # DDL against an existing object.
        "alter_table",
    }
)

# What a symbol name may look like. Segments are identifier-shaped (a leading
# ``-`` would be a make-target artefact, a leading digit a literal) and may be
# joined by the qualification separators the extractors emit. Anything else --
# a quote, a space, an ellipsis -- is a literal or a fragment of one, never a
# name a reviewer can be asked to attest to.
_SYMBOL_NAME_RE = re.compile(r"(?:[^\W\d]|\$)[\w$-]*(?:(?:::|[.#])(?:[^\W\d]|\$)[\w$-]*)*")


def _definition_pattern(token: str) -> re.Pattern[str] | None:
    """A line-anchored pattern matching *token*'s own definition, or None.

    None for anything that is not a bare identifier -- a contract literal has no
    definition site, so there is nothing to prefer.
    """

    if not _IDENTIFIER_RE.fullmatch(token):
        return None
    keywords = "|".join(_DEFINITION_KEYWORDS)
    modifiers = "|".join(_DEFINITION_MODIFIERS)
    return re.compile(
        rf"^[ \t]*(?:(?:{modifiers})[ \t]+)*(?:{keywords})[ \t]+{re.escape(token)}\b",
        re.MULTILINE,
    )


@dataclass(frozen=True)
class ImpactResult:
    """Everything the impact pass learned, including what it could not learn."""

    symbols: tuple[ChangedSymbol, ...] = ()
    sites: tuple[ImpactSite, ...] = ()
    index_status: IndexStatus = "absent"
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImpactReuse:
    """Deterministic detector evidence safe to carry into one later revision.

    Graph-derived facts are deliberately absent. Another changed file can alter
    caller counts, centrality and untouched callers even when this source edit's
    bytes did not move, so those facts are recomputed every time.
    """

    detector_sites: tuple[ImpactSite, ...] = ()
    reusable_source_paths: frozenset[str] = frozenset()
    previous_touched_paths: frozenset[str] = frozenset()
    carried_degraded: tuple[str, ...] = ()


# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _impact_site_file(path: str) -> str:
    """Return the repo path from ``path:L12`` without mangling real colons."""

    head, separator, tail = path.rpartition(":L")
    return head if separator and head and tail.isdigit() else path


def _kind_from_line(lines: Sequence[str], line: int) -> str | None:
    """Classify a definition from its own source line.

    The Python tag extractor leaves ``Tag.node_kind`` unset, so without this a
    tree-sitter-sourced symbol would render with no kind at all. Indentation is
    what separates a method from a module-level function, which is exactly the
    distinction a reviewer scanning the symbol list is using.
    """

    if not 1 <= line <= len(lines):
        return None
    raw = lines[line - 1]
    stripped = raw.strip()
    if stripped.startswith("class "):
        return "class"
    if stripped.startswith(("def ", "async def ")):
        return "method" if raw[:1].isspace() else "function"
    return None


def _is_definition_tag(tag: Tag) -> bool:
    """True when *tag* names a definition, not a use of one.

    ``Tag.kind == "definition"`` answers a broader question than this one: it
    means "this node introduces a name", and a JS ``import_statement`` introduces
    the imported binding, a JSON ``pair`` introduces a key, a Ruby ``call``
    introduces whatever ``attr_accessor`` was handed, and a CSS ``rule_set``
    introduces a selector. All four then key the same bare-name caller and
    centrality lookups a real definition does, and the index answers them --
    which is how ``afterEach``, *imported from vitest on line 9* of a brand-new
    test file, came to rank second in this repository's own ATTENTION pane on the
    strength of twelve "known callers" that were other files' imports of the same
    vitest name, and six "untouched caller" sites in two other repositories.

    Two independent gates, because neither alone is enough:

    * **Node kind** must be in :data:`_DEFINITION_NODE_KINDS`. That set is an
      allowlist, not a denylist, so a language newly added to the outliner
      under-claims until someone classifies its kinds -- a missing symbol costs a
      reviewer far less than a fabricated one. ``node_kind`` is ``None`` on the
      Python AST and regex paths, which emit only real declarations; those pass.
    * **Name shape** must be an identifier. A ``.tsx`` file is parsed with the
      TypeScript grammar, which has no JSX, so the ternary inside a ``className``
      is reinterpreted as type syntax and the *string literal* becomes a
      ``property_signature`` named ``"min-h-full font-mono text-neutral-200"``.
      The kind is legitimate; the name is not a name. Nothing a reviewer can be
      asked to attest to is spelled with a quote or a space in it.
    """

    if tag.node_kind is not None and tag.node_kind not in _DEFINITION_NODE_KINDS:
        return False
    return _SYMBOL_NAME_RE.fullmatch(tag.name) is not None


@dataclass(frozen=True)
class _DefinitionAnalysis:
    definitions: tuple[Tag, ...] = ()
    dropped: int = 0
    body_spans: tuple[tuple[int, int], ...] = ()


def _dedupe_definition_tags(tags: Sequence[Tag]) -> tuple[tuple[Tag, ...], int]:
    seen: set[tuple[int, str]] = set()
    out: list[Tag] = []
    dropped = 0
    for tag in sorted((item for item in tags if item.kind == "definition"), key=lambda item: (item.line, item.name)):
        key = (tag.line, tag.name)
        if key in seen:
            continue
        seen.add(key)
        if not _is_definition_tag(tag):
            dropped += 1
            continue
        out.append(tag)
    return tuple(out), dropped


def _python_definition_analysis(text: str, path: str) -> _DefinitionAnalysis:
    """Review-only Python definitions and body spans from one AST parse.

    ``infra.tree_sitter.tags._python_tags`` intentionally also emits every
    ``ast.Name`` reference because the code index needs them. Review impact
    immediately threw those references away and then parsed the same file again
    solely for ``end_lineno``. This narrower projection preserves that extractor's
    definition semantics while avoiding both the reference walk and the second
    parse.
    """

    if not text:
        return _DefinitionAnalysis()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return _DefinitionAnalysis()

    offsets = [0]
    total = 0
    for raw in text.splitlines(keepends=True):
        total += len(raw.encode("utf-8"))
        offsets.append(total)

    def tag(name: str, line: int) -> Tag:
        end_index = min(max(line, 0), len(offsets) - 1)
        start_index = min(max(line - 1, 0), len(offsets) - 1)
        return Tag(name, "definition", str(Path(path)), line, (offsets[start_index], offsets[end_index]))

    definitions: list[Tag] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    definitions.append(tag(target.id, int(target.lineno)))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            definitions.append(tag(stmt.target.id, int(stmt.target.lineno)))

    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            line = int(getattr(node, "lineno", 1))
            definitions.append(tag(node.name, line))
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign | ast.AnnAssign):
            start = _as_int(getattr(node, "lineno", 0), 0)
            end = _as_int(getattr(node, "end_lineno", 0), 0)
            if start > 0 and end >= start:
                spans.append((start, end))

    kept, dropped = _dedupe_definition_tags(definitions)
    return _DefinitionAnalysis(definitions=kept, dropped=dropped, body_spans=tuple(spans))


def _definition_analysis(text: str, path: str) -> _DefinitionAnalysis:
    if not text:
        return _DefinitionAnalysis()
    if Path(path).suffix.lower() in _PYTHON_SUFFIXES:
        return _python_definition_analysis(text, path)
    try:
        tags = extract_tags_from_text(text, path)
    except Exception:
        # Unparseable source is a normal mid-refactor state, not a packet failure.
        return _DefinitionAnalysis()
    kept, dropped = _dedupe_definition_tags(tags)
    return _DefinitionAnalysis(definitions=kept, dropped=dropped)


def _definition_tags_with_drops(text: str, path: str) -> tuple[tuple[Tag, ...], int]:
    """The file's definition tags, plus how many non-definitions were dropped."""

    analysis = _definition_analysis(text, path)
    return analysis.definitions, analysis.dropped


def _definition_tags(text: str, path: str) -> tuple[Tag, ...]:
    """Return the file's definition tags, deduplicated and line-ordered."""

    return _definition_analysis(text, path).definitions


def _overlaps(start: int, end: int, hunk: DiffHunk) -> bool:
    """True when [start, end] intersects a line the hunk actually changed.

    Not the hunk's printed span: ``new_start .. new_start + new_lines`` covers the
    three unchanged context lines git emits on each side under ``-U3``, so a
    two-line comment added above a definition put that definition's first three
    lines inside the span and the packet reported a byte-identical function as
    changed. Measured on this repository, roughly one ATTENTION headline in five
    was manufactured that way. ``DiffHunk.new_ranges`` holds only the added lines
    plus each deletion's join point.
    """

    if not hunk.new_ranges:
        # Empty ranges mean the producer could not read the patch body, not that
        # nothing changed -- so widen back to the printed span rather than drop
        # the symbol. (``+ new_lines - 1``: the span is inclusive of new_start.)
        return start <= hunk.new_start + hunk.new_lines - 1 and end >= hunk.new_start
    return any(start <= high and end >= low for low, high in hunk.new_ranges)


# ---------------------------------------------------------------------------
# engine access -- every call here is allowed to fail
# ---------------------------------------------------------------------------


def _open_engine(repo_root: Path) -> Any:
    """Return a non-autosyncing engine, or None when one cannot be built.

    ``autosync_enabled=False`` is mandatory: the default constructor starts a
    background indexing worker and a filesystem watchdog, and a read-only review
    command has no business leaving either behind.
    """

    try:
        from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine
    except Exception:
        return None
    try:
        return CodeContextEngine(repo_root, autosync_enabled=False)
    except Exception:
        return None


def _index_ready(engine: Any) -> bool:
    if engine is None:
        return False
    try:
        return bool(engine.index_ready())
    except Exception:
        return False


def _outline_entries(engine: Any, path: str) -> list[dict[str, Any]] | None:
    """Return the index's symbol rows for *path*; None when the query failed.

    ``None`` (query error) and ``[]`` (the index genuinely holds no rows for this
    file) mean different things upstream -- the first degrades ``symbol_relations``,
    the second is evidence the index has drifted -- so they stay distinguishable.
    """

    try:
        payload = engine.file_outline(file_path=path, limit=_OUTLINE_LIMIT, auto_index=False)
    except Exception:
        return None
    grouped = payload.get("files") if isinstance(payload, Mapping) else None
    if not isinstance(grouped, Mapping):
        return None
    entries = grouped.get(path)
    if entries is None and len(grouped) == 1:
        # The index normalizes paths on write; with a single group there is no
        # ambiguity about which file it belongs to.
        entries = next(iter(grouped.values()))
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _astgrep_ready(repo_root: Path) -> bool:
    """True when ast-grep can run without a download.

    Probed after the detectors have run, so a binary they bootstrapped counts as
    available. The probe refuses to download: reporting reduced precision costs a
    word in ``degraded``, while a network fetch from a read-only review command
    would be a surprise. The bootstrap fetches before it touches the filesystem,
    so a refused probe also creates no directory and no lockfile -- a review must
    not leave the working copy dirtier than it found it.
    """

    try:
        from lemoncrow.infra.code_intel.astgrep import (
            ManagedAstGrepAsset,
            bootstrap_managed_astgrep,
            discover_astgrep_binary,
        )
    except Exception:
        return False

    def _refuse(asset: ManagedAstGrepAsset) -> bytes:
        raise OSError("ast-grep availability probe does not download")

    try:
        if bool(discover_astgrep_binary(repo_root, allow_bootstrap=False).available):
            return True
        return bool(bootstrap_managed_astgrep(repo_root, downloader=_refuse).available)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# impact sites from the edit_impact detectors
# ---------------------------------------------------------------------------


def _site_dicts(raw: Any) -> list[dict[str, Any]]:
    """Normalize a detector's return value to a flat list of site dicts.

    ``contract_literal_impact`` returns ``{"reason": ..., "sites": [...]} | None``
    while the other three return a bare list, so the shapes are unified once here
    rather than at each of the four call sites.
    """

    payload: Any = raw
    if isinstance(payload, Mapping):
        payload = payload.get("sites")
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def _site_token(kind: ImpactKind, old: str) -> str:
    """The identifier a detector site is *about*, extracted from its ``old`` field.

    Each detector spells ``old`` differently -- a bare literal, a bare name,
    ``name(...)``, ``@deco on name()`` -- and the token is what links the site
    back to the changed file that produced it.
    """

    text = old.strip()
    if kind == "contract_literal":
        return text
    if kind == "decorator_contract":
        text = text.rpartition(" on ")[2] or text
    # A method site spells `old` as `Class.method(...)`. The attribution scan below
    # looks for the token in the *changed* file's text, and that file writes
    # `class Class:` and `def method(` on separate lines -- it never contains the
    # dotted spelling, so the qualifier has to come off or every method finding
    # would lose the link back to the edit that caused it.
    return text.split("(")[0].strip().rpartition(".")[2]


def _attribute_source(
    token: str,
    files: Sequence[ChangedFile],
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
) -> str:
    """Return the one changed file *token* came out of, or ``""`` when unsure.

    The detectors take every changed file in a single batch and return sites
    without saying which edit produced them, so the link is recovered here in two
    narrowing passes:

    1. which changed **code** file mentions the token at all (prose is excluded --
       a README that names the function is not where the contract lives);
    2. if that is still ambiguous, which of them actually *changed* a line
       containing it.

    Anything still ambiguous yields ``""``. A mis-attributed finding promotes the
    wrong file to the top of the reading order, which is worse than an
    unattributed one -- the site is still reported either way.
    """

    if not token:
        return ""
    # Whole-word for an identifier: a test named `test_refresh` is not a mention
    # of `refresh`, and counting it as one makes every attribution ambiguous.
    # Literals can hold punctuation, so those fall back to a substring test.
    pattern = re.compile(rf"\b{re.escape(token)}\b") if _IDENTIFIER_RE.fullmatch(token) else None
    definition = _definition_pattern(token)

    def _mentions(text: str) -> bool:
        if not text:
            return False
        return bool(pattern.search(text)) if pattern is not None else token in text

    def _defines(path: str) -> bool:
        if definition is None:
            return False
        return any(definition.search(blobs.get(path, "") or "") is not None for blobs in (new_blobs, old_blobs))

    def _lines_with_token(text: str) -> frozenset[str]:
        return frozenset(stripped for line in text.splitlines() if _mentions(stripped := line.strip()))

    hits = [
        item.path
        for item in files
        if item.category not in _NON_CODE_CATEGORIES
        and (_mentions(old_blobs.get(item.path, "")) or _mentions(new_blobs.get(item.path, "")))
    ]
    if len(hits) == 1:
        return hits[0]
    # The file that *defines* the token is where the contract lives, and it wins
    # ahead of the edited-line test rather than after it. `SessionManager.refresh`
    # gaining a parameter edits both the definition and its in-patch callers, so
    # "which file changed a line mentioning `refresh`" is ambiguous by two and
    # returned nothing -- leaving the marquee finding with no source file printed
    # beside it and its sites credited to no changed file at all, which is also
    # how REVIEW ORDER came to count fewer sites than ATTENTION listed.
    defining = [path for path in hits if _defines(path)]
    if len(defining) == 1:
        return defining[0]
    edited = [
        path
        for path in hits
        if _lines_with_token(old_blobs.get(path, "")) != _lines_with_token(new_blobs.get(path, ""))
    ]
    return edited[0] if len(edited) == 1 else ""


def _foreign_path_filter(repo_root: Path) -> Callable[[str], bool]:
    """Return "does this path belong to a *different* repository?".

    The code index is keyed to the *workspace*, and a workspace contains whatever
    is checked out under it -- including submodules and nested clones. Nothing
    downstream distinguishes them, so a change to this repository was cited as
    reaching ``lemoncode/packages/opencode/test/...`` and
    ``landing/functions/api/license.test.ts``: two other repositories, whose
    files this diff cannot affect and whose contents are a gitlink, not a blob,
    in every revision of this one.

    ``head_path_filter`` does not catch it. In ``commit_range`` mode it happens
    to, because the tree lookup fails on a path the tree does not contain; in
    ``working_tree`` mode the filesystem is the authority and the submodule is
    right there on disk.

    A directory that contains a ``.git`` entry -- file or directory, so a
    submodule's gitlink file counts -- is another repository's root. The check
    walks a candidate's ancestors, never the tree, and caches per directory, so a
    review with no out-of-patch reach pays nothing. Failure answers *not foreign*:
    an unreadable directory must not delete a real finding.
    """

    cache: dict[str, bool] = {}

    def _foreign(path: str) -> bool:
        # Sites are spelled "rel/p.py:L12"; only the file part is a location.
        head = path.split(":L", 1)[0]
        if not head:
            return False
        candidate = PurePosixPath(head)
        if candidate.is_absolute() or ".." in candidate.parts:
            # Not expressible as a path inside this repository at all.
            return True
        for parent in reversed(candidate.parents):
            name = str(parent)
            if name == ".":
                continue  # the repository root's own .git is not foreign
            hit = cache.get(name)
            if hit is None:
                try:
                    hit = (repo_root / name / ".git").exists()
                except OSError:
                    hit = False
                cache[name] = hit
            if hit:
                return True
        return False

    return _foreign


def _to_site(
    kind: ImpactKind,
    entry: Mapping[str, Any],
    changed_paths: frozenset[str],
    source_path: str = "",
) -> ImpactSite | None:
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return None
    new_value = entry.get("new")
    return ImpactSite(
        kind=kind,
        path=path,
        old=str(entry.get("old") or ""),
        new=str(new_value) if isinstance(new_value, str) else None,
        snippet=str(entry.get("snippet") or ""),
        # `path` is "rel/p.py:L12"; only the file part decides membership.
        in_patch=path.split(":L")[0] in changed_paths,
        source_path=source_path,
        # Absent on every confident site, so a detector that says nothing about
        # confidence keeps producing plain facts.
        uncertainty=str(entry.get("uncertainty") or ""),
    )


def _detector_sites(
    repo_root: Path,
    files: Sequence[ChangedFile],
    *,
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
    engine: Any,
    all_touched_paths: Sequence[str] | None = None,
) -> tuple[list[ImpactSite], set[str]]:
    """Run the four deterministic detectors over source edits.

    ``files`` may be only the source edits that changed since the previous
    ReviewRevision. ``all_touched_paths`` remains the complete current patch,
    so detector results still exclude every in-patch consumer exactly as a full
    run would. This split is the incremental-analysis seam.
    """

    edits: list[dict[str, Any]] = [
        {
            "path": item.path,
            "old_string": old_blobs.get(item.path, ""),
            "new_string": new_blobs.get(item.path, ""),
        }
        for item in files
    ]
    touched_paths = list(all_touched_paths) if all_touched_paths is not None else [item.path for item in files]
    changed_paths = frozenset(touched_paths)

    detectors: list[tuple[str, ImpactKind, Callable[[], Any]]] = [
        (
            "contract_literal_impact",
            "contract_literal",
            lambda: contract_literal_impact(edits, engine=engine, repo_root=repo_root, touched_paths=touched_paths),
        ),
        (
            "decorator_contract_impact",
            "decorator_contract",
            lambda: decorator_contract_impact(edits, engine=engine, touched_paths=touched_paths),
        ),
        (
            "symbol_contract_impact",
            "removed_symbol",
            lambda: symbol_contract_impact(edits, engine=engine, repo_root=repo_root, touched_paths=touched_paths),
        ),
        (
            "signature_change_impact",
            "signature_change",
            lambda: signature_change_impact(edits, engine=engine, repo_root=repo_root, touched_paths=touched_paths),
        ),
    ]
    sites: list[ImpactSite] = []
    degraded: set[str] = set()
    attribution: dict[str, str] = {}
    # The four detectors are independent, read-only analyses over the same edit
    # snapshot. Run them together, but consume results in declaration order so
    # site order, attribution order, and degradation semantics stay identical to
    # the former serial loop.
    with ThreadPoolExecutor(max_workers=len(detectors), thread_name_prefix="lc-review-impact") as executor:
        futures = {name: executor.submit(call) for name, _kind, call in detectors}
        for name, kind, _call in detectors:
            try:
                raw = futures[name].result()
            except Exception:
                degraded.add(name)
                continue
            if isinstance(raw, Mapping):
                degraded.update(str(reason) for reason in (raw.get("degraded") or ()) if isinstance(reason, str))
            else:
                # A detector that returns a plain list has no room for a
                # "this pass was partial" note, so the two that need one return
                # a list subclass carrying it. Reading the attribute here is
                # what keeps a capped ast-grep scan from being reported as a
                # complete one -- the whole point of the truncation signal.
                degraded.update(str(reason) for reason in getattr(raw, "degraded", ()) if isinstance(reason, str))
            for entry in _site_dicts(raw):
                token = _site_token(kind, str(entry.get("old") or ""))
                source = attribution.get(token)
                if source is None:
                    source = _attribute_source(token, files, old_blobs, new_blobs)
                    attribution[token] = source
                site = _to_site(kind, entry, changed_paths, source)
                if site is not None:
                    sites.append(site)
    return sites, degraded


# ---------------------------------------------------------------------------
def _new_side_change(item: ChangedFile) -> SymbolChange:
    return "added" if item.status == "added" else "modified"


def _symbol_text(new_lines: Sequence[str], start: int, end: int) -> str:
    """The new-side source of the symbol spanning [start, end], or ``""``."""

    if start < 1 or start > len(new_lines):
        return ""
    return "\n".join(new_lines[start - 1 : min(end, len(new_lines))]).strip()


def _bracket_delta(text: str) -> int:
    """Openers minus closers on one line. Approximate by design -- see :func:`_prelude_start`."""

    return sum(text.count(char) for char in "([{") - sum(text.count(char) for char in ")]}")


def _prelude_start(lines: Sequence[str], floor: int, definition_line: int) -> int:
    """The first line of *definition_line*'s own prelude: decorators, comments, blanks.

    ``Tag.line`` points at the ``def`` keyword, not at what is stacked above it,
    so "the previous definition ends where the next one begins" quietly handed
    every decorator and every banner comment to the definition *before* it. A
    commit that adds one ``@click.option(...)`` above a command therefore
    reported the command above that one as changed -- byte-identical, with real
    callers attached, and immune to the ``body in baseline`` guard because the
    body it was compared against had just grown the added line. Two comment lines
    above a ``def`` did the same thing for the same reason, which is how a
    pure-comment commit still opened with an ATTENTION headline.

    Walking up from the ``def`` is enough: skip blanks and line comments, and
    skip any line that begins a balanced ``@`` block or closes a ``/* ... */``
    one. Balance is counted from brackets so that a multi-line
    ``@click.option(\n ... \n)`` is climbed as one unit rather than stopping at
    its closing paren. The count ignores brackets inside strings, so when it is
    wrong the scan simply stops early and the window stays where it already was,
    which is the behaviour being replaced.

    Nothing skipped here is executable, so nothing skipped here can be a change
    to the definition below it or to the one above it. Never walks at or below
    *floor* (the owning definition's own line), and is bounded so a pathological
    file cannot turn this into a quadratic scan.
    """

    cursor = definition_line
    budget = _MAX_PRELUDE_LINES
    while cursor - 1 > floor and budget > 0:
        budget -= 1
        probe = cursor - 1
        text = lines[probe - 1].strip()
        if not text or text.startswith(_COMMENT_PREFIXES):
            cursor = probe
            continue
        head = probe
        if text.endswith("*/"):
            while not lines[head - 1].lstrip().startswith("/*") and head - 1 > floor and budget > 0:
                budget -= 1
                head -= 1
            if not lines[head - 1].lstrip().startswith("/*"):
                break
            cursor = head
            continue
        depth = _bracket_delta(lines[head - 1])
        while depth < 0 and head - 1 > floor and budget > 0:
            budget -= 1
            head -= 1
            depth += _bracket_delta(lines[head - 1])
        if depth != 0 or not lines[head - 1].lstrip().startswith("@"):
            break
        cursor = head
    return cursor


def _indent_width(text: str) -> int:
    """Leading-whitespace width of *text*."""

    return len(text) - len(text.lstrip())


def _body_spans(text: str, path: str) -> tuple[tuple[int, int], ...]:
    """``(first line, last line)`` of every Python definition a real parse can see.

    Direct callers retain the old helper contract. The main review path uses
    :func:`_definition_analysis` so the tags and these spans come from the same
    parse rather than paying for this projection separately.
    """

    if not text or Path(path).suffix.lower() not in _PYTHON_SUFFIXES:
        return ()
    return _python_definition_analysis(text, path).body_spans


def _indented_body_end(lines: Sequence[str], start: int) -> int:
    """Last line of the body opened at *start*, by indentation. The fallback parse.

    Used where :func:`_body_spans` has nothing to say. The rule is the one the
    languages this pass sees all obey: a body is the run of blank or
    more-indented lines below its header, and a line back at the header's own
    indentation belongs to something else. Trailing blanks are dropped, so a
    blank line added in the gap between two definitions is not an edit to the one
    above it.

    Brace languages put the header's opener and the body's closer at the header's
    indentation, which the indentation rule alone would read as "the body is
    empty". So an unclosed bracket run is followed to its close: while the count
    is open every line belongs to the body, and a line that merely opens or
    closes a block does not end it. The count ignores brackets inside strings and
    comments (see :func:`_bracket_delta`), and when it is wrong the window is
    still fenced by the next sibling definition's prelude in
    :func:`_definition_windows`.
    """

    total = len(lines)
    if not 1 <= start <= total:
        return start
    base = _indent_width(lines[start - 1])
    depth = max(0, _bracket_delta(lines[start - 1]))
    end = start
    for cursor in range(start + 1, min(total, start + _MAX_BODY_LINES) + 1):
        raw = lines[cursor - 1]
        text = raw.strip()
        if depth <= 0:
            if not text:
                continue
            if _indent_width(raw) <= base and not text.startswith(_BLOCK_DELIMITERS):
                break
        end = cursor
        depth = max(0, depth + _bracket_delta(raw))
    return end


def _body_end(lines: Sequence[str], spans: Sequence[tuple[int, int]], start: int) -> int:
    """Where the definition beginning at *start* actually ends.

    The smallest parsed span containing *start* -- smallest, so a nested
    definition answers for its own lines rather than its parent's -- and the
    indentation scan when no span contains it.
    """

    best: int | None = None
    width: int | None = None
    for low, high in spans:
        if low <= start <= high and (width is None or high - low < width):
            width, best = high - low, high
    if best is None:
        return _indented_body_end(lines, start)
    return max(start, min(best, len(lines)))


def _sibling_ceiling(definitions: Sequence[Tag], index: int, lines: Sequence[str]) -> int:
    """The last line before the next *sibling* definition's prelude.

    Sibling, not merely next: a nested definition sits inside the body it is
    being fenced against, so stopping at it would hand the rest of the parent's
    body to nobody. Indentation is what separates the two, and it is the same
    fact :func:`_kind_from_line` already reads off the definition's own line.

    A ceiling only ever narrows a window, so a parse that is right loses nothing
    by it and a bracket count that is wrong cannot leak into the next definition.
    """

    total = len(lines)
    start = definitions[index].line
    if not 1 <= start <= total:
        return total
    base = _indent_width(lines[start - 1])
    for following in definitions[index + 1 :]:
        line = following.line
        if not 1 <= line <= total or line <= start:
            continue
        if _indent_width(lines[line - 1]) > base:
            continue
        return max(start, _prelude_start(lines, start, line) - 1)
    return total


def _definition_windows(
    definitions: Sequence[Tag], lines: Sequence[str], spans: Sequence[tuple[int, int]] = ()
) -> list[tuple[Tag, int, int, int]]:
    """``(tag, start, window_end, reported_end)`` per definition, in file order.

    Each definition owns its own body and nothing else. It used to own everything
    up to the next definition's prelude, which is the same span only when nothing
    sits between the two -- and an ordinary Python file is full of things that
    do: a ``try:``/``except ImportError:`` guard, an ``if`` block configuring a
    logger, a bare registration call, a ``for`` loop, a ``with`` block. Editing
    any of them reported the byte-identical definition *above* it as changed,
    with its real untouched callers attached and no degraded signal, because the
    body that reached the ``body in baseline`` guard had just grown the edited
    line. The same arithmetic gave a nested definition the remainder of its
    parent's body, so an edit after a nested ``def`` was attributed to the nested
    ``def`` and not to the parent it was actually in.

    So the end comes from a parse (:func:`_body_spans`) wherever one is
    available and from indentation (:func:`_indented_body_end`) where it is not,
    fenced either way by the next sibling's prelude. ``reported_end`` is that
    same line: the window is the body now, so there is nothing left to be
    conservative about.

    Shared by both symbol sources so that "which lines is this definition made
    of" has exactly one answer in this module, derived from one text.
    """

    total = len(lines)
    windows: list[tuple[Tag, int, int, int]] = []
    for index, tag in enumerate(definitions):
        start = tag.line
        if not 1 <= start <= total:
            windows.append((tag, start, start, start))
            continue
        end = min(_body_end(lines, spans, start), _sibling_ceiling(definitions, index, lines))
        end = max(start, end)
        windows.append((tag, start, end, end))
    return windows


@dataclass(frozen=True)
class SymbolWindow:
    """One definition in a file, with the exact body text the window covers.

    The public shape of what :func:`_definition_windows` has always produced
    internally. It exists so a fingerprint can be taken over a symbol's body
    without importing a private name and without a second, drifting copy of the
    window arithmetic.
    """

    name: str
    kind: str
    """Tree-sitter node kind where one is known, else ``function``/``method``/
    ``class`` read off the definition line, else ``""``."""
    path: str
    start_line: int
    """1-based line of the ``def``/``class`` keyword -- ``Tag.line``."""
    end_line: int
    """1-based last line of the body window."""
    body: str


def symbol_windows(text: str, path: str) -> tuple[SymbolWindow, ...]:
    """Every definition in *text* with its body window. Unfiltered by hunks.

    Composed from exactly the calls :func:`_index_symbols` makes, so a window
    here and a window there are the same lines by construction rather than by
    agreement. The one difference is deliberate: ``_index_symbols`` keeps only
    the definitions a hunk touched, while a unit index needs *every* definition
    the blob contains -- a symbol nobody edited this revision is still a thing a
    reviewer marked reviewed last revision.

    Two limitations to know before fingerprinting the body:

    * **The window starts at the ``def``/``class`` line, so decorators and
      leading comments are outside it** (:func:`_prelude_start` only ever fences
      the *previous* definition). Adding ``@lru_cache`` above a function
      therefore changes that file's fingerprint and **not** the symbol's. That is
      a documented limitation, not a bug: the prelude is what separates two
      neighbouring definitions, and charging it to the one below would hand every
      decorator edit to a body nobody touched.
    * Python bodies end at ``ast.end_lineno``; every other language falls back to
      indentation plus an approximate bracket balance, so non-Python symbol
      fingerprints are more prone to a spurious reopen.

    Never raises. An unparseable or unsupported file yields ``()`` -- its file
    unit still exists, and that is the honest answer.
    """

    lines = text.splitlines()
    analysis = _definition_analysis(text, path)
    tags = analysis.definitions
    spans = analysis.body_spans
    return tuple(
        SymbolWindow(
            name=tag.name,
            kind=tag.node_kind or _kind_from_line(lines, start) or "",
            path=path,
            start_line=start,
            end_line=end,
            body=_symbol_text(lines, start, end),
        )
        for tag, start, end, _reported in _definition_windows(tags, lines, spans)
    )


def _bodies_by_name(text: str, path: str) -> dict[str, set[str]]:
    """Every definition *text* holds, keyed by name, spelled as :func:`_symbol_text` spells it.

    The base side of "did this definition actually change?". A set per name
    because one blob may define the same bare name twice (an overload, a method
    on two classes); a new body matching *any* of them is a body that already
    existed under that name, which is what the test is asking.

    Both sides are joined from ``splitlines()``, so a CRLF blob still compares
    against a ``\\n``-joined body instead of never matching.
    """

    bodies: dict[str, set[str]] = {}
    for window in symbol_windows(text, path):
        if window.body:
            bodies.setdefault(window.name, set()).add(window.body)
    return bodies


def _index_symbols(
    item: ChangedFile,
    entries: Sequence[Mapping[str, Any]],
    definitions: Sequence[Tag],
    *,
    old_text: str = "",
    new_text: str = "",
    body_spans: Sequence[tuple[int, int]] | None = None,
) -> tuple[list[ChangedSymbol], set[str]]:
    """Name a file's changed definitions -- enriched by the index, never located by it.

    The index is keyed to the workspace on disk; a hunk's line numbers belong to
    the revision under review. Intersecting one against the other was arithmetic
    across two coordinate systems, and when the file had shifted between them it
    did not merely add noise: it reported byte-identical definitions as changed
    *and silently dropped the ones that had actually changed*, while the footer
    still read ``index: fresh``. Trimming six header lines from a file was enough
    to swap a real finding for a fabricated one.

    So every row is re-anchored to a definition parsed out of the reviewed blob
    (:func:`_definition_tags`), and a row naming something that blob does not
    define is dropped outright. Geometry comes from the blob, always. What the
    index is kept for is what only it knows: qualified names, and the caller and
    centrality lookups :func:`_enrich` keys off the symbol name.

    Belt and braces on top of that: a symbol whose new-side source is
    byte-identical to *its own* definition in the base blob is dropped whatever
    the line arithmetic said. A definition nobody touched must never reach the
    packet, because every warning built on it -- caller counts, centrality,
    ATTENTION headlines -- is then a warning about nothing.

    Its own, and not the whole old file: the test used to be ``body in
    old_text``, which asks "do these bytes appear anywhere back there?" and
    that is a different question. An edit that only deletes the trailing lines
    of a body leaves a remainder that is a verbatim *prefix* of what that body
    used to be, so a genuinely changed definition was dropped in silence --
    ``symbols: 0``, no ATTENTION section, three untouched callers unreported,
    and a footer still reading ``index: fresh``. See :func:`_bodies_by_name`.

    What the index may never do is *subtract*. A definition the reviewed blob
    contains and the index has no row for used to be dropped where it stood, in
    silence: on a commit whose outline the index had walked to 94 of the blob's
    223 definitions, eleven hunk-touched definitions vanished and the packet
    reported zero symbols for a file whose ``create_app`` had grown by 3.3 kB.
    Such a definition is now reported from the blob alone -- real name, real
    lines, no qualified name and no graph signal, because those are the parts
    only the index knows -- and the shortfall is named in ``degraded``.
    """

    if not item.hunks or not definitions:
        return [], set()
    by_name: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        name = str(entry.get("name") or "")
        if name and name not in by_name:
            by_name[name] = entry

    degraded: set[str] = set()
    change = _new_side_change(item)
    new_lines = new_text.splitlines()
    # Parsed on first need: a file whose hunks land on no definition at all
    # never pays for the base-side parse.
    base_bodies: dict[str, set[str]] | None = None
    # The pre-parse fallback, kept for the case below where the base side has
    # no body under this name to compare against.
    baseline = "\n".join(old_text.splitlines())
    out: list[ChangedSymbol] = []
    seen: set[tuple[str, int]] = set()
    spans = _body_spans(new_text, item.path) if body_spans is None else body_spans
    for tag, start, window_end, reported_end in _definition_windows(definitions, new_lines, spans):
        if not any(_overlaps(start, window_end, hunk) for hunk in item.hunks):
            continue
        body = _symbol_text(new_lines, start, window_end)
        if body and old_text:
            if base_bodies is None:
                base_bodies = _bodies_by_name(old_text, item.path)
            known = base_bodies.get(tag.name)
            if known is not None:
                # The base side defines this name: an exact body match means it
                # did not change, and a mismatch means it did -- including the
                # trailing-lines deletion the whole-blob substring test missed.
                if body in known:
                    continue
            elif body in baseline:
                # No base-side body under this name, so there is nothing exact
                # to compare. That happens for a name the base genuinely did
                # not define -- and also whenever the base blob does not parse
                # (`symbol_windows` returns nothing at all, e.g. the commit
                # that fixes a syntax error). Falling through to the old
                # whole-blob test keeps an unparseable base from reporting
                # every hunk-touched definition in the file as changed.
                continue
        key = (tag.name, start)
        if key in seen:
            continue
        seen.add(key)
        row = by_name.get(tag.name)
        if row is None:
            # No row to enrich from -- but the blob defines it and the diff
            # touched it, so it is a changed symbol whatever the index knows.
            degraded.add(_SIGNAL_OUTLINE_INCOMPLETE)
            out.append(
                ChangedSymbol(
                    symbol_name=tag.name,
                    qualified_name=None,
                    kind=tag.node_kind or _kind_from_line(new_lines, start),
                    file_path=item.path,
                    start_line=start,
                    end_line=reported_end,
                    change=change,
                    source="tree_sitter",
                )
            )
            continue
        qualified = row.get("qualified_name")
        kind = row.get("kind")
        out.append(
            ChangedSymbol(
                symbol_name=tag.name,
                # file_outline omits qualified_name when it equals the bare name;
                # restoring it keeps the centrality lookup a single code path.
                qualified_name=qualified if isinstance(qualified, str) and qualified else tag.name,
                kind=str(kind) if kind else (tag.node_kind or _kind_from_line(new_lines, start)),
                file_path=item.path,
                start_line=start,
                end_line=reported_end,
                change=change,
                source="index",
            )
        )
    return out, degraded


def _tree_sitter_symbols(
    item: ChangedFile,
    new_text: str,
    definitions: Sequence[Tag],
    *,
    body_spans: Sequence[tuple[int, int]] | None = None,
) -> list[ChangedSymbol]:
    """Map hunks onto definitions parsed from the new-side text.

    Each definition owns its own body -- see :func:`_definition_windows` for how
    that end is found and what the arithmetic it replaced cost. That window is
    what the hunk is tested against, and it is what ``end_line`` reports.
    """

    if not item.hunks or not definitions:
        return []
    lines = new_text.splitlines()
    change = _new_side_change(item)
    spans = _body_spans(new_text, item.path) if body_spans is None else body_spans
    windows = _definition_windows(definitions, lines, spans)

    out: list[ChangedSymbol] = []
    seen: set[tuple[str, int]] = set()
    for hunk in item.hunks:
        if hunk.new_lines == 0:
            continue  # pure deletion -- resolved from the base side instead
        for tag, start, window_end, reported_end in windows:
            # Same correction as `_index_symbols`: the hunk's printed span
            # includes context lines, so testing against it made every
            # definition within three lines of an edit look edited.
            if not _overlaps(start, window_end, hunk):
                continue
            key = (tag.name, start)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ChangedSymbol(
                    symbol_name=tag.name,
                    qualified_name=None,
                    kind=tag.node_kind or _kind_from_line(lines, start),
                    file_path=item.path,
                    start_line=start,
                    end_line=reported_end,
                    change=change,
                    source="tree_sitter",
                )
            )
    return out


def _deleted_symbols(item: ChangedFile, old_text: str) -> list[ChangedSymbol]:
    """Resolve definitions that only exist on the base side.

    A deleted file and a pure-deletion hunk have no new-side text at all, so the
    index and the new blob are both silent about them -- and a definition that
    vanished is exactly what a reviewer most needs named.

    Compute the deletion ranges *before* parsing the old blob. Most modified
    files have no pure-deletion hunk at all; parsing every base-side Python file
    anyway was a full extra AST walk that could not possibly produce a deleted
    symbol for those files.
    """

    if not old_text:
        return []
    ranges: list[tuple[int, int]] = []
    if item.status == "deleted" and not item.hunks:
        ranges.append((1, _line_count(old_text)))
    else:
        for hunk in item.hunks:
            if item.status != "deleted" and hunk.new_lines != 0:
                continue
            # `old_ranges` names the removed lines themselves; the header span is
            # the context-padded fallback for a patch body that could not be read.
            ranges.extend(hunk.old_ranges or ((hunk.old_start, hunk.old_start + max(0, hunk.old_lines) - 1),))
    if not ranges:
        return []

    definitions = _definition_tags(old_text, item.old_path or item.path)
    if not definitions:
        return []
    lines = old_text.splitlines()
    out: list[ChangedSymbol] = []
    seen: set[tuple[str, int]] = set()
    for low, high in ranges:
        for tag in definitions:
            if not low <= tag.line <= high:
                continue
            key = (tag.name, tag.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ChangedSymbol(
                    symbol_name=tag.name,
                    qualified_name=None,
                    kind=tag.node_kind or _kind_from_line(lines, tag.line),
                    file_path=item.path,
                    start_line=tag.line,
                    end_line=tag.line,
                    change="deleted",
                    source="tree_sitter",
                )
            )
    return out


def _outline_drifted(entries: Sequence[Mapping[str, Any]], new_text: str) -> bool:
    """True when the index's rows do not describe the blob under review.

    Content-based and, unlike its predecessor, symmetric. The old test asked
    "does a row end past the end of the file?", which can only fire when the
    workspace copy is *longer* than the reviewed one -- so every shift in the
    other direction was invisible and the footer printed ``index: fresh`` over a
    packet built from mismatched line numbers. Deleting six header lines is a
    shift in the invisible direction.

    The test here is the one that matters: a row must actually find its own name
    on the line it claims. That catches a shift either way, and a rename in
    place. It never goes near ``tool_blame``, which hard-errors ``index_stale``
    instead of degrading.

    An empty or unreadable blob is not evidence of drift -- ``blob_unreadable``
    already reports that -- so it answers False.
    """

    lines = new_text.splitlines()
    if not lines:
        return False
    for entry in entries:
        name = str(entry.get("name") or "")
        # A row whose name is not a bare identifier has no reliable spelling to
        # look for; silence beats a fabricated staleness warning.
        if not name or not _IDENTIFIER_RE.fullmatch(name):
            continue
        start = _as_int(entry.get("line_start"), 0)
        if not 1 <= start <= len(lines):
            return True
        if not re.search(rf"\b{re.escape(name)}\b", lines[start - 1]):
            return True
    return False


def _changed_symbols(
    engine: Any,
    files: Sequence[ChangedFile],
    *,
    ready: bool,
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
) -> tuple[list[ChangedSymbol], bool, set[str]]:
    """Return (symbols, index-drifted, degraded) for every changed file."""

    symbols: list[ChangedSymbol] = []
    degraded: set[str] = set()
    drifted = False

    for item in files:
        if item.category in _NON_CODE_CATEGORIES:
            continue
        new_text = new_blobs.get(item.path, "")
        old_text = old_blobs.get(item.path, "")
        analysis = _DefinitionAnalysis()
        definitions: tuple[Tag, ...] = ()
        if item.status != "deleted":
            analysis = _definition_analysis(new_text, item.path)
            definitions = analysis.definitions
            if analysis.dropped:
                # The file had tags that name something without defining it. They
                # are gone from every count and every site; say so rather than
                # let the shorter list read as "this file defines less".
                degraded.add(_SIGNAL_NON_DEFINITIONS)

        entries: list[dict[str, Any]] | None = None
        if ready and item.status != "deleted":
            entries = _outline_entries(engine, item.path)
            if entries is None:
                degraded.add(_SIGNAL_RELATIONS)
            elif _outline_drifted(entries, new_text):
                drifted = True

        if entries:
            named, index_degraded = _index_symbols(
                item,
                entries,
                definitions,
                old_text=old_text,
                new_text=new_text,
                body_spans=analysis.body_spans,
            )
            symbols.extend(named)
            degraded.update(index_degraded)
        else:
            if ready and entries is not None and definitions:
                # The index answered, and it has never seen a file that plainly
                # defines symbols -- that is drift, not an empty file.
                drifted = True
            symbols.extend(_tree_sitter_symbols(item, new_text, definitions, body_spans=analysis.body_spans))
        symbols.extend(_deleted_symbols(item, old_text))

    ordered: list[ChangedSymbol] = []
    seen: set[tuple[str, str, int, str]] = set()
    for symbol in sorted(symbols, key=lambda item: (item.file_path, item.start_line, item.symbol_name, item.change)):
        key = (symbol.file_path, symbol.symbol_name, symbol.start_line, symbol.change)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(symbol)
    return ordered, drifted, degraded


# ---------------------------------------------------------------------------
# fan-out, centrality and untouched callers
# ---------------------------------------------------------------------------


def _merged_target_count(payload: Mapping[str, Any]) -> int:
    """How many same-named definitions the engine merged to answer this query.

    ``1`` when the name resolved to exactly one definition (the key is absent),
    so callers may be attributed to it.
    """

    ambiguity = payload.get("ambiguity")
    if not isinstance(ambiguity, Mapping):
        return 1
    return max(1, _as_int(ambiguity.get("merged_target_count"), 1))


def _resolved_to(payload: Mapping[str, Any]) -> str:
    target = payload.get("target")
    if not isinstance(target, Mapping):
        return ""
    path = target.get("path")
    return path if isinstance(path, str) else ""


@dataclass(frozen=True)
class _Resolution:
    """What one caller query proved about a bare symbol name.

    ``merged_targets`` is how many same-named definitions the engine folded into
    its answer and ``definition_path`` is the file it settled on. Together they
    are the only evidence available that a bare-name count belongs to the
    definition this diff touched. ``payload`` is kept so the caller-site pass can
    reuse the answer instead of paying for the same query twice.
    """

    payload: Mapping[str, Any]
    merged_targets: int
    definition_path: str


def _resolution_from_payload(payload: object) -> _Resolution | None:
    if not isinstance(payload, Mapping):
        return None
    return _Resolution(
        payload=payload,
        merged_targets=_merged_target_count(payload),
        definition_path=_resolved_to(payload),
    )


def _resolve_name(engine: Any, name: str) -> _Resolution | None:
    """Ask the index which definition *name* refers to; None when it could not say.

    ``file_path`` is deliberately *not* passed: supplying it makes the engine
    short-circuit to a direct lookup that reports no ambiguity at all, which
    would turn every name into a false "unambiguous" and defeat the guard this
    call exists to feed.
    """

    try:
        payload = engine.tool_callers(symbol_name=name, limit=_CALLERS_PER_SYMBOL, auto_index=False)
    except Exception:
        return None
    return _resolution_from_payload(payload)


def _resolve_names(engine: Any, names: Sequence[str]) -> dict[str, _Resolution]:
    """Qualify many bare names, using the batch seam when the engine provides it.

    Older engines and test doubles remain valid: any missing or malformed batch
    answer falls back to the scalar resolver for that name. This keeps the trust
    rule identical while collapsing the normal large-review path from hundreds
    of graph-tool calls to one local-index batch.
    """

    ordered = list(dict.fromkeys(name for name in names if name))
    if not ordered:
        return {}
    resolved: dict[str, _Resolution] = {}
    batch = getattr(engine, "tool_callers_batch", None)
    if callable(batch):
        try:
            payloads = batch(ordered, limit=_CALLERS_PER_SYMBOL, auto_index=False)
        except Exception:
            payloads = None
        if isinstance(payloads, Mapping):
            for name in ordered:
                resolution = _resolution_from_payload(payloads.get(name))
                if resolution is not None:
                    resolved[name] = resolution
    for name in ordered:
        if name in resolved:
            continue
        resolution = _resolve_name(engine, name)
        if resolution is not None:
            resolved[name] = resolution
    return resolved


def _out_of_private_scope(name: str, definition_path: str, caller_path: str) -> bool:
    """True when *caller_path* is somewhere a private *name* cannot be called from.

    One leading underscore makes a Python name module-private. The widest honest
    reading of the convention is package-private -- ``from ._shared import _emit``
    is ordinary in this tree -- so a sibling module in the same directory still
    counts as a caller and anything further away does not. Two leading
    underscores are class-private and name-mangled, so only the defining file
    can reach them. Dunder names (``__init__``) are public protocol, not private,
    and are not gated at all. Non-Python definitions are left alone: this is
    Python's rule, not a universal one.
    """

    if not name.startswith("_") or (name.startswith("__") and name.endswith("__")):
        return False
    if Path(definition_path).suffix.lower() not in _PYTHON_SUFFIXES:
        return False
    if caller_path == definition_path:
        return False
    if name.startswith("__"):
        return True
    return Path(caller_path).parent != Path(definition_path).parent


def _merged_namesakes(resolution: _Resolution, name: str, file_path: str) -> bool:
    """True when the answer's own caller list proves same-named definitions were merged.

    :func:`_merged_target_count` believes the index, and the index reports ``1``
    whenever the payload carries no ``ambiguity`` block -- which is exactly the
    case this catches. A reported caller that could not legally reach a
    module-private *name* is not this definition's caller; it belongs to a
    namesake the engine folded in without saying so. One such site is proof of
    the merge, and a merged answer cannot qualify any count derived from it.
    """

    related = resolution.payload.get("related")
    if not isinstance(related, list):
        return False
    for node in related:
        if not isinstance(node, Mapping):
            continue
        path = node.get("path") or node.get("file_path")
        if isinstance(path, str) and path and _out_of_private_scope(name, file_path, path):
            return True
    return False


def _attributable(resolution: _Resolution | None, file_path: str, name: str) -> bool:
    """True only when the counted definition is the one in *file_path*.

    Unresolved, merged, and resolved-elsewhere all answer False: an absent
    signal is honest, a borrowed one silently reorders the reviewer's attention.
    "Merged" is asked twice -- once as the index reports it, once against where
    the callers actually live -- because the index does not always know that it
    merged (see :func:`_merged_namesakes`).
    """

    if resolution is None:
        return False
    if resolution.merged_targets > _MAX_MERGED_TARGETS:
        return False
    if not resolution.definition_path or resolution.definition_path != file_path:
        return False
    return not _merged_namesakes(resolution, name, file_path)


def _resolution_order(symbols: Sequence[ChangedSymbol]) -> list[str]:
    """Bare names worth qualifying, strongest provisional signal first.

    Only names carrying a non-zero count can mislead, so only those are worth a
    query; ordering them by strength means a budget that runs out drops the
    weakest claims rather than an arbitrary slice.
    """

    strongest: dict[str, tuple[int, int, int]] = {}
    for symbol in symbols:
        if symbol.caller_count <= 0 and symbol.usage_count <= 0 and symbol.centrality_rank is None:
            continue
        key = (
            -symbol.caller_count,
            symbol.centrality_rank if symbol.centrality_rank is not None else _CENTRALITY_LIMIT + 1,
            -symbol.usage_count,
        )
        current = strongest.get(symbol.symbol_name)
        if current is None or key < current:
            strongest[symbol.symbol_name] = key
    return [name for name, _ in sorted(strongest.items(), key=lambda item: (item[1], item[0]))]


def _percentile(rank: int | None, node_count: int) -> float | None:
    if rank is None:
        return None
    return round(1.0 - (rank - 1) / max(1, node_count), 4)


def _enrich(
    engine: Any, symbols: Sequence[ChangedSymbol]
) -> tuple[list[ChangedSymbol], set[str], dict[str, _Resolution]]:
    """Attach caller/usage counts and centrality rank, qualified to *this* diff.

    Two whole-batch calls carry the raw numbers: ``badge_counts_batch`` is three
    ``IN (?)`` queries for the entire list and ``call_graph_centrality`` is one
    cached whole-repo ranking. Both are keyed on the **bare** symbol name, and
    the index is one flat namespace -- so every number they return is provisional
    until something proves it belongs to the definition this diff touched.

    Measured on this repository before that proof existed: a brand-new test
    file's local ``_invoke`` helper reported 174 callers and a 0.9973 centrality
    percentile, all of it borrowed from the nineteen other ``_invoke``s in the
    tree, which was enough to rank six new test files above the 890-line
    production module they test. The qualification is the same one the untouched-
    caller pass already applies -- exactly one same-named definition in the index,
    and it is in the changed file -- hoisted here so the counts are attributed
    before anything reads them. Names that fail it, and names the query budget
    never reached, report no signal at all.

    The resolutions are returned rather than discarded so the caller-site pass
    reuses them instead of re-running the same queries.
    """

    if not symbols:
        return [], set(), {}
    degraded: set[str] = set()

    counts: dict[str, Any] = {}
    try:
        raw_counts = engine.badge_counts_batch([symbol.symbol_name for symbol in symbols])
        if isinstance(raw_counts, Mapping):
            counts = dict(raw_counts)
    except Exception:
        degraded.add(_SIGNAL_RELATIONS)

    ranks: dict[str, int] = {}
    node_count = 0
    try:
        centrality = engine.call_graph_centrality(limit=_CENTRALITY_LIMIT, use_cache=True)
        node_count = _as_int(centrality.get("node_count"), 0)
        ranking = centrality.get("ranking")
        if isinstance(ranking, list):
            for position, entry in enumerate(ranking, start=1):
                name = entry.get("symbol") if isinstance(entry, Mapping) else None
                if isinstance(name, str) and name not in ranks:
                    ranks[name] = position
    except Exception:
        degraded.add(_SIGNAL_CENTRALITY)

    provisional: list[ChangedSymbol] = []
    for symbol in symbols:
        badge = counts.get(symbol.symbol_name)
        badge_map: Mapping[str, Any] = badge if isinstance(badge, Mapping) else {}
        # The call graph stores callers by qualified name and callees by bare
        # name, so both spellings are legitimate keys for the same symbol.
        rank = ranks.get(symbol.qualified_name or "") or ranks.get(symbol.symbol_name)
        provisional.append(
            replace(
                symbol,
                caller_count=_as_int(badge_map.get("callers"), -1),
                usage_count=_as_int(badge_map.get("usages"), -1),
                centrality_rank=rank,
            )
        )

    resolution_order = _resolution_order(provisional)
    selected_names = set(resolution_order[:_MAX_RESOLVE_QUERIES])
    if len(resolution_order) > _MAX_RESOLVE_QUERIES:
        degraded.add(_SIGNAL_RESOLUTION_TRUNCATED)
    resolutions = _resolve_names(engine, list(resolution_order[:_MAX_RESOLVE_QUERIES]))

    out: list[ChangedSymbol] = []
    for symbol in provisional:
        if symbol.symbol_name not in selected_names and (
            symbol.caller_count > 0 or symbol.usage_count > 0 or symbol.centrality_rank is not None
        ):
            out.append(
                replace(
                    symbol,
                    caller_count=-1,
                    usage_count=-1,
                    centrality_rank=None,
                    centrality_percentile=None,
                )
            )
            continue
        if _attributable(resolutions.get(symbol.symbol_name), symbol.file_path, symbol.symbol_name):
            out.append(replace(symbol, centrality_percentile=_percentile(symbol.centrality_rank, node_count)))
            continue
        if symbol.caller_count > 0 or symbol.usage_count > 0 or symbol.centrality_rank is not None:
            degraded.add(_SIGNAL_AMBIGUOUS)
        # "The index never answered" (-1) and "the index answered but the answer
        # is not this symbol's" (0) are different facts and stay distinguishable.
        blank = -1 if symbol.caller_count < 0 else 0
        out.append(
            replace(
                symbol,
                caller_count=blank,
                usage_count=blank,
                centrality_rank=None,
                centrality_percentile=None,
            )
        )
    return out, degraded, resolutions


def _untouched_caller_sites(
    engine: Any,
    symbols: Sequence[ChangedSymbol],
    changed_paths: frozenset[str],
    *,
    limit: int,
    resolutions: Mapping[str, _Resolution] | None = None,
    exists_in_head: Callable[[str, str, str], int | None] | None = None,
    is_foreign: Callable[[str], bool] | None = None,
) -> tuple[list[ImpactSite], set[str]]:
    """Name the call sites of changed symbols that live outside the patch, and what that cost.

    This is the one impact class the call graph can answer directly, so it is the
    only place a graph traversal is worth its cost -- and it is capped twice (how
    many symbols get expanded, and how many callers each may return) so a change
    to a very central symbol cannot turn the packet into a fan-out dump.

    Callers are held to the changed symbol's own file extension. ``tool_callers``
    falls back to a reference lookup that folds in cross-language usages, and the
    index keeps one flat symbol namespace, so without this a workflow YAML key
    named ``release`` reports every ``release`` in the TypeScript tree as a broken
    caller. Cross-language reach is out of scope here by design; what little of it
    is real arrives through the text/ast-grep detectors instead.

    Two further guards exist because the index resolves call edges by bare name.
    Measured on this repository: a change to ``static_frontend.serve`` reported
    fourteen "untouched callers" -- every ``main`` and ``serve`` in the tree,
    including TypeScript ones -- because the engine merged twenty same-named
    definitions into one target. So a symbol is expanded only when the engine
    resolved it to a *single* definition, and only when that definition is the one
    in the changed file. Both are under-claiming by design: a fabricated caller
    costs a reviewer far more than a missing one.

    A third guard exists because those two believe the index about its own
    ambiguity, and it is not always right: ``_chip`` resolves to one target with
    no ``ambiguity`` block and still returns three callers belonging to the
    other three ``_chip``s in the tree. A leading underscore is Python's
    module-private marker, so a caller outside the defining package is not a
    caller of *this* definition, whatever the index says (see
    :func:`_out_of_private_scope`).

    ``resolutions`` is ``_enrich``'s cache of those same answers. It is optional
    so this stays callable on its own, but on the real path it means the two
    passes share one query per name rather than issuing two.

    ``exists_in_head`` is the fourth guard and the only one that is not about
    ambiguity. The index is keyed to the workspace on disk, not to the revision
    under review, so reviewing an older commit cited call sites in files that
    commit never contained -- a location the reviewer opens and does not find.
    It is asked ``(path, symbol)`` rather than ``(path)``: a file surviving into
    the reviewed revision proves nothing about whether that revision's copy of it
    named this symbol, and "the file is there but the call is not" sends the
    reviewer to the same dead end as "the file is not there". ``None`` disables
    the check for a caller that has no revision to check against.

    It also answers *where*, and the returned ``degraded`` set carries
    ``caller_line_ranges`` whenever that answer differed from the index's --
    which is to say, whenever the index's caller geometry did not describe the
    reviewed revision. ``_outline_drifted`` cannot see this: it only ever walks
    the files the diff touched, while these sites live in files the diff never
    names. Without the signal the footer printed an unqualified ``index: fresh``
    over geometry nothing had checked.

    ``is_foreign`` is the fifth and last guard, and the only one about *which
    repository* a caller lives in: see :func:`_foreign_path_filter`.
    """

    if limit <= 0:
        return [], set()
    degraded: set[str] = set()
    cached = resolutions or {}
    candidates = [symbol for symbol in symbols if symbol.caller_count > 0]
    candidates.sort(key=lambda item: (-item.caller_count, item.file_path, item.start_line, item.symbol_name))
    if len(candidates) > _MAX_CALLER_QUERIES:
        degraded.add(_SIGNAL_CALLER_QUERIES_TRUNCATED)

    sites: list[ImpactSite] = []
    seen: set[tuple[str, str]] = set()
    for symbol in candidates[:_MAX_CALLER_QUERIES]:
        resolution = cached.get(symbol.symbol_name) or _resolve_name(engine, symbol.symbol_name)
        if resolution is None:
            continue
        if resolution.merged_targets > _MAX_MERGED_TARGETS:
            continue
        if resolution.definition_path and resolution.definition_path != symbol.file_path:
            continue
        related = resolution.payload.get("related")
        if not isinstance(related, list):
            continue
        for node in related:
            if not isinstance(node, Mapping):
                continue
            # MCP payloads ship field-name-shortened keys (file_path -> path,
            # start_line -> line); accept either spelling.
            path = node.get("path") or node.get("file_path")
            if not isinstance(path, str) or not path or path in changed_paths:
                continue
            if is_foreign is not None and is_foreign(path):
                # Another repository checked out inside this workspace. Its files
                # are a gitlink here, so this change cannot reach them.
                degraded.add(_SIGNAL_FOREIGN_SITES)
                continue
            if Path(path).suffix.lower() != Path(symbol.file_path).suffix.lower():
                continue
            # A module-private name cannot be called from outside its own
            # package, so a "caller" that lives there is a namesake's, silently
            # merged in (`ambiguity` is absent from those payloads).
            if _out_of_private_scope(symbol.symbol_name, symbol.file_path, path):
                continue
            caller = str(node.get("qualified_name") or node.get("name") or "")
            # The index's line is a *workspace* coordinate. Printed beside a path
            # in a reviewed commit range it is a fabrication -- four of these
            # landed past the end of the file the reviewer was reading -- so the
            # revision under review is asked both questions at once: is this call
            # site real there, and where is it. `None` means it is not real
            # there; `0` means real but unlocatable, and then no line is printed
            # at all rather than a borrowed one.
            line = _as_int(node.get("line") or node.get("start_line"), 0)
            if exists_in_head is not None:
                resolved = exists_in_head(path, symbol.symbol_name, caller)
                if resolved is None:
                    continue
                if resolved != line:
                    degraded.add(_SIGNAL_CALLER_LINES)
                line = resolved
            location = f"{path}:L{line}" if line > 0 else path
            key = (symbol.symbol_name, location)
            if key in seen:
                continue
            seen.add(key)
            sites.append(
                ImpactSite(
                    kind="untouched_caller",
                    path=location,
                    old=symbol.symbol_name,
                    # The symbol's own verdict, so the renderer can say what
                    # actually happened to it. Hardcoding "changed" reported a
                    # brand-new definition as a modification of something that
                    # never existed before.
                    new=symbol.change,
                    snippet=caller[:80],
                    in_patch=False,
                    # Exact here, unlike the detector sites: this site was built
                    # from one specific changed symbol.
                    source_path=symbol.file_path,
                )
            )
            if len(sites) >= limit:
                return sites, degraded
    return sites, degraded


def _workspace_text(repo_root: Path, path: str, cache: dict[str, str]) -> str:
    """The on-disk copy of *path*, or ``""`` when it cannot be read.

    This is the text the detectors and the index were built from, so it is the
    coordinate system every detector line belongs to. Read once per file and
    capped like every other blob this package loads.
    """

    if path not in cache:
        text = ""
        try:
            target = repo_root / path
            if target.is_file() and target.stat().st_size <= MAX_BLOB_BYTES:
                text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        cache[path] = text
    return cache[path]


def _sites_in_reviewed_revision(
    repo_root: Path,
    sites: Sequence[ImpactSite],
    exists_in_head: Callable[[str, str, str], int | None] | None,
) -> tuple[list[ImpactSite], int, int]:
    """Hold detector sites to the revision under review: file, and coordinates.

    Returns the surviving sites, how many were dropped outright, and how many
    kept their file but lost their line.

    The detectors read the workspace on disk and the index built from it, not
    the revision the reviewer asked about. Reviewing a commit a few revisions
    back therefore cited files added *since*: ``app/late.py:L1`` under a symbol
    removed two commits ago, in a file that commit had never heard of, and a
    footer reading ``index: fresh`` over it. :func:`_untouched_caller_sites`
    has asked this question since it learned the same lesson; the detector pass
    never did.

    Surviving the file check is not enough, because the two readings disagree
    about *line numbers* long before they disagree about files. Fifty lines
    prepended to a consumer since the reviewed commit sent the reviewer to
    ``app/user.py:L51`` in a file five lines long there -- past its end, and no
    less fabricated than citing a file that does not exist. So the revision is
    asked *where* it puts the site's token, and the workspace is asked the same
    question with the same rule: two answers that differ prove the detector's
    line is a coordinate in the wrong file revision, and a coordinate that
    cannot be trusted is not printed. The site is still cited, as a bare path --
    the form :func:`_untouched_caller_sites` already uses for "real, but nothing
    here knows where".

    Deliberately not re-anchored to the revision's own answer: that answer is
    the token's first *mention*, which in a consumer file is the ``import``
    line, and moving the site there would claim the import is the use. Silence
    about the line is the honest answer, so it is the one given -- and only on
    proof of disagreement. An answer of ``0`` (unreadable blob, or a token no
    line-locating rule applies to, such as a contract literal) proves nothing
    either way and leaves the detector's line alone.

    The verdict is per file rather than per site: a file whose numbering has
    moved has moved for every site in it, including the ones whose token cannot
    be located.
    """

    if exists_in_head is None:
        return list(sites), 0, 0
    workspace: dict[str, str] = {}
    survivors: list[tuple[ImpactSite, str]] = []
    moved: set[str] = set()
    dropped = 0
    for site in sites:
        path = _impact_site_file(site.path)
        if path:
            token = _site_token(site.kind, site.old)
            located = exists_in_head(path, token, token)
            if located is None:
                dropped += 1
                continue
            if located > 0 and path not in moved:
                on_disk = _anchor_line(_workspace_text(repo_root, path, workspace), token)
                if on_disk > 0 and on_disk != located:
                    moved.add(path)
        survivors.append((site, path))

    kept: list[ImpactSite] = []
    unanchored = 0
    for site, path in survivors:
        if path in moved and site.path != path:
            kept.append(replace(site, path=path))
            unanchored += 1
            continue
        kept.append(site)
    return kept, dropped, unanchored


def _caller_reserve(limit: int) -> int:
    """Slots held for the untouched-caller pass out of a budget of *limit*.

    A flat ``min(_CALLER_SITE_RESERVE, limit)`` inverted the priority the
    kind-ordered sort encodes: at ``lc review --limit 5`` the reserve swallowed
    the whole budget and the weakest site kind evicted every ``removed_symbol``
    and ``signature_change``. A quarter of the budget keeps the caller pass
    represented at the default limit of 40 (still 8) without letting it own a
    small one.
    """

    return max(0, min(_CALLER_SITE_RESERVE, limit // 4))


def _cap_sites(sites: Sequence[ImpactSite], limit: int) -> tuple[list[ImpactSite], int]:
    """Trim to *limit*, holding :func:`_caller_reserve` slots for callers.

    *sites* arrives ordered by kind, and ``untouched_caller`` sorts after every
    detector kind, so a plain head slice deleted the class whole whenever the
    detectors alone filled the budget. A cap that drops findings is a fact
    about the packet, so the count comes back with the list and the caller
    names it in ``degraded``: silence here reported the cap as the total.
    """

    if len(sites) <= limit:
        return list(sites), 0
    chosen: set[int] = set()
    reserve = _caller_reserve(limit)
    for index, site in enumerate(sites):
        if len(chosen) >= reserve:
            break
        if site.kind == "untouched_caller":
            chosen.add(index)
    for index in range(len(sites)):
        if len(chosen) >= limit:
            break
        chosen.add(index)
    return [site for index, site in enumerate(sites) if index in chosen], len(sites) - len(chosen)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def collect_changed_symbols(
    repo_root: Path,
    files: Sequence[ChangedFile],
    *,
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
) -> ImpactResult:
    """Return the stable changed-symbol projection without expensive fan-out work.

    Review startup needs symbol identity to establish its target denominator, but
    it does not need detector sites, caller expansion, centrality, or impact
    ranking before the human can read source. This uses the same index-backed
    symbol extraction as :func:`collect_impact`; the later enrichment therefore
    adds evidence to those symbols rather than inventing a second identity set.
    """

    text_files = [item for item in files if not item.is_binary]
    if not text_files:
        return ImpactResult()
    engine = _open_engine(repo_root)
    ready = _index_ready(engine)
    symbols, drifted, degraded = _changed_symbols(
        engine,
        text_files,
        ready=ready,
        old_blobs=old_blobs,
        new_blobs=new_blobs,
    )
    index_status: IndexStatus = "absent"
    if ready:
        index_status = "stale" if drifted else "fresh"
        if drifted:
            degraded.add(_SIGNAL_LINE_RANGES)
    return ImpactResult(
        symbols=tuple(symbols),
        sites=(),
        index_status=index_status,
        degraded=tuple(sorted(degraded)),
    )


def collect_impact(
    repo_root: Path,
    files: Sequence[ChangedFile],
    *,
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
    limit_sites: int = 40,
    exists_in_head: Callable[[str, str, str], int | None] | None = None,
    reuse: ImpactReuse | None = None,
) -> ImpactResult:
    """Return semantic impact, optionally reusing proven-stable detector work.

    The reusable portion is intentionally narrow. Deterministic detector sites
    may be carried only for source edits whose old/new bytes are unchanged and
    only while the current patch has not *lost* a touched path. If a path leaves
    the patch it becomes eligible as an outside consumer again, so every
    detector is rerun. Symbol extraction, graph enrichment, caller sites and
    ordering remain fresh on every revision.
    """

    text_files = [item for item in files if not item.is_binary]
    if not text_files:
        return ImpactResult()

    current_paths = frozenset(item.path for item in files)
    detector_files = text_files
    reused_detector_sites: list[ImpactSite] = []
    carried_degraded: set[str] = set()
    if reuse is not None and reuse.previous_touched_paths and reuse.previous_touched_paths <= current_paths:
        reusable = reuse.reusable_source_paths & current_paths
        # A site with no source attribution cannot be tied to one stable edit;
        # carrying it would be a guess, so fall back to a full detector pass.
        if reusable and all(site.source_path for site in reuse.detector_sites):
            detector_files = [item for item in text_files if item.path not in reusable]
            reused_detector_sites = [
                site
                for site in reuse.detector_sites
                if site.source_path in reusable and _impact_site_file(site.path) not in current_paths
            ]
            carried_degraded.update(reuse.carried_degraded)

    degraded: set[str] = set(carried_degraded)
    engine = _open_engine(repo_root)
    ready = _index_ready(engine)
    if not ready:
        degraded.update({_SIGNAL_RELATIONS, _SIGNAL_CENTRALITY})

    is_foreign = _foreign_path_filter(repo_root)
    raw_sites: list[ImpactSite] = []
    detector_degraded: set[str] = set()
    if detector_files:
        raw_sites, detector_degraded = _detector_sites(
            repo_root,
            detector_files,
            old_blobs=old_blobs,
            new_blobs=new_blobs,
            engine=engine,
            all_touched_paths=tuple(current_paths),
        )
    degraded.update(detector_degraded)
    filtered_raw = [site for site in raw_sites if not is_foreign(site.path)]
    sites = [*reused_detector_sites, *filtered_raw]
    if len(filtered_raw) != len(raw_sites):
        degraded.add(_SIGNAL_FOREIGN_SITES)
    sites, absent, unanchored = _sites_in_reviewed_revision(repo_root, sites, exists_in_head)
    if absent:
        degraded.add(_SIGNAL_ABSENT_SITES)
    if unanchored:
        degraded.add(_SIGNAL_CALLER_LINES)
    if detector_files and not _astgrep_ready(repo_root):
        degraded.add(_SIGNAL_ASTGREP)

    symbols, drifted, symbol_degraded = _changed_symbols(
        engine,
        text_files,
        ready=ready,
        old_blobs=old_blobs,
        new_blobs=new_blobs,
    )
    degraded.update(symbol_degraded)

    index_status: IndexStatus = "absent"
    if ready:
        index_status = "stale" if drifted else "fresh"
        if drifted:
            degraded.add(_SIGNAL_LINE_RANGES)
        symbols, enrich_degraded, resolutions = _enrich(engine, symbols)
        degraded.update(enrich_degraded)
        caller_sites, caller_degraded = _untouched_caller_sites(
            engine,
            symbols,
            current_paths,
            # A floor, not a remainder: the detectors used to be able to spend
            # the whole budget and leave the caller pass nothing to run with.
            limit=max(limit_sites - len(sites), _caller_reserve(limit_sites)),
            resolutions=resolutions,
            exists_in_head=exists_in_head,
            is_foreign=is_foreign,
        )
        sites.extend(caller_sites)
        degraded.update(caller_degraded)

    unique: list[ImpactSite] = []
    seen: set[tuple[str, str, str]] = set()
    for site in sorted(sites, key=lambda item: (item.kind, item.path, item.old)):
        key = (site.kind, site.path, site.old)
        if key in seen:
            continue
        seen.add(key)
        unique.append(site)

    kept, over_cap = _cap_sites(unique, max(0, limit_sites))
    if over_cap:
        degraded.add(_SIGNAL_SITE_CAP)

    return ImpactResult(
        symbols=tuple(symbols),
        sites=tuple(kept),
        index_status=index_status,
        degraded=tuple(sorted(degraded)),
    )


__all__ = [
    "ImpactResult",
    "ImpactReuse",
    "SymbolWindow",
    "collect_changed_symbols",
    "collect_impact",
    "symbol_windows",
]
