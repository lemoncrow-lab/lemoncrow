"""Wilson-score CI and per-cell summary assembly — AB-05."""

import datetime
import json
import math
from pathlib import Path

__all__ = ["compute_summary", "wilson_score_ci"]


def wilson_score_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Returns (lower, upper) where 0 ≤ lower ≤ upper ≤ 1.
    Handles the degenerate case n=0 → (0.0, 1.0).
    Does NOT use the normal approximation (which gives (0,0) for k=0).
    """
    if n == 0:
        return (0.0, 1.0)
    p_hat = k / n
    denominator = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denominator
    margin = (z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2))) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def _is_passed(record: dict) -> bool:
    """Return True iff the record's grader resolved the task."""
    return record.get("grader_is_resolved") is True


def compute_summary(run_id: str, raw_dir: Path) -> dict:
    """Scan raw_dir for completed trial JSON files and return summary.json schema dict.

    Cell key format: "{task_id}__{mode}" (no rep suffix).
    Never stores p_hat — only raw counts + CI bounds (AB-05).
    Skips malformed or partial-write (.tmp) files.
    """
    counts: dict[str, dict[str, int]] = {}

    for path in sorted(raw_dir.glob("*.json")):
        if path.suffix == ".tmp" or path.name.endswith(".json.tmp"):
            continue
        # Parse cell key: everything before the final __repN segment
        cell_key = path.stem.rsplit("__rep", 1)[0]
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  warn: skipping malformed file {path.name}: {exc}")
            continue
        entry = counts.setdefault(cell_key, {"passed": 0, "total": 0})
        entry["total"] += 1
        if _is_passed(record):
            entry["passed"] += 1

    cells: dict[str, dict] = {}
    for cell_key, data in counts.items():
        k, n = data["passed"], data["total"]
        lo, hi = wilson_score_ci(k, n)
        cells[cell_key] = {
            "passed": k,
            "total": n,
            "ci_lower": round(lo, 4),
            "ci_upper": round(hi, 4),
        }

    return {
        "run_id": run_id,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "cells": cells,
    }
