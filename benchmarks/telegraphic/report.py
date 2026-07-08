"""Render the telegraphic savings table -- baseline vs any of: full atelier
runtime, atelier's telegraphic register alone, caveman's own skill alone.

Reads ``results.jsonl`` (one row per (prompt, arm, rep), ``ArmResult``-shaped
for codebench arms, ``extra_arms.run_extra_arm``-shaped for the isolated
system-prompt-only arms -- same field names either way) and reports real
per-prompt output-token counts per arm, plus each non-baseline arm's percent
delta vs baseline, from ``output_tokens`` -- Claude's actual reported usage,
not a tokenizer approximation. Ad-hoc tasks are named ``local1``..``localN``
in run order, matching ``benchmarks.codebench.local.build_local_tasks`` --
``_prompt_index`` maps that back to this suite's ``prompts.json`` order.
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

_TASK_RE = re.compile(r"^local(\d+)$")

_ARM_LABELS = {
    "baseline": "Baseline",
    "atelier": "Atelier (full runtime)",
    "atelier-telegraphic": "Atelier (telegraphic only)",
    "caveman": "Caveman",
}


def load_results(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _prompt_index(task: str) -> int | None:
    m = _TASK_RE.match(task)
    return int(m.group(1)) - 1 if m else None


def render_report(results: list[dict], prompt_entries: list[dict]) -> str:
    by_prompt: dict[int, dict[str, list[dict]]] = {}
    arm_order: list[str] = []
    for row in results:
        idx = _prompt_index(str(row.get("task", "")))
        if idx is None or not (0 <= idx < len(prompt_entries)):
            continue
        arm = str(row["arm"])
        if arm not in arm_order:
            arm_order.append(arm)
        by_prompt.setdefault(idx, {}).setdefault(arm, []).append(row)

    if not arm_order:
        return "_No results.jsonl rows matched any prompt._"

    if "baseline" in arm_order:
        arm_order = ["baseline"] + [a for a in arm_order if a != "baseline"]

    headers = ["ID", "Source"] + [_ARM_LABELS.get(a, a) for a in arm_order]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["----", "------"] + ["------------------:"] * (len(arm_order))) + "|",
    ]

    totals: dict[str, list[float]] = {a: [] for a in arm_order}
    skipped: list[str] = []

    for idx, entry in enumerate(prompt_entries):
        arms_here = by_prompt.get(idx, {})
        medians: dict[str, float | None] = {}
        for a in arm_order:
            ok_rows = [r for r in arms_here.get(a, []) if r.get("ok") and not r.get("is_error")]
            medians[a] = statistics.median(r["output_tokens"] for r in ok_rows) if ok_rows else None
        if any(v is None for v in medians.values()):
            skipped.append(entry["id"])
            continue
        for a in arm_order:
            totals[a].append(medians[a])  # type: ignore[arg-type]
        cells = [entry["id"], entry["source"]] + [str(int(medians[a])) for a in arm_order]  # type: ignore[arg-type]
        lines.append("| " + " | ".join(cells) + " |")

    n = len(totals[arm_order[0]])
    if n:
        avg_cells = ["**Average**", ""] + [f"**{round(statistics.mean(totals[a]))}**" for a in arm_order]
        lines.append("| " + " | ".join(avg_cells) + " |")
        lines.append("")
        if "baseline" in arm_order:
            for a in arm_order:
                if a == "baseline":
                    continue
                deltas = [1 - v / b if b else 0.0 for v, b in zip(totals[a], totals["baseline"], strict=True)]
                lines.append(
                    f"_{_ARM_LABELS.get(a, a)} vs baseline: mean {round(statistics.mean(deltas) * 100)}%, "
                    f"median {round(statistics.median(deltas) * 100)}%, "
                    f"range {round(min(deltas) * 100)}%-{round(max(deltas) * 100)}%, "
                    f"stdev {statistics.pstdev(deltas) * 100:.0f}pp across {n} prompts._"
                )
        if skipped:
            lines.append(f"\n_Skipped (errored/missing arm): {', '.join(skipped)}._")
    elif skipped:
        lines.append(f"\nAll prompts errored or missing an arm: {', '.join(skipped)}")

    return "\n".join(lines)
