"""Tree-sitter based outline extractor for non-Python/TS languages.

Uses ``tree-sitter-language-pack`` (a maintained bundle of tree-sitter grammars
with pre-built wheels) to parse Go / Rust / Java / Ruby / C / C++. For each
language we declare which AST node kinds count as top-level declarations and
which are "containers" whose children's signatures should also be extracted
(e.g. Java class → method signatures).

Returns a plain text outline (signature lines, no bodies) suitable for the
smart_read "outline" payload. Languages not in the config table fall through
to the regex-based generic outline in capability.py.

Adding a new language is editing ``_LANG_CONFIG``:

    "kotlin": LangCfg(
        keep={"package_header", "import_list", "class_declaration", "function_declaration", ...},
        container={"class_declaration", "object_declaration"},
        member={"function_declaration", "property_declaration"},
        body_kinds={"class_body", "function_body"},
    ),
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cache
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LangCfg:
    """Per-language tree-sitter node-kind allowlists."""

    # Top-level node kinds we keep verbatim (entire byte range).
    # Use for short declarations: imports, package, use, type aliases, etc.
    keep_full: frozenset[str] = field(default_factory=frozenset)
    # Top-level node kinds we keep as signature-only (header lines up to body).
    keep_signature: frozenset[str] = field(default_factory=frozenset)
    # Container kinds whose member signatures we also recurse into.
    container: frozenset[str] = field(default_factory=frozenset)
    # Member kinds inside a container that we keep as signature-only.
    member: frozenset[str] = field(default_factory=frozenset)
    # Child node kinds that delimit the start of a body (used to trim signatures).
    body_kinds: frozenset[str] = field(default_factory=frozenset)
    # tree-sitter-language-pack key (defaults to the dict key).
    parser_name: str = ""
    # Transparent wrapper kinds we descend into recursively (no output of their
    # own). Used for grammars where declarations are buried in wrapper nodes.
    unwrap: frozenset[str] = field(default_factory=frozenset)
    # Data key/value kinds we keep as the first source line only (rstripped).
    keep_first_line: frozenset[str] = field(default_factory=frozenset)


_LANG_CONFIG: dict[str, LangCfg] = {
    "go": LangCfg(
        keep_full=frozenset(
            {
                "package_clause",
                "import_declaration",
                "const_declaration",
                "var_declaration",
                "type_declaration",
            }
        ),
        keep_signature=frozenset({"function_declaration", "method_declaration"}),
        body_kinds=frozenset({"block"}),
    ),
    "rust": LangCfg(
        keep_full=frozenset(
            {
                "use_declaration",
                "const_item",
                "static_item",
                "type_item",
                "macro_definition",
            }
        ),
        keep_signature=frozenset(
            {"function_item", "function_signature_item", "struct_item", "enum_item", "foreign_mod_item"}
        ),
        container=frozenset({"impl_item", "trait_item", "mod_item"}),
        member=frozenset({"function_item", "function_signature_item", "const_item"}),
        body_kinds=frozenset({"block", "declaration_list"}),
    ),
    "java": LangCfg(
        keep_full=frozenset({"package_declaration", "import_declaration"}),
        container=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            }
        ),
        member=frozenset(
            {
                "method_declaration",
                "constructor_declaration",
                "field_declaration",
            }
        ),
        body_kinds=frozenset(
            {
                "class_body",
                "interface_body",
                "enum_body",
                "record_body",
                "annotation_type_body",
                "block",
            }
        ),
    ),
    "ruby": LangCfg(
        keep_full=frozenset({"call", "assignment"}),  # require/load + top-level constants
        container=frozenset({"class", "module"}),
        member=frozenset({"method", "singleton_method"}),
        body_kinds=frozenset({"body_statement"}),
    ),
    "c": LangCfg(
        keep_full=frozenset(
            {
                "preproc_include",
                "preproc_def",
                "preproc_function_def",
                "declaration",
                "type_definition",
                "enum_specifier",
            }
        ),
        keep_signature=frozenset({"function_definition"}),
        container=frozenset({"struct_specifier", "union_specifier"}),
        member=frozenset({"field_declaration"}),
        body_kinds=frozenset({"compound_statement", "field_declaration_list"}),
    ),
    "cpp": LangCfg(
        keep_full=frozenset(
            {
                "preproc_include",
                "preproc_def",
                "declaration",
                "type_definition",
                "using_declaration",
                "namespace_alias_definition",
                "alias_declaration",
                "enum_specifier",
            }
        ),
        keep_signature=frozenset({"function_definition", "template_declaration"}),
        container=frozenset(
            {
                "struct_specifier",
                "class_specifier",
                "union_specifier",
                "namespace_definition",
            }
        ),
        member=frozenset({"field_declaration", "function_definition"}),
        body_kinds=frozenset({"compound_statement", "field_declaration_list", "declaration_list"}),
    ),
    "csharp": LangCfg(
        keep_full=frozenset(
            {
                "using_directive",
                "file_scoped_namespace_declaration",
                "global_attribute",
                "global_statement",
            }
        ),
        container=frozenset(
            {
                "namespace_declaration",
                "class_declaration",
                "interface_declaration",
                "struct_declaration",
                "enum_declaration",
                "record_declaration",
                "delegate_declaration",
            }
        ),
        member=frozenset(
            {
                "method_declaration",
                "constructor_declaration",
                "property_declaration",
                "field_declaration",
                "event_declaration",
                "indexer_declaration",
            }
        ),
        body_kinds=frozenset({"declaration_list", "block", "enum_member_declaration_list"}),
    ),
    "kotlin": LangCfg(
        keep_full=frozenset({"package_header", "import_list", "type_alias"}),
        keep_signature=frozenset({"function_declaration"}),
        container=frozenset(
            {
                "class_declaration",
                "object_declaration",
                "companion_object",
            }
        ),
        member=frozenset(
            {
                "function_declaration",
                "property_declaration",
                "secondary_constructor",
                "primary_constructor",
            }
        ),
        body_kinds=frozenset({"class_body", "function_body"}),
    ),
    "php": LangCfg(
        keep_full=frozenset(
            {
                "namespace_definition",
                "namespace_use_declaration",
                "const_declaration",
            }
        ),
        keep_signature=frozenset({"function_definition"}),
        container=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "trait_declaration",
                "enum_declaration",
            }
        ),
        member=frozenset(
            {
                "method_declaration",
                "property_declaration",
                "const_declaration",
            }
        ),
        body_kinds=frozenset({"declaration_list", "compound_statement"}),
    ),
    "swift": LangCfg(
        keep_full=frozenset(
            {
                "import_declaration",
                "typealias_declaration",
            }
        ),
        keep_signature=frozenset({"function_declaration"}),
        container=frozenset(
            {
                "class_declaration",
                "protocol_declaration",
                "struct_declaration",
                "enum_declaration",
                "extension_declaration",
            }
        ),
        member=frozenset(
            {
                "function_declaration",
                "init_declaration",
                "property_declaration",
                "protocol_property_declaration",
            }
        ),
        body_kinds=frozenset({"class_body", "protocol_body", "function_body", "enum_class_body"}),
    ),
    "scala": LangCfg(
        keep_full=frozenset(
            {
                "package_clause",
                "import_declaration",
                "val_definition",
                "var_definition",
                "type_definition",
            }
        ),
        keep_signature=frozenset({"function_definition"}),
        container=frozenset(
            {
                "class_definition",
                "object_definition",
                "trait_definition",
            }
        ),
        member=frozenset(
            {
                "function_definition",
                "val_definition",
                "var_definition",
            }
        ),
        body_kinds=frozenset({"template_body", "block"}),
    ),
    "bash": LangCfg(
        keep_full=frozenset({"variable_assignment", "declaration_command"}),
        keep_signature=frozenset({"function_definition"}),
        body_kinds=frozenset({"compound_statement"}),
    ),
    "toml": LangCfg(
        keep_full=frozenset({"pair"}),
        keep_first_line=frozenset({"table", "table_array_element"}),
    ),
}

# Languages that have a configured outliner. Imported by capability.py to gate
# the tree-sitter branch.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(_LANG_CONFIG.keys())


@cache
def _get_parser(lang: str) -> Any:
    try:
        from tree_sitter_language_pack import get_parser

        cfg = _LANG_CONFIG.get(lang)
        name = (cfg.parser_name if cfg else "") or lang
        return get_parser(name)
    except Exception as exc:
        _logger.warning("tree-sitter parser unavailable for %s: %s", lang, exc)
        return None


def _node_attr(node: Any, name: str) -> Any:
    """Read a tree-sitter node attribute that might be exposed as method or value."""
    val = getattr(node, name)
    return val() if callable(val) else val


def _child_count(node: Any) -> int:
    return int(_node_attr(node, "child_count"))


def _children(node: Any) -> list[Any]:
    n = _child_count(node)
    return [node.child(i) for i in range(n)]


def _byte_range(node: Any) -> tuple[int, int]:
    # The tree-sitter-language-pack binding exposes start_byte/end_byte as
    # methods and offers a ByteRange object that isn't subscriptable, so we
    # ignore byte_range and read the bytes directly via _node_attr.
    return int(_node_attr(node, "start_byte")), int(_node_attr(node, "end_byte"))


def _kind(node: Any) -> str:
    return str(_node_attr(node, "kind") or _node_attr(node, "type") or "")


def _signature_slice(source: bytes, node: Any, body_kinds: frozenset[str]) -> bytes:
    """Extract bytes from node start up to its body, or its end if no body found."""
    start, end = _byte_range(node)
    for child in _children(node):
        if _kind(child) in body_kinds:
            child_start, _ = _byte_range(child)
            return source[start:child_start].rstrip() + b" { ... }"
    return source[start:end].rstrip()


def _extract_member_signatures(container: Any, source: bytes, cfg: LangCfg, indent: str = "    ") -> list[bytes]:
    """Walk into a container and collect signature lines for its members."""
    out: list[bytes] = []
    # Find the body child (class_body, declaration_list, etc.) and iterate its children.
    body_node = None
    for child in _children(container):
        if _kind(child) in cfg.body_kinds:
            body_node = child
            break
    if body_node is None:
        return out
    for child in _children(body_node):
        kind = _kind(child)
        if kind not in cfg.member:
            continue
        sig = _signature_slice(source, child, cfg.body_kinds)
        # Indent each line for readability.
        sig_text = sig.decode("utf-8", errors="replace")
        first_line = sig_text.splitlines()[0] if sig_text else ""
        out.append((indent + first_line).encode("utf-8"))
    return out


def outline_text(language: str, source: str) -> str | None:
    """Build a structural skeleton for a supported language.

    Returns ``None`` when:
      * the language has no config, or
      * the parser is unavailable, or
      * parsing fails for any reason.

    Caller is responsible for falling back to the generic regex outline.
    """
    cfg = _LANG_CONFIG.get(language)
    if cfg is None:
        return None
    parser = _get_parser(language)
    if parser is None:
        return None
    try:
        tree = parser.parse(source)
    except Exception as exc:
        _logger.warning("tree-sitter parse failed for %s: %s", language, exc)
        return None
    source_bytes = source.encode("utf-8")
    root = _node_attr(tree, "root_node")
    pieces: list[bytes] = []

    def visit(node: Any) -> None:
        for child in _children(node):
            kind = _kind(child)
            if kind in cfg.unwrap:
                # Transparent wrapper: descend without emitting anything.
                visit(child)
            elif kind in cfg.keep_full:
                start, end = _byte_range(child)
                pieces.append(source_bytes[start:end].rstrip())
            elif kind in cfg.keep_signature:
                pieces.append(_signature_slice(source_bytes, child, cfg.body_kinds))
            elif kind in cfg.keep_first_line:
                start, end = _byte_range(child)
                text = source_bytes[start:end].decode("utf-8", errors="replace")
                lines = text.splitlines()
                pieces.append(lines[0].rstrip().encode("utf-8") if lines else b"")
            elif kind in cfg.container:
                # Container declaration line + member signatures inside.
                header = _signature_slice(source_bytes, child, cfg.body_kinds)
                pieces.append(header)
                pieces.extend(_extract_member_signatures(child, source_bytes, cfg))
            # else: skip — and crucially do NOT recurse (preserves top-level-only output).

    visit(root)
    if not pieces:
        return None
    return b"\n".join(pieces).decode("utf-8", errors="replace")
