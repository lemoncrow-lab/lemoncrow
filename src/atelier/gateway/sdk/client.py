"""Shared SDK contracts and namespace clients."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from atelier.core.foundation.lesson_models import LessonCandidate, LessonPromotion
from atelier.core.foundation.memory_models import MemoryBlock
from atelier.core.foundation.models import (
    FailureCluster,
    ReasonBlock,
    RescueResult,
    Rubric,
    RubricResult,
    Trace,
    TraceStatus,
    ValidationResult,
)

if TYPE_CHECKING:
    from atelier.gateway.sdk.local import LocalClient
    from atelier.gateway.sdk.mcp import MCPClient
    from atelier.gateway.sdk.remote import RemoteClient


class ReasoningContextRecalledPassage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    score: float


class ReasoningContextTokenBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasonblocks: int
    memory: int
    total: int


class ContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    tokens_used: int | None = None
    tokens_saved_vs_naive: int | None = None
    recalled_passages: builtins.list[ReasoningContextRecalledPassage] = Field(default_factory=list)
    tokens_breakdown: ReasoningContextTokenBreakdown | None = None
    run_ledger: dict[str, Any] | None = None


class TraceRecordResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str


class MemoryUpsertBlockResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    arbitration: dict[str, Any] = Field(default_factory=dict)


class MemoryArchiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    dedup_hit: bool = False


class MemoryRecallPassage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    source_ref: str = ""
    tags: builtins.list[str] = Field(default_factory=list)


class MemoryRecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passages: builtins.list[MemoryRecallPassage] = Field(default_factory=list)
    recall_id: str


class FailureAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clusters: builtins.list[FailureCluster] = Field(default_factory=list)


class LessonInboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lessons: builtins.list[LessonCandidate] = Field(default_factory=list)


class LessonDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lesson: LessonCandidate
    promotion: LessonPromotion | None = None


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str
    domain: str
    description: str
    expected_status: str
    actual_status: str
    passed: bool


class EvalRunResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "completed"
    results: builtins.list[EvalRecord] = Field(default_factory=list)
    note: str = ""


class SavingsSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    operations_tracked: int = 0
    total_calls: int = 0
    would_have_cost_usd: float = 0.0
    actually_cost_usd: float = 0.0
    saved_usd: float = 0.0
    saved_pct: float = 0.0
    per_operation: builtins.list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


class ReasonBlockClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def list(self, *, domain: str | None = None, include_deprecated: bool = False) -> builtins.list[ReasonBlock]:
        return self._client._list_reasonblocks(domain=domain, include_deprecated=include_deprecated)

    def search(self, query: str, *, limit: int = 20) -> builtins.list[ReasonBlock]:
        return self._client._search_reasonblocks(query=query, limit=limit)

    def get(self, block_id: str) -> ReasonBlock | None:
        return self._client._get_reasonblock(block_id)


class RubricClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def list(self, *, domain: str | None = None) -> builtins.list[Rubric]:
        return self._client._list_rubrics(domain=domain)

    def get(self, rubric_id: str) -> Rubric | None:
        return self._client._get_rubric(rubric_id)

    def run(self, rubric_id: str, checks: dict[str, bool | None]) -> RubricResult:
        return self._client.run_rubric_gate(rubric_id=rubric_id, checks=checks)


class TraceClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def record(
        self,
        *,
        agent: str,
        domain: str,
        task: str,
        status: TraceStatus,
        files_touched: builtins.list[str | dict[str, Any]] | None = None,
        commands_run: builtins.list[str | dict[str, Any]] | None = None,
        tools_called: builtins.list[dict[str, Any]] | None = None,
        errors_seen: builtins.list[str] | None = None,
        diff_summary: str = "",
        output_summary: str = "",
        validation_results: builtins.list[ValidationResult] | None = None,
    ) -> TraceRecordResult:
        return self._client.record_trace(
            agent=agent,
            domain=domain,
            task=task,
            status=status,
            files_touched=files_touched,
            commands_run=commands_run,
            tools_called=tools_called,
            errors_seen=errors_seen,
            diff_summary=diff_summary,
            output_summary=output_summary,
            validation_results=validation_results,
        )

    def get(self, trace_id: str) -> Trace | None:
        return self._client._get_trace(trace_id)

    def list(self, *, domain: str | None = None, status: str | None = None, limit: int = 50) -> builtins.list[Trace]:
        return self._client._list_traces(domain=domain, status=status, limit=limit)


class FailureAnalyzerClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def analyze(self, *, domain: str | None = None, limit: int = 100) -> FailureAnalysisResult:
        return self._client.analyze_failures(domain=domain, limit=limit)


class EvalClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def list(self, *, domain: str | None = None) -> builtins.list[dict[str, Any]]:
        return self._client._list_evals(domain=domain)

    def run(self, *, case_id: str | None = None, domain: str | None = None, limit: int = 50) -> EvalRunResult:
        return self._client._run_evals(case_id=case_id, domain=domain, limit=limit)


class SavingsClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def summary(self) -> SavingsSummary:
        return self._client.get_savings()


class MemoryClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def upsert_block(
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
        return self._client.memory_upsert_block(
            agent_id=agent_id,
            label=label,
            value=value,
            limit_chars=limit_chars,
            description=description,
            read_only=read_only,
            pinned=pinned,
            metadata=metadata,
            expected_version=expected_version,
            actor=actor,
        )

    def get_block(self, *, agent_id: str, label: str) -> MemoryBlock | None:
        return self._client.memory_get_block(agent_id=agent_id, label=label)

    def archive(
        self,
        *,
        agent_id: str,
        text: str,
        source: str,
        source_ref: str = "",
        tags: builtins.list[str] | None = None,
    ) -> MemoryArchiveResult:
        return self._client.memory_archive(
            agent_id=agent_id,
            text=text,
            source=source,
            source_ref=source_ref,
            tags=tags,
        )

    def recall(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int = 5,
        tags: builtins.list[str] | None = None,
        since: str | None = None,
    ) -> MemoryRecallResult:
        return self._client.memory_recall(
            agent_id=agent_id,
            query=query,
            top_k=top_k,
            tags=tags,
            since=since,
        )


class LessonClient:
    def __init__(self, client: AtelierClient) -> None:
        self._client = client

    def inbox(self, *, domain: str | None = None, limit: int = 25) -> LessonInboxResult:
        return self._client.lesson_inbox(domain=domain, limit=limit)

    def decide(self, *, lesson_id: str, decision: str, reviewer: str, reason: str) -> LessonDecisionResult:
        return self._client.lesson_decide(
            lesson_id=lesson_id,
            decision=decision,
            reviewer=reviewer,
            reason=reason,
        )


class AtelierClient(ABC):
    """Stable SDK facade over Atelier's local, remote, and MCP modes."""

    def __init__(self) -> None:
        self.reasonblocks = ReasonBlockClient(self)
        self.blocks = self.reasonblocks
        self.rubrics = RubricClient(self)
        self.traces = TraceClient(self)
        self.failures = FailureAnalyzerClient(self)
        self.evals = EvalClient(self)
        self.savings = SavingsClient(self)
        self.memory = MemoryClient(self)
        self.lessons = LessonClient(self)

    @classmethod
    def local(cls, *, root: str | None = None) -> LocalClient:
        from atelier.gateway.sdk.local import LocalClient

        if root is None:
            from atelier.core.foundation.paths import default_store_root

            root = str(default_store_root())

        return LocalClient(root=root)

    @classmethod
    def remote(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> RemoteClient:
        from atelier.gateway.sdk.remote import RemoteClient

        return RemoteClient(base_url=base_url, api_key=api_key, timeout=timeout)

    @classmethod
    def mcp(
        cls,
        *,
        root: str | None = None,
        transport: MCPToolTransport | None = None,
    ) -> MCPClient:
        from atelier.gateway.sdk.mcp import MCPClient

        if root is None:
            from atelier.core.foundation.paths import default_store_root

            root = str(default_store_root())

        return MCPClient(root=root, transport=transport)

    @abstractmethod
    def get_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: builtins.list[str] | None = None,
        tools: builtins.list[str] | None = None,
        errors: builtins.list[str] | None = None,
        max_blocks: int = 5,
    ) -> ContextResult:
        raise NotImplementedError

    @abstractmethod
    def rescue_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: builtins.list[str] | None = None,
        recent_actions: builtins.list[str] | None = None,
    ) -> RescueResult:
        raise NotImplementedError

    @abstractmethod
    def run_rubric_gate(self, *, rubric_id: str, checks: dict[str, bool | None]) -> RubricResult:
        raise NotImplementedError

    @abstractmethod
    def record_trace(
        self,
        *,
        agent: str,
        domain: str,
        task: str,
        status: TraceStatus,
        files_touched: builtins.list[str | dict[str, Any]] | None = None,
        commands_run: builtins.list[str | dict[str, Any]] | None = None,
        tools_called: builtins.list[dict[str, Any]] | None = None,
        errors_seen: builtins.list[str] | None = None,
        diff_summary: str = "",
        output_summary: str = "",
        validation_results: builtins.list[ValidationResult] | None = None,
    ) -> TraceRecordResult:
        raise NotImplementedError

    @abstractmethod
    def analyze_failures(self, *, domain: str | None = None, limit: int = 100) -> FailureAnalysisResult:
        raise NotImplementedError

    @abstractmethod
    def get_savings(self) -> SavingsSummary:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def memory_get_block(self, *, agent_id: str, label: str) -> MemoryBlock | None:
        raise NotImplementedError

    @abstractmethod
    def memory_archive(
        self,
        *,
        agent_id: str,
        text: str,
        source: str,
        source_ref: str = "",
        tags: builtins.list[str] | None = None,
    ) -> MemoryArchiveResult:
        raise NotImplementedError

    @abstractmethod
    def memory_recall(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int = 5,
        tags: builtins.list[str] | None = None,
        since: str | None = None,
    ) -> MemoryRecallResult:
        raise NotImplementedError

    @abstractmethod
    def lesson_inbox(self, *, domain: str | None = None, limit: int = 25) -> LessonInboxResult:
        raise NotImplementedError

    @abstractmethod
    def lesson_decide(
        self,
        *,
        lesson_id: str,
        decision: str,
        reviewer: str,
        reason: str,
    ) -> LessonDecisionResult:
        raise NotImplementedError

    @abstractmethod
    def _list_reasonblocks(
        self,
        *,
        domain: str | None = None,
        include_deprecated: bool = False,
    ) -> builtins.list[ReasonBlock]:
        raise NotImplementedError

    @abstractmethod
    def _search_reasonblocks(self, *, query: str, limit: int = 20) -> builtins.list[ReasonBlock]:
        raise NotImplementedError

    @abstractmethod
    def _get_reasonblock(self, block_id: str) -> ReasonBlock | None:
        raise NotImplementedError

    @abstractmethod
    def _list_rubrics(self, *, domain: str | None = None) -> builtins.list[Rubric]:
        raise NotImplementedError

    @abstractmethod
    def _get_rubric(self, rubric_id: str) -> Rubric | None:
        raise NotImplementedError

    @abstractmethod
    def _get_trace(self, trace_id: str) -> Trace | None:
        raise NotImplementedError

    @abstractmethod
    def _list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[Trace]:
        raise NotImplementedError

    @abstractmethod
    def _list_evals(self, *, domain: str | None = None) -> builtins.list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def _run_evals(
        self,
        *,
        case_id: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> EvalRunResult:
        raise NotImplementedError


class MCPToolTransport(ABC):
    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
