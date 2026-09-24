"""Trace MCP handler registration and persistence orchestration."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from lemoncrow.core.foundation.memory_models import ArchivalPassage
from lemoncrow.core.foundation.models import RawArtifact, Trace
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool

logger = logging.getLogger(__name__)

MAX_TRACE_FILES = int(os.environ.get("LEMONCROW_TRACE_MAX_FILES", "50"))
MAX_TRACE_FILE_BYTES = int(os.environ.get("LEMONCROW_TRACE_MAX_FILE_BYTES", str(1024 * 1024)))


@dataclass(frozen=True, slots=True)
class TraceHandlerHooks:
    runtime: Callable[[], Any]
    get_ledger: Callable[[], Any]
    get_realtime_context: Callable[[], Any]
    get_product_session_id: Callable[[], str]
    memory_store: Callable[[], Any]
    spawn_worker_if_idle: Callable[[Any], Any]
    lemoncrow_root: Callable[[], Any]
    max_trace_files: int
    max_trace_file_bytes: int


_HooksFactory = Callable[[], TraceHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_trace_handler_hooks(factory: _HooksFactory) -> None:
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> TraceHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("trace handler hooks are not configured")
    return factory()


def _runtime() -> Any:
    return _hooks().runtime()


def _get_ledger() -> Any:
    return _hooks().get_ledger()


def _get_realtime_context() -> Any:
    return _hooks().get_realtime_context()


def _get_product_session_id() -> str:
    return _hooks().get_product_session_id()


def _memory_store() -> Any:
    return _hooks().memory_store()


def _spawn_worker_if_idle(root: Any) -> Any:
    return _hooks().spawn_worker_if_idle(root)


def _lemoncrow_root() -> Any:
    return _hooks().lemoncrow_root()


@mcp_tool(name="trace")
def tool_record_trace(
    agent: str,
    domain: str,
    task: str,
    status: Literal["success", "failed", "partial"],
    errors_seen: list[str] | None = None,
    diff_summary: str = "",
    output_summary: str = "",
    tools_called: list[Any] | None = None,
    validation_results: list[Any] | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    host: str | None = None,
    trace_confidence: str | None = None,
    capture_sources: list[str] | None = None,
    missing_surfaces: list[str] | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    capture_files: list[str] | None = None,
    learnings: list[Any] | None = None,
) -> dict[str, Any]:
    """Record an observable trace of an agent run (status, diffs, tools, validations, learnings) to the run ledger.

    Call once when a task is done so outcomes and lessons persist for later recall.

    Returns: {trace_id, event_recorded}.
    """
    from lemoncrow_client.kit.redaction import redact, redact_list

    if tools_called is None:
        tools_called = []
    if validation_results is None:
        validation_results = []
    if errors_seen is None:
        errors_seen = []
    if capture_sources is None:
        capture_sources = []
    if missing_surfaces is None:
        missing_surfaces = []
    if event_payload is None:
        event_payload = {}
    if capture_files is None:
        capture_files = []
    if learnings is None:
        learnings = []
    rt = _runtime()
    led = _get_ledger()
    rtc = _get_realtime_context()

    def _redact_json_strings(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [_redact_json_strings(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _redact_json_strings(item) for key, item in value.items()}
        return value

    def _coerce_validation_passed(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"pass", "passed", "success", "successful", "ok", "true"}:
                return True
            if lowered in {"fail", "failed", "failure", "error", "errored", "false"}:
                return False
        return False

    def _normalize_validation_results(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("name") or item.get("check") or "validation"
                detail = item.get("detail") or item.get("output") or ""
                passed = item.get("passed")
                if passed is None:
                    passed = item.get("status")
                normalized.append(
                    {
                        "name": redact(str(name)),
                        "passed": _coerce_validation_passed(passed),
                        "detail": redact(str(detail)),
                    }
                )
                continue
            text = redact(str(item))
            lowered = text.lower()
            passed = not any(token in lowered for token in ("fail", "error", "not run"))
            normalized.append({"name": text, "passed": passed, "detail": ""})
        return normalized

    def _normalize_tool_calls(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"name": redact(item), "args_hash": "", "count": 1})
                continue
            if isinstance(item, dict):
                raw_count = item.get("count") or 1
                with contextlib.suppress(TypeError, ValueError):
                    raw_count = int(raw_count)
                if not isinstance(raw_count, int):
                    raw_count = 1
                tool_call: dict[str, Any] = {
                    "name": redact(str(item.get("name") or item.get("tool") or "unknown")),
                    "args_hash": redact(str(item.get("args_hash") or "")),
                    "count": raw_count,
                }
                if "args" in item:
                    tool_call["args"] = _redact_json_strings(item["args"])
                if isinstance(item.get("result_summary"), str):
                    tool_call["result_summary"] = redact(item["result_summary"])
                normalized.append(tool_call)
                continue
            normalized.append({"name": redact(str(item)), "args_hash": "", "count": 1})
        return normalized

    def _normalize_learnings(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                text = redact(item.strip())
                if text:
                    normalized.append({"kind": "note", "text": text})
                continue
            if not isinstance(item, dict):
                continue
            raw_text = (
                item.get("text")
                or item.get("learning")
                or item.get("lesson")
                or item.get("body")
                or item.get("summary")
                or ""
            )
            text = redact(str(raw_text).strip())
            if not text:
                continue
            entry: dict[str, Any] = {"text": text}
            if item.get("kind") is not None:
                entry["kind"] = redact(str(item["kind"]))
            if item.get("evidence") is not None:
                entry["evidence"] = redact(str(item["evidence"]))
            promote_to = item.get("promote_to")
            if promote_to is None:
                promote_to = item.get("target") or item.get("promotion_target")
            if promote_to is not None:
                entry["promote_to"] = redact(str(promote_to))
            normalized.append(entry)
        return normalized

    def _normalize_trace_confidence(value: Any) -> str | None:
        if value is None:
            return None
        normalized = redact(str(value)).strip().lower()
        if not normalized or normalized in {"none", "null", "unknown"}:
            return None
        if normalized in {"full_live", "mcp_live", "wrapper_live", "imported", "manual"}:
            return normalized
        if normalized in {"high", "medium", "low"}:
            # Legacy callers treated this field like a confidence strength rather
            # than a capture provenance. Preserve the trace conservatively.
            return "manual"
        return "manual"

    def _normalize_workflow_trace_payload(raw_event_type: str, raw_payload: dict[str, Any]) -> dict[str, Any] | None:
        normalized_type = redact(raw_event_type).strip().lower()
        payload = _redact_json_strings(raw_payload)
        if not isinstance(payload, dict):
            return None
        if normalized_type == "workflow_state":
            workflow_step = str(payload.get("workflow_step") or payload.get("current_step") or "").strip()
            session_phase = str(payload.get("session_phase") or "").strip()
            result: dict[str, Any] = {}
            if workflow_step:
                result["workflow_step"] = workflow_step
            if session_phase:
                result["session_phase"] = session_phase
            return result or None
        if normalized_type == "plan_review":
            review_decision = str(payload.get("review_decision") or payload.get("decision") or "").strip()
            plan_id = str(payload.get("plan_id") or "").strip()
            workflow_step = str(payload.get("workflow_step") or "").strip()
            result = {}
            if review_decision:
                result["review_decision"] = review_decision
            if plan_id:
                result["plan_id"] = plan_id
            if workflow_step:
                result["workflow_step"] = workflow_step
            return result or None
        if normalized_type == "task_progress":
            task_id = str(payload.get("task_id") or "").strip()
            workflow_step = str(payload.get("workflow_step") or "").strip()
            result = {}
            if task_id:
                result["task_id"] = task_id
            if workflow_step:
                result["workflow_step"] = workflow_step
            for key in ("completed_tasks", "remaining_tasks"):
                value = payload.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    result[key] = max(0, int(value or 0))
                except (TypeError, ValueError):
                    continue
            return result or None
        return None

    # Derive host label from agent string and environment
    def _derive_host(a: str) -> str:
        al = a.lower()
        if "antigravity" in al or "agy" in al or os.environ.get("ANTIGRAVITY_CLI") or os.environ.get("AGY_CLI"):
            return "antigravity"
        if "cursor" in al or os.environ.get("CURSOR_SESSION_ID") or os.environ.get("CURSOR_TRACE_ID"):
            return "cursor"
        if (
            "hermes" in al
            or os.environ.get("HERMES_HOME")
            or os.environ.get("HERMES_SESSION_ID")
            or os.environ.get("HERMES_CLI")
        ):
            return "hermes"
        if "copilot" in al or os.environ.get("COPILOT_CLI"):
            return "copilot"
        if "codex" in al or os.environ.get("CODEX_CLI"):
            return "codex"
        if (
            "opencode" in al
            or os.environ.get("OPENCODE_CLI")
            or os.environ.get("OPENCODE_SESSION_ID")
            or os.environ.get("LEMONCROW_AGENT", "") == "opencode"
        ):
            return "opencode"
        if "claude" in al or os.environ.get("CLAUDE_CODE"):
            return "claude"

        # Default to the agent name if no known host environment is detected
        return "lemoncrow" if al.startswith("lemoncrow:") else al

    normalized_capture_sources = [redact(str(source)) for source in capture_sources]
    normalized_trace_confidence = _normalize_trace_confidence(trace_confidence)
    normalized_missing_surfaces = redact_list([str(value) for value in missing_surfaces])
    if normalized_trace_confidence == "full_live" and not any(
        source in {"hooks", "live_hooks", "plugin_hooks"} for source in normalized_capture_sources
    ):
        normalized_trace_confidence = "mcp_live"
        if "hooks" not in normalized_missing_surfaces:
            normalized_missing_surfaces.append("hooks")

    payload: dict[str, Any] = {
        "agent": agent,
        "domain": domain,
        "task": redact(task),
        "status": status,
        "errors_seen": redact_list([str(v) for v in errors_seen]),
        "diff_summary": redact(diff_summary),
        "output_summary": redact(output_summary),
        "session_id": session_id or run_id or led.session_id,
        "host": redact(host) if host else _derive_host(agent),
        "trace_confidence": normalized_trace_confidence,
        "capture_sources": normalized_capture_sources,
        "missing_surfaces": normalized_missing_surfaces,
    }
    payload["tools_called"] = _normalize_tool_calls(tools_called)
    payload["validation_results"] = _normalize_validation_results(validation_results)
    payload["learnings"] = _normalize_learnings(learnings)

    raw_artifacts: list[str] = []
    pending_artifacts: list[tuple[RawArtifact, str]] = []
    if capture_files:
        source_session_id = (
            _get_product_session_id()
            or os.environ.get("CODEX_SESSION_ID")
            or os.environ.get("OPENCODE_SESSION_ID")
            or "unknown"
        )
        for fpath in capture_files[: _hooks().max_trace_files]:
            try:
                p = Path(fpath)
                if not p.is_file():
                    continue
                if p.stat().st_size > _hooks().max_trace_file_bytes:
                    logging.info(
                        "trace capture: skipping %s (%d bytes > cap %d; raise LEMONCROW_TRACE_MAX_FILE_BYTES)",
                        fpath,
                        p.stat().st_size,
                        _hooks().max_trace_file_bytes,
                    )
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
                # We redact secrets from files before capture for safety
                redacted_content = redact(content)
                digest = sha256(redacted_content.encode("utf-8", errors="replace")).hexdigest()

                # Use a stable but unique ID for the file artifact
                artifact_id = f"file-{sha256(fpath.encode()).hexdigest()[:12]}-{digest[:12]}"

                artifact = RawArtifact(
                    id=artifact_id,
                    source="mcp",
                    source_session_id=source_session_id,
                    kind="source.code",
                    relative_path=f"{artifact_id}.txt",
                    content_path=f"raw/mcp/{source_session_id}/{artifact_id}.txt",
                    sha256_original=sha256(content.encode()).hexdigest(),
                    sha256_redacted=digest,
                    byte_count_original=len(content.encode("utf-8")),
                    byte_count_redacted=len(redacted_content.encode("utf-8")),
                    redacted=True,
                    source_path=str(p.absolute()),
                    source_file_mtime=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
                )
                pending_artifacts.append((artifact, redacted_content))
                raw_artifacts.append(artifact_id)
            except Exception as e:
                logging.exception("Recovered from broad exception handler")
                logger.warning("Failed to capture context file %s: %s", fpath, e)

    if raw_artifacts:
        payload["raw_artifact_ids"] = raw_artifacts

    if "id" not in payload:
        payload["id"] = Trace.make_id(task, agent)

    # Validate BEFORE any store/ledger write so a malformed payload returns a
    # structured error instead of leaving committed artifacts / a workflow event
    # behind with no persisted trace (partial-write inconsistency).
    trace = Trace.model_validate(payload)

    for _artifact, _redacted_content in pending_artifacts:
        rt.store.history.record_raw_artifact(_artifact, _redacted_content)

    if event_type:
        normalized_event_payload = _normalize_workflow_trace_payload(event_type, event_payload)
        if normalized_event_payload is not None:
            led.record_workflow_event(event_type, normalized_event_payload)
        else:
            led.record("note", f"event:{redact(event_type)}", _redact_json_strings(event_payload))

    rt.store.history.record_trace(trace)
    from lemoncrow.pro.capabilities.lesson_promotion import ingest_failed_trace

    ingest_failed_trace(rt.store, trace)

    # Write learnings to archival memory (not Playbooks - those are curated).
    # Each learning is a short sentence the agent synthesises; stored deduped so
    # repeated identical insights across sessions don't accumulate noise.
    if trace.learnings:
        mem = _memory_store()
        for learning in trace.learnings:
            text = redact(learning.text.strip())
            if not text:
                continue
            dedup_hash = sha256(f"{agent}:{text}".encode()).hexdigest()[:32]
            passage = ArchivalPassage(
                agent_id=agent,
                text=text,
                source="trace",
                source_ref=trace.id,
                tags=["learning", domain, learning.kind],
                dedup_hash=dedup_hash,
            )
            with contextlib.suppress(Exception):
                mem.insert_passage(passage)

    led.close(status=status)
    led.persist()

    rtc.persist()

    # Emit to Langfuse if configured (fail-open)
    from lemoncrow.gateway.integrations.langfuse import emit_trace as _lf_emit

    _lf_emit(payload)

    # Kick off a background consolidation tick so knowledge blocks are extracted
    # from this trace without waiting for the daemon's next cycle. Throttled
    # (>=30s) so a burst of trace calls can't spawn a thread storm.
    _spawn_worker_if_idle(_lemoncrow_root())

    # Stable compact receipt.
    return {
        "trace_id": trace.id,
        "event_recorded": bool(event_type),
    }


__all__ = [
    "MAX_TRACE_FILES",
    "MAX_TRACE_FILE_BYTES",
    "TraceHandlerHooks",
    "configure_trace_handler_hooks",
    "tool_record_trace",
]
