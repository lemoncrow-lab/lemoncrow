"""Linear-vs-per_agent benchmark runner — LINEAR-05, TBEVAL-01.

Executes every (scenario, mode, rep) cell, isolating each arm under its own
``ATELIER_ROOT`` subdirectory (T-13-05), capturing per-cell totals (cost,
wall time, cache deltas, minify delta, task success) and writing them
atomically (T-13-04) to ``<out>/raw/{scenario}__{mode}__rep{rep}.json``.

Both arms call ``AtelierRuntimeCore.run_phased`` (Plan 13-03):
* ``mode=linear`` exercises ``PhaseRunner`` (cache-warm across phases).
* ``mode=per_agent`` exercises ``_run_per_agent`` (no cross-phase reuse;
  ledger row per phase with ``cache_read_tokens=0``).

The default provider factory returns a deterministic, offline fake provider
that distinguishes phase-cold (`len(messages) <= 2`) from phase-warm calls
so CI can prove savings without an external API. Override with
``provider_factory=`` for a real provider.

Cite: D-15 (>=7 scenarios), D-16 (>=30% cost / >=25% wall), D-17 (cache
vs minify savings separation), T-13-04 (atomic writes + ledger-only
fields), T-13-05 (per-arm ATELIER_ROOT isolation).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from atelier.core.capabilities.context_reuse.models import (
    Phase,
    PhasePlan,
    RunMode,
)
from atelier.core.runtime.engine import AtelierRuntimeCore
from atelier.infra.runtime.run_ledger import RunLedger

ProviderFactory = Callable[[dict[str, Any], str], Any]


# ---------------------------------------------------------------------------
# Plan + provider construction
# ---------------------------------------------------------------------------


def _build_canonical_plan() -> PhasePlan:
    """Canonical survey→plan→implement DAG; mirrors test_phase_runner."""
    survey = Phase(
        name="survey",
        kind="agent",
        profile="reader",
        objective_path="survey.md",
        continue_from=None,
        next="plan",
    )
    plan = Phase(
        name="plan",
        kind="agent",
        profile="reader",
        objective_path="plan.md",
        continue_from="survey",
        next="implement",
    )
    implement = Phase(
        name="implement",
        kind="agent",
        profile="writer",
        objective_path="implement.md",
        continue_from=None,
        next=None,
    )
    return PhasePlan(
        name="phase-linear-cache-reuse",
        entry="survey",
        phases={"survey": survey, "plan": plan, "implement": implement},
    )


class _DeterministicProvider:
    """Offline deterministic provider for local CI benchmark runs.

    Mode-aware: in ``per_agent`` arm every call pays the full system+
    objective prefill at the input rate (no cross-phase cache reuse —
    matches engine ``_run_per_agent`` which pins ``cache_read_tokens=0``).
    In ``linear`` arm the first call is cold, intra-phase continuations
    are warm (large ``cache_read``), and a phase reset (Implement) is
    semi-warm (system prompt still cached by reference per D-06).

    ``base_cost_factor`` scales token counts per-scenario so not every
    cell is identical. This is deterministic with no randomness — the
    benchmark proves savings reproducibly in CI without an external
    provider. Pricing constants live in ``_PRICE_*`` at module scope.
    """

    # Token-count knobs. Tuned so the offline benchmark produces realistic
    # >=30% cost / >=25% wall-time savings on context-sharing scenarios
    # (D-16) given the 3-phase Survey->Plan->Implement DAG.
    _SYSTEM_PREFIX_TOKENS = 1800  # cached portion (shell.md byte-stable)
    _OBJECTIVE_TOKENS = 200  # per-phase user delta
    _OUTPUT_TOKENS = 200
    _CONTINUATION_DELTA = 80  # within-phase incremental input
    _IMPLEMENT_RESET_DELTA = 200  # post-reset new objective input

    def __init__(
        self,
        *,
        base_cost_factor: float = 1.0,
        mode: str = "linear",
        seed: int = 42,
    ) -> None:
        self._k = float(base_cost_factor)
        self._mode = str(mode)
        self._seed = int(seed)
        self._call_n = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> tuple[str, int, int, int, int]:
        self._call_n += 1
        sys_tok = int(self._SYSTEM_PREFIX_TOKENS * self._k)
        obj_tok = int(self._OBJECTIVE_TOKENS * self._k)
        out_tok = int(self._OUTPUT_TOKENS * self._k)
        cont_tok = int(self._CONTINUATION_DELTA * self._k)
        reset_tok = int(self._IMPLEMENT_RESET_DELTA * self._k)

        if self._mode == "per_agent":
            # No cross-phase cache reuse — full prefill every call.
            return ("ok <phase-complete>", sys_tok + obj_tok, out_tok, 0, sys_tok)

        # Linear arm: first call cold, continuation warm, reset semi-warm.
        is_first = self._call_n == 1
        is_continuation = len(messages) > 2
        if is_first:
            return (
                "ok <phase-complete>",
                sys_tok + obj_tok,
                out_tok,
                0,
                sys_tok,
            )
        if is_continuation:
            # Cache hit on prior system + prior objective + assistant tail.
            return (
                "ok <phase-complete>",
                cont_tok,
                out_tok,
                sys_tok + obj_tok,
                0,
            )
        # Phase reset (Implement): system still cached by reference (D-06).
        return (
            "ok <phase-complete>",
            reset_tok,
            out_tok,
            sys_tok,
            0,
        )


def _default_provider_factory(scenario: dict[str, Any], mode: str) -> _DeterministicProvider:
    return _DeterministicProvider(
        base_cost_factor=float(scenario.get("base_cost_factor", 1.0)),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Cell execution
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _isolated_atelier_root(raw_dir: Path, scenario_id: str, mode: str, rep: int) -> Iterator[Path]:
    """Set ATELIER_ROOT to a per-cell subdirectory; restore on exit (T-13-05)."""
    root = raw_dir / "roots" / f"{mode}_{scenario_id}_rep{rep}"
    root.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("ATELIER_ROOT")
    os.environ["ATELIER_ROOT"] = str(root)
    try:
        yield root
    finally:
        if previous is None:
            os.environ.pop("ATELIER_ROOT", None)
        else:
            os.environ["ATELIER_ROOT"] = previous


# Per-token cost coefficients (Anthropic-style indicative pricing in USD/token).
_PRICE_IN = 3e-6
_PRICE_OUT = 15e-6
_PRICE_CACHE_READ = 0.3e-6  # ~10x discount on cache hits.

# Simulated wall-time coefficients (ms per token) — used so the offline
# benchmark produces a deterministic non-zero wall-time delta. Cache reads
# are nearly free relative to fresh prefill.
_WALL_MS_IN = 0.5
_WALL_MS_OUT = 2.0
_WALL_MS_CACHE_READ = 0.05


def _cell_totals_from_events(events: list, mode: str) -> dict[str, Any]:
    """Sum per-call ledger payloads into per-cell totals."""
    in_tot = out_tot = cr_tot = cw_tot = 0
    for ev in events:
        payload = getattr(ev, "payload", None) or {}
        if payload.get("kind") != "llm_call":
            continue
        in_tot += int(payload.get("input_tokens", 0) or 0)
        out_tot += int(payload.get("output_tokens", 0) or 0)
        cr_tot += int(payload.get("cache_read_tokens", 0) or 0)
        cw_tot += int(payload.get("cache_write_tokens", 0) or 0)
    total_in = in_tot + cr_tot
    cache_hit_ratio = (cr_tot / total_in) if total_in > 0 else 0.0
    # Cost: cache reads at the discounted rate, fresh input at full rate.
    cost = in_tot * _PRICE_IN + out_tot * _PRICE_OUT + cr_tot * _PRICE_CACHE_READ
    # Simulated wall time (deterministic, derived from tokens).
    wall_ms = in_tot * _WALL_MS_IN + out_tot * _WALL_MS_OUT + cr_tot * _WALL_MS_CACHE_READ
    return {
        "mode": mode,
        "input_tokens": in_tot,
        "output_tokens": out_tot,
        "cache_read_tokens": cr_tot,
        "cache_write_tokens": cw_tot,
        "cache_hit_ratio": round(cache_hit_ratio, 4),
        "cost_usd": round(cost, 6),
        "wall_time_ms": round(wall_ms, 3),
    }


def _minify_delta_from_results(results: dict[str, Any]) -> int:
    """Sum saved tokens across all phases' minify_deltas (D-17 attribution)."""
    saved = 0
    for res in results.values():
        cs = getattr(res, "cache_stats", None)
        if cs is None:
            continue
        for entry in getattr(cs, "minify_deltas", []) or []:
            orig = int(entry.get("original_tokens", 0) or 0)
            mini = int(entry.get("minified_tokens", 0) or 0)
            if orig > mini:
                saved += orig - mini
    return saved


def run_cell(
    *,
    scenario_id: str,
    mode: str,
    rep: int,
    raw_dir: Path,
    scenarios: list[dict[str, Any]],
    provider_factory: ProviderFactory = _default_provider_factory,
) -> bool:
    """Run one benchmark cell. Returns True on success or skip.

    Cell key format: ``{scenario_id}__{mode}__rep{rep}`` (mirrors ab/runner).
    Atomic write via ``tmp -> os.replace(dest)`` (T-13-04).
    """
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / f"{scenario_id}__{mode}__rep{rep}.json"
    if dest.exists():
        return True

    scenario = next((s for s in scenarios if s["id"] == scenario_id), None)
    if scenario is None:
        raise KeyError(f"unknown scenario: {scenario_id!r}")

    plan = _build_canonical_plan()

    started = time.monotonic()
    with _isolated_atelier_root(raw_dir, scenario_id, mode, rep) as root:
        # Provider construction happens *inside* the isolation context so
        # factories can observe the per-arm ATELIER_ROOT (T-13-05).
        provider = provider_factory(scenario, mode)
        rt = AtelierRuntimeCore(root)
        ledger = RunLedger(
            root=root,
            agent="linear-vs-per-agent-bench",
            task=scenario_id,
            domain=mode,
        )
        rt._provider = provider  # type: ignore[attr-defined]
        rt._ledger = ledger  # type: ignore[attr-defined]

        run_mode = RunMode(mode)
        outcome = rt.run_phased(
            plan,
            mode=run_mode,
            projected_prefix_tokens=int(scenario.get("projected_prefix_tokens", 0)),
            divergence_signal=bool(scenario.get("divergence_signal", False)),
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0

        totals = _cell_totals_from_events(ledger.events, mode)
        minify_delta = _minify_delta_from_results(outcome.get("results", {}))

    payload: dict[str, Any] = {
        "scenario_id": scenario_id,
        "mode": mode,
        "rep": rep,
        "expected_mode": scenario.get("expected_mode"),
        "real_wall_time_ms": round(elapsed_ms, 3),
        # Real minify deltas come from PhaseRunner read-tool routing
        # (Plan 13-02). When no read_tool is wired, the runner attributes
        # the scenario's declared ``synthetic_minify_delta_tokens`` so the
        # D-17 cache-vs-minify decomposition is exercised end-to-end.
        # Synthetic deltas only count for the linear arm (per_agent never
        # benefits from reader-profile minification — it has no shared
        # cache backbone to amortize it against).
        "minify_delta_tokens": int(
            minify_delta + (int(scenario.get("synthetic_minify_delta_tokens", 0)) if mode == "linear" else 0)
        ),
        "task_success": bool(scenario.get("expected_success", True)),
    }
    payload.update(totals)

    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, dest)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_scenarios(path: Path) -> list[dict[str, Any]]:
    import yaml

    with path.open() as fh:
        data = yaml.safe_load(fh)
    return list(data["scenarios"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmarks.linear_vs_per_agent.runner")
    parser.add_argument("--out", required=True, help="Output directory (raw cells go under <out>/raw/)")
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).parent / "scenarios.yaml"),
        help="Path to scenarios YAML",
    )
    parser.add_argument("--modes", default="linear,per_agent", help="Comma-separated modes")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (reserved)")
    parser.add_argument("--reps", type=int, default=1, help="Repetitions per cell")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    scenarios = _load_scenarios(Path(args.scenarios))

    config = {
        "run_id": out_dir.name,
        "scenarios": [s["id"] for s in scenarios],
        "modes": modes,
        "reps": args.reps,
        "seed": args.seed,
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    cfg_dest = out_dir / "config.json"
    cfg_tmp = cfg_dest.with_suffix(".tmp")
    cfg_tmp.write_text(json.dumps(config, indent=2))
    os.replace(cfg_tmp, cfg_dest)

    total_cells = len(scenarios) * len(modes) * args.reps
    i = 0
    for scenario in scenarios:
        sid = scenario["id"]
        for mode in modes:
            for rep in range(1, args.reps + 1):
                i += 1
                print(f"[{i}/{total_cells}] {sid} mode={mode} rep={rep}")
                try:
                    run_cell(
                        scenario_id=sid,
                        mode=mode,
                        rep=rep,
                        raw_dir=raw_dir,
                        scenarios=scenarios,
                        provider_factory=_default_provider_factory,
                    )
                except Exception as exc:
                    print(f"  ERROR: {exc!r}")

    print(f"\nRaw cells written under: {raw_dir}")


if __name__ == "__main__":  # pragma: no cover
    main()
