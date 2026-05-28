"""PhaseRunner — phase-linear conversation orchestrator (LINEAR-01/02).

Implements the Survey→Plan→Implement state machine described in
``docs/plans/phase-linear-cache-reuse/02-DESIGN-SPEC.md`` and CONTEXT
decisions D-04..D-08:

* D-04 — Survey and Plan share one message list; Plan's first provider
  call includes Survey's tail messages verbatim (``continue_from``).
* D-05 — Implement starts lean: ``[system, user(objective)]`` only.
* D-06 — One fixed system prompt (``shell.md``) loaded once and reused
  by reference across every provider call (byte-stable).
* D-07 — A cache breakpoint is recorded at every phase tail via
  ``PrefixCachePlanner.plan_with_history`` + ``PrefixCacheDiagnostics``.
* D-08 — Reader profile rejects mutation tools; writer profile accepts
  them. Mitigates T-13-01 (elevation of privilege).

Telemetry: every provider call is recorded via ``RunLedger.record_call``
with the new ``cache_write_tokens`` and ``phase`` fields (T-13-04: the
additions are keyword-only with defaults; ``CostTracker.record_call`` is
not modified).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from atelier.core.capabilities.prefix_cache.diagnostics import PrefixCacheDiagnostics
from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
from atelier.core.capabilities.prompt_compilation.models import (
    BlockKind,
    PromptBlock,
    Stability,
)
from atelier.infra.runtime.run_ledger import RunLedger

from .models import Phase, PhaseCacheStats, PhasePlan, PhaseResult

_READER_TOOLS: frozenset[str] = frozenset({"read", "search", "glob", "code_intel", "web"})
_WRITER_TOOLS: frozenset[str] = _READER_TOOLS | frozenset({"write", "edit", "delete"})

_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


class _ProviderProto(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> tuple[str, int, int, int, int]: ...


class PhaseRunner:
    """Orchestrate one phase-linear run over a ``PhasePlan``."""

    def __init__(
        self,
        plan: PhasePlan,
        *,
        provider: _ProviderProto,
        ledger: RunLedger,
        planner: PrefixCachePlanner,
        diag: PrefixCacheDiagnostics,
        prompts_dir: Path | None = None,
        model: str = "test-model",
    ) -> None:
        self.plan = plan
        self.provider = provider
        self.ledger = ledger
        self.planner = planner
        self.diag = diag
        self._prompts_dir = prompts_dir or _DEFAULT_PROMPTS_DIR
        self._model = model
        # D-06: load shell.md once and reuse by reference for byte stability.
        self._shell_prompt: str = (self._prompts_dir / "shell.md").read_text(encoding="utf-8")
        # Pre-load each phase objective; key by phase name.
        self._objectives: dict[str, str] = {}
        for name, phase in plan.phases.items():
            target = phase.objective_path or f"{name}.md"
            self._objectives[name] = (self._prompts_dir / target).read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> dict[str, PhaseResult]:
        """Execute every phase in order, returning per-phase results."""
        results: dict[str, PhaseResult] = {}
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._shell_prompt}]
        order = self.plan.iter_order()
        for idx, phase_name in enumerate(order):
            phase = self.plan.phases[phase_name]
            # D-05: if a phase does not continue from its predecessor and it
            # is not the first phase, reset to a lean [system, user] context.
            if idx > 0 and phase.continue_from is None:
                messages = [{"role": "system", "content": self._shell_prompt}]
            messages.append({"role": "user", "content": self._objectives[phase_name]})
            messages, phase_stats, output_text = self._run_agent_loop(phase, messages)
            results[phase_name] = PhaseResult(
                phase_name=phase_name,
                messages=list(messages),
                cache_stats=phase_stats,
                output_text=output_text,
            )
        return results

    # ------------------------------------------------------------------
    # Tool profile (T-13-01)
    # ------------------------------------------------------------------

    def _allowed_tools(self, phase: Phase) -> frozenset[str]:
        return _WRITER_TOOLS if phase.profile == "writer" else _READER_TOOLS

    def _dispatch_tool(self, phase: Phase, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = self._allowed_tools(phase)
        if tool_name not in allowed:
            raise PermissionError(
                f"{tool_name!r} not allowed under {phase.profile!r} profile " f"(phase={phase.name!r})"
            )
        # No tools wired in this plan — concrete dispatch lands in 13-02+.
        return {"tool": tool_name, "phase": phase.name, "payload": payload}

    # ------------------------------------------------------------------
    # Internal: agent loop + block conversion
    # ------------------------------------------------------------------

    def _run_agent_loop(
        self, phase: Phase, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], PhaseCacheStats, str]:
        """Issue one provider call, record breakpoint + ledger telemetry.

        Per the 13-01 scope, the agent loop is intentionally a single
        turn: the model is expected to emit the phase-completion sentinel
        and the loop terminates. Multi-turn tool-call iteration lands in
        later plans (13-02+) where reader/writer tools become wired.
        """
        text, in_tok, out_tok, cache_read, cache_write = self.provider.complete(messages)
        # Append a minimal assistant turn so blocks reflect what happened.
        messages = [*messages, {"role": "assistant", "content": text}]

        # D-07: cache breakpoint at phase tail.
        plan_record = self.planner.plan_with_history(
            blocks=self._to_blocks(messages),
            prior_prefix_hash=self.diag.last_prefix_hash,
        )
        self.diag.record_plan(plan_record)

        # Telemetry — additive ledger fields cache_write_tokens + phase.
        self.ledger.record_call(
            operation=f"phase:{phase.name}",
            model=self._model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            phase=phase.name,
            stable_prefix_hash=plan_record.prefix_hash,
            prefix_invalidated_reason=plan_record.invalidated_reason,
        )

        stats = PhaseCacheStats(
            prefix_hash=plan_record.prefix_hash,
            prefix_tokens=plan_record.prefix_tokens,
            dynamic_tokens=plan_record.dynamic_tokens,
            total_tokens=plan_record.total_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            invalidated_reason=plan_record.invalidated_reason,
        )
        return messages, stats, text

    def _to_blocks(self, messages: list[dict[str, Any]]) -> list[PromptBlock]:
        """Convert the message list to PromptBlocks for cache planning.

        - messages[0] (system) → SYSTEM/STATIC (the byte-stable shell).
        - user phase objectives → USER_TASK/BRANCH (stable across turns
          within a phase) with the required override reason.
        - everything else (assistant/tool turns) → USER_TASK/TURN.
        """
        blocks: list[PromptBlock] = []
        for idx, msg in enumerate(messages):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            if idx == 0 and role == "system":
                blocks.append(
                    PromptBlock(
                        id="phase_runner/shell",
                        kind=BlockKind.SYSTEM,
                        stability=Stability.STATIC,
                        content=content,
                    )
                )
                continue
            if role == "user" and self._is_objective(content):
                phase_name = self._phase_name_for_objective(content) or f"idx{idx}"
                blocks.append(
                    PromptBlock(
                        id=f"phase_runner/objective/{phase_name}",
                        kind=BlockKind.USER_TASK,
                        stability=Stability.BRANCH,
                        content=content,
                        stability_override_reason=("phase objective is per-phase but stable across turns"),
                    )
                )
                continue
            blocks.append(
                PromptBlock(
                    id=f"phase_runner/turn/{idx}",
                    kind=BlockKind.USER_TASK,
                    stability=Stability.TURN,
                    content=content,
                )
            )
        return blocks

    def _is_objective(self, content: str) -> bool:
        return content in self._objectives.values()

    def _phase_name_for_objective(self, content: str) -> str | None:
        for name, body in self._objectives.items():
            if body == content:
                return name
        return None
