"""Agent runners: a small abstraction so the harness can swap CLIs.

Only the ``mock`` agent is fully implemented (deterministic, no network)
so that unit tests, CI, and offline reproduction work. Real-host adapters
(``claude``, ``codex``, ``opencode``, ``copilot``, ``gemini``) raise
``NotImplementedError`` until wired by the integration installer; the CLI
prints actionable instructions when a host is missing.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from benchmarks.swe.config import BenchConfig
from benchmarks.swe.datasets import Task
from benchmarks.swe.modes import Mode, ModeSpec


@dataclass
class AgentResult:
    """What the harness needs from any agent invocation."""

    patch: str
    tokens_input: int = 0
    tokens_output: int = 0
    estimated_cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    file_reads: int = 0
    searches: int = 0
    repeated_commands: int = 0
    workflow_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    budget_cap_hit: bool = False


class Agent(Protocol):
    host: str

    def solve(self, task: Task, mode_spec: ModeSpec, cfg: BenchConfig) -> AgentResult: ...


# ----------------------------- mock agent --------------------------------- #


class MockAgent:
    """Deterministic offline agent used for tests and dry-runs.

    Behavior:
    - For ``mock__add-1`` it emits the gold-equivalent patch (so the run is
      ``resolved=True`` under the simple text-equality check used by the
      mock evaluator).
    - For other tasks it emits a small stub patch.
    - Atelier modes "use" more tools and accumulate workflow events so the
      report shows non-zero ReasonBlock / monitor counters; vanilla mode
      stays empty.
    """

    host = "mock"

    def __init__(self, model: str = "mock-1") -> None:
        self.model = model

    def solve(self, task: Task, mode_spec: ModeSpec, cfg: BenchConfig) -> AgentResult:
        time.sleep(0)  # placeholder for network latency
        seed = int(hashlib.sha256(task.instance_id.encode()).hexdigest(), 16) % 1000
        # Deterministic-but-distinct token usage per mode.
        base_in = 1500 + seed
        base_out = 400 + seed % 200
        if mode_spec.name == Mode.VANILLA:
            tokens_in, tokens_out = base_in, base_out
            tool_calls = 0
            workflow: list[dict[str, Any]] = []
        else:
            # Forced workflow does fewer wasted searches; full runtime even fewer.
            shrink = {
                Mode.ATELIER_TOOLS_AVAILABLE: 0.95,
                Mode.ATELIER_FORCED_WORKFLOW: 0.85,
                Mode.ATELIER_FULL_RUNTIME: 0.75,
                Mode.ATELIER_WARM_REASONBLOCKS: 0.60,
            }[mode_spec.name]
            tokens_in = int(base_in * shrink)
            tokens_out = int(base_out * shrink)
            tool_calls = len(mode_spec.forced_steps) or 2
            workflow = [{"event": s, "ok": True} for s in mode_spec.forced_steps]
            if mode_spec.enable_monitors:
                workflow.append({"event": "monitor_event", "kind": "repeated_command"})
            if mode_spec.enable_compressor:
                workflow.append({"event": "compression_event", "saved_tokens": 350})
            if mode_spec.requires_warm_blocks:
                workflow.append({"event": "reasonblock_hit", "block_id": "rb-warm"})
                workflow.append({"event": "reasonblock_hit", "block_id": "rb-warm-2"})

        # Cost: $3/M input + $15/M output (claude-sonnet-ish defaults).
        cost = tokens_in * 3.0 / 1_000_000 + tokens_out * 15.0 / 1_000_000

        # Patch heuristic
        if task.instance_id == "mock__add-1":
            patch = (
                "--- a/calc.py\n+++ b/calc.py\n" "@@ -1 +1 @@\n-def add(a,b): return a-b\n+def add(a,b): return a+b\n"
            )
        else:
            patch = f"--- a/{task.instance_id}.py\n+++ b/{task.instance_id}.py\n@@\n-pass\n+pass  # patched\n"

        budget_hit = cost > cfg.max_cost_usd
        if budget_hit:
            patch = ""

        return AgentResult(
            patch=patch,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            estimated_cost_usd=round(cost, 6),
            turns=2 + (1 if mode_spec.forced_steps else 0),
            tool_calls=tool_calls,
            file_reads=2,
            searches=1 if mode_spec.name == Mode.VANILLA else 0,
            repeated_commands=1 if mode_spec.name == Mode.VANILLA else 0,
            workflow_events=workflow,
            error=None,
            budget_cap_hit=budget_hit,
        )


# --------------------------- host adapters -------------------------------- #


class _UnsupportedAgent:
    """Stub for hosts not yet wired. Raises with an actionable message."""

    def __init__(self, host: str, model: str) -> None:
        self.host = host
        self.model = model

    def solve(self, task: Task, mode_spec: ModeSpec, cfg: BenchConfig) -> AgentResult:
        raise NotImplementedError(
            f"agent host '{self.host}' is not wired in the open-source harness. "
            "Use agent_host: mock for offline runs, or implement an adapter "
            "in benchmarks/swe/agent_runner.py and register it in build_agent()."
        )


def build_agent(cfg: BenchConfig) -> Agent:
    if cfg.agent_host == "mock":
        return MockAgent(model=cfg.model)
    return _UnsupportedAgent(cfg.agent_host, cfg.model)


def summarize_workflow(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull Atelier-specific counters out of an event list."""
    out: dict[str, Any] = {
        "monitor_events": 0,
        "compression_events": 0,
        "reasonblocks_retrieved": 0,
        "reasonblock_hits": 0,
        "check_plan_statuses": [],
        "rubric_status": None,
        "rescue_count": 0,
        "trace_id": None,
    }
    for e in events:
        ev = e.get("event")
        if ev == "monitor_event":
            out["monitor_events"] += 1
        elif ev == "compression_event":
            out["compression_events"] += 1
        elif ev == "get_reasoning_context":
            out["reasonblocks_retrieved"] += 1
        elif ev == "reasonblock_hit":
            out["reasonblock_hits"] += 1
        elif ev == "rescue_failure":
            out["rescue_count"] += 1
        elif ev == "run_rubric_gate":
            out["rubric_status"] = e.get("status", "pass")
        elif ev == "record_trace":
            out["trace_id"] = e.get("trace_id", "trace-stub")
    return out
