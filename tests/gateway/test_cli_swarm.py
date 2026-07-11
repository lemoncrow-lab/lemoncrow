from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.core.capabilities.swarm.capability import (
    _expand_command_tokens,
    build_child_env,
    launch_swarm_children,
    read_swarm_child_activity,
    resolve_swarm_child_command,
    resolve_swarm_provider_command,
    save_swarm_state,
)
from lemoncrow.core.capabilities.swarm.models import (
    SwarmAcceptedCommit,
    SwarmArtifactRef,
    SwarmChildState,
    SwarmRunState,
    SwarmWaveState,
)
from lemoncrow.gateway.cli.commands.swarm import swarm_group


def test_swarm_start_requires_child_command(tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# spec\n", encoding="utf-8")
    result = runner.invoke(swarm_group, ["start", str(spec)], obj={"root": tmp_path})
    assert result.exit_code != 0


def test_swarm_start_defaults_to_program_md(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# default spec\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    state = SwarmRunState(
        run_id="swarm-123",
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(spec),
        copied_spec_path=str(spec),
        runner_name="custom",
        child_command=["echo", "hi"],
        runs=1,
        max_runs=1,
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.initialize_swarm_run",
        lambda **kwargs: (captured.update(kwargs) or state, tmp_path / "state.json"),
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.launch_swarm_children",
        lambda _root, _state: state,
    )

    result = runner.invoke(
        swarm_group,
        ["start", "--runner", "claude"],
        obj={"root": tmp_path / "lemoncrow-root"},
    )

    assert result.exit_code == 0
    assert captured["spec_path"] == spec
    assert captured["spec_resolution"] == "default"
    assert captured["used_program_md"] is True


def test_swarm_start_missing_default_program_md_fails(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)

    result = runner.invoke(
        swarm_group,
        ["start", "--runner", "claude"],
        obj={"root": tmp_path / "lemoncrow-root"},
    )

    assert result.exit_code != 0
    assert "default swarm spec not found" in result.output


def test_swarm_start_rejects_spec_outside_repo(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    outside = tmp_path.parent / "outside-program.md"
    outside.write_text("# spec\n", encoding="utf-8")
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)

    result = runner.invoke(
        swarm_group,
        ["start", str(outside), "--", "echo", "hi"],
        obj={"root": tmp_path / "lemoncrow-root"},
    )

    assert result.exit_code != 0
    assert "must stay under the selected project root" in result.output


def test_swarm_start_reports_winner(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# spec\n", encoding="utf-8")
    root = tmp_path / "lemoncrow-root"
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    state = SwarmRunState(
        run_id="swarm-123",
        status="success",
        mode="continuous",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        base_snapshot_ref="base-snapshot",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(spec),
        copied_spec_path=str(spec),
        runner_name="claude",
        runner_model="sonnet",
        child_command=["echo", "hi"],
        runs=1,
        max_runs=3,
        current_wave=2,
        winner_child_id="wave-02-run-01",
        primary_winner_child_id="wave-02-run-01",
        accepted_child_ids=["wave-01-run-01", "wave-02-run-01"],
        waves=[
            SwarmWaveState(
                wave_index=2,
                max_runs=3,
                planned_runs=2,
                planning_mode="bounded",
                child_ids=["wave-02-run-01"],
                accepted_child_ids=["wave-02-run-01"],
                primary_winner_child_id="wave-02-run-01",
            )
        ],
        children=[
            SwarmChildState(
                child_id="wave-02-run-01",
                label="candidate-1",
                wave_index=2,
                status="success",
                worktree_path=str(tmp_path / "pool" / "wave-02-run-01"),
                lemoncrow_root=str(root / "child"),
                run_dir=str(root / "runs" / "run-01"),
                spec_path=str(spec),
                result_path=str(root / "runs" / "run-01" / "result.json"),
                stdout_path=str(root / "runs" / "run-01" / "stdout.log"),
                stderr_path=str(root / "runs" / "run-01" / "stderr.log"),
                metadata_path=str(root / "runs" / "run-01" / "meta.json"),
                summary="best candidate",
                score=110.0,
                accepted=True,
            )
        ],
    )

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.initialize_swarm_run",
        lambda **_: (state, tmp_path / "state.json"),
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.launch_swarm_children",
        lambda _root, _state: state,
    )

    result = runner.invoke(
        swarm_group,
        [
            "start",
            str(spec),
            "--continuous",
            "--",
            "echo",
            "hi",
        ],
        obj={"root": root},
    )

    assert result.exit_code == 0
    assert "wave-02-run-01" in result.output
    assert "mode:" in result.output
    assert "2/3" in result.output


def test_swarm_start_accepts_runner_profile(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# spec\n", encoding="utf-8")
    root = tmp_path / "lemoncrow-root"
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    captured: dict[str, object] = {}
    state = SwarmRunState(
        run_id="swarm-123",
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(spec),
        copied_spec_path=str(spec),
        runner_name="claude",
        runner_model="sonnet",
        child_command=["claude", "--print", "stub"],
        runs=1,
        max_runs=1,
        children=[],
    )

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)

    def _initialize(**kwargs: object) -> tuple[SwarmRunState, Path]:
        captured["child_command"] = kwargs["child_command"]
        captured["runner_name"] = kwargs["runner_name"]
        captured["runner_model"] = kwargs["runner_model"]
        return state, tmp_path / "state.json"

    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.initialize_swarm_run",
        _initialize,
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.launch_swarm_children",
        lambda _root, _state: state,
    )

    result = runner.invoke(
        swarm_group,
        [
            "start",
            str(spec),
            "--runner",
            "ollama-claude",
            "--runner-model",
            "qwen3.6",
            "--runner-arg",
            "--append-system-prompt",
            "--runner-arg",
            "runner-note",
        ],
        obj={"root": root},
    )

    assert result.exit_code == 0
    assert captured["child_command"] == [
        "ollama",
        "launch",
        "claude",
        "--yes",
        "--model",
        "qwen3.6",
        "--",
        "--dangerously-skip-permissions",
        "--print",
        "--append-system-prompt",
        "runner-note",
        "The authoritative task spec is stored at {spec}.\n\n<task_spec>\n{spec_contents}\n</task_spec>\n\nWork directly in the current repository, make only the requested changes, do not commit, and print a concise summary of what you changed or why you left it unchanged.",
    ]
    assert captured["runner_name"] == "ollama-claude"
    assert captured["runner_model"] == "qwen3.6"


def test_swarm_start_forwards_evaluator_controls(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# spec\n", encoding="utf-8")
    root = tmp_path / "lemoncrow-root"
    captured: dict[str, object] = {}
    state = SwarmRunState(
        run_id="swarm-123",
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(spec),
        copied_spec_path=str(spec),
        runner_name="copilot",
        runner_model="gpt-5.4",
        child_command=["copilot", "agent", "run"],
        runs=2,
        max_runs=2,
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)

    def _initialize(**kwargs: object) -> tuple[SwarmRunState, Path]:
        captured.update(kwargs)
        return state, tmp_path / "state.json"

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.initialize_swarm_run", _initialize)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.launch_swarm_children",
        lambda _root, _state: state,
    )

    result = runner.invoke(
        swarm_group,
        [
            "start",
            str(spec),
            "--runner",
            "copilot",
            "--runner-model",
            "gpt-5.4",
            "--continuous",
            "--max-waves",
            "6",
            "--evaluator-backend",
            "ollama",
            "--evaluator-model",
            "claude-opus-4.8",
            "--max-evaluator-failures",
            "4",
        ],
        obj={"root": root},
    )

    assert result.exit_code == 0
    assert captured["evaluator_backend"] == "ollama"
    assert captured["evaluator_model"] == "claude-opus-4.8"
    assert captured["max_waves"] == 6
    assert captured["max_evaluator_failures"] == 4


def test_resolve_swarm_child_command_uses_claude_print_mode() -> None:
    resolved = resolve_swarm_child_command(
        runner="claude",
        runner_model="claude-sonnet-4-6",
        runner_args=["--append-system-prompt", "runner-note"],
        child_command=[],
        prompt_template="Read {spec}",
    )

    assert resolved == [
        "claude",
        "--model",
        "claude-sonnet-4-6",
        "--dangerously-skip-permissions",
        "--print",
        "--append-system-prompt",
        "runner-note",
        "Read {spec}",
    ]


def test_expand_command_tokens_inlines_spec_contents(tmp_path: Path) -> None:
    spec = tmp_path / "PROGRAM.md"
    spec.write_text("find the best optimization\n", encoding="utf-8")
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        status="pending",
        worktree_path=str(tmp_path / "worktree"),
        lemoncrow_root=str(tmp_path / "lemoncrow-root"),
        run_dir=str(tmp_path / "run"),
        spec_path=str(spec),
        result_path=str(tmp_path / "result.json"),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        metadata_path=str(tmp_path / "meta.json"),
    )

    expanded = _expand_command_tokens(
        child,
        ["claude", "--print", "Spec at {spec}\n\n{spec_contents}"],
    )

    assert expanded == [
        "claude",
        "--print",
        f"Spec at {spec}\n\nfind the best optimization",
    ]


def test_swarm_start_rejects_runner_and_raw_command(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    spec = tmp_path / "program.md"
    spec.write_text("# spec\n", encoding="utf-8")
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.require_pro", lambda _feature, _label: None)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.discover_repo_root", lambda _cwd: tmp_path)

    result = runner.invoke(
        swarm_group,
        [
            "start",
            str(spec),
            "--runner",
            "claude",
            "--",
            "echo",
            "hi",
        ],
        obj={"root": tmp_path},
    )

    assert result.exit_code != 0
    assert "choose either a built-in runner or a raw child command" in result.output


def test_provider_swarm_command_uses_python_module_path() -> None:
    command = resolve_swarm_provider_command("openai")

    assert command[:3]
    assert command[1:3] == ["-m", "lemoncrow.gateway.cli"]
    assert command[-2:] == ["swarm", "_provider-worker"]


def test_build_child_env_sets_provider_backend(tmp_path: Path) -> None:
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        status="pending",
        worktree_path=str(tmp_path / "worktree"),
        lemoncrow_root=str(tmp_path / "lemoncrow-root"),
        run_dir=str(tmp_path / "run"),
        spec_path=str(tmp_path / "program.md"),
        result_path=str(tmp_path / "result.json"),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        metadata_path=str(tmp_path / "meta.json"),
    )
    state = SwarmRunState(
        run_id="swarm-123",
        status="pending",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path="program.md",
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="openai",
        runner_model="gpt-4o-mini",
        child_command=resolve_swarm_provider_command("openai"),
        runs=1,
        max_runs=1,
        launch_provider="openai",
        launch_effort="medium",
    )

    env = build_child_env(child, state)

    assert env["LEMONCROW_LLM_BACKEND"] == "openai"
    assert env["LEMONCROW_OPENAI_MODEL"] == "gpt-4o-mini"
    assert env["LEMONCROW_SWARM_PROVIDER"] == "openai"
    assert env["LEMONCROW_SWARM_STEP_BUDGET"] == "10"


def test_swarm_status_reads_state(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    run_id = "swarm-123"
    state_path = root / "swarm" / "runs" / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    state = SwarmRunState(
        run_id=run_id,
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="claude",
        runner_model="sonnet",
        evaluator_backend="ollama",
        evaluator_model="claude-opus-4.8",
        child_command=["echo", "hi"],
        runs=0,
        max_runs=0,
        convergence_status="stagnating",
        convergence_summary="Only duplicate patch ideas remain.",
        next_wave_directives=["Probe runtime metadata trimming."],
        children=[
            SwarmChildState(
                child_id="wave-01-run-01",
                label="candidate-1",
                wave_index=1,
                status="failed",
                worktree_path=str(tmp_path / "pool" / "wave-01-run-01"),
                lemoncrow_root=str(root / "child"),
                run_dir=str(root / "runs" / "run-01"),
                spec_path=str(tmp_path / "program.md"),
                result_path=str(root / "runs" / "run-01" / "result.json"),
                stdout_path=str(root / "runs" / "run-01" / "stdout.log"),
                stderr_path=str(root / "runs" / "run-01" / "stderr.log"),
                metadata_path=str(root / "runs" / "run-01" / "meta.json"),
                summary="selected model is invalid",
            )
        ],
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.load_swarm_state", lambda _path: state)

    result = runner.invoke(swarm_group, ["status", run_id], obj={"root": root})

    assert result.exit_code == 0
    assert "swarm-123" in result.output
    assert "claude" in result.output
    assert "ollama" in result.output
    assert "stagnating" in result.output
    assert "Only duplicate patch ideas remain." in result.output
    assert "selected model is invalid" in result.output


def test_launch_swarm_children_stops_on_first_no_improvement_wave(monkeypatch: object, tmp_path: Path) -> None:
    root = tmp_path / "lemoncrow-root"
    state_path = tmp_path / "state.json"
    state = SwarmRunState(
        run_id="swarm-123",
        status="pending",
        mode="continuous",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="copilot",
        runner_model="gpt-5.4",
        child_command=["copilot", "agent", "run"],
        runs=2,
        max_runs=2,
        keep_worktrees=True,
    )
    save_swarm_state(state_path, state)

    def _prepare(state: SwarmRunState, _root: Path, wave_index: int) -> SwarmWaveState:
        wave = SwarmWaveState(wave_index=wave_index, max_runs=2, planned_runs=2)
        state.current_wave = wave_index
        state.waves.append(wave)
        return wave

    def _run_wave(_root: Path, run_state_path: Path, _wave_index: int) -> SwarmRunState:
        return SwarmRunState.model_validate_json(run_state_path.read_text(encoding="utf-8"))

    call_count = {"count": 0}

    def _apply(state: SwarmRunState, _children: list[SwarmChildState], wave: SwarmWaveState) -> bool:
        call_count["count"] += 1
        state.convergence_status = "stagnating"
        state.convergence_summary = "No materially new ideas this wave."
        wave.status = "no-improvement"
        wave.summary = state.convergence_summary
        return False

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.swarm.capability._prepare_wave",
        _prepare,
    )
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.swarm.capability._run_wave_children",
        _run_wave,
    )
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.swarm.capability.apply_wave_candidates",
        _apply,
    )

    completed = launch_swarm_children(root, state_path)

    assert call_count["count"] == 1
    assert completed.status == "success"
    assert "No materially new ideas this wave." in completed.stop_reason
    assert completed.current_wave == 1


def test_launch_swarm_children_stops_at_max_waves(monkeypatch: object, tmp_path: Path) -> None:
    root = tmp_path / "lemoncrow-root"
    state_path = tmp_path / "state.json"
    state = SwarmRunState(
        run_id="swarm-123",
        status="pending",
        mode="continuous",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="copilot",
        child_command=["copilot"],
        runs=3,
        max_runs=3,
        max_waves=2,
        keep_worktrees=True,
        max_evaluator_failures=99,
    )
    save_swarm_state(state_path, state)

    def _prepare(state: SwarmRunState, _root: Path, wave_index: int) -> SwarmWaveState:
        wave = SwarmWaveState(wave_index=wave_index, max_runs=3, planned_runs=3)
        state.current_wave = wave_index
        state.waves.append(wave)
        return wave

    def _run_wave(_root: Path, run_state_path: Path, _wave_index: int) -> SwarmRunState:
        return SwarmRunState.model_validate_json(run_state_path.read_text(encoding="utf-8"))

    call_count = {"count": 0}

    def _apply(state: SwarmRunState, _children: list[SwarmChildState], wave: SwarmWaveState) -> bool:
        call_count["count"] += 1
        child_id = f"wave-{wave.wave_index:02d}-run-01"
        state.accepted_child_ids.append(child_id)
        wave.accepted_child_ids = [child_id]
        wave.status = "applied"
        state.convergence_status = "continue"
        return True

    monkeypatch.setattr("lemoncrow.core.capabilities.swarm.capability._prepare_wave", _prepare)
    monkeypatch.setattr("lemoncrow.core.capabilities.swarm.capability._run_wave_children", _run_wave)
    monkeypatch.setattr("lemoncrow.core.capabilities.swarm.capability.apply_wave_candidates", _apply)

    completed = launch_swarm_children(root, state_path)

    assert call_count["count"] == 2
    assert completed.status == "success"
    assert completed.current_wave == 2
    assert "max_waves=2" in completed.stop_reason


def test_swarm_list_prints_known_runs(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    state = SwarmRunState(
        run_id="swarm-123",
        status="running",
        mode="continuous",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="ollama-claude",
        runner_model="qwen3.6",
        child_command=["echo", "hi"],
        runs=2,
        max_runs=4,
        current_wave=3,
        accepted_child_ids=["wave-01-run-01"],
        waves=[
            SwarmWaveState(
                wave_index=3,
                max_runs=4,
                planned_runs=2,
                planning_mode="bounded",
                child_ids=["wave-03-run-01"],
                accepted_child_ids=[],
            )
        ],
        children=[
            SwarmChildState(
                child_id="wave-03-run-01",
                label="candidate-1",
                wave_index=3,
                status="running",
                worktree_path=str(tmp_path / "pool" / "wave-03-run-01"),
                lemoncrow_root=str(root / "child"),
                run_dir=str(root / "runs" / "run-01"),
                spec_path=str(tmp_path / "program.md"),
                result_path=str(root / "runs" / "run-01" / "result.json"),
                stdout_path=str(root / "runs" / "run-01" / "stdout.log"),
                stderr_path=str(root / "runs" / "run-01" / "stderr.log"),
                metadata_path=str(root / "runs" / "run-01" / "meta.json"),
            )
        ],
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.list_swarm_runs", lambda _root: [state])

    result = runner.invoke(swarm_group, ["list"], obj={"root": root})

    assert result.exit_code == 0
    assert "swarm-123" in result.output
    assert "ollama-claude" in result.output
    assert "qwen3.6" in result.output
    assert "  2/4" in result.output


def test_swarm_logs_reads_child_output(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.read_swarm_log",
        lambda *_args, **_kwargs: "child is compacting json",
    )

    result = runner.invoke(
        swarm_group,
        [
            "logs",
            "swarm-123",
            "--child-id",
            "wave-01-run-01",
        ],
        obj={"root": root},
    )

    assert result.exit_code == 0
    assert "compacting json" in result.output


def test_swarm_status_export_flag_prints_artifacts(monkeypatch: object, tmp_path: Path) -> None:
    """`swarm export` was folded into `swarm status --export`."""
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    run_id = "swarm-123"
    state_path = root / "swarm" / "runs" / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    artifact = SwarmArtifactRef(
        kind="wave-manifest",
        label="Wave 1 manifest",
        path=str(root / "swarm" / "runs" / run_id / "artifacts" / "waves" / "wave-01-manifest.json"),
        exists=True,
    )
    state = SwarmRunState(
        run_id=run_id,
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        base_snapshot_ref="base-snapshot",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="accepted-head",
        artifact_root=str(root / "swarm" / "runs" / run_id / "artifacts"),
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="claude",
        child_command=["echo", "hi"],
        runs=2,
        max_runs=2,
        accepted_commits=[
            SwarmAcceptedCommit(
                order=1,
                child_id="wave-01-run-01",
                commit_ref="abc1234",
                patch_path="/tmp/candidate.patch",
            )
        ],
        export_artifacts=[artifact],
        transplant_commands=["git cherry-pick abc1234"],
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.load_swarm_state", lambda _path: state)

    result = runner.invoke(swarm_group, ["status", run_id, "--export"], obj={"root": root})

    assert result.exit_code == 0
    assert "base_snapshot_ref: base-snapshot" in result.output
    assert "git cherry-pick abc1234" in result.output
    assert "wave-manifest" in result.output


def test_swarm_status_watch_exits_when_run_finishes(monkeypatch: object, tmp_path: Path) -> None:
    """--watch redraws once and returns immediately once the run is no longer running."""
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    run_id = "swarm-123"
    state_path = root / "swarm" / "runs" / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    state = SwarmRunState(
        run_id=run_id,
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="claude",
        child_command=["echo", "hi"],
        runs=1,
        max_runs=1,
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.load_swarm_state", lambda _path: state)

    result = runner.invoke(swarm_group, ["status", run_id, "--watch", "--interval", "0"], obj={"root": root})

    assert result.exit_code == 0
    assert "swarm-123" in result.output
    assert "[watching" in result.output


def test_read_swarm_child_activity_tails_live_transcript(monkeypatch: object, tmp_path: Path) -> None:
    """Children run non-interactively so stdout is empty while running; the
    live Claude Code session transcript is the only real-time signal."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake_home = tmp_path / "claude-home"
    encoded = str(worktree).replace("/", "-")
    project_dir = fake_home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Investigating the ranking bug."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "name": "mcp__lemon__bash", "input": {"command": "pytest -q"}}
                            ],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        status="running",
        worktree_path=str(worktree),
        lemoncrow_root=str(tmp_path / "child"),
        run_dir=str(tmp_path / "run"),
        spec_path=str(tmp_path / "program.md"),
        result_path=str(tmp_path / "result.json"),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        metadata_path=str(tmp_path / "meta.json"),
    )

    activity = read_swarm_child_activity(child, turns=2)

    assert "Investigating the ranking bug." in activity
    assert "mcp__lemon__bash" in activity


def test_swarm_apply_prints_transplant_commands(monkeypatch: object, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "lemoncrow-root"
    run_id = "swarm-123"
    state_path = root / "swarm" / "runs" / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    state = SwarmRunState(
        run_id=run_id,
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        base_snapshot_ref="base-snapshot",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="accepted-head",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="claude",
        child_command=["echo", "hi"],
        runs=2,
        max_runs=2,
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.swarm.load_swarm_state", lambda _path: state)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.swarm.build_swarm_apply_payload",
        lambda _state, wave_index=None, child_id=None: {
            "selected_commits": [{"child_id": child_id or "wave-01-run-01"}],
            "commands": ["git cherry-pick abc1234"],
        },
    )

    result = runner.invoke(
        swarm_group,
        ["apply", run_id, "--child-id", "wave-01-run-01"],
        obj={"root": root},
    )

    assert result.exit_code == 0
    assert "selected_commits: 1" in result.output
    assert "git cherry-pick abc1234" in result.output
