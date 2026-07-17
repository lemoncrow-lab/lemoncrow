"""Honest deferred credit for code-intel turn reduction.

The counterfactual this module bills is narrow on purpose. Only two tools
surface inline content substantial enough to genuinely replace a ``read``:
``node`` (a whole definition body) and ``explore`` (inline file source
sections). We credit ONE avoided read for a file IFF:

  (a) ``node`` or ``explore`` surfaced that file WITH non-empty inline source,
      AND
  (b) the agent does NOT subsequently ``read`` that file within an observation
      window (``threshold`` ticks).

The partial line-snippets from ``callers``/``callees``/``usages``
are deliberately NOT credited: a few caller lines rarely substitute for a full
read, so crediting them would over-count. A surfacing with no inline source
earns nothing. If the agent later reads the file, nothing is earned (the read
happened; no avoidance). One credit per distinct file per surfacing.

Every function here is a PURE, total transform over a plain ``state`` dict. No
I/O, no exceptions raised to callers, tolerant of missing keys and wrong types.
The caller (the MCP dispatcher) owns persistence and the kill switch.

Keyed shapes (post-strip, i.e. what the agent actually sees) -- see
``mcp_server._strip_code_op_response`` and ``code_context.engine``:

  callers/callees : ``related`` -> flat list of {path|file_path, snippet?}
  usages          : ``references`` -> dict-of-lists keyed by path, each item
                    {path|file_path, snippet?}; OR a flat list (group_by=none)
  node            : single definition {path|file_path, source|snippet|content}
  explore         : ``files`` -> [{file_path, source_sections:[{content}]}]
                    plus ``relationships`` -> {callers,callees,usages} each a
                    list of {related:[...], references:[...]}
"""

from __future__ import annotations

import os
from typing import Any

CODE_INTEL_TOOLS: frozenset[str] = frozenset({"callers", "callees", "usages", "explore", "node"})

# Of the code-intel tools, only these surface inline content substantial enough
# to genuinely substitute for a read: `node` returns a whole definition body and
# `explore` returns inline file source sections. The partial line-snippets from
# `callers`/`callees`/`usages` rarely replace a full read, so crediting
# them would over-count -- they are deliberately NOT credit-eligible.
CREDIT_ELIGIBLE_TOOLS: frozenset[str] = frozenset({"node", "explore"})

# Keys under which a result may carry a reference's inline code.
_SNIPPET_KEYS: tuple[str, ...] = ("snippet", "source", "content")
# Keys under which a reference dict may carry its file path.
_PATH_KEYS: tuple[str, ...] = ("path", "file_path")
# Pending-list key inside the session state dict.
_PENDING_KEY = "code_intel_pending"


def _has_snippet(item: Any) -> bool:
    """True when ``item`` carries a non-empty inline code snippet."""
    if not isinstance(item, dict):
        return False
    for key in _SNIPPET_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _ref_path(item: Any) -> str:
    """Return the raw path string for a reference dict, or '' if absent."""
    if not isinstance(item, dict):
        return ""
    for key in _PATH_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_path(raw: str) -> str:
    """Normalize a reference path to a stable workspace-relative key.

    Strips a leading cwd prefix and any ``#line`` / range suffix so that a
    reference and a later ``read`` of the same file compare equal.
    """
    if not isinstance(raw, str):
        return ""
    path = raw.strip()
    if not path:
        return ""
    # Drop range / anchor suffixes: foo.py#10-20, foo.py#L10, foo.py:10
    for sep in ("#", ":"):
        idx = path.find(sep)
        if idx > 0:
            tail = path[idx + 1 :]
            # Only treat as a line/range suffix when it looks numeric-ish.
            if tail and tail[0] in "0123456789L":
                path = path[:idx]
    # Strip a leading cwd prefix so absolute refs match relative reads.
    cwd = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    if cwd:
        cwd_norm = cwd.rstrip("/") + "/"
        if path.startswith(cwd_norm):
            path = path[len(cwd_norm) :]
    return os.path.normpath(path).strip("/") if path not in (".", "/") else path


def _iter_credited(item: Any) -> list[str]:
    """Yield the normalized path for ``item`` iff it carries a snippet."""
    if _has_snippet(item):
        path = _normalize_path(_ref_path(item))
        if path:
            return [path]
    return []


def _collect_from_explore(result: dict[str, Any], out: list[str]) -> None:
    """explore: credit ONLY inline file source sections.

    The nested ``relationships`` groups carry partial caller/usage line-snippets,
    which do not reliably substitute for a full read, so they are not credited.
    """
    # files: [{file_path, source_sections: [{content, ...}]}]
    files = result.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            sections = entry.get("source_sections")
            has_inline = isinstance(sections, list) and any(_has_snippet(sec) for sec in sections)
            if has_inline:
                path = _normalize_path(_ref_path(entry))
                if path:
                    out.append(path)


def extract_credited_paths(tool_name: str, result: Any) -> list[str]:
    """Distinct workspace-relative paths of snippet-bearing references.

    Returns ``[]`` for non-code-intel tools, non-dict results, or results whose
    references carry no inline snippet. One entry per distinct file.
    """
    if tool_name not in CREDIT_ELIGIBLE_TOOLS or not isinstance(result, dict):
        return []
    out: list[str] = []
    try:
        if tool_name == "node":
            # Single definition: credit its own file iff it ships inline source.
            out.extend(_iter_credited(result))
        elif tool_name == "explore":
            _collect_from_explore(result, out)
    except Exception:  # noqa: BLE001 - total + defensive: never raise to the dispatcher
        return []
    # Distinct, preserve first-seen order.
    seen: set[str] = set()
    distinct: list[str] = []
    for path in out:
        if path and path not in seen:
            seen.add(path)
            distinct.append(path)
    return distinct


def _pending(state: Any) -> list[dict[str, Any]]:
    """Return the live pending list inside ``state`` (tolerant of bad shapes)."""
    if not isinstance(state, dict):
        return []
    current = state.get(_PENDING_KEY)
    if not isinstance(current, list):
        current = []
        if isinstance(state, dict):
            state[_PENDING_KEY] = current
    return current


def record_pending(state: dict[str, Any], tool_name: str, paths: list[str]) -> dict[str, Any]:
    """Append {path, tool, age:0} entries; dedupe by path (keep earliest tool)."""
    if not isinstance(state, dict):
        return state
    pending = _pending(state)
    known = {entry.get("path") for entry in pending if isinstance(entry, dict)}
    for path in paths or []:
        if not isinstance(path, str) or not path or path in known:
            continue
        pending.append({"path": path, "tool": tool_name, "age": 0})
        known.add(path)
    state[_PENDING_KEY] = pending
    return state


def consume_reads(state: dict[str, Any], read_paths: list[str]) -> dict[str, Any]:
    """Drop pending entries whose path matches a read (the read happened)."""
    if not isinstance(state, dict):
        return state
    targets = {_normalize_path(p) for p in (read_paths or []) if isinstance(p, str) and p.strip()}
    if not targets:
        return state
    pending = _pending(state)
    state[_PENDING_KEY] = [entry for entry in pending if not (isinstance(entry, dict) and entry.get("path") in targets)]
    return state


def tick_and_credit(state: dict[str, Any], threshold: int) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Age every pending entry; bill + remove entries that reached ``threshold``.

    Each returned credit ``{"tool", "path"}`` is exactly one avoided read.
    """
    if not isinstance(state, dict):
        return state, []
    try:
        limit = int(threshold)
    except (TypeError, ValueError):
        return state, []
    pending = _pending(state)
    survivors: list[dict[str, Any]] = []
    credits: list[dict[str, str]] = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        try:
            age = int(entry.get("age", 0)) + 1
        except (TypeError, ValueError):
            age = 1
        if age >= limit:
            credits.append(
                {
                    "tool": str(entry.get("tool") or ""),
                    "path": str(entry.get("path") or ""),
                }
            )
        else:
            entry["age"] = age
            survivors.append(entry)
    state[_PENDING_KEY] = survivors
    return state, credits


def reset_pending(state: dict[str, Any]) -> dict[str, Any]:
    """Clear all pending entries (used on compaction / epoch change)."""
    if isinstance(state, dict):
        state[_PENDING_KEY] = []
    return state
