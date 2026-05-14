"""``atelier benchmark swe`` — Click command group entry point.

Subcommands:
    run                     run the harness end-to-end against a config
    evaluate                shell out to the official swebench evaluator (or mock)
    report                  re-render report.md/json from existing metrics
    show-modes              print the mode matrix
    measure-context-savings run the WP-19 11-prompt savings benchmark
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click

from benchmarks.swe.agent_runner import build_agent
from benchmarks.swe.config import load_config
from benchmarks.swe.datasets import gold_patch_lookup, load_tasks
from benchmarks.swe.metrics import RunMetrics, write_metrics
from benchmarks.swe.modes import mode_specs
from benchmarks.swe.patch_export import export_predictions
from benchmarks.swe.report import write_combined_report
from benchmarks.swe.swebench_eval import (
    ensure_dependency_or_print,
    mock_evaluate,
)
from benchmarks.swe.swebench_eval import (
    evaluate as run_swebench_eval,
)
from benchmarks.swe.task_runner import run_one


@click.group(name="swe", help="SWE-bench harness for Atelier.")
def swe() -> None:
    pass


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """SWE-bench benchmarking harnesses for Atelier."""


cli.add_command(swe)


# ---------------------------------- run ----------------------------------- #


@swe.command("run")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_override", default=None, type=click.Path(file_okay=False))
def cmd_run(config_path: str, out_override: str | None) -> None:
    """Execute the configured benchmark and write predictions + metrics."""
    cfg = load_config(config_path)
    if cfg.warm_required_but_missing():
        raise click.ClickException("warm_reasonblocks_path is required when modes include atelier_warm_reasonblocks")
    out_dir = Path(out_override or cfg.output_dir) / time.strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = load_tasks(cfg)
    if not tasks:
        raise click.ClickException("no tasks loaded — check dataset_name/task_ids")

    agent = build_agent(cfg)
    meta = {
        "dataset_name": cfg.dataset_name,
        "split": cfg.split,
        "agent_host": cfg.agent_host,
        "model": cfg.model,
        "task_count": len(tasks),
        "attempts_per_task": cfg.attempts_per_task,
        "seed": cfg.seed,
        "config_path": str(Path(config_path).resolve()),
        "modes": [m.value for m in cfg.modes],
        "max_cost_usd": cfg.max_cost_usd,
        "max_turns": cfg.max_turns,
        "timeout_seconds": cfg.timeout_seconds,
    }
    (out_dir / "config.snapshot.json").write_text(json.dumps(meta, indent=2, sort_keys=True))

    for mode in cfg.modes:
        rows: list[RunMetrics] = []
        preds: list[tuple[str, str, str]] = []
        for task in tasks:
            for attempt in range(1, cfg.attempts_per_task + 1):
                m = run_one(
                    task=task,
                    mode=mode,
                    attempt=attempt,
                    cfg=cfg,
                    agent=agent,
                    out_dir=out_dir,
                )
                rows.append(m)
                if attempt == 1:
                    patch_text = ""
                    if m.patch_path:
                        patch_text = Path(m.patch_path).read_text()
                    preds.append((task.instance_id, f"{cfg.agent_host}:{cfg.model}:{mode.value}", patch_text))
        write_metrics(rows, out_dir / f"metrics_{mode.value}.jsonl")
        export_predictions(preds, out_dir / f"predictions_{mode.value}.jsonl")
        click.echo(f"[swe] mode={mode.value} attempts={len(rows)} -> {out_dir}")

    rep_md, rep_json = write_combined_report(out_dir, meta)
    click.echo(f"[swe] report: {rep_md}")
    click.echo(f"[swe] report: {rep_json}")


# ------------------------------- evaluate --------------------------------- #


@swe.command("evaluate")
@click.option("--run-dir", "run_dir", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--mode", default=None, help="Evaluate a single mode; default: every predictions_*.jsonl")
@click.option("--mock/--official", default=False, help="Force the dependency-free mock evaluator")
def cmd_evaluate(run_dir: str, mode: str | None, mock: bool) -> None:
    """Run the official SWE-bench evaluator on the predictions files."""
    rd = Path(run_dir)
    cfg_path = rd / "config.snapshot.json"
    cfg_meta: dict[str, Any] = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    dataset_name = cfg_meta.get("dataset_name", "swe_bench_lite")
    pred_files = sorted(rd.glob("predictions_*.jsonl"))
    if mode:
        pred_files = [p for p in pred_files if p.stem == f"predictions_{mode}"]
    if not pred_files:
        raise click.ClickException(f"no predictions_*.jsonl in {rd}")

    if not mock and not ensure_dependency_or_print():
        mock = True

    out: dict[str, Any] = {}
    if mock:
        # Reconstruct a minimal cfg for gold lookup.
        from benchmarks.swe.config import BenchConfig as _BC

        bc = _BC(dataset_name=dataset_name, task_limit=10_000)
        gold = gold_patch_lookup(bc)
        for pf in pred_files:
            out[pf.stem] = mock_evaluate(pf, gold)
    else:
        for pf in pred_files:
            out[pf.stem] = run_swebench_eval(pf, dataset_name=dataset_name, session_id=pf.stem)

    (rd / "evaluation.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    click.echo(json.dumps(out, indent=2))


# -------------------------------- report ---------------------------------- #


@swe.command("report")
@click.option("--run-dir", "run_dir", required=True, type=click.Path(exists=True, file_okay=False))
def cmd_report(run_dir: str) -> None:
    """Re-render report.md / report.json from existing metrics jsonl files."""
    rd = Path(run_dir)
    cfg_path = rd / "config.snapshot.json"
    meta = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    md, js = write_combined_report(rd, meta)
    click.echo(f"[swe] {md}")
    click.echo(f"[swe] {js}")


# ----------------------------- show-modes --------------------------------- #


@swe.command("show-modes")
def cmd_show_modes() -> None:
    """Print the mode matrix."""
    for mode, spec in mode_specs().items():
        click.echo(
            f"- {mode.value}: mcp={spec.mcp_available} "
            f"forced={list(spec.forced_steps)} "
            f"runtime={spec.enable_run_ledger} warm={spec.requires_warm_blocks}"
        )


# ----------------------- measure-context-savings -------------------------- #


@swe.command("measure-context-savings")
@click.option(
    "--suite",
    "suite_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to prompts YAML (default: benchmarks/swe/prompts_11.yaml)",
)
@click.option("--json", "emit_json", is_flag=True, default=False, help="Emit JSON output")
def cmd_measure_context_savings(suite_path: str | None, emit_json: bool) -> None:
    """Run the WP-19 11-prompt context-savings benchmark.

    Measures the token reduction achieved by all V2 Atelier levers
    (smart_read, AST truncation, memory recall, compact lifecycle,
    batch_edit, search_read, sql_inspect, cached_grep) against the
    vanilla baseline (ATELIER_DISABLE_ALL=1).

    Exits with code 1 when the aggregate reduction is below 50 %.
    """
    from pathlib import Path as _Path

    from benchmarks.swe.savings_bench import _build_text_report, run_savings_bench

    kw: dict[str, Any] = {}
    if suite_path is not None:
        kw["suite_path"] = _Path(suite_path)

    result = run_savings_bench(**kw)

    if emit_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
    else:
        click.echo(_build_text_report(result))

    if result.reduction_pct < 50.0:
        raise click.ClickException(f"context savings {result.reduction_pct:.2f}% is below the 50% CI gate")


def main() -> None:
    raise click.ClickException("atelier-bench has been removed; use `atelier benchmark swe ...`")


if __name__ == "__main__":
    main()
