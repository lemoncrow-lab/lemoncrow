"""Symbol tag extraction used by the PageRank repo map."""

from __future__ import annotations

import ast
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from atelier.infra.code_intel.languages import language_for_path

TagKind = Literal["definition", "reference", "call"]
_LEGACY_REGEX_LANGUAGES = frozenset({"javascript", "typescript", "go", "rust"})
_DATA_LANGUAGES = frozenset({"json", "toml", "yaml"})
# Markup / style / prose languages: references are not meaningful code symbols.
_MARKUP_LANGUAGES = frozenset({"html", "css", "markdown"})
_NO_REFERENCE_LANGUAGES = _DATA_LANGUAGES | _MARKUP_LANGUAGES | frozenset({"bash", "sql"})
_IDENTIFIER_KINDS = frozenset(
    {
        "bare_key",
        "constant",
        "dotted_key",
        "field_identifier",
        "id_name",  # CSS id-selector names: #foo → id_name "foo"
        "identifier",
        "name",
        "namespace_identifier",
        "package_identifier",
        "property_identifier",
        "simple_identifier",
        "string_content",
        "type_identifier",
        "variable_name",
        "word",
    }
)


@dataclass(frozen=True)
class Tag:
    name: str
    kind: TagKind
    file: str
    line: int
    byte_range: tuple[int, int]
    # tree-sitter node kind of the definition node (e.g. "function_definition",
    # "struct_specifier"). None for reference tags and non-tree-sitter paths.
    # Downstream symbol extraction maps this to a symbol kind; without it a
    # keyword-less definition (every C function) is unclassifiable from its
    # signature line alone.
    node_kind: str | None = None


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    return offsets


def _line_for_byte(offsets: list[int], byte_offset: int) -> int:
    return max(1, bisect_right(offsets, byte_offset))


def _node_attr(node: Any, name: str) -> Any:
    val = getattr(node, name, None)
    if val is None:
        return None
    return val() if callable(val) else val


def _child_count(node: Any) -> int:
    return int(_node_attr(node, "child_count"))


def _children(node: Any) -> list[Any]:
    return [node.child(index) for index in range(_child_count(node))]


def _kind(node: Any) -> str:
    return str(_node_attr(node, "kind") or _node_attr(node, "type") or "")


def _byte_range(node: Any) -> tuple[int, int]:
    return int(_node_attr(node, "start_byte")), int(_node_attr(node, "end_byte"))


def _node_text(source: bytes, node: Any) -> str:
    start, end = _byte_range(node)
    return source[start:end].decode("utf-8", errors="replace").strip()


def _child_by_field_name(node: Any, field_name: str) -> Any | None:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if child_by_field_name is None:
        return None
    child = child_by_field_name(field_name)
    return child if child is not None else None


def _walk(node: Any) -> list[Any]:
    # Iterative pre-order traversal: a generated/minified or pathologically
    # nested file can exceed Python's recursion limit, and callers in
    # _treesitter_tags do not wrap _walk in a try/except.
    nodes: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        nodes.append(current)
        stack.extend(reversed(_children(current)))
    return nodes


def _first_descendant(node: Any, kinds: frozenset[str]) -> Any | None:
    for candidate in _walk(node):
        if _kind(candidate) in kinds:
            return candidate
    return None


def _last_descendant(node: Any, kinds: frozenset[str]) -> Any | None:
    """Rightmost matching descendant: for `obj->ops->submit(...)` the callee
    identifier is the LAST field_identifier, not the first (`obj`)."""
    last = None
    for candidate in _walk(node):
        if _kind(candidate) in kinds:
            last = candidate
    return last


def _definition_name_node(node: Any, language: str) -> Any | None:
    kind = _kind(node)
    if language == "bash":
        if kind == "function_definition":
            return next((child for child in _children(node) if _kind(child) == "word"), None)
        return _first_descendant(node, frozenset({"variable_name"}))
    if language == "json" and kind == "pair":
        return _first_descendant(_children(node)[0], frozenset({"string_content"})) if _children(node) else None
    if language == "toml" and kind in {"pair", "table", "table_array_element"}:
        return _first_descendant(node, frozenset({"bare_key", "dotted_key"}))
    if language == "yaml" and kind == "block_mapping_pair":
        return _child_by_field_name(node, "key") or (_children(node)[0] if _children(node) else None)
    if language == "sql":
        return _first_descendant(node, frozenset({"identifier"}))
    if language == "lua":
        if kind == "function_declaration":
            for child in _children(node):
                ck = _kind(child)
                if ck in {"dot_index_expression", "method_index_expression"}:
                    # function M.greet() / function Cls:method() — last identifier
                    # is the function name; the first is the table/object.
                    ids = [c for c in _children(child) if _kind(c) == "identifier"]
                    return ids[-1] if ids else None
                if ck == "identifier":
                    # local function foo() or function foo()
                    return child
        return _first_descendant(node, _IDENTIFIER_KINDS)

    for field_name in ("name", "declarator"):
        field_node = _child_by_field_name(node, field_name)
        if field_node is not None:
            identifier = _first_descendant(field_node, _IDENTIFIER_KINDS)
            return identifier or field_node
    return _first_descendant(node, _IDENTIFIER_KINDS)


# Node kinds that open a local (function/method-body) scope, per language.
# Definition-shaped nodes nested inside them are locals -- loop counters,
# temporaries, nested helpers -- not navigable symbols. In C every local
# variable is a `declaration`, indistinguishable by node kind from a
# file-scope global, so scope position is the only signal. Mirrors the Python
# extractor, which indexes module- and class-level definitions only.
# C-family type-specifier kinds that are only definitions when they carry a
# body (field/enumerator list); bodyless occurrences are type REFERENCES
# (`struct foo *x`, `enum bar baz;`).
_BODYLESS_REFERENCE_KINDS = frozenset({"struct_specifier", "union_specifier", "enum_specifier", "class_specifier"})

# Call-expression node kinds per language -> child fields naming the callee
# (tried in order). Emits `call` tags (callee name + call site) from the same
# parse that produces definition/reference tags; the engine folds them into
# call_edges, which feed call-graph PageRank and caller-count popularity.
# Python has its own richer AST path; a language absent here simply produces
# no call tags. Only field-based resolution is used -- guessing the callee
# from bare descendants would pick up argument identifiers.
_CALL_NODE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "c": {"call_expression": ("function",)},
    "cpp": {"call_expression": ("function",)},
    "go": {"call_expression": ("function",)},
    "rust": {"call_expression": ("function",), "macro_invocation": ("macro",)},
    "java": {"method_invocation": ("name",), "object_creation_expression": ("type",)},
    "ruby": {"call": ("method",)},
    "javascript": {"call_expression": ("function",), "new_expression": ("constructor",)},
    "typescript": {"call_expression": ("function",), "new_expression": ("constructor",)},
    "csharp": {"invocation_expression": ("function",), "object_creation_expression": ("type",)},
    "php": {"function_call_expression": ("function",), "member_call_expression": ("name",)},
    "scala": {"call_expression": ("function",)},
    "lua": {"function_call": ("name",)},
}

_LOCAL_SCOPE_KINDS: dict[str, frozenset[str]] = {
    "c": frozenset({"compound_statement"}),
    "cpp": frozenset({"compound_statement"}),
    "go": frozenset({"block"}),
    "java": frozenset({"block"}),
    "rust": frozenset({"block"}),
    "ruby": frozenset({"method", "singleton_method"}),
}


def _definition_candidates(root: Any, language: str, definition_kinds: frozenset[str]) -> list[Any]:
    from atelier.core.capabilities.semantic_file_memory.treesitter_ast import transparent_node_kinds

    if language in {"json", "yaml"}:
        unwrap = transparent_node_kinds(language)
        candidates: list[Any] = []

        def visit(node: Any) -> None:
            for child in _children(node):
                kind = _kind(child)
                if kind in unwrap:
                    visit(child)
                elif kind in definition_kinds:
                    candidates.append(child)

        visit(root)
        return candidates
    local_scopes = _LOCAL_SCOPE_KINDS.get(language)
    if not local_scopes:
        return [node for node in _walk(root) if _kind(node) in definition_kinds]
    # Scope-aware walk: a definition-shaped node nested inside a local scope is
    # skipped. A scope-opening node is itself still a candidate (a ruby `method`
    # is kept; only its body contents are locals).
    scoped: list[Any] = []
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        node, in_local = stack.pop()
        kind = _kind(node)
        if not in_local and kind in definition_kinds:
            scoped.append(node)
        child_local = in_local or kind in local_scopes
        stack.extend((child, child_local) for child in reversed(_children(node)))
    return scoped


def _treesitter_tags(path: Path, text: str, language: str) -> list[Tag] | None:
    from atelier.core.capabilities.semantic_file_memory.treesitter_ast import (
        definition_node_kinds,
        tree_sitter_parser,
    )

    parser = tree_sitter_parser(language)
    if parser is None:
        return None
    try:
        source = text.encode("utf-8")
        try:
            tree = parser.parse(source)
        except TypeError:
            # tree-sitter binding versions disagree on the source type: 0.22+
            # wants a bytestring, some older builds want str. Retry the other.
            tree = parser.parse(text)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return None
    offsets = _line_offsets(text)
    root = _node_attr(tree, "root_node")
    definition_kinds = definition_node_kinds(language)
    tags: list[Tag] = []
    seen: set[tuple[str, TagKind, int, int]] = set()

    for node in _definition_candidates(root, language, definition_kinds):
        # C-family grammars parse the bare type reference in `struct foo *x`
        # as a struct_specifier node too -- only the body-bearing form is a
        # definition. Without this, every parameter/field naming a struct
        # emits a fake definition of that struct at the use site.
        if _kind(node) in _BODYLESS_REFERENCE_KINDS and _child_by_field_name(node, "body") is None:
            continue
        name_node = _definition_name_node(node, language)
        if name_node is None:
            continue
        start, end = _byte_range(name_node)
        name = _node_text(source, name_node)
        if not name:
            continue
        kind: TagKind = "definition"
        key = (name, kind, start, end)
        if key in seen:
            continue
        seen.add(key)
        tags.append(Tag(name, kind, str(path), _line_for_byte(offsets, start), (start, end), node_kind=_kind(node)))

    call_fields = _CALL_NODE_FIELDS.get(language, {})
    if language not in _NO_REFERENCE_LANGUAGES:
        for node in _walk(root):
            node_kind = _kind(node)
            fields = call_fields.get(node_kind)
            if fields:
                callee_node = None
                for field in fields:
                    field_node = _child_by_field_name(node, field)
                    if field_node is not None:
                        callee_node = _last_descendant(field_node, _IDENTIFIER_KINDS) or field_node
                        break
                if callee_node is not None:
                    callee = _node_text(source, callee_node)
                    if callee:
                        start, end = _byte_range(callee_node)
                        call_kind: TagKind = "call"
                        call_key = (callee, call_kind, start, end)
                        if call_key not in seen:
                            seen.add(call_key)
                            tags.append(Tag(callee, call_kind, str(path), _line_for_byte(offsets, start), (start, end)))
            if node_kind not in _IDENTIFIER_KINDS:
                continue
            start, end = _byte_range(node)
            name = _node_text(source, node)
            if not name:
                continue
            kind = "reference"
            key = (name, kind, start, end)
            if key in seen:
                continue
            seen.add(key)
            tags.append(Tag(name, kind, str(path), _line_for_byte(offsets, start), (start, end)))

    return tags


def _python_tags(path: Path, text: str) -> list[Tag]:
    offsets = _line_offsets(text)
    tags: list[Tag] = []
    tree = ast.parse(text)
    # Module-level assignments: index as definitions so constants and type
    # aliases are findable by exact name (e.g. _EXPLORE_ESSENTIAL_KEYS).
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    line = int(target.lineno)
                    tags.append(Tag(target.id, "definition", str(path), line, (offsets[line - 1], offsets[line])))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            line = int(stmt.target.lineno)
            tags.append(Tag(stmt.target.id, "definition", str(path), line, (offsets[line - 1], offsets[line])))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            line = int(getattr(node, "lineno", 1))
            tags.append(Tag(node.name, "definition", str(path), line, (offsets[line - 1], offsets[line])))
        elif isinstance(node, ast.Name):
            line = int(getattr(node, "lineno", 1))
            tags.append(Tag(node.id, "reference", str(path), line, (offsets[line - 1], offsets[line])))
    return tags


def _regex_tags(path: Path, text: str, language: str) -> list[Tag]:
    patterns = {
        "javascript": r"(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)",
        "typescript": r"(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)",
        "go": r"(?:func|type|var|const)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)",
        "rust": r"(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)",
    }
    def_re = re.compile(patterns.get(language, patterns["javascript"]))
    ident_re = re.compile(r"[A-Za-z_][$\w]*")
    tags: list[Tag] = []
    byte_offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        for match in def_re.finditer(line):
            tags.append(
                Tag(
                    match.group(1),
                    "definition",
                    str(path),
                    line_no,
                    (byte_offset + match.start(1), byte_offset + match.end(1)),
                )
            )
        for match in ident_re.finditer(line):
            tags.append(
                Tag(
                    match.group(0),
                    "reference",
                    str(path),
                    line_no,
                    (byte_offset + match.start(0), byte_offset + match.end(0)),
                )
            )
        byte_offset += len(line.encode("utf-8"))
    return tags


def detect_language(path: Path) -> str | None:
    # Delegate to the canonical registry (DLS-LANG-04). Preserves the
    # str | None contract: extract_tags_from_text short-circuits to [] on None.
    lang = language_for_path(path)
    return lang.name if lang is not None else None


def extract_tags_from_text(text: str | bytes, file_path: str | Path, language: str | None = None) -> list[Tag]:
    """Extract definition/reference tags from source text without reading from disk."""
    if isinstance(text, bytes):
        # Defensive: callers occasionally hand us raw bytes despite the str
        # contract (e.g. an un-decoded blob). Normalize once here so every
        # downstream `.encode("utf-8")` call below operates on str, not bytes
        # (bytes has no `.encode`, so that would otherwise raise).
        text = text.decode("utf-8", errors="replace")

    from atelier.core.capabilities.semantic_file_memory.treesitter_ast import (
        supported_tree_sitter_languages,
    )

    path = Path(file_path)
    resolved_language = language or detect_language(path)
    if resolved_language is None:
        return []
    if resolved_language == "python":
        try:
            return _python_tags(path, text)
        except SyntaxError:
            return []
    if resolved_language in supported_tree_sitter_languages():
        tags = _treesitter_tags(path, text, resolved_language)
        if tags is not None:
            return tags
        if resolved_language not in _LEGACY_REGEX_LANGUAGES:
            return []
    return _regex_tags(path, text, resolved_language)


def extract_tags(file_path: str | Path, language: str | None = None) -> list[Tag]:
    """Extract definition/reference tags from a supported source file."""

    path = Path(file_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_tags_from_text(text, path, language=language)


__all__ = ["Tag", "detect_language", "extract_tags", "extract_tags_from_text"]
