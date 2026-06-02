"""Thin ``atelier benchmark`` command group."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from os import cpu_count, environ
from pathlib import Path
from shutil import rmtree, which

import click
import yaml

from atelier.core.capabilities.host_runners import (
    CLAUDE_PROVIDER_PRESETS,
    resolve_claude_provider_preset,
)
from atelier.gateway.cli.progress import ProgressReporter


@click.group("benchmark")
def benchmark_group() -> None:
    """Run Atelier benchmark suites and reports."""


@benchmark_group.command("mcp")
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option(
    "--jobs",
    type=int,
    default=0,
    show_default="auto",
    help="Parallel suite shards. Use 0 to auto-size.",
)
def benchmark_mcp_cmd(out: Path | None, jobs: int) -> None:
    """Run the public MCP tool benchmark suite and write results."""
    repo_root = Path.cwd().resolve()
    run_dir = _run_dir("mcp", out)
    workspace_dir = _workspace_dir("mcp", repo_root=repo_root, run_id=run_dir.name)
    resolved_jobs = _resolve_mcp_jobs(jobs, repo_root=repo_root)
    progress = ProgressReporter("mcp", total=1)
    progress.start("starting benchmark", current=f"reports {run_dir} | jobs {resolved_jobs}")
    _run(
        [
            *_python_cmd(repo_root),
            "-m",
            "benchmarks.mcp_tools.export_public_mcp_csv",
            "--artifact-root",
            str(workspace_dir),
            "--csv-out",
            str(run_dir / "results.csv"),
            "--jobs",
            str(resolved_jobs),
        ],
        cwd=repo_root,
        label="MCP benchmark",
    )
    progress.step("benchmark command complete", current="public MCP tools")
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("providers")
@click.option("--repo-root", type=click.Path(path_type=Path, file_okay=False), default=Path("."))
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Benchmark workspace/cache root. Defaults outside the repo under ../benchmarks/<repo>/.",
)
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--iterations", type=int, default=1, show_default=True)
@click.option("--max-cases", type=int, default=100, show_default=True)
@click.option(
    "--jobs",
    type=int,
    default=0,
    show_default="auto",
    help="Parallel provider processes. Use 0 to auto-size.",
)
@click.option(
    "--providers",
    default=(
        "atelier,atelier-zoekt,zoekt,atelier-serena,serena," "atelier-codegraph,codegraph,code-index-mcp,jcodemunch-mcp"
    ),
    show_default=True,
)
@click.option("--families", default="exact_search,substring_search,nohit_search", show_default=True)
@click.option("--install", is_flag=True, help="Install/check external provider tools first.")
def benchmark_providers_cmd(
    repo_root: Path,
    workspace_root: Path | None,
    out: Path | None,
    iterations: int,
    max_cases: int,
    jobs: int,
    providers: str,
    families: str,
    install: bool,
) -> None:
    """Run the external code-search provider matrix and write CSV/JSON artifacts."""
    repo_root = repo_root.resolve()
    run_dir = _run_dir("providers", out, repo_root=repo_root)
    workspace_root = (
        workspace_root.resolve()
        if workspace_root is not None
        else _workspace_dir("providers", repo_root=repo_root, run_id=run_dir.name)
    )
    cache_root = _cache_dir("providers", repo_root=repo_root)
    provider_list = _csv_values(providers)
    resolved_jobs = _resolve_provider_jobs(jobs, provider_list)
    csv_out = run_dir / "results.csv"
    json_out = run_dir / "results.json"
    progress = ProgressReporter("providers", total=1)
    progress.start("starting benchmark", current=f"reports {run_dir} | jobs {resolved_jobs}")
    cmd = [
        *_python_cmd(repo_root),
        "-m",
        "benchmarks.mcp_tools.bench_external_matrix",
        "--repo-root",
        str(repo_root),
        "--workspace-root",
        str(workspace_root),
        "--cache-root",
        str(cache_root),
        "--manifest-path",
        str(workspace_root / "external_matrix_cases.json"),
        "--audit-path",
        str(workspace_root / "external_tool_surfaces.json"),
        "--json-out",
        str(json_out),
        "--csv-out",
        str(csv_out),
        "--iterations",
        str(iterations),
        "--jobs",
        str(resolved_jobs),
        "--tools",
        providers,
        "--families",
        families,
    ]
    if max_cases > 0:
        cmd.extend(["--max-cases", str(max_cases)])
    if install:
        cmd.append("--install")
    _run(cmd, cwd=repo_root, label="provider benchmark")
    progress.step("benchmark command complete", current="external provider matrix")
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("terminalbench")
@click.option("--task", default="all", show_default=True, help="TerminalBench task id or 'all'.")
@click.option("--mode", default="all", show_default=True, type=click.Choice(["all", "on", "off"]))
@click.option("--model", default="claude-sonnet-4-5", show_default=True)
@click.option("--provider", default="claude", type=click.Choice(["claude", "ollama"]), show_default=True)
@click.option("--rep", type=int, default=1, show_default=True, help="Repetitions per task/arm.")
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
def benchmark_terminalbench_cmd(
    task: str,
    mode: str,
    model: str,
    provider: str,
    rep: int,
    out: Path,
) -> None:
    """Run TerminalBench tasks and write transcripts plus summary."""
    repo_root = Path.cwd().resolve()
    run_dir = _run_dir("terminalbench", out)
    tasks_path = repo_root / "benchmarks" / "terminalbench" / "tasks.yaml"
    known_tasks, dataset_meta = _load_terminalbench_catalog(tasks_path)
    task_ids = known_tasks if task == "all" else [task]
    unknown = [task_id for task_id in task_ids if task_id not in known_tasks]
    if unknown:
        raise click.ClickException(f"Unknown TerminalBench task(s): {', '.join(unknown)}")
    modes = ["on", "off"] if mode == "all" else [mode]
    total_trials = len(task_ids) * len(modes) * rep
    progress = ProgressReporter("terminalbench", total=total_trials + 1)
    progress.start(
        "starting benchmark",
        current=f"{len(task_ids)} tasks x {len(modes)} modes x {rep} rep(s)",
    )
    for task_id in task_ids:
        for mode_name in modes:
            for rep_index in range(1, rep + 1):
                _run(
                    [
                        *_python_cmd(repo_root),
                        "-m",
                        "benchmarks.terminalbench.runner",
                        "--task",
                        task_id,
                        "--mode",
                        mode_name,
                        "--model",
                        model,
                        "--provider",
                        provider,
                        "--rep",
                        str(rep_index),
                        "--dataset-name",
                        dataset_meta["name"],
                        "--dataset-version",
                        dataset_meta["version"],
                        "--out",
                        str(run_dir),
                    ],
                    cwd=repo_root,
                    label="TerminalBench trial",
                )
                progress.step("trial complete", current=f"{task_id} {mode_name} rep {rep_index}")
    _run(
        [
            *_python_cmd(repo_root),
            "-m",
            "benchmarks.terminalbench.aggregate",
            "--runs",
            str(run_dir / "runs.jsonl"),
            "--out",
            str(run_dir / "summary.json"),
        ],
        cwd=repo_root,
        label="TerminalBench summary",
    )
    progress.step("summary complete", current="summary.json")
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("swe")
@click.option(
    "--swebench-config",
    "--config",
    "swebench_config",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Path to mini-SWE-agent's swebench.yaml. Auto-detected if omitted.",
)
@click.option(
    "--baseline-input",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="mini-SWE-agent baseline output directory.",
)
@click.option(
    "--atelier-input",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="mini-SWE-agent Atelier output directory.",
)
@click.option("--savings-log", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.option(
    "--baseline-config",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("benchmarks/swe/configs/ollama_baseline.yaml"),
    show_default=True,
    help="Baseline mini-SWE-agent override config.",
)
@click.option(
    "--atelier-config",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("benchmarks/swe/configs/ollama_atelier.yaml"),
    show_default=True,
    help="Atelier mini-SWE-agent override config.",
)
@click.option("--subset", default="lite", show_default=True, help="SWE-bench subset.")
@click.option("--split", default="dev", show_default=True, help="SWE-bench split.")
@click.option("--slice", "slice_expr", default="0:5", show_default=True, help="Python slice of tasks.")
@click.option("--workers", type=int, default=1, show_default=True, help="mini-SWE-agent worker count.")
@click.option(
    "--proxy-upstream",
    default="http://localhost:11434/v1",
    show_default=True,
    help="OpenAI-compatible upstream for the Atelier proxy.",
)
@click.option("--proxy-port", type=int, default=11435, show_default=True)
@click.option("--run-id", default="atelier-eval", show_default=True)
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
def benchmark_swe_cmd(
    swebench_config: Path | None,
    baseline_input: Path | None,
    atelier_input: Path | None,
    savings_log: Path | None,
    baseline_config: Path,
    atelier_config: Path,
    subset: str,
    split: str,
    slice_expr: str,
    workers: int,
    proxy_upstream: str,
    proxy_port: int,
    run_id: str,
    out: Path | None,
) -> None:
    """Run the SWE benchmark and write predictions, metrics, and reports."""
    run_dir = _run_dir("swe", out)
    if baseline_input is None and atelier_input is None and savings_log is None:
        _run_swe_eval(
            swebench_config=swebench_config,
            baseline_config=baseline_config,
            atelier_config=atelier_config,
            subset=subset,
            split=split,
            slice_expr=slice_expr,
            workers=workers,
            proxy_upstream=proxy_upstream,
            proxy_port=proxy_port,
            run_id=run_id,
            run_dir=run_dir,
        )
        click.echo(f"Results: {run_dir}")
        return
    if baseline_input is None or atelier_input is None or savings_log is None:
        raise click.ClickException(
            "Provide all of --baseline-input, --atelier-input, and --savings-log, "
            "or provide none to run the default real mini-SWE-agent benchmark."
        )
    baseline_preds = run_dir / "baseline_preds.json"
    atelier_preds = run_dir / "atelier_preds.json"
    report_path = run_dir / "report.md"
    progress = ProgressReporter("swe", total=3)
    progress.start("starting benchmark", current=f"reports {run_dir}")
    _run(
        [
            *_python_cmd(Path.cwd()),
            "-m",
            "benchmarks.swe.make_preds",
            "--input",
            str(baseline_input),
            "--output",
            str(baseline_preds),
            "--run-id",
            f"{run_id}-baseline",
        ],
        cwd=Path.cwd(),
        label="SWE baseline preds export",
    )
    progress.step("baseline predictions complete", current=baseline_preds.name)
    _run(
        [
            *_python_cmd(Path.cwd()),
            "-m",
            "benchmarks.swe.make_preds",
            "--input",
            str(atelier_input),
            "--output",
            str(atelier_preds),
            "--run-id",
            f"{run_id}-atelier",
        ],
        cwd=Path.cwd(),
        label="SWE Atelier preds export",
    )
    progress.step("atelier predictions complete", current=atelier_preds.name)
    _run(
        [
            *_python_cmd(Path.cwd()),
            "-m",
            "benchmarks.swe.report",
            "--baseline",
            str(baseline_preds),
            "--atelier",
            str(atelier_preds),
            "--savings-log",
            str(savings_log),
            "--output",
            str(report_path),
        ],
        cwd=Path.cwd(),
        label="SWE report",
    )
    progress.step("report complete", current=report_path.name)
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


@benchmark_group.command("vix")
@click.option(
    "--task",
    "tasks",
    multiple=True,
    default=("all",),
    show_default=True,
    help="VIX task id; repeat for multiple or use 'all'.",
)
@click.option(
    "--arm",
    "arms",
    multiple=True,
    default=("baseline", "atelier"),
    show_default=True,
    type=click.Choice(["baseline", "atelier", "vix"]),
)
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--model", default="sonnet", show_default=True)
@click.option("--timeout", type=int, default=900, show_default=True)
@click.option("--transport", type=click.Choice(["cli", "api"]), default="cli", show_default=True)
@click.option(
    "--cli-driver",
    type=click.Choice(["claude", "copilot", "codex", "opencode"]),
    default="claude",
    show_default=True,
    help="CLI host to benchmark when --transport cli is used.",
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
@click.option(
    "--api-provider",
    type=click.Choice(["openai", "litellm", "ollama"]),
    default="ollama",
    show_default=True,
)
@click.option("--api-base-url", default=None, help="OpenAI-compatible base URL.")
@click.option("--api-key-env", default=None, help="Environment variable containing the API key.")
@click.option("--launch-ollama", is_flag=True, help="Start 'ollama serve' before API runs.")
@click.option("--judge", is_flag=True, help="Score correctness with an LLM judge.")
@click.option("--judge-transport", type=click.Choice(["cli", "api"]), default=None)
@click.option("--judge-provider", type=click.Choice(["openai", "litellm", "ollama"]), default=None)
@click.option("--judge-model", default=None)
@click.option("--judge-agent-command", default=None)
@click.option("--judge-api-base-url", default=None)
@click.option("--judge-api-key-env", default=None)
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
@click.option("--vix-eval-dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
def benchmark_vix_cmd(
    tasks: tuple[str, ...],
    arms: tuple[str, ...],
    reps: int,
    model: str,
    timeout: int,
    transport: str,
    cli_driver: str,
    jobs: int,
    parallel_scope: str,
    api_provider: str,
    api_base_url: str | None,
    api_key_env: str | None,
    launch_ollama: bool,
    judge: bool,
    judge_transport: str | None,
    judge_provider: str | None,
    judge_model: str | None,
    judge_agent_command: str | None,
    judge_api_base_url: str | None,
    judge_api_key_env: str | None,
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
    vix_eval_dir: Path | None,
    out: Path | None,
) -> None:
    """Run the VIX head-to-head benchmark and write a report."""
    repo_root = Path.cwd().resolve()
    run_dir = _run_dir("vix", out)
    resolved_vix_eval_dir = _ensure_vix_eval_dir(repo_root, vix_eval_dir)
    env = {"VIX_EVAL_DIR": str(resolved_vix_eval_dir)}
    bridge_args = []
    if bridge_command:
        bridge_args = ["--bridge-command", bridge_command, "--bridge-wait", str(bridge_wait)]
    api_args = ["--transport", transport, "--api-provider", api_provider]
    if api_base_url:
        api_args.extend(["--api-base-url", api_base_url])
    if api_key_env:
        api_args.extend(["--api-key-env", api_key_env])
    if launch_ollama:
        api_args.append("--launch-ollama")
    judge_args = []
    if judge:
        judge_args.append("--judge")
    if judge_transport:
        judge_args.extend(["--judge-transport", judge_transport])
    if judge_provider:
        judge_args.extend(["--judge-provider", judge_provider])
    if judge_model:
        judge_args.extend(["--judge-model", judge_model])
    if judge_agent_command:
        judge_args.extend(["--judge-agent-command", judge_agent_command])
    if judge_api_base_url:
        judge_args.extend(["--judge-api-base-url", judge_api_base_url])
    if judge_api_key_env:
        judge_args.extend(["--judge-api-key-env", judge_api_key_env])
    agent_env_args: list[str] = []
    if openrouter_claude:
        if transport != "cli" or cli_driver != "claude":
            raise click.ClickException("--openrouter-claude only applies to --transport cli --cli-driver claude.")
        claude_provider_preset = claude_provider_preset or "openrouter-claude"
    if (transport != "cli" or cli_driver != "claude") and (
        claude_provider_preset or claude_base_url or claude_auth_token_env or claude_api_key_env or clear_claude_api_key
    ):
        raise click.ClickException("Claude CLI provider env flags only apply to --transport cli --cli-driver claude.")
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
    forwarded_cli_extra_args = [f"--cli-extra-arg={arg}" for arg in cli_extra_args]
    progress = ProgressReporter("vix", total=1)
    progress.start("starting benchmark", current=f"{len(tasks)} task selector(s) x {len(arms)} arm(s)")
    _run(
        [
            *_python_cmd(repo_root),
            "-m",
            "benchmarks.vix_eval.run",
            "--tasks",
            *tasks,
            "--arms",
            *arms,
            "--reps",
            str(reps),
            "--model",
            model,
            "--timeout",
            str(timeout),
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
            *api_args,
            *judge_args,
            *bridge_args,
            "--out",
            str(run_dir),
        ],
        cwd=repo_root,
        label="VIX benchmark",
        env=env,
    )
    progress.step("benchmark command complete", current=run_dir.name)
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


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
    path = repo_root.resolve().parent / "benchmarks" / repo_root.name / suite / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_dir(suite: str, *, repo_root: Path) -> Path:
    path = repo_root.resolve().parent / "benchmarks" / repo_root.name / f"{suite}-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _auto_jobs(item_count: int, *, hard_cap: int) -> int:
    detected = max(cpu_count() or 1, 1)
    return max(1, min(item_count, hard_cap, detected))


def _resolve_mcp_jobs(requested_jobs: int, *, repo_root: Path) -> int:
    if requested_jobs > 0:
        return requested_jobs
    repo_root = repo_root.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from benchmarks.mcp_tools.export_public_mcp_csv import _select_suite_specs

    return _auto_jobs(len(_select_suite_specs(None)), hard_cap=32)


def _resolve_provider_jobs(requested_jobs: int, providers: list[str]) -> int:
    if requested_jobs > 0:
        return requested_jobs
    return _auto_jobs(len(providers), hard_cap=32)


def _ensure_vix_eval_dir(repo_root: Path, configured_dir: Path | None) -> Path:
    resolved = (
        configured_dir.resolve()
        if configured_dir is not None
        else repo_root.parent / "benchmarks" / repo_root.name / "vix-eval"
    )
    tasks_dir = resolved / "tasks"
    if tasks_dir.is_dir():
        return resolved
    if resolved.exists() and not tasks_dir.is_dir():
        raise click.ClickException(f"VIX benchmark tasks directory not found: {tasks_dir}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "https://github.com/kirby88/vix-eval", str(resolved)],
        check=False,
        cwd=resolved.parent,
        capture_output=True,
        text=True,
    )
    if clone.returncode != 0:
        raise click.ClickException(
            "Failed to clone vix-eval benchmark data. "
            f"Pass --vix-eval-dir explicitly or fix git/network access.\n{clone.stderr.strip()}"
        )
    if not tasks_dir.is_dir():
        raise click.ClickException(f"VIX benchmark tasks directory not found after clone: {tasks_dir}")
    return resolved


def _python_cmd(repo_root: Path) -> list[str]:
    repo_root = repo_root.resolve()
    if which("uv") and (repo_root / "pyproject.toml").is_file():
        return ["uv", "run", "--project", str(repo_root), "python"]
    return [sys.executable]


def _resolve_repo_path(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _load_terminalbench_catalog(tasks_path: Path) -> tuple[list[str], dict[str, str]]:
    payload = yaml.safe_load(tasks_path.read_text(encoding="utf-8")) or {}
    dataset = payload.get("dataset")
    tasks = payload.get("tasks")
    if not isinstance(dataset, dict) or not isinstance(tasks, list):
        raise click.ClickException(f"Invalid TerminalBench catalog: {tasks_path}")
    name = dataset.get("name")
    version = dataset.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise click.ClickException(f"Invalid TerminalBench dataset metadata: {tasks_path}")
    task_ids = [task_id for task_id in tasks if isinstance(task_id, str)]
    if len(task_ids) != len(tasks):
        raise click.ClickException(f"Invalid TerminalBench task list: {tasks_path}")
    return task_ids, {"name": name, "version": version}


def _wait_for_http(url: str, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 500:
                    return True
        except (TimeoutError, OSError, urllib.error.URLError):
            time.sleep(0.5)
    return False


def _resolve_miniswebench_config_path(repo_root: Path, configured_path: Path | None) -> Path:
    if configured_path is not None:
        resolved = _resolve_repo_path(repo_root, configured_path)
        if not resolved.is_file():
            raise click.ClickException(f"mini-SWE-agent config not found: {resolved}")
        return resolved
    spec = importlib.util.find_spec("minisweagent")
    if spec is None:
        raise click.ClickException(
            "mini-SWE-agent is not installed in this environment. "
            "Install it in the uv environment, then rerun `atelier benchmark swe`."
        )
    search_roots: list[Path] = []
    if spec.submodule_search_locations is not None:
        search_roots.extend(Path(root).resolve() for root in spec.submodule_search_locations)
    if spec.origin:
        search_roots.append(Path(spec.origin).resolve().parent)
    for root in search_roots:
        candidate = root / "config" / "benchmarks" / "swebench.yaml"
        if candidate.is_file():
            return candidate
    roots_text = ", ".join(str(root) for root in search_roots) or "<unknown>"
    raise click.ClickException(
        "Could not find mini-SWE-agent's swebench.yaml under: " f"{roots_text}. Pass --swebench-config explicitly."
    )


def _run_swe_eval(
    *,
    swebench_config: Path | None,
    baseline_config: Path,
    atelier_config: Path,
    subset: str,
    split: str,
    slice_expr: str,
    workers: int,
    proxy_upstream: str,
    proxy_port: int,
    run_id: str,
    run_dir: Path,
) -> None:
    repo_root = Path.cwd().resolve()
    baseline_config = _resolve_repo_path(repo_root, baseline_config)
    atelier_config = _resolve_repo_path(repo_root, atelier_config)
    swebench_config = _resolve_miniswebench_config_path(repo_root, swebench_config)
    for path in (baseline_config, atelier_config, swebench_config):
        if not path.is_file():
            raise click.ClickException(f"Required SWE benchmark config not found: {path}")
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        raise click.ClickException(
            "SWE benchmark proxy dependencies are missing. Install fastapi and uvicorn in the uv environment."
        )
    if which("docker") is None:
        raise click.ClickException("Docker is required for the real mini-SWE-agent benchmark.")
    upstream_models_url = f"{proxy_upstream.rstrip('/')}/models"
    if not _wait_for_http(upstream_models_url, timeout_seconds=5):
        raise click.ClickException(f"Upstream model endpoint is not reachable: {upstream_models_url}")

    baseline_dir = run_dir / "baseline"
    atelier_dir = run_dir / "atelier"
    baseline_preds = run_dir / "baseline_preds.json"
    atelier_preds = run_dir / "atelier_preds.json"
    proxy_log = run_dir / "proxy_savings.jsonl"
    report_path = run_dir / "report.md"
    for path in (baseline_dir, atelier_dir):
        if path.exists():
            rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    if proxy_log.exists():
        proxy_log.unlink()

    progress = ProgressReporter("swe", total=5)
    progress.start("starting mini-SWE benchmark", current=f"{subset}/{split} {slice_expr}")
    benchmark_base_cmd = [
        *_python_cmd(repo_root),
        "-m",
        "minisweagent.run.benchmarks.swebench",
        "-c",
        str(swebench_config),
        "--subset",
        subset,
        "--split",
        split,
        "--slice",
        slice_expr,
        "--workers",
        str(workers),
    ]
    _run(
        [*benchmark_base_cmd, "-c", str(baseline_config), "--output", str(baseline_dir)],
        cwd=repo_root,
        label="SWE baseline run",
    )
    progress.step("baseline run complete", current=baseline_dir.name)

    proxy_cmd = [
        *_python_cmd(repo_root),
        "benchmarks/swe/atelier_proxy.py",
        "--upstream",
        proxy_upstream,
        "--port",
        str(proxy_port),
        "--log",
        str(proxy_log),
    ]
    click.echo("Running: " + _display_cmd(proxy_cmd))
    proxy_env = dict(environ)
    proxy_process = subprocess.Popen(proxy_cmd, cwd=repo_root, env=proxy_env)
    try:
        proxy_models_url = f"http://localhost:{proxy_port}/v1/models"
        if not _wait_for_http(proxy_models_url, timeout_seconds=15):
            raise click.ClickException(f"Atelier proxy did not become ready at {proxy_models_url}")
        progress.step("proxy ready", current=f"localhost:{proxy_port}")
        _run(
            [*benchmark_base_cmd, "-c", str(atelier_config), "--output", str(atelier_dir)],
            cwd=repo_root,
            label="SWE Atelier run",
        )
        progress.step("atelier run complete", current=atelier_dir.name)
    finally:
        if proxy_process.poll() is None:
            proxy_process.terminate()
            try:
                proxy_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy_process.kill()
                proxy_process.wait(timeout=5)

    _run(
        [
            *_python_cmd(repo_root),
            "benchmarks/swe/make_preds.py",
            "--input",
            str(baseline_dir),
            "--output",
            str(baseline_preds),
            "--run-id",
            f"{run_id}-baseline",
        ],
        cwd=repo_root,
        label="SWE baseline preds export",
    )
    _run(
        [
            *_python_cmd(repo_root),
            "benchmarks/swe/make_preds.py",
            "--input",
            str(atelier_dir),
            "--output",
            str(atelier_preds),
            "--run-id",
            f"{run_id}-atelier",
        ],
        cwd=repo_root,
        label="SWE Atelier preds export",
    )
    progress.step("predictions exported", current=atelier_preds.name)
    _run(
        [
            *_python_cmd(repo_root),
            "benchmarks/swe/report.py",
            "--baseline",
            str(baseline_preds),
            "--atelier",
            str(atelier_preds),
            "--savings-log",
            str(proxy_log),
            "--output",
            str(report_path),
        ],
        cwd=repo_root,
        label="SWE report",
    )
    progress.step("report complete", current=report_path.name)
    progress.finish("benchmark complete")


def _run(cmd: list[str], *, cwd: Path, label: str, env: dict[str, str] | None = None) -> None:
    click.echo("Running: " + _display_cmd(cmd))
    run_env = None
    if env is not None:
        run_env = dict(environ)
        run_env.update(env)
    completed = subprocess.run(cmd, check=False, cwd=cwd, env=run_env)
    if completed.returncode != 0:
        raise click.ClickException(f"{label} failed with exit {completed.returncode}")


def _display_cmd(cmd: list[str]) -> str:
    if "-c" not in cmd:
        return " ".join(cmd)
    index = cmd.index("-c")
    compact = [*cmd[: index + 1], "<inline python>", *cmd[index + 2 :]]
    return " ".join(compact)
