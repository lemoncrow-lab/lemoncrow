"""Transport-neutral finalization lifecycle for executed LemonCrow tools."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.gateway.tools.dedup import MCP_DEDUP_TOOLS, dedup_output, read_dedup_resource
from lemoncrow.gateway.tools.errors import ToolProtocolError, classify_tool_exception, execution_error_payload
from lemoncrow.gateway.tools.presentation import (
    apply_requested_output_format,
    assemble_response_text,
    bound_tool_output,
)
from lemoncrow.gateway.tools.results import clean_tool_result
from lemoncrow.gateway.tools.state import tool_call_images, tool_call_raw_result
from lemoncrow.infra.runtime.run_ledger import RunLedger


@dataclass(frozen=True, slots=True)
class ToolLifecycleHooks:
    get_ledger: Callable[..., Any]
    make_outcome_writer: Callable[..., Any]
    append_live_savings_event: Callable[..., Any]
    append_debug_event: Callable[..., Any]
    append_tool_profile: Callable[..., Any]
    scrub_args_for_debug: Callable[..., Any]
    process_tool_accounting: Callable[..., Any]
    record_context_budget: Callable[..., Any]
    loop_review_enabled: Callable[..., Any]
    loop_nudge_for_call: Callable[..., Any]
    write_statusline_sidecar: Callable[..., Any]
    debug_enabled: Callable[..., Any]
    append_workspace_savings: Callable[..., Any]
    spill: Callable[..., Any]
    root: Callable[..., Any]
    coerce_saved_tokens: Callable[..., Any]
    extract_tokens_saved: Callable[..., Any]
    renderer: Callable[..., Any]
    read_tools: frozenset[str]


@dataclass(slots=True)
class ToolCallLifecycle:
    rid: Any
    name: str
    args: dict[str, Any]
    remote_routed: bool
    call_started: float
    duration_ms: int = 0
    ledger: RunLedger | None = None
    hooks: ToolLifecycleHooks | None = None

    def _hooks(self) -> ToolLifecycleHooks:
        if self.hooks is None:
            raise RuntimeError("tool lifecycle hooks are not configured")
        return self.hooks

    def record_error(self, exc: Exception) -> None:
        hooks = self._hooks()
        rid = self.rid
        name = self.name
        args = self.args
        remote_routed = self.remote_routed
        _call_duration_ms = self.duration_ms
        _call_started = self.call_started
        logging.exception("Recovered from broad exception handler")
        if not remote_routed:
            with contextlib.suppress(Exception):
                from lemoncrow.pro.runtime import outcome_capture

                led = hooks.get_ledger()
                outcome_capture.advance(
                    led.session_id,
                    tool_name=name,
                    is_error=True,
                    is_env_error=isinstance(exc, (OSError, IOError)),
                    writer=hooks.make_outcome_writer(led),
                )
            _err_session_id = getattr(hooks.get_ledger(), "session_id", "") or ""
            with contextlib.suppress(Exception):
                hooks.append_live_savings_event(
                    {
                        "kind": "tool_call",
                        "tool": name,
                        "status": "error",
                        "error": type(exc).__name__,
                        "duration_ms": _call_duration_ms,
                        "session_id": _err_session_id,
                        "ts": time.time(),
                    }
                )
            with contextlib.suppress(Exception):
                hooks.append_debug_event(
                    tool=name,
                    args=args if isinstance(args, dict) else {},
                    duration_ms=_call_duration_ms,
                    response_size=0,
                    status="error",
                    error=type(exc).__name__,
                    session_id=_err_session_id,
                    rid=str(rid) if rid is not None else None,
                )
            with contextlib.suppress(Exception):
                hooks.append_tool_profile(
                    tool=name,
                    handler_ms=_call_duration_ms,
                    total_ms=round((time.perf_counter() - _call_started) * 1000),
                    response_size=0,
                    status="error",
                    error=type(exc).__name__,
                    session_id=_err_session_id,
                )
            with contextlib.suppress(Exception):
                from lemoncrow.gateway.integrations.langfuse import emit_tool_call as _lf_emit_tool

                _lf_emit_tool(
                    tool=name,
                    args=hooks.scrub_args_for_debug(args) if isinstance(args, dict) else {},
                    duration_ms=_call_duration_ms,
                    response_size=0,
                    status="error",
                    error=type(exc).__name__,
                    session_id=_err_session_id,
                )

    def finalize_error_payload(self, exc: Exception) -> dict[str, Any]:
        self.record_error(exc)
        disposition = classify_tool_exception(exc)
        if disposition.protocol_code is not None:
            raise ToolProtocolError(disposition.protocol_code, disposition.message)
        return execution_error_payload(exc)

    def finalize_payload(self, result: dict[str, Any] | Any) -> dict[str, Any]:
        hooks = self._hooks()
        rid = self.rid
        name = self.name
        args = self.args
        remote_routed = self.remote_routed
        _call_duration_ms = self.duration_ms
        _call_started = self.call_started
        led = self.ledger
        if not remote_routed and led is None:
            led = hooks.get_ledger()
            self.ledger = led
        try:
            rendered_text: str | None = None
            _loop_note: str | None = None
            _args = args if isinstance(args, dict) else {}
            _calls_saved_credit = 0
            if isinstance(result, dict):
                result = clean_tool_result(result, name)

            # Presentation is deployment-independent: hosted/remote and local
            # calls must give the model the same compact text. Only accounting
            # and local runtime side effects remain behind `remote_routed`.
            rendered_text = hooks.renderer(name, result)

            if not remote_routed:
                assert led is not None
                # Pull the internal `calls_saved` credit out of `result` NOW,
                # before the JSON-dump fallback below can serialize it verbatim.
                # `hooks.renderer` has already used it (e.g. edit's
                # silent-success check); anything that falls through to
                # `json.dumps(result, ...)` because rendered_text is falsy must
                # not still be carrying this model-invisible bookkeeping key.
                if isinstance(result, dict) and "calls_saved" in result:
                    _calls_saved_credit = hooks.coerce_saved_tokens(result.pop("calls_saved", None))

                # Spiral nudge: surface a soft note when the agent repeats an
                # identical tool call -- a narrow, false-positive-free
                # no-progress signal. Fail-open; never blocks.
                if hooks.loop_review_enabled():
                    with contextlib.suppress(Exception):
                        _loop_note = hooks.loop_nudge_for_call(name, _args)
                        if _loop_note and isinstance(result, dict):
                            # Carry the note as a field for JSON consumers; the
                            # response_text below appends it for the rendered view.
                            result.setdefault("loop_note", _loop_note)

                # Per-call savings accounting (read baseline de-dup + deferred
                # code-intel credit). Runs BEFORE budget recording so a zeroed
                # read saving flows into both the recorder and the `saved` field.
                # Local-handler path only; never touches the response bytes.
                hooks.process_tool_accounting(name, _args, result, rid)

                hooks.record_context_budget(
                    name,
                    _args,
                    led,
                    result if isinstance(result, dict) else {"result": result},
                    rendered_text_size=len(rendered_text) if rendered_text else None,
                )

                with contextlib.suppress(Exception):
                    from lemoncrow.pro.runtime import outcome_capture

                    outcome_capture.advance(
                        led.session_id,
                        tool_name=name,
                        is_error=False,
                        is_read_tool=name in hooks.read_tools,
                        writer=hooks.make_outcome_writer(led),
                    )

                _ok_session_id = getattr(hooks.get_ledger(), "session_id", "") or ""
                with contextlib.suppress(Exception):
                    hooks.append_live_savings_event(
                        {
                            "kind": "tool_call",
                            "tool": name,
                            "status": "ok",
                            "duration_ms": _call_duration_ms,
                            "session_id": _ok_session_id,
                            "ts": time.time(),
                        }
                    )
                # Refresh the statusline frames sidecar on EVERY dispatch
                # (not only savings-bearing calls) so the shell render's
                # 10s freshness gate keeps passing during active work and
                # the slow `lc savings --segment` subprocess fallback
                # never fires. Rate-limited internally to one write per 5s.
                with contextlib.suppress(Exception):
                    hooks.write_statusline_sidecar()

            response_text = assemble_response_text(result, rendered_text, _loop_note)

            # Only pay the full-payload UTF-8 encode when a telemetry sink will
            # consume the byte count; otherwise approximate with the O(1) char len.
            if hooks.debug_enabled():
                _ok_response_size = len(response_text.encode("utf-8", errors="replace"))
            else:
                _ok_response_size = len(response_text)
            _ok_sid = _ok_session_id if not remote_routed else (getattr(hooks.get_ledger(), "session_id", "") or "")
            with contextlib.suppress(Exception):
                hooks.append_debug_event(
                    tool=name,
                    args=args if isinstance(args, dict) else {},
                    duration_ms=_call_duration_ms,
                    response_size=_ok_response_size,
                    status="ok",
                    session_id=_ok_sid,
                    rid=str(rid) if rid is not None else None,
                )
            with contextlib.suppress(Exception):
                from lemoncrow.gateway.integrations.langfuse import emit_tool_call as _lf_emit_tool

                _lf_emit_tool(
                    tool=name,
                    args=hooks.scrub_args_for_debug(args) if isinstance(args, dict) else {},
                    duration_ms=_call_duration_ms,
                    response_size=_ok_response_size,
                    status="ok",
                    session_id=_ok_sid,
                )

            # Explicit caller formatting remains between telemetry-size capture
            # and within-session dedup, preserving the historical response order.
            response_text, _format_saved = apply_requested_output_format(args, result, response_text)

            # Within-session content dedup policy is shared with lc code; MCP
            # supplies its session/resource identity and keeps savings attribution here.
            _dedup_sid = ""
            with contextlib.suppress(Exception):
                _dedup_sid = hooks.get_ledger().session_id or ""
            _dedup_resource = ""
            if name == "read":
                with contextlib.suppress(Exception):
                    _dedup_resource = read_dedup_resource(_args)
            _dedup = dedup_output(
                name=name,
                args=_args,
                text=response_text,
                session_id=_dedup_sid,
                eligible_tools=MCP_DEDUP_TOOLS,
                salt=_dedup_resource if name == "read" else "",
                resource=_dedup_resource,
            )
            response_text = _dedup.text
            dedup_stubbed = _dedup.stubbed
            if _dedup.chars_saved > 0:
                with contextlib.suppress(Exception):
                    hooks.append_workspace_savings(name, _dedup.chars_saved // 4, 0, rid=str(rid))
            # Embed per-call savings on the content item so they also ride into
            # the Claude transcript JSONL. NOTE: this is a secondary record —
            # the live statusline/analytics source is the per-session sidecar
            # sessions/<id>/savings.jsonl written by hooks.append_workspace_savings
            # below, not the transcript.
            # Shape: {"tokens": int, "calls": int}. Either may be 0 but the
            # object is omitted entirely when both are 0.
            # First bound, for context hygiene: head+tail compact a single
            # runaway result so it can't flood the host prompt (which the host
            # re-pays for on every later turn). The legacy char-compaction path
            # (_compact_result_text) is deterministic and prefix-cache stable
            # across identical calls; the T7 spill summary below is intentionally
            # NOT -- it embeds a unique filename (timestamp+random), so two identical
            # capped-tool calls yield different host text (spill summaries are
            # <4096 chars and so never get the cache_control marker below anyway).
            # T8 (LEMONCROW_AUTO_COMPACT_OUTPUT, default off): auto-compact an
            # oversized result (AST-aware for code) while preserving the
            # untransformed original in the T7 spill store so it stays
            # reversible. Off -> returns response_text unchanged.
            response_text, _trim_saved = bound_tool_output(
                name,
                args,
                response_text,
                spill=hooks.spill,
            )
            # N4 — per-tool exact input/output token ledger. Runs HERE, after the
            # spill/compact/truncate bounds above, so output is measured against
            # the FINAL emitted text the host actually receives (a spilled summary,
            # not the pre-spill payload). Additive only -- never touches the
            # response bytes; best-effort so a write failure can't break the call.
            with contextlib.suppress(Exception):
                from lemoncrow.core.capabilities.tool_token_ledger import record_tool_tokens

                record_tool_tokens(
                    hooks.root(),
                    name,
                    input_payload=args,
                    output_payload=response_text,
                )
            content_item: dict[str, Any] = {
                "type": "text",
                "text": response_text,
            }
            # Best-effort cache hint, NOT a measured saving. Tag large results
            # so a host that honors MCP cache_control can checkpoint them for
            # prompt caching. Caveats kept honest on purpose: (1) we do not
            # verify the host actually forwards this; (2) the conversation
            # prefix is already auto-cached by the host, so the marginal gain
            # is small; (3) a cache *write* costs ~25% over input, so a one-off
            # large result that is never re-read pays the write premium for
            # nothing. The ≥4096-char floor (~1024 tokens) is Anthropic's
            # minimum cacheable size.
            # ...and skip it entirely for dedup-eligible tools (read): an exact
            # re-read is elided by the dedup pass below, so the marker can never
            # earn its cache-read payoff there and only risks a redundant
            # breakpoint on top of the host's automatic prefix caching.
            if len(response_text) >= 4096 and name not in MCP_DEDUP_TOOLS:
                content_item["cache_control"] = {"type": "ephemeral"}
            # When deduped, skip the original per-call savings (they'd otherwise be
            # credited against bytes we just elided).
            if not dedup_stubbed and isinstance(result, dict):
                saved_tokens = hooks.extract_tokens_saved(result) + _format_saved + _trim_saved
                saved_calls = _calls_saved_credit
                if saved_tokens > 0 or saved_calls > 0:
                    content_item["saved"] = {
                        "tokens": int(saved_tokens),
                        "calls": int(saved_calls),
                    }
                    hooks.append_workspace_savings(name, saved_tokens, saved_calls, rid=str(rid))

            response_payload: dict[str, Any] = {"content": [content_item]}
            # Attach image content blocks produced by a read of an image file so
            # the multimodal model receives the image itself alongside the text
            # metadata (the base64 never rides in response_text/JSON above).
            _pending_images = getattr(tool_call_images, "value", None)
            if isinstance(_pending_images, list) and _pending_images:
                response_payload["content"].extend(_pending_images)
                tool_call_images.value = []
            # Stash the full structured result for the in-process CLI so `tools call
            # ... --json` returns the dict for EVERY tool -- including the ones whose
            # host-facing content is rendered text (read, grep, search, shell, ...).
            # This never goes on response_payload, so the MCP host's main model only
            # ever sees `content`; no structured data rides the wire to any consumer.
            tool_call_raw_result.value = result if isinstance(result, dict) else None
            with contextlib.suppress(Exception):
                hooks.append_tool_profile(
                    tool=name,
                    handler_ms=_call_duration_ms,
                    total_ms=round((time.perf_counter() - _call_started) * 1000),
                    response_size=_ok_response_size,
                    status="ok",
                    session_id=_ok_sid,
                )
            return response_payload
        except Exception:
            raise


__all__ = ["ToolCallLifecycle", "ToolLifecycleHooks"]
