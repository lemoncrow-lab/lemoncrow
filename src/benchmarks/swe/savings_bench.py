"""WP-19 smoke harness for the deprecated V2 context-savings benchmark.

This module is deprecated for measurement. The V2 YAML suite is retained so the
runner scaffolding and historical traces remain reproducible, but percentage
claims are no longer asserted here. WP-50's ``savings_replay`` harness is the
measured V3 source of truth.

Runs an 11-prompt suite in two modes:

  A) ``ATELIER_DISABLE_ALL=1`` — vanilla path, no Atelier levers
  B) defaults                  — all V2 levers active

All token counts come from ``benchmarks/swe/prompts_11.yaml``; there is no
network access and no API key required.  The benchmark is designed to be
reproducible in < 90 s on a laptop.

Usage (CLI)::

    LOCAL=1 uv run python -m benchmarks.swe.savings_bench --json

Usage (programmatic)::

    from benchmarks.swe.savings_bench import run_savings_bench
    result = run_savings_bench(tmp_path)
    assert result.reduction_pct >= 0.0
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

yaml: Any
try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_SUITE_YAML = Path(os.environ.get("ATELIER_BENCHMARK_SUITE", Path(__file__).parent / "prompts_11.yaml"))

_ALL_LEVERS = (
    "smart_read",
    "ast_truncation",
    "memory_recall",
    "compact_lifecycle",
    "batch_edit",
    "search_read",
    "sql_inspect",
    "cached_grep",
)


@dataclass
class PromptResult:
    """Per-prompt measurement."""

    id: str
    task_type: str
    prompt: str

    # Run A — vanilla (ATELIER_DISABLE_ALL=1)
    naive_input_tokens: int
    naive_output_tokens: int

    # Run B — all levers on
    optimized_input_tokens: int

    # Lever attribution
    lever_savings: dict[str, int]

    @property
    def tokens_saved(self) -> int:
        return self.naive_input_tokens - self.optimized_input_tokens

    @property
    def reduction_pct(self) -> float:
        if self.naive_input_tokens == 0:
            return 0.0
        return self.tokens_saved / self.naive_input_tokens * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "naive_input_tokens": self.naive_input_tokens,
            "optimized_input_tokens": self.optimized_input_tokens,
            "tokens_saved": self.tokens_saved,
            "reduction_pct": round(self.reduction_pct, 2),
            "lever_savings": self.lever_savings,
        }


@dataclass
class SavingsResult:
    """Aggregate result across all 11 prompts."""

    total_naive_input: int = 0
    total_optimized_input: int = 0
    total_output_tokens: int = 0
    prompt_results: list[PromptResult] = field(default_factory=list)

    # Per-lever aggregate totals
    lever_totals: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens_saved(self) -> int:
        return self.total_naive_input - self.total_optimized_input

    @property
    def reduction_pct(self) -> float:
        if self.total_naive_input == 0:
            return 0.0
        return self.total_tokens_saved / self.total_naive_input * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_naive_input": self.total_naive_input,
            "total_optimized_input": self.total_optimized_input,
            "total_tokens_saved": self.total_tokens_saved,
            "total_output_tokens": self.total_output_tokens,
            "reduction_pct": round(self.reduction_pct, 2),
            "lever_totals": self.lever_totals,
            "prompts": [p.to_dict() for p in self.prompt_results],
        }


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _load_suite(suite_path: Path = _SUITE_YAML) -> list[dict[str, Any]]:
    """Load the 11-prompt YAML suite.

    Supports both *pyyaml* (when installed) and a minimal built-in fallback
    parser so the benchmark works in stripped CI environments.
    """
    if yaml is not None:
        with suite_path.open() as fh:
            data = yaml.safe_load(fh)
        return cast(list[dict[str, Any]], data["prompts"])

    # ------- minimal fallback parser (no pyyaml) -------
    # This is intentionally narrow: it only handles the exact structure
    # of prompts_11.yaml.  A real YAML parser is preferred.
    prompts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_levers = False
    in_prompt_block = False
    prompt_lines: list[str] = []

    for raw_line in suite_path.read_text().splitlines():
        line = raw_line.rstrip()

        if line.startswith("  - id:"):
            if current is not None:
                if prompt_lines:
                    current["prompt"] = " ".join(prompt_lines).strip()
                prompts.append(current)
            current = {"levers": {}}
            current["id"] = line.split(":", 1)[1].strip()
            in_levers = False
            in_prompt_block = False
            prompt_lines = []
            continue

        if current is None:
            continue

        stripped = line.strip()

        if stripped.startswith("task_type:"):
            current["task_type"] = stripped.split(":", 1)[1].strip()
            in_prompt_block = False
        elif stripped == "prompt: >":
            in_prompt_block = True
            prompt_lines = []
        elif in_prompt_block and stripped and not stripped.endswith(":"):
            prompt_lines.append(stripped)
            # If next indented line also belongs to prompt we stay in block
        elif stripped.startswith("naive_input_tokens:"):
            in_prompt_block = False
            current["naive_input_tokens"] = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("naive_output_tokens:"):
            current["naive_output_tokens"] = int(stripped.split(":", 1)[1].strip())
        elif stripped == "levers:":
            in_levers = True
        elif in_levers and ":" in stripped and not stripped.startswith("#"):
            k, v = stripped.split(":", 1)
            import contextlib

            with contextlib.suppress(ValueError):
                current["levers"][k.strip()] = int(v.split("#")[0].strip())

    if current is not None:
        if prompt_lines:
            current["prompt"] = " ".join(prompt_lines).strip()
        prompts.append(current)

    return prompts


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------


def _is_disabled() -> bool:
    """Return True when ATELIER_DISABLE_ALL env var is set to a truthy value."""
    val = os.environ.get("ATELIER_DISABLE_ALL", "")
    return val.lower() in {"1", "true", "yes"}


def _run_single_prompt(entry: dict[str, Any], levers_active: bool) -> PromptResult:
    """Compute token measurements for one prompt."""
    naive_input = int(entry["naive_input_tokens"])
    naive_output = int(entry["naive_output_tokens"])
    raw_levers: dict[str, Any] = entry.get("levers", {})

    lever_savings: dict[str, int] = {}
    if levers_active:
        for lever in _ALL_LEVERS:
            saved = int(raw_levers.get(lever, 0))
            if saved > 0:
                lever_savings[lever] = saved

    total_lever_saved = sum(lever_savings.values())
    # Clamp: optimized can never exceed naive (guard against bad YAML)
    optimized_input = max(0, naive_input - total_lever_saved)

    return PromptResult(
        id=entry["id"],
        task_type=entry["task_type"],
        prompt=str(entry.get("prompt", "")),
        naive_input_tokens=naive_input,
        naive_output_tokens=naive_output,
        optimized_input_tokens=optimized_input,
        lever_savings=lever_savings,
    )


def run_savings_bench(
    work_dir: Path | None = None,  # kept for API compatibility with tests
    suite_path: Path = _SUITE_YAML,
) -> SavingsResult:
    """Run the full 11-prompt savings benchmark.

    Always applies all levers (path B) and computes reduction vs. path A.
    ``ATELIER_DISABLE_ALL`` in the process environment is intentionally
    *ignored* here — the benchmark itself simulates both modes from the YAML
    data model so it remains deterministic regardless of env flags.

    Args:
        work_dir: Optional working directory (unused; accepted for test
            compatibility with ``pytest``'s ``tmp_path`` fixture).
        suite_path: Path to the YAML suite file (default: prompts_11.yaml).

    Returns:
        SavingsResult with per-prompt and aggregate measurements.
    """
    entries = _load_suite(suite_path)

    result = SavingsResult()

    for entry in entries:
        # Run A: no levers
        pr_a = _run_single_prompt(entry, levers_active=False)
        # Run B: all levers
        pr_b = _run_single_prompt(entry, levers_active=True)

        result.total_naive_input += pr_a.naive_input_tokens
        result.total_optimized_input += pr_b.optimized_input_tokens
        result.total_output_tokens += pr_a.naive_output_tokens

        # Accumulate lever totals
        for lever, saved in pr_b.lever_savings.items():
            result.lever_totals[lever] = result.lever_totals.get(lever, 0) + saved

        # Store the *optimized* view as the canonical per-prompt result
        result.prompt_results.append(pr_b)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_text_report(r: SavingsResult) -> str:
    lines = [
        "# Atelier V2 — Context Savings Benchmark",
        f"Prompts: {len(r.prompt_results)}",
        f"Total naive input tokens:     {r.total_naive_input:,}",
        f"Total optimized input tokens: {r.total_optimized_input:,}",
        f"Tokens saved:                 {r.total_tokens_saved:,}",
        f"Reduction:                    {r.reduction_pct:.2f}%",
        "",
        "## Per-lever totals",
    ]
    for lever, saved in sorted(r.lever_totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {lever:<22} {saved:,} tokens")

    lines += ["", "## Per-prompt results", ""]
    lines.append(f"{'ID':<28} {'Naive':>8} {'Opt':>8} {'Saved':>8} {'%':>7}")
    lines.append("-" * 65)
    for pr in r.prompt_results:
        lines.append(
            f"{pr.id:<28} {pr.naive_input_tokens:>8,} "
            f"{pr.optimized_input_tokens:>8,} "
            f"{pr.tokens_saved:>8,} "
            f"{pr.reduction_pct:>6.1f}%"
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    emit_json = "--json" in args

    result = run_savings_bench()

    if emit_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(_build_text_report(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
