"""Generate side-by-side benchmark reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.swe.metrics import RunMetrics, aggregate, read_metrics

_HEADERS = [
    ("mode", "Mode"),
    ("attempts", "Tasks"),
    ("resolve_rate", "Resolve %"),
    ("cost_usd", "Cost $"),
    ("tokens_input", "In tok"),
    ("tokens_output", "Out tok"),
    ("turns", "Turns"),
    ("tool_calls", "Tools"),
    ("wall_time_seconds", "Time s"),
    ("monitor_events", "Mon ev"),
    ("compression_events", "Comp ev"),
    ("reasonblock_hits", "RB hits"),
    ("rescue_count", "Rescue"),
    ("rubric_pass", "Rubric✓"),
]


def render_markdown(by_mode: dict[str, dict[str, Any]], meta: dict[str, Any]) -> str:
    out: list[str] = []
    out.append("# SWE-bench harness — Atelier vs vanilla\n")
    out.append("")
    out.append("## Run")
    out.append(f"- dataset: `{meta.get('dataset_name')}` ({meta.get('split')})")
    out.append(f"- agent_host: `{meta.get('agent_host')}` model: `{meta.get('model')}`")
    out.append(f"- tasks: {meta.get('task_count')}  attempts/task: {meta.get('attempts_per_task')}")
    out.append(f"- seed: `{meta.get('seed')}`  config: `{meta.get('config_path')}`")
    out.append("")
    out.append("## Side-by-side")
    head = "| " + " | ".join(h[1] for h in _HEADERS) + " |"
    sep = "| " + " | ".join("---" for _ in _HEADERS) + " |"
    out.append(head)
    out.append(sep)
    for mode_name, agg in by_mode.items():
        agg_with_mode = {"mode": mode_name, **agg}
        cells: list[str] = []
        for key, _ in _HEADERS:
            v = agg_with_mode.get(key, 0)
            if key == "resolve_rate":
                cells.append(f"{float(v) * 100:.1f}%")
            elif key == "cost_usd":
                cells.append(f"${float(v):.4f}")
            elif key == "wall_time_seconds":
                cells.append(f"{float(v):.2f}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    out.append("## Notes")
    out.append("- Resolve % comes from the configured evaluator (mock if `swebench` is absent).")
    out.append(
        "- Costs use the same per-1M token rates as `atelier benchmark` (see `cost_tracker.py`)."
    )
    out.append("- ReasonBlock hits / monitor events are zero by construction in `vanilla` mode.")
    return "\n".join(out) + "\n"


def write_report(
    metrics_path: Path, report_md: Path, report_json: Path, meta: dict[str, Any]
) -> None:
    rows: list[RunMetrics] = read_metrics(metrics_path)
    by_mode = aggregate(rows)
    md = render_markdown(by_mode, meta)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(md)
    payload = {
        "meta": meta,
        "by_mode": by_mode,
        "rows": [r.model_dump() for r in rows],
    }
    report_json.write_text(json.dumps(payload, indent=2, sort_keys=True))


def write_combined_report(run_dir: Path, meta: dict[str, Any]) -> tuple[Path, Path]:
    """Combine every ``metrics_<mode>.jsonl`` in ``run_dir`` into one report."""
    rows: list[RunMetrics] = []
    for p in sorted(run_dir.glob("metrics_*.jsonl")):
        rows.extend(read_metrics(p))
    by_mode = aggregate(rows)
    md = render_markdown(by_mode, meta)
    rep_md = run_dir / "report.md"
    rep_json = run_dir / "report.json"
    rep_md.write_text(md)
    rep_json.write_text(
        json.dumps(
            {
                "meta": meta,
                "by_mode": by_mode,
                "rows": [r.model_dump() for r in rows],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return rep_md, rep_json
