"""SWE-bench official-evaluator integration.

If the ``swebench`` package is installed we shell out to its harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name <name> \\
        --predictions_path <predictions.jsonl> \\
        --max_workers 4 \\
        --session_id <id>

Otherwise we print exact install instructions and return a "skipped" verdict
so unit tests and offline runs still pass.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

INSTALL_HINT = (
    "[swebench] official evaluator not installed.\n"
    "  pip install swebench\n"
    "  # or, with uv:  uv pip install swebench\n"
    "Then re-run:  uv run atelier-bench swe evaluate --run-dir <dir>\n"
    "Reference: https://github.com/princeton-nlp/SWE-bench"
)


def is_available() -> bool:
    return importlib.util.find_spec("swebench") is not None


def evaluate(
    predictions_path: Path,
    *,
    dataset_name: str,
    session_id: str,
    max_workers: int = 4,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Run the official harness if available; otherwise return a skip record."""
    if not is_available():
        print(INSTALL_HINT, file=sys.stderr)
        return {
            "status": "skipped",
            "reason": "swebench package not installed",
            "predictions_path": str(predictions_path),
        }
    if not predictions_path.is_file():
        return {"status": "error", "reason": f"predictions not found: {predictions_path}"}

    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(max_workers),
        "--session_id",
        session_id,
    ]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        return {"status": "error", "reason": f"failed to launch swebench: {e}"}

    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
    }


def mock_evaluate(
    predictions_path: Path,
    gold_patches: dict[str, str],
) -> dict[str, Any]:
    """Cheap, dependency-free evaluator used when ``swebench`` is missing.

    Marks an instance ``resolved`` if the predicted patch text equals the
    gold patch (after stripping). Useful for harness self-tests; never
    publish numbers based on this.
    """
    if not predictions_path.is_file():
        return {"status": "error", "reason": f"predictions not found: {predictions_path}"}
    resolved: list[str] = []
    failed: list[str] = []
    with predictions_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            iid = rec["instance_id"]
            pred = (rec.get("model_patch") or "").strip()
            gold = (gold_patches.get(iid) or "").strip()
            if pred and gold and pred == gold:
                resolved.append(iid)
            else:
                failed.append(iid)
    return {
        "status": "mock_ok",
        "resolved": resolved,
        "failed": failed,
        "resolve_rate": round(len(resolved) / max(1, len(resolved) + len(failed)), 4),
    }


def ensure_dependency_or_print() -> bool:
    """Helper for CLI: return True if available, else print hint."""
    if is_available():
        return True
    if shutil.which("python3"):
        print(INSTALL_HINT, file=sys.stderr)
    return False
