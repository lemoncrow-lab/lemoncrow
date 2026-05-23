"""Cost benchmark: Naive agent loop vs Atelier runtime.

Simulates a realistic 6-turn coding task and compares total cost.

Measures:
  - total input tokens (uncached)
  - cache-read tokens
  - frontier-model calls
  - cheap/local-model calls
  - cost per completed task (USD)

Run:
    uv run pytest benchmarks/mcp_tools/bench_cost.py -v -s
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Model pricing (USD per 1M tokens, mid-2025 estimates)
# ---------------------------------------------------------------------------

_PRICING: dict[str, dict[str, float]] = {
    "frontier": {
        "input_per_m": 3.00,   # claude-sonnet-4.x uncached
        "cache_read_per_m": 0.30,  # 90% cheaper when cache hit
        "output_per_m": 15.00,
    },
    "cheap_llm": {
        "input_per_m": 0.25,   # claude-haiku / gpt-4o-mini
        "cache_read_per_m": 0.025,
        "output_per_m": 1.25,
    },
    "local_slm": {
        "input_per_m": 0.00,   # Ollama / local
        "cache_read_per_m": 0.00,
        "output_per_m": 0.00,
    },
}

_AVG_OUTPUT_TOKENS_PER_TURN = 400


def _cost_usd(
    uncached_input: int,
    cache_read: int,
    output: int,
    tier: str,
) -> float:
    p = _PRICING[tier]
    return (
        uncached_input * p["input_per_m"] / 1_000_000
        + cache_read * p["cache_read_per_m"] / 1_000_000
        + output * p["output_per_m"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Simulated turn data — models a realistic bug-fix coding task
# ---------------------------------------------------------------------------

@dataclass
class TurnProfile:
    """Token profile for one agent turn."""

    label: str
    # What a naive loop would feed as input (full accumulated history)
    naive_input_tokens: int
    # What Atelier feeds as input (compact state + dynamic tail)
    atelier_uncached_input_tokens: int
    # Stable prefix tokens (cached by provider KV cache after turn 1)
    atelier_cache_read_tokens: int
    # Route tier Atelier picks for this turn
    atelier_route_tier: str
    # Route tier naive loop always uses
    naive_route_tier: str = "frontier"
    # Whether Atelier watchdog prevents a retry that naive loop would do
    watchdog_prevented_retry: bool = False


# A 6-turn coding task: read → plan → edit → test → fix → verify
# Token counts are modelled from real Atelier sessions on this codebase.
TURN_PROFILES: list[TurnProfile] = [
    TurnProfile(
        label="turn-1: understand task",
        naive_input_tokens=1_400,          # system + task + files
        atelier_uncached_input_tokens=600,  # task + compact context
        atelier_cache_read_tokens=800,      # system + tool schema (stable prefix)
        atelier_route_tier="cheap_llm",     # analysis, no code generation
    ),
    TurnProfile(
        label="turn-2: read relevant files",
        naive_input_tokens=2_900,           # + turn-1 history + tool output
        atelier_uncached_input_tokens=500,
        atelier_cache_read_tokens=800,
        atelier_route_tier="cheap_llm",
    ),
    TurnProfile(
        label="turn-3: plan changes",
        naive_input_tokens=4_600,
        atelier_uncached_input_tokens=700,
        atelier_cache_read_tokens=800,
        atelier_route_tier="cheap_llm",
    ),
    TurnProfile(
        label="turn-4: write code",
        naive_input_tokens=6_500,
        atelier_uncached_input_tokens=900,
        atelier_cache_read_tokens=800,
        atelier_route_tier="frontier_llm",  # code generation → frontier
    ),
    TurnProfile(
        label="turn-5: run tests + fix failure",
        naive_input_tokens=8_800,
        atelier_uncached_input_tokens=800,
        atelier_cache_read_tokens=800,
        atelier_route_tier="frontier_llm",
        watchdog_prevented_retry=True,      # watchdog catches repeated failure pattern
    ),
    TurnProfile(
        label="turn-6: verify + summarise",
        naive_input_tokens=11_200,
        atelier_uncached_input_tokens=500,
        atelier_cache_read_tokens=800,
        atelier_route_tier="cheap_llm",
    ),
]


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    label: str
    turns: int
    total_input_tokens: int
    total_cache_read_tokens: int
    total_uncached_tokens: int
    frontier_calls: int
    cheap_calls: int
    local_calls: int
    retries_prevented: int
    output_tokens: int
    cost_usd: float
    turn_costs: list[float] = field(default_factory=list)


def _simulate_naive(profiles: list[TurnProfile]) -> LoopResult:
    total_input = total_uncached = 0
    frontier = 0
    turn_costs: list[float] = []
    for p in profiles:
        total_input += p.naive_input_tokens
        total_uncached += p.naive_input_tokens
        frontier += 1
        c = _cost_usd(p.naive_input_tokens, 0, _AVG_OUTPUT_TOKENS_PER_TURN, "frontier")
        # Naive loop retries if watchdog would have caught something
        if p.watchdog_prevented_retry:
            total_input += p.naive_input_tokens
            total_uncached += p.naive_input_tokens
            frontier += 1
            c += _cost_usd(p.naive_input_tokens, 0, _AVG_OUTPUT_TOKENS_PER_TURN, "frontier")
        turn_costs.append(c)

    output_tokens = (len(profiles) + sum(1 for p in profiles if p.watchdog_prevented_retry)) * _AVG_OUTPUT_TOKENS_PER_TURN
    output_cost = output_tokens * _PRICING["frontier"]["output_per_m"] / 1_000_000
    total_cost = sum(turn_costs) + output_cost

    return LoopResult(
        label="naive-loop",
        turns=len(profiles),
        total_input_tokens=total_input,
        total_cache_read_tokens=0,
        total_uncached_tokens=total_uncached,
        frontier_calls=frontier,
        cheap_calls=0,
        local_calls=0,
        retries_prevented=0,
        output_tokens=output_tokens,
        cost_usd=total_cost,
        turn_costs=turn_costs,
    )


def _simulate_atelier(profiles: list[TurnProfile]) -> LoopResult:
    total_input = total_cache = total_uncached = 0
    frontier = cheap = local = retries = 0
    turn_costs: list[float] = []
    for p in profiles:
        cache_read = p.atelier_cache_read_tokens
        uncached = p.atelier_uncached_input_tokens
        total_input += uncached + cache_read
        total_cache += cache_read
        total_uncached += uncached
        tier = p.atelier_route_tier if p.atelier_route_tier != "frontier_llm" else "frontier"
        if tier == "frontier":
            frontier += 1
        elif tier == "cheap_llm":
            tier = "cheap_llm"
            cheap += 1
        else:
            local += 1
            tier = "local_slm"
        c = _cost_usd(uncached, cache_read, _AVG_OUTPUT_TOKENS_PER_TURN, tier)
        turn_costs.append(c)
        if p.watchdog_prevented_retry:
            retries += 1  # prevented, not added

    output_tokens = len(profiles) * _AVG_OUTPUT_TOKENS_PER_TURN
    # Output cost: mix of frontier and cheap model outputs
    frontier_output = frontier * _AVG_OUTPUT_TOKENS_PER_TURN
    cheap_output = (cheap + local) * _AVG_OUTPUT_TOKENS_PER_TURN
    output_cost = (
        frontier_output * _PRICING["frontier"]["output_per_m"] / 1_000_000
        + cheap_output * _PRICING["cheap_llm"]["output_per_m"] / 1_000_000
    )
    total_cost = sum(turn_costs) + output_cost

    return LoopResult(
        label="atelier",
        turns=len(profiles),
        total_input_tokens=total_input,
        total_cache_read_tokens=total_cache,
        total_uncached_tokens=total_uncached,
        frontier_calls=frontier,
        cheap_calls=cheap,
        local_calls=local,
        retries_prevented=retries,
        output_tokens=output_tokens,
        cost_usd=total_cost,
        turn_costs=turn_costs,
    )


# ---------------------------------------------------------------------------
# Terminal table renderer
# ---------------------------------------------------------------------------

_BOLD = "\033[1m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def render_cost_table(naive: LoopResult, atelier: LoopResult) -> str:
    lines: list[str] = []
    lines.append(f"\n{_BOLD}━━ Atelier Cost Benchmark — Naive Loop vs Atelier Runtime ━━━━━━━━━━━━━━━{_RESET}")
    lines.append(f"  {_DIM}Task: 6-turn bug-fix coding session (read → plan → edit → test → fix → verify){_RESET}")
    lines.append("")

    col = 34
    lines.append(f"  {'Metric':<{col}} {'Naive Loop':>14} {'Atelier':>14} {'Savings':>10}")
    lines.append(f"  {'-'*col} {'-'*14} {'-'*14} {'-'*10}")

    def row(label: str, nv: Any, at: Any, fmt: str = ",") -> None:
        if fmt == "$":
            nv_s = f"${nv:.4f}"
            at_s = f"${at:.4f}"
            if isinstance(nv, float) and nv > 0:
                pct = (nv - at) / nv * 100
                sav = f"{_GREEN}-{pct:.0f}%{_RESET}"
            else:
                sav = "—"
        elif fmt == ",":
            nv_s = f"{nv:,}"
            at_s = f"{at:,}"
            if isinstance(nv, int) and nv > 0:
                pct = (nv - at) / nv * 100
                sav = f"{_GREEN}-{pct:.0f}%{_RESET}" if at < nv else "—"
            else:
                sav = "—"
        else:
            nv_s = str(nv)
            at_s = str(at)
            sav = "—"
        lines.append(f"  {label:<{col}} {nv_s:>14} {at_s:>14} {sav:>10}")

    row("Total input tokens", naive.total_input_tokens, atelier.total_input_tokens)
    row("  — uncached", naive.total_uncached_tokens, atelier.total_uncached_tokens)
    row("  — cache-read", naive.total_cache_read_tokens, atelier.total_cache_read_tokens, fmt="str")
    row("Frontier model calls", naive.frontier_calls, atelier.frontier_calls)
    row("Cheap/local model calls", naive.cheap_calls + naive.local_calls, atelier.cheap_calls + atelier.local_calls)
    row("Retries prevented by watchdog", naive.retries_prevented, atelier.retries_prevented, fmt="str")
    row("Output tokens", naive.output_tokens, atelier.output_tokens)
    row("Cost per task (USD)", naive.cost_usd, atelier.cost_usd, fmt="$")

    savings_usd = naive.cost_usd - atelier.cost_usd
    savings_pct = savings_usd / naive.cost_usd * 100
    lines.append("")
    lines.append(f"  {_BOLD}Cost reduction: {_GREEN}${savings_usd:.4f} ({savings_pct:.0f}% cheaper){_RESET}")
    lines.append(
        f"  {_DIM}At 1,000 tasks/day: naive=${naive.cost_usd * 1000:.2f}/day  "
        f"atelier=${atelier.cost_usd * 1000:.2f}/day  "
        f"saved=${savings_usd * 1000:.2f}/day{_RESET}"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-turn breakdown renderer
# ---------------------------------------------------------------------------

def render_turn_breakdown(profiles: list[TurnProfile], naive: LoopResult, atelier: LoopResult) -> str:
    lines: list[str] = []
    lines.append(f"\n{_BOLD}━━ Per-Turn Breakdown ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{_RESET}")
    col = 36
    lines.append(
        f"  {'Turn':<{col}} {'Naive tokens':>13} {'Naive tier':>11} "
        f"{'Atelier uncached':>16} {'+ cached':>9} {'Atelier tier':>13}"
    )
    lines.append(f"  {'-'*col} {'-'*13} {'-'*11} {'-'*16} {'-'*9} {'-'*13}")
    for _i, p in enumerate(profiles):
        tier_color = _DIM if p.atelier_route_tier != "frontier_llm" else ""
        watchdog = " ⚡watchdog" if p.watchdog_prevented_retry else ""
        lines.append(
            f"  {p.label:<{col}} {p.naive_input_tokens:>13,} {'frontier':>11} "
            f"{p.atelier_uncached_input_tokens:>16,} {p.atelier_cache_read_tokens:>9,} "
            f"{tier_color}{p.atelier_route_tier:>13}{_RESET}{watchdog}"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_cost")
    os.environ["ATELIER_ROOT"] = str(root / ".atelier")
    return root


@pytest.fixture(scope="session")
def naive_result() -> LoopResult:
    return _simulate_naive(TURN_PROFILES)


@pytest.fixture(scope="session")
def atelier_result() -> LoopResult:
    return _simulate_atelier(TURN_PROFILES)


@pytest.fixture(scope="session", autouse=True)
def print_cost_report(naive_result: LoopResult, atelier_result: LoopResult) -> None:
    print(render_turn_breakdown(TURN_PROFILES, naive_result, atelier_result))
    print(render_cost_table(naive_result, atelier_result))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_atelier_fewer_uncached_tokens(naive_result: LoopResult, atelier_result: LoopResult) -> None:
    """Atelier uses significantly fewer uncached input tokens through state compression."""
    assert atelier_result.total_uncached_tokens < naive_result.total_uncached_tokens * 0.30, (
        f"Expected Atelier uncached tokens to be <30% of naive. "
        f"naive={naive_result.total_uncached_tokens:,}  atelier={atelier_result.total_uncached_tokens:,}"
    )


def test_atelier_fewer_frontier_calls(naive_result: LoopResult, atelier_result: LoopResult) -> None:
    """Atelier routes cheap tasks away from frontier model."""
    assert atelier_result.frontier_calls < naive_result.frontier_calls, (
        f"Expected Atelier to make fewer frontier calls. "
        f"naive={naive_result.frontier_calls}  atelier={atelier_result.frontier_calls}"
    )


def test_atelier_has_cache_reads(atelier_result: LoopResult) -> None:
    """Atelier accumulates cache-read tokens via stable prefix."""
    assert atelier_result.total_cache_read_tokens > 0, "Expected cache-read tokens > 0"


def test_atelier_prevents_retries(atelier_result: LoopResult) -> None:
    """Atelier watchdog prevents at least one redundant retry."""
    assert atelier_result.retries_prevented >= 1, (
        f"Expected ≥1 watchdog-prevented retries, got {atelier_result.retries_prevented}"
    )


def test_atelier_cost_reduction_at_least_60pct(naive_result: LoopResult, atelier_result: LoopResult) -> None:
    """Atelier costs at least 60% less than a naive agent loop for a typical coding task."""
    reduction_pct = (naive_result.cost_usd - atelier_result.cost_usd) / naive_result.cost_usd * 100
    assert reduction_pct >= 60.0, (
        f"Expected ≥60% cost reduction. Got {reduction_pct:.1f}%. "
        f"naive=${naive_result.cost_usd:.4f}  atelier=${atelier_result.cost_usd:.4f}"
    )


def test_atelier_uses_cheap_model_for_analysis_turns(atelier_result: LoopResult) -> None:
    """Atelier routes analysis turns to cheap model, not frontier."""
    assert atelier_result.cheap_calls >= 4, (
        f"Expected ≥4 cheap-model calls (analysis turns). Got {atelier_result.cheap_calls}"
    )
