"""Pure content-addressed structural analysis for Layer 1.

This module deliberately knows nothing about repositories, Views or files on
 disk. A caller supplies bytes plus a language and receives versioned facts.
Tree-sitter (and Python's AST-backed tag path) is reused from LemonCrow's mature
repo-map extractor, but the result is normalized into the server index's stable
content-local contracts.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Final

from lemoncrow.infra.tree_sitter.tags import extract_tags_from_text

from .contracts import SymbolDef, SymbolRef

__all__ = ["STRUCTURAL_ANALYZER_VERSION", "StructuralFacts", "analyze_structural"]

STRUCTURAL_ANALYZER_VERSION: Final[str] = "tree-sitter-1"
_MAX_DEFINITIONS: Final[int] = 2048
_MAX_REFERENCES: Final[int] = 4096
_EXTENSION: Final[dict[str, str]] = {
    "python": ".py",
    "typescript": ".ts",
    "go": ".go",
    "rust": ".rs",
}


@dataclass(frozen=True, slots=True)
class StructuralFacts:
    """Versioned, path-independent facts derived from one immutable blob."""

    analyzer_version: str
    language: str
    definitions: tuple[SymbolDef, ...]
    references: tuple[SymbolRef, ...]


def _kind(node_kind: str | None) -> str:
    raw = (node_kind or "").lower()
    if "impl" in raw:
        return ""
    if "method" in raw:
        return "method"
    if "function" in raw or raw in {"function_item", "function_declaration"}:
        return "function"
    if "class" in raw:
        return "class"
    if "interface" in raw:
        return "interface"
    if "struct" in raw:
        return "struct"
    if "enum" in raw:
        return "enum"
    if "trait" in raw:
        return "trait"
    if "type" in raw:
        return "type"
    return "binding"


def _line_text(text: str, line: int) -> str:
    rows = text.splitlines()
    return rows[line - 1] if 0 < line <= len(rows) else ""


def _exported(language: str, name: str, line: str) -> bool:
    if language == "go":
        return bool(name[:1].isupper())
    if language == "rust":
        return re.search(r"\bpub(?:\([^)]*\))?\s+", line) is not None
    if language == "typescript":
        return "export " in line or "export default " in line
    return not name.startswith("_")


def _python_facts(data: bytes, text: str) -> StructuralFacts:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return StructuralFacts(STRUCTURAL_ANALYZER_VERSION, "python", (), ())
    encoded_lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in encoded_lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))

    definitions: dict[tuple[int, int, str], SymbolDef] = {}
    definition_nodes: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            definition_nodes.add(node)
            line = int(node.lineno)
            raw_line = encoded_lines[line - 1] if 0 < line <= len(encoded_lines) else ""
            name_at = raw_line.find(node.name)
            start = offsets[line - 1] + len(raw_line[: max(0, name_at)].encode("utf-8"))
            end = start + len(node.name.encode("utf-8"))
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else (
                    "method"
                    if any(node in parent.body for parent in ast.walk(tree) if isinstance(parent, ast.ClassDef))
                    else "function"
                )
            )
            definitions[(start, end, node.name)] = SymbolDef(
                node.name, kind, line, start, end, not node.name.startswith("_")
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node in getattr(tree, "body", ()):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                line = int(target.lineno)
                start = offsets[line - 1] + int(target.col_offset)
                end = start + len(target.id.encode("utf-8"))
                definitions[(start, end, target.id)] = SymbolDef(
                    target.id, "binding", line, start, end, not target.id.startswith("_")
                )

    call_names: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name):
                call_names.add((callee.id, int(callee.lineno)))
            elif isinstance(callee, ast.Attribute):
                call_names.add((callee.attr, int(callee.lineno)))
    references: dict[tuple[str, int, bool], SymbolRef] = {}
    defined_names = {(item.name, item.line) for item in definitions.values()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            key = (node.id, int(node.lineno))
            if key in defined_names:
                continue
            is_call = key in call_names
            references[(node.id, key[1], is_call)] = SymbolRef(node.id, key[1], is_call)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            key = (node.attr, int(node.lineno))
            is_call = key in call_names
            references[(node.attr, key[1], is_call)] = SymbolRef(node.attr, key[1], is_call)
    return StructuralFacts(
        STRUCTURAL_ANALYZER_VERSION,
        "python",
        tuple(definitions[key] for key in sorted(definitions)[:_MAX_DEFINITIONS]),
        tuple(references[key] for key in sorted(references)[:_MAX_REFERENCES]),
    )


def analyze_structural(data: bytes, language: str) -> StructuralFacts | None:
    """Analyze one blob without repository/worktree state.

    ``None`` means this analyzer version does not support the language; callers
    retain the legacy parser for old profiles and unsupported content.
    """
    extension = _EXTENSION.get(language)
    if extension is None:
        return None
    text = data.decode("utf-8", errors="replace")
    if language == "python":
        return _python_facts(data, text)
    tags = extract_tags_from_text(text, f"content{extension}", language=language)
    definitions: dict[tuple[int, int, str], SymbolDef] = {}
    references: dict[tuple[str, int, bool], SymbolRef] = {}
    for tag in tags:
        if tag.kind == "definition" and len(definitions) < _MAX_DEFINITIONS:
            kind = _kind(tag.node_kind)
            if not kind:
                continue
            start, end = tag.byte_range
            key = (start, end, tag.name)
            definitions[key] = SymbolDef(
                name=tag.name,
                kind=kind,
                line=tag.line,
                start=start,
                end=end,
                exported=_exported(language, tag.name, _line_text(text, tag.line)),
            )
        elif tag.kind in {"reference", "call"} and len(references) < _MAX_REFERENCES:
            key = (tag.name, tag.line, tag.kind == "call")
            references[key] = SymbolRef(name=tag.name, line=tag.line, is_call=tag.kind == "call")
    defined_sites = {(item.name, item.line) for item in definitions.values()}
    call_sites = {(name, line) for name, line, is_call in references if is_call}
    filtered = {
        key: item
        for key, item in references.items()
        if (item.name, item.line) not in defined_sites and (item.is_call or (item.name, item.line) not in call_sites)
    }
    return StructuralFacts(
        analyzer_version=STRUCTURAL_ANALYZER_VERSION,
        language=language,
        definitions=tuple(definitions[key] for key in sorted(definitions)),
        references=tuple(filtered[key] for key in sorted(filtered)),
    )
