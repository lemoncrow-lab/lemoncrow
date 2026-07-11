"""BYO-repo support for the CodeBench A/B engine.

Synthesizes ephemeral :class:`~benchmarks.codebench.tasks.Task` objects from
inline user prompts pointed at the user's own repository, and estimates the
real-token cost of a head-to-head run before any provider spend happens.

This reuses the existing CodeBench runner end to end -- it only adds task
synthesis (workspace-copy source) and a cost estimator. No engine rebuild.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from pathlib import Path

from lemoncrow.core.capabilities.pricing import usage_cost_usd

from benchmarks.codebench.tasks import Task

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bare short names accepted by the `claude` CLI map to a concrete model id so the
# pricing catalog can resolve a rate card for the estimate. The benchmark run
# itself still passes the short name straight through to the driver.
_SHORT_NAME_MODELS: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-7",
}

# Heuristic per-turn token shape when no historical runs are available to
# calibrate against. A non-trivial coding turn reads a few files and emits a
# patch; these are deliberately conservative round numbers, not measurements.
_HEURISTIC_INPUT_TOKENS_PER_TURN = 900
_HEURISTIC_OUTPUT_TOKENS_PER_TURN = 70

# Minimum historical samples required before we trust a calibrated median.
_MIN_CALIBRATION_SAMPLES = 3


def build_local_tasks(repo: Path, prompts: list[str], setup: list[str]) -> list[Task]:
    """Synthesize one workspace-copy :class:`Task` per inline prompt.

    Writes each prompt to ``<tmp>/tasks/local<i>/prompt.md`` and points
    ``CODEBENCH_TASKS_DIR`` at the temp root so ``Task.prompt()`` resolves.
    The repo is never mutated -- ``prepare_workspace`` copies it (minus
    ``.git``) into an isolated workspace per run.
    """
    repo_abs = repo.expanduser().resolve()
    tmp_root = Path(tempfile.mkdtemp(prefix="codebench-local-"))
    tasks: list[Task] = []
    for index, prompt in enumerate(prompts, start=1):
        task_dir = f"local{index}"
        prompt_dir = tmp_root / "tasks" / task_dir
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        tasks.append(
            Task(
                id=task_dir,
                language="generic",
                source=("path", str(repo_abs)),
                weight=1,
                task_dir=task_dir,
                setup_cmds=tuple(setup),
            )
        )
    os.environ["CODEBENCH_TASKS_DIR"] = str(tmp_root)
    return tasks


def _resolve_model_for_pricing(model: str) -> str:
    return _SHORT_NAME_MODELS.get(model.strip().lower(), model)


def _model_family(model_id: str) -> str:
    """Coarse model family ('opus'/'sonnet'/'haiku') for cost matching, or ''."""
    text = (model_id or "").strip().lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in text:
            return family
    return ""


def _calibration_samples() -> list[tuple[float, str]]:
    """(per-run cost, model family) pairs from prior CodeBench / local runs."""
    roots = [
        REPO_ROOT / "benchmarks" / "codebench" / "results",
        REPO_ROOT / "reports" / "benchmark" / "local",
    ]
    samples: list[tuple[float, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for jsonl_path in root.glob("*/results.jsonl"):
            try:
                text = jsonl_path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = obj.get("cost_usd")
                if not isinstance(cost, (int, float)) or cost <= 0:
                    continue
                family = ""
                models = obj.get("models")
                if isinstance(models, list) and models:
                    family = _model_family(str(models[0]))
                if not family:
                    usage = obj.get("model_usage")
                    if isinstance(usage, dict) and usage:
                        family = _model_family(str(next(iter(usage))))
                samples.append((float(cost), family))
    return samples


def estimate_cost(
    *,
    n_prompts: int,
    arms: int,
    reps: int,
    model: str,
    max_turns: int,
) -> dict[str, object]:
    """Estimate the real-token cost of a head-to-head local run.

    Calibrates ``per_run_usd`` from the median of historical per-run costs when
    at least three positive samples exist; otherwise falls back to a per-turn
    token heuristic priced through the shared catalog. The result is always an
    estimate -- the caller is responsible for labeling it as such.
    """
    n_runs = n_prompts * arms * reps
    priced_model = _resolve_model_for_pricing(model)
    target_family = _model_family(priced_model)
    matching = [cost for cost, family in _calibration_samples() if target_family and family == target_family]
    if len(matching) >= _MIN_CALIBRATION_SAMPLES:
        per_run_usd = float(statistics.median(matching))
        basis = "calibrated"
        assumption = f"median of {len(matching)} historical per-run costs for {target_family or 'comparable'} models"
    else:
        input_tokens = max_turns * _HEURISTIC_INPUT_TOKENS_PER_TURN
        output_tokens = max_turns * _HEURISTIC_OUTPUT_TOKENS_PER_TURN
        per_run_usd = usage_cost_usd(
            priced_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        basis = "heuristic"
        assumption = (
            f"~{_HEURISTIC_INPUT_TOKENS_PER_TURN:,} input + "
            f"{_HEURISTIC_OUTPUT_TOKENS_PER_TURN:,} output tokens per turn x "
            f"{max_turns} turns at {priced_model} rates"
        )
    total_usd = per_run_usd * n_runs
    return {
        "n_runs": n_runs,
        "per_run_usd": float(per_run_usd),
        "total_usd": float(total_usd),
        "low_usd": float(total_usd * 0.5),
        "high_usd": float(total_usd * 2.0),
        "basis": basis,
        "assumption": assumption,
    }
