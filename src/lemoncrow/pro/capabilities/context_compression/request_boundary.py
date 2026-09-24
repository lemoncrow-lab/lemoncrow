"""Optional second-stage request-boundary context compression.

LemonCrow's tool renderers remain authoritative for correctness-sensitive code,
search, graph, rescue, and shell output.  This module only gives a replaceable
compressor a chance to compact *residual* tool results immediately before an
LLM request is sent.

The canonical session history is never mutated.  Compressed wire messages are
tracked separately so provider prefix caching sees the same bytes on the next
turn.  Any tool result changed by an external compressor is first preserved in
LemonCrow's spill store; if that spill fails, the original payload is sent.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from lemoncrow.pro.capabilities.tool_supervision.tool_output_spill import spill, summary_with_ref

logger = logging.getLogger(__name__)

# These outputs already have LemonCrow-specific semantic rendering/compaction.
# Keep them byte-for-byte exact; a generic compressor must never reinterpret
# pointers, source ranges, graph edges, fatal shell evidence, or edit receipts.
_PROTECTED_TOOL_NAMES = frozenset(
    {
        "bash",
        "callers",
        "code",
        "code_search",
        "context",
        "definition",
        "edit",
        "graph",
        "grep",
        "mcp_tool",
        "read",
        "references",
        "rescue",
        "search_code",
        "symbols",
        "verify",
    }
)


@dataclass(frozen=True)
class RequestCompressionResult:
    """Wire-ready messages plus incremental compression telemetry."""

    messages: list[dict[str, Any]]
    backend: str
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    estimated_tokens_saved: int = 0
    backend_reported_tokens_saved: int = 0
    changed_messages: int = 0
    protected_messages: int = 0
    transforms: tuple[str, ...] = ()
    reason: str = ""

    @property
    def savings_pct(self) -> float:
        if self.estimated_tokens_before <= 0:
            return 0.0
        return round(100.0 * self.estimated_tokens_saved / self.estimated_tokens_before, 2)


class RequestContextCompressor(Protocol):
    """Replaceable second-stage compressor for provider-bound messages."""

    @property
    def enabled(self) -> bool: ...

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        frozen_message_count: int = 0,
    ) -> RequestCompressionResult: ...


class NoopRequestContextCompressor:
    """Zero-overhead baseline used unless explicitly enabled."""

    @property
    def enabled(self) -> bool:
        return False

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        frozen_message_count: int = 0,
    ) -> RequestCompressionResult:
        del model, frozen_message_count
        return RequestCompressionResult(messages=messages, backend="none")


@dataclass
class RequestCompressionState:
    """Preserve the exact compressed prefix previously sent to one provider."""

    raw_message_count: int = 0
    raw_prefix_hash: str = ""
    wire_prefix: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""

    def prepare(
        self,
        canonical_messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> tuple[list[dict[str, Any]], int]:
        if (
            self.raw_message_count > 0
            and self.model == model
            and len(canonical_messages) >= self.raw_message_count
            and len(self.wire_prefix) == self.raw_message_count
            and _messages_hash(canonical_messages[: self.raw_message_count]) == self.raw_prefix_hash
        ):
            delta = copy.deepcopy(canonical_messages[self.raw_message_count :])
            return copy.deepcopy(self.wire_prefix) + delta, self.raw_message_count
        return copy.deepcopy(canonical_messages), 0

    def commit(
        self,
        canonical_messages: list[dict[str, Any]],
        wire_messages: list[dict[str, Any]],
        *,
        model: str,
    ) -> None:
        # Headroom's public contract preserves message count.  Refuse to cache a
        # prefix if a future backend violates that invariant.
        if len(canonical_messages) != len(wire_messages):
            self.reset()
            return
        self.raw_message_count = len(canonical_messages)
        self.raw_prefix_hash = _messages_hash(canonical_messages)
        self.wire_prefix = copy.deepcopy(wire_messages)
        self.model = model

    def reset(self) -> None:
        self.raw_message_count = 0
        self.raw_prefix_hash = ""
        self.wire_prefix.clear()
        self.model = ""


class HeadroomRequestContextCompressor:
    """Headroom adapter with LemonCrow-owned protection and retrieval."""

    @property
    def enabled(self) -> bool:
        return True

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        frozen_message_count: int = 0,
    ) -> RequestCompressionResult:
        original = copy.deepcopy(messages)
        tokens_before = _estimate_tokens(original)
        tool_names = _tool_names_by_call_id(original)
        protected = _protected_message_indexes(original, tool_names)

        kwargs: dict[str, Any] = {
            "compress_user_messages": False,
            "compress_system_messages": False,
            "protect_recent": 0,
            "protect_analysis_context": True,
            "frozen_message_count": max(0, frozen_message_count),
            "min_tokens_to_compress": _int_env("LEMONCROW_HEADROOM_MIN_TOKENS", 250),
        }
        target_ratio = _optional_float_env("LEMONCROW_HEADROOM_TARGET_RATIO")
        if target_ratio is not None:
            kwargs["target_ratio"] = target_ratio

        try:
            headroom = importlib.import_module("headroom")
            compressed = headroom.compress(copy.deepcopy(original), model=model, **kwargs)
        except Exception as exc:
            logger.warning("Headroom compression failed open: %s", exc, exc_info=True)
            return RequestCompressionResult(
                messages=original,
                backend="headroom",
                estimated_tokens_before=tokens_before,
                estimated_tokens_after=tokens_before,
                protected_messages=len(protected),
                reason=f"failed open: {type(exc).__name__}",
            )

        candidate = getattr(compressed, "messages", None)
        if not isinstance(candidate, list) or len(candidate) != len(original):
            return RequestCompressionResult(
                messages=original,
                backend="headroom",
                estimated_tokens_before=tokens_before,
                estimated_tokens_after=tokens_before,
                protected_messages=len(protected),
                reason="backend changed message cardinality",
            )

        final = copy.deepcopy(candidate)
        changed_messages = 0
        for index, original_message in enumerate(original):
            if index in protected:
                final[index] = original_message
                continue

            # Stage one intentionally compresses generic tool results only.
            if original_message.get("role") != "tool":
                final[index] = original_message
                continue

            old_content = original_message.get("content")
            new_content = final[index].get("content") if isinstance(final[index], dict) else None
            if not isinstance(old_content, str) or not isinstance(new_content, str) or new_content == old_content:
                final[index] = original_message
                continue

            tool_call_id = str(original_message.get("tool_call_id") or "")
            tool_name = tool_names.get(tool_call_id, "tool")
            record = spill(old_content, tool_name=_spill_safe_tool_name(tool_name), kind="headroom_original")
            if record is None:
                # Reversibility is a LemonCrow invariant.  Never accept lossy
                # compression when the original cannot be hydrated on demand.
                final[index] = original_message
                continue

            with_ref = summary_with_ref(
                new_content,
                record,
                original_chars=len(old_content),
                verb="compacted:headroom",
            )
            if len(with_ref) >= len(old_content):
                final[index] = original_message
                continue

            restored_shape = copy.deepcopy(original_message)
            restored_shape["content"] = with_ref
            final[index] = restored_shape
            changed_messages += 1

        tokens_after = _estimate_tokens(final)
        transforms_raw = getattr(compressed, "transforms_applied", ()) or ()
        transforms = tuple(str(item) for item in transforms_raw)
        backend_saved = max(0, int(getattr(compressed, "tokens_saved", 0) or 0))
        return RequestCompressionResult(
            messages=final,
            backend="headroom",
            estimated_tokens_before=tokens_before,
            estimated_tokens_after=tokens_after,
            estimated_tokens_saved=max(0, tokens_before - tokens_after),
            backend_reported_tokens_saved=backend_saved,
            changed_messages=changed_messages,
            protected_messages=len(protected),
            transforms=transforms,
        )


def request_context_compressor_from_env(
    env: Mapping[str, str] | None = None,
) -> RequestContextCompressor:
    values = os.environ if env is None else env
    backend = values.get("LEMONCROW_CONTEXT_COMPRESSOR", "none").strip().lower()
    if backend in {"", "none", "off", "disabled"}:
        return NoopRequestContextCompressor()
    if backend == "headroom":
        try:
            importlib.import_module("headroom")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "LEMONCROW_CONTEXT_COMPRESSOR=headroom requires Headroom; "
                "install it alongside LemonCrow with: pip install headroom-ai"
            ) from exc
        return HeadroomRequestContextCompressor()
    raise ValueError(f"unknown LEMONCROW_CONTEXT_COMPRESSOR={backend!r}; expected 'none' or 'headroom'")


def _tool_names_by_call_id(messages: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function")
            if not call_id or not isinstance(function, dict):
                continue
            result[call_id] = str(function.get("name") or "")
    return result


def _protected_message_indexes(
    messages: list[dict[str, Any]],
    tool_names: Mapping[str, str],
) -> set[int]:
    protected: set[int] = set()
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            protected.add(index)
            continue
        tool_call_id = str(message.get("tool_call_id") or "")
        if _is_protected_tool(tool_names.get(tool_call_id, "")):
            protected.add(index)
    return protected


def _is_protected_tool(tool_name: str) -> bool:
    lowered = tool_name.strip().lower()
    if not lowered:
        return True
    base = lowered.rsplit("__", 1)[-1]
    return base in _PROTECTED_TOOL_NAMES


def _spill_safe_tool_name(tool_name: str) -> str:
    base = tool_name.strip().lower().rsplit("__", 1)[-1] or "tool"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in base)
    return safe[:48] or "tool"


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    rendered = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    rendered = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, len(rendered) // 4)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _optional_float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(1.0, max(0.01, value))
