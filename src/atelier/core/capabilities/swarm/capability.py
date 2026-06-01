"""Coordinator and child-run logic for the Atelier swarm harness."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.capabilities.swarm.models import (
    SwarmChildState,
    SwarmRunState,
    SwarmValidationCheck,
)
from atelier.infra.runtime.run_ledger import RunLedger
from atelier.infra.runtime.swarm_worktree import (
    SwarmWorktreeManager,
    collect_dirty_paths,
    git_repo_root,
    read_head_ref,
)
from atelier.infra.storage.factory import create_store


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
            tmp_path = handle.name
        Path(tmp_path).replace(path)
    except OSError:
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
        raise


def load_swarm_state(path: Path) -> SwarmRunState:
    return SwarmRunState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_swarm_state(path: Path, state: SwarmRunState) -> None:
    state.updated_at = _utcnow()
    _write_json(path, state.model_dump(mode="json"))


def swarm_run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / "swarm" / "runs" / run_id


def resolve_state_path(root: Path, run_id: str) -> Path:
    return swarm_run_dir(root, run_id) / "state.json"


def _child_run_dir(root: Path, run_id: str, child_id: str) -> Path:
    return swarm_run_dir(root, run_id) / "children" / child_id


def _relative_git_status(worktree: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(worktree), "status", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return [line.rstrip() for line in completed.stdout.splitlines() if line.strip()]


def _load_child_metadata(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_validation_results(base: list[SwarmValidationCheck], payload: object) -> list[SwarmValidationCheck]:
    merged = list(base)
    if not isinstance(payload, list):
        return merged
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"child-metadata-{index}")
        passed = bool(item.get("passed"))
        detail = str(item.get("detail") or "")
        merged.append(
            SwarmValidationCheck(
                name=name,
                command=str(item.get("command") or name),
                passed=passed,
                exit_code=0 if passed else int(item.get("exit_code") or 1),
                detail=detail,
                stdout_path="",
                stderr_path="",
                duration_seconds=float(item.get("duration_seconds") or 0.0),
            )
        )
    return merged


def _summarize_output(stdout_path: Path, stderr_path: Path) -> str:
    for path in (stdout_path, stderr_path):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1][:400]
    return "No summary emitted."


def _collect_cost_and_tokens(atelier_root: Path) -> tuple[int, float]:
    runs_dir = atelier_root / "runs"
    if not runs_dir.is_dir():
        return 0, 0.0
    total_tokens = 0
    total_cost = 0.0
    for path in runs_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total_tokens += int(payload.get("token_count") or 0)
        cost = payload.get("cost") or {}
        if isinstance(cost, dict):
            total_cost += float(cost.get("total_cost_usd") or 0.0)
    return total_tokens, round(total_cost, 6)


def _expand_command_tokens(child: SwarmChildState, command: list[str]) -> list[str]:
    replacements = {
        "{spec}": child.spec_path,
        "{worktree}": child.worktree_path,
        "{child_id}": child.child_id,
    }
    expanded: list[str] = []
    for token in command:
        updated = token
        for placeholder, value in replacements.items():
            updated = updated.replace(placeholder, value)
        expanded.append(updated)
    return expanded


def _coerce_int(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return int(value)
    return fallback


def _coerce_float(value: object, fallback: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return float(value)
    return fallback


def _score_child(child: SwarmChildState) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if child.status == "success":
        score += 100.0
        reasons.append("+100 successful child run")
    elif child.status == "stopped":
        score -= 30.0
        reasons.append("-30 stopped before completion")
    else:
        score -= 60.0
        reasons.append("-60 failed child run")
    validation_passes = sum(1 for item in child.validation_results if item.passed)
    validation_failures = sum(1 for item in child.validation_results if not item.passed)
    if validation_passes:
        delta = validation_passes * 15.0
        score += delta
        reasons.append(f"+{delta:.1f} validation checks passed")
    if validation_failures:
        delta = validation_failures * 25.0
        score -= delta
        reasons.append(f"-{delta:.1f} validation checks failed")
    if child.files_changed:
        score += 5.0
        reasons.append("+5 produced a git diff")
    else:
        score -= 10.0
        reasons.append("-10 no files changed")
    file_penalty = min(len(child.files_changed), 50) * 0.2
    if file_penalty:
        score -= file_penalty
        reasons.append(f"-{file_penalty:.1f} changed-file penalty")
    if child.cost_usd > 0:
        cost_penalty = child.cost_usd * 10.0
        score -= cost_penalty
        reasons.append(f"-{cost_penalty:.2f} cost penalty")
    if child.duration_seconds > 0:
        duration_penalty = min(child.duration_seconds / 120.0, 10.0)
        score -= duration_penalty
        reasons.append(f"-{duration_penalty:.2f} duration penalty")
    return round(score, 3), reasons


def rank_children(children: list[SwarmChildState]) -> list[SwarmChildState]:
    ranked: list[SwarmChildState] = []
    for child in children:
        score, breakdown = _score_child(child)
        ranked.append(child.model_copy(update={"score": score, "score_breakdown": breakdown}))
    return sorted(
        ranked,
        key=lambda item: (
            item.score if item.score is not None else float("-inf"),
            sum(1 for check in item.validation_results if check.passed),
            -(len(item.files_changed)),
        ),
        reverse=True,
    )


def initialize_swarm_run(
    *,
    root: Path,
    repo_root: Path,
    spec_path: Path,
    child_command: list[str],
    runs: int,
    validation_commands: list[str],
    keep_worktrees: bool,
    detached: bool,
) -> tuple[SwarmRunState, Path]:
    root = Path(root).resolve()
    repo_root = Path(repo_root).resolve()
    spec_path = Path(spec_path).resolve()
    run_id = f"swarm-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = swarm_run_dir(root, run_id)
    spec_copy = run_dir / "program.md"
    spec_copy.parent.mkdir(parents=True, exist_ok=True)
    spec_copy.write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    worktree_pool = repo_root.parent / f"{repo_root.name}-swarm-worktrees" / run_id
    manager = SwarmWorktreeManager(repo_root=repo_root, pool_root=worktree_pool)
    dirty_paths = collect_dirty_paths(repo_root)
    children: list[SwarmChildState] = []
    for index in range(1, runs + 1):
        child_id = f"run-{index:02d}"
        child_dir = _child_run_dir(root, run_id, child_id)
        child_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = manager.create_worktree(run_id=run_id, child_id=child_id)
        manager.sync_dirty_state(base_worktree=repo_root, child_worktree=worktree_path)
        child_spec_path = worktree_path / ".atelier-swarm" / "program.md"
        child_spec_path.parent.mkdir(parents=True, exist_ok=True)
        child_spec_path.write_text(spec_copy.read_text(encoding="utf-8"), encoding="utf-8")
        child_root = child_dir / "atelier-root"
        store = create_store(child_root)
        store.init()
        children.append(
            SwarmChildState(
                child_id=child_id,
                label=f"candidate-{index}",
                worktree_path=str(worktree_path),
                atelier_root=str(child_root),
                run_dir=str(child_dir),
                spec_path=str(child_spec_path),
                result_path=str(child_dir / "result.json"),
                stdout_path=str(child_dir / "child.stdout.log"),
                stderr_path=str(child_dir / "child.stderr.log"),
                metadata_path=str(child_dir / "child-metadata.json"),
            )
        )
    state = SwarmRunState(
        run_id=run_id,
        repo_root=str(repo_root),
        base_worktree=str(repo_root),
        base_ref=read_head_ref(repo_root),
        worktree_pool=str(worktree_pool),
        spec_source_path=str(spec_path),
        copied_spec_path=str(spec_copy),
        child_command=list(child_command),
        validation_commands=list(validation_commands),
        runs=runs,
        keep_worktrees=keep_worktrees,
        detached=detached,
        dirty_paths=dirty_paths,
        limitations=[
            "Each child gets an isolated git worktree and ATELIER_ROOT.",
            "The agent command must consume the provided worktree/spec env vars to use Atelier MCP/runtime inside each child.",
            "Ranking is heuristic MVP scoring over exit status, validations, diff presence, cost, and duration.",
        ],
        children=children,
    )
    state_path = resolve_state_path(root, run_id)
    save_swarm_state(state_path, state)
    return state, state_path


def build_child_env(child: SwarmChildState, state: SwarmRunState) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ATELIER_ROOT": child.atelier_root,
            "ATELIER_WORKSPACE_ROOT": child.worktree_path,
            "CLAUDE_WORKSPACE_ROOT": child.worktree_path,
            "ATELIER_SWARM_RUN_ID": state.run_id,
            "ATELIER_SWARM_CHILD_ID": child.child_id,
            "ATELIER_SWARM_SPEC_PATH": child.spec_path,
            "ATELIER_SWARM_RESULT_PATH": child.result_path,
            "ATELIER_SWARM_METADATA_PATH": child.metadata_path,
            "ATELIER_SWARM_BASE_REF": state.base_ref,
        }
    )
    return env


def _python_cli_invocation() -> list[str]:
    return [sys.executable, "-m", "atelier.gateway.cli"]


def launch_swarm_children(root: Path, state_path: Path) -> SwarmRunState:
    state = load_swarm_state(state_path)
    procs: dict[str, tuple[subprocess.Popen[str], contextlib.ExitStack]] = {}
    state.status = "running"
    state.coordinator_pid = os.getpid()
    state.updated_at = _utcnow()
    save_swarm_state(state_path, state)
    try:
        for child in state.children:
            stack = contextlib.ExitStack()
            stdout_handle = stack.enter_context(Path(child.stdout_path).open("w", encoding="utf-8"))
            stderr_handle = stack.enter_context(Path(child.stderr_path).open("w", encoding="utf-8"))
            proc = subprocess.Popen(
                [
                    *_python_cli_invocation(),
                    "--root",
                    child.atelier_root,
                    "swarm",
                    "_child-run",
                    "--state",
                    str(state_path),
                    "--child-id",
                    child.child_id,
                ],
                cwd=child.worktree_path,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            procs[child.child_id] = (proc, stack)
            child.pid = proc.pid
            child.status = "running"
            child.started_at = _utcnow()
            save_swarm_state(state_path, state)

        while procs:
            finished: list[str] = []
            for child_id, (proc, _stack) in procs.items():
                if proc.poll() is None:
                    continue
                finished.append(child_id)
            for child_id in finished:
                proc, stack = procs.pop(child_id)
                child_state = _refresh_child_result(state.children, child_id)
                if child_state is None:
                    child_state = next(item for item in state.children if item.child_id == child_id)
                    child_state.status = "failed" if proc.returncode else "success"
                    child_state.exit_code = proc.returncode
                    child_state.finished_at = _utcnow()
                stack.close()
                save_swarm_state(state_path, state)
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_swarm_run(root=root, state_path=state_path, cleanup=False)
        state = load_swarm_state(state_path)
        state.status = "stopped"
        save_swarm_state(state_path, state)
        raise

    state = load_swarm_state(state_path)
    ranked = rank_children(state.children)
    state.children = ranked
    state.winner_child_id = ranked[0].child_id if ranked else None
    state.ranking_notes = ranked[0].score_breakdown if ranked else []
    state.status = "success" if ranked and ranked[0].status == "success" else "failed"
    save_swarm_state(state_path, state)
    if not state.keep_worktrees:
        cleanup_swarm_run(state)
        state.ranking_notes.append("Removed child worktrees because --cleanup was enabled.")
        save_swarm_state(state_path, state)
    return state


def _refresh_child_result(children: list[SwarmChildState], child_id: str) -> SwarmChildState | None:
    child = next((item for item in children if item.child_id == child_id), None)
    if child is None:
        return None
    result_path = Path(child.result_path)
    if not result_path.exists():
        return child
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    refreshed = SwarmChildState.model_validate(payload)
    index = children.index(child)
    children[index] = refreshed
    return refreshed


def _kill_pid(pid: int | None) -> None:
    if pid is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def stop_swarm_run(*, root: Path, state_path: Path, cleanup: bool) -> SwarmRunState:
    state = load_swarm_state(state_path)
    for child in state.children:
        _kill_pid(child.pid)
        if child.status == "running":
            child.status = "stopped"
            child.finished_at = _utcnow()
    if state.coordinator_pid and state.coordinator_pid != os.getpid():
        _kill_pid(state.coordinator_pid)
    state.status = "stopped"
    save_swarm_state(state_path, state)
    if cleanup:
        cleanup_swarm_run(state)
        state.ranking_notes.append("Removed swarm worktrees after stop/cleanup.")
        save_swarm_state(state_path, state)
    return state


def cleanup_swarm_run(state: SwarmRunState) -> None:
    manager = SwarmWorktreeManager(repo_root=Path(state.repo_root), pool_root=Path(state.worktree_pool))
    for child in state.children:
        manager.remove_worktree(Path(child.worktree_path))


def run_child_once(state_path: Path, child_id: str) -> SwarmChildState:
    state = load_swarm_state(state_path)
    child = next((item for item in state.children if item.child_id == child_id), None)
    if child is None:
        raise RuntimeError(f"unknown child id: {child_id}")

    child.status = "running"
    child.started_at = _utcnow()
    env = build_child_env(child, state)
    ledger = RunLedger(
        session_id=f"{state.run_id}-{child.child_id}",
        agent="swarm-child",
        root=Path(child.atelier_root),
        task=f"Swarm child {child.child_id}",
        domain="swarm",
    )
    stdout_path = Path(child.stdout_path)
    stderr_path = Path(child.stderr_path)
    metadata_path = Path(child.metadata_path)
    result_path = Path(child.result_path)
    worktree = Path(child.worktree_path)
    active_proc: subprocess.Popen[str] | None = None

    def _terminate(signum: int, _frame: object) -> None:
        nonlocal active_proc
        if active_proc is not None and active_proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(active_proc.pid, signum)
        raise KeyboardInterrupt

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)
    started = time.perf_counter()
    try:
        command = _expand_command_tokens(child, state.child_command)
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_handle,
            stderr_path.open("w", encoding="utf-8") as stderr_handle,
        ):
            active_proc = subprocess.Popen(
                command,
                cwd=worktree,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            exit_code = active_proc.wait()
        ledger.record_command(" ".join(shlex.quote(token) for token in command), ok=exit_code == 0)
        validation_results: list[SwarmValidationCheck] = []
        for index, validate_command in enumerate(state.validation_commands, start=1):
            validation_results.append(
                _run_validation_command(
                    child=child,
                    command=validate_command,
                    env=env,
                    cwd=worktree,
                    ordinal=index,
                    ledger=ledger,
                )
            )

        metadata = _load_child_metadata(metadata_path)
        token_count, cost_usd = _collect_cost_and_tokens(Path(child.atelier_root))
        result = child.model_copy(
            update={
                "status": "success" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "files_changed": _relative_git_status(worktree),
                "validation_results": _merge_validation_results(validation_results, metadata.get("validation_results")),
                "summary": str(metadata.get("summary") or _summarize_output(stdout_path, stderr_path)),
                "error": str(metadata.get("error") or ""),
                "token_count": _coerce_int(metadata.get("token_count"), token_count),
                "cost_usd": _coerce_float(metadata.get("cost_usd"), cost_usd),
                "duration_seconds": round(time.perf_counter() - started, 3),
                "finished_at": _utcnow(),
            }
        )
        score, score_breakdown = _score_child(result)
        result.score = score
        result.score_breakdown = score_breakdown
        ledger.close("complete" if result.status == "success" else "failed")
        ledger.persist(Path(child.atelier_root))
        _write_json(result_path, result.model_dump(mode="json"))
        return result
    except KeyboardInterrupt:
        child.status = "stopped"
        child.finished_at = _utcnow()
        child.duration_seconds = round(time.perf_counter() - started, 3)
        child.error = "Interrupted."
        ledger.close("partial")
        ledger.persist(Path(child.atelier_root))
        _write_json(result_path, child.model_dump(mode="json"))
        return child
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def _run_validation_command(
    *,
    child: SwarmChildState,
    command: str,
    env: dict[str, str],
    cwd: Path,
    ordinal: int,
    ledger: RunLedger,
) -> SwarmValidationCheck:
    log_base = Path(child.run_dir) / f"validation-{ordinal:02d}"
    stdout_path = log_base.with_suffix(".stdout.log")
    stderr_path = log_base.with_suffix(".stderr.log")
    started = time.perf_counter()
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_handle,
        stderr_path.open("w", encoding="utf-8") as stderr_handle,
    ):
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            shell=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            check=False,
        )
    duration = round(time.perf_counter() - started, 3)
    ledger.record_command(command, ok=proc.returncode == 0)
    return SwarmValidationCheck(
        name=f"validation-{ordinal}",
        command=command,
        passed=proc.returncode == 0,
        exit_code=proc.returncode,
        detail="pass" if proc.returncode == 0 else "failed",
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        duration_seconds=duration,
    )


def format_swarm_summary(state: SwarmRunState) -> str:
    winner = next((child for child in state.children if child.child_id == state.winner_child_id), None)
    lines = [
        f"run_id: {state.run_id}",
        f"status: {state.status}",
        f"children: {len(state.children)}",
    ]
    if winner is not None:
        lines.append(
            f"winner: {winner.child_id} score={winner.score} files={len(winner.files_changed)} validations={sum(1 for item in winner.validation_results if item.passed)}/{len(winner.validation_results)}"
        )
        lines.append(f"winner_worktree: {winner.worktree_path}")
        lines.append(f"winner_summary: {winner.summary}")
    return "\n".join(lines)


def discover_repo_root(cwd: Path) -> Path:
    return git_repo_root(cwd)
