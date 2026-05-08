"""In-process runtime adapter — used by Beseam product agents.

Usage:

    from atelier.gateway.adapters.runtime import ReasoningRuntime

    rt = ReasoningRuntime(root=".atelier")
    with rt.run(domain="beseam.shopify.publish", task=task,
                tools=["shopify.update_metafield"]) as session:
        session.inject_reasoning_context()
        plan = agent.plan()
        session.check_plan(plan)
        result = agent.execute(monitors=session.monitors)
        session.verify(result, rubric_id="rubric_shopify_publish")
        session.record_trace(result)
        session.extract_candidate_blocks()
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atelier.core.foundation.paths import default_store_root
from atelier.core.foundation.extractor import CandidateBlock, extract_candidate
from atelier.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    PlanCheckResult,
    ReasonBlock,
    RescueResult,
    RubricResult,
    ToolCall,
    Trace,
    ValidationResult,
)
from atelier.core.foundation.monitors import (
    MonitorAlert,
    SessionState,
    args_signature,
    default_monitors,
    error_signature,
    run_monitors,
)
from atelier.core.foundation.plan_checker import check_plan
from atelier.core.foundation.renderer import render_context_for_agent
from atelier.core.foundation.retriever import (
    ScoredBlock,
    TaskContext,
    deduplicate_scored_blocks,
    pack_by_reasonblock_token_budget,
    retrieve,
    score_block,
)
from atelier.core.foundation.routing_models import RouteDecision, StepType, TaskType
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.core.foundation.store import ReasoningStore
from atelier.core.runtime import AtelierRuntimeCore


def _load_domain_reasonblocks(store_root: Path) -> list[ReasonBlock]:
    from atelier.core.domains import DomainManager

    manager = DomainManager(store_root)
    blocks: list[ReasonBlock] = []
    seen_ids: set[str] = set()

    for block in manager.all_reasonblocks():
        if block.id in seen_ids or block.status in ("quarantined",):
            continue
        seen_ids.add(block.id)
        blocks.append(block)

    return blocks


def _retrieve_with_pack_context(
    store: ReasoningStore,
    ctx: TaskContext,
    *,
    limit: int,
    token_budget: int | None = 2000,
    dedup: bool = True,
) -> list[ScoredBlock]:
    # Retrieve learned/runtime blocks first, then merge with domain bundle blocks.
    learned = retrieve(
        store,
        ctx,
        limit=max(limit * 3, 15),
        token_budget=None,
        dedup=False,
    )
    merged: dict[str, ScoredBlock] = {entry.block.id: entry for entry in learned}

    for block in _load_domain_reasonblocks(store.root):
        if block.status == "deprecated":
            continue
        scored = score_block(block, ctx)
        if scored.score < 0.15:
            continue
        existing = merged.get(block.id)
        if existing is None or scored.score > existing.score:
            merged[block.id] = scored

    ranked = sorted(merged.values(), key=lambda entry: entry.score, reverse=True)
    if dedup:
        ranked = deduplicate_scored_blocks(ranked)
    return pack_by_reasonblock_token_budget(
        ranked,
        lambda item: item.block,
        limit=limit,
        token_budget=token_budget,
    )


@dataclass
class RuntimeSession:
    """A single agent run wrapped by the reasoning runtime."""

    domain: str
    task: str
    agent: str
    files: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    state: SessionState = field(default_factory=SessionState)
    store: ReasoningStore | None = None
    trace_id: str | None = None
    monitors: list[Any] = field(default_factory=default_monitors)
    core_runtime: AtelierRuntimeCore | None = None
    started_at: float = field(default_factory=time.time)

    # ----- context injection ---------------------------------------------- #

    def inject_reasoning_context(
        self,
        *,
        max_blocks: int = 5,
        token_budget: int | None = 2000,
        dedup: bool = True,
    ) -> str:
        if self.core_runtime is not None:
            context = self.core_runtime.get_reasoning_context(
                task=self.task,
                domain=self.domain,
                files=self.files,
                tools=self.tools,
                max_blocks=max_blocks,
                token_budget=token_budget,
                dedup=dedup,
            )
            return context["context"] if isinstance(context, dict) else context
        assert self.store is not None
        ctx = TaskContext(task=self.task, domain=self.domain, files=self.files, tools=self.tools)
        scored = _retrieve_with_pack_context(
            self.store,
            ctx,
            limit=max_blocks,
            token_budget=token_budget,
            dedup=dedup,
        )
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        for rank, item in enumerate(scored, start=1):
            emit_product(
                "reasonblock_retrieved",
                block_id_hash=hash_identifier(item.block.id),
                domain=item.block.domain,
                retrieval_score=float(item.score),
                rank=rank,
            )
        return render_context_for_agent([s.block for s in scored])

    # ----- plan checking --------------------------------------------------- #

    def check_plan(self, plan: list[str]) -> PlanCheckResult:
        assert self.store is not None
        self.state.plan = plan
        result = check_plan(
            self.store,
            task=self.task,
            plan=plan,
            domain=self.domain,
            files=self.files,
            tools=self.tools,
        )
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        matched_blocks = list(getattr(result, "matched_blocks", []) or [])
        if result.status == "blocked":
            emit_product(
                "plan_check_blocked",
                domain=self.domain,
                blocking_rule_id=hash_identifier(str(matched_blocks[0] if matched_blocks else "blocked")),
                severity="high",
            )
        else:
            emit_product(
                "plan_check_passed",
                domain=self.domain,
                rule_count=len(matched_blocks),
            )
        return result

    # ----- monitor hooks --------------------------------------------------- #

    def record_command(
        self,
        command: str,
        *,
        succeeded: bool,
        error: str = "",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        sig = error_signature(error) if error else ""
        self.state.commands_run.append(command)
        self.state.command_results.append((command, succeeded, sig))
        self.state.command_outputs.append((command, exit_code, stdout[:1024], stderr[:1024]))

    def record_tool_call(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.state.tool_calls.append((name, args_signature(args)))
        self.state.tool_call_args.append((name, args))

    def record_tool_output(self, output: str) -> None:
        self.state.tool_outputs_chars += len(output)
        # Attach result summary to the most recent tool call if we have one.
        if self.state.tool_call_args:
            last_name = self.state.tool_call_args[-1][0]
            self.state.tool_call_results.append((last_name, output[:200]))

    def record_file_edit(self, path: str, event: str = "edit", diff: str = "") -> None:
        """Record a file edit with optional diff content."""
        if path not in self.files:
            self.files.append(path)
        self.state.file_events.append((path, event))
        if event == "revert":
            from atelier.core.service.telemetry import emit_product

            emit_product("frustration_signal_behavioral", signal_type="file_revert")
        if diff:
            self.state.file_diffs.append((path, event, diff[:4096]))

    def run_monitors(self) -> list[MonitorAlert]:
        assert self.store is not None
        ctx = TaskContext(task=self.task, domain=self.domain, files=self.files, tools=self.tools)
        blocks = [s.block for s in _retrieve_with_pack_context(self.store, ctx, limit=10)]
        alerts = run_monitors(self.state, blocks, self.monitors)
        if alerts:
            from atelier.core.service.telemetry import emit_product

            for alert in alerts:
                if alert.monitor == "repeated_command_failure":
                    emit_product("frustration_signal_behavioral", signal_type="retry_burst")
                elif alert.monitor == "known_dead_end":
                    emit_product("frustration_signal_behavioral", signal_type="repeated_dead_end")
        return alerts

    # ----- rubric gate ----------------------------------------------------- #

    def verify(self, checks: dict[str, bool | None], rubric_id: str) -> RubricResult:
        assert self.store is not None
        rubric = self.store.get_rubric(rubric_id)
        if rubric is None:
            raise KeyError(f"rubric not found: {rubric_id}")
        result = run_rubric(rubric, checks)
        self.state.rubric_run = True
        self.state.validation_passed = result.status != "blocked"
        return result

    # ----- trace recording ------------------------------------------------- #

    def record_trace(
        self,
        *,
        status: str,
        diff_summary: str = "",
        output_summary: str = "",
        validation_results: list[ValidationResult] | None = None,
    ) -> Trace:
        assert self.store is not None
        from atelier.core.foundation.redaction import redact, redact_list

        # Build enriched tool calls with args and result summaries
        result_map: dict[str, str] = {}
        for name, summary in self.state.tool_call_results:
            result_map.setdefault(name, summary)
        args_map: dict[str, dict[str, Any] | None] = {}
        for name, args in self.state.tool_call_args:
            args_map.setdefault(name, args)

        # Merge duplicate tool calls by (name, args_hash)
        tool_call_merged: dict[tuple[str, str], ToolCall] = {}
        for name, sig in self.state.tool_calls:
            key = (name, sig)
            if key in tool_call_merged:
                tool_call_merged[key].count += 1
            else:
                tool_call_merged[key] = ToolCall(
                    name=name,
                    args_hash=sig,
                    count=1,
                    args=args_map.get(name),
                    result_summary=result_map.get(name, ""),
                )

        # Build enriched file records with diffs
        diff_map: dict[str, tuple[str, str]] = {}
        for path, event, diff_text in self.state.file_diffs:
            diff_map[path] = (event, diff_text)
        files_enriched: list[str | FileEditRecord] = []
        for f in self.files:
            if f in diff_map:
                evt, diff_text = diff_map[f]
                files_enriched.append(FileEditRecord(path=f, diff=diff_text, event=evt))
            else:
                files_enriched.append(f)

        # Build enriched command records with output
        output_map: dict[str, tuple[int | None, str, str]] = {}
        for cmd, rc, out, err in self.state.command_outputs:
            output_map.setdefault(cmd, (rc, out, err))
        commands_enriched: list[str | CommandRecord] = []
        for cmd in self.state.commands_run:
            if cmd in output_map:
                rc, out, err = output_map[cmd]
                commands_enriched.append(CommandRecord(command=cmd, exit_code=rc, stdout=out, stderr=err))
            else:
                commands_enriched.append(cmd)

        trace = Trace(
            id=Trace.make_id(self.task, self.agent),
            agent=self.agent,
            domain=self.domain,
            task=redact(self.task),
            status=status,  # type: ignore[arg-type]
            files_touched=files_enriched,
            tools_called=list(tool_call_merged.values()),
            commands_run=commands_enriched,
            errors_seen=redact_list([sig for _, ok, sig in self.state.command_results if not ok and sig]),
            diff_summary=redact(diff_summary),
            output_summary=redact(output_summary),
            validation_results=validation_results or [],
        )
        self.store.record_trace(trace)
        self.trace_id = trace.id
        return trace

    # ----- candidate extraction ------------------------------------------- #

    def extract_candidate_blocks(self) -> CandidateBlock | None:
        assert self.store is not None
        if not self.trace_id:
            return None
        trace = self.store.get_trace(self.trace_id)
        if trace is None:
            return None
        return extract_candidate(trace)


# --------------------------------------------------------------------------- #
# Runtime façade                                                              #
# --------------------------------------------------------------------------- #


class ReasoningRuntime:
    """Top-level facade for product agents."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.core_runtime = AtelierRuntimeCore(default_store_root() if root is None else root)
        self.store = self.core_runtime.store

    @contextmanager
    def run(
        self,
        *,
        domain: str,
        task: str,
        agent: str = "beseam-product-agent",
        files: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> Iterator[RuntimeSession]:
        session = RuntimeSession(
            domain=domain,
            task=task,
            agent=agent,
            files=files or [],
            tools=tools or [],
            store=self.store,
            core_runtime=self.core_runtime,
        )
        try:
            yield session
        finally:
            # Make sure something is recorded even if the agent crashed.
            if session.trace_id is None:
                if time.time() - session.started_at <= 30 and any(not ok for _, ok, _ in session.state.command_results):
                    from atelier.core.service.telemetry import emit_product

                    emit_product(
                        "frustration_signal_behavioral",
                        signal_type="abandon_after_error",
                    )
                with suppress(Exception):  # pragma: no cover - defensive
                    session.record_trace(status="partial")

    # ----- standalone helpers used by MCP/CLI ----------------------------- #

    def get_reasoning_context(
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
    ) -> str | dict[str, Any]:
        return self.core_runtime.get_reasoning_context(
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

    def rescue_failure(
        self,
        *,
        task: str,
        error: str,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
        domain: str | None = None,
    ) -> RescueResult:
        scored = self.core_runtime.reasoning_reuse.retrieve(
            task=task,
            domain=domain,
            files=files,
            errors=[error],
            limit=3,
        )
        if not scored:
            return RescueResult(
                rescue=(
                    "No matching ReasonBlock found. Stop and summarize: "
                    "files changed, errors seen, assumptions tested, current "
                    "blocker. Then ask for guidance."
                ),
                matched_blocks=[],
            )
        top = scored[0].block
        rescue = f"Stop retrying. Apply procedure '{top.title}': " + " | ".join(top.procedure)
        if top.verification:
            rescue += " | Verify: " + ", ".join(top.verification)
        return RescueResult(
            rescue=rescue,
            matched_blocks=[s.block.id for s in scored],
        )

    def sql_inspect(
        self,
        *,
        connection_alias: str,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        row_limit: int = 200,
    ) -> dict[str, Any]:
        return self.core_runtime.sql_inspect(
            connection_alias=connection_alias,
            sql=sql,
            params=params,
            row_limit=row_limit,
        )

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
        run_id: str | None = None,
        evidence_summary: dict[str, Any] | None = None,
        ledger: Any | None = None,
    ) -> RouteDecision:
        return self.core_runtime.route_decide(
            user_goal=user_goal,
            repo_root=repo_root,
            task_type=task_type,
            risk_level=risk_level,
            changed_files=changed_files,
            domain=domain,
            step_type=step_type,
            step_index=step_index,
            run_id=run_id,
            evidence_summary=evidence_summary,
            ledger=ledger,
        )
