"""Render and persist mini eval reports as JSON and Markdown."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import MiniEvalReport

_STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "dry_run": "DRY RUN"}


def render_markdown(report: MiniEvalReport) -> str:
    """Render a :class:`MiniEvalReport` as a Markdown document."""
    status_label = _STATUS_LABEL.get(report.status, report.status.upper())
    lines: list[str] = [
        "# LemonCrow Mini Eval Report",
        "",
        f"**Status:** {status_label}",
        f"**Suite:** {report.suite}",
        f"**Ran:** {report.total_tasks} cases",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Accepted patch rate | {report.accepted_patch_rate:.2f} |",
        f"| Total cost | ${report.total_cost_usd:.2f} |",
        f"| Cost per accepted patch | ${report.cost_per_accepted_patch:.2f} |",
        f"| Cheap success rate | {report.cheap_success_rate:.2f} |",
        f"| Trace coverage | {report.trace_coverage_pct:.0f}% |",
        f"| Routing regressions | {report.routing_regression_rate * 100:.0f}% |",
    ]
    if report.context_reduction_pct is not None:
        lines.append(f"| Context reduction | {report.context_reduction_pct:.0f}% |")
    lines += [
        "",
        "## Cases",
        "",
        "| ID | Title | Status | Accepted | Cost | Trace |",
        "|----|-------|--------|----------|------|-------|",
    ]
    for case in report.cases:
        lines.append(
            f"| {case.id} | {case.title} | {case.status} "
            f"| {case.accepted} | ${case.estimated_cost_usd:.4f} "
            f"| {case.trace_id or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def save_report(report: MiniEvalReport, output_dir: Path) -> tuple[Path, Path]:
    """Write ``mini-report.json`` and ``mini-report.md`` into *output_dir*.

    Returns ``(json_path, md_path)``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "mini-report.json"
    md_path = output_dir / "mini-report.md"

    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


__all__ = ["render_markdown", "save_report"]
