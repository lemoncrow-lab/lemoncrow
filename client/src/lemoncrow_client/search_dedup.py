"""Bounded, reversible code-search dedup at the model-facing MCP boundary.

This never caches a search execution or changes its stored response. It only
abbreviates text already emitted by this McpServer, after client hydration.
Hosts with compaction hooks reset the scope; other hosts and independent
callers sharing an MCP connection can always recover with force=true.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .dispatcher import ToolOutcome

_MIN_CHARS = 4096
_MAX_ENTRIES = 128
_MAX_STATE_BYTES = 256 * 1024


def _context_identity(repo_root: Path) -> tuple[str, int] | None:
    """Use the existing host relay; unreadable state disables dedup for this call."""
    path = repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return "", 0
    except OSError:
        return None
    if len(raw) > _MAX_STATE_BYTES:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(state, dict):
        return None
    epoch = state.get("compaction_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        return None
    return str(state.get("session_id") or ""), epoch


class SearchResultDedup:
    """One MCP session's bounded history; initialize/close clear it explicitly."""

    __slots__ = ("_ordinal", "_repo_root", "_scope", "_seen")

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._scope: tuple[object, ...] | None = None
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._ordinal = 0

    def clear(self) -> None:
        self._scope = None
        self._seen.clear()
        self._ordinal = 0

    def compact(
        self,
        outcome: ToolOutcome,
        *,
        arguments: Mapping[str, Any],
        session_id: str,
        view_id: str,
    ) -> ToolOutcome:
        if os.environ.get("LEMONCROW_CONTEXT_DEDUP", "1") == "0":
            self.clear()
            return outcome
        identity = _context_identity(self._repo_root)
        if identity is None:
            self.clear()
            return outcome
        scope = (session_id, view_id, outcome.view_revision, identity)
        if scope != self._scope:
            self.clear()
            self._scope = scope
        if outcome.is_error or outcome.degraded or len(outcome.content) != 1:
            return outcome
        block = outcome.content[0]
        text = block.get("text")
        if block.get("type") != "text" or not isinstance(text, str) or len(text) < _MIN_CHARS:
            return outcome
        # force controls delivery, not search identity. Preserve distinctions
        # such as query, scope and limit even when their rendered text matches.
        try:
            request = json.dumps(
                {key: value for key, value in arguments.items() if key != "force"},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, RecursionError):
            return outcome
        digest = hashlib.sha256((request + "\0" + text).encode("utf-8", "surrogatepass")).hexdigest()
        previous = self._seen.pop(digest, None)
        if previous is None:
            self._ordinal += 1
        self._seen[digest] = self._ordinal if previous is None else previous
        if len(self._seen) > _MAX_ENTRIES:
            self._seen.popitem(last=False)
        if previous is None or arguments.get("force"):
            return outcome
        marker = (
            f"[dedup] code_search unchanged from search #{previous} ({len(text)} chars); "
            "rerun with force=true for full source."
        )
        return replace(outcome, content=({**block, "text": marker},))
