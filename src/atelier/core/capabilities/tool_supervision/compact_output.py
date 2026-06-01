"""Threshold-triggered tool-output compaction.

Head+tail compression strategy validated at -51.8% input tokens on SWE-bench Pro
(n=75 paired runs, Claude Sonnet 4.6) — ReasonBlocks TokenSavingMiddleware approach.

Key design choices:
- Char-based threshold (1800 chars) instead of token-based — predictable and fast
- Asymmetric head/tail split: head gets more budget (start has command, first error,
  context; tail has final result/status — middle is usually repetitive output)
- LLM summarization is opt-in only; head+tail alone achieves the benchmark savings
- keep_recent_tool_messages exempts the last N messages from compression, matching
  the RB spec (agent must see its active step at full fidelity)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

import tiktoken
from pydantic import BaseModel, ConfigDict

from atelier.infra.internal_llm import InternalLLMError, summarize

CompactMethod = Literal["passthrough", "deterministic_truncate", "llm_summary"]
ContentType = Literal["file", "grep", "bash", "tool_output", "unknown"]

# Validated threshold from ReasonBlocks SWE-bench benchmark
DEFAULT_COMPRESS_THRESHOLD_CHARS = 1800
DEFAULT_HEAD_KEEP_CHARS = 900  # ~56% of budget — head has more signal
DEFAULT_TAIL_KEEP_CHARS = 700  # ~44% of budget — tail has final result/status


# --------------------------------------------------------------------------- #
# Stats tracker                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class TokenSavingStats:
    """Aggregate token-saving counters for a session or benchmark run.

    Matches the ReasonBlocks TokenSavingMiddleware.stats surface.
    """

    compressions: int = 0
    chars_saved: int = 0
    early_exits: int = 0
    tokens_saved: int = 0  # approx from tiktoken cl100k_base
    messages_compressed: list[int] = field(default_factory=list)  # chars saved per message

    def record(self, original_chars: int, compacted_chars: int, original_tokens: int, compacted_tokens: int) -> None:
        """Record one compression event."""
        if original_chars > compacted_chars:
            self.compressions += 1
            saved_chars = original_chars - compacted_chars
            self.chars_saved += saved_chars
            self.tokens_saved += max(0, original_tokens - compacted_tokens)
            self.messages_compressed.append(saved_chars)

    @property
    def compression_ratio(self) -> float:
        """Fraction of chars removed (0 = no savings, 1 = all removed)."""
        if not self.messages_compressed:
            return 0.0
        total_original = self.chars_saved + sum(DEFAULT_COMPRESS_THRESHOLD_CHARS for _ in self.messages_compressed)
        return self.chars_saved / max(1, total_original + self.chars_saved)


class CompactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compacted: str
    original_tokens: int
    compacted_tokens: int
    recovery_hint: str
    method: CompactMethod
    content_type: str


_ENCODING = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def compress_tool_output(
    content: str,
    *,
    threshold_chars: int = DEFAULT_COMPRESS_THRESHOLD_CHARS,
    head_chars: int = DEFAULT_HEAD_KEEP_CHARS,
    tail_chars: int = DEFAULT_TAIL_KEEP_CHARS,
) -> str:
    """Head+tail compress a single tool output string.

    Returns the content unchanged when it is within the threshold.
    When above the threshold, returns head + omission notice + tail.

    This is a standalone helper matching the ReasonBlocks compress_tool_output()
    API, usable outside the compact MCP tool lifecycle.

    Args:
        content:         The tool output string.
        threshold_chars: Minimum length before compression is applied.
        head_chars:      Characters to keep from the start (default 900 — more
                         signal: command, first error, initial context).
        tail_chars:      Characters to keep from the end (default 700 — final
                         result, return value, last error).
    """
    if len(content) <= threshold_chars:
        return content
    elided = len(content) - head_chars - tail_chars
    return f"{content[:head_chars]}\n\n[... {elided} chars truncated ...]\n\n{content[-tail_chars:]}"


def _head_tail(text: str, *, max_chars: int) -> str:
    """Legacy helper — kept for backward compatibility with existing callers.

    Uses asymmetric split: 60% head / 40% tail.
    """
    if len(text) <= max_chars:
        return text
    head = max(1, int(max_chars * 0.6))
    tail = max(1, max_chars - head)
    elided = len(text) - head - tail
    return f"{text[:head]}\n... ({elided} chars elided) ...\n{text[-tail:]}"


def _compact_grep(content: str) -> str:
    grouped: dict[str, list[str]] = {}
    for line in content.splitlines():
        file_name = line.split(":", 1)[0] if ":" in line else "unknown"
        grouped.setdefault(file_name, []).append(line)
    parts: list[str] = []
    for file_name, lines in grouped.items():
        parts.extend(lines[:3])
        remaining = len(lines) - 3
        if remaining > 0:
            parts.append(f"... and {remaining} more in {file_name}")
    return "\n".join(parts)


def _compact_bash(content: str, budget_chars: int = 8000) -> str:
    """Compress bash output keeping head and tail by char budget.

    Preserves stderr context even after truncation — extracts it first,
    then re-attaches it if the truncated body lost it.
    """
    stderr_match = re.search(r"stderr:\s*(.+?)(?:\n\n|\Z)", content, flags=re.IGNORECASE | re.DOTALL)
    stderr = stderr_match.group(1).strip() if stderr_match else ""

    if len(content) <= budget_chars:
        return content

    head_chars = int(budget_chars * 0.6)
    tail_chars = budget_chars - head_chars
    compacted = compress_tool_output(
        content, threshold_chars=budget_chars, head_chars=head_chars, tail_chars=tail_chars
    )
    if stderr and stderr not in compacted:
        return f"{compacted}\n\nFull stderr:\n{stderr}"
    return compacted


def _compact_json(content: str) -> str | None:
    # Emit compact JSON (no indent whitespace): this helper exists to reduce
    # oversized tool output, so pretty-printing would be self-defeating.
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        list_sample = data[:2]
        return json.dumps(
            {"type": "list", "len": len(data), "sample": list_sample},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if isinstance(data, dict):
        keys = sorted(data.keys())
        dict_sample = {key: data[key] for key in keys[:10]}
        return json.dumps(
            {"type": "object", "keys": keys, "sample": dict_sample},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(
        {"type": type(data).__name__, "value": data},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deterministic_truncate(content: str, content_type: str, budget_tokens: int) -> str:
    if content_type == "grep":
        return _compact_grep(content)
    if content_type == "bash":
        return _compact_bash(content, budget_chars=max(200, budget_tokens * 4))
    if content_type == "tool_output":
        compact_json = _compact_json(content)
        if compact_json is not None:
            return compact_json
    max_chars = max(200, budget_tokens * 4)
    return _head_tail(content, max_chars=max_chars)


def compact(
    content: str,
    content_type: str = "unknown",
    budget_tokens: int = 500,
    *,
    recovery_hint: str | None = None,
    enable_llm: bool = False,
) -> CompactResult:
    """Compact tool output using char-based threshold + head/tail compression.

    Uses a char-based threshold (1800 chars by default) rather than token-based
    for consistency with the validated ReasonBlocks approach. LLM summarization
    is opt-in only — head+tail alone achieves the benchmark -51.8% token savings.

    Args:
        content:       Tool output to compact.
        content_type:  One of file, grep, bash, tool_output, unknown.
        budget_tokens: Target token budget for the compacted result.
        recovery_hint: How to get the full output if needed.
        enable_llm:    If True, attempt LLM summarization for large outputs
                       when Internal LLM is available. Adds latency; off by default.
    """
    original_tokens = _count_tokens(content)
    hint = recovery_hint or "Re-run the original tool call or request the full output by path/range."

    # Passthrough: under the validated char threshold — no compression needed
    if len(content) <= DEFAULT_COMPRESS_THRESHOLD_CHARS:
        return CompactResult(
            compacted=content,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            recovery_hint=hint,
            method="passthrough",
            content_type=content_type,
        )

    method: CompactMethod = "deterministic_truncate"
    compacted = deterministic_truncate(content, content_type, budget_tokens)

    if enable_llm and original_tokens > 2000 and content_type != "grep":
        try:
            prompt = f"Recovery hint: {hint}\n\nOutput to summarize:\n{content}"
            compacted = summarize(prompt, max_tokens=budget_tokens)
            method = "llm_summary"
        except InternalLLMError:
            method = "deterministic_truncate"

    compacted_tokens = _count_tokens(compacted)
    return CompactResult(
        compacted=compacted,
        original_tokens=original_tokens,
        compacted_tokens=compacted_tokens,
        recovery_hint=hint,
        method=method,
        content_type=content_type,
    )


def compress_history(
    messages: list[dict[str, str]],
    *,
    keep_recent: int = 2,
    threshold_chars: int = DEFAULT_COMPRESS_THRESHOLD_CHARS,
    head_chars: int = DEFAULT_HEAD_KEEP_CHARS,
    tail_chars: int = DEFAULT_TAIL_KEEP_CHARS,
    stats: TokenSavingStats | None = None,
) -> list[dict[str, str]]:
    """Head+tail compress stale tool-output messages in a history list.

    The most recent ``keep_recent`` tool messages are exempt — the agent must
    see its active step at full fidelity (matches RB ``keep_recent_tool_messages``
    default of 2).

    Each message is expected to be a dict with at least a ``"role"`` key and
    a ``"content"`` key (LangChain / OpenAI message shape). Only messages with
    ``role == "tool"`` are candidates for compression.

    Args:
        messages:         The full message history list (modified in a new list).
        keep_recent:      How many of the most recent tool messages to exempt.
        threshold_chars:  Minimum content length before compression is applied.
        head_chars:       Characters to keep from the start of a tool message.
        tail_chars:       Characters to keep from the end of a tool message.
        stats:            Optional stats tracker — records each compression event.

    Returns:
        A new list of messages with stale tool outputs compressed.
    """
    # Identify tool-message indices in order.
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    # The last `keep_recent` tool messages are exempt.
    exempt = set(tool_indices[-keep_recent:]) if keep_recent > 0 else set()

    result: list[dict[str, str]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool" or i in exempt:
            result.append(msg)
            continue
        content = msg.get("content", "")
        compressed = compress_tool_output(
            content,
            threshold_chars=threshold_chars,
            head_chars=head_chars,
            tail_chars=tail_chars,
        )
        if stats is not None and compressed != content:
            orig_tokens = _count_tokens(content)
            comp_tokens = _count_tokens(compressed)
            stats.record(len(content), len(compressed), orig_tokens, comp_tokens)
        result.append({**msg, "content": compressed})
    return result


__all__ = [
    "CompactResult",
    "TokenSavingStats",
    "compact",
    "compress_history",
    "compress_tool_output",
    "deterministic_truncate",
]
