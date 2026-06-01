"""Coordinator and child-run logic for the Atelier swarm harness."""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
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
    SwarmAcceptedCommit,
    SwarmArtifactRef,
    SwarmChildState,
    SwarmPlanningMode,
    SwarmRunState,
    SwarmValidationCheck,
    SwarmWaveState,
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


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def load_swarm_state(path: Path) -> SwarmRunState:
    return SwarmRunState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_swarm_state(path: Path, state: SwarmRunState) -> None:
    state.updated_at = _utcnow()
    _write_json(path, state.model_dump(mode="json"))


def swarm_run_dir(root: Path, run_id: str) -> Path:
    return Path(root).resolve() / "swarm" / "runs" / run_id


def resolve_state_path(root: Path, run_id: str) -> Path:
    return swarm_run_dir(root, run_id) / "state.json"


def _run_artifact_root(state: SwarmRunState) -> Path:
    if state.artifact_root:
        return Path(state.artifact_root)
    return Path(state.copied_spec_path).resolve().parent / "artifacts"


def _artifact_relative_path(run_dir: Path, path: Path) -> str:
    with contextlib.suppress(ValueError):
        return str(path.relative_to(run_dir))
    return str(path)


def _artifact_ref(
    run_dir: Path,
    path: Path,
    *,
    kind: str,
    label: str,
    metadata: dict[str, object] | None = None,
) -> SwarmArtifactRef:
    mime_type, _ = mimetypes.guess_type(path.name)
    exists = path.exists()
    return SwarmArtifactRef(
        artifact_id=f"{kind}:{_artifact_relative_path(run_dir, path)}",
        kind=kind,
        label=label,
        path=str(path),
        relative_path=_artifact_relative_path(run_dir, path),
        mime_type=mime_type or ("application/json" if path.suffix == ".json" else "text/plain"),
        size_bytes=path.stat().st_size if exists else 0,
        exists=exists,
        metadata=metadata or {},
    )


def _upsert_run_artifact(state: SwarmRunState, artifact: SwarmArtifactRef) -> None:
    for index, existing in enumerate(state.export_artifacts):
        if existing.path == artifact.path:
            state.export_artifacts[index] = artifact
            return
    state.export_artifacts.append(artifact)


def _spec_text(state: SwarmRunState) -> str:
    spec_path = Path(state.copied_spec_path)
    if not spec_path.exists():
        return ""
    return spec_path.read_text(encoding="utf-8", errors="replace")


def _plan_wave_runs(state: SwarmRunState, wave_index: int) -> tuple[SwarmPlanningMode, int, str]:
    max_runs = max(state.max_runs or state.runs or 1, 1)
    if max_runs == 1:
        return "bounded", 1, "max_runs is 1, so the coordinator launches a single child."

    spec_text = _spec_text(state)
    lowered = spec_text.lower()
    file_hints = {
        match
        for match in re.findall(
            r"[\w./-]+\.(?:py|ts|tsx|js|jsx|json|md|sql|sh|yaml|yml|toml|css|scss)",
            spec_text,
        )
    }
    bullet_count = sum(1 for line in spec_text.splitlines() if re.match(r"\s*(?:[-*]|\d+\.)\s+", line))
    open_signals = [
        signal
        for signal in (
            "end-to-end",
            "dashboard",
            "frontend",
            "backend",
            "api",
            "ux",
            "open-ended",
            "explore",
            "across",
            "multiple",
            "service endpoints",
            "integration/export",
        )
        if signal in lowered
    ]
    bounded_signals = [
        signal
        for signal in (
            "rename",
            "one file",
            "single file",
            "small fix",
            "typo",
            "bounded",
            "narrow",
        )
        if signal in lowered
    ]
    previous_wave_accepts = len(state.waves[-1].accepted_child_ids) if state.waves else 0
    is_open_ended = bool(open_signals) or len(file_hints) >= 3 or bullet_count >= 4
    if wave_index > 1 and previous_wave_accepts > 1:
        is_open_ended = True
    if bounded_signals and not open_signals and len(file_hints) <= 2 and bullet_count <= 3:
        is_open_ended = False

    if is_open_ended:
        reason = (
            f"Open-ended search space detected from {len(file_hints)} file hints, "
            f"{bullet_count} task bullets, and signals: {', '.join(open_signals) or 'broad scope'}."
        )
        return "open-ended", max_runs, reason

    planned = min(max_runs, 2)
    if wave_index > 1 and previous_wave_accepts <= 1:
        planned = 1
    reason = (
        f"Bounded search space detected from {len(file_hints)} file hints, "
        f"{bullet_count} task bullets, and signals: {', '.join(bounded_signals) or 'narrow scope'}."
    )
    return "bounded", planned, reason


def _write_artifact_payload(
    run_dir: Path,
    relative_path: str,
    payload: dict[str, object],
    *,
    kind: str,
    label: str,
    metadata: dict[str, object] | None = None,
) -> SwarmArtifactRef:
    artifact_path = run_dir / relative_path
    _write_json(artifact_path, payload)
    return _artifact_ref(run_dir, artifact_path, kind=kind, label=label, metadata=metadata)


def _refresh_transplant_commands(state: SwarmRunState) -> None:
    commit_refs = [item.commit_ref for item in state.accepted_commits if item.commit_ref]
    commands: list[str] = []
    if commit_refs:
        commands.append(f"git cherry-pick {' '.join(commit_refs)}")
    for accepted in state.accepted_commits:
        if accepted.patch_path:
            commands.append(f"git apply {shlex.quote(accepted.patch_path)}")
    state.transplant_commands = commands


def _write_run_base_snapshot_manifest(state: SwarmRunState, run_dir: Path) -> None:
    state.base_snapshot_ref = state.base_snapshot_ref or state.integration_base_ref or state.base_ref
    artifact = _write_artifact_payload(
        run_dir,
        "artifacts/base-snapshot.json",
        {
            "run_id": state.run_id,
            "base_ref": state.base_ref,
            "base_snapshot_ref": state.base_snapshot_ref,
            "integration_base_ref": state.integration_base_ref,
            "dirty_paths": state.dirty_paths,
            "dirty_state_applied": bool(state.dirty_paths),
            "semantics": "base_snapshot_ref is the exact integration snapshot the swarm launched from, including synced dirty state when present.",
        },
        kind="base-snapshot",
        label="Base snapshot manifest",
        metadata={
            "base_ref": state.base_ref,
            "base_snapshot_ref": state.base_snapshot_ref,
            "dirty_state_applied": bool(state.dirty_paths),
        },
    )
    state.base_snapshot_artifact = artifact
    _upsert_run_artifact(state, artifact)


def _write_wave_manifest(state: SwarmRunState, wave: SwarmWaveState) -> None:
    run_dir = Path(state.copied_spec_path).resolve().parent
    rejected_outcomes = [
        {
            "child_id": child.child_id,
            "status": child.status,
            "summary": child.summary,
            "error": child.error,
            "acceptance_note": child.acceptance_note,
        }
        for child in _children_for_wave(state, wave.wave_index)
        if child.child_id in wave.rejected_child_ids
    ]
    artifact = _write_artifact_payload(
        run_dir,
        f"artifacts/waves/wave-{wave.wave_index:02d}-manifest.json",
        {
            "run_id": state.run_id,
            "wave_index": wave.wave_index,
            "status": wave.status,
            "max_runs": wave.max_runs,
            "planned_runs": wave.planned_runs,
            "planning_mode": wave.planning_mode,
            "planning_reason": wave.planning_reason,
            "primary_winner_child_id": wave.primary_winner_child_id,
            "accepted_child_ids": wave.accepted_child_ids,
            "accepted_commits": [item.model_dump(mode="json") for item in wave.accepted_commits],
            "rejected_outcomes": rejected_outcomes,
            "summary": wave.summary,
        },
        kind="wave-manifest",
        label=f"Wave {wave.wave_index} manifest",
        metadata={
            "wave_index": wave.wave_index,
            "accepted_count": len(wave.accepted_commits),
            "rejected_count": len(rejected_outcomes),
        },
    )
    wave.manifest_artifact = artifact
    _upsert_run_artifact(state, artifact)


def _write_run_acceptance_manifest(state: SwarmRunState) -> None:
    run_dir = Path(state.copied_spec_path).resolve().parent
    artifact = _write_artifact_payload(
        run_dir,
        "artifacts/accepted-commits.json",
        {
            "run_id": state.run_id,
            "base_snapshot_ref": state.base_snapshot_ref,
            "integration_base_ref": state.integration_base_ref,
            "accepted_child_ids": state.accepted_child_ids,
            "accepted_commits": [item.model_dump(mode="json") for item in state.accepted_commits],
            "transplant_commands": state.transplant_commands,
        },
        kind="accepted-commits",
        label="Accepted commit manifest",
        metadata={"accepted_count": len(state.accepted_commits)},
    )
    _upsert_run_artifact(state, artifact)


def _child_run_dir(root: Path, run_id: str, child_id: str) -> Path:
    return swarm_run_dir(root, run_id) / "children" / child_id


def _relative_git_status(worktree: Path) -> list[str]:
    completed = _git("status", "--short", cwd=worktree)
    if completed.returncode != 0:
        return []
    filtered: list[str] = []
    for line in completed.stdout.splitlines():
        text = line.rstrip()
        if not text:
            continue
        path_text = text[3:]
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        if path_text == ".atelier-swarm/" or path_text.startswith(".atelier-swarm/"):
            continue
        filtered.append(text)
    return filtered


def _load_child_metadata(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_validation_results(
    base: list[SwarmValidationCheck],
    payload: object,
) -> list[SwarmValidationCheck]:
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
    for child in children:
        score, breakdown = _score_child(child)
        child.score = score
        child.score_breakdown = breakdown
    return sorted(
        children,
        key=lambda item: (
            item.score if item.score is not None else float("-inf"),
            sum(1 for check in item.validation_results if check.passed),
            -(len(item.files_changed)),
        ),
        reverse=True,
    )


def _ensure_snapshot_commit(worktree: Path, message: str) -> str:
    status = _relative_git_status(worktree)
    if not status:
        return read_head_ref(worktree)
    add_completed = _git("add", "-A", cwd=worktree)
    if add_completed.returncode != 0:
        raise RuntimeError(add_completed.stderr.strip() or "git add failed")
    commit_completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Atelier Swarm",
            "-c",
            "user.email=swarm@atelier.local",
            "commit",
            "--allow-empty",
            "--no-verify",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_completed.returncode != 0:
        raise RuntimeError(commit_completed.stderr.strip() or "git commit failed")
    return read_head_ref(worktree)


def _tail_lines(path: Path, tail: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-tail:])


def _preview_file(path: Path, tail: int = 5) -> tuple[str, datetime | None]:
    if not path.exists():
        return "", None
    preview = _tail_lines(path, tail).strip()
    last_output_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return preview[-800:], last_output_at


def _update_child_activity(child: SwarmChildState) -> bool:
    stdout_preview, stdout_time = _preview_file(Path(child.stdout_path))
    stderr_preview, stderr_time = _preview_file(Path(child.stderr_path))
    latest_time = max(
        [item for item in (stdout_time, stderr_time) if item is not None],
        default=None,
    )
    activity = ""
    for preview in (stderr_preview, stdout_preview):
        lines = [line.strip() for line in preview.splitlines() if line.strip()]
        if lines:
            activity = lines[-1][:240]
            break
    updated = (
        child.stdout_preview != stdout_preview
        or child.stderr_preview != stderr_preview
        or child.current_activity != activity
        or child.last_output_at != latest_time
    )
    child.stdout_preview = stdout_preview
    child.stderr_preview = stderr_preview
    child.current_activity = activity
    child.last_output_at = latest_time
    return updated


def _children_for_wave(state: SwarmRunState, wave_index: int) -> list[SwarmChildState]:
    return [child for child in state.children if child.wave_index == wave_index]


def _latest_wave(state: SwarmRunState, wave_index: int) -> SwarmWaveState:
    for wave in state.waves:
        if wave.wave_index == wave_index:
            return wave
    raise RuntimeError(f"wave {wave_index} not found")


def _refresh_child_result(
    children: list[SwarmChildState],
    child_id: str,
) -> SwarmChildState | None:
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


def _python_cli_invocation() -> list[str]:
    return [sys.executable, "-m", "atelier.gateway.cli"]


def list_swarm_runs(root: Path) -> list[SwarmRunState]:
    runs_root = Path(root) / "swarm" / "runs"
    if not runs_root.exists():
        return []
    states: list[SwarmRunState] = []
    for state_path in sorted(runs_root.glob("*/state.json")):
        with contextlib.suppress(OSError, json.JSONDecodeError, ValueError):
            states.append(load_swarm_state(state_path))
    return sorted(states, key=lambda item: item.created_at, reverse=True)


def read_swarm_log(
    root: Path,
    run_id: str,
    *,
    child_id: str | None = None,
    stderr: bool = False,
    tail: int = 40,
) -> str:
    state = load_swarm_state(resolve_state_path(root, run_id))
    if child_id is None:
        log_path = Path(state.coordinator_log_path or swarm_run_dir(root, run_id) / "coordinator.log")
    else:
        child = next((item for item in state.children if item.child_id == child_id), None)
        if child is None:
            raise RuntimeError(f"unknown child id: {child_id}")
        log_path = Path(child.stderr_path if stderr else child.stdout_path)
    content = _tail_lines(log_path, tail).strip()
    return content or f"No log output yet at {log_path}"


def build_swarm_export_payload(state: SwarmRunState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "status": state.status,
        "mode": state.mode,
        "runner_name": state.runner_name,
        "runner_model": state.runner_model,
        "base_ref": state.base_ref,
        "base_snapshot_ref": state.base_snapshot_ref,
        "integration_base_ref": state.integration_base_ref,
        "artifact_root": state.artifact_root,
        "base_snapshot_artifact": (
            state.base_snapshot_artifact.model_dump(mode="json") if state.base_snapshot_artifact is not None else None
        ),
        "accepted_child_ids": list(state.accepted_child_ids),
        "accepted_commits": [item.model_dump(mode="json") for item in state.accepted_commits],
        "waves": [
            {
                "wave_index": wave.wave_index,
                "status": wave.status,
                "max_runs": wave.max_runs,
                "planned_runs": wave.planned_runs,
                "planning_mode": wave.planning_mode,
                "primary_winner_child_id": wave.primary_winner_child_id,
                "accepted_child_ids": list(wave.accepted_child_ids),
                "rejected_child_ids": list(wave.rejected_child_ids),
                "manifest_artifact": (
                    wave.manifest_artifact.model_dump(mode="json") if wave.manifest_artifact is not None else None
                ),
            }
            for wave in state.waves
        ],
        "artifacts": [artifact.model_dump(mode="json") for artifact in state.export_artifacts],
        "transplant_commands": list(state.transplant_commands),
    }


def build_swarm_apply_payload(
    state: SwarmRunState,
    *,
    wave_index: int | None = None,
    child_id: str | None = None,
) -> dict[str, object]:
    selected_commits = list(state.accepted_commits)
    if wave_index is not None:
        selected_ids = {child_id for child_id in _latest_wave(state, wave_index).accepted_child_ids}
        selected_commits = [item for item in selected_commits if item.child_id in selected_ids]
    if child_id is not None:
        selected_commits = [item for item in selected_commits if item.child_id == child_id]
    cherry_pick_refs = [item.commit_ref for item in selected_commits if item.commit_ref]
    commands: list[str] = []
    if cherry_pick_refs:
        commands.append(f"git cherry-pick {' '.join(cherry_pick_refs)}")
    commands.extend(f"git apply {shlex.quote(item.patch_path)}" for item in selected_commits if item.patch_path)
    return {
        "run_id": state.run_id,
        "wave_index": wave_index,
        "child_id": child_id,
        "base_snapshot_ref": state.base_snapshot_ref,
        "integration_base_ref": state.integration_base_ref,
        "selected_commits": [item.model_dump(mode="json") for item in selected_commits],
        "commands": commands,
        "artifacts": [artifact.model_dump(mode="json") for item in selected_commits for artifact in item.artifacts],
    }


def initialize_swarm_run(
    *,
    root: Path,
    repo_root: Path,
    spec_path: Path,
    runner_name: str = "custom",
    runner_model: str = "",
    child_command: list[str],
    runs: int,
    validation_commands: list[str],
    keep_worktrees: bool,
    detached: bool,
    continuous: bool = False,
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
    integration_worktree = manager.create_worktree(
        run_id=run_id,
        child_id="integration",
        ref=read_head_ref(repo_root),
    )
    manager.sync_dirty_state(base_worktree=repo_root, child_worktree=integration_worktree)
    integration_base_ref = _ensure_snapshot_commit(
        integration_worktree,
        "[swarm] base snapshot",
    )
    artifact_root = run_dir / "artifacts"
    state = SwarmRunState(
        run_id=run_id,
        status="pending",
        mode="continuous" if continuous else "single",
        repo_root=str(repo_root),
        base_worktree=str(repo_root),
        base_ref=read_head_ref(repo_root),
        base_snapshot_ref=integration_base_ref,
        worktree_pool=str(worktree_pool),
        integration_worktree=str(integration_worktree),
        integration_base_ref=integration_base_ref,
        artifact_root=str(artifact_root),
        spec_source_path=str(spec_path),
        copied_spec_path=str(spec_copy),
        runner_name=runner_name,
        runner_model=runner_model,
        child_command=list(child_command),
        validation_commands=list(validation_commands),
        runs=runs,
        max_runs=runs,
        keep_worktrees=keep_worktrees,
        detached=detached,
        dirty_paths=dirty_paths,
        limitations=[
            "Each child gets an isolated git worktree and ATELIER_ROOT.",
            "The agent command must consume the provided worktree/spec env vars to use Atelier MCP/runtime inside each child.",
            "Accepted child patches are merged onto an integration worktree in score order; later waves branch from that accepted base.",
            "max_runs is the per-wave cap; planned_runs records how many children the coordinator actually launched in that wave.",
        ],
    )
    _write_run_base_snapshot_manifest(state, run_dir)
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
            "ATELIER_SWARM_BASE_REF": state.integration_base_ref,
        }
    )
    return env


def _prepare_wave(state: SwarmRunState, root: Path, wave_index: int) -> SwarmWaveState:
    manager = SwarmWorktreeManager(
        repo_root=Path(state.repo_root),
        pool_root=Path(state.worktree_pool),
    )
    planning_mode, planned_runs, planning_reason = _plan_wave_runs(state, wave_index)
    state.max_runs = max(state.max_runs or state.runs or 1, 1)
    state.runs = state.max_runs
    state.planning_mode = planning_mode
    state.fan_out_reason = planning_reason
    wave = SwarmWaveState(
        wave_index=wave_index,
        max_runs=state.max_runs,
        planned_runs=planned_runs,
        planning_mode=planning_mode,
        planning_reason=planning_reason,
    )
    spec_copy = Path(state.copied_spec_path)
    for index in range(1, wave.planned_runs + 1):
        child_id = f"wave-{wave_index:02d}-run-{index:02d}"
        child_dir = _child_run_dir(root, state.run_id, child_id)
        child_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = manager.create_worktree(
            run_id=state.run_id,
            child_id=child_id,
            ref=state.integration_base_ref,
        )
        child_spec_path = worktree_path / ".atelier-swarm" / "program.md"
        child_spec_path.parent.mkdir(parents=True, exist_ok=True)
        child_spec_path.write_text(spec_copy.read_text(encoding="utf-8"), encoding="utf-8")
        child_root = child_dir / "atelier-root"
        store = create_store(child_root)
        store.init()
        child = SwarmChildState(
            child_id=child_id,
            label=f"candidate-{index}",
            wave_index=wave_index,
            worktree_path=str(worktree_path),
            atelier_root=str(child_root),
            run_dir=str(child_dir),
            spec_path=str(child_spec_path),
            result_path=str(child_dir / "result.json"),
            stdout_path=str(child_dir / "child.stdout.log"),
            stderr_path=str(child_dir / "child.stderr.log"),
            metadata_path=str(child_dir / "child-metadata.json"),
            patch_path=str(child_dir / "candidate.patch"),
        )
        state.children.append(child)
        wave.child_ids.append(child_id)
    state.waves.append(wave)
    state.current_wave = wave_index
    return wave


def _run_wave_children(root: Path, state_path: Path, wave_index: int) -> SwarmRunState:
    state = load_swarm_state(state_path)
    procs: dict[str, tuple[subprocess.Popen[str], contextlib.ExitStack]] = {}
    wave_children = _children_for_wave(state, wave_index)
    for child in wave_children:
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
        state = load_swarm_state(state_path)
        if state.stop_requested:
            for child_id, (proc, stack) in procs.items():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                child = next(item for item in state.children if item.child_id == child_id)
                child.status = "stopped"
                child.finished_at = _utcnow()
                stack.close()
            procs.clear()
            wave = _latest_wave(state, wave_index)
            wave.status = "stopped"
            wave.finished_at = _utcnow()
            wave.summary = "Stopped by user."
            save_swarm_state(state_path, state)
            return state

        changed = False
        for child in _children_for_wave(state, wave_index):
            if child.child_id in procs:
                changed = _update_child_activity(child) or changed

        finished: list[str] = []
        for child_id, (proc, _stack) in procs.items():
            if proc.poll() is not None:
                finished.append(child_id)
        for child_id in finished:
            proc, stack = procs.pop(child_id)
            child_state = _refresh_child_result(state.children, child_id)
            if child_state is None:
                child_state = next(item for item in state.children if item.child_id == child_id)
                child_state.status = "failed" if proc.returncode else "success"
                child_state.exit_code = proc.returncode
                child_state.finished_at = _utcnow()
            _update_child_activity(child_state)
            stack.close()
            changed = True

        if changed:
            save_swarm_state(state_path, state)
        time.sleep(0.2)

    return load_swarm_state(state_path)


def _write_child_patch(child: SwarmChildState) -> Path | None:
    patch_path = Path(child.patch_path)
    completed = _git("diff", "--binary", cwd=Path(child.worktree_path))
    if completed.returncode != 0:
        child.acceptance_note = completed.stderr.strip() or "Failed to build patch."
        return None
    if not completed.stdout.strip():
        child.acceptance_note = "No diff to apply."
        return None
    patch_path.write_text(completed.stdout, encoding="utf-8")
    return patch_path


def _can_apply_patch(worktree: Path, patch_path: Path) -> bool:
    completed = _git("apply", "--check", "--3way", str(patch_path), cwd=worktree)
    return completed.returncode == 0


def _apply_patch_to_worktree(worktree: Path, patch_path: Path) -> None:
    completed = _git("apply", "--3way", str(patch_path), cwd=worktree)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git apply failed")


def _recreate_integration_worktree(state: SwarmRunState) -> Path:
    manager = SwarmWorktreeManager(
        repo_root=Path(state.repo_root),
        pool_root=Path(state.worktree_pool),
    )
    current = Path(state.integration_worktree)
    manager.remove_worktree(current)
    recreated = manager.create_worktree(
        run_id=state.run_id,
        child_id="integration",
        ref=state.integration_base_ref,
    )
    state.integration_worktree = str(recreated)
    return recreated


def apply_wave_candidates(
    state: SwarmRunState,
    wave_children: list[SwarmChildState],
    wave: SwarmWaveState,
) -> bool:
    integration = Path(state.integration_worktree)
    ranked = rank_children(wave_children)
    accepted: list[str] = []
    rejected: list[str] = []
    accepted_commits: list[SwarmAcceptedCommit] = []
    run_dir = Path(state.copied_spec_path).resolve().parent

    for child in ranked:
        if child.status != "success":
            child.accepted = False
            child.acceptance_note = "Child command did not succeed."
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        if not child.files_changed:
            child.accepted = False
            child.acceptance_note = "Child produced no code changes."
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        if any(not check.passed for check in child.validation_results):
            child.accepted = False
            child.acceptance_note = "Validation failed."
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        patch_path = _write_child_patch(child)
        if patch_path is None:
            child.accepted = False
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        if not _can_apply_patch(integration, patch_path):
            child.accepted = False
            child.acceptance_note = "Patch conflicted with the already accepted integration state."
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        try:
            _apply_patch_to_worktree(integration, patch_path)
        except RuntimeError:
            integration = _recreate_integration_worktree(state)
            child.accepted = False
            child.acceptance_note = "Patch conflicted with the already accepted integration state."
            rejected.append(child.child_id)
            wave.rejected_child_notes[child.child_id] = child.acceptance_note
            continue
        child.accepted = True
        child.acceptance_note = "Applied to integration base."
        state.integration_base_ref = _ensure_snapshot_commit(
            integration,
            f"[swarm] wave {wave.wave_index:02d} accept {child.child_id}",
        )
        patch_artifact = _artifact_ref(
            run_dir,
            patch_path,
            kind="patch",
            label=f"{child.child_id} patch",
            metadata={
                "wave_index": wave.wave_index,
                "child_id": child.child_id,
                "commit_ref": state.integration_base_ref,
            },
        )
        apply_commands = [
            f"git cherry-pick {state.integration_base_ref}",
            f"git apply {shlex.quote(str(patch_path))}",
        ]
        accepted_commit = SwarmAcceptedCommit(
            order=len(state.accepted_commits) + len(accepted_commits) + 1,
            child_id=child.child_id,
            commit_ref=state.integration_base_ref,
            summary=child.summary,
            files_changed=list(child.files_changed),
            patch_path=str(patch_path),
            score=child.score,
            artifacts=[patch_artifact],
            apply_commands=apply_commands,
        )
        child.accepted_commit_ref = accepted_commit.commit_ref
        child.accepted_order = accepted_commit.order
        child.export_artifacts = [patch_artifact]
        child.apply_commands = apply_commands
        accepted_commits.append(accepted_commit)
        _upsert_run_artifact(state, patch_artifact)
        accepted.append(child.child_id)

    wave.accepted_child_ids = accepted
    wave.rejected_child_ids = rejected
    wave.accepted_commits = accepted_commits
    wave.primary_winner_child_id = accepted[0] if accepted else None
    wave.finished_at = _utcnow()
    if accepted:
        for child_id in accepted:
            if child_id not in state.accepted_child_ids:
                state.accepted_child_ids.append(child_id)
        state.accepted_commits.extend(accepted_commits)
        state.primary_winner_child_id = accepted[0]
        state.winner_child_id = accepted[0]
        winner = next(child for child in ranked if child.child_id == accepted[0])
        state.ranking_notes = winner.score_breakdown
        wave.status = "applied"
        wave.summary = f"Accepted {len(accepted)} child patch(es)."
        _refresh_transplant_commands(state)
        _write_wave_manifest(state, wave)
        _write_run_acceptance_manifest(state)
        return True
    wave.status = "no-improvement"
    wave.summary = "No child patch was accepted in this wave."
    _refresh_transplant_commands(state)
    _write_wave_manifest(state, wave)
    _write_run_acceptance_manifest(state)
    return False


def launch_swarm_children(root: Path, state_path: Path) -> SwarmRunState:
    state = load_swarm_state(state_path)
    state.status = "running"
    state.coordinator_pid = os.getpid()
    save_swarm_state(state_path, state)
    try:
        while True:
            state = load_swarm_state(state_path)
            if state.stop_requested:
                state.status = "stopped"
                state.stop_reason = state.stop_reason or "Stopped by user."
                save_swarm_state(state_path, state)
                break

            wave_index = state.current_wave + 1
            wave = _prepare_wave(state, root, wave_index)
            save_swarm_state(state_path, state)

            state = _run_wave_children(root, state_path, wave_index)
            wave = _latest_wave(state, wave_index)
            if state.stop_requested:
                state.status = "stopped"
                state.stop_reason = state.stop_reason or "Stopped by user."
                save_swarm_state(state_path, state)
                break

            wave_children = _children_for_wave(state, wave_index)
            accepted_any = apply_wave_candidates(state, wave_children, wave)
            save_swarm_state(state_path, state)

            if state.mode == "single":
                state.status = "success" if accepted_any else "failed"
                state.stop_reason = "Single-wave swarm completed."
                save_swarm_state(state_path, state)
                break

            if not accepted_any:
                state.status = "success"
                state.stop_reason = f"Stopped after wave {wave_index}: no accepted improvements."
                save_swarm_state(state_path, state)
                break

        state = load_swarm_state(state_path)
        if not state.keep_worktrees:
            cleanup_swarm_run(state)
            state.ranking_notes.append("Removed child worktrees because --cleanup was enabled.")
            save_swarm_state(state_path, state)
        return state
    except KeyboardInterrupt:
        stop_swarm_run(root=root, state_path=state_path, cleanup=False)
        state = load_swarm_state(state_path)
        state.status = "stopped"
        state.stop_reason = "Interrupted."
        save_swarm_state(state_path, state)
        raise


def stop_swarm_run(*, root: Path, state_path: Path, cleanup: bool) -> SwarmRunState:
    state = load_swarm_state(state_path)
    state.stop_requested = True
    if not state.stop_reason:
        state.stop_reason = "Stop requested by user."
    for child in state.children:
        if child.status == "running":
            _kill_pid(child.pid)
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
    manager = SwarmWorktreeManager(
        repo_root=Path(state.repo_root),
        pool_root=Path(state.worktree_pool),
    )
    for child in state.children:
        manager.remove_worktree(Path(child.worktree_path))
    if state.integration_worktree:
        manager.remove_worktree(Path(state.integration_worktree))


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
        ledger.record_command(
            " ".join(shlex.quote(token) for token in command),
            ok=exit_code == 0,
        )
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
                "validation_results": _merge_validation_results(
                    validation_results,
                    metadata.get("validation_results"),
                ),
                "summary": str(metadata.get("summary") or _summarize_output(stdout_path, stderr_path)),
                "error": str(metadata.get("error") or ""),
                "token_count": _coerce_int(metadata.get("token_count"), token_count),
                "cost_usd": _coerce_float(metadata.get("cost_usd"), cost_usd),
                "duration_seconds": round(time.perf_counter() - started, 3),
                "finished_at": _utcnow(),
            }
        )
        _update_child_activity(result)
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
        _update_child_activity(child)
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
    failed = [child for child in state.children if child.status == "failed"]
    stopped = [child for child in state.children if child.status == "stopped"]
    lines = [
        f"run_id: {state.run_id}",
        f"status: {state.status}",
        f"mode: {state.mode}",
        f"runner: {state.runner_name}",
        f"runner_model: {state.runner_model or '(default)'}",
        f"max_runs: {state.max_runs or state.runs}",
        f"current_wave: {state.current_wave}",
        f"accepted_children: {len(state.accepted_child_ids)}",
        f"children: {len(state.children)}",
    ]
    lines.append(f"child_command: {' '.join(state.child_command)}")
    if state.stop_reason:
        lines.append(f"stop_reason: {state.stop_reason}")
    if state.integration_worktree:
        lines.append(f"integration_worktree: {state.integration_worktree}")
    if state.base_snapshot_ref:
        lines.append(f"base_snapshot_ref: {state.base_snapshot_ref}")
    if state.primary_winner_child_id is not None:
        lines.append(f"primary_winner: {state.primary_winner_child_id}")
    if state.fan_out_reason:
        lines.append(f"fan_out_reason: {state.fan_out_reason}")
    running = [child for child in state.children if child.status == "running"]
    if running:
        lines.append("running_children:")
        for child in running[:8]:
            activity = child.current_activity or "running"
            lines.append(f"  - {child.child_id}: {activity}")
    recent_wave = state.waves[-1] if state.waves else None
    if recent_wave is not None:
        lines.append(
            f"latest_wave: {recent_wave.wave_index} status={recent_wave.status} planned={recent_wave.planned_runs}/{recent_wave.max_runs} accepted={len(recent_wave.accepted_child_ids)}"
        )
        if recent_wave.summary:
            lines.append(f"latest_wave_summary: {recent_wave.summary}")
    if state.transplant_commands:
        lines.append("transplant_commands:")
        for command in state.transplant_commands[:8]:
            lines.append(f"  - {command}")
    if failed:
        lines.append("failed_children:")
        for child in failed[:8]:
            detail = child.summary or child.error or child.acceptance_note or "failed"
            lines.append(f"  - {child.child_id}: {detail}")
    if stopped:
        lines.append("stopped_children:")
        for child in stopped[:8]:
            detail = child.summary or child.error or child.acceptance_note or "stopped"
            lines.append(f"  - {child.child_id}: {detail}")
    return "\n".join(lines)


def discover_repo_root(cwd: Path) -> Path:
    return git_repo_root(cwd)
