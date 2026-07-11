"""Remote HTTP SDK client."""

from __future__ import annotations

import urllib.parse
from typing import Any

from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.core.foundation.models import (
    Playbook,
    RescueResult,
    Rubric,
    RubricResult,
    Trace,
    TraceLearning,
    TraceStatus,
    ValidationResult,
)
from lemoncrow.gateway.adapters import remote_client as service_remote_client
from lemoncrow.gateway.sdk.client import (
    ContextResult,
    EvalRunResult,
    LemonCrowClient,
    LessonDecisionResult,
    LessonInboxResult,
    MemoryArchiveResult,
    MemoryRecallResult,
    MemoryUpsertBlockResult,
    SavingsSummary,
    TraceRecordResult,
)
from lemoncrow.gateway.trace_payloads import serialize_trace_learnings, serialize_validation_results


class RemoteClient(LemonCrowClient):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = service_remote_client.RemoteClient(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        super().__init__()

    def _ensure_ok(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("ok") is False:
            detail = payload.get("detail") or payload.get("error") or "remote request failed"
            raise RuntimeError(str(detail))
        return payload

    def get_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
        errors: list[str] | None = None,
        max_blocks: int = 5,
        token_budget: int | None = 2000,
        dedup: bool = True,
        include_telemetry: bool = False,
        agent_id: str | None = None,
        recall: bool = True,
    ) -> ContextResult:
        payload = self._ensure_ok(
            self._client.get_context(
                {
                    "task": task,
                    "domain": domain,
                    "files": files or [],
                    "tools": tools or [],
                    "errors": errors or [],
                    "max_blocks": max_blocks,
                    "token_budget": token_budget,
                    "dedup": dedup,
                    "include_telemetry": include_telemetry,
                    "agent_id": agent_id,
                    "recall": recall,
                }
            )
        )
        return ContextResult.model_validate(payload)

    def rescue_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> RescueResult:
        payload = self._ensure_ok(
            self._client.rescue_failure(
                {
                    "task": task,
                    "error": error,
                    "domain": domain,
                    "files": files or [],
                    "recent_actions": recent_actions or [],
                }
            )
        )
        payload = {
            "rescue": str(payload.get("rescue") or ""),
            "matched_blocks": list(payload.get("matched_blocks") or []),
        }
        return RescueResult.model_validate(payload)

    def run_rubric_gate(self, *, rubric_id: str, checks: dict[str, bool | None]) -> RubricResult:
        payload = self._ensure_ok(self._client.run_rubric_gate({"rubric_id": rubric_id, "checks": checks}))
        return RubricResult.model_validate(payload)

    def record_trace(
        self,
        *,
        agent: str,
        domain: str,
        task: str,
        status: TraceStatus,
        files_touched: list[str | dict[str, Any]] | None = None,
        commands_run: list[str | dict[str, Any]] | None = None,
        tools_called: list[dict[str, Any]] | None = None,
        errors_seen: list[str] | None = None,
        diff_summary: str = "",
        output_summary: str = "",
        validation_results: list[ValidationResult] | None = None,
        learnings: list[str | dict[str, Any] | TraceLearning] | None = None,
    ) -> TraceRecordResult:
        serialized_learnings = serialize_trace_learnings(learnings)
        payload = self._ensure_ok(
            self._client.record_trace(
                {
                    "agent": agent,
                    "domain": domain,
                    "task": task,
                    "status": status,
                    "files_touched": files_touched or [],
                    "commands_run": commands_run or [],
                    "tools_called": tools_called or [],
                    "errors_seen": errors_seen or [],
                    "diff_summary": diff_summary,
                    "output_summary": output_summary,
                    "validation_results": serialize_validation_results(validation_results),
                    "learnings": serialized_learnings,
                }
            )
        )
        trace_id = str(payload.get("id") or payload.get("trace_id") or payload.get("session_id") or "")
        if not trace_id:
            raise RuntimeError("remote record_trace returned no trace id")
        return TraceRecordResult.model_validate({"id": trace_id})

    def get_savings(self) -> SavingsSummary:
        payload = self._ensure_ok(self._client._get("/v1/savings/summary"))
        return SavingsSummary.model_validate(payload)

    def lesson_inbox(self, *, domain: str | None = None, limit: int = 25) -> LessonInboxResult:
        payload = self._ensure_ok(self._client.lesson_inbox({"domain": domain, "limit": limit}))
        return LessonInboxResult.model_validate(payload)

    def lesson_decide(
        self,
        *,
        lesson_id: str,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> LessonDecisionResult:
        payload = self._ensure_ok(
            self._client.lesson_decide(
                {
                    "lesson_id": lesson_id,
                    "decision": decision,
                    "reviewer": reviewer,
                    "reason": reason,
                }
            )
        )
        return LessonDecisionResult.model_validate(payload)

    def memory_upsert_block(
        self,
        *,
        agent_id: str,
        label: str,
        value: str,
        limit_chars: int = 8000,
        description: str = "",
        read_only: bool = False,
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
        actor: str | None = None,
    ) -> MemoryUpsertBlockResult:
        payload = self._ensure_ok(
            self._client._post(
                "/v1/memory/blocks",
                {
                    "agent_id": agent_id,
                    "label": label,
                    "value": value,
                    "limit_chars": limit_chars,
                    "description": description,
                    "read_only": read_only,
                    "pinned": pinned,
                    "metadata": metadata or {},
                    "expected_version": expected_version,
                    "actor": actor,
                },
            )
        )
        return MemoryUpsertBlockResult.model_validate(payload)

    def memory_get_block(self, *, agent_id: str, label: str) -> MemoryBlock | None:
        payload = self._ensure_ok(
            self._client._get(
                f"/v1/memory/blocks?agent_id={urllib.parse.quote(agent_id)}&label={urllib.parse.quote(label)}"
            )
        )
        return MemoryBlock.model_validate(payload) if payload else None

    def memory_archive(
        self,
        *,
        agent_id: str,
        text: str,
        source: str,
        source_ref: str = "",
        tags: list[str] | None = None,
    ) -> MemoryArchiveResult:
        payload = self._ensure_ok(
            self._client._post(
                "/v1/memory/archive",
                {
                    "agent_id": agent_id,
                    "text": text,
                    "source": source,
                    "source_ref": source_ref,
                    "tags": tags or [],
                },
            )
        )
        return MemoryArchiveResult.model_validate(payload)

    def memory_recall(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> MemoryRecallResult:
        payload = self._ensure_ok(
            self._client._post(
                "/v1/memory/recall",
                {
                    "agent_id": agent_id,
                    "query": query,
                    "top_k": top_k,
                    "tags": tags or [],
                    "since": since,
                },
            )
        )
        return MemoryRecallResult.model_validate(payload)

    def _list_playbooks(
        self,
        *,
        domain: str | None = None,
        include_deprecated: bool = False,
    ) -> list[Playbook]:
        items = self._client._get("/blocks")
        blocks: list[Playbook] = [Playbook.model_validate(item) for item in items] if isinstance(items, list) else []
        if domain is not None:
            blocks = [block for block in blocks if block.domain == domain]
        if include_deprecated:
            return blocks
        return [block for block in blocks if block.status == "active"]

    def _search_playbooks(self, *, query: str, limit: int = 20) -> list[Playbook]:
        items = self._client._get("/blocks")
        blocks: list[Playbook] = [Playbook.model_validate(item) for item in items] if isinstance(items, list) else []
        needle = query.lower()
        matched = [block for block in blocks if needle in block.title.lower() or needle in block.situation.lower()]
        return matched[:limit]

    def _get_playbook(self, block_id: str) -> Playbook | None:
        payload = self._client._get(f"/blocks/{urllib.parse.quote(block_id)}")
        if isinstance(payload, dict) and payload.get("id"):
            return Playbook.model_validate(payload)
        return None

    def _list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        suffix = f"?domain={urllib.parse.quote(domain)}" if domain else ""
        items = self._client._get(f"/v1/rubrics{suffix}")
        return [Rubric.model_validate(item) for item in items] if isinstance(items, list) else []

    def _get_rubric(self, rubric_id: str) -> Rubric | None:
        payload = self._client._get(f"/v1/rubrics/{urllib.parse.quote(rubric_id)}")
        if isinstance(payload, dict) and payload.get("id"):
            return Rubric.model_validate(payload)
        return None

    def _get_trace(self, trace_id: str) -> Trace | None:
        payload = self._client._get(f"/v1/traces/{urllib.parse.quote(trace_id)}")
        if isinstance(payload, dict) and payload.get("id"):
            return Trace.model_validate(payload)
        return None

    def _list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        params: list[str] = []
        if domain:
            params.append(f"domain={urllib.parse.quote(domain)}")
        if status:
            params.append(f"status={urllib.parse.quote(status)}")
        params.append(f"limit={limit}")
        suffix = f"?{'&'.join(params)}" if params else ""
        payload = self._client._get(f"/traces{suffix}")
        return [Trace.model_validate(item) for item in payload] if isinstance(payload, list) else []

    def _list_evals(self, *, domain: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("evals are not available over the remote service API")

    def _run_evals(
        self,
        *,
        case_id: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> EvalRunResult:
        raise NotImplementedError("evals are not available over the remote service API")
