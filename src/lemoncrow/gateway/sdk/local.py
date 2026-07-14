"""Local in-process SDK client."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.retrieval import Retriever
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
from lemoncrow.core.foundation.redaction import redact
from lemoncrow.core.foundation.rubric_gate import run_rubric
from lemoncrow.gateway.sdk.client import (
    ContextResult,
    EvalRecord,
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
from lemoncrow.infra.storage.factory import make_memory_store


def _evaluate_eval_payload(item: dict[str, Any]) -> EvalRecord:
    expected_status = str(item.get("expected_status", "pass"))
    actual_status = str(item.get("actual_status", expected_status))
    return EvalRecord(
        case_id=str(item.get("id", "unknown")),
        domain=str(item.get("domain", "unknown")),
        description=str(item.get("description", "")),
        expected_status=expected_status,
        actual_status=actual_status,
        passed=actual_status == expected_status,
    )


class LocalClient(LemonCrowClient):
    def __init__(
        self,
        *,
        root: str | Path | None = None,
        retriever_factory: Callable[[str | Path], Retriever] | None = None,
    ) -> None:
        if root is None:
            from lemoncrow.core.foundation.paths import default_store_root

            root = default_store_root()
        self.root = Path(root)
        from lemoncrow.gateway.adapters.runtime import ContextRuntime

        self.runtime = ContextRuntime(self.root, retriever_factory=retriever_factory)
        self.store = self.runtime.store
        super().__init__()

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
        payload = self.runtime.get_context(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
            errors=errors,
            max_blocks=max_blocks,
            token_budget=token_budget,
            dedup=dedup,
            include_telemetry=include_telemetry,
            agent_id=agent_id,
            recall=recall,
        )
        if isinstance(payload, dict):
            return ContextResult.model_validate(payload)
        return ContextResult(context=payload)

    def rescue_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> RescueResult:
        return self.runtime.rescue_failure(
            task=task,
            error=error,
            domain=domain,
            files=files,
            recent_actions=recent_actions,
        )

    def run_rubric_gate(self, *, rubric_id: str, checks: dict[str, bool | None]) -> RubricResult:
        rubric = self.store.knowledge.get_rubric(rubric_id)
        if rubric is None:
            raise KeyError(f"rubric not found: {rubric_id}")
        return run_rubric(rubric, checks)

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
        from lemoncrow.pro.capabilities.lesson_promotion import ingest_failed_trace

        trace = Trace.model_validate(
            {
                "id": Trace.make_id(task, agent),
                "agent": agent,
                "domain": domain,
                "task": task,
                "status": status,
                "files_touched": files_touched or [],
                "tools_called": tools_called or [],
                "commands_run": commands_run or [],
                "errors_seen": errors_seen or [],
                "diff_summary": diff_summary,
                "output_summary": output_summary,
                "validation_results": validation_results or [],
                "learnings": learnings or [],
            }
        )
        self.store.history.record_trace(trace)
        ingest_failed_trace(self.store, trace)
        return TraceRecordResult(id=trace.id)

    def get_savings(self) -> SavingsSummary:
        from lemoncrow.infra.runtime.cost_tracker import CostTracker

        return SavingsSummary.model_validate(CostTracker(self.root).total_savings())

    def lesson_inbox(self, *, domain: str | None = None, limit: int = 25) -> LessonInboxResult:
        from lemoncrow.pro.capabilities.lesson_promotion import LessonPromoterCapability

        promoter = LessonPromoterCapability(self.store)
        return LessonInboxResult(lessons=promoter.inbox(domain=domain, limit=limit))

    def lesson_decide(
        self,
        *,
        lesson_id: str,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> LessonDecisionResult:
        from lemoncrow.pro.capabilities.lesson_promotion import LessonPromoterCapability

        promoter = LessonPromoterCapability(self.store)
        payload = promoter.decide(
            lesson_id=lesson_id,
            decision=decision,
            reviewer=reviewer,
            reason=reason,
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
        store = make_memory_store(self.root)
        existing = store.get_block(agent_id, label)
        version = expected_version if expected_version is not None else (existing.version if existing else 1)
        seed = existing or MemoryBlock(agent_id=agent_id, label=label, value=value)
        block = MemoryBlock(
            id=seed.id,
            agent_id=agent_id,
            label=label,
            value=value,
            limit_chars=limit_chars,
            description=description,
            read_only=read_only,
            metadata=metadata or {},
            pinned=pinned,
            version=version,
            current_history_id=existing.current_history_id if existing else None,
            created_at=seed.created_at,
        )
        stored = store.upsert_block(block, actor=actor or f"agent:{agent_id}")
        return MemoryUpsertBlockResult(id=stored.id, version=stored.version)

    def memory_get_block(self, *, agent_id: str, label: str) -> MemoryBlock | None:
        return make_memory_store(self.root).get_block(agent_id, label)

    def memory_archive(
        self,
        *,
        agent_id: str,
        text: str,
        source: str,
        source_ref: str = "",
        tags: list[str] | None = None,
    ) -> MemoryArchiveResult:
        from lemoncrow.infra.embeddings.factory import make_embedder
        from lemoncrow.pro.capabilities.archival_recall import ArchivalRecallCapability

        capability = ArchivalRecallCapability(make_memory_store(self.root), make_embedder(), redactor=redact)
        passage = capability.archive(
            agent_id=agent_id,
            text=text,
            source=source,  # type: ignore[arg-type]
            source_ref=source_ref,
            tags=tags,
        )
        return MemoryArchiveResult(id=passage.id, dedup_hit=passage.dedup_hit)

    def memory_recall(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> MemoryRecallResult:
        from datetime import datetime

        from lemoncrow.infra.embeddings.factory import make_embedder
        from lemoncrow.pro.capabilities.archival_recall import ArchivalRecallCapability

        capability = ArchivalRecallCapability(make_memory_store(self.root), make_embedder(), redactor=redact)
        passages, recall = capability.recall(
            agent_id=agent_id,
            query=query,
            top_k=top_k,
            tags=tags,
            since=datetime.fromisoformat(since) if since else None,
        )
        return MemoryRecallResult.model_validate(
            {
                "passages": [
                    {
                        "id": passage.id,
                        "text": passage.text,
                        "source_ref": passage.source_ref,
                        "tags": passage.tags,
                    }
                    for passage in passages
                ],
                "recall_id": recall.id,
            }
        )

    def _list_playbooks(
        self,
        *,
        domain: str | None = None,
        include_deprecated: bool = False,
    ) -> list[Playbook]:
        return self.store.knowledge.list_blocks(domain=domain, include_deprecated=include_deprecated)

    def _search_playbooks(self, *, query: str, limit: int = 20) -> list[Playbook]:
        return self.store.knowledge.search_blocks(query, limit=limit)

    def _get_playbook(self, block_id: str) -> Playbook | None:
        return self.store.knowledge.get_block(block_id)

    def _list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        return self.store.knowledge.list_rubrics(domain=domain)

    def _get_rubric(self, rubric_id: str) -> Rubric | None:
        return self.store.knowledge.get_rubric(rubric_id)

    def _get_trace(self, trace_id: str) -> Trace | None:
        return self.store.history.get_trace(trace_id)

    def _list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        return self.store.history.list_traces(domain=domain, status=status, limit=limit)

    def _list_evals(self, *, domain: str | None = None) -> list[dict[str, Any]]:
        evals_dir = self.root / "evals"
        if not evals_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(evals_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if domain and payload.get("domain") != domain:
                continue
            items.append(payload)
        return items

    def _run_evals(
        self,
        *,
        case_id: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> EvalRunResult:
        """Evaluate local eval cases using their declared expected outcome."""
        items = self._list_evals(domain=domain)
        if case_id is not None:
            items = [item for item in items if item.get("id") == case_id]
        items = items[:limit]
        results = [_evaluate_eval_payload(item) for item in items]
        return EvalRunResult(results=results)
