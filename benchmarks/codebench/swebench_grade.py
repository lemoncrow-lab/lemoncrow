"""Grade SWE-bench candidate patches with the official ``swebench`` harness.

Mirrors :mod:`benchmarks.codebench.grade` (the multi-swe grader) but drives
``swebench.harness.run_evaluation``: write a predictions JSONL (one
``{instance_id, model_name_or_path, model_patch}`` row per instance), run the
Docker evaluation, then read ``resolved_ids`` from the run report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from benchmarks.codebench.run import REPO_ROOT
from benchmarks.codebench.swebench_data import DEFAULT_DATASET, DEFAULT_SPLIT

# Fixed prediction author label -> deterministic report filename.
MODEL_NAME = "lemoncrow-codebench"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _diff_paths(patch_text: str) -> set[str]:
    """Repo-relative paths touched by a unified diff (from its ``diff --git`` lines)."""
    paths: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            for tok in line.split()[2:]:
                paths.add(tok[2:] if tok.startswith(("a/", "b/")) else tok)
    return paths


def _strip_gold_test_files(model_patch: str, test_patch: str) -> str:
    """Drop model-patch sections for files the gold ``test_patch`` owns.

    The gold test patch is authoritative and is applied by the harness after the
    model patch. If the agent also touched those exact files (commonly by adding
    its own test or fixtures at the same path), ``git apply`` of the gold test
    patch fails with "already exists in working directory" and the real test
    never runs -> a false-negative. Stripping those sections keeps only the
    agent's solution code and lets the gold test decide.
    """
    gold = _diff_paths(test_patch)
    if not gold or not model_patch.strip():
        return model_patch
    kept: list[str] = []
    for chunk in model_patch.split("diff --git "):
        if not chunk.strip():
            continue
        section = "diff --git " + chunk
        header = section.splitlines()[0]
        sec_paths = {tok[2:] if tok.startswith(("a/", "b/")) else tok for tok in header.split()[2:]}
        if sec_paths & gold:
            continue
        kept.append(section)
    out = "".join(kept)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def grade(
    instances: Iterable[Any],
    patches: dict[str, str],
    *,
    work_dir: str | Path,
    dataset_name: str | None = None,
    split: str = DEFAULT_SPLIT,
    max_workers: int = 4,
    timeout: int = 1800,
    namespace: str = "swebench",
) -> dict[str, bool]:
    """Grade ``patches`` (instance_id -> diff) for ``instances``.

    Returns ``{instance_id: resolved}``. Instances absent from the harness
    report default to ``False`` (unresolved).
    """
    insts = list(instances)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    run_id = work.name  # filesystem/docker-safe; identifies the run + report file
    name = dataset_name or DEFAULT_DATASET
    ids = [i.instance_id for i in insts]

    preds = [
        {
            "instance_id": i.instance_id,
            "model_name_or_path": MODEL_NAME,
            "model_patch": _strip_gold_test_files(patches.get(i.instance_id, ""), getattr(i, "test_patch", "")),
        }
        for i in insts
    ]
    preds_file = work / "predictions.jsonl"
    _write_jsonl(preds_file, preds)

    # Force a fresh evaluation of every requested instance. swebench's
    # run_evaluation skips an instance when a prior report.json for the same
    # run_id already exists, so a re-grade (e.g. --resume after re-running a
    # task with a new patch) would silently inherit the STALE verdict from the
    # previous patch. Drop the per-instance logs and the aggregate report first.
    eval_logs = work / "logs" / "run_evaluation" / run_id / MODEL_NAME
    for iid in ids:
        shutil.rmtree(eval_logs / iid, ignore_errors=True)
    (work / f"{MODEL_NAME.replace('/', '__')}.{run_id}.json").unlink(missing_ok=True)

    # run_evaluation writes its final report as ``<model>.<run_id>.json`` relative
    # to the process cwd, so run from the work dir and read it back there.
    outer_timeout = timeout * max(1, -(-len(ids) // max(1, max_workers))) + 1800
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT / "benchmarks"),
            "python",
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            name,
            "--split",
            split,
            "--predictions_path",
            str(preds_file),
            "--run_id",
            run_id,
            "--namespace",
            namespace,
            "--cache_level",
            "env",
            "--max_workers",
            str(max_workers),
            "--timeout",
            str(timeout),
            "--report_dir",
            str(work),
            "--instance_ids",
            *ids,
        ],
        cwd=str(work),
        capture_output=True,
        text=True,
        timeout=outer_timeout,
        check=False,
    )

    report_file = work / f"{MODEL_NAME.replace('/', '__')}.{run_id}.json"
    if not report_file.exists():
        raise RuntimeError(
            f"swebench produced no report ({report_file.name}; exit {proc.returncode}).\n"
            f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
        )
    report = json.loads(report_file.read_text(encoding="utf-8"))
    resolved = set(report.get("resolved_ids", []))
    return {i.instance_id: (i.instance_id in resolved) for i in insts}
