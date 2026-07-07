"""Grade SWE-bench Pro candidate patches with ScaleAI's own harness.

SWE-bench Pro is graded by ``scaleapi/SWE-bench_Pro-os`` -- a *different*
harness from ``swebench`` (:mod:`swebench_grade`), inspired by but not the
same project as princeton-nlp/SWE-bench. It expects a CSV of the raw dataset
rows and a ``[{instance_id, patch, prefix}]`` JSON of patches, and writes a
flat ``{instance_id: bool}`` to ``<output_dir>/eval_results.json`` (verified
directly against the harness source: ``swe_bench_pro_eval.py``'s ``main()``
writes exactly this shape -- no ``resolved_ids`` wrapper like ``swebench``).

The harness repo ships per-instance ``run_scripts/`` and ``dockerfiles/`` that
``swe_bench_pro_eval.py`` reads via *relative* paths, so it must run with its
repo root as cwd; :func:`_ensure_harness_repo` shallow-clones it into a local
cache on first use.
"""

from __future__ import annotations

import csv
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from benchmarks.codebench.run import REPO_ROOT
from benchmarks.codebench.swebench_pro_data import DOCKERHUB_USERNAME

# Fixed prediction author label -> deterministic per-instance output filename
# (the harness writes ``<prefix>_output.json`` per instance).
MODEL_NAME = "atelier-codebench"

HARNESS_REPO_URL = "https://github.com/scaleapi/SWE-bench_Pro-os.git"
HARNESS_CACHE_DIR = REPO_ROOT / ".cache" / "swe-bench-pro-os"

# All 16 HF dataset columns, in dataset order -- the harness's --raw_sample_path
# CSV must match this schema (create_entryscript/eval_with_docker read several
# of these columns by name off the pandas row).
_CSV_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "patch",
    "test_patch",
    "problem_statement",
    "requirements",
    "interface",
    "repo_language",
    "fail_to_pass",
    "pass_to_pass",
    "issue_specificity",
    "issue_categories",
    "before_repo_set_cmd",
    "selected_test_files_to_run",
    "dockerhub_tag",
)


def _ensure_harness_repo(cache_dir: Path = HARNESS_CACHE_DIR) -> Path:
    """Shallow-clone scaleapi/SWE-bench_Pro-os into the local cache if absent.

    The harness ships its own ``run_scripts/``/``dockerfiles/`` per instance
    (needed to build each instance's entryscript), so a full ``--depth 1``
    clone -- not just the eval script -- is required. ``.cache/`` is already
    gitignored (generic ``.cache`` rule), so no vendoring lands in git.
    """
    if (cache_dir / "swe_bench_pro_eval.py").exists():
        return cache_dir
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    print(f"[swe-pro] cloning {HARNESS_REPO_URL} -> {cache_dir}", flush=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", HARNESS_REPO_URL, str(cache_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"failed to clone {HARNESS_REPO_URL} into {cache_dir}: {exc.stderr}") from exc
    return cache_dir


def _csv_row(inst: Any) -> dict[str, str]:
    return {
        "instance_id": inst.instance_id,
        "repo": inst.repo,
        "base_commit": inst.base_commit,
        "patch": inst.patch,
        "test_patch": inst.test_patch,
        "problem_statement": inst.problem_statement,
        "requirements": inst.requirements or "",
        "interface": inst.interface or "",
        "repo_language": inst.repo_language,
        # Re-serialize the parsed list fields as JSON -- valid input for the
        # harness's own ``eval(sample[...])`` calls, and always double-quoted
        # (unlike some source rows, which mix single- and double-quoted reprs).
        "fail_to_pass": json.dumps(inst.fail_to_pass),
        "pass_to_pass": json.dumps(inst.pass_to_pass),
        "issue_specificity": json.dumps(inst.issue_specificity),
        "issue_categories": json.dumps(inst.issue_categories),
        "before_repo_set_cmd": inst.before_repo_set_cmd,
        "selected_test_files_to_run": json.dumps(inst.selected_test_files_to_run),
        "dockerhub_tag": inst.dockerhub_tag,
    }


def _write_csv(path: Path, instances: list[Any]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for inst in instances:
            writer.writerow(_csv_row(inst))


def _write_patches_json(path: Path, instances: list[Any], patches: dict[str, str], prefix: str) -> None:
    rows = [
        {"instance_id": i.instance_id, "patch": patches.get(i.instance_id, ""), "prefix": prefix} for i in instances
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")


def grade(
    instances: Iterable[Any],
    patches: dict[str, str],
    *,
    work_dir: str | Path,
    max_workers: int = 4,
    timeout: int = 1800,
    dockerhub_username: str = DOCKERHUB_USERNAME,
    use_local_docker: bool = True,
) -> dict[str, bool]:
    """Grade ``patches`` (instance_id -> diff) for ``instances`` with the SWE-bench Pro harness.

    Returns ``{instance_id: resolved}``. Instances absent from the harness's
    ``eval_results.json`` default to ``False`` (unresolved) -- same shape as
    :func:`benchmarks.codebench.swebench_grade.grade`.
    """
    insts = list(instances)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    repo_dir = _ensure_harness_repo()

    csv_path = work / "raw_samples.csv"
    patches_path = work / "patches.json"
    _write_csv(csv_path, insts)
    _write_patches_json(patches_path, insts, patches, MODEL_NAME)

    cmd = [
        "uv",
        "run",
        "--project",
        str(REPO_ROOT / "benchmarks"),
        "python",
        str(repo_dir / "swe_bench_pro_eval.py"),
        "--raw_sample_path",
        str(csv_path),
        "--patch_path",
        str(patches_path),
        "--output_dir",
        str(work),
        "--scripts_dir",
        "run_scripts",
        "--num_workers",
        str(max_workers),
        "--dockerhub_username",
        dockerhub_username,
        # Force a fresh evaluation every call: the harness's prepare_run() skips
        # an instance whose <prefix>_output.json already exists in output_dir,
        # which would silently inherit a STALE verdict from a prior patch if a
        # work_dir is ever reused (mirrors the staleness guard in swebench_grade).
        "--redo",
    ]
    if use_local_docker:
        cmd.append("--use_local_docker")

    outer_timeout = timeout * max(1, -(-len(insts) // max(1, max_workers))) + 1800
    proc = subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=outer_timeout,
        check=False,
    )

    report_file = work / "eval_results.json"
    if not report_file.exists():
        raise RuntimeError(
            f"swe-bench-pro harness produced no eval_results.json (exit {proc.returncode}).\n"
            f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
        )
    try:
        report = json.loads(report_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"swe-bench-pro harness wrote an unparseable eval_results.json ({exc}); refusing to guess results.\n"
            f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
        ) from exc
    if not isinstance(report, dict):
        raise RuntimeError(
            f"swe-bench-pro harness's eval_results.json was not a JSON object (got {type(report).__name__}); "
            "refusing to guess results."
        )
    return {i.instance_id: bool(report.get(i.instance_id, False)) for i in insts}
