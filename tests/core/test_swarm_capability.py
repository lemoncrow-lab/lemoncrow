from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lemoncrow.infra.runtime.swarm_worktree import SwarmWorktreeManager, read_head_ref
from lemoncrow.pro.capabilities.swarm import (
    apply_wave_candidates,
    initialize_swarm_run,
    launch_swarm_children,
    load_swarm_state,
    rank_children,
    run_child_once,
    save_swarm_state,
    swarm_run_dir,
)
from lemoncrow.pro.capabilities.swarm.capability import (
    _fallback_wave_evaluation,
    _plan_wave_runs,
    _score_child,
    _write_child_patch,
)
from lemoncrow.pro.capabilities.swarm.models import (
    SwarmAcceptedCommit,
    SwarmChildState,
    SwarmRunState,
    SwarmValidationCheck,
    SwarmWaveDecision,
    SwarmWaveEvaluation,
    SwarmWaveState,
)


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
        lemoncrow_root=str(run_dir / "lemoncrow-root"),
        run_dir=str(run_dir),
        spec_path=str(run_dir / "program.md"),
        result_path=str(run_dir / "result.json"),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        metadata_path=str(run_dir / "meta.json"),
        patch_path=str(run_dir / "candidate.patch"),
        files_changed=[f" M {changed_file}"],
        validation_results=_passing_validation(),
    )


def test_swarm_run_dir_resolves_relative_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    resolved = swarm_run_dir(Path("relative-root"), "swarm-123")

    assert resolved.is_absolute()
    assert resolved == tmp_path / "relative-root" / "swarm" / "runs" / "swarm-123"


def test_run_child_once_writes_structured_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = tmp_path / "lemoncrow-root"
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
            "Path(os.environ['LEMONCROW_SWARM_METADATA_PATH']).write_text("
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
        lemoncrow_root=str(child_dir / "lemoncrow-root"),
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
    from lemoncrow.pro.capabilities.swarm import save_swarm_state

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


def test_run_child_once_marks_silent_noop_as_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    root = tmp_path / "lemoncrow-root"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    spec = repo / "program.md"
    spec.write_text("# program\n\nDo the thing.\n", encoding="utf-8")
    state, state_path = initialize_swarm_run(
        root=root,
        repo_root=repo,
        spec_path=spec,
        child_command=[sys.executable, "-c", ""],
        runs=1,
        validation_commands=[],
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
        lemoncrow_root=str(child_dir / "lemoncrow-root"),
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
    from lemoncrow.pro.capabilities.swarm import save_swarm_state

    save_swarm_state(state_path, load_swarm)

    result = run_child_once(state_path, "wave-01-run-01")

    assert result.status == "failed"
    assert result.files_changed == []
    assert result.summary == "Child runner exited successfully without output or code changes."
    assert result.error == "Child runner exited successfully without output or code changes."


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
        lemoncrow_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        files_changed=[" M file.py"],
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


def test_score_child_penalizes_missing_validation_evidence() -> None:
    validated = SwarmChildState(
        child_id="run-01",
        label="candidate-1",
        status="success",
        worktree_path="/workspace/a",
        lemoncrow_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        files_changed=[" M file.py"],
        validation_results=_passing_validation(),
    )
    unvalidated = validated.model_copy(update={"child_id": "run-02", "validation_results": []})

    validated_score, _ = _score_child(validated)
    unvalidated_score, reasons = _score_child(unvalidated)

    assert validated_score > unvalidated_score
    assert "-12 no validation evidence" in reasons


def test_fallback_wave_evaluation_accepts_only_top_candidate_per_overlap_cluster(tmp_path: Path) -> None:
    state = SwarmRunState(
        run_id="swarm-test",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        base_snapshot_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        child_command=["echo", "hi"],
        runs=3,
    )
    primary = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        status="success",
        worktree_path="/workspace/a",
        lemoncrow_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        patch_path="/workspace/a.patch",
        files_changed=[" M shared.py"],
        validation_results=_passing_validation(),
        duration_seconds=10,
    )
    overlap = primary.model_copy(
        update={
            "child_id": "wave-01-run-02",
            "label": "candidate-2",
            "validation_results": [],
            "duration_seconds": 30,
        }
    )
    disjoint = primary.model_copy(
        update={
            "child_id": "wave-01-run-03",
            "label": "candidate-3",
            "files_changed": [" M independent.py"],
            "duration_seconds": 12,
        }
    )

    evaluation = _fallback_wave_evaluation(state, [overlap, disjoint, primary], error="Evaluator backend disabled.")
    decision_map = {item.child_id: item for item in evaluation.decisions}

    assert evaluation.accepted_child_ids == ["wave-01-run-01", "wave-01-run-03"]
    assert decision_map["wave-01-run-02"].verdict == "reject"
    assert decision_map["wave-01-run-02"].conflicts_with == ["wave-01-run-01"]
    assert "overlaps" in decision_map["wave-01-run-02"].rationale


def test_fallback_wave_evaluation_rejects_duplicate_of_accepted_history(tmp_path: Path) -> None:
    accepted_patch = tmp_path / "accepted.patch"
    candidate_patch = tmp_path / "candidate.patch"
    patch_text = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-base\n+updated\n"
    accepted_patch.write_text(patch_text, encoding="utf-8")
    candidate_patch.write_text(patch_text, encoding="utf-8")
    program = tmp_path / "program.md"
    program.write_text("# program\n", encoding="utf-8")
    state = SwarmRunState(
        run_id="swarm-test",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        base_snapshot_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        spec_source_path=str(program),
        copied_spec_path=str(program),
        child_command=["echo", "hi"],
        runs=1,
        accepted_commits=[
            SwarmAcceptedCommit(
                order=1,
                child_id="wave-00-run-01",
                commit_ref="accepted123",
                patch_path=str(accepted_patch),
            )
        ],
    )
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        status="success",
        worktree_path="/workspace/a",
        lemoncrow_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        patch_path=str(candidate_patch),
        files_changed=[" M a.txt"],
        validation_results=_passing_validation(),
    )

    evaluation = _fallback_wave_evaluation(state, [child], error="Evaluator backend disabled.")
    decision = evaluation.decisions[0]

    assert evaluation.accepted_child_ids == []
    assert decision.verdict == "reject"
    assert decision.duplicates == ["wave-00-run-01"]
    assert "matches already accepted" in decision.rationale


def test_fallback_wave_evaluation_defers_revisiting_recent_files_without_new_validation(tmp_path: Path) -> None:
    accepted_patch = tmp_path / "accepted.patch"
    candidate_patch = tmp_path / "candidate.patch"
    accepted_patch.write_text("diff --git a/a.txt b/a.txt\n", encoding="utf-8")
    candidate_patch.write_text("diff --git a/a.txt b/a.txt\ncontext\n", encoding="utf-8")
    program = tmp_path / "program.md"
    program.write_text("# program\n", encoding="utf-8")
    state = SwarmRunState(
        run_id="swarm-test",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        base_snapshot_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        spec_source_path=str(program),
        copied_spec_path=str(program),
        child_command=["echo", "hi"],
        runs=1,
        accepted_commits=[
            SwarmAcceptedCommit(
                order=1,
                child_id="wave-00-run-01",
                commit_ref="accepted123",
                files_changed=[" M src/app.py", " M tests/test_app.py"],
                patch_path=str(accepted_patch),
            )
        ],
    )
    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        status="success",
        worktree_path="/workspace/a",
        lemoncrow_root="/workspace/a-root",
        run_dir="/workspace/a-run",
        spec_path="/workspace/spec",
        result_path="/workspace/result",
        stdout_path="/workspace/stdout",
        stderr_path="/workspace/stderr",
        metadata_path="/workspace/meta",
        patch_path=str(candidate_patch),
        files_changed=[" M src/app.py", " M tests/test_app.py"],
        validation_results=[
            SwarmValidationCheck(
                name="structural-diff-check",
                command="git diff --check",
                passed=True,
                exit_code=0,
            )
        ],
    )

    evaluation = _fallback_wave_evaluation(state, [child], error="Evaluator backend disabled.")
    decision = evaluation.decisions[0]

    assert evaluation.accepted_child_ids == []
    assert evaluation.deferred_child_ids == [child.child_id]
    assert evaluation.verdict == "continue"
    assert evaluation.next_wave_directives
    assert decision.verdict == "defer"
    assert decision.conflicts_with == ["wave-00-run-01"]
    assert "revisits recently accepted file set" in decision.rationale


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
    assert "overlaps" in children[2].acceptance_note.lower()
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


def test_apply_wave_candidates_respects_evaluator_conflict_metadata(
    monkeypatch: pytest.MonkeyPatch,
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
        runs=2,
        children=children,
    )
    wave = SwarmWaveState(
        wave_index=1,
        child_ids=[child.child_id for child in children],
    )

    def _fake_evaluate(
        _state: SwarmRunState, _wave: SwarmWaveState, _children: list[SwarmChildState]
    ) -> SwarmWaveEvaluation:
        return SwarmWaveEvaluation(
            status="completed",
            evaluator_backend="disabled",
            summary="Only one candidate should land.",
            verdict="continue",
            candidate_order=["wave-01-run-01", "wave-01-run-02"],
            accepted_child_ids=["wave-01-run-01", "wave-01-run-02"],
            decisions=[
                SwarmWaveDecision(
                    child_id="wave-01-run-01",
                    verdict="accept",
                    rationale="Primary candidate.",
                ),
                SwarmWaveDecision(
                    child_id="wave-01-run-02",
                    verdict="accept",
                    rationale="Conflicts with the primary candidate.",
                    conflicts_with=["wave-01-run-01"],
                ),
            ],
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.swarm.capability._evaluate_wave", _fake_evaluate)

    accepted_any = apply_wave_candidates(state, children, wave)

    assert accepted_any is True
    assert wave.accepted_child_ids == ["wave-01-run-01"]
    assert wave.rejected_child_ids == ["wave-01-run-02"]
    assert children[0].accepted is True
    assert children[1].accepted is False
    assert children[1].acceptance_note == "Conflicts with the primary candidate."
    assert (integration / "a.txt").read_text(encoding="utf-8") == "improvement-a\n"
    assert (integration / "b.txt").read_text(encoding="utf-8") == "base-b\n"


def test_apply_wave_candidates_rejects_duplicate_of_accepted_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    program = tmp_path / "program.md"
    program.write_text("# program\n", encoding="utf-8")
    accepted_patch = tmp_path / "accepted.patch"
    candidate_patch = tmp_path / "candidate.patch"
    patch_text = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-base\n+updated\n"
    accepted_patch.write_text(patch_text, encoding="utf-8")
    candidate_patch.write_text(patch_text, encoding="utf-8")

    child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        status="success",
        worktree_path=str(tmp_path / "worktree"),
        lemoncrow_root=str(tmp_path / "lemoncrow-root"),
        run_dir=str(tmp_path / "run"),
        spec_path=str(program),
        result_path=str(tmp_path / "result.json"),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        metadata_path=str(tmp_path / "meta.json"),
        patch_path=str(candidate_patch),
        files_changed=[" M a.txt"],
        validation_results=_passing_validation(),
    )
    state = SwarmRunState(
        run_id="swarm-test",
        status="running",
        mode="continuous",
        repo_root=str(tmp_path / "repo"),
        base_worktree=str(tmp_path / "repo"),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(program),
        copied_spec_path=str(program),
        child_command=["echo", "hi"],
        runs=1,
        accepted_commits=[
            SwarmAcceptedCommit(
                order=1,
                child_id="wave-00-run-01",
                commit_ref="accepted123",
                patch_path=str(accepted_patch),
            )
        ],
        children=[child],
    )
    wave = SwarmWaveState(wave_index=1, child_ids=[child.child_id])

    def _fake_evaluate(
        _state: SwarmRunState, _wave: SwarmWaveState, _children: list[SwarmChildState]
    ) -> SwarmWaveEvaluation:
        return SwarmWaveEvaluation(
            status="completed",
            evaluator_backend="disabled",
            summary="Accept candidate.",
            verdict="continue",
            candidate_order=[child.child_id],
            accepted_child_ids=[child.child_id],
            decisions=[SwarmWaveDecision(child_id=child.child_id, verdict="accept", rationale="Looks good.")],
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.swarm.capability._evaluate_wave", _fake_evaluate)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.swarm.capability._write_child_patch", lambda _child: candidate_patch
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.swarm.capability._can_apply_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate should be rejected before patch apply")
        ),
    )

    accepted_any = apply_wave_candidates(state, [child], wave)

    assert accepted_any is False
    assert wave.accepted_child_ids == []
    assert wave.rejected_child_ids == [child.child_id]
    assert child.accepted is False
    assert "duplicates already accepted candidate(s): wave-00-run-01" in child.acceptance_note


def test_launch_swarm_children_continues_after_deferred_wave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
        runs=2,
        max_runs=2,
        max_waves=2,
        keep_worktrees=True,
        max_evaluator_failures=99,
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
        if call_count["count"] == 1:
            state.convergence_status = "continue"
            state.convergence_summary = "Need a distinct angle."
            state.next_wave_directives = ["Pursue a distinct angle."]
            wave.status = "no-improvement"
            wave.summary = state.convergence_summary
            wave.evaluation = SwarmWaveEvaluation(
                status="fallback",
                summary=state.convergence_summary,
                verdict="continue",
                candidate_order=[],
                deferred_child_ids=["wave-01-run-01"],
                next_wave_directives=list(state.next_wave_directives),
            )
            return False
        child_id = f"wave-{wave.wave_index:02d}-run-01"
        state.accepted_child_ids.append(child_id)
        wave.accepted_child_ids = [child_id]
        wave.status = "applied"
        state.convergence_status = "continue"
        return True

    monkeypatch.setattr("lemoncrow.pro.capabilities.swarm.capability._prepare_wave", _prepare)
    monkeypatch.setattr("lemoncrow.pro.capabilities.swarm.capability._run_wave_children", _run_wave)
    monkeypatch.setattr("lemoncrow.pro.capabilities.swarm.capability.apply_wave_candidates", _apply)

    completed = launch_swarm_children(root, state_path)

    assert call_count["count"] == 2
    assert completed.current_wave == 2
    assert completed.accepted_child_ids == ["wave-02-run-01"]


def test_write_child_patch_includes_untracked_files(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init")
    _git(worktree, "config", "user.email", "swarm@test")
    _git(worktree, "config", "user.name", "swarm")
    (worktree / "existing.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "base")
    # Child only creates a brand-new, untracked file.
    (worktree / "created.py").write_text("print('hi')\n", encoding="utf-8")

    child = _make_child(
        tmp_path,
        child_id="run-01",
        worktree_path=worktree,
        changed_file="created.py",
    )

    patch_path = _write_child_patch(child)

    assert patch_path is not None, child.acceptance_note
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "created.py" in patch_text
    assert "print('hi')" in patch_text
