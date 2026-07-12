#!/usr/bin/env python3
"""Shadow-mode PostToolUse hook: shrinks oversized results from NON-LemonCrow MCP tools.

The host's other MCP servers (anything not LemonCrow's own) execute normally --
this hook only intercepts their *result* on the way back to the model. When the
extracted text exceeds a threshold, the full text is written to a recoverable
spill file (the same spill store LemonCrow's own gateway uses) and the tool result
the model sees is replaced (``hookSpecificOutput.updatedToolOutput``) with a
bounded head+tail summary plus the canonical footer notice naming the spill path.

FAIL-OPEN everywhere: below-threshold outputs, spill failures, JSON parse
errors, an unrecognized ``tool_response`` shape, or any exception -> this
script emits nothing and exits 0, so the original tool result stands untouched.

Env:
    LEMONCROW_SHADOW_SHRINK=0          Kill switch -- disables this hook entirely.
    LEMONCROW_SHADOW_SHRINK_CHARS      Char threshold override (default mirrors
                                      the gateway's own
                                      ``_DEFAULT_COMPACT_RESULT_CHARS``); 0
                                      disables shrinking.
    LEMONCROW_SHADOW_SHRINK_DEBUG=1    Best-effort append the raw ``tool_response``
                                      shape/preview to
                                      ``<LEMONCROW_ROOT>/logs/mcp_output_shrink_debug.log``
                                      for live shape validation. Off by default
                                      so normal operation never pays disk I/O.

Matcher: registered in hooks.json against ``mcp__.*`` (broad) -- this script
filters out LemonCrow's own tools itself rather than relying on a matcher-side
negative-lookahead regex, since the exact tool-name prefix depends on how the
plugin is installed (bare ``mcp__lc__*`` vs plugin-namespaced
``mcp__plugin_lemoncrow_lc__*``) and in-script filtering has to happen
anyway for the fail-open contract -- a matcher-only exclusion would still need
this same check as a backstop.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Known tool-name prefixes for LemonCrow's OWN MCP tools -- never shrink these.
# "mcp__lc__*" is the bare/dev install shape (server key "lemoncrow" in a
# plain .mcp.json); "mcp__plugin_lemoncrow_lc__*" is the plugin-namespaced
# shape Claude Code uses when LemonCrow is installed as a marketplace plugin
# derived from the installed marketplace/plugin names -- verified against this repo's own
# installed_plugins.json / live tool names, and already relied on elsewhere
# (see mcp_proxy._is_self / SELF_SERVER_NAMES).
_LEMONCROW_MCP_PREFIXES = ("mcp__lc__", "mcp__plugin_lemoncrow_lc__")

# Default shrink threshold: ~8K tokens. Deliberately LOWER than the gateway's
# own 256 KiB dispatch-layer bound (_DEFAULT_COMPACT_RESULT_CHARS): foreign MCP
# servers (browser snapshots, API dumps) routinely return 50-150 KB results
# that a 256 KiB gate would wave through untouched, and unlike LemonCrow's own
# tools there is no upstream lane already bounding them. 32 KiB is where a
# result stops being readable context and starts being ballast. Override with
# LEMONCROW_SHADOW_SHRINK_CHARS.
_DEFAULT_SHRINK_CHARS = 32 * 1024

# Bounded summary target: never exceeds 16 KiB regardless of how large the
# threshold is, mirroring mcp_server._spill_oversized_result_text's own
# target-size clamp for the char-gated spill path.
_MAX_SUMMARY_CHARS = 16384


def _is_non_lemoncrow_mcp_tool(tool_name: str) -> bool:
    if not tool_name.startswith("mcp__"):
        return False
    return not any(tool_name.startswith(prefix) for prefix in _LEMONCROW_MCP_PREFIXES)


def _shrink_threshold_chars() -> int:
    raw = os.environ.get("LEMONCROW_SHADOW_SHRINK_CHARS", str(_DEFAULT_SHRINK_CHARS))
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_SHRINK_CHARS


def _extract_text_blocks(blocks: list[Any]) -> str | None:
    parts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "".join(parts) if parts else None


def _extract_text(tool_response: Any) -> str | None:
    """Best-effort text extraction from an MCP tool_response.

    Handles the two documented shapes: a plain string, or a dict/list of MCP
    content blocks (``{"content": [{"type": "text", "text": ...}, ...]}`` or a
    bare list of such blocks). Anything else (e.g. image-only content, an
    unrecognized dict shape) returns None so the caller fails open.
    """
    if isinstance(tool_response, str):
        return tool_response or None
    if isinstance(tool_response, list):
        return _extract_text_blocks(tool_response)
    if isinstance(tool_response, dict):
        content = tool_response.get("content")
        if isinstance(content, list):
            return _extract_text_blocks(content)
        text = tool_response.get("text")
        if isinstance(text, str):
            return text or None
    return None


def _debug_log(tool_name: str, tool_response: Any) -> None:
    """Best-effort raw-shape logging, opt-in via LEMONCROW_SHADOW_SHRINK_DEBUG=1."""
    if os.environ.get("LEMONCROW_SHADOW_SHRINK_DEBUG", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        root = Path(os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow")
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        shape = type(tool_response).__name__
        preview = repr(tool_response)[:500]
        with (log_dir / "mcp_output_shrink_debug.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} tool={tool_name} shape={shape} preview={preview}\n")
    except Exception:  # noqa: BLE001 -- debug logging must never break the hook
        pass


def _shrink(tool_name: str, text: str, threshold: int) -> str | None:
    """Spill the full text and return a bounded head+tail summary + the
    canonical footer notice, or None if spilling failed (caller then fails
    open)."""
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill
    from lemoncrow.core.capabilities.tool_supervision.compact_output import compress_tool_output

    record = tool_output_spill.spill(text, tool_name=tool_name, kind="tool_output")
    if record is None:
        return None  # spill failed -- never lossily replace without a recovery path.

    target = max(256, min(threshold, _MAX_SUMMARY_CHARS))
    head_chars = int(target * 0.7)
    tail_chars = max(1, target - head_chars)
    summary = compress_tool_output(text, threshold_chars=target, head_chars=head_chars, tail_chars=tail_chars)
    # Footer-only (no stats header): kept_chars is len(summary) -- the body
    # actually shown -- matching the convention every other producer of this
    # footer uses.
    return tool_output_spill.summary_with_ref(
        summary,
        record,
        original_chars=len(text),
        verb="shrunk",
        max_chars=threshold,
    )


def _run(payload: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or "")
    if not _is_non_lemoncrow_mcp_tool(tool_name):
        return 0

    tool_response = payload.get("tool_response")
    _debug_log(tool_name, tool_response)

    text = _extract_text(tool_response)
    if not text:
        return 0

    threshold = _shrink_threshold_chars()
    if threshold <= 0 or len(text) <= threshold:
        return 0

    composed = _shrink(tool_name, text, threshold)
    if composed is None:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": composed,
                }
            }
        )
    )
    return 0


def main() -> int:
    if os.environ.get("LEMONCROW_SHADOW_SHRINK", "1").strip() == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        return _run(payload)
    except Exception:  # noqa: BLE001 -- hooks must be fail-open
        return 0


if __name__ == "__main__":
    sys.exit(main())
