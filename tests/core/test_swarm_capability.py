from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from atelier.core.capabilities.swarm import (
    apply_wave_candidates,
    initialize_swarm_run,
    load_swarm_state,
    rank_children,
    run_child_once,
    swarm_run_dir,
)
from atelier.core.capabilities.swarm.capability import _plan_wave_runs
from atelier.core.capabilities.swarm.models import (
    SwarmChildState,
    SwarmRunState,
    SwarmValidationCheck,
    SwarmWaveState,
)
from atelier.infra.runtime.swarm_worktree import SwarmWorktreeManager, read_head_ref


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _passing_validation() -> list[SwarmValidationCheck]:
    return [
        SwarmValidationCheck(
            name="lint",
            command="make lint",
            passed=True,
            exit_code=0,
        )
    ]


def _make_child(
    tmp_path: Path,
    *,
    child_id: str,
    worktree_path: Path,
    changed_file: str,
) -> SwarmChildState:
    run_dir = tmp_path / child_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return SwarmChildState(
        child_id=child_id,
        label=child_id,
        wave_index=1,
        status="success",
        worktree_path=str(worktree_path),
        atelier_root=str(run_dir / "atelier-root"),
        run_dir=str(run_dir),
        spec_path=str(run_dir / "program.md"),
        result_path=str(run_dir / "result.json"),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        metadata_path=str(run_dir / "meta.json"),
        patch_path=str(run_dir / "candidate.patch"),
        files_changed=[f"M {changed_file}"],
        validation_results=_passing_validation(),
    )


def test_swarm_run_dir_resolves_relative_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    resolved = swarm_run_dir(Path("relative-root"), "swarm-123")

    assert resolved.is_absolute()
    assert resolved == tmp_path / "relative-root" / "swarm" / "runs" / "swarm-123"


def test_run_child_once_writes_structured_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = tmp_path / "atelier-root"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    spec = repo / "program.md"
    spec.write_text("# program\n\nDo the thing.\n", encoding="utf-8")
    command = [
        sys.executable,
        "-c",
        (
            "import json, os; "
            "from pathlib import Path; "
            "Path('child.txt').write_text('done\\n', encoding='utf-8'); "
            "Path(os.environ['ATELIER_SWARM_METADATA_PATH']).write_text("
            "json.dumps({'summary': 'candidate finished', 'token_count': 17}),"
            " encoding='utf-8')"
        ),
    ]
    state, state_path = initialize_swarm_run(
        root=root,
        repo_root=repo,
        spec_path=spec,
        child_command=command,
        runs=1,
        validation_commands=["git status --short"],
        keep_worktrees=True,
        detached=False,
    )

    wave = SwarmWaveState(wave_index=1, child_ids=["wave-01-run-01"])
    state.waves.append(wave)
    manager = SwarmWorktreeManager(repo_root=repo, pool_root=Path(state.worktree_pool))
    child_worktree = manager.create_worktree(
        run_id=state.run_id,
        child_id="wave-01-run-01",
        ref=state.integration_base_ref,
    )
    child_dir = Path(root) / "swarm" / "runs" / state.run_id / "children" / "wave-01-run-01"
    child_dir.mkdir(parents=True, exist_ok=True)
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        worktree_path=str(child_worktree),
        atelier_root=str(child_dir / "atelier-root"),
        run_dir=str(child_dir),
        spec_path=str(spec),
        result_path=str(child_dir / "result.json"),
        stdout_path=str(child_dir / "stdout.log"),
        stderr_path=str(child_dir / "stderr.log"),
        metadata_path=str(child_dir / "meta.json"),
        patch_path=str(child_dir / "candidate.patch"),
    )
    state.children.append(child)
    load_swarm = load_swarm_state(state_path)
    load_swarm.children = state.children
    load_swarm.waves = state.waves
    load_swarm.current_wave = 1
    from atelier.core.capabilities.swarm import save_swarm_state

    save_swarm_state(state_path, load_swarm)

    result = run_child_once(state_path, "wave-01-run-01")

    assert result.status == "success"
    assert result.summary == "candidate finished"
    assert any("child.txt" in line for line in result.files_changed)
    assert result.validation_results[0].passed
    assert Path(result.result_path).exists()
    assert state.base_snapshot_artifact is not None
    assert Path(state.base_snapshot_artifact.path).exists()
    assert state.base_snapshot_ref == state.integration_base_ref


def test_plan_wave_runs_uses_max_for_open_ended_scope(tmp_path: Path) -> None:
    spec_path = tmp_path / "open-ended.md"
    spec_path.write_text(
        "\n".join(
            [
                "# Adaptive swarm",
                "- build backend API",
                "- add frontend dashboard",
                "- wire export/apply UX",
                "- inspect multiple files: src/a.py frontend/src/App.tsx docs/spec.md",
            ]
        ),
        encoding="utf-8",
    )
    state = SwarmRunState(
        run_id="swarm-open",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        base_snapshot_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        spec_source_path=str(spec_path),
        copied_spec_path=str(spec_path),
        child_command=["echo", "hi"],
        runs=4,
        max_runs=4,
    )

    planning_mode, planned_runs, planning_reason = _plan_wave_runs(state, 1)

    assert planning_mode == "open-ended"
    assert planned_runs == 4
    assert "Open-ended search space" in planning_reason


def test_plan_wave_runs_launches_fewer_for_bounded_scope(tmp_path: Path) -> None:
    spec_path = tmp_path / "bounded.md"
    spec_path.write_text("# Fix typo\n\nRename one file and update a small fix.\n", encoding="utf-8")
    state = SwarmRunState(
        run_id="swarm-bounded",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        base_snapshot_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        spec_source_path=str(spec_path),
        copied_spec_path=str(spec_path),
        child_command=["echo", "hi"],
        runs=5,
        max_runs=5,
    )

    planning_mode, planned_runs, planning_reason = _plan_wave_runs(state, 1)

    assert planning_mode == "bounded"
    assert planned_runs == 2
    assert "Bounded search space" in planning_reason


def test_rank_children_prefers_successful_validated_candidate() -> None:
    winner = SwarmChildState(
        child_id="run-01",
        label="candidate-1",
        status="success",
        worktree_path="/workspace/a",
        atelier_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        files_changed=["M file.py"],
        validation_results=_passing_validation(),
    )
    loser = winner.model_copy(
        update={
            "child_id": "run-02",
            "status": "failed",
            "validation_results": [
                SwarmValidationCheck(
                    name="lint",
                    command="make lint",
                    passed=False,
                    exit_code=1,
                    detail="failed",
                )
            ],
            "files_changed": [],
        }
    )

    ranked = rank_children([loser, winner])

    assert ranked[0].child_id == "run-01"
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score > ranked[1].score


def test_apply_wave_candidates_merges_disjoint_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base-b\n", encoding="utf-8")
    _commit_all(repo, "base")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    integration = manager.create_worktree(
        run_id="swarm-test",
        child_id="integration",
        ref="HEAD",
    )

    child_one_path = manager.create_worktree(
        run_id="swarm-test",
        child_id="wave-01-run-01",
        ref=read_head_ref(integration),
    )
    (child_one_path / "a.txt").write_text("improvement-a\n", encoding="utf-8")

    child_two_path = manager.create_worktree(
        run_id="swarm-test",
        child_id="wave-01-run-02",
        ref=read_head_ref(integration),
    )
    (child_two_path / "b.txt").write_text("improvement-b\n", encoding="utf-8")

    child_three_path = manager.create_worktree(
        run_id="swarm-test",
        child_id="wave-01-run-03",
        ref=read_head_ref(integration),
    )
    (child_three_path / "a.txt").write_text("conflicting-a\n", encoding="utf-8")

    children = [
        _make_child(
            tmp_path,
            child_id="wave-01-run-01",
            worktree_path=child_one_path,
            changed_file="a.txt",
        ),
        _make_child(
            tmp_path,
            child_id="wave-01-run-02",
            worktree_path=child_two_path,
            changed_file="b.txt",
        ),
        _make_child(
            tmp_path,
            child_id="wave-01-run-03",
            worktree_path=child_three_path,
            changed_file="a.txt",
        ),
    ]
    state = SwarmRunState(
        run_id="swarm-test",
        status="running",
        mode="continuous",
        repo_root=str(repo),
        base_worktree=str(repo),
        base_ref=read_head_ref(repo),
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(integration),
        integration_base_ref=read_head_ref(integration),
        spec_source_path=str(repo / "program.md"),
        copied_spec_path=str(repo / "program.md"),
        child_command=["echo", "hi"],
        runs=3,
        children=children,
    )
    wave = SwarmWaveState(
        wave_index=1,
        child_ids=[child.child_id for child in children],
    )

    accepted_any = apply_wave_candidates(state, children, wave)

    assert accepted_any is True
    assert wave.accepted_child_ids == ["wave-01-run-01", "wave-01-run-02"]
    assert "wave-01-run-03" in wave.rejected_child_ids
    assert children[0].accepted is True
    assert children[1].accepted is True
    assert children[2].accepted is False
    assert "conflicted" in children[2].acceptance_note.lower()
    assert (integration / "a.txt").read_text(encoding="utf-8") == "improvement-a\n"
    assert (integration / "b.txt").read_text(encoding="utf-8") == "improvement-b\n"
    assert state.integration_base_ref == read_head_ref(integration)
    assert wave.primary_winner_child_id == "wave-01-run-01"
    assert len(wave.accepted_commits) == 2
    assert len(state.accepted_commits) == 2
    assert state.transplant_commands[0].startswith("git cherry-pick ")
    accepted_manifest = Path(state.copied_spec_path).parent / "artifacts" / "accepted-commits.json"
    wave_manifest = Path(state.copied_spec_path).parent / "artifacts" / "waves" / "wave-01-manifest.json"
    assert accepted_manifest.exists()
    assert wave_manifest.exists()
