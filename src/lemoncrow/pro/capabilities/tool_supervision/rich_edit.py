"""Main-package extensions for the shared edit engine.

The engine is :mod:`lemoncrow_client.kit.edit`, the same code the thin client
runs. This module supplies what only the main package has -- symbol edits (the
code index), projection edits and minified-view matching (source projections),
fuzzy matching (``diff_match_patch``), reindexing after writes and preserving a
large ``new`` payload -- and keeps :func:`apply_rich_edits` as the main
package's entry point.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.edit import EditExtensions, ProjectionResult, SymbolTarget, apply_edits

from lemoncrow.pro.capabilities.source_projection import (
    MinifiedEditError,
    ProjectionEditError,
    ProjectionMapping,
    apply_compact_projection_edit,
    apply_compact_projection_edits,
    apply_minified_edit,
    language_for_minify,
)

from .fuzzy_match import apply_fuzzy_replace, normalize_for_fuzzy
from .symbol_edit import ResolvedSymbolEdit, SymbolEditError, record_symbol_edit_memory, resolve_symbol_edit

__all__ = ["apply_rich_edits"]

logger = logging.getLogger(__name__)


def _resolve_symbol(edit: dict[str, Any], root: Path) -> SymbolTarget:
    resolved = resolve_symbol_edit(edit, repo_root=root)
    return SymbolTarget(
        scoped_file_path=resolved.scoped_file_path,
        old_string=resolved.old_string,
        new_string=resolved.new_string,
        symbol_id=resolved.symbol_id,
        handle=resolved,
    )


def _record_symbol_edits(targets: list[SymbolTarget]) -> None:
    for target in targets:
        if isinstance(target.handle, ResolvedSymbolEdit):
            record_symbol_edit_memory(target.handle)


def _apply_projection(edit: dict[str, Any], content: str, path: Path) -> ProjectionResult:
    raw_mapping = edit.get("projection_mapping")
    if not isinstance(raw_mapping, dict):
        raise ProjectionEditError(
            "projection_mapping is required for projection edits",
            code="missing_projection_mapping",
            hint="Pass the projection_mapping returned by a compact read with include_meta=true.",
        )
    mapping = ProjectionMapping.from_dict(raw_mapping)
    mapping_path = Path(mapping.path).resolve() if mapping.path else path
    if mapping.path and mapping_path != path:
        raise ProjectionEditError(
            "projection_mapping path does not match file_path",
            code="projection_path_mismatch",
            hint="Use the same file_path that produced the compact projection.",
        )
    projected_ranges = edit.get("projected_ranges")
    if isinstance(projected_ranges, list) and projected_ranges:
        new_content, hunks = apply_compact_projection_edits(
            content,
            mapping=mapping,
            projected_edits=[
                {
                    "projected_start": int(item.get("projected_start", 0)),
                    "projected_end": int(item.get("projected_end", 0)),
                    "new_string": str(item.get("new_string", "")),
                }
                for item in projected_ranges
                if isinstance(item, dict)
            ],
        )
    else:
        if not {"projected_start", "projected_end", "new_string"}.issubset(edit):
            raise ProjectionEditError(
                "projection edit must provide projected_start/projected_end/new_string or projected_ranges",
                code="missing_projection_span",
                hint="Pass a single exact projected span or a non-empty projected_ranges array.",
            )
        new_content, line_start, line_end = apply_compact_projection_edit(
            content,
            mapping=mapping,
            projected_start=int(edit.get("projected_start", 0)),
            projected_end=int(edit.get("projected_end", 0)),
            new_string=str(edit.get("new_string", "")),
        )
        hunks = [(line_start, line_end)]
    return ProjectionResult(content=new_content, hunks=tuple(hunks), projection_kind=mapping.projection_kind)


def _minified_replace(content: str, path: str, old_string: str, new_string: str) -> tuple[str, int, int] | None:
    lang = language_for_minify(path)
    if lang is None:
        return None
    try:
        return apply_minified_edit(content, lang, old_string, new_string, path=path)
    except MinifiedEditError:
        return None


def _fuzzy_replace(scoped: str, old_string: str, new_string: str) -> tuple[str, int, int] | None:
    if not normalize_for_fuzzy(old_string):
        return None
    return apply_fuzzy_replace(scoped, old_string, new_string)


def _reindex(root: Path, paths: list[Path]) -> None:
    # Synchronously reindex every written file so the index version is bumped
    # before the edit response returns; the next explore call then misses its
    # cache and re-queries the fresh index instead of pre-edit results.
    from lemoncrow.pro.capabilities.code_context import CodeContextEngine

    try:
        CodeContextEngine(root, autosync_enabled=False)._reindex_files([str(path) for path in paths])
    except Exception:
        logging.exception("Non-fatal: post-edit reindex failed")


def _preserve_payload(payload: str) -> str | None:
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(payload, tool_name="edit", kind="new-payload")
    return None if record is None else str(record.path)


@functools.cache
def _extensions() -> EditExtensions:
    return EditExtensions(
        resolve_symbol=_resolve_symbol,
        after_symbol_edits=_record_symbol_edits,
        apply_projection=_apply_projection,
        minified_replace=_minified_replace,
        fuzzy_replace=_fuzzy_replace,
        after_write=_reindex,
        preserve_payload=_preserve_payload,
        structured_errors=(SymbolEditError, ProjectionEditError),
    )


def apply_rich_edits(
    edits: Sequence[dict[str, Any]],
    *,
    repo_root: str | Path | None = None,
    atomic: bool = True,
    allowed_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Apply LemonCrow edits with every main-package capability enabled."""
    return apply_edits(
        edits,
        root=Path(repo_root or Path.cwd()),
        allowed_roots=tuple(allowed_roots or ()),
        atomic=atomic,
        extensions=_extensions(),
    )
