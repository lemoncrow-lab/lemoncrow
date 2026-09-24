"""Tests for the benchmark CLI subcommand workflow."""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import benchmark as benchmark_cmds

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_legacy_top_level_commands_are_removed(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"

    assert runner.invoke(cli, ["--root", str(root), "benchmark-core", "--json"]).exit_code != 0
    assert runner.invoke(cli, ["--root", str(root), "benchmark", "--prompt", "Fix PDP", "--json"]).exit_code != 0


def test_help_command_shows_root_command_help(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"

    root_help = runner.invoke(cli, ["--root", str(root), "help"])
    assert root_help.exit_code == 0, root_help.output
    assert "Commands:" in root_help.output
    assert "benchmark" in root_help.output


def test_benchmark_gate_command_reads_gate_and_optionally_fails(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    run_dir = tmp_path / "terminalbench"
    run_dir.mkdir()
    (run_dir / "benchmark-gate.json").write_text(
        json.dumps({"suite": "terminalbench", "passed": False, "reasons": ["candidate cost was higher"]}),
        encoding="utf-8",
    )

    ok = runner.invoke(cli, ["--root", str(root), "benchmark", "gate", "--run-dir", str(run_dir), "--json"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["suite"] == "terminalbench"

    failed = runner.invoke(
        cli,
        ["--root", str(root), "benchmark", "gate", "--run-dir", str(run_dir), "--require-pass"],
    )
    assert failed.exit_code != 0
    assert "candidate cost was higher" in failed.output


def test_benchmark_harbor_resume_uses_benchmarks_uv_project(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    resume_dir = tmp_path / "harbor-job"
    resume_dir.mkdir()
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def _fake_call(cmd, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(subprocess, "call", _fake_call)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "lemoncrow",
            "--resume",
            str(resume_dir),
            "--attempts",
            "1",
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, env = calls[0]
    assert cmd[:5] == ["uv", "run", "--project", str(REPO_ROOT / "benchmarks"), "--no-sync"]
    assert cmd[5:] == ["harbor", "job", "resume", "-p", str(resume_dir), "-y"]
    assert env is not None
    assert env["PYTHONPATH"].split(":")[0] == str(REPO_ROOT)


def test_benchmark_harbor_fresh_run_maps_attempts_to_n_attempts(monkeypatch, tmp_path: Path) -> None:
    """--attempts must map to harbor's -k/--n-attempts; -n is concurrency, not attempts.

    Regression: the command builder previously hardcoded ``-k 1`` and passed
    attempts to ``-n``, silently running one attempt per task.
    """
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    out_dir = tmp_path / "harbor-out"
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def _fake_call(cmd, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(subprocess, "call", _fake_call)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "lemoncrow",
            "--attempts",
            "5",
            "--concurrent",
            "8",
            "--output",
            str(out_dir),
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, env = calls[0]
    assert "run" in cmd
    assert env is not None
    assert env["LEMONCROW_BENCH_COMMIT"]
    # attempts -> -k/--n-attempts, concurrency -> --n-concurrent (distinct values).
    assert cmd[cmd.index("-k") + 1] == "5"
    assert cmd[cmd.index("--n-concurrent") + 1] == "8"
    # harbor's -n is concurrency: it must never carry the attempts value.
    assert "-n" not in cmd


def test_benchmark_harbor_context_arm_is_recorded_in_agent_kwargs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    out_dir = tmp_path / "harbor-out"
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth-token")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def _fake_call(cmd, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(subprocess, "call", _fake_call)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "lemoncrow-claude-code",
            "--context-arm",
            "lemoncrow-headroom",
            "--no-rebuild-bundle",
            "--bundle",
            str(bundle),
            "--attempts",
            "1",
            "--concurrent",
            "1",
            "--output",
            str(out_dir),
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, _ = calls[0]
    pairs = list(itertools.pairwise(cmd))
    assert ("--ak", "context_arm=lemoncrow-headroom") in pairs
    assert "arm              : lemoncrow-headroom" in result.output


def test_benchmark_harbor_baseline_alias_maps_to_raw_context_arm(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    calls: list[list[str]] = []

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth-token")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(subprocess, "call", lambda cmd, env=None: calls.append(cmd) or 0)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "lemoncrow-claude-code",
            "--baseline",
            "--no-rebuild-bundle",
            "--bundle",
            str(bundle),
            "--attempts",
            "1",
            "--concurrent",
            "1",
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    pairs = list(itertools.pairwise(calls[0]))
    assert ("--ak", "context_arm=raw") in pairs
    assert "bench_mode=off" not in calls[0]


def test_benchmark_harbor_rejects_context_arm_for_non_claude_agent(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "harbor",
            "--agent",
            "lemoncrow",
            "--context-arm",
            "rtk",
            "-y",
        ],
    )

    assert result.exit_code != 0
    assert "--context-arm is only supported" in result.output


def test_benchmark_codebench_wraps_runner(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert cmd[:3] == ["python", "-m", "benchmarks.codebench.run"]
    assert cmd[3] == "all"
    assert "--tasks" not in cmd
    assert "--arms" in cmd
    assert cmd[cmd.index("--cli-driver") + 1] == "claude"
    assert cmd[cmd.index("--timeout") + 1] == "1800"
    assert "--max-output-tokens" not in cmd
    assert cmd[cmd.index("--rate-limit-rpm") + 1] == "0.0"
    assert cmd[cmd.index("--rate-limit-tpm") + 1] == "0"
    assert cmd[cmd.index("--jobs") + 1] == "1"
    assert cmd[cmd.index("--parallel-scope") + 1] == "task"
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}
    manifest = json.loads((tmp_path / "codebench" / "benchmark-manifest.json").read_text("utf-8"))
    assert manifest["suite"] == "codebench"
    assert manifest["protocol"]["baseline_arm"] == "baseline"
    assert manifest["protocol"]["arm_agents"] == {
        "lemoncrow": "lemoncrow:code",
        "baseline": "host-default",
    }
    assert manifest["corpus"]["tasks"][0]["id"] == "cg_vscode"
    assert manifest["artifacts"]["model_audit_csv"] == "model_audit.csv"
    assert manifest["artifacts"]["task_correctness_csv"] == "task_correctness.csv"
    assert manifest["artifacts"]["pairwise_quality_csv"] == "pairwise_quality.csv"
    evidence = json.loads((tmp_path / "codebench" / "benchmark-evidence.json").read_text("utf-8"))
    assert evidence["suite"] == "codebench"
    assert evidence["artifacts"]["results_jsonl"]["path"].endswith("results.jsonl")
    assert evidence["artifacts"]["quality_adjusted_summary_csv"]["path"].endswith("quality_adjusted_summary.csv")
    gate = json.loads((tmp_path / "codebench" / "benchmark-gate.json").read_text("utf-8"))
    assert gate["suite"] == "codebench"
    assert gate["passed"] is False
    comparison = json.loads((tmp_path / "codebench" / "benchmark-comparison-gates.json").read_text("utf-8"))
    assert comparison["baseline_arm"] == "baseline"
    assert comparison["candidate_arms"] == ["lemoncrow"]


def test_benchmark_codebench_accepts_eval_arm_and_api_options(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--arm",
            "baseline",
            "--arm",
            "lemoncrow",
            "--model",
            "llama3.2",
            "--bridge-wait",
            "0",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert cmd[cmd.index("--arms") + 1 : cmd.index("--reps")] == ["baseline", "lemoncrow"]
    assert cmd[cmd.index("--model") + 1] == "llama3.2"
    assert "--transport" not in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_judge_defaults_to_runner_transport(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "claude-sonnet-4-6",
            "--judge",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, _label, _env = calls[0]
    assert "--judge" in cmd
    assert "--judge-provider" not in cmd
    assert "--judge-model" not in cmd
    assert "--judge-transport" not in cmd
    assert "--transport" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_benchmark_codebench_openrouter_claude_preset_passes_agent_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "openrouter/owl-alpha",
            "--openrouter-claude",
            "--openrouter-key-env",
            "OPENROUTER_API_KEY",
            "--agent-env",
            "EXTRA_FLAG=1",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert "--agent-env" in cmd
    assert "EXTRA_FLAG=1" in cmd
    assert "ANTHROPIC_BASE_URL=https://openrouter.ai/api" in cmd
    assert "ANTHROPIC_API_KEY=" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY" in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_generic_claude_provider_flags_pass_through(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--model",
            "provider/model-x",
            "--claude-base-url",
            "https://provider.example/api",
            "--claude-auth-token-env",
            "PROVIDER_AUTH",
            "--claude-api-key-env",
            "PROVIDER_API_KEY",
            "--clear-claude-api-key",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, env = calls[0]
    assert label == "CodeBench"
    assert "ANTHROPIC_BASE_URL=https://provider.example/api" in cmd
    assert "ANTHROPIC_AUTH_TOKEN=PROVIDER_AUTH" in cmd
    assert "ANTHROPIC_API_KEY=PROVIDER_API_KEY" in cmd
    assert env == {"CODEBENCH_TASKS_DIR": str(codebench_tasks_dir.resolve())}


def test_benchmark_codebench_forwards_cli_driver_and_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--cli-driver",
            "codex",
            "--jobs",
            "3",
            "--parallel-scope",
            "arm",
            "--cli-extra-arg",
            "-c",
            "--cli-extra-arg",
            'model_reasoning_effort="medium"',
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "CodeBench"
    assert cmd[cmd.index("--cli-driver") + 1] == "codex"
    assert cmd[cmd.index("--jobs") + 1] == "3"
    assert cmd[cmd.index("--parallel-scope") + 1] == "arm"
    assert "--cli-extra-arg=-c" in cmd
    assert '--cli-extra-arg=model_reasoning_effort="medium"' in cmd


def test_benchmark_codebench_named_aws_claude_preset_passes_env(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        # Model the real _run contract: it now returns the subprocess exit code
        # and accepts check=. A 0 here represents a successful runner subprocess.
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--cli-driver",
            "claude",
            "--claude-provider-preset",
            "aws-claude",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, label, _env = calls[0]
    assert label == "CodeBench"
    assert "CLAUDE_CODE_USE_BEDROCK=1" in cmd
    assert "AWS_REGION=AWS_REGION" in cmd


def test_benchmark_codebench_rejects_claude_flags_for_non_claude_driver(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--cli-driver",
            "copilot",
            "--openrouter-claude",
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code != 0
    assert "openrouter-claude only supports CLI drivers: claude" in result.output


def test_benchmark_mcp_defaults_jobs_to_auto(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_resolve_mcp_jobs", lambda jobs, repo_root, suite_names=None: 6)
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "eval", "mcp"])

    assert result.exit_code == 0, result.output
    cmd, _label, _env = calls[0]
    assert cmd[cmd.index("--jobs") + 1] == "6"


def test_benchmark_mcp_passes_parallel_jobs(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None: calls.append((cmd, label, env)),
    )

    result = runner.invoke(cli, ["--root", str(root), "eval", "mcp", "--jobs", "3"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label, _env = calls[0]
    assert label == "MCP benchmark"
    assert "--jobs" in cmd
    assert cmd[cmd.index("--jobs") + 1] == "3"


def test_benchmark_auto_jobs_uses_full_cpu_up_to_cap(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_cmds, "cpu_count", lambda: 16)

    assert benchmark_cmds._auto_jobs(20, hard_cap=32) == 16

    monkeypatch.setattr(benchmark_cmds, "cpu_count", lambda: 64)
    assert benchmark_cmds._auto_jobs(40, hard_cap=32) == 32


def test_benchmark_swe_wraps_multiswe_runner(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append((cmd, label)) or 0,
    )

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "swe",
            "--language",
            "go",
            "--language",
            "rust",
            "--per-language-limit",
            "5",
            "--jobs",
            "2",
            "--no-grade",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd, label = calls[0]
    assert label == "benchmark swe"
    assert cmd[:3] == ["python", "-m", "benchmarks.codebench.multiswe_run"]
    assert cmd[cmd.index("--arms") + 1 : cmd.index("--arms") + 3] == ["baseline", "lemoncrow"]
    assert cmd[cmd.index("--languages") + 1 : cmd.index("--languages") + 3] == ["go", "rust"]
    assert cmd[cmd.index("--per-language-limit") + 1] == "5"
    assert cmd[cmd.index("--jobs") + 1] == "2"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert "--no-grade" in cmd
    assert cmd[cmd.index("--out") + 1] == str(tmp_path / "swe")


def test_benchmark_swe_defaults_to_grading(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--limit", "1"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert "--no-grade" not in cmd
    assert cmd[cmd.index("--limit") + 1] == "1"
    assert cmd[cmd.index("--grade-workers") + 1] == "4"


def test_benchmark_swe_forwards_suite(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(
        cli,
        ["--root", str(root), "benchmark", "swe", "--suite", "swe-bench-verified", "--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][calls[0].index("--suite") + 1] == "swe-bench-verified"

    calls.clear()
    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert calls[0][calls[0].index("--suite") + 1] == "multi-swe-bench"


def test_benchmark_swe_accepts_swe_lite_suite(monkeypatch, tmp_path: Path) -> None:
    """``--suite swe-lite`` is a valid choice, forwarded as-is with no CLI-side
    dataset/instance defaulting -- the subprocess (multiswe_run.py) fills in the
    pinned SWE-bench Lite defaults itself when --dataset/--instance are absent.
    """
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "swe-lite"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert cmd[cmd.index("--suite") + 1] == "swe-lite"
    assert "--dataset" not in cmd
    assert "--instances" not in cmd

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "bogus-suite"])
    assert result.exit_code != 0


def test_benchmark_swe_accepts_swe_pro_suite(monkeypatch, tmp_path: Path) -> None:
    """``--suite swe-pro`` is a valid choice, forwarded as-is with no CLI-side
    dataset/instance defaulting -- the subprocess (multiswe_run.py) fills in the
    pinned SWE-bench Pro defaults itself when --dataset/--instance are absent.
    """
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[list[str]] = []

    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _project: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_run_dir", lambda suite, out, repo_root=None: tmp_path / suite)
    monkeypatch.setattr(
        benchmark_cmds,
        "_run",
        lambda cmd, cwd, label, env=None, check=True: calls.append(cmd) or 0,
    )

    result = runner.invoke(cli, ["--root", str(root), "benchmark", "swe", "--suite", "swe-pro"])

    assert result.exit_code == 0, result.output
    cmd = calls[0]
    assert cmd[cmd.index("--suite") + 1] == "swe-pro"
    assert "--dataset" not in cmd
    assert "--instances" not in cmd


def test_benchmark_codebench_forwards_external_comparator(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    calls: list[tuple[list[str], str, dict[str, str] | None]] = []
    codebench_tasks_dir = tmp_path / "codebench-tasks"
    codebench_tasks_dir.mkdir()
    competitor = tmp_path / "other-tool.json"
    competitor.write_text(
        json.dumps(
            {
                "name": "other-tool",
                "repo": "https://example.invalid/other-tool.git",
                "ref": "v1",
                "mcp": {"command": "other-tool", "args": ["serve"]},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(benchmark_cmds, "_python_cmd", lambda _repo_root: ["python"])
    monkeypatch.setattr(benchmark_cmds, "_codebench_run_dir", lambda repo_root: tmp_path / "codebench")
    monkeypatch.setattr(
        benchmark_cmds,
        "_ensure_codebench_tasks_dir",
        lambda repo_root, path: codebench_tasks_dir,
    )

    def _fake_run(cmd, cwd, label, env=None, check=True):
        calls.append((cmd, label, env))
        return 0

    monkeypatch.setattr(benchmark_cmds, "_run", _fake_run)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "codebench",
            "--competitor",
            str(competitor),
            "--task-source-dir",
            str(codebench_tasks_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    cmd, _, _ = calls[0]
    assert cmd[cmd.index("--competitor") + 1] == str(competitor.resolve())
    manifest = json.loads((tmp_path / "codebench" / "benchmark-manifest.json").read_text("utf-8"))
    assert manifest["protocol"]["treatment_arms"] == ["lemoncrow", "other-tool"]
    external = manifest["runtime_attribution"]["arms"]["other-tool"]
    assert external["role"] == "external"
    assert external["manifest_fingerprint"]
    assert external["manifest_sha256"]
    assert (tmp_path / "codebench" / "benchmark-comparison-gates.json").exists()


def test_benchmark_protocol_validates_frozen_runtime_matrix_without_running_agents(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    protocol = REPO_ROOT / "benchmarks" / "codebench" / "protocols" / "intelligent-runtime-v1.json"

    monkeypatch.chdir(REPO_ROOT)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("protocol validation must not launch benchmark subprocesses")

    monkeypatch.setattr(benchmark_cmds, "_run", _must_not_run)
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "protocol",
            "--file",
            str(protocol),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "intelligent-runtime-v1"
    assert payload["total_agent_rows"] == 308
    assert payload["pairwise_judge_comparisons"] == 273
    assert len(payload["fingerprint"]) == 64
    assert [run["id"] for run in payload["runs"]] == [
        "claude-policy-qualification",
        "codex-policy-generalization",
        "claude-codegraph-external",
    ]
    assert payload["runs"][2]["competitors"][0]["name"] == "codegraph"
    assert len(payload["commands"]) == 3


def test_benchmark_protocol_output_root_and_verify_all_write_publication_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    bundle = tmp_path / "bundle"
    protocol = REPO_ROOT / "benchmarks" / "codebench" / "protocols" / "intelligent-runtime-v1.json"
    run_ids = [
        "claude-policy-qualification",
        "codex-policy-generalization",
        "claude-codegraph-external",
    ]
    for run_id in run_ids:
        (bundle / run_id).mkdir(parents=True)

    monkeypatch.chdir(REPO_ROOT)
    fingerprint = "f" * 64
    publication = {
        "protocol_id": "intelligent-runtime-v1",
        "protocol_fingerprint": fingerprint,
        "run_root": str(bundle.resolve()),
        "passed": True,
        "reasons": [],
        "host_preflight": {"passed": True, "reasons": [], "checks": [], "current_versions": {}},
        "runs": {
            run_id: {
                "protocol_id": "intelligent-runtime-v1",
                "protocol_fingerprint": fingerprint,
                "run_id": run_id,
                "passed": True,
                "reasons": [],
                "details": {"commit_under_test": {"commit": "same", "dirty": False}},
            }
            for run_id in run_ids
        },
        "commit_under_test": "same",
    }
    monkeypatch.setattr(benchmark_cmds, "verify_codebench_publication", lambda *args, **kwargs: publication)

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "protocol",
            "--file",
            str(protocol),
            "--output-root",
            str(bundle),
            "--verify-all",
            str(bundle),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["publication_verification"]["passed"] is True
    for command in payload["commands"]:
        argv = command["argv"]
        assert argv[argv.index("--out") + 1] == str((bundle / command["run_id"]).resolve())
    publication_path = bundle / "publication-readiness.json"
    assert publication_path.exists()
    assert json.loads(publication_path.read_text(encoding="utf-8"))["passed"] is True
    for run_id in run_ids:
        assert (bundle / run_id / "protocol-verification.json").exists()


def test_benchmark_protocol_verify_all_incomplete_bundle_writes_failed_readiness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    protocol = REPO_ROOT / "benchmarks" / "codebench" / "protocols" / "intelligent-runtime-v1.json"

    monkeypatch.chdir(REPO_ROOT)
    versions = {
        "claude": "2.1.197 (Claude Code)",
        "codex": "codex-cli 0.155.1",
    }
    monkeypatch.setattr(benchmark_cmds, "_benchmark_command_version", lambda command: versions.get(command, ""))

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "protocol",
            "--file",
            str(protocol),
            "--verify-all",
            str(bundle),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["publication_verification"]["passed"] is False
    assert (bundle / "publication-readiness.json").exists()
    for run_id in (
        "claude-policy-qualification",
        "codex-policy-generalization",
        "claude-codegraph-external",
    ):
        assert (bundle / run_id / "protocol-verification.json").exists()


def test_benchmark_protocol_host_preflight_matches_frozen_versions(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    protocol = REPO_ROOT / "benchmarks" / "codebench" / "protocols" / "intelligent-runtime-v1.json"

    monkeypatch.chdir(REPO_ROOT)
    versions = {
        "claude": "2.1.197 (Claude Code)",
        "codex": "codex-cli 0.155.1",
    }
    monkeypatch.setattr(benchmark_cmds, "_benchmark_command_version", lambda command: versions.get(command, ""))

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "protocol",
            "--file",
            str(protocol),
            "--check-hosts",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["host_preflight"]["passed"] is True
    assert payload["runs"][1]["model"] == "gpt-6-astra"


def test_benchmark_protocol_json_verify_run_exits_nonzero_on_failed_publication_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    protocol = REPO_ROOT / "benchmarks" / "codebench" / "protocols" / "intelligent-runtime-v1.json"
    empty_run = tmp_path / "incomplete-run"
    empty_run.mkdir()

    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "protocol",
            "--file",
            str(protocol),
            "--verify-run",
            f"claude-policy-qualification={empty_run}",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verifications"][0]["passed"] is False
    assert any("missing benchmark manifest" in reason for reason in payload["verifications"][0]["reasons"])
    verification_path = empty_run / "protocol-verification.json"
    assert verification_path.exists()
    written = json.loads(verification_path.read_text(encoding="utf-8"))
    assert written["passed"] is False
