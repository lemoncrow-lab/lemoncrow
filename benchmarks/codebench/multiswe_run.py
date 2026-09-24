"""One-command Multi-SWE-bench A/B: vanilla Claude Code vs LemonCrow, in-container.

Pipeline (single command):
  load+filter instances -> build per-arm overlays -> run each (instance, arm,
  rep) inside its Docker image -> extract the git diff -> grade every diff with
  the official multi_swe_bench harness -> reuse run.py savings/report/CSV.

The arms differ only in overlay contents + claude flags (baseline = vanilla
Claude Code; lemoncrow = + LemonCrow plugin/MCP), same model, same instance -- the
clean isolation that attributes any cost/quality delta to LemonCrow alone.

Example:
  uv run --project benchmarks python -m benchmarks.codebench.multiswe_run \
      --languages go rust --per-language-limit 5 --reps 1 --model sonnet --jobs 2
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import queue
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.codebench import (
    grade,
    incontainer,
    multiswe,
    swebench_data,
    swebench_grade,
    swebench_pro_data,
    swebench_pro_grade,
)
from benchmarks.codebench.run import (
    BY_ID,
    RESULTS_ROOT,
    ArmResult,
    _apply_savings,
    _load_benchmark_env,
    _write_results_jsonl,
    build_pairwise_quality_rows,
    report,
    write_csv_artifacts,
)
from benchmarks.codebench.tasks import Task

FLASH_URL = (
    "https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench-flash/resolve/main/multi_swe_bench_flash.jsonl"
)
DATA_DIR = Path(__file__).parent / "data"
ARMS = ("baseline", "lemoncrow")


def ensure_flash() -> Path:
    """Download the flash dataset (7 non-Python languages) to the cache if absent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "multi_swe_bench_flash.jsonl"
    if not path.exists() or path.stat().st_size < 1_000_000:
        print(f"[dataset] downloading flash -> {path}", flush=True)
        urllib.request.urlretrieve(FLASH_URL, path)
    return path


def _register_stub_task(inst: Any) -> None:
    """Register a lightweight Task so run.py's reporting BY_ID lookups resolve."""
    if inst.instance_id not in BY_ID:
        BY_ID[inst.instance_id] = Task(
            inst.instance_id, inst.language, ("empty",), 1, inst.instance_id, capability="code"
        )


def _patch_path(out_dir: Path, inst: Any, arm: str, rep: int) -> Path:
    return out_dir / f"{inst.instance_id}_{arm}_rep{rep}.patch"


def _prebuild_overlays(instances: list[Any], arms: list[str], driver: str = "claude") -> None:
    """Build every needed overlay serially up front so parallel runs don't race.

    One instance's broken/unusual base image (e.g. missing apt-get -- a non-
    Debian image the overlay install script can't handle) must not abort every
    OTHER instance's run: log and continue. The per-job ThreadPoolExecutor in
    run() already catches a lazy ensure_overlay() failure at container-run time
    and records it as an errored row, so a skipped image here still surfaces as
    a normal (not a crash) unresolved result for just that instance/arm.
    """
    seen: set[tuple[str, bool]] = set()
    for inst in instances:
        for arm in arms:
            key = (inst.image, arm == "lemoncrow")
            if key in seen:
                continue
            seen.add(key)
            print(f"[overlay] ensuring {arm} overlay for {inst.image}", flush=True)
            try:
                incontainer.ensure_overlay(inst.image, lc=(arm == "lemoncrow"), driver=driver)
            except Exception as exc:
                print(f"[overlay] FAILED for {arm}/{inst.image}: {exc} -- skipping, will error at run time", flush=True)


def _grade_arms(
    instances: list[Any],
    results: list[ArmResult],
    *,
    out_dir: Path,
    reps: int,
    arms: list[str],
    grade_workers: int,
    grade_fn: Callable[[list[Any], dict[str, str], Path, int], dict[str, bool]],
    label: str,
) -> None:
    by_id = {inst.instance_id: inst for inst in instances}
    for arm in arms:
        for rep in range(1, reps + 1):
            group = [r for r in results if r.arm == arm and r.rep == rep and r.correct is None]
            if not group:
                continue
            insts = [by_id[r.task] for r in group if r.task in by_id]
            patches = {
                inst.instance_id: _patch_path(out_dir, inst, arm, rep).read_text(encoding="utf-8")
                for inst in insts
                if _patch_path(out_dir, inst, arm, rep).exists()
            }
            print(f"[grade] {arm} rep{rep}: {len(patches)} patch(es)", flush=True)
            resolved = grade_fn(insts, patches, out_dir / f"grade_{arm}_rep{rep}", grade_workers)
            for r in group:
                ok = bool(resolved.get(r.task, False))
                r.correct = ok
                r.score = 1.0 if ok else 0.0
                r.judge_model = label
                r.judge_reason = "resolved" if ok else "unresolved"


GradeFn = Callable[[list[Any], dict[str, str], Path, int], dict[str, bool]]


def _grade_one(
    inst: Any,
    arm: str,
    rep: int,
    res: ArmResult,
    *,
    grade_fn: GradeFn,
    out_dir: Path,
    label: str,
) -> None:
    """Grade one freshly-run rep in place, the moment it finishes.

    Sets ``correct``/``score``/``judge_*`` on *res* so the incrementally written
    results.jsonl carries live correctness -- no waiting for every rep to finish.
    An errored or empty-patch rep grades unresolved without invoking the Docker
    harness. A grader exception leaves ``correct`` as ``None`` so the end-of-run
    safety net re-grades it.
    """
    patch_file = _patch_path(out_dir, inst, arm, rep)
    patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""
    if res.is_error or not patch.strip():
        res.correct = False
        res.score = 0.0
        res.judge_model = label
        res.judge_reason = "unresolved"
        return
    # Per-rep work dir -> unique docker run_id (grade() derives it from dir name),
    # so concurrent inline grades never collide.
    work = out_dir / "grade" / f"{inst.instance_id}_{arm}_rep{rep}"
    try:
        out = grade_fn([inst], {inst.instance_id: patch}, work, 1)
    except Exception as exc:  # leave correct=None -> end-of-run pass retries
        res.judge_reason = f"grade-error: {exc}"[:200]
        return
    resolved = bool(out.get(inst.instance_id, False))
    res.correct = resolved
    res.score = 1.0 if resolved else 0.0
    res.judge_model = label
    res.judge_reason = "resolved" if resolved else "unresolved"


def _select_backend(args: argparse.Namespace) -> tuple[list[Any], GradeFn, str]:
    """Resolve (instances, grade_fn, judge-label) for the selected ``--suite``.

    swe-bench-verified / swe-lite -> SWE-bench (Python), graded by the
    ``swebench`` harness; swe-pro -> SWE-bench Pro (ScaleAI), a structurally
    different dataset/harness with its own loader+grader (not folded into
    ``SUITE_DEFAULTS``); multi-swe-bench -> the 7 non-Python languages, graded
    by ``multi_swe_bench``.
    """
    if args.suite == "swe-pro":
        dataset = args.dataset or swebench_pro_data.DEFAULT_DATASET
        # Explicit --instances always wins and is never sliced by --limit;
        # otherwise the loader itself fills in the pinned default slice (sliced
        # by --limit), mirroring the swe-lite/verified convention.
        wanted = list(args.instances) if args.instances else None
        instances = swebench_pro_data.load_instances(dataset=dataset, instances=wanted, limit=args.limit)

        def grade_swe_pro(insts: list[Any], patches: dict[str, str], work_dir: Path, workers: int) -> dict[str, bool]:
            return swebench_pro_grade.grade(
                insts, patches, work_dir=work_dir, max_workers=workers, timeout=args.timeout
            )

        return list(instances), grade_swe_pro, "swebench-pro"

    if args.suite in swebench_data.SUITE_DEFAULTS:
        default_dataset, default_instances = swebench_data.SUITE_DEFAULTS[args.suite]
        dataset = args.dataset or default_dataset
        # Explicit --instances always wins; otherwise fall back to the suite's
        # pinned default (sliced by --limit, since load_instances() never applies
        # --limit once an explicit/default instance list is in play).
        if args.instances:
            wanted: list[str] | None = list(args.instances)
        elif default_instances:
            wanted = list(default_instances[: args.limit])
        else:
            wanted = None
        instances = swebench_data.load_instances(
            dataset=dataset,
            instances=wanted,
            min_changed_files=args.min_changed_files,
            limit=args.limit,
        )

        def grade_swe(insts: list[Any], patches: dict[str, str], work_dir: Path, workers: int) -> dict[str, bool]:
            return swebench_grade.grade(
                insts, patches, dataset_name=dataset, work_dir=work_dir, max_workers=workers, timeout=args.timeout
            )

        return list(instances), grade_swe, "swebench"

    dataset_path = Path(args.dataset) if args.dataset else ensure_flash()
    # Explicit --instances must never be silently dropped by the corpus filters
    # (min-changed-files / per-language / limit). Those filters shape the *random*
    # sample; an explicitly named instance is a deliberate request, so bypass them
    # and surface any id missing from the dataset instead of dropping it quietly.
    explicit = set(args.instances) if args.instances else None
    multi = multiswe.load_instances(
        dataset_path,
        languages=args.languages,
        min_changed_files=0 if explicit else args.min_changed_files,
        per_language_limit=None if explicit else args.per_language_limit,
        limit=None if explicit else args.limit,
    )
    if explicit:
        multi = [i for i in multi if i.instance_id in explicit]
        missing = explicit - {i.instance_id for i in multi}
        if missing:
            print(f"[warn] requested --instances not found in dataset: {sorted(missing)}", flush=True)

    def grade_multi(insts: list[Any], patches: dict[str, str], work_dir: Path, workers: int) -> dict[str, bool]:
        return grade.grade(insts, patches, dataset_path=dataset_path, work_dir=work_dir, max_workers=workers)

    return list(multi), grade_multi, "multiswe"


def _load_prior_results(out_dir: Path) -> dict[tuple[str, str, int], ArmResult]:
    """Index an existing results.jsonl by (task, arm, rep) for --resume reuse.

    Each row is ``asdict(ArmResult)`` so it round-trips back via ``ArmResult(**row)``
    (extra/unknown keys are dropped defensively against schema drift).
    """
    path = out_dir / "results.jsonl"
    if not path.exists():
        return {}
    names = {f.name for f in dataclasses.fields(ArmResult)}
    prior: dict[tuple[str, str, int], ArmResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        res = ArmResult(**{k: v for k, v in row.items() if k in names})
        prior[(res.task, res.arm, res.rep)] = res
    return prior


def _effective_parallelism(*, jobs: int, jobs_per_token: int, token_count: int) -> int:
    """Return the actual container concurrency, always honoring ``--jobs``.

    OAuth rotation adds credential capacity; it must not silently override the
    caller's global concurrency request.  In particular, ``--jobs 2`` with one
    token and the default ``--jobs-per-token 4`` means two containers, not four.
    """
    requested = max(1, jobs)
    if token_count <= 0:
        return requested
    token_capacity = max(1, jobs_per_token) * token_count
    return min(requested, token_capacity)


def _resolve_oauth_tokens(agent_env: dict[str, str]) -> list[str]:
    """OAuth tokens to rotate container runs across, in priority order.

    Reads CLAUDE_CODE_OAUTH_TOKEN_1 / _2 from the host env first, then the .env
    cascade (agent_env). Whichever are set are used; both set -> both used. Falls
    back to the single CLAUDE_CODE_OAUTH_TOKEN when neither numbered token is
    present, preserving prior single-credential behavior.
    """
    tokens: list[str] = []
    for name in ("CLAUDE_CODE_OAUTH_TOKEN_1", "CLAUDE_CODE_OAUTH_TOKEN_2"):
        val = os.environ.get(name) or agent_env.get(name)
        if val and val not in tokens:
            tokens.append(val)
    if not tokens:
        val = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or agent_env.get("CLAUDE_CODE_OAUTH_TOKEN")
        if val:
            tokens.append(val)
    return tokens


_RUNTIME_FINGERPRINT_PATHS = (
    "src/lemoncrow",
    "client/src/lemoncrow_client",
    "server/src/lemoncrow_server_core",
    "integrations/claude/plugin",
    "benchmarks/codebench/incontainer.py",
    "benchmarks/codebench/incontainer_entry.sh",
)


def _runtime_source_fingerprint() -> str:
    """Hash every host file that can change the live Claude/LemonCrow runtime.

    SWE containers bind-mount these paths, so changing them while a run is in
    progress can otherwise create a mixed-version result without changing the
    command line. The publication manifest records the hash at both ends.
    """
    digest = hashlib.sha256()
    root = incontainer.REPO_ROOT
    for relative in _RUNTIME_FINGERPRINT_PATHS:
        target = root / relative
        files = [target] if target.is_file() else sorted(path for path in target.rglob("*") if path.is_file())
        for path in files:
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            name = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(name)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _git_head() -> str:
    proc = subprocess.run(
        ["git", "-C", str(incontainer.REPO_ROOT), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _write_benchmark_manifest(
    out_dir: Path,
    *,
    args: argparse.Namespace,
    instances: list[Any],
    grade_label: str,
    start_fingerprint: str,
    end_fingerprint: str | None = None,
) -> dict[str, Any]:
    path = out_dir / "benchmark-manifest.json"
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    manifest: dict[str, Any] = {
        **previous,
        "schema_version": 1,
        "suite": args.suite,
        "dataset": args.dataset,
        "instances": [inst.instance_id for inst in instances],
        "arms": list(args.arms),
        "reps": args.reps,
        "driver": args.driver,
        "model": args.model,
        "max_turns": args.max_turns,
        "timeout_s": args.timeout,
        "jobs": args.jobs,
        "jobs_per_token": args.jobs_per_token,
        "grader": grade_label,
        "command_argv": list(sys.argv),
        "claude_code_version": incontainer.CLAUDE_CODE_VERSION if args.driver == "claude" else None,
        "lemoncrow_overlay_revision": incontainer.LEMONCROW_OVERLAY_REVISION,
        "runtime_fingerprint_paths": list(_RUNTIME_FINGERPRINT_PATHS),
        "lemoncrow_git_commit_start": previous.get("lemoncrow_git_commit_start", _git_head()),
        "runtime_source_fingerprint_start": previous.get("runtime_source_fingerprint_start", start_fingerprint),
        "started_at_utc": previous.get("started_at_utc", datetime.now(UTC).isoformat()),
    }
    if end_fingerprint is not None:
        manifest.update(
            {
                "lemoncrow_git_commit_end": _git_head(),
                "runtime_source_fingerprint_end": end_fingerprint,
                "runtime_source_changed_during_run": end_fingerprint != manifest["runtime_source_fingerprint_start"],
                "finished_at_utc": datetime.now(UTC).isoformat(),
            }
        )
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def run(args: argparse.Namespace) -> int:
    instances, grade_fn, grade_label = _select_backend(args)
    if not instances:
        print("no instances matched the filters", flush=True)
        return 1
    for inst in instances:
        _register_stub_task(inst)

    # Resolve to absolute: out_dir feeds docker -v bind mounts (prompt.txt, flow)
    # and the grader's predictions path; a relative path makes docker reject the
    # mount ("invalid characters for a local volume name") and grading FileNotFound.
    out_dir = (Path(args.out) if args.out else RESULTS_ROOT / f"multiswe-{time.strftime('%Y%m%d-%H%M%S')}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run] {len(instances)} instance(s) x {len(args.arms)} arm(s) x {args.reps} rep(s)", flush=True)
    print(f"[run] results -> {out_dir}", flush=True)
    source_fingerprint = _runtime_source_fingerprint()
    manifest = _write_benchmark_manifest(
        out_dir,
        args=args,
        instances=instances,
        grade_label=grade_label,
        start_fingerprint=source_fingerprint,
    )
    if args.driver == "claude":
        print(
            f"[runtime] claude-code={incontainer.CLAUDE_CODE_VERSION} "
            f"lemoncrow={manifest['lemoncrow_git_commit_start'][:12]} "
            f"source={source_fingerprint[:12]}",
            flush=True,
        )

    _prebuild_overlays(instances, args.arms, args.driver)

    agent_env = _load_benchmark_env()
    # Rotate container runs across the available OAuth tokens, capping each at
    # --jobs-per-token concurrent runs via a slot queue. With both tokens set and
    # the default cap of 4, total parallelism is 8 (4 per token).
    # OAuth-token rotation is claude-only; cursor authenticates from auth.json and
    # parallelizes via --jobs.
    # OAuth-token rotation is claude-only: cursor authenticates from auth.json,
    # and the lemoncode driver runs on Zen's keyless public tier.
    tokens = [] if args.driver in ("cursor", "lemoncode") else _resolve_oauth_tokens(agent_env)
    per_token = max(1, args.jobs_per_token)
    token_slots: queue.Queue[str] | None = None
    effective_jobs = _effective_parallelism(
        jobs=args.jobs,
        jobs_per_token=per_token,
        token_count=len(tokens),
    )
    if tokens:
        token_slots = queue.Queue()
        for tok in tokens:
            for _ in range(per_token):
                token_slots.put(tok)
        token_capacity = per_token * len(tokens)
        print(
            f"[auth] {len(tokens)} OAuth token(s) x {per_token} job(s)/token = {token_capacity} token slot(s); "
            f"--jobs {args.jobs} -> up to {effective_jobs} parallel",
            flush=True,
        )
    else:
        print(f"[auth] no CLAUDE_CODE_OAUTH_TOKEN_1/_2 set; ambient creds, jobs={effective_jobs}", flush=True)
    jobs = [(inst, arm, rep) for inst in instances for arm in args.arms for rep in range(1, args.reps + 1)]
    # --resume: reuse a prior (task, arm, rep) result when its patch artifact is
    # still present, so a re-run re-executes only the missing/stripped jobs (e.g.
    # keep valid baseline runs, re-run lemoncrow after a fix) without re-paying.
    prior = _load_prior_results(out_dir) if getattr(args, "resume", False) else {}
    results: list[ArmResult] = []
    reused_rows: list[ArmResult] = []
    pending: list[tuple[Any, str, int]] = []
    for job in jobs:
        inst, arm, rep = job
        cached = prior.get((inst.instance_id, arm, rep))
        # Reuse only a valid completed rep. An errored / not-ok prior row (e.g. an
        # empty-index FATAL abort, timeout, or 403) leaves a 0-byte patch on disk;
        # reusing it would defeat the runner's "--resume retries" recovery, so we
        # re-run those instead of carrying the broken $0 row forward.
        reusable = (
            cached is not None and not cached.is_error and cached.ok and _patch_path(out_dir, inst, arm, rep).exists()
        )
        if reusable:
            results.append(cached)
            reused_rows.append(cached)
            print(
                f"  -> {inst.instance_id}/{arm} rep{rep}: reused (resume) ok={cached.ok} "
                f"correct={cached.correct} cost=${cached.cost_usd:.4f} turns={cached.num_turns} "
                f"in={cached.input_tokens} out={cached.output_tokens} "
                f"cacheW={cached.cache_creation_tokens // 1000}k cacheR={cached.cache_read_tokens // 1000}k",
                flush=True,
            )
        else:
            pending.append(job)
    if prior:
        # Carry forward prior rows outside this run's scope so a narrower resume
        # (e.g. -a lemoncrow only) never drops the rows it isn't re-running.
        covered = {(i.instance_id, a, r) for (i, a, r) in jobs}
        preserved = [res for key, res in prior.items() if key not in covered]
        results.extend(preserved)
        # Summarize what we're keeping vs re-running with the same cost/correctness
        # the per-row lines carry, so a resume log is self-contained at a glance.
        carried = reused_rows + preserved
        kept_correct = sum(1 for r in carried if r.correct)
        kept_cost = sum(r.cost_usd for r in carried)
        kept_in = sum(r.input_tokens for r in carried)
        kept_out = sum(r.output_tokens for r in carried)
        kept_cachew = sum(r.cache_creation_tokens for r in carried)
        kept_cacher = sum(r.cache_read_tokens for r in carried)
        print(
            f"[resume] kept {len(carried)} prior row(s) ({len(reused_rows)} in-scope + "
            f"{len(preserved)} out-of-scope): {kept_correct}/{len(carried)} correct, "
            f"${kept_cost:.2f} / {kept_in // 1000}k in + {kept_out // 1000}k out + "
            f"{kept_cachew // 1_000_000}M cacheW + {kept_cacher // 1_000_000}M cacheR tok "
            f"already spent; running {len(pending)} new job(s)",
            flush=True,
        )

    def _one(job: tuple[Any, str, int]) -> ArmResult:
        inst, arm, rep = job
        job_env = agent_env
        tok = token_slots.get() if token_slots is not None else None
        if tok is not None:
            job_env = {**agent_env, "CLAUDE_CODE_OAUTH_TOKEN": tok}
        try:
            res = incontainer.run_in_container(
                inst,
                arm,
                rep,
                driver=args.driver,
                model=args.model,
                out_dir=out_dir,
                timeout=args.timeout,
                agent_env=job_env,
                max_turns=args.max_turns,
            )
        finally:
            if tok is not None and token_slots is not None:
                token_slots.put(tok)
        # Grade inline the moment the rep finishes (token already released so other
        # runs proceed) -> live correctness in results.jsonl, no end-of-run wait.
        if not args.no_grade:
            _grade_one(inst, arm, rep, res, grade_fn=grade_fn, out_dir=out_dir, label=grade_label)
        return res

    with ThreadPoolExecutor(max_workers=max(1, min(effective_jobs, len(pending) or 1))) as pool:
        futures = {pool.submit(_one, job): job for job in pending}
        for fut in as_completed(futures):
            inst, arm, rep = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # keep going; record a failed row
                res = ArmResult(
                    inst.instance_id,
                    arm,
                    rep,
                    False,
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    [],
                    True,
                    f"runner error: {exc}"[:200],
                    "",
                )
            results.append(res)
            # Persist incrementally so partial progress is durable and visible
            # mid-run (resume-safe: `results` already holds reused/preserved rows,
            # so a full rewrite never duplicates). Runs in the main thread, so the
            # as_completed loop serializes these writes.
            _write_results_jsonl(out_dir, results)
            print(
                f"  -> {inst.instance_id}/{arm} rep{rep}: ok={res.ok} correct={res.correct} "
                f"cost=${res.cost_usd:.4f} turns={res.num_turns} "
                f"in={res.input_tokens} out={res.output_tokens} "
                f"cacheW={res.cache_creation_tokens // 1000}k cacheR={res.cache_read_tokens // 1000}k",
                flush=True,
            )

    if not args.no_grade:
        _grade_arms(
            instances,
            results,
            out_dir=out_dir,
            reps=args.reps,
            arms=args.arms,
            grade_workers=args.grade_workers,
            grade_fn=grade_fn,
            label=grade_label,
        )

    _apply_savings(results)
    pairwise = build_pairwise_quality_rows(results)
    _write_results_jsonl(out_dir, results)
    write_csv_artifacts(out_dir, results, pairwise)
    end_fingerprint = _runtime_source_fingerprint()
    manifest = _write_benchmark_manifest(
        out_dir,
        args=args,
        instances=instances,
        grade_label=grade_label,
        start_fingerprint=source_fingerprint,
        end_fingerprint=end_fingerprint,
    )
    rendered = report(results)
    rendered += (
        "\n\n=== Runtime provenance ===\n"
        f"claude_code_version: {manifest.get('claude_code_version') or 'n/a'}\n"
        f"lemoncrow_git_commit: {manifest['lemoncrow_git_commit_start']}\n"
        f"runtime_source_fingerprint: {manifest['runtime_source_fingerprint_start']}\n"
        f"runtime_source_changed_during_run: {manifest['runtime_source_changed_during_run']}\n"
        "manifest: benchmark-manifest.json"
    )
    if manifest["runtime_source_changed_during_run"]:
        rendered += (
            "\nWARNING: live benchmark runtime source changed during this run; "
            "do not publish this result as a controlled release comparison."
        )
    (out_dir / "report.txt").write_text(rendered, encoding="utf-8")
    print(rendered, flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="SWE A/B: vanilla Claude Code vs LemonCrow, in-container (multi-swe-bench or swe-bench)"
    )
    p.add_argument(
        "--suite",
        choices=["multi-swe-bench", "swe-bench-verified", "swe-lite", "swe-pro"],
        default="multi-swe-bench",
        help=(
            "Backend: multi-swe-bench (7 non-Python langs), swe-bench-verified, swe-lite (Python), "
            "or swe-pro (SWE-bench Pro, ScaleAI harness)."
        ),
    )
    p.add_argument("--dataset", default=None, help="Dataset path/name (default: per-suite default)")
    p.add_argument("--languages", nargs="*", default=None, help="Filter to these languages (multi-swe-bench only)")
    p.add_argument("--per-language-limit", type=int, default=None, help="Max instances per language")
    p.add_argument("--min-changed-files", type=int, default=2, help="Min files in the gold patch (multi-file filter)")
    p.add_argument("--limit", type=int, default=None, help="Max total instances")
    p.add_argument("--instances", nargs="*", default=None, help="Explicit instance ids to run")
    p.add_argument("-a", "--arms", nargs="*", default=list(ARMS), choices=ARMS)
    p.add_argument(
        "--driver",
        choices=["claude", "cursor", "lemoncode"],
        default="claude",
        help="Agent CLI run inside each container: claude (default) or cursor-agent. "
        "cursor requires a host `cursor-agent login` (auth.json is mounted in).",
    )
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--model", default="claude-opus-4-8")
    p.add_argument(
        "--max-turns",
        type=int,
        default=50,
        help=(
            "Runaway-loop safety cap on agentic turns. Kept at 50: raising it to 100 let "
            "non-converging tasks spiral into the 1800s --timeout wall (more cost, same "
            "failure) instead of stopping early. Converging tasks finish well below it."
        ),
    )
    p.add_argument("--timeout", type=int, default=1800, help="Per-run agent timeout (s)")
    p.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Global maximum parallel container runs (also caps OAuth-token rotation)",
    )
    p.add_argument(
        "--jobs-per-token",
        type=int,
        default=4,
        help="Max concurrent container runs per OAuth token (CLAUDE_CODE_OAUTH_TOKEN_1/_2). "
        "Total parallelism = jobs-per-token x tokens available (e.g. 2 tokens -> 8).",
    )
    p.add_argument("--grade-workers", type=int, default=4, help="multi_swe_bench eval workers")
    p.add_argument("--no-grade", action="store_true", help="Skip Docker grading (cost/turns only)")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing results.jsonl rows whose patch artifact is present; run only the rest",
    )
    p.add_argument("--out", default=None, help="Results dir")
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
