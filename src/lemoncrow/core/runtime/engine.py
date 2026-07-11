"""Compounding reasoning runtime orchestration.

Coordinates core capabilities, rubrics, traces, evals, and storage
from a single runtime entry point.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, cast

from lemoncrow.core.capabilities import (
    ContextCompressionCapability,
    ContextReuseCapability,
    FailureAnalysisCapability,
    ProofGateCapability,
    QualityRouterCapability,
    SemanticFileMemoryCapability,
    ToolSupervisionCapability,
)
from lemoncrow.core.capabilities.retrieval import Retriever, default_retriever_factory
from lemoncrow.core.capabilities.tool_supervision.sql_inspect import SqlInspectCapability
from lemoncrow.core.foundation.paths import (
    WorkspaceNotRegisteredError,
    default_store_root,
    resolve_workspace_root,
)
from lemoncrow.core.foundation.renderer import render_context_for_agent
from lemoncrow.core.foundation.retriever import (
    count_tokens,
    filter_scoped_passages,
    render_memory_facts_for_agent,
    render_memory_for_agent,
    summarize_memory_facts,
    summarize_recalled_passages,
)
from lemoncrow.core.foundation.routing_models import RouteDecision, StepType, TaskType
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.infra.runtime.run_ledger import RunLedger, iter_run_files


class LemonCrowRuntimeCore:
    """Single runtime orchestrator for LemonCrow core capabilities."""

    CAPABILITIES: ClassVar[dict[str, str]] = {
        "context_compression": "Compress stale history into actionable runtime context.",
        "failure_analysis": "Cluster repeated failures and propose root-cause fixes.",
        "proof_gate": "Cost-quality proof gate combining context savings, routing evals, and trace confidence.",
        "quality_router": "Deterministic quality-aware route selection for runtime steps.",
        "context_reuse": "Reuse prior successful procedures and failure signatures.",
        "semantic_file_memory": "Semantic summaries and symbol maps for local files.",
        "tool_supervision": "Redundancy detection, observation cache, and efficiency metrics.",
    }

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        retriever_factory: Callable[[str | Path], Retriever] | None = None,
    ) -> None:
        resolved_root = default_store_root() if root is None else Path(root).resolve()
        self.root = resolved_root
        self.store = ContextStore(self.root)
        self.store.init()
        # One-shot retriever over the workspace corpus. Defaults to the code
        # vertical (CodeContextEngine); any Retriever-conforming corpus
        # (docs, tickets, chat memory) can be injected instead.
        self.retriever_factory: Callable[[str | Path], Retriever] = retriever_factory or default_retriever_factory

        self.context_reuse = ContextReuseCapability(self.store, self.root)
        self.semantic_memory = SemanticFileMemoryCapability(self.root)
        self.quality_router = QualityRouterCapability(
            self.store,
            self.root,
        )
        self.tool_supervision = ToolSupervisionCapability(self.root)
        self.context_compression = ContextCompressionCapability()
        self.failure_analysis = FailureAnalysisCapability(self.store, self.context_reuse)
        self.proof_gate = ProofGateCapability(self.root)

    def capability_list(self) -> list[dict[str, str]]:
        return [
            {"id": key, "description": value}
            for key, value in sorted(self.CAPABILITIES.items(), key=lambda item: item[0])
        ]

    def capability_status(self) -> dict[str, Any]:
        return {
            "capabilities": self.capability_list(),
            "tool_supervision": self.tool_supervision.status(),
            "semantic_entries": len(self.semantic_memory._load().get("files", {})),
        }

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
        monitor_composite: float = 0.0,
        fsm_skip_etraces: bool = False,
    ) -> str | dict[str, Any]:
        scored = self.context_reuse.retrieve(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
            errors=errors,
            limit=max_blocks,
            token_budget=token_budget,
            dedup=dedup,
            monitor_composite=monitor_composite,
            fsm_skip_etraces=fsm_skip_etraces,
        )
        should_return_payload = include_telemetry or agent_id is not None
        playbook_context = render_context_for_agent([item.block for item in scored])
        bootstrap_context = ""
        bootstrap_blocks: list[dict[str, Any]] = []
        bootstrap_repo_id: str | None = None
        bootstrap_state = "cold"
        memory_context = ""
        recalled_passages: list[dict[str, str | float]] = []
        try:
            from lemoncrow.core.service.bootstrap_context import (
                bootstrap_status,
                render_bootstrap_context,
            )
            from lemoncrow.infra.storage.factory import make_memory_store

            workspace_root = resolve_workspace_root(self.root)
            bootstrap_repo_id = self.retriever_factory(workspace_root).source_id
            memory_store = make_memory_store(self.root)
            bootstrap_state = bootstrap_status(memory_store, bootstrap_repo_id)
            bootstrap_context, bootstrap_blocks = render_bootstrap_context(memory_store, bootstrap_repo_id)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            bootstrap_context = ""
            bootstrap_blocks = []
            bootstrap_repo_id = None

        if recall:
            try:
                from lemoncrow.core.capabilities.archival_recall import ArchivalRecallCapability
                from lemoncrow.core.foundation.redaction import redact
                from lemoncrow.infra.embeddings.factory import get_embedder
                from lemoncrow.infra.storage.factory import make_memory_store

                memory_store = make_memory_store(self.root)
                recall_agent_id = agent_id if agent_id else "shared"
                fact_agent_ids = [recall_agent_id]
                fact_blocks = []
                for fact_agent_id in fact_agent_ids:
                    for block in memory_store.list_blocks(fact_agent_id, include_tombstoned=False, limit=200):
                        metadata = block.metadata or {}
                        if metadata.get("kind") != "memory_fact":
                            continue
                        fact_blocks.append(block)
                fact_blocks.sort(
                    key=lambda block: (
                        -int(((block.metadata or {}).get("votes") or {}).get("upvote", 0) or 0)
                        + int(((block.metadata or {}).get("votes") or {}).get("downvote", 0) or 0),
                        -int(block.version),
                    )
                )
                fact_blocks = fact_blocks[:5]

                capability = ArchivalRecallCapability(memory_store, get_embedder(), redactor=redact)
                passages, _ = capability.recall(agent_id=recall_agent_id, query=task, top_k=3)
                scoped_passages = filter_scoped_passages(passages, requested_agent_id=recall_agent_id)[:3]
                if not scoped_passages:
                    scoped_passages = filter_scoped_passages(
                        memory_store.list_passages(recall_agent_id, limit=3),
                        requested_agent_id=recall_agent_id,
                    )[:3]
                memory_context = render_memory_facts_for_agent(fact_blocks) + render_memory_for_agent(scoped_passages)
                recalled_passages = summarize_memory_facts(fact_blocks) + summarize_recalled_passages(
                    scoped_passages, query=task
                )
            except Exception:
                logging.exception("Recovered from broad exception handler")
                memory_context = ""
                recalled_passages = []

        context = playbook_context + bootstrap_context + memory_context
        playbook_tokens = count_tokens(playbook_context)
        bootstrap_tokens = count_tokens(bootstrap_context) if bootstrap_context else 0
        memory_tokens = count_tokens(memory_context) if memory_context else 0
        if not should_return_payload and not context:
            return ""
        if not should_return_payload:
            return context
        payload: dict[str, Any] = {
            "context": context,
            "recalled_passages": recalled_passages,
            "tokens_breakdown": {
                "playbooks": playbook_tokens,
                "bootstrap": bootstrap_tokens,
                "memory": memory_tokens,
                "total": playbook_tokens + bootstrap_tokens + memory_tokens,
            },
            "bootstrap": {
                "status": "warm" if bootstrap_context else bootstrap_state,
                "repo_id": bootstrap_repo_id,
                # Lightweight metadata only. The repo-map markdown is already
                # inlined into the model-facing `context` string above, so the
                # structured echo must NOT repeat any rendered block text
                # (`value`) — that would ship the same markdown twice.
                "blocks": [
                    {key: value for key, value in block.items() if key != "value"} for block in bootstrap_blocks
                ],
            },
        }
        if not include_telemetry:
            return payload

        naive = self.context_reuse.retrieve(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
            errors=errors,
            limit=max_blocks,
            token_budget=None,
            dedup=False,
        )
        naive_context = render_context_for_agent([item.block for item in naive])
        tokens_used = count_tokens(context)
        naive_tokens = count_tokens(naive_context)
        payload["tokens_used"] = tokens_used
        payload["tokens_saved_vs_naive"] = max(0, naive_tokens - tokens_used)
        return payload

    def smart_search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"matches": [], "semantic": [], "glob": [], "snippets": [], "ranked": []}

        cache_key = f"smart-search:{query}:{limit}"
        cached = self.tool_supervision.get(cache_key)
        if cached is not None:
            self.tool_supervision.observe(cache_key, cached, cache_hit=True)
            return {"cached": True, **cached}

        block_matches = self.store.search_blocks(query, limit=limit)
        semantic_matches = self.semantic_memory.semantic_search(query, limit=limit)
        glob_matches = self._glob_search(query, limit=limit)
        snippets = self._snippet_search(query, limit=limit)

        ranked: list[dict[str, Any]] = []
        for item in semantic_matches:
            ranked.append(
                {
                    "source": "semantic",
                    "path": item.get("path"),
                    "score": float(item.get("score", 0.0)),
                }
            )
        for item in snippets:
            ranked.append(
                {
                    "source": "snippet",
                    "path": item.get("path"),
                    "score": float(item.get("score", 0.0)),
                }
            )
        ranked.sort(key=lambda x: x["score"], reverse=True)

        payload = {
            "matches": [{"id": block.id, "title": block.title, "domain": block.domain} for block in block_matches],
            "semantic": semantic_matches,
            "glob": glob_matches,
            "snippets": snippets,
            "ranked": ranked[:limit],
        }
        self.tool_supervision.observe(cache_key, payload, cache_hit=False)
        return {"cached": False, **payload}

    def smart_read(self, path: str | Path, *, max_lines: int = 120) -> dict[str, Any]:
        file_path = Path(path)
        cache_key = f"smart-read:{file_path.resolve()}:{max_lines}"
        cache_enabled = self.tool_supervision.cache_enabled

        cached_summary = self.semantic_memory.get_cached(file_path) if cache_enabled else None
        if cached_summary is not None:
            token_metrics = self._token_metrics(cached_summary.lines_total, len(cached_summary.summary))
            payload = {
                "path": cached_summary.path,
                "summary": cached_summary.summary,
                "language": cached_summary.language,
                "symbols": cached_summary.symbols,
                "exports": cached_summary.exports,
                "lines_total": cached_summary.lines_total,
                "ast_summary": cached_summary.ast_summary,
                "token_metrics": token_metrics,
            }
            self.tool_supervision.observe(cache_key, payload, cache_hit=True)
            return {"cached": True, **payload}

        summary = self.semantic_memory.summarize_file(
            file_path,
            max_lines=max_lines,
            cache_enabled=cache_enabled,
        )
        payload = {
            "path": summary.path,
            "summary": summary.summary,
            "language": summary.language,
            "symbols": summary.symbols,
            "exports": summary.exports,
            "lines_total": summary.lines_total,
            "ast_summary": summary.ast_summary,
            "token_metrics": self._token_metrics(summary.lines_total, len(summary.summary)),
        }
        self.tool_supervision.observe(cache_key, payload, cache_hit=False)
        return {"cached": False, **payload}

    def smart_edit(self, edits: list[dict[str, str]]) -> dict[str, Any]:
        applied = 0
        failed: list[dict[str, str]] = []
        rollback_files: list[str] = []

        by_path: dict[str, list[dict[str, str]]] = {}
        for item in edits:
            key = str(item.get("path", ""))
            by_path.setdefault(key, []).append(item)

        for path_str, file_edits in by_path.items():
            path = Path(path_str)
            if not path.is_file():
                failed.append({"path": str(path), "error": "file-not-found"})
                continue

            original = path.read_text(encoding="utf-8", errors="replace")
            updated = original
            file_applied = 0
            file_failed = False

            for item in file_edits:
                find = item.get("find", "")
                replace = item.get("replace", "")
                changed, next_text = self._apply_single_edit(updated, find, replace)
                if not changed:
                    failed.append({"path": str(path), "error": "pattern-not-found"})
                    file_failed = True
                    break
                updated = next_text
                file_applied += 1

            if file_failed:
                rollback_files.append(str(path))
                continue

            path.write_text(updated, encoding="utf-8")
            applied += file_applied

        cache_key = f"smart-edit:{applied}:{len(failed)}"
        payload = {
            "applied": applied,
            "failed": failed,
            "rollback": {"count": len(rollback_files), "files": rollback_files},
        }
        self.tool_supervision.observe(cache_key, payload, cache_hit=False)
        return payload

    def sql_inspect(
        self,
        *,
        connection_alias: str | None = None,
        sql: str | None = None,
        file_path: str | None = None,
        params: list[Any] | dict[str, Any] | None = None,
        row_limit: int = 200,
    ) -> dict[str, Any]:
        source = sql
        if source is None and file_path:
            p = Path(file_path)
            if not p.is_file():
                raise FileNotFoundError(f"file not found: {file_path}")
            source = p.read_text(encoding="utf-8", errors="replace")

        if source is None:
            raise ValueError("provide sql text or file_path")

        if connection_alias:
            capability = SqlInspectCapability(self.root)
            return capability.inspect(
                connection_alias=connection_alias,
                sql=source,
                params=params,
                row_limit=row_limit,
            )

        return self._static_sql_inspect(source)

    def route_decide(
        self,
        *,
        user_goal: str,
        repo_root: str,
        task_type: TaskType,
        risk_level: str,
        changed_files: list[str] | None = None,
        domain: str | None = None,
        step_type: StepType = "plan",
        step_index: int = 0,
        session_id: str | None = None,
        evidence_summary: dict[str, Any] | None = None,
        ledger: RunLedger | None = None,
    ) -> RouteDecision:
        """Compute a deterministic quality-aware route decision."""
        return cast(
            RouteDecision,
            self.quality_router.decide(
                user_goal=user_goal,
                repo_root=repo_root,
                task_type=task_type,
                risk_level=risk_level,
                changed_files=changed_files,
                domain=domain,
                step_type=step_type,
                step_index=step_index,
                session_id=session_id,
                evidence_summary=evidence_summary,
                ledger=ledger,
            ),
        )

    def _static_sql_inspect(self, source: str) -> dict[str, Any]:
        """Fallback static SQL analysis mode (no live DB connection)."""

        schema = self._sql_schema_snapshot(source)
        tables = sorted(set(re.findall(r"\b(?:from|join|update|into)\s+([a-zA-Z0-9_.]+)", source, re.I)))
        fks = re.findall(
            r"\bforeign\s+key\s*\(([^)]+)\)\s*references\s+([a-zA-Z0-9_.]+)\s*\(([^)]+)\)",
            source,
            re.I,
        )

        select_count = len(re.findall(r"\bselect\b", source, re.I))
        join_count = len(re.findall(r"\bjoin\b", source, re.I))
        mutation_count = len(re.findall(r"\b(insert|update|delete|alter|create|drop)\b", source, re.I))

        return {
            "tables": tables,
            "foreign_keys": [
                {
                    "local_columns": local_cols.strip(),
                    "references_table": table.strip(),
                    "references_columns": ref_cols.strip(),
                }
                for local_cols, table, ref_cols in fks
            ],
            "schema": schema,
            "fk_graph": self._build_fk_graph(schema),
            "query_profile": {
                "select_count": select_count,
                "join_count": join_count,
                "mutation_count": mutation_count,
            },
            "migration_awareness": {
                "contains_alter_table": bool(re.search(r"\balter\s+table\b", source, re.I)),
                "contains_create_table": bool(re.search(r"\bcreate\s+table\b", source, re.I)),
            },
            "introspection_mode": "static-sql",
        }

    def bash_intercept(self, command: str, *, history: list[str] | None = None) -> dict[str, Any]:
        """Suggest structured LemonCrow tools for repetitive grep/cat/find shell commands."""
        text = command.strip()
        history = history or []
        repeated = 1 + sum(1 for item in history if item.strip() == text)

        suggestion: dict[str, Any] | None = None
        if text.startswith("grep ") or text.startswith("rg "):
            parts = text.split()
            if len(parts) >= 2:
                suggestion = {
                    "tool": "search",
                    "args": {"query": parts[1], "path": ".", "mode": "chunks"},
                    "reason": "Use smart search to persist and rank search context in the ledger.",
                }
        elif text.startswith("cat "):
            path = text[4:].strip()
            if path:
                suggestion = {
                    "tool": "read",
                    "args": {"file_path": path},
                    "reason": "Use AST-aware smart read with automatic outline mode.",
                }
        elif text.startswith("find "):
            query = text.replace("find", "", 1).strip()
            suggestion = {
                "tool": "search",
                "args": {"query": query or ".", "path": ".", "mode": "chunks"},
                "reason": "Use smart search for repeated repository lookups.",
            }

        return {
            "intercepted": suggestion is not None,
            "repetition_count": repeated,
            "input": text,
            "suggestion": suggestion,
        }

    def summarize_memory(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            from lemoncrow.core.foundation.paths import find_session_dir

            session_dir = find_session_dir(self.root, session_id)
            if session_dir is None:
                raise FileNotFoundError(f"no run ledger for session {session_id}")
            ledger_path = session_dir / "run.json"
        else:
            paths = iter_run_files(self.root)
            if not paths:
                raise FileNotFoundError("no run ledgers available")
            ledger_path = paths[-1]

        ledger = RunLedger.load(ledger_path)
        if self._should_auto_compact(ledger):
            compressed = self.context_compression.compress(ledger)
        else:
            compressed = {"compacted": False}
        compressed["session_id"] = ledger.session_id
        return cast(dict[str, Any], compressed)

    def _should_auto_compact(self, ledger: RunLedger) -> bool:
        """Decide whether history compaction should run for this ledger.

        DEFAULT-OFF behind ``LEMONCROW_AUTO_COMPACT``. When the flag is off the
        method always returns ``True`` so ``summarize_memory`` behaves exactly
        as it did before (unconditional compress). When the flag is on,
        compaction only fires once live context fill reaches the policy's
        ``trigger_at_context_fraction``.

        Fail-open and headless: any error while evaluating the gate falls
        through to the prior behavior (compress) and never crashes the turn.
        """
        if os.environ.get("LEMONCROW_AUTO_COMPACT", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return True
        try:
            from lemoncrow.core.capabilities.optimization.policy import load_current_policy, should_compact

            policy = load_current_policy(self.root)
            fill = self._live_context_fill(ledger)
            return should_compact(fill, policy.compaction)
        except Exception:  # noqa: BLE001 - fail-open: never crash the turn on gating errors
            logging.getLogger(__name__).debug("auto-compact gate failed; falling back to compress", exc_info=True)
            return True

    def _live_context_fill(self, ledger: RunLedger) -> float:
        """Estimate the live context fill fraction for a ledger.

        Mirrors ``optimization/audit.py``'s per-trace fill formula
        ``(effective_input + always_on) / max(window, 1)``: the audit computes
        fill from a *single* trace's input occupancy, not a running total. So
        this estimates the live window occupancy from the MOST-RECENT LLM call
        for the dominant model rather than summing every historical call -- a
        cumulative sum monotonically inflates the fraction over a long session
        and contradicts the "live fill" the gate needs. ``always_on`` defaults
        to 0 (matching the audit module when no context audit is supplied).
        """
        from lemoncrow.core.capabilities.optimization.audit import context_window_for_model

        def _occupancy(payload: dict[str, Any]) -> int:
            return (
                int(payload.get("input_tokens", 0) or 0)
                + int(payload.get("cache_read_tokens", 0) or 0)
                + int(payload.get("cache_write_tokens", 0) or 0)
            )

        # Tally per-model totals only to pick the dominant model; the fill
        # itself comes from that model's most-recent call, not the sum.
        token_by_model: dict[str, int] = {}
        for event in ledger.events:
            payload = event.payload
            if payload.get("kind") != "llm_call":
                continue
            model = str(payload.get("model", "") or "")
            token_by_model[model] = token_by_model.get(model, 0) + _occupancy(payload)

        if not token_by_model:
            return 0.0
        dominant_model = max(token_by_model, key=lambda m: token_by_model[m])

        recent_occupancy = 0
        for event in reversed(ledger.events):
            payload = event.payload
            if payload.get("kind") != "llm_call":
                continue
            if str(payload.get("model", "") or "") != dominant_model:
                continue
            recent_occupancy = _occupancy(payload)
            break

        window = context_window_for_model(dominant_model)
        return recent_occupancy / max(window, 1)

    def benchmark_runtime_metrics(self) -> dict[str, Any]:
        supervision = self.tool_supervision.status()
        memory_state = self.semantic_memory._load()
        return {
            "total_tool_calls": supervision["total_tool_calls"],
            "avoided_tool_calls": supervision["avoided_tool_calls"],
            "retries_prevented": supervision["retries_prevented"],
            "token_savings": supervision["avoided_tool_calls"] * 200,
            "loops_prevented": supervision["retries_prevented"],
            "successful_rescues": len(memory_state.get("files", {})),
            "validation_catches": 0,
            "context_reduction": 0,
            "task_success_rate": 0.0,
        }

    def export_benchmark_runtime(self, output: Path) -> Path:
        payload = self.benchmark_runtime_metrics()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return output

    # ------------------------------------------------------------------ #
    # Semantic file memory helpers                                         #
    # ------------------------------------------------------------------ #

    def module_summary(self, path: str | Path) -> dict[str, Any]:
        """Return a concise module-level summary: exports, symbols, imports."""
        return cast(dict[str, Any], self.semantic_memory.module_summary(path))

    def symbol_search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search all cached files for symbols matching query."""
        return cast(list[dict[str, Any]], self.semantic_memory.symbol_search(query, limit=limit))

    # ------------------------------------------------------------------ #
    # Tool supervision helpers                                             #
    # ------------------------------------------------------------------ #

    def tool_report(self) -> dict[str, Any]:
        """Return human-readable tool usage + savings summary."""
        return cast(dict[str, Any], self.tool_supervision.tool_report())

    def diff_context(self, paths: list[str], *, lines: int = 5) -> dict[str, Any]:
        """Return unified diff context for the given paths (git diff HEAD)."""
        return cast(dict[str, Any], self.tool_supervision.diff_context(paths, lines=lines))

    def test_context(self, paths: list[str]) -> dict[str, Any]:
        """Return test files related to the given source files."""
        return cast(dict[str, Any], self.tool_supervision.test_context(paths))

    # ------------------------------------------------------------------ #
    # Context compression helpers                                          #
    # ------------------------------------------------------------------ #

    def context_report(self, session_id: str | None = None) -> dict[str, Any]:
        """Return compression + provenance report for a run."""
        ledger = self._load_ledger(session_id)
        return cast(dict[str, Any], self.context_compression.context_report(ledger))

    # ------------------------------------------------------------------ #
    # Failure analysis helpers                                             #
    # ------------------------------------------------------------------ #

    def analyze_failure_for_error(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        lookback: int = 200,
    ) -> dict[str, Any]:
        """Return the closest historical failure incident for a live error."""
        return cast(
            dict[str, Any],
            self.failure_analysis.analyze_for_error(
                task=task,
                error=error,
                domain=domain,
                lookback=lookback,
            ),
        )

    # ------------------------------------------------------------------ #
    # Inject runtime context                                               #
    # ------------------------------------------------------------------ #

    def inject_context(
        self,
        *,
        task: str,
        domain: str | None = None,
        files: list[str] | None = None,
        tools: list[str] | None = None,
        errors: list[str] | None = None,
        max_blocks: int = 5,
    ) -> dict[str, Any]:
        """Return full inject_runtime_context payload."""
        return cast(
            dict[str, Any],
            self.context_reuse.inject_runtime_context(
                task=task,
                domain=domain,
                files=files,
                tools=tools,
                errors=errors,
                max_blocks=max_blocks,
            ),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle hooks (LemonCrowRuntimeV3)                                   #
    # ------------------------------------------------------------------ #

    def pre_plan(
        self,
        plan: list[str],
        *,
        domain: str | None = None,
        task: str = "",
    ) -> dict[str, Any]:
        """Hook: called before executing a plan.

        Returns context information and any loop/dead-end warnings.
        """
        context_data = self.inject_context(task=task, domain=domain)
        return {
            "hook": "pre_plan",
            "procedures": context_data.get("procedures", []),
            "dead_ends": context_data.get("dead_ends", []),
            "rescue_strategies": context_data.get("rescue_strategies", []),
            "plan_step_count": len(plan),
        }

    def pre_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        ledger: RunLedger | None = None,
    ) -> dict[str, Any]:
        """Hook: called before a tool invocation.

        Returns a cached tool result when one is available. ``ledger`` is part of
        the hook contract but no longer inspected here.
        """
        args_key = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)[:100]}"
        cached = self.tool_supervision.get(args_key)
        return {
            "hook": "pre_tool",
            "tool": tool_name,
            "cached_result": cached,
            "cache_available": cached is not None,
        }

    def post_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        *,
        output_chars: int = 0,
    ) -> None:
        """Hook: record tool observation after invocation."""
        args_key = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)[:100]}"
        payload = dict(result)
        payload["output_chars"] = output_chars
        self.tool_supervision.observe(args_key, payload, cache_hit=False)

    def pre_patch(
        self,
        files: list[str],
        diff: str = "",
    ) -> dict[str, Any]:
        """Hook: called before applying a patch.

        Returns safety info from loop detection + file summaries.
        """
        summaries: list[dict[str, Any]] = []
        for path in files:
            try:
                summaries.append(self.module_summary(path))
            except (FileNotFoundError, OSError):
                summaries.append({"path": path, "error": "file-not-found"})
        return {
            "hook": "pre_patch",
            "files": files,
            "file_summaries": summaries,
            "diff_preview": diff[:500] if diff else "",
        }

    def post_patch(
        self,
        files: list[str],
        result: dict[str, Any],
    ) -> None:
        """Hook: invalidate semantic memory cache after patch."""
        for path in files:
            # Re-summarize to update cache
            import contextlib

            with contextlib.suppress(FileNotFoundError, OSError):
                self.semantic_memory.summarize_file(path)

    def pre_validation(
        self,
        checks: list[str],
        *,
        rubric_id: str | None = None,
    ) -> dict[str, Any]:
        """Hook: gather context before running validation rubric."""
        context_data = self.inject_context(
            task=f"validation:{rubric_id or 'unknown'}",
        )
        return {
            "hook": "pre_validation",
            "checks": checks,
            "rubric_id": rubric_id,
            "rescue_strategies": context_data.get("rescue_strategies", []),
        }

    def post_validation(self, result: dict[str, Any]) -> None:
        """Hook: record validation outcome (no-op unless extended)."""
        pass  # Extend to record pass/fail in ledger

    def finalize(self, *, status: str = "completed") -> dict[str, Any]:
        """Hook: produce final run summary with aggregate savings."""
        supervision = self.tool_supervision.status()
        memory_state = self.semantic_memory._load()
        files_cached = len(memory_state.get("files", {}))
        from lemoncrow.core.service.telemetry import emit_product

        emit_product(
            "value_estimate",
            tokens_saved_estimate=int(supervision.get("token_savings", 0) or 0),
            cache_hits=int(supervision.get("avoided_tool_calls", 0) or 0),
            total_tool_calls=int(supervision.get("total_tool_calls", 0) or 0),
            cache_hit_rate=float(supervision.get("cache_hit_rate", 0.0) or 0.0),
            blocks_applied=files_cached,
        )
        return {
            "hook": "finalize",
            "status": status,
            "savings": {
                "total_tool_calls": supervision["total_tool_calls"],
                "avoided_tool_calls": supervision["avoided_tool_calls"],
                "token_savings": supervision["token_savings"],
                "chars_saved": supervision["chars_saved"],
                "retries_prevented": supervision["retries_prevented"],
            },
            "semantic_cache_size": files_cached,
        }

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _load_ledger(self, session_id: str | None = None) -> RunLedger:
        if session_id:
            from lemoncrow.core.foundation.paths import find_session_dir

            session_dir = find_session_dir(self.root, session_id)
            if session_dir is None:
                raise FileNotFoundError(f"no run ledger for session {session_id}")
            ledger_path = session_dir / "run.json"
        else:
            paths = iter_run_files(self.root)
            if not paths:
                raise FileNotFoundError("no run ledgers available")
            ledger_path = paths[-1]
        return RunLedger.load(ledger_path)

    def _workspace_root(self) -> Path:
        # Read-only fallback: this backs the glob/snippet search fallbacks
        # below, which don't persist anything, so an unregistered non-git
        # directory should still be searchable rather than erroring out.
        try:
            return resolve_workspace_root(self.root)
        except WorkspaceNotRegisteredError:
            return Path.cwd()

    def _glob_search(self, query: str, *, limit: int) -> list[str]:
        workspace = self._workspace_root()
        if any(ch in query for ch in "*?[]"):
            matches = [str(p) for p in workspace.rglob(query) if p.is_file()]
        else:
            needle = query.lower()
            matches = [str(p) for p in workspace.rglob("*") if p.is_file() and needle in p.name.lower()]
        return matches[:limit]

    def _snippet_search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        workspace = self._workspace_root()
        query_lower = query.lower()
        snippets: list[dict[str, Any]] = []
        text_ext = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".sql", ".yaml", ".yml"}

        for path in workspace.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_ext:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                logging.exception("Recovered from broad exception handler")
                continue
            pos = text.lower().find(query_lower)
            if pos < 0:
                continue
            start = max(0, pos - 80)
            end = min(len(text), pos + len(query) + 80)
            snippet = text[start:end].replace("\n", " ")
            score = 0.5 + min(0.5, text.lower().count(query_lower) / 10.0)
            snippets.append({"path": str(path), "snippet": snippet.strip(), "score": round(score, 3)})
            if len(snippets) >= limit * 3:
                break

        snippets.sort(key=lambda x: x["score"], reverse=True)
        return snippets[:limit]

    @staticmethod
    def _token_metrics(lines_total: int, returned_chars: int) -> dict[str, Any]:
        full_chars = lines_total * 80
        full_tokens = max(1, full_chars // 4)
        returned_tokens = max(1, returned_chars // 4)
        saved_tokens = max(0, full_tokens - returned_tokens)
        return {
            "estimated_full_tokens": full_tokens,
            "returned_tokens": returned_tokens,
            "saved_tokens": saved_tokens,
            "reduction_pct": round((saved_tokens / max(1, full_tokens)) * 100.0, 1),
        }

    @staticmethod
    def _apply_single_edit(text: str, find: str, replace: str) -> tuple[bool, str]:
        if find in text:
            return True, text.replace(find, replace, 1)

        pattern = re.escape(find).replace(r"\ ", r"\s+")
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            return True, text[: match.start()] + replace + text[match.end() :]

        target = find.strip()
        if target:
            lines = text.splitlines(keepends=True)
            stripped = [line.strip() for line in lines]
            close = difflib.get_close_matches(target, stripped, n=1, cutoff=0.86)
            # Only apply the fuzzy fallback when the match is unambiguous; a
            # close match that appears on several lines could clobber the wrong
            # one while reporting success.
            if close and stripped.count(close[0]) == 1:
                idx = stripped.index(close[0])
                line = lines[idx]
                newline = "\n" if line.endswith("\n") else ""
                # Insert `replace` verbatim (it carries its own indentation,
                # matching the two exact/whitespace-flex branches above);
                # preserve only the line terminator.
                lines[idx] = replace + newline
                return True, "".join(lines)

        return False, text

    @staticmethod
    def _sql_schema_snapshot(sql: str) -> dict[str, Any]:
        tables: dict[str, Any] = {}
        create_re = re.compile(
            r"create\s+table\s+([a-zA-Z0-9_.]+)\s*\((.*?)\);",
            re.IGNORECASE | re.DOTALL,
        )
        for match in create_re.finditer(sql):
            table = match.group(1).strip()
            body = match.group(2)
            columns: list[str] = []
            constraints: list[str] = []
            for raw in body.split(","):
                line = raw.strip()
                if not line:
                    continue
                if line.lower().startswith("constraint") or "foreign key" in line.lower():
                    constraints.append(line)
                    continue
                col_match = re.match(r"([a-zA-Z0-9_]+)", line)
                if col_match:
                    columns.append(col_match.group(1))
            tables[table] = {"columns": columns, "constraints": constraints}
        return tables

    @staticmethod
    def _build_fk_graph(schema: dict[str, Any]) -> list[dict[str, str]]:
        edges: list[dict[str, str]] = []
        fk_re = re.compile(r"references\s+([a-zA-Z0-9_.]+)", re.IGNORECASE)
        for table, info in schema.items():
            for constraint in info.get("constraints", []):
                match = fk_re.search(constraint)
                if match:
                    edges.append({"from": table, "to": match.group(1)})
        return edges


# Alias for the V3 lifecycle-enabled runtime
LemonCrowRuntimeV3 = LemonCrowRuntimeCore
