#!/usr/bin/env python3
"""PostToolUse MCP result compaction at the tool-result boundary.

Two independent lanes live here:

* Foreign MCP shadow shrink (existing behavior): oversized non-LemonCrow MCP
  results are spilled and replaced with a bounded head/tail view.
* Optional LemonCrow + Headroom residual Bash analysis: only a large, newly
  produced Bash result that survived LemonCrow/RTK compaction is considered.
  The safe default is shadow telemetry; lossy replacement requires explicit
  ``LEMONCROW_HEADROOM_MCP_TAIL_MODE=apply``.
* Large foreign MCP results may use Headroom first when a supported compressor
  yields material savings; the existing deterministic head/tail spill remains
  the fallback when Headroom is absent, unsupported, or not worthwhile.

The Headroom lane deliberately does NOT proxy Anthropic traffic. It cannot
touch Claude's system prompt, tool schemas, prior conversation, or any
already-sent message, so provider prompt-cache prefixes remain under Claude
Code's control.

All other model-facing LemonCrow tools remain authoritative and untouched:
``code_search``, ``read``, ``edit`` and ``web_fetch``. Bash results that already
carry LemonCrow compaction/spill markers are also skipped. Any accepted lossy
compression first spills the original and adds LemonCrow's canonical ``full:``
recovery pointer.

FAIL-OPEN everywhere: below-threshold outputs, unavailable Headroom, spill
failures, unrecognized response shapes, or any exception leave the original
tool result untouched.
"""

from __future__ import annotations

import json
import os
import shlex
import site
import sys
import time
from pathlib import Path
from typing import Any

_LEMONCROW_MCP_PREFIXES = ("mcp__lc__", "mcp__plugin_lemoncrow_lc__")

_HEADROOM_PROTECTED_LC_TOOLS = frozenset(
    {
        "callers",
        "code",
        "code_search",
        "context",
        "definition",
        "edit",
        "graph",
        "grep",
        "read",
        "references",
        "rescue",
        "symbols",
        "verify",
        "web_fetch",
    }
)

_DEFAULT_HEADROOM_TAIL_MIN_CHARS = 8 * 1024
_DEFAULT_HEADROOM_MIN_SAVED_TOKENS = 1000
_DEFAULT_HEADROOM_MIN_SAVINGS_RATIO = 0.35
_DEFAULT_SHRINK_CHARS = 32 * 1024
_MAX_SUMMARY_CHARS = 16384


def _is_lemoncrow_mcp_tool(tool_name: str) -> bool:
    return any(tool_name.startswith(prefix) for prefix in _LEMONCROW_MCP_PREFIXES)


def _is_non_lemoncrow_mcp_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") and not _is_lemoncrow_mcp_tool(tool_name)


def _lc_tool_leaf(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1]


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _headroom_tail_mode() -> str:
    """Return off|shadow|apply for LC residual Bash compression.

    The explicit mode is authoritative. The legacy boolean now maps to
    ``shadow`` so upgrading cannot silently turn an observational experiment
    into a lossy rewrite. Benchmarks that intentionally exercise replacement
    set ``LEMONCROW_HEADROOM_MCP_TAIL_MODE=apply``.
    """
    raw = os.environ.get("LEMONCROW_HEADROOM_MCP_TAIL_MODE", "").strip().lower()
    if raw in {"off", "shadow", "apply"}:
        return raw
    if _truthy(os.environ.get("LEMONCROW_HEADROOM_MCP_TAIL", "0")):
        return "shadow"
    return "off"


def _headroom_tail_enabled() -> bool:
    return _headroom_tail_mode() != "off"


def _headroom_tail_min_chars() -> int:
    raw = os.environ.get(
        "LEMONCROW_HEADROOM_TAIL_MIN_CHARS",
        str(_DEFAULT_HEADROOM_TAIL_MIN_CHARS),
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_HEADROOM_TAIL_MIN_CHARS


def _headroom_min_saved_tokens() -> int:
    raw = os.environ.get(
        "LEMONCROW_HEADROOM_MIN_SAVED_TOKENS",
        str(_DEFAULT_HEADROOM_MIN_SAVED_TOKENS),
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_HEADROOM_MIN_SAVED_TOKENS


def _headroom_min_savings_ratio() -> float:
    raw = os.environ.get(
        "LEMONCROW_HEADROOM_MIN_SAVINGS_RATIO",
        str(_DEFAULT_HEADROOM_MIN_SAVINGS_RATIO),
    )
    try:
        return min(0.95, max(0.0, float(raw)))
    except ValueError:
        return _DEFAULT_HEADROOM_MIN_SAVINGS_RATIO


def _lc_already_compacted(text: str) -> bool:
    tail = text[-1600:]
    return any(
        marker in tail
        for marker in (
            "[lc:",
            "[output compacted",
            "[output truncated",
        )
    )


def _command_family(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    command = str(tool_input.get("command") or "").strip()
    if not command:
        return ""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    while tokens and ("=" in tokens[0] and not tokens[0].startswith(("./", "/"))):
        tokens.pop(0)
    while tokens and tokens[0] in {"env", "sudo", "command", "time"}:
        tokens.pop(0)
    return Path(tokens[0]).name if tokens else ""


def _shrink_threshold_chars() -> int:
    raw = os.environ.get(
        "LEMONCROW_SHADOW_SHRINK_CHARS",
        str(_DEFAULT_SHRINK_CHARS),
    )
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
    """Best-effort text extraction from a Claude PostToolUse response."""
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
    if os.environ.get("LEMONCROW_SHADOW_SHRINK_DEBUG", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        root = Path(
            os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow"
        )
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        shape = type(tool_response).__name__
        preview = repr(tool_response)[:500]
        with (log_dir / "mcp_output_shrink_debug.log").open(
            "a",
            encoding="utf-8",
        ) as fh:
            fh.write(f"{time.time():.3f} tool={tool_name} " f"shape={shape} preview={preview}\n")
    except Exception:
        pass


def _shrink(tool_name: str, text: str, threshold: int) -> str | None:
    """Existing foreign-MCP bounded head/tail shrink."""
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill
    from lemoncrow.pro.capabilities.tool_supervision.compact_output import (
        compress_tool_output,
    )

    record = tool_output_spill.spill(
        text,
        tool_name=tool_name,
        kind="tool_output",
    )
    if record is None:
        return None

    target = max(256, min(threshold, _MAX_SUMMARY_CHARS))
    head_chars = int(target * 0.7)
    tail_chars = max(1, target - head_chars)
    summary = compress_tool_output(
        text,
        threshold_chars=target,
        head_chars=head_chars,
        tail_chars=tail_chars,
    )
    return tool_output_spill.summary_with_ref(
        summary,
        record,
        original_chars=len(text),
        verb="shrunk",
        max_chars=threshold,
    )


def _append_headroom_stats(payload: dict[str, Any]) -> None:
    path = os.environ.get("LEMONCROW_HEADROOM_TAIL_STATS", "").strip()
    if not path:
        return
    try:
        stats_path = Path(path)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        with stats_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        pass


def _enable_isolated_headroom() -> None:
    site_packages = os.environ.get("LEMONCROW_HEADROOM_SITE_PACKAGES", "").strip()
    if site_packages:
        site.addsitedir(site_packages)


def _headroom_candidate(
    tool_name: str,
    text: str,
    tool_input: Any,
    *,
    foreign: bool,
) -> tuple[str | None, dict[str, Any]]:
    """Return a Headroom candidate plus telemetry without mutating model context."""
    _enable_isolated_headroom()

    from headroom.providers.anthropic import AnthropicProvider
    from headroom.transforms.content_detector import ContentType, detect_content_type

    tool_args = dict(tool_input) if isinstance(tool_input, dict) else {}
    context = str(tool_args.get("command") or tool_args.get("query") or "")
    detected = detect_content_type(text)
    candidate: str | None = None
    strategy = ""

    if detected.content_type is ContentType.BUILD_OUTPUT:
        from headroom.transforms.log_compressor import LogCompressor, LogCompressorConfig

        result = LogCompressor(LogCompressorConfig(enable_ccr=False, max_total_lines=100)).compress(
            text, context=context
        )
        candidate = result.compressed
        strategy = "log"
    elif foreign and detected.content_type is ContentType.JSON_ARRAY:
        from headroom.integrations.mcp import HeadroomMCPCompressor

        result = HeadroomMCPCompressor().compress(
            text,
            tool_name=tool_name,
            tool_args=tool_args,
            user_query=context,
        )
        if result.was_compressed:
            candidate = result.compressed_content
            strategy = "mcp-json"
    elif foreign and detected.content_type is ContentType.SEARCH_RESULTS:
        from headroom.transforms.search_compressor import SearchCompressor, SearchCompressorConfig

        result = SearchCompressor(
            SearchCompressorConfig(enable_ccr=False, max_total_matches=30, max_files=15)
        ).compress(text, context=context)
        candidate = result.compressed
        strategy = "search"

    counter = AnthropicProvider().get_token_counter(os.environ.get("LEMONCROW_HEADROOM_MODEL", "claude-opus-4-8"))
    tokens_before = int(counter.count_text(text) or 0)
    tokens_after = (
        int(counter.count_text(candidate) or 0) if isinstance(candidate, str) and candidate != text else tokens_before
    )
    saved = max(0, tokens_before - tokens_after)
    ratio = (saved / tokens_before) if tokens_before > 0 else 0.0
    meta = {
        "tool": tool_name,
        "command_family": _command_family(tool_input),
        "strategy": strategy,
        "content_type": detected.content_type.value,
        "confidence": detected.confidence,
        "post_lc_chars": len(text),
        "candidate_chars": len(candidate) if isinstance(candidate, str) else len(text),
        "chars_saved": max(0, len(text) - len(candidate)) if isinstance(candidate, str) else 0,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_saved": saved,
        "savings_ratio": ratio,
    }
    return candidate, meta


def _worthwhile_headroom(meta: dict[str, Any]) -> bool:
    return (
        int(meta.get("tokens_saved") or 0) >= _headroom_min_saved_tokens()
        and float(meta.get("savings_ratio") or 0.0) >= _headroom_min_savings_ratio()
    )


def _headroom_tail_compress(
    tool_name: str,
    text: str,
    tool_input: Any,
) -> str | None:
    """Observe or apply Headroom to residual LC Bash output only."""
    leaf = _lc_tool_leaf(tool_name)
    if leaf != "bash" or leaf in _HEADROOM_PROTECTED_LC_TOOLS:
        return None

    threshold = _headroom_tail_min_chars()
    if threshold <= 0 or len(text) <= threshold:
        return None

    mode = _headroom_tail_mode()
    base = {
        "mode": mode,
        "tool": tool_name,
        "command_family": _command_family(tool_input),
        "post_lc_chars": len(text),
        "lc_already_compacted": _lc_already_compacted(text),
    }
    if base["lc_already_compacted"]:
        _append_headroom_stats({**base, "decision": "skip_lc_compacted"})
        return None

    try:
        candidate, meta = _headroom_candidate(
            tool_name,
            text,
            tool_input,
            foreign=False,
        )
    except Exception as exc:
        _append_headroom_stats({**base, "decision": "fail_open", "error": type(exc).__name__})
        return None

    worthwhile = (
        isinstance(candidate, str) and candidate != text and len(candidate) < len(text) and _worthwhile_headroom(meta)
    )
    telemetry = {**base, **meta, "worthwhile": worthwhile}
    if mode == "shadow":
        _append_headroom_stats({**telemetry, "decision": "shadow_worthwhile" if worthwhile else "shadow_rejected"})
        return None
    if mode != "apply" or not worthwhile:
        _append_headroom_stats({**telemetry, "decision": "apply_rejected"})
        return None

    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(
        text,
        tool_name=tool_name,
        kind="headroom_tail_original",
    )
    if record is None:
        _append_headroom_stats({**telemetry, "decision": "spill_failed"})
        return None

    composed = tool_output_spill.summary_with_ref(
        candidate,
        record,
        original_chars=len(text),
        verb="compacted:headroom-tail",
    )
    if len(composed) >= len(text):
        _append_headroom_stats({**telemetry, "decision": "footer_erased_savings"})
        return None

    _append_headroom_stats({**telemetry, "decision": "applied", "wire_chars": len(composed)})
    return composed


def _headroom_foreign_compress(
    tool_name: str,
    text: str,
    tool_input: Any,
) -> str | None:
    """Observe/apply Headroom for large foreign MCP output under the same mode gate."""
    mode = _headroom_tail_mode()
    if mode == "off":
        return None
    try:
        candidate, meta = _headroom_candidate(
            tool_name,
            text,
            tool_input,
            foreign=True,
        )
    except Exception as exc:
        _append_headroom_stats(
            {
                "mode": mode,
                "tool": tool_name,
                "decision": "foreign_fail_open",
                "error": type(exc).__name__,
            }
        )
        return None
    worthwhile = (
        isinstance(candidate, str) and candidate != text and len(candidate) < len(text) and _worthwhile_headroom(meta)
    )
    telemetry = {**meta, "mode": mode, "worthwhile": worthwhile}
    if mode == "shadow":
        _append_headroom_stats(
            {
                **telemetry,
                "decision": "foreign_shadow_worthwhile" if worthwhile else "foreign_shadow_rejected",
            }
        )
        return None
    if not worthwhile:
        _append_headroom_stats({**telemetry, "decision": "foreign_apply_rejected"})
        return None

    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(text, tool_name=tool_name, kind="headroom_foreign_original")
    if record is None:
        _append_headroom_stats({**telemetry, "decision": "foreign_spill_failed"})
        return None
    composed = tool_output_spill.summary_with_ref(
        candidate,
        record,
        original_chars=len(text),
        verb="compacted:headroom",
    )
    if len(composed) >= len(text):
        _append_headroom_stats({**telemetry, "decision": "foreign_footer_erased_savings"})
        return None
    _append_headroom_stats({**telemetry, "decision": "foreign_applied", "wire_chars": len(composed)})
    return composed


def _emit_updated_output(composed: str) -> None:
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


def _run(payload: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or "")
    tool_response = payload.get("tool_response")
    _debug_log(tool_name, tool_response)

    if _is_lemoncrow_mcp_tool(tool_name):
        if not _headroom_tail_enabled():
            return 0
        text = _extract_text(tool_response)
        if not text:
            return 0
        composed = _headroom_tail_compress(
            tool_name,
            text,
            payload.get("tool_input"),
        )
        if composed is not None:
            _emit_updated_output(composed)
        return 0

    if not _is_non_lemoncrow_mcp_tool(tool_name):
        return 0
    if os.environ.get("LEMONCROW_SHADOW_SHRINK", "1").strip() == "0":
        return 0

    text = _extract_text(tool_response)
    if not text:
        return 0

    threshold = _shrink_threshold_chars()
    if threshold <= 0 or len(text) <= threshold:
        return 0

    composed = _headroom_foreign_compress(
        tool_name,
        text,
        payload.get("tool_input"),
    )
    if composed is None:
        composed = _shrink(tool_name, text, threshold)
    if composed is not None:
        _emit_updated_output(composed)
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        return _run(payload)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
