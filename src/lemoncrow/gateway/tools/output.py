"""Transport-independent tool output bounding, compaction, and spill policy."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.notices import spill_notice
from lemoncrow_client.kit.read_path import split_read_range_suffix

from lemoncrow.core.environment import bool_env, tool_output_spill_enabled

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_WIRE_BYTES = 14 * 1024 * 1024
DEFAULT_COMPACT_RESULT_CHARS = 256 * 1024
DEFAULT_SPILL_RESULT_CHARS = 2 * 1024
HOST_INLINE_RESULT_CHARS = 50 * 1024
SPILL_RESULT_CHARS_BY_TOOL = {
    "bash": 8 * 1024,
    "code_search": 20 * 1024,
}

SPILL_TOOLS = frozenset({"bash", "code_search", "sql", "read", "web_fetch", "mcp"})
SPILL_CHAR_CAP_TOOLS = frozenset({"bash", "code_search", "sql", "mcp"})
REWRITE_SPILL_IDENTITY = {
    "read": "read",
    "read_range": "read",
    "web_fetch": "web_fetch",
}
CODE_CONTENT_TOOLS = frozenset({"read"})


def trimmed_tokens_saved(pre_chars: int, post_chars: int) -> int:
    """Credit only prompt bytes the host would actually have inlined."""
    return max(0, min(pre_chars, HOST_INLINE_RESULT_CHARS) - post_chars) // 4


def max_result_bytes() -> int:
    raw = os.environ.get("LEMONCROW_MCP_MAX_RESULT_BYTES", str(DEFAULT_MAX_RESULT_BYTES))
    try:
        configured = int(raw)
    except ValueError:
        logger.warning(
            "invalid LEMONCROW_MCP_MAX_RESULT_BYTES=%r; using %d",
            raw,
            DEFAULT_MAX_RESULT_BYTES,
        )
        return DEFAULT_MAX_RESULT_BYTES
    # Floor avoids pathological tiny caps; ceiling keeps capped results safely
    # under the hard wire limit even after JSON string-escaping inflation.
    return max(64 * 1024, min(configured, MAX_WIRE_BYTES - 1024 * 1024))


def truncate_result_text(text: str | bytes, limit: int, tool_name: str | None = None) -> str:
    """Bound a tool-result string to *limit* UTF-8 bytes, appending a notice.

    A single oversized result would otherwise serialize into one JSON-RPC frame
    larger than the host's stdout guard, which disconnects the whole server.

    This is the last-resort backstop: tools in ``SPILL_TOOLS`` are already
    spilled by ``spill_oversized_result_text`` before this runs, so this
    mainly fires for everything else. When T7 spill is enabled and *tool_name*
    is given, the full pre-truncation text is persisted here too, so the footer
    names a recoverable path instead of the bare spill-failed shape.
    """
    if isinstance(text, bytes):
        # Defensive: this is the last-resort backstop for arbitrary tool output --
        # normalize once here so `.encode("utf-8")` below never raises on bytes
        # handed in despite the str contract.
        text = text.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    record = None
    if tool_name and output_spill_enabled():
        record = tool_output_spill.spill(text, tool_name=tool_name, kind="original")
    notice = "\n\n" + spill_notice(
        verb="truncated",
        original_chars=len(text),
        kept_chars=limit,
        path=record.path if record is not None else None,
    )
    headroom = max(0, limit - len(notice.encode("utf-8")))
    head = encoded[:headroom].decode("utf-8", "ignore")
    return head + notice


def compact_result_chars() -> int:
    """Char threshold above which an oversized tool result is head+tail compacted.

    Distinct from the multi-MB wire guard (``max_result_bytes``): that one only
    keeps the JSON-RPC frame under the host's stdout limit. This is a context-
    hygiene bound -- a single runaway result otherwise floods the host prompt and
    the host re-pays for it on every later turn. Set
    ``LEMONCROW_MCP_COMPACT_RESULT_CHARS=0`` to disable.
    """
    raw = os.environ.get("LEMONCROW_MCP_COMPACT_RESULT_CHARS", str(DEFAULT_COMPACT_RESULT_CHARS))
    try:
        configured = int(raw)
    except ValueError:
        logger.warning(
            "invalid LEMONCROW_MCP_COMPACT_RESULT_CHARS=%r; using %d",
            raw,
            DEFAULT_COMPACT_RESULT_CHARS,
        )
        return DEFAULT_COMPACT_RESULT_CHARS
    return max(0, configured)


def spill_result_chars(tool_name: str | None = None) -> int:
    """Strict returned-char cap for recoverably spilled tool outputs.

    The full original is persisted before the bounded summary is emitted, so this
    limit never discards output and does not affect tools without spill support.
    Resolution order: an explicit ``LEMONCROW_MCP_SPILL_RESULT_CHARS`` env value
    wins for every tool (set it to ``0`` to disable the char-gated cap while
    retaining the multi-MB wire backstop); otherwise a per-tool override from
    ``SPILL_RESULT_CHARS_BY_TOOL`` (e.g. bash gets a larger inline budget);
    otherwise ``DEFAULT_SPILL_RESULT_CHARS``.
    """
    raw = os.environ.get("LEMONCROW_MCP_SPILL_RESULT_CHARS")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning(
                "invalid LEMONCROW_MCP_SPILL_RESULT_CHARS=%r; using per-tool defaults",
                raw,
            )
    if tool_name is not None:
        return SPILL_RESULT_CHARS_BY_TOOL.get(tool_name, DEFAULT_SPILL_RESULT_CHARS)
    return DEFAULT_SPILL_RESULT_CHARS


def compact_result_text(text: str | bytes, tool_name: str) -> str:
    """Head+tail compact a single oversized tool result before it reaches the host.

    Deterministic (no LLM) so identical calls yield identical bytes and never
    bust the host's prefix cache. Keeps the head (command, first error, initial
    context) and the tail (final status/return value) with an omission marker in
    between, then appends a recovery hint. Results within the threshold pass
    through untouched.

    This is the generic backstop for tools OUTSIDE ``SPILL_TOOLS`` -- those get
    spilled earlier (``spill_oversized_result_text``) and never reach here at
    full size. When T7 spill is enabled the full pre-compaction text is
    persisted here too, so the recovery hint names a path instead of just
    "narrow the query" regardless of which tool produced the result.
    """
    if isinstance(text, bytes):
        # Defensive: generic backstop for arbitrary tool output -- normalize once
        # here so `.encode("utf-8")` below never raises on bytes handed in
        # despite the str contract.
        text = text.decode("utf-8", errors="replace")
    threshold = compact_result_chars()
    if threshold <= 0:
        return text
    # Gate on bytes, not just chars: a multibyte/CJK result can sit under the
    # char threshold while its UTF-8 footprint is several times larger and would
    # still flood the host prompt. len(text) is a lower bound on bytes, so only
    # pay the full encode when chars are under but bytes might exceed (x4 worst case).
    _over = len(text) > threshold
    if not _over and len(text) * 4 > threshold:
        _over = len(text.encode("utf-8")) > threshold
    if not _over:
        return text
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill
    from lemoncrow.pro.capabilities.tool_supervision.compact_output import compress_tool_output

    target = max(4096, threshold // 4)
    head = int(target * 0.7)
    tail = max(1, target - head)
    compacted = compress_tool_output(text, threshold_chars=target, head_chars=head, tail_chars=tail)
    record = tool_output_spill.spill(text, tool_name=tool_name, kind="original") if output_spill_enabled() else None
    footer = spill_notice(
        verb="compacted",
        original_chars=len(text),
        kept_chars=len(compacted),
        path=record.path if record is not None else None,
    )
    return f"{compacted}\n\n{footer}"


def effective_spill_tool(tool_name: str, args: dict[str, Any]) -> str:
    """Spill identity for a call: a bash command rewritten to another tool spills
    AS that tool (its budget + semantics); everything else spills as itself."""
    if tool_name != "bash":
        return tool_name
    command = str(args.get("command") or "").strip() if isinstance(args, dict) else ""
    if not command:
        return tool_name
    try:
        from lemoncrow.pro.capabilities.tool_supervision.bash_exec import classify_command

        decision = classify_command(command)
    except Exception:
        return tool_name
    if decision.action == "rewrite" and decision.rewrite_target:
        return REWRITE_SPILL_IDENTITY.get(decision.rewrite_target, tool_name)
    return tool_name


def output_spill_enabled() -> bool:
    """T7 flag: spill oversized output instead of discarding the overflow."""
    return tool_output_spill_enabled()


def auto_compact_output_enabled() -> bool:
    """T8 flag: auto-apply compact_output.compact() to oversized results."""
    return bool_env("LEMONCROW_AUTO_COMPACT_OUTPUT", False)


def read_path_arg(args: dict[str, Any]) -> str:
    """Best-effort extraction of the path a read-style call targeted."""
    raw = args.get("path") if isinstance(args, dict) else None
    if isinstance(raw, str) and raw:
        # A read path may carry a ':Lx-Ly' line-range suffix; strip it.
        return split_read_range_suffix(raw)[0]
    return ""


def is_spill_path(path: str) -> bool:
    """True when ``path`` resolves to a file inside the shared spill directory."""
    from lemoncrow.pro.capabilities.tool_supervision.tool_output_spill import _spill_dir

    try:
        return Path(path).resolve().parent == _spill_dir().resolve()
    except (ValueError, OSError):
        return False


def auto_compact_result_text(text: str, tool_name: str, args: dict[str, Any]) -> str:
    """T8 — auto-apply compaction to an oversized result, reversibly.

    AST/structure-aware for code (uses the read-side source-projection compact so
    the projected view stays line-diffable); falls back to the deterministic
    head+tail compaction (``compact_output.compact``) for everything else.

    REVERSIBLE: the untransformed original is written to the T7 spill store and a
    recovery hint naming ``read <path>`` is appended, so the dropped detail is
    never lost. Flag-gated by ``LEMONCROW_AUTO_COMPACT_OUTPUT`` (off -> returns
    ``text`` unchanged).
    """
    if not auto_compact_output_enabled():
        return text
    threshold = compact_result_chars()
    if threshold <= 0 or len(text) <= threshold:
        return text

    from lemoncrow.pro.capabilities.tool_supervision import compact_output, tool_output_spill

    # 16x reduction from the char threshold: first chars/4 to estimate tokens
    # (the ~4-chars-per-token rule), then /4 again for headroom so the compacted
    # view lands well under the threshold. e.g. 256K chars -> ~16K-token budget.
    budget_tokens = max(256, threshold // 4 // 4)
    compacted_text = text
    method = "compact_output"

    # AST-aware path for code reads: project the source to its compact view.
    # source_projection is a free feature (see licensing/features.py); the
    # has_feature check below always passes but stays as the single seam if
    # that ever changes.
    lang = ""
    from lemoncrow.core.capabilities import feature_access as _licensing

    if tool_name in CODE_CONTENT_TOOLS and _licensing.has_feature("source_projection"):
        with contextlib.suppress(Exception):
            from lemoncrow.infra.code_intel.languages import language_for_path
            from lemoncrow.pro.capabilities.source_projection import build_compact_projection

            lang_record = language_for_path(read_path_arg(args))
            if lang_record is not None:
                lang = lang_record.name
                projection = build_compact_projection(text, lang)
                # Char-based gate (we budget in chars here): the projection is
                # token-neutral on pure whitespace but still trims bytes, which
                # is what shrinks the host-prompt footprint.
                if len(projection.content) < len(text):
                    compacted_text = projection.content
                    method = f"projection:{lang}"

    if compacted_text is text:
        compacted = compact_output.compact(
            text,
            content_type="file" if tool_name in CODE_CONTENT_TOOLS else "tool_output",
            budget_tokens=budget_tokens,
        )
        compacted_text = compacted.compacted

    if len(compacted_text) >= len(text):
        return text  # compaction did not help — leave the original untouched.

    record = tool_output_spill.spill(
        text,
        tool_name=tool_name,
        kind="original",
    )
    if record is None:
        # Could not preserve the original -> do NOT lossily compact; return as-is
        # so the downstream wire guard handles it rather than dropping detail
        # irreversibly.
        return text
    footer = spill_notice(
        verb=f"compacted:{method}",
        original_chars=len(text),
        kept_chars=len(compacted_text),
        path=record.path,
    )
    return f"{compacted_text}\n\n{footer}"


def spill_oversized_result_text(
    text: str | bytes,
    tool_name: str,
    args: dict[str, Any],
    limit: int,
    *,
    unit: str = "bytes",
    tools: frozenset[str] = SPILL_TOOLS,
) -> str:
    """T7 — spill an over-budget result instead of discarding the overflow.

    When a ``bash``/``sql``/``read``/``web_fetch`` result exceeds the budget, the
    legacy path truncates/compacts and the middle is *lost*. Here the full,
    UNTRANSFORMED payload is written to the spill store as plain text and the
    host-facing text becomes a head/tail summary + the path + a ``read`` hint,
    so the agent can pull the rest back without re-running the tool. If the
    target of a ``read`` call is itself a spill file, re-spilling is skipped and
    normal truncation applies instead — no recursive spill chain.

    M1 — the gate ``unit`` selects the budget basis: ``"chars"`` (compared
    against ``len(text)``) lets the spill fire at the legacy char threshold
    (``compact_result_chars``), i.e. BEFORE ``compact_result_text`` would have
    dropped the middle; ``"bytes"`` keeps the original wire-byte semantics.

    Enabled by default, explicitly disabled with ``LEMONCROW_TOOL_OUTPUT_SPILL=0``,
    and limited to ``tools`` (default ``SPILL_TOOLS``; the char-gated call site
    passes ``SPILL_CHAR_CAP_TOOLS`` to exempt ``read``). Off / ineligible tools
    -> returns ``text`` unchanged so the caller's existing compaction/truncation
    runs exactly as before.
    """
    if isinstance(text, bytes):
        # Defensive: T7 spill backstop for arbitrary tool output -- normalize once
        # here so `.encode("utf-8")` below never raises on bytes handed in
        # despite the str contract.
        text = text.decode("utf-8", errors="replace")
    if not output_spill_enabled() or tool_name not in tools:
        return text
    # Don't re-spill a read that targets an already-spilled file: let normal
    # truncation apply so there is no recursive spill chain.
    if tool_name == "read" and is_spill_path(read_path_arg(args)):
        return text
    measured = len(text) if unit == "chars" else len(text.encode("utf-8"))
    if limit <= 0 or measured <= limit:
        return text

    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill
    from lemoncrow.pro.capabilities.tool_supervision.compact_output import compress_tool_output

    record = tool_output_spill.spill(
        text,
        tool_name=tool_name,
        kind="tool_output",
    )
    if record is None:
        return text  # spill failed -> fall back to the legacy compaction/truncation.

    # A compact head+tail summary. summary_with_ref applies the final strict cap
    # after reserving room for the recovery path and instructions.
    summary_budget = limit if unit == "chars" else limit // 8
    target = max(256, min(summary_budget, 16384))
    head = int(target * 0.7)
    tail = max(1, target - head)
    summary = compress_tool_output(text, threshold_chars=target, head_chars=head, tail_chars=tail)
    return tool_output_spill.summary_with_ref(
        summary,
        record,
        original_chars=len(text),
        verb="shrunk",
        max_chars=limit if unit == "chars" else None,
    )


__all__ = [
    "DEFAULT_COMPACT_RESULT_CHARS",
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_SPILL_RESULT_CHARS",
    "HOST_INLINE_RESULT_CHARS",
    "MAX_WIRE_BYTES",
    "REWRITE_SPILL_IDENTITY",
    "SPILL_CHAR_CAP_TOOLS",
    "SPILL_RESULT_CHARS_BY_TOOL",
    "SPILL_TOOLS",
    "auto_compact_output_enabled",
    "auto_compact_result_text",
    "compact_result_chars",
    "compact_result_text",
    "effective_spill_tool",
    "is_spill_path",
    "max_result_bytes",
    "output_spill_enabled",
    "read_path_arg",
    "spill_oversized_result_text",
    "spill_result_chars",
    "trimmed_tokens_saved",
    "truncate_result_text",
]
