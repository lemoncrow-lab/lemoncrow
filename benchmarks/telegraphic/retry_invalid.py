"""Rerun only the content-invalid rows from a finished `lc benchmark telegraphic` run.

``lc benchmark telegraphic`` has no ``--resume``, and codebench's own
``--retry-failed`` only reruns rows where ``ok`` is false (transport/exec
failure) -- not rows that ran fine but produced an off-topic/placeholder/empty
answer (``valid: false``, see ``benchmarks.codebench.run._is_content_invalid``).
This script finds that second bucket and reruns exactly those (task, arm, rep)
triples, leaving every other row untouched, then re-merges results.jsonl and
regenerates telegraphic_report.md.

How it reuses the existing machinery instead of reimplementing it:

- codebench arms (baseline/lemoncrow): each batch dir already has its own
  ``results.jsonl`` keyed by ``(task, arm, rep)`` (see run.py's ``_result_key``
  / ``completed`` set). Stripping just the invalid rows out of a batch's file
  and re-invoking ``codebench.run --resume`` with the SAME batch prompts (so
  local task numbering matches) makes it skip every row still on disk and
  execute only the ones we removed -- no custom retry logic needed.
- the caveman extra arm has no per-batch results file (it is a plain Python
  loop in benchmark_telegraphic_cmd, not a codebench.run subprocess), so its
  invalid rows are rerun directly via ``extra_arms.run_extra_arm``.

Usage::

    uv run --project benchmarks python -m benchmarks.telegraphic.retry_invalid \
        --run-dir /tmp/lemoncrow-telegraphic-scratch-repo/reports/benchmark/telegraphic/<ts> \
        -y

    # Preview what would be rerun without spending anything:
    uv run --project benchmarks python -m benchmarks.telegraphic.retry_invalid \
        --run-dir <run_dir> --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
REPO_ROOT = _HERE.parents[1]
# Must match benchmark_telegraphic_cmd's own batch size (src/lemoncrow/gateway/
# cli/commands/benchmark.py) -- codebench's ad-hoc --prompt mode hard-caps at
# 10 values per invocation, so that command batches into chunks of 10 and this
# script has to reproduce the exact same batching to find the right batch dir
# and the right local task number for any given absolute prompt index.
_BATCH = 10
_TASK_RE = re.compile(r"^local(\d+)$")


def _python_cmd() -> list[str]:
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(REPO_ROOT), "python"]
    return [sys.executable]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _prompt_index(task: str) -> int | None:
    m = _TASK_RE.match(str(task))
    return int(m.group(1)) - 1 if m else None


def find_invalid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content-invalid rows: ran fine (not a transport/exec failure) but the
    answer is off-topic/placeholder/empty. Distinct from the ``ok: false``
    bucket ``--retry-failed`` covers -- see ``run.py::_is_content_invalid``.
    """
    return [r for r in rows if r.get("valid") is False]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True, type=Path, help="Finished `lc benchmark telegraphic` run dir")
    p.add_argument("--repo", default=None, type=Path, help="Repo the original run used (default: scratch repo)")
    p.add_argument("--model", default="claude-opus-4-8", help="default: claude-opus-4-8")
    p.add_argument("--max-turns", dest="max_turns", type=int, default=50, help="default: 50")
    p.add_argument("--cli-driver", dest="cli_driver", default="claude", help="default: claude")
    p.add_argument("--jobs", type=int, default=1, help="default: 1")
    p.add_argument(
        "--reps",
        type=int,
        default=None,
        help="Rep count the original run used (default: inferred from results.jsonl)",
    )
    p.add_argument("--capture", action=argparse.BooleanOptionalAction, default=True, help="default: on")
    p.add_argument("--dry-run", action="store_true", help="Print what would be rerun; spend nothing")
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    args = p.parse_args()

    run_dir: Path = args.run_dir.resolve()
    results_path = run_dir / "results.jsonl"
    original_rows = _load_jsonl(results_path)
    if not original_rows:
        p.error(f"no results.jsonl in {run_dir} (run must have finished)")

    invalid = find_invalid(original_rows)
    if not invalid:
        print(f"No content-invalid rows in {results_path} -- nothing to do.")
        return 0

    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
    from benchmarks.telegraphic import ensure_scratch_repo, load_prompts
    from benchmarks.telegraphic.extra_arms import EXTRA_ARMS, run_extra_arm

    repo_abs = (args.repo.expanduser().resolve()) if args.repo is not None else ensure_scratch_repo()
    n_prompts = max((_prompt_index(r["task"]) or 0) for r in original_rows) + 1
    prompt_entries = load_prompts(limit=n_prompts)
    reps = args.reps or (max(int(r["rep"]) for r in original_rows) + 1)

    codebench_invalid = [r for r in invalid if r["arm"] not in EXTRA_ARMS]
    extra_invalid = [r for r in invalid if r["arm"] in EXTRA_ARMS]

    print(
        f"Invalid rows: {len(invalid)} total ({len(codebench_invalid)} codebench-arm, {len(extra_invalid)} extra-arm)"
    )
    for r in invalid:
        print(f"  {r['task']:>8} {r['arm']:<10} rep{r['rep']}  {r.get('validity_reason', '')}")

    if args.dry_run:
        print("\n--dry-run: nothing executed.")
        return 0
    if not args.yes:
        reply = input(f"\nRerun {len(invalid)} row(s) and spend real tokens? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted; no tokens spent.")
            return 1

    # ---- codebench arms (baseline/lemoncrow): patch each affected batch's own
    # results.jsonl, then let `codebench.run --resume` fill the gap. ----------
    by_batch: dict[int, list[dict[str, Any]]] = {}
    for r in codebench_invalid:
        idx = _prompt_index(r["task"])
        assert idx is not None
        by_batch.setdefault(idx // _BATCH, []).append(r)

    for batch_idx, bad_rows in sorted(by_batch.items()):
        batch_dir = run_dir / f"batch{batch_idx}"
        batch_results = batch_dir / "results.jsonl"
        # Absolute task index -> this batch's own local1..local10 numbering
        # (benchmark_telegraphic_cmd remaps local{n} -> local{offset+n} on
        # merge; codebench.run inside the batch only ever knows the local one).
        bad_keys = {(f"local{(_prompt_index(r['task']) % _BATCH) + 1}", r["arm"], int(r["rep"])) for r in bad_rows}
        kept = [row for row in _load_jsonl(batch_results) if (row["task"], row["arm"], int(row["rep"])) not in bad_keys]
        _write_jsonl(batch_results, kept)

        batch_prompts = [e["prompt"] for e in prompt_entries[batch_idx * _BATCH : (batch_idx + 1) * _BATCH]]
        arms_needed = sorted({r["arm"] for r in bad_rows})
        cmd = [
            *_python_cmd(),
            "-m",
            "benchmarks.codebench.run",
            "--repo",
            str(repo_abs),
            "--arm",
            *arms_needed,
            "--reps",
            str(reps),
            "--model",
            args.model,
            "--max-turns",
            str(args.max_turns),
            "--cli-driver",
            args.cli_driver,
            "--jobs",
            str(args.jobs),
            "--out",
            str(batch_dir),
            "--resume",
            "--capture" if args.capture else "--no-capture",
        ]
        for prompt in batch_prompts:
            cmd.extend(["--prompt", prompt])
        print(f"\n[batch{batch_idx}] rerunning {len(bad_keys)} row(s): {sorted(bad_keys)}")
        # cwd MUST be the repo root, not benchmarks/ itself -- `python -m
        # benchmarks.codebench.run` resolves the `benchmarks` package relative
        # to cwd (mirrors benchmark_telegraphic_cmd's own _run(cmd, cwd=bench_root, ...)
        # where bench_root = _bench_source_root() = the repo root).
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        if result.returncode != 0:
            print(f"[batch{batch_idx}] codebench.run exited {result.returncode} -- rows may still be missing")

        if args.capture:
            import contextlib
            import io

            from benchmarks.flowlib.dump import extract

            for fp in sorted(batch_dir.glob("*.flow")):
                if fp.stat().st_size == 0:
                    continue
                with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
                    extract(str(fp), str(fp.with_suffix(".flow_dump.txt")))

    # ---- extra arms (caveman): no batch results.jsonl -- call run_extra_arm
    # directly for each invalid (task, rep). ----------------------------------
    extra_new_rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    if extra_invalid:
        from benchmarks.codebench.run import _make_baseline_config

        for r in extra_invalid:
            idx = _prompt_index(r["task"])
            assert idx is not None
            rep = int(r["rep"])
            batch_dir = run_dir / f"batch{idx // _BATCH}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            local_n = idx % _BATCH + 1
            flow_path = batch_dir / f"local{local_n}_{r['arm']}_rep{rep}.flow" if args.capture else None
            print(f"\n[extra] rerunning {r['task']} {r['arm']} rep{rep}")
            new_row = run_extra_arm(
                arm=r["arm"],
                task_id=r["task"],
                prompt=prompt_entries[idx]["prompt"],
                model=args.model,
                rep=rep,
                make_baseline_config=_make_baseline_config,
                flow_path=flow_path,
            )
            extra_new_rows[(r["task"], r["arm"], rep)] = new_row

    # ---- re-merge: codebench rows come fresh from the (now-patched) batch
    # dirs; extra-arm rows come from the original merged file with invalid
    # ones swapped for their reruns. ------------------------------------------
    from benchmarks.telegraphic.report import load_results, render_report

    merged: list[dict[str, Any]] = []
    n_batches = (n_prompts + _BATCH - 1) // _BATCH
    for batch_idx in range(n_batches):
        batch_dir = run_dir / f"batch{batch_idx}"
        offset = batch_idx * _BATCH
        for row in load_results(batch_dir):
            m = _TASK_RE.match(str(row.get("task", "")))
            if m:
                row = {**row, "task": f"local{offset + int(m.group(1))}"}
            merged.append(row)

    bad_extra_keys = {(r["task"], r["arm"], int(r["rep"])) for r in extra_invalid}
    for row in original_rows:
        if row["arm"] not in EXTRA_ARMS:
            continue  # already rebuilt from batch dirs above
        key = (row["task"], row["arm"], int(row["rep"]))
        if key in bad_extra_keys:
            continue  # replaced below
        merged.append(row)
    merged.extend(extra_new_rows.values())

    _write_jsonl(results_path, merged)
    table_md = render_report(merged, prompt_entries)
    (run_dir / "telegraphic_report.md").write_text(table_md, encoding="utf-8")
    print("\n" + table_md)

    still_invalid = find_invalid(merged)
    print(f"\nResults: {run_dir}")
    print(f"Remaining invalid rows: {len(still_invalid)}")
    for r in still_invalid:
        print(f"  {r['task']:>8} {r['arm']:<10} rep{r['rep']}  {r.get('validity_reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
