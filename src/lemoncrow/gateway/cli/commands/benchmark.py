"""``lc benchmark`` command group.

Quick-reference invocation patterns
------------------------------------

All examples use ``lc benchmark codebench``
(default task = all, default model = sonnet).


LemonCrow vs Baseline on Claude CLI (default transport)
......................................................

  # LemonCrow arm (latent + swarm), local Claude CLI:
  lc benchmark codebench --arm lemoncrow

  # Baseline arm (no LemonCrow, vanilla Claude CLI):
  lc benchmark codebench --arm baseline

  # Compare both in one run:
  lc benchmark codebench --arm baseline --arm lemoncrow

  # With a specific model:
  lc benchmark codebench --arm lemoncrow --model claude-sonnet-4-20250514

  # Limit to a single task for fast iteration:
  lc benchmark codebench --task codegen_hello_world --arm lemoncrow



OpenCode as the CLI driver (--cli-driver opencode)
...................................................

  # LemonCrow arm, but the sub-task prompt is handed to `opencode run`:
  lc benchmark codebench --arm lemoncrow --cli-driver opencode

  # Compare lemoncrow vs baseline on OpenCode driver:
  lc benchmark codebench --arm baseline --arm lemoncrow --cli-driver opencode


LemonCrow on Bedrock (AWS) with rate limiting
............................................

  Shorthand via --provider:

    lc benchmark codebench --arm lemoncrow --provider bedrock --rate-limit-rpm 5
    lc benchmark codebench --arm baseline --arm lemoncrow --provider bedrock --rate-limit-rpm 5

  Explicit preset (same effect):

    lc benchmark codebench --arm lemoncrow --claude-provider-preset aws-claude --rate-limit-rpm 5

  With token-level rate limit:

    lc benchmark codebench --arm lemoncrow --provider bedrock --rate-limit-rpm 5 --rate-limit-tpm 50000


Baseline on Bedrock with rate limiting
.......................................

  lc benchmark codebench --arm baseline --provider bedrock --rate-limit-rpm 5


LemonCrow on GCP Vertex with rate limiting
........................................

  lc benchmark codebench --arm lemoncrow --provider gcp --rate-limit-rpm 5
  lc benchmark codebench --arm baseline --arm lemoncrow --provider gcp --rate-limit-rpm 5


LemonCrow on Azure with rate limiting
....................................

  lc benchmark codebench --arm lemoncrow --provider azure --rate-limit-rpm 5
  lc benchmark codebench --arm baseline --arm lemoncrow --provider azure --rate-limit-rpm 5


LemonCrow on OpenRouter
.....................

  lc benchmark codebench --arm lemoncrow --provider openrouter --rate-limit-rpm 10
  lc benchmark codebench --arm baseline --arm lemoncrow --provider openrouter --rate-limit-rpm 10


All five arms together (compare everything)
...........................................

  lc benchmark codebench --arm baseline --arm lemoncrow --arm lemoncrow.raw \
      --cli-driver claude --reps 3


LemonCrow-run arm (runs ``lc run start`` as the driver -- LemonCrow's own
owned-agent loop, using YOUR API credentials directly)
........................................................

  lc benchmark codebench --arm lemoncrow --cli-driver lemoncrow-run

  # LemonCrow-run on Bedrock with rate limiting (the driver is `lc run start`,
  # not the `claude` CLI -- `lc run` uses your own ANTHROPIC_API_KEY or
  # other provider credentials):
  lc benchmark codebench --arm lemoncrow --cli-driver lemoncrow-run \
      --model us.anthropic.claude-sonnet-4-6 --rate-limit-rpm 10

  # Compare lemoncrow (plugin) vs lemoncrow-run (owned-agent loop) on Bedrock:
  lc benchmark codebench \
      --arm lemoncrow \
      --cli-driver lemoncrow-run \
      --model us.anthropic.claude-sonnet-4-6 \
      --rate-limit-rpm 10 \
      --reps 1


LemonCrow on Bedrock with explicit model + rate limit (copy-paste ready)
......................................................................

  # LemonCrow plugin arm via Claude CLI routed through Bedrock:
  lc benchmark codebench \
      --arms lemoncrow \
      --provider bedrock \
      --model us.anthropic.claude-sonnet-4-6 \
      --rate-limit-rpm 10 \
      --transport cli --cli-driver claude \
      --reps 1 --tasks all

  # Compare lemoncrow vs baseline on Bedrock:
  lc benchmark codebench \
      --arms baseline --arms lemoncrow \
      --provider bedrock \
      --model us.anthropic.claude-sonnet-4-6 \
      --rate-limit-rpm 10 \
      --reps 1 --tasks all


Common pitfalls
...............

  # WRONG: --cli-driver lemoncrow-run gets rejected by the CLI gateway if the
  # click.Choice is out of sync. This is now fixed.
  #
  # WRONG: --cli-extra-arg=--provider --cli-extra-arg=bedrock
  # Those get forwarded to the CLI driver binary (claude / lc run start),
  # not to the benchmark harness. Use --provider / --agent-env instead.
  #
  # CORRECT: use --provider to set cloud-provider env vars for the claude CLI:
  lc benchmark codebench --arm lemoncrow --provider bedrock --rate-limit-rpm 5


Use --help on the sub-command for all available flags:

  lc benchmark codebench --help
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from os import cpu_count, environ
from pathlib import Path
from shutil import which

import click

from lemoncrow.core.capabilities.benchmark_evidence import (
    build_codebench_evidence,
    git_state,
    write_benchmark_evidence,
)
from lemoncrow.core.capabilities.benchmark_gate import (
    evaluate_codebench_gate,
    load_benchmark_gate,
    require_benchmark_gate_pass,
    write_benchmark_gate,
)
from lemoncrow.core.capabilities.benchmark_manifest import (
    build_codebench_manifest,
    write_benchmark_manifest,
)
from lemoncrow.core.capabilities.host_runners import (
    CLAUDE_PROVIDER_PRESETS,
    resolve_claude_provider_preset,
)
from lemoncrow.gateway.cli.progress import ProgressReporter

_PROVIDER_ALIASES: dict[str, str] = {
    "aws": "aws-claude",
    "bedrock": "aws-claude",
    "gcp": "gcp-claude",
    "vertex": "gcp-claude",
    "azure": "azure-claude",
    "openrouter": "openrouter-claude",
}


@click.group("benchmark")
def benchmark_group() -> None:
    """Run LemonCrow benchmark suites and reports."""


@benchmark_group.command("mini")
@click.option("--dry-run", "dry_run", is_flag=True, help="Validate cases, print plan, no API calls.")
@click.option("--limit", default=5, show_default=True, type=int, help="Max cases to run.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON report to stdout.")
@click.option("--output", default=None, help="Path to write JSON report (default: .lemoncrow/evals/mini-report.json)")
@click.option("--cases", "cases_path", default=None, help="Path to cases YAML (default: benchmarks/mini/cases.yaml)")
@click.pass_context
def benchmark_mini_cmd(
    ctx: click.Context,
    dry_run: bool,
    limit: int,
    as_json: bool,
    output: str | None,
    cases_path: str | None,
) -> None:
    """Run the LemonCrow mini eval suite (5-10 tasks, cost-quality proof).

    \b
    Usage:
      lc benchmark mini --dry-run --json       # Offline validation, no API keys needed
      lc benchmark mini --limit 5 --json        # Run 5 cases, write JSON report
    """
    import json as _json

    bench_root = _bench_source_root()
    if str(bench_root) not in sys.path:
        sys.path.insert(0, str(bench_root))
    from benchmarks.mini import load_cases, render_markdown, repo_root, run_suite, save_report

    root: Path = ctx.obj["root"]
    git_repo = repo_root()

    try:
        cases = load_cases(cases_path)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    report = run_suite(cases, root=root, git_repo=git_repo, dry_run=dry_run, limit=limit)

    if output:
        json_path = Path(output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            _json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path = json_path.with_suffix(".md")
        md_path.write_text(render_markdown(report), encoding="utf-8")
    else:
        json_path, _md_path = save_report(report, Path(root) / "evals")

    if as_json:
        click.echo(_json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return

    status_str = {"pass": "PASS", "fail": "FAIL", "dry_run": "DRY RUN"}.get(report.status, report.status)
    click.echo(f"eval mini status={status_str} suite={report.suite}")
    click.echo(f"tasks={report.total_tasks} accepted={report.accepted_tasks} failed={report.failed_tasks}")
    click.echo(f"accepted_patch_rate={report.accepted_patch_rate:.2f}")
    click.echo(f"total_cost_usd=${report.total_cost_usd:.4f}")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    click.echo(f"cheap_success_rate={report.cheap_success_rate:.2f}")
    click.echo(f"trace_coverage_pct={report.trace_coverage_pct:.0f}%")
    click.echo(f"routing_regression_rate={report.routing_regression_rate:.4f}")
    click.echo(f"report: {json_path}")


@benchmark_group.command("harbor")
@click.option(
    "--dataset",
    "-d",
    default="terminal-bench/terminal-bench-2-1",
    show_default=True,
    help="Harbor dataset to run against.",
)
@click.option("--limit", default=None, type=int, help="Max tasks to run (default: all).")
@click.option(
    "--include-task",
    "-i",
    "include_tasks",
    multiple=True,
    help="Run only these task names (repeatable). Forwarded to harbor -i/--include-task-name; "
    "use it to re-run a specific regression set instead of the whole dataset.",
)
@click.option(
    "--agent",
    "agent_arm",
    default="lemoncrow-claude-code",
    type=click.Choice(["lemoncrow", "lemoncrow-bedrock", "lemoncrow-claude-code"]),
    show_default=True,
    help="Agent arm: direct API, Bedrock, or Claude Code CLI + LemonCrow plugin.",
)
@click.option("--baseline", is_flag=True, default=False, help="Run baseline arm (bench_mode=off, no plugin).")
@click.option("--model", default=None, help="Model override (default: LEMONCROW_BENCH_MODEL or claude-opus-4-8).")
@click.option(
    "--attempts",
    "-n",
    default=5,
    show_default=True,
    type=int,
    help="Number of attempts per task (pass@k scoring).",
)
@click.option(
    "--concurrent",
    "-c",
    default=None,
    type=int,
    help="Max concurrent trials. Default: slots x tokens (LEMONCROW_BENCH_TOKEN_SLOTS x num_tokens).",
)
@click.option(
    "--slots",
    default=None,
    type=int,
    help="Token slots per OAuth token (default: LEMONCROW_BENCH_TOKEN_SLOTS env or 2).",
)
@click.option(
    "--bundle",
    default="/tmp/avbuild/lemoncrow-bundle.tar.gz",
    show_default=True,
    help="Path to prebuilt lemoncrow bundle (claude-code arm only).",
)
@click.option(
    "--rebuild-bundle/--no-rebuild-bundle",
    default=True,
    help="Rebuild bundle from current source before a fresh run (default: on).",
)
@click.option(
    "--filter-error-type",
    "-f",
    "filter_error_types",
    multiple=True,
    help="Remove trials with these error types before resuming (repeatable). Forwarded to harbor job resume -f.",
)
@click.option("--resume", "resume_dir", default=None, help="Resume an existing job dir instead of starting fresh.")
@click.option(
    "--output", "-o", default=None, help="Output directory for results (default: benchmarks/harbor/results/<arm>/)."
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def benchmark_harbor_cmd(
    ctx: click.Context,
    dataset: str,
    limit: int | None,
    include_tasks: tuple[str, ...],
    agent_arm: str,
    baseline: bool,
    model: str | None,
    attempts: int,
    concurrent: int | None,
    slots: int | None,
    bundle: str,
    rebuild_bundle: bool,
    resume_dir: str | None,
    filter_error_types: tuple[str, ...],
    output: str | None,
    yes: bool,
) -> None:
    """Run LemonCrow on a Harbor benchmark dataset.

    \b
    Requires: Docker (for container execution)
    Reads tokens from benchmarks/harbor/.env (CLAUDE_CODE_OAUTH_TOKEN_1/_2).

    \b
    Examples:
      # Fresh run, all tasks, 5 attempts, slots from LEMONCROW_BENCH_TOKEN_SLOTS (auto-rebuilds bundle):
      lc benchmark harbor -y

      # Baseline arm (no LemonCrow plugin):
      lc benchmark harbor --baseline -y

      # Resume a rate-limited job (must point at the dated job dir, not its parent):
      lc benchmark harbor --resume benchmarks/harbor/results/lemoncrow/2026-07-01__12-00-00 -y

      # Quick smoke test (3 tasks, 1 attempt):
      lc benchmark harbor --limit 3 --attempts 1 -y
    """
    import json as _json
    import os as _os
    import shutil
    import subprocess as _subprocess
    from pathlib import Path as _Path

    repo_root = _Path(__file__).parents[5]
    repo_root_str = str(repo_root)

    # ── helpers ────────────────────────────────────────────────────────────
    def _read_env(key: str) -> str:
        """Read key from shell env or benchmarks/harbor/.env."""
        val = _os.environ.get(key, "")
        if val:
            return val
        env_file = repo_root / "benchmarks" / "harbor" / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                k, _, v = stripped.partition("=")
                if k.strip() == key:
                    return v.strip().strip("'\"")
        return ""

    uv_bin = shutil.which("uv")
    harbor_bin = shutil.which("harbor")
    if uv_bin is None and harbor_bin is None:
        raise click.ClickException("harbor / uv not found on PATH.")
    # Prefer the benchmarks project so Harbor does not need to be installed in the root LemonCrow env.
    harbor_cmd_prefix: list[str] = (
        ["uv", "run", "--project", str(repo_root / "benchmarks"), "--no-sync", "harbor"] if uv_bin else ["harbor"]
    )

    # ── OAuth token pool ───────────────────────────────────────────────────
    tokens = [
        _read_env("CLAUDE_CODE_OAUTH_TOKEN_1"),
        _read_env("CLAUDE_CODE_OAUTH_TOKEN_2"),
    ]
    tokens = [t for t in tokens if t]
    if not tokens:
        # Fall back to bare CLAUDE_CODE_OAUTH_TOKEN
        single = _read_env("CLAUDE_CODE_OAUTH_TOKEN")
        if single:
            tokens = [single]
    if agent_arm == "lemoncrow-claude-code" and not tokens:
        raise click.ClickException(
            "No OAuth token found. Set CLAUDE_CODE_OAUTH_TOKEN_1 (and optionally _2) "
            "in benchmarks/harbor/.env or your shell."
        )
    if slots is None:
        slots = int(_read_env("LEMONCROW_BENCH_TOKEN_SLOTS") or "2")
    n_concurrent = concurrent if concurrent is not None else slots * max(len(tokens), 1)

    # ── Agent setup ────────────────────────────────────────────────────────
    _agent_import_paths = {
        "lemoncrow": "benchmarks.harbor.lemoncrow_agent:LemonCrowHarborAgent",
        "lemoncrow-bedrock": "benchmarks.harbor.lemoncrow_agent:LemonCrowBedrockHarborAgent",
        "lemoncrow-claude-code": "benchmarks.harbor.lemoncrow_agent:LemonCrowClaudeCodeHarborAgent",
    }
    agent_import_path = _agent_import_paths[agent_arm]

    # ── Output dir ─────────────────────────────────────────────────────────
    arm_label = "baseline" if baseline else "lemoncrow"
    out_dir = _Path(output) if output else repo_root / "benchmarks" / "harbor" / "results" / arm_label
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir_str = str(out_dir)

    # ── Resume path ────────────────────────────────────────────────────────
    if resume_dir:
        jd = _Path(resume_dir)
        click.echo(f"Resuming job at: {jd}")
        if not yes:
            click.confirm("Proceed?", abort=True)
        existing_pythonpath = _os.environ.get("PYTHONPATH", "")
        pythonpath = f"{repo_root_str}:{existing_pythonpath}" if existing_pythonpath else repo_root_str
        env = {**_os.environ, "PYTHONPATH": pythonpath, "LEMONCROW_BENCH_TOKEN_SLOTS": str(slots)}
        for i, tok in enumerate(tokens, 1):
            env[f"CLAUDE_CODE_OAUTH_TOKEN_{i}"] = tok
        filter_args = []
        for et in filter_error_types:
            filter_args.extend(["-f", et])
        ret = _subprocess.call(
            [*harbor_cmd_prefix, "job", "resume", "-p", str(jd), *filter_args, "-y"],
            env=env,
        )
        if ret != 0:
            raise click.ClickException(f"harbor job resume exited with code {ret}")
        return

    # ── Bundle handling (claude-code arm only) ─────────────────────────────
    bundle_path = _Path(bundle)
    if agent_arm == "lemoncrow-claude-code":
        if rebuild_bundle:
            # Always a from-scratch build, never a patch onto whatever bundle a
            # prior run left behind -- back-to-back fresh runs must not leak
            # state into each other. Only --resume (handled earlier, above)
            # reuses an existing bundle.
            if ctx.get_parameter_source("bundle") == click.core.ParameterSource.DEFAULT:
                # Caller didn't pin a path -- tag the built artifact with the
                # source commit so the bundle filename on disk shows what it
                # was built from.
                commit_sha = (
                    _subprocess.run(
                        ["git", "-C", repo_root_str, "rev-parse", "--short", "HEAD"],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    or "nogit"
                )
                bundle_path = bundle_path.with_name(f"lemoncrow-bundle-{commit_sha}.tar.gz")
            click.echo(f"Building bundle from current source -> {bundle_path} ...")
            rebuild_script = repo_root / "benchmarks" / "harbor" / "build_bundle.sh"
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            rebuild_cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{repo_root_str}:/lemoncrow:ro",
                "-v",
                f"{bundle_path.parent}:/out",
                "debian:bullseye-slim",
                "bash",
                f"/lemoncrow/{rebuild_script.relative_to(repo_root)}",
            ]
            click.echo(f"Command: {' '.join(rebuild_cmd)}\n")
            ret = _subprocess.call(rebuild_cmd)
            if ret != 0:
                raise click.ClickException("Bundle rebuild failed.")
            new_bundle = bundle_path.parent / "lemoncrow-bundle-new.tar.gz"
            if not new_bundle.exists():
                raise click.ClickException("Bundle rebuild produced no output.")
            new_bundle.rename(bundle_path)
            click.echo(f"Bundle built: {bundle_path} ({bundle_path.stat().st_size // 1024 // 1024} MB)")
        if not bundle_path.exists():
            raise click.ClickException(
                f"Bundle not found: {bundle_path}. Run without --no-rebuild-bundle to build it first."
            )

    # ── Print plan ─────────────────────────────────────────────────────────
    click.echo("\n◆ Harbor eval")
    click.echo(f"  dataset          : {dataset}")
    click.echo(f"  arm              : {arm_label}")
    click.echo(f"  model            : {model or _read_env('LEMONCROW_BENCH_MODEL') or 'claude-opus-4-8'}")
    click.echo(f"  attempts/task    : {attempts}")
    if include_tasks:
        click.echo(f"  include-task     : {', '.join(include_tasks)}")
    click.echo(f"  concurrent       : {n_concurrent}  ({slots} slots x {len(tokens)} token(s))")
    click.echo(f"  output           : {out_dir_str}")
    if agent_arm == "lemoncrow-claude-code":
        click.echo(f"  bundle           : {bundle_path}")
    click.echo("")
    if not yes:
        click.confirm("Start run?", abort=True)

    # ── Build harbor run command ────────────────────────────────────────────
    # No source-repo bind mount: the claude-code arm's install() reads the
    # plugin dir out of the bundle's installed lemoncrow package (it's
    # force-included in the wheel -- see pyproject.toml's
    # [tool.hatch.build.targets.wheel.force-include]), not off a live
    # working-tree mount. The other arms pip-install lemoncrow from PyPI and
    # never touch a source mount at all.
    mounts = []
    if agent_arm == "lemoncrow-claude-code":
        mounts.append(
            {"type": "bind", "source": str(bundle_path), "target": "/lemoncrow-bundle.tar.gz", "read_only": True}
        )

    cmd = [
        *harbor_cmd_prefix,
        "run",
        "--dataset",
        dataset,
        "--agent",
        agent_import_path,
        "--jobs-dir",
        out_dir_str,
        "--mounts",
        _json.dumps(mounts),
        # -k / --n-attempts is attempts-per-task; -n / --n-concurrent is
        # concurrency (set explicitly below). Don't swap these: passing
        # attempts to -n silently ran 1 attempt/task at attempts-way concurrency.
        "-k",
        str(attempts),
        "--n-concurrent",
        str(n_concurrent),
        "-y",
    ]
    if limit is not None:
        cmd += ["--n-tasks", str(limit)]
    # Harbor matches the fully-qualified task name ("<org>/<task>"). Accept bare
    # names and auto-prepend the dataset's namespace so `-i model-extraction-...`
    # works without the user retyping the `terminal-bench/` prefix.
    _task_ns = dataset.split("/", 1)[0]
    for _task in include_tasks:
        cmd += ["-i", _task if "/" in _task else f"{_task_ns}/{_task}"]
    if model:
        cmd += ["--model", model]
    if baseline:
        cmd += ["--ak", "bench_mode=off"]
    if agent_arm == "lemoncrow-claude-code":
        # Recorded into config.agents[].kwargs.reasoning_effort -- the only
        # place third-party tooling (e.g. the TB-2.1 leaderboard's `lb filter`)
        # can see this from. LemonCrowClaudeCodeHarborAgent.CLI_FLAGS resolves
        # the same value (kwarg > this env var > "high") to build the actual
        # `claude --effort` flag, so the two never drift apart. See
        # harbor-framework/terminal-bench-2-1#166.
        cmd += ["--ak", f"reasoning_effort={_read_env('LEMONCROW_BENCH_EFFORT') or 'high'}"]

    # ── Env: PYTHONPATH + token pool + slots ───────────────────────────────
    existing_pythonpath = _os.environ.get("PYTHONPATH", "")
    pythonpath = f"{repo_root_str}:{existing_pythonpath}" if existing_pythonpath else repo_root_str
    run_env = {**_os.environ, "PYTHONPATH": pythonpath, "LEMONCROW_BENCH_TOKEN_SLOTS": str(slots)}
    # Forward all bench env vars from .env
    for key in (
        "LEMONCROW_BENCH_MODEL",
        "LEMONCROW_BENCH_EFFORT",
        "LEMONCROW_BENCH_DISALLOWED_TOOLS",
        "LEMONCROW_BENCH_HIDDEN_MCP_TOOLS",
    ):
        val = _read_env(key)
        if val:
            run_env[key] = val
    # Token pool: _1 / _2 for dual-subscription management
    for i, tok in enumerate(tokens, 1):
        run_env[f"CLAUDE_CODE_OAUTH_TOKEN_{i}"] = tok
    if len(tokens) == 1:
        run_env["CLAUDE_CODE_OAUTH_TOKEN"] = tokens[0]

    click.echo(f"Command: {' '.join(cmd)}\n")
    ret = _subprocess.call(cmd, env=run_env)
    if ret != 0:
        raise click.ClickException(f"harbor run exited with code {ret}")
    click.echo(f"\n✓ Harbor eval complete. Results in: {out_dir_str}")


@benchmark_group.command("gate", hidden=True)
@click.option(
    "--run-dir",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="Benchmark run directory containing benchmark-gate.json.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the loaded benchmark gate as JSON.")
@click.option(
    "--require-pass/--allow-failed-gate",
    default=False,
    show_default=True,
    help="Exit non-zero when the loaded benchmark gate did not pass.",
)
def benchmark_gate_cmd(run_dir: Path, as_json: bool, require_pass: bool) -> None:
    """Load an existing benchmark gate artifact and optionally fail on a failed gate."""
    try:
        gate = load_benchmark_gate(run_dir.resolve())
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(gate))
    else:
        click.echo(f"suite: {gate.get('suite', '')}")
        click.echo(f"passed: {bool(gate.get('passed'))}")
        for reason in gate.get("reasons", []) or []:
            click.echo(f"- {reason}")
    if require_pass:
        try:
            require_benchmark_gate_pass(run_dir.resolve())
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc


@benchmark_group.command("codebench")
@click.option(
    "--task",
    "tasks",
    multiple=True,
    default=("all",),
    show_default=True,
    help="CodeBench task id; repeat for multiple or use 'all'.",
)
@click.option(
    "--list",
    "list_tasks",
    is_flag=True,
    default=False,
    help="List available CodeBench task ids and exit.",
)
@click.option(
    "--arm",
    "arms",
    multiple=True,
    default=("baseline", "lemoncrow"),
    show_default=True,
    type=click.Choice(["baseline", "lemoncrow"]),
)
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--model", default="sonnet", show_default=True)
@click.option("--timeout", type=int, default=1800, show_default=True)
@click.option(
    "--rate-limit-rpm",
    type=click.FloatRange(min=0),
    default=0,
    show_default=True,
    help="Maximum model inference requests per minute; 0 disables throttling.",
)
@click.option(
    "--rate-limit-tpm",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Maximum reserved output tokens per rolling minute; 0 disables throttling.",
)
@click.option(
    "--cli-driver",
    type=click.Choice(["claude", "copilot", "codex", "opencode", "lemoncrow-run", "cursor"]),
    default="claude",
    show_default=True,
    help="CLI host to benchmark.",
)
@click.option(
    "--jobs",
    type=int,
    default=1,
    show_default=True,
    help="Parallel task/rep workers; arms stay serial within each worker.",
)
@click.option(
    "--parallel-scope",
    type=click.Choice(["task", "arm"]),
    default="task",
    show_default=True,
    help="Use 'arm' only for throughput experiments; 'task' preserves fair per-task comparisons.",
)
@click.option("--judge", is_flag=True, help="Score correctness with an LLM judge.")
@click.option("--judge-model", default=None)
@click.option("--judge-agent-command", default=None)
@click.option(
    "--agent-command",
    default="claude",
    show_default=True,
    help="Claude-compatible command to run each arm.",
)
@click.option(
    "--agent-env",
    "agent_env",
    multiple=True,
    help="CLI transport env override in KEY=VALUE form; repeatable.",
)
@click.option(
    "--agent-env-from-host",
    "agent_env_from_host",
    multiple=True,
    help="Copy a host env var into the Claude CLI env as DEST_KEY=SOURCE_ENV; repeatable.",
)
@click.option(
    "--cli-extra-arg",
    "cli_extra_args",
    multiple=True,
    help="Extra CLI argument passed to the selected driver; repeatable.",
)
@click.option(
    "--openrouter-claude/--no-openrouter-claude",
    "--openrouter-anthropic/--no-openrouter-anthropic",
    "openrouter_claude",
    default=False,
    show_default=True,
    help="Preset Claude CLI env for OpenRouter's Anthropic-compatible endpoint.",
)
@click.option(
    "--claude-provider-preset",
    type=click.Choice(sorted(CLAUDE_PROVIDER_PRESETS)),
    default=None,
    help="Named Claude CLI provider preset (for example openrouter-claude, aws-claude, azure-claude, gcp-claude).",
)
@click.option(
    "--openrouter-key-env",
    default="OPENROUTER_API_KEY",
    show_default=True,
    help="Host env var that holds the OpenRouter API key for --openrouter-claude.",
)
@click.option(
    "--claude-base-url",
    default=None,
    help="Set ANTHROPIC_BASE_URL for Claude CLI transport.",
)
@click.option(
    "--claude-auth-token-env",
    default=None,
    help="Copy a host env var into ANTHROPIC_AUTH_TOKEN for Claude CLI transport.",
)
@click.option(
    "--claude-api-key-env",
    default=None,
    help="Copy a host env var into ANTHROPIC_API_KEY for Claude CLI transport.",
)
@click.option(
    "--clear-claude-api-key",
    is_flag=True,
    help="Set ANTHROPIC_API_KEY to an empty string for Claude CLI transport.",
)
@click.option("--bridge-command", default=None, help="Optional background bridge command to launch first.")
@click.option("--bridge-wait", type=float, default=3.0, show_default=True)
@click.option(
    "--task-source-dir",
    "codebench_tasks_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
)
@click.option(
    "--require-pass/--allow-failed-gate",
    default=False,
    show_default=True,
    help="Exit non-zero after writing artifacts when the benchmark gate does not pass.",
)
@click.option(
    "--provider",
    default=None,
    metavar="PROVIDER",
    help=(
        "Cloud provider shorthand: aws/bedrock, gcp/vertex, azure, openrouter. "
        "Reads credentials from .env or the current environment. "
        "Shorthand for --claude-provider-preset; explicit --agent-env takes precedence."
    ),
)
def benchmark_codebench_cmd(
    tasks: tuple[str, ...],
    list_tasks: bool,
    arms: tuple[str, ...],
    reps: int,
    model: str,
    timeout: int,
    rate_limit_rpm: float,
    rate_limit_tpm: int,
    cli_driver: str,
    jobs: int,
    parallel_scope: str,
    judge: bool,
    judge_model: str | None,
    judge_agent_command: str | None,
    agent_command: str,
    agent_env: tuple[str, ...],
    agent_env_from_host: tuple[str, ...],
    cli_extra_args: tuple[str, ...],
    openrouter_claude: bool,
    claude_provider_preset: str | None,
    openrouter_key_env: str,
    claude_base_url: str | None,
    claude_auth_token_env: str | None,
    claude_api_key_env: str | None,
    clear_claude_api_key: bool,
    bridge_command: str | None,
    bridge_wait: float,
    codebench_tasks_dir: Path | None,
    require_pass: bool,
    provider: str | None,
) -> None:
    """Run cost/quality comparison (LemonCrow vs baseline) and write a report."""
    repo_root = Path.cwd().resolve()
    if list_tasks:
        catalog = _load_codebench_catalog(repo_root)
        click.echo(f"{len(catalog)} CodeBench tasks:")
        for task in catalog:
            click.echo(f"  {task.get('id', '?')!s:30} {task.get('language', '')!s:12} {task.get('source', '')!s}")
        return
    run_dir = _codebench_run_dir(repo_root)
    resolved_codebench_tasks_dir = _ensure_codebench_tasks_dir(repo_root, codebench_tasks_dir)
    env = {"CODEBENCH_TASKS_DIR": str(resolved_codebench_tasks_dir)}
    bridge_args = []
    if bridge_command:
        bridge_args = ["--bridge-command", bridge_command, "--bridge-wait", str(bridge_wait)]
    judge_args = []
    if judge:
        judge_args.append("--judge")
    if judge_model:
        judge_args.extend(["--judge-model", judge_model])
    if judge_agent_command:
        judge_args.extend(["--judge-agent-command", judge_agent_command])
    agent_env_args: list[str] = []
    if provider:
        preset_key = _PROVIDER_ALIASES.get(provider.lower())
        if preset_key is None:
            raise click.ClickException(
                f"unknown --provider {provider!r}; choices: {', '.join(sorted(_PROVIDER_ALIASES))}"
            )
        claude_provider_preset = claude_provider_preset or preset_key
    if openrouter_claude:
        claude_provider_preset = claude_provider_preset or "openrouter-claude"
    if claude_provider_preset:
        preset = resolve_claude_provider_preset(
            claude_provider_preset,
            openrouter_key_env=openrouter_key_env,
        )
        if cli_driver not in preset.supported_drivers:
            raise click.ClickException(
                f"{claude_provider_preset} only supports CLI drivers: {', '.join(preset.supported_drivers)}"
            )
        for key, value in preset.env.items():
            agent_env_args.extend(["--agent-env", f"{key}={value}"])
        for dest, source in preset.env_from_host.items():
            agent_env_args.extend(["--agent-env-from-host", f"{dest}={source}"])
    if claude_base_url:
        agent_env_args.extend(["--agent-env", f"ANTHROPIC_BASE_URL={claude_base_url}"])
    if claude_auth_token_env:
        agent_env_args.extend(["--agent-env-from-host", f"ANTHROPIC_AUTH_TOKEN={claude_auth_token_env}"])
    if claude_api_key_env:
        agent_env_args.extend(["--agent-env-from-host", f"ANTHROPIC_API_KEY={claude_api_key_env}"])
    if clear_claude_api_key:
        agent_env_args.extend(["--agent-env", "ANTHROPIC_API_KEY="])
    for item in agent_env:
        agent_env_args.extend(["--agent-env", item])
    for item in agent_env_from_host:
        agent_env_args.extend(["--agent-env-from-host", item])
    baseline_arm = "baseline" if "baseline" in arms else arms[0]
    candidate_arm = next((arm for arm in arms if arm != baseline_arm), baseline_arm)
    task_catalog = _load_codebench_catalog(repo_root)
    task_ids = [task["id"] for task in task_catalog] if tasks == ("all",) else list(tasks)
    task_payload = [task for task in task_catalog if task["id"] in task_ids]
    manifest_path = write_benchmark_manifest(
        run_dir,
        build_codebench_manifest(
            tasks=task_payload,
            arms=list(arms),
            reps=reps,
            model=model,
            cli_driver=cli_driver,
            timeout=timeout,
            jobs=jobs,
            parallel_scope=parallel_scope,
            codebench_tasks_dir=resolved_codebench_tasks_dir,
            bridge_command=bridge_command,
        ),
    )
    repo_state = git_state(repo_root)
    forwarded_cli_extra_args = [f"--cli-extra-arg={arg}" for arg in cli_extra_args]
    # The heavy lifting runs in a subprocess that streams its own per-arm
    # progress straight to the terminal, so this reporter only brackets the
    # run. Disable the heartbeat (it would otherwise re-print a static bar
    # every 30s) and write whole lines (in_place would fight the subprocess
    # output for the cursor).
    progress = ProgressReporter("codebench", total=1, heartbeat_seconds=0, in_place=False)
    progress.start("starting benchmark", current=f"{len(tasks)} task selector(s) x {len(arms)} arm(s)")
    returncode = _run(
        [
            *_python_cmd(repo_root),
            "-m",
            "benchmarks.codebench.run",
            *tasks,
            "--arms",
            *arms,
            "--reps",
            str(reps),
            "--model",
            model,
            "--timeout",
            str(timeout),
            "--rate-limit-rpm",
            str(rate_limit_rpm),
            "--rate-limit-tpm",
            str(rate_limit_tpm),
            "--cli-driver",
            cli_driver,
            "--jobs",
            str(jobs),
            "--parallel-scope",
            parallel_scope,
            "--agent-command",
            agent_command,
            *forwarded_cli_extra_args,
            *agent_env_args,
            *judge_args,
            *bridge_args,
            "--out",
            str(run_dir),
        ],
        cwd=repo_root,
        label="CodeBench",
        env=env,
        check=False,
    )
    results_path = run_dir / "results.jsonl"
    if returncode != 0:
        if not results_path.is_file() or results_path.stat().st_size == 0:
            raise click.ClickException(f"CodeBench failed with exit {returncode} before producing results")
        click.echo(
            f"Note: CodeBench exited {returncode} -- some runs failed, timed out, or were off-topic; "
            "per-task detail is in report.txt. Writing evidence + gate from the runs that completed."
        )
    progress.step("benchmark command complete", current=run_dir.name)
    write_benchmark_evidence(
        run_dir,
        build_codebench_evidence(
            run_dir=run_dir,
            manifest_path=manifest_path,
            repo_state=repo_state,
        ),
    )
    write_benchmark_gate(
        run_dir,
        evaluate_codebench_gate(
            run_dir,
            baseline_arm=baseline_arm,
            candidate_arm=candidate_arm,
        ),
    )
    if require_pass:
        try:
            require_benchmark_gate_pass(run_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("local")
@click.option(
    "--repo",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Your repo to benchmark against (copied per run; never mutated).",
)
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    required=True,
    metavar="TEXT",
    help="A real coding prompt to run on the repo; repeat for up to 10.",
)
@click.option("--model", default="sonnet", show_default=True)
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--max-turns", type=int, default=50, show_default=True, help="Turn cap per run.")
@click.option(
    "--arm",
    "arms",
    multiple=True,
    default=("baseline", "lemoncrow"),
    show_default=True,
    type=click.Choice(["baseline", "lemoncrow"]),
)
@click.option(
    "--cli-driver",
    type=click.Choice(["claude", "copilot", "codex", "opencode", "lemoncrow-run"]),
    default="claude",
    show_default=True,
    help="CLI host to benchmark.",
)
@click.option(
    "--setup",
    "setup",
    multiple=True,
    metavar="CMD",
    help="Setup command run inside each workspace before the agent; repeatable.",
)
@click.option(
    "--provider",
    default=None,
    metavar="PROVIDER",
    help=(
        "Cloud provider shorthand: aws/bedrock, gcp/vertex, azure, openrouter. "
        "Reads credentials from .env or the current environment."
    ),
)
@click.option(
    "--competitor",
    "competitors",
    multiple=True,
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    metavar="MANIFEST.json",
    help=(
        "Benchmark a GitHub tool as a 3rd arm (baseline vs lemoncrow vs <tool>). "
        "Pass a competitor manifest JSON describing how to clone/install/wire the "
        "tool; it runs on the same model, so the delta is the tool. Repeatable."
    ),
)
@click.option("--estimate-only", is_flag=True, help="Print the cost estimate and exit without spending.")
@click.option(
    "--capture/--no-capture",
    default=False,
    show_default=True,
    help=(
        "Capture model traffic via mitmproxy for wire-level cost verification "
        "(requires mitmproxy). Off by default — cost comes from CLI receipts."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt and run.")
def benchmark_local_cmd(
    repo: Path,
    prompts: tuple[str, ...],
    model: str,
    reps: int,
    max_turns: int,
    arms: tuple[str, ...],
    cli_driver: str,
    setup: tuple[str, ...],
    provider: str | None,
    competitors: tuple[Path, ...],
    estimate_only: bool,
    capture: bool,
    yes: bool,
) -> None:
    """Uses LLM: Benchmark LemonCrow vs vanilla on YOUR repo with YOUR prompts (real spend).

    Runs each prompt for both arms on the same model and driver, then reports
    cost / turn / time deltas. Prints an up-front cost estimate and asks to
    confirm before spending. Uses provider API credentials, not a Claude
    subscription.
    """
    repo_abs = repo.expanduser().resolve()
    if not repo_abs.is_dir():
        raise click.ClickException(f"--repo is not a directory: {repo_abs}")
    git_check = subprocess.run(
        ["git", "-C", str(repo_abs), "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    )
    if git_check.returncode != 0:
        raise click.ClickException(f"--repo is not a git repository: {repo_abs}")
    if not 1 <= len(prompts) <= 10:
        raise click.ClickException("provide between 1 and 10 --prompt values")

    agent_env_args: list[str] = []
    if provider:
        preset_key = _PROVIDER_ALIASES.get(provider.lower())
        if preset_key is None:
            raise click.ClickException(
                f"unknown --provider {provider!r}; choices: {', '.join(sorted(_PROVIDER_ALIASES))}"
            )
        preset = resolve_claude_provider_preset(preset_key)
        if cli_driver not in preset.supported_drivers:
            raise click.ClickException(f"{preset_key} only supports CLI drivers: {', '.join(preset.supported_drivers)}")
        for key, value in preset.env.items():
            agent_env_args.extend(["--agent-env", f"{key}={value}"])
        for dest, source in preset.env_from_host.items():
            agent_env_args.extend(["--agent-env-from-host", f"{dest}={source}"])

    run_dir = _run_dir("local", None)
    bench_root = _bench_source_root()

    def _bench_cmd(*, estimate: bool) -> list[str]:
        cmd = [
            *_python_cmd(bench_root),
            "-m",
            "benchmarks.codebench.run",
            "--repo",
            str(repo_abs),
            "--arm",
            *arms,
            "--reps",
            str(reps),
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--cli-driver",
            cli_driver,
            "--out",
            str(run_dir),
        ]
        for prompt in prompts:
            cmd.extend(["--prompt", prompt])
        for cmd_str in setup:
            cmd.extend(["--setup", cmd_str])
        for manifest in competitors:
            cmd.extend(["--competitor", str(Path(manifest).expanduser().resolve())])
        cmd.extend(agent_env_args)
        cmd.append("--capture" if capture else "--no-capture")
        if estimate:
            cmd.append("--estimate-only")
        return cmd

    # Always show the estimate first.
    _run(_bench_cmd(estimate=True), cwd=bench_root, label="benchmark local estimate", check=False)
    if estimate_only:
        return
    if not yes and not click.confirm("Proceed and spend real tokens?"):
        raise click.ClickException("Aborted; no tokens spent.")
    _run(_bench_cmd(estimate=False), cwd=bench_root, label="benchmark local", check=False)
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("telegraphic")
@click.option(
    "--repo",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help=(
        "Repo to run the prompts against (copied per run; never mutated). "
        "Default: a minimal scratch repo with nothing to explore -- these are "
        "general Q&A prompts, not tied to a codebase, and pointing this at a "
        "large real repo lets agents wander it for unrelated tokens/cost noise."
    ),
)
@click.option(
    "--model",
    default="claude-opus-4-8",
    show_default=True,
    help="Every committed telegraphic number in BENCHMARKS.md/caveman.astro is opus-4-8 -- "
    "overriding this produces a run that isn't comparable to the checked-in baseline.",
)
@click.option("--reps", type=int, default=1, show_default=True)
@click.option(
    "--max-turns",
    type=int,
    default=50,
    show_default=True,
    help="Turn cap per run (Q&A prompts need 1-2; small headroom, not codebench's 15).",
)
@click.option(
    "--arm",
    "arms",
    multiple=True,
    default=("baseline", "lemoncrow"),
    show_default=True,
    type=click.Choice(["baseline", "lemoncrow", "caveman"]),
    help=(
        "baseline/lemoncrow run through the full codebench harness (plugin+MCP for "
        "lemoncrow). caveman is vanilla Claude Code plus ONE appended system prompt "
        "(its own SKILL.md verbatim) -- no plugin, no agent, no MCP; isolates the "
        "reply style alone, the way caveman's own harness does."
    ),
)
@click.option(
    "--cli-driver",
    type=click.Choice(["claude", "copilot", "codex", "opencode", "lemoncrow-run"]),
    default="claude",
    show_default=True,
    help="CLI host to benchmark (codebench arms only -- caveman always uses claude).",
)
@click.option(
    "--jobs",
    type=int,
    default=1,
    show_default=True,
    help="Parallel task/rep workers, forwarded to codebench's own --jobs (arms stay serial per worker; codebench arms only).",
)
@click.option("--limit", type=int, default=None, help="Only run the first N of the 20 prompts (smoke test).")
@click.option(
    "--capture/--no-capture",
    default=True,
    show_default=True,
    help=(
        "Wire-capture each call via mitmproxy and write a human-readable "
        "<task>_<arm>_rep<n>.flow_dump.txt next to each .flow file -- on by "
        "default here (unlike codebench's generic ad-hoc mode) because the "
        "whole point of this suite is comparing what each arm actually says."
    ),
)
@click.option("--estimate-only", is_flag=True, help="Print the cost estimate and exit without spending.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt and run.")
def benchmark_telegraphic_cmd(
    repo: Path | None,
    model: str,
    reps: int,
    max_turns: int,
    arms: tuple[str, ...],
    cli_driver: str,
    jobs: int,
    limit: int | None,
    capture: bool,
    estimate_only: bool,
    yes: bool,
) -> None:
    """Uses LLM: token-savings vs vanilla Claude Code on caveman's 20-prompt Q&A set.

    Reproduces JuliusBrussee/caveman's benchmark+eval prompt sets
    (github.com/JuliusBrussee/caveman/tree/main/{benchmarks,evals}) with the
    FULL real LemonCrow runtime as the "lemoncrow" arm (tools + MCP + the
    ``lemoncrow:auto`` persona's shipped ultra reply-register) -- not an
    isolated system-prompt swap, so the number is apples-to-apples with every
    other figure in BENCHMARKS.md. Prints a cost estimate and confirms before
    spending real tokens; report = per-prompt output-token savings, not
    patch-accept-rate (these are Q&A prompts, no golden patch to verify).

    \b
    Usage:
      lc benchmark telegraphic --repo . --limit 2 --estimate-only  # dry run, no spend
      lc benchmark telegraphic --repo . --limit 2 -y               # smoke test, 2 prompts
      lc benchmark telegraphic --repo . -y                         # full 20-prompt run
    """
    bench_root = _bench_source_root()
    if str(bench_root) not in sys.path:
        sys.path.insert(0, str(bench_root))
    from benchmarks.telegraphic import ensure_scratch_repo, load_prompts

    repo_abs = repo.expanduser().resolve() if repo is not None else ensure_scratch_repo()
    if not repo_abs.is_dir():
        raise click.ClickException(f"--repo is not a directory: {repo_abs}")
    git_check = subprocess.run(
        ["git", "-C", str(repo_abs), "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    )
    if git_check.returncode != 0:
        raise click.ClickException(f"--repo is not a git repository: {repo_abs}")

    if not arms:
        raise click.ClickException("no --arm selected")
    from benchmarks.telegraphic.extra_arms import EXTRA_ARMS

    codebench_arms = tuple(a for a in arms if a not in EXTRA_ARMS)
    extra_arm_list = tuple(a for a in arms if a in EXTRA_ARMS)

    prompt_entries = load_prompts(limit=limit)
    if not prompt_entries:
        raise click.ClickException("no prompts loaded from benchmarks/telegraphic/prompts.json")
    prompts = [entry["prompt"] for entry in prompt_entries]

    # codebench's ad-hoc --prompt mode hard-caps at 10 values per invocation
    # (benchmarks/codebench/run.py argparse validation) -- batch into chunks,
    # run each as its own codebench invocation into its own subdir, then
    # remap task ids ("local1".."localN" reset per batch) back to the
    # absolute prompt index before merging into one combined results.jsonl.
    _BATCH = 10
    batches = [prompts[i : i + _BATCH] for i in range(0, len(prompts), _BATCH)] if codebench_arms else []

    run_dir = _run_dir("telegraphic", None, repo_root=repo_abs)

    from benchmarks.codebench.local import estimate_cost

    estimate = estimate_cost(n_prompts=len(prompts), arms=len(arms), reps=reps, model=model, max_turns=max_turns)
    click.echo("=== Cost ESTIMATE (not a charge) ===")
    click.echo(f"  runs:        {estimate['n_runs']} ({len(prompts)} prompt(s) x {len(arms)} arm(s) x {reps} rep(s))")
    click.echo(f"  per run:     ${estimate['per_run_usd']:.4f}")
    click.echo(
        f"  total:       ${estimate['total_usd']:.4f}  (range ${estimate['low_usd']:.4f}-${estimate['high_usd']:.4f})"
    )
    click.echo(f"  basis:       {estimate['basis']}")
    click.echo(f"  assumption:  {estimate['assumption']}")
    click.echo("  NOTE: an estimate only; real spend depends on the agent's actual token use.")
    if estimate_only:
        return
    if not yes and not click.confirm("Proceed and spend real tokens?"):
        raise click.ClickException("Aborted; no tokens spent.")

    def _bench_cmd(batch_prompts: list[str], batch_dir: Path) -> list[str]:
        cmd = [
            *_python_cmd(bench_root),
            "-m",
            "benchmarks.codebench.run",
            "--repo",
            str(repo_abs),
            "--arm",
            *codebench_arms,
            "--reps",
            str(reps),
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--cli-driver",
            cli_driver,
            "--jobs",
            str(jobs),
            "--out",
            str(batch_dir),
        ]
        for prompt in batch_prompts:
            cmd.extend(["--prompt", prompt])
        cmd.append("--capture" if capture else "--no-capture")
        return cmd

    # mitmdump is installed in the active UV environment (the workspace root
    # .venv), alongside the Python used by this command.
    run_env: dict[str, str] | None = None
    if capture and codebench_arms:
        mitmdump = which("mitmdump")
        if mitmdump is None:
            raise click.ClickException(
                "--capture needs mitmdump, but it is not on PATH (uv sync); pass --no-capture to skip wire capture."
            )

    from benchmarks.telegraphic.report import load_results, render_report

    merged: list[dict[str, object]] = []
    for batch_idx, batch_prompts in enumerate(batches):
        batch_dir = run_dir / f"batch{batch_idx}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        _run(
            _bench_cmd(batch_prompts, batch_dir),
            cwd=bench_root,
            label=f"benchmark telegraphic batch {batch_idx + 1}/{len(batches)}",
            env=run_env,
            check=False,
        )
        if capture:
            from benchmarks.flowlib.dump import extract

            for fp in sorted(batch_dir.glob("*.flow")):
                if fp.stat().st_size == 0:
                    continue
                with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
                    extract(str(fp), str(fp.with_suffix(".flow_dump.txt")))
        offset = batch_idx * _BATCH
        for row in load_results(batch_dir):
            m = re.match(r"^local(\d+)$", str(row.get("task", "")))
            if m:
                row = {**row, "task": f"local{offset + int(m.group(1))}"}
            merged.append(row)

    if extra_arm_list:
        # Isolated system-prompt-only arms: no codebench, no plugin/agent/MCP --
        # one claude -p subprocess per (prompt, arm, rep), reusing codebench's
        # own baseline-config isolation so the only variable vs "baseline" is
        # the one appended system prompt.
        from benchmarks.codebench.run import _make_baseline_config
        from benchmarks.telegraphic.extra_arms import run_extra_arm

        total_extra = len(prompt_entries) * len(extra_arm_list) * reps
        done = 0
        for idx, entry in enumerate(prompt_entries):
            task_id = f"local{idx + 1}"
            # Same batch{N}/local{n} layout codebench arms use above, so an extra
            # arm's flow_dump.txt sits next to baseline/lemoncrow's for the same task.
            batch_dir = run_dir / f"batch{idx // _BATCH}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            local_n = idx % _BATCH + 1
            for arm in extra_arm_list:
                for rep in range(reps):
                    done += 1
                    click.echo(f"[{done}/{total_extra}] {task_id} {arm} rep{rep}")
                    flow_path = batch_dir / f"local{local_n}_{arm}_rep{rep}.flow" if capture else None
                    merged.append(
                        run_extra_arm(
                            arm=arm,
                            task_id=task_id,
                            prompt=entry["prompt"],
                            model=model,
                            rep=rep,
                            make_baseline_config=_make_baseline_config,
                            flow_path=flow_path,
                        )
                    )

    (run_dir / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in merged), encoding="utf-8")
    table_md = render_report(merged, prompt_entries)
    (run_dir / "telegraphic_report.md").write_text(table_md, encoding="utf-8")
    click.echo("\n" + table_md)
    click.echo(f"\nResults: {run_dir}")


@benchmark_group.command("swe")
@click.option(
    "--suite",
    type=click.Choice(["multi-swe-bench", "swe-bench-verified", "swe-lite", "swe-pro"]),
    default="multi-swe-bench",
    show_default=True,
    help=(
        "Backend: multi-swe-bench (7 non-Python langs), swe-bench-verified, swe-lite (Python), "
        "or swe-pro (SWE-bench Pro, ScaleAI harness)."
    ),
)
@click.option(
    "--dataset",
    default=None,
    help=(
        "Dataset path/name (default: per-suite default — Multi-SWE-bench flash, SWE-bench Verified, or SWE-bench Lite)."
    ),
)
@click.option(
    "--language",
    "languages",
    multiple=True,
    metavar="LANG",
    help="Restrict to these languages (e.g. go, rust, typescript); repeatable.",
)
@click.option("--per-language-limit", type=int, default=None, help="Max instances per language.")
@click.option(
    "--min-changed-files",
    type=int,
    default=2,
    show_default=True,
    help="Min files in the gold patch (multi-file filter).",
)
@click.option("--limit", type=int, default=None, help="Max total instances across all languages.")
@click.option(
    "--instance",
    "instances",
    multiple=True,
    metavar="ID",
    help="Run only these explicit instance ids; repeatable.",
)
@click.option(
    "--arm",
    "arms",
    multiple=True,
    default=("baseline", "lemoncrow"),
    show_default=True,
    type=click.Choice(["baseline", "lemoncrow"]),
    help="Arm to run; repeat for both.",
)
@click.option(
    "--driver",
    type=click.Choice(["claude", "cursor"]),
    default="claude",
    show_default=True,
    help="Agent CLI inside each container: claude or cursor-agent (needs a host `cursor-agent login`).",
)
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--model", default="claude-opus-4-8", show_default=True)
@click.option(
    "--max-turns",
    type=int,
    default=50,
    show_default=True,
    help="Runaway-loop safety cap on agentic turns; real tasks finish well below it. --timeout is the hard credit guard.",
)
@click.option("--timeout", type=int, default=1800, show_default=True, help="Per-run agent timeout (s).")
@click.option("--jobs", type=int, default=1, show_default=True, help="Parallel container runs.")
@click.option(
    "--grade-workers",
    type=int,
    default=4,
    show_default=True,
    help="Docker eval workers for the suite's grading harness.",
)
@click.option(
    "--grade/--no-grade",
    "grade",
    default=True,
    show_default=True,
    help="Grade each diff with the suite's official Docker harness (off = cost/turns only).",
)
def benchmark_swe_cmd(
    suite: str,
    dataset: str | None,
    languages: tuple[str, ...],
    per_language_limit: int | None,
    min_changed_files: int,
    limit: int | None,
    instances: tuple[str, ...],
    arms: tuple[str, ...],
    driver: str,
    reps: int,
    model: str,
    max_turns: int,
    timeout: int,
    jobs: int,
    grade_workers: int,
    grade: bool,
) -> None:
    """Uses LLM: SWE A/B (vanilla Claude Code vs LemonCrow), graded in-container (real spend).

    Loads + filters instances, builds per-arm Docker overlays, runs each
    (instance, arm, rep) inside its image, extracts the git diff, and grades
    every diff with the suite's official harness. Same model and instance per
    pair, so any cost / quality delta is attributable to LemonCrow.

    \b
    --suite multi-swe-bench    -> 7 non-Python languages (multi_swe_bench harness)
    --suite swe-bench-verified -> Python (swebench harness); --language is ignored
    --suite swe-lite           -> Python, 10 pinned baseline-solvable SWE-bench Verified
                                  instances (swebench harness); --language is ignored
    --suite swe-pro            -> 20 pinned SWE-bench Pro instances (ScaleAI's own harness, a
                                  structurally different dataset/grader); --language is ignored

    \b
    Usage:
      lc benchmark swe --language go --language rust --per-language-limit 5
      lc benchmark swe --suite swe-bench-verified --limit 3 --jobs 2
      lc benchmark swe --suite swe-lite --arm baseline --arm lemoncrow --grade
      lc benchmark swe --suite swe-pro --arm baseline --arm lemoncrow --grade
    """
    bench_root = _bench_source_root()
    run_dir = _run_dir("swe", None)
    # multi-swe-bench is declared in benchmarks/pyproject.toml (not the root
    # project), so the subprocess env must resolve against that project while
    # cwd stays at bench_root, where ``benchmarks`` is importable as a package.
    cmd = [
        *_python_cmd(bench_root / "benchmarks"),
        "-m",
        "benchmarks.codebench.multiswe_run",
        "--suite",
        suite,
        "--arms",
        *arms,
        "--driver",
        driver,
        "--reps",
        str(reps),
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--timeout",
        str(timeout),
        "--jobs",
        str(jobs),
        "--grade-workers",
        str(grade_workers),
        "--min-changed-files",
        str(min_changed_files),
        "--out",
        str(run_dir),
    ]
    if dataset:
        cmd.extend(["--dataset", dataset])
    if languages:
        cmd.extend(["--languages", *languages])
    if per_language_limit is not None:
        cmd.extend(["--per-language-limit", str(per_language_limit)])
    if limit is not None:
        cmd.extend(["--limit", str(limit)])
    if instances:
        cmd.extend(["--instances", *instances])
    if not grade:
        cmd.append("--no-grade")
    _run(cmd, cwd=bench_root, label="benchmark swe", check=False)
    click.echo(f"Results: {run_dir}")


def _codebench_run_dir(repo_root: Path) -> Path:
    # Central, verifiable record location shared with every other suite:
    # reports/benchmark/codebench/<timestamp>/ (NOT benchmarks/codebench/results).
    return _run_dir("codebench", None, repo_root=repo_root)


def _run_dir(suite: str, out: Path | None, *, repo_root: Path | None = None) -> Path:
    if out is not None:
        path = out.resolve()
    else:
        root = (repo_root or Path.cwd()).resolve()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = root / "reports" / "benchmark" / suite / timestamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workspace_dir(suite: str, *, repo_root: Path, run_id: str) -> Path:
    # mcp-only helper (eval_mcp is the sole caller): scratch per-shard
    # artifacts and logs live inside the repo under benchmarks/mcp_tools/results/
    # (gitignored), not a sibling directory outside the checkout. The
    # committed results.csv/summary.csv stay at reports/benchmark/<suite>/ --
    # the documented convention every other suite uses (see _codebench_run_dir).
    path = repo_root.resolve() / "benchmarks" / "mcp_tools" / "results" / run_id / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _auto_jobs(item_count: int, *, hard_cap: int) -> int:
    detected = max(cpu_count() or 1, 1)
    return max(1, min(item_count, hard_cap, detected))


def _resolve_mcp_jobs(requested_jobs: int, *, repo_root: Path, suite_names: list[str] | None = None) -> int:
    if requested_jobs > 0:
        return requested_jobs
    repo_root = repo_root.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from benchmarks.mcp_tools.export_public_mcp_csv import _select_suite_specs

    return _auto_jobs(len(_select_suite_specs(suite_names)), hard_cap=32)


def _mcp_suite_filter(tools: tuple[str, ...]) -> list[str] | None:
    requested: list[str] = []
    for value in tools:
        requested.extend(_csv_values(value))
    return requested or None


def _validate_mcp_suites(suite_names: list[str], *, repo_root: Path) -> None:
    repo_root = repo_root.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from benchmarks.mcp_tools.export_public_mcp_csv import (
        _select_suite_specs,
        _suite_aliases,
        _suite_specs,
    )

    try:
        _select_suite_specs(suite_names)
    except ValueError as exc:
        available = sorted({name for name, _size, _runner in _suite_specs()} | set(_suite_aliases()))
        raise click.ClickException(f"{exc}. Available --tool values: {', '.join(available)}") from exc


def _ensure_codebench_tasks_dir(repo_root: Path, configured_dir: Path | None) -> Path:
    resolved = (
        configured_dir.resolve() if configured_dir is not None else repo_root / "benchmarks" / "codebench" / "cg_tasks"
    )
    tasks_dir = resolved / "tasks"
    if tasks_dir.is_dir():
        return resolved
    raise click.ClickException(
        f"CodeBench tasks directory not found: {tasks_dir}\n"
        "Pass --task-source-dir pointing to a directory that contains a 'tasks/' subdirectory."
    )


def _python_cmd(repo_root: Path) -> list[str]:
    repo_root = repo_root.resolve()
    if which("uv") and (repo_root / "pyproject.toml").is_file():
        return ["uv", "run", "--project", str(repo_root), "python"]
    return [sys.executable]


def _bench_source_root() -> Path:
    """LemonCrow source root that contains the ``benchmarks/`` harness package.

    The ``benchmarks.*`` packages live in the LemonCrow source tree, not in the
    target repo under test, so subprocesses that import them must run from here
    (the target repo is passed explicitly via ``--repo-root``).
    """
    return Path(__file__).resolve().parents[5]


def _load_codebench_catalog(repo_root: Path) -> list[dict[str, object]]:
    tasks_path = repo_root / "benchmarks" / "codebench" / "tasks.py"
    module_name = "_codebench_tasks"
    spec = importlib.util.spec_from_file_location(module_name, tasks_path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"Unable to load CodeBench task catalog: {tasks_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    tasks = getattr(module, "TASKS", None)
    if not isinstance(tasks, list):
        raise click.ClickException(f"Invalid CodeBench task catalog: {tasks_path}")
    catalog: list[dict[str, object]] = []
    for task in tasks:
        task_id = getattr(task, "id", None)
        language = getattr(task, "language", None)
        weight = getattr(task, "weight", None)
        task_dir = getattr(task, "task_dir", None)
        source = getattr(task, "source", None)
        if (
            not isinstance(task_id, str)
            or not isinstance(language, str)
            or not isinstance(weight, int)
            or not isinstance(task_dir, str)
        ):
            raise click.ClickException(f"Invalid CodeBench task metadata: {tasks_path}")
        catalog.append(
            {
                "id": task_id,
                "language": language,
                "weight": weight,
                "task_dir": task_dir,
                "source": list(source) if isinstance(source, tuple) else [],
            }
        )
    return catalog


def _run(cmd: list[str], *, cwd: Path, label: str, env: dict[str, str] | None = None, check: bool = True) -> int:
    click.echo("Running: " + _display_cmd(cmd))
    run_env = None
    if env is not None:
        run_env = dict(environ)
        run_env.update(env)
    completed = subprocess.run(cmd, check=False, cwd=cwd, env=run_env)
    if check and completed.returncode != 0:
        raise click.ClickException(f"{label} failed with exit {completed.returncode}")
    return completed.returncode


def _display_cmd(cmd: list[str]) -> str:
    if "-c" not in cmd:
        return " ".join(cmd)
    index = cmd.index("-c")
    compact = [*cmd[: index + 1], "<inline python>", *cmd[index + 2 :]]
    return " ".join(compact)
