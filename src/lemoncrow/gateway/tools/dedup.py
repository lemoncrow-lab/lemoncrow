"""Shared within-session context deduplication for tool results."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Any

from lemoncrow_client.kit.read_path import split_file_opts

MCP_DEDUP_TOOLS = frozenset({"read", "code_search"})
CLI_DEDUP_TOOLS = frozenset({"read", "search", "grep"})


@dataclass(frozen=True, slots=True)
class DedupResult:
    text: str
    stubbed: bool = False
    chars_saved: int = 0


def read_dedup_resource(args: dict[str, Any]) -> str:
    """Stable delta-dedup resource key for one read projection."""
    files = args.get("files")
    if files is not None:
        if not isinstance(files, list) or len(files) != 1:
            return ""
        entry = files[0]
        if isinstance(entry, str):
            path, line_range, expand, head, tail, _summary, _outline = split_file_opts(entry)
            range_spec = line_range or ""
            lines_spec = "" if head is None else str(head)
            tail_spec = "" if tail is None else str(tail)
            projection_spec = ""
        elif isinstance(entry, dict):
            path = str(entry.get("path") or "")
            range_spec = str(entry.get("range") or "")
            expand = bool(entry.get("full"))
            lines = entry.get("lines", entry.get("max_lines"))
            lines_spec = "" if lines is None else str(lines)
            tail_spec = "" if entry.get("tail") is None else str(entry.get("tail"))
            projection_spec = str(entry.get("projection_kind") or "")
        else:
            return ""
        if not path:
            return ""
        return f"read:{path}:{range_spec}:{lines_spec}:{tail_spec}:{projection_spec}:{int(bool(expand))}"

    path = str(args.get("path") or "")
    if not path:
        return ""
    range_spec = str(args.get("range") or "")
    max_lines = args.get("lines", args.get("max_lines"))
    max_lines_spec = "" if max_lines is None else str(max_lines)
    projection_spec = str(args.get("projection_kind") or "")
    return f"read:{path}:{range_spec}:{max_lines_spec}::{projection_spec}:{int(bool(args.get('full')))}"


def dedup_output(
    *,
    name: str,
    args: dict[str, Any],
    text: str,
    session_id: str,
    eligible_tools: frozenset[str],
    salt: str = "",
    resource: str = "",
    require_session_id: bool = False,
) -> DedupResult:
    """Return a repeated-content stub/delta while preserving caller-specific policy."""
    if name not in eligible_tools or os.environ.get("LEMONCROW_CONTEXT_DEDUP", "1") == "0":
        return DedupResult(text)
    if require_session_id and not session_id:
        return DedupResult(text)

    with contextlib.suppress(Exception):
        from lemoncrow.pro.capabilities import context_dedup

        outcome = context_dedup.registry().stub_for(
            session_id=session_id,
            content=text,
            epoch=context_dedup.current_epoch(),
            force=bool(args.get("force")),
            salt=salt,
        )
        if outcome is None and name == "read" and resource:
            outcome = context_dedup.registry().delta_for(
                session_id=session_id,
                resource=resource,
                content=text,
                epoch=context_dedup.current_epoch(),
                force=bool(args.get("force")),
            )
        if outcome is not None:
            stub_text, chars_saved = outcome
            return DedupResult(stub_text, stubbed=True, chars_saved=max(0, int(chars_saved)))
    return DedupResult(text)


__all__ = [
    "CLI_DEDUP_TOOLS",
    "MCP_DEDUP_TOOLS",
    "DedupResult",
    "dedup_output",
    "read_dedup_resource",
]
