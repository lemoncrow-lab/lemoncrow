"""Transport-independent policy helpers for LemonCrow's read tool."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow.gateway.tools.semantic_memory import semantic_file_memory

DEFAULT_READ_INLINE_BUDGET_BYTES = 40 * 1024
DEFAULT_READ_BATCH_BUDGET_BYTES = 24 * 1024
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024
SUMMARY_TARGET_CHARS = 4096

ARCHIVE_SUFFIXES = frozenset({".gz", ".bz2", ".xz", ".zst", ".tgz", ".tbz2"})
ARCHIVE_MEDIA_TYPES = frozenset(
    {
        "application/zip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/vnd.rar",
        "application/x-rar-compressed",
    }
)
READ_SUGGEST_PRUNE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".next",
        "target",
    }
)


def binary_read_message(media_type: str, size_bytes: int, suffix: str = "") -> str:
    """Return actionable guidance when read encounters non-text binary content."""
    base = f"Binary file ({media_type}, {size_bytes} bytes) -- not UTF-8 text, not decoded."
    if media_type.startswith("image/"):
        return base + f" Image too large to inline (> {MAX_INLINE_IMAGE_BYTES} bytes)."
    if media_type.startswith("video/"):
        return (
            base + " Video not viewable. Extract a frame -- "
            "`ffmpeg -ss <time> -i <file> -frames:v 1 frame.png` -- then read() it."
        )
    if media_type.startswith("audio/"):
        return base + " Audio is not transcribed by this tool; no transcript path is available here."
    if media_type == "application/pdf":
        return base + " PDF not extracted. `pdftotext` for text, or `pdftoppm -png` a page and read() the PNG."
    if media_type in ARCHIVE_MEDIA_TYPES or suffix in ARCHIVE_SUFFIXES:
        return (
            base + " Archive contents are not listed by this tool; use bash (e.g. `unzip -l`, `tar -tf`) to inspect it."
        )
    return base


def suggest_paths_for_missing(workspace_root: Path, missing: str, *, limit: int = 3) -> list[str]:
    """Return workspace-relative paths whose basename matches a missing path."""
    name = Path(missing).name
    if not name:
        return []
    lowered = name.lower()
    hits: list[str] = []
    scanned = 0
    deadline = time.monotonic() + 0.25
    try:
        for dirpath, dirnames, filenames in os.walk(workspace_root):
            dirnames[:] = [directory for directory in dirnames if directory not in READ_SUGGEST_PRUNE_DIRS]
            scanned += len(filenames)
            for filename in filenames:
                if filename.lower() == lowered:
                    with contextlib.suppress(ValueError):
                        hits.append(str((Path(dirpath) / filename).relative_to(workspace_root)))
                    if len(hits) >= limit:
                        return hits
            if scanned > 20_000 or time.monotonic() > deadline:
                break
    except OSError:
        return hits
    return hits


def read_summary_response(
    resolved: Path,
    *,
    semantic_memory_root: Path,
    render_outline: Callable[[str, dict[str, Any], str], str],
) -> dict[str, Any]:
    """Build the bounded ``:summary`` response for one file."""
    from lemoncrow_client.kit.notices import spill_notice

    from lemoncrow.pro.capabilities.semantic_file_memory.capability import _read_source_bounded
    from lemoncrow.pro.capabilities.source_projection import SourceProjection
    from lemoncrow.pro.capabilities.tool_supervision.text_summary import heuristic_summary, llm_summary_tier

    source_text, _truncated = _read_source_bounded(resolved)
    original_chars = len(source_text)

    body = ""
    verb = ""
    tier = llm_summary_tier(source_text, target_chars=SUMMARY_TARGET_CHARS)
    if tier is not None:
        body, verb = tier

    if not body:
        capability = semantic_file_memory(semantic_memory_root)
        outline_payload = capability.smart_read(resolved, expand=False, outline_threshold=0)
        if outline_payload.get("mode") == "outline":
            language = str(outline_payload.get("language") or "")
            rendered = render_outline(str(resolved), dict(outline_payload.get("outline") or {}), language)
            if len(rendered) <= SUMMARY_TARGET_CHARS:
                body = rendered
                verb = "summarized:outline"
        if not body:
            body = heuristic_summary(source_text, path=resolved, target_chars=SUMMARY_TARGET_CHARS)
            verb = "summarized:heuristic"

    footer = spill_notice(
        verb=verb,
        original_chars=original_chars,
        kept_chars=len(body),
        path=resolved,
    )
    return {
        "mode": "summary",
        "summary": f"{body}\n\n{footer}",
        "path": str(resolved),
        "projection": SourceProjection.summary().to_dict(),
    }


def read_inline_budget_bytes() -> int:
    """Return the per-file inline read budget; zero disables the cap."""
    raw = os.environ.get("LEMONCROW_READ_INLINE_BUDGET_BYTES", str(DEFAULT_READ_INLINE_BUDGET_BYTES))
    try:
        configured = int(raw)
    except ValueError:
        return DEFAULT_READ_INLINE_BUDGET_BYTES
    if configured <= 0:
        return 0
    return max(8 * 1024, configured)


def read_batch_budget_bytes() -> int:
    """Return the aggregate batch read budget; zero disables downgrade."""
    raw = os.environ.get("LEMONCROW_READ_BATCH_BUDGET_BYTES", str(DEFAULT_READ_BATCH_BUDGET_BYTES))
    try:
        configured = int(raw)
    except ValueError:
        return DEFAULT_READ_BATCH_BUDGET_BYTES
    if configured <= 0:
        return 0
    return max(8 * 1024, configured)


def batch_entry_bytes(entry: object) -> int:
    """Serialized size of one read-batch entry."""
    try:
        return len(json.dumps(entry, default=str))
    except (TypeError, ValueError):
        return 0


def apply_batch_read_budget(
    results: list[dict[str, Any]],
    entry_specs: list[str | None],
    include_meta: bool,
    *,
    outline_reader: Callable[..., dict[str, Any]],
) -> list[str]:
    """Downgrade largest whole-file batch entries until the batch fits its budget."""
    budget = read_batch_budget_bytes()
    if budget <= 0:
        return []
    total = sum(batch_entry_bytes(entry) for entry in results)
    if total <= budget:
        return []
    candidates = sorted(
        (
            idx
            for idx, spec_path in enumerate(entry_specs)
            if spec_path is not None and isinstance(results[idx], dict) and not results[idx].get("error")
        ),
        key=lambda idx: batch_entry_bytes(results[idx]),
        reverse=True,
    )
    downgraded: list[str] = []
    for idx in candidates:
        if total <= budget:
            break
        spec_path = entry_specs[idx]
        if spec_path is None:
            continue
        before = batch_entry_bytes(results[idx])
        try:
            outlined = outline_reader(path=spec_path, outline=True, include_meta=include_meta)
        except Exception:
            continue
        after = batch_entry_bytes(outlined)
        if after >= before:
            continue
        results[idx] = outlined
        total -= before - after
        downgraded.append(spec_path)
    return downgraded


__all__ = [
    "ARCHIVE_MEDIA_TYPES",
    "ARCHIVE_SUFFIXES",
    "DEFAULT_READ_BATCH_BUDGET_BYTES",
    "DEFAULT_READ_INLINE_BUDGET_BYTES",
    "MAX_INLINE_IMAGE_BYTES",
    "READ_SUGGEST_PRUNE_DIRS",
    "SUMMARY_TARGET_CHARS",
    "apply_batch_read_budget",
    "batch_entry_bytes",
    "binary_read_message",
    "read_batch_budget_bytes",
    "read_inline_budget_bytes",
    "read_summary_response",
    "suggest_paths_for_missing",
]
