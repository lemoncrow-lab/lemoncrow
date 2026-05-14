"""SWE-bench harness for Atelier.

Compares vanilla coding agents against Atelier-instrumented agents and
reports side-by-side numbers (cost, tokens, turns, time)
plus Atelier-specific metrics (ReasonBlock hits, monitor events, rescue
events, rubric verdicts).

Public entry points:

* :func:`benchmarks.swe.run_swe_bench.cli` — Click command group exposed
  as ``atelier benchmark swe`` subcommands.
* :class:`benchmarks.swe.config.BenchConfig`
* :class:`benchmarks.swe.modes.Mode`

Safety invariants
-----------------
* Never reads or passes the gold patch to the agent.
* Calibration ReasonBlock paths must not include eval task IDs.
* Every run records its config + git SHA for reproducibility.
"""

from benchmarks.swe.config import BenchConfig, load_config
from benchmarks.swe.metrics import RunMetrics
from benchmarks.swe.modes import Mode, mode_specs

__all__ = ["BenchConfig", "Mode", "RunMetrics", "load_config", "mode_specs"]
