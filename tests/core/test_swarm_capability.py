from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from atelier.core.capabilities.swarm import (
    initialize_swarm_run,
    load_swarm_state,
    rank_children,
    run_child_once,
)
from atelier.core.capabilities.swarm.models import SwarmChildState, SwarmValidationCheck


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

    result = run_child_once(state_path, "run-01")

    assert result.status == "success"
    assert result.summary == "candidate finished"
    assert any("child.txt" in line for line in result.files_changed)
    assert result.validation_results[0].passed
    assert Path(result.result_path).exists()

    persisted = load_swarm_state(state_path)
    assert persisted.run_id == state.run_id


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
        validation_results=[
            SwarmValidationCheck(
                name="lint",
                command="make lint",
                passed=True,
                exit_code=0,
            )
        ],
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
