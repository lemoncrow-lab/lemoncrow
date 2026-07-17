"""Honest per-file cap on the "avoided full read" saving.

Both outline and range reads compute ``tokens_saved`` against the FULL-file
baseline (what the host's built-in Read would have emitted). Across multiple
reads of the SAME file in one session that double-counts: you can only avoid
reading a file once. This module records which files have already been credited
for baseline avoidance and signals callers to zero the saving on the 2nd+
outline/range read of the same file.

The credited set is SHARED with the code-intel counterfactual credit
(``mcp_server._finish_code_result``'s "vanilla would have grepped + Read each
surfaced file" arm, netted via :func:`should_credit_path`): both bill "the
model avoided ingesting this file's content once", so a file surfaced by
``code_search`` and later outline-read — or surfaced twice by two code calls
— draws on one credit, not two.

Full-mode reads are deliberately untouched -- their saving is *minification*
(byte reduction of the content actually delivered), not baseline avoidance, so
it never double-counts.

Every function is a PURE, total transform over a plain session-state dict: no
I/O, no exceptions raised to callers, tolerant of missing keys and wrong types.
The caller owns persistence, the kill switch, and the per-session epoch reset.
"""

from __future__ import annotations

import os
from typing import Any

# Modes whose ``tokens_saved`` is measured against the full-file baseline and so
# may be credited at most once per file per session.
_BASELINE_MODES = frozenset({"outline", "range"})
_CREDITED_KEY = "read_baseline_credited"
# Hard cap on the credited set so the per-read hot path (which rewrites this
# list into session_state.json every read) can't grow unbounded between
# compactions. Mirrors the bounded ``context_dedup`` sibling. At the cap we
# evict oldest (FIFO), so the de-dup guarantee degrades gracefully -- worst
# case an occasional re-credit -- instead of growing without limit.
_MAX_CREDITED = 512


def _normalize_path(raw: Any) -> str:
    """Normalize to a stable workspace-relative key (abs and rel must match)."""
    if not isinstance(raw, str):
        return ""
    path = raw.strip()
    if not path:
        return ""
    cwd = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    if cwd:
        cwd_norm = cwd.rstrip("/") + "/"
        if path.startswith(cwd_norm):
            path = path[len(cwd_norm) :]
    return os.path.normpath(path).strip("/") if path not in (".", "/") else path


def _credited(state: Any) -> list[str]:
    if not isinstance(state, dict):
        return []
    current = state.get(_CREDITED_KEY)
    if not isinstance(current, list):
        current = []
        state[_CREDITED_KEY] = current
    return current


def should_credit_path(state: dict[str, Any], path: Any) -> tuple[dict[str, Any], bool]:
    """Claim *path*'s once-per-session full-content baseline credit.

    Mode-agnostic entry point shared by the read tool's outline/range baseline
    credit and the code-intel counterfactual (vanilla grep+read of surfaced
    files). ``credit=True`` -> first claim this session, count the saving;
    ``credit=False`` -> already claimed, zero it. Unknown/empty paths always
    credit (left untouched).
    """
    if not isinstance(state, dict):
        return state, True
    norm = _normalize_path(path)
    if not norm:
        return state, True
    credited = _credited(state)
    if norm in credited:
        return state, False
    credited.append(norm)
    if len(credited) > _MAX_CREDITED:
        del credited[: len(credited) - _MAX_CREDITED]
    state[_CREDITED_KEY] = credited
    return state, True


def should_credit(state: dict[str, Any], path: Any, mode: Any) -> tuple[dict[str, Any], bool]:
    """Return ``(state, credit?)`` for a read.

    ``credit=True``  -> emit the read's ``tokens_saved`` unchanged.
    ``credit=False`` -> this file's full-baseline avoidance was already counted
    this session, so the saving must be zeroed to avoid double-counting.

    Non-baseline modes (``full``/``summary``/``directory``) and unknown paths
    always return ``True`` (left untouched).
    """
    if not isinstance(state, dict):
        return state, True
    if mode not in _BASELINE_MODES:
        return state, True
    return should_credit_path(state, path)


def reset(state: dict[str, Any]) -> dict[str, Any]:
    """Clear the credited set (used on compaction / epoch change)."""
    if isinstance(state, dict):
        state[_CREDITED_KEY] = []
    return state
