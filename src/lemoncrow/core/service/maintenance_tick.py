"""Periodic local-maintenance tick.

The retired ``servicectl`` daemon used to run six periodic duties on its own
schedule: session auto-import, recall indexing, stale-workspace pruning,
knowledge-block consolidation, jobs/review retention cleanup, and (opt-in)
context-optimization automation. Retiring that daemon (see git history of
``infra/runtime/servicectl_lifecycle.py``) dropped every one of them --
nothing else ever called them again. This module restores them, driven from
``_run_worker_tick_safe`` (see ``gateway/adapters/mcp_server.py``) instead of
a standalone process, the same way ``public_rollup.maybe_flush_public_rollup``
was restored.

Design constraints this follows:

- No new CLI command. Every duty already has one (``lc import``,
  ``lc session recall index``, ``lc code prune``, the job queue); this module
  only decides *when* to call them, the way servicectl's tick used to.
- Never blocks the caller's request. The caller (``_run_worker_tick_safe``)
  already runs in a daemon thread spawned fire-and-forget from the MCP tool
  path, so work done here never holds up a tool response. Within that
  thread, ``run_maintenance_tick`` takes a single non-blocking lock attempt
  (see ``_acquire_lock``): if another thread or process already holds it,
  this call returns immediately instead of waiting, so overlapping ticks
  from concurrent tool calls never pile up or serialize on each other.
- Each duty is independently exception-guarded so one stuck subprocess or
  failing job cannot suppress the others, the rollup flush, or job draining.
- Each duty is a cheap no-op (one JSON read) when not yet due, so calling
  this on every tick (throttled to ~once/30s by the caller) is fine.

lc-debt: subprocess timeouts here are not escalated on repeated failure the
way the old servicectl tick's ``_scaled_timeout``/``_note_subprocess_outcome``
did. Failed duties use a fixed five-minute retry delay instead. Add adaptive
budgets back if large stores are observed timing out repeatedly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS, JOB_OPTIMIZE, JOB_RETENTION_CLEANUP

logger = logging.getLogger("lemoncrow.product.service.maintenance_tick")

_STATE_RELPATH = ("maintenance", "tick_state.json")
_LOCK_RELPATH = ("maintenance", "tick.lock")
# A valid tick can spend up to 20 minutes inside the three bounded subprocesses
# (5m import + 5m recall + 10m prune). Keep the stale threshold comfortably
# above that so a slow but healthy tick is never stolen by the next worker.
_LOCK_STALE_SECONDS = 30 * 60

# Same cadence the retired servicectl tick used (its --maintenance-interval-seconds
# default). Governs recall indexing and the three job-enqueue duties.
MAINTENANCE_INTERVAL_SECONDS = 300
SESSION_IMPORT_INTERVAL_SECONDS = 1_800
WORKSPACE_PRUNE_INTERVAL_SECONDS = 86_400
WORKSPACE_PRUNE_MAX_AGE_DAYS = 10
_FAILURE_RETRY_SECONDS = 300

SESSION_IMPORT_TIMEOUT_SECONDS = 300
RECALL_INDEX_TIMEOUT_SECONDS = 300
WORKSPACE_PRUNE_TIMEOUT_SECONDS = 600

SESSION_IMPORT_KEY = "session_import"
RECALL_INDEX_KEY = "index_recall_sessions"
WORKSPACE_PRUNE_KEY = "prune_workspaces"
CONSOLIDATE_KEY = "consolidate_blocks"
RETENTION_KEY = "retention_cleanup"
OPTIMIZE_KEY = "optimize_automation"

# Jobs table / archived-review retention window -- matches worker.py's own default.
DEFAULT_JOB_RETENTION_DAYS = 14


# ---------------------------------------------------------------------------
# Durable per-key due-time state (mirrors public_rollup's state/lock pattern)
# ---------------------------------------------------------------------------


def _state_path(root: Path) -> Path:
    return root.joinpath(*_STATE_RELPATH)


def _lock_path(root: Path) -> Path:
    return root.joinpath(*_LOCK_RELPATH)


def _read_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(root: Path, payload: dict[str, Any]) -> None:
    path = _state_path(root)
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _due(state: dict[str, Any], key: str, *, interval_seconds: int, now: datetime) -> bool:
    last_raw = state.get(key)
    if not isinstance(last_raw, str) or not last_raw.strip():
        return True
    try:
        last = datetime.fromisoformat(last_raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() >= interval_seconds


def _failure_key(key: str) -> str:
    return f"{key}_last_failure_at"


def _in_retry_wait(state: dict[str, Any], key: str, *, now: datetime) -> bool:
    """True if this duty failed recently and hasn't waited out
    ``_FAILURE_RETRY_SECONDS`` yet. Shared by both duty kinds (subprocess and
    job-enqueue) so a repeatedly-failing duty of either kind backs off at the
    same fixed cadence instead of retrying on every ~30s worker tick."""
    failed_raw = state.get(_failure_key(key))
    if not (isinstance(failed_raw, str) and failed_raw.strip()):
        return False
    try:
        failed_at = datetime.fromisoformat(failed_raw)
    except ValueError:
        return False
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=UTC)
    return (now - failed_at).total_seconds() < _FAILURE_RETRY_SECONDS


def _mark_succeeded(state: dict[str, Any], key: str, *, now: datetime) -> None:
    state[key] = now.isoformat()
    state.pop(_failure_key(key), None)


def _mark_failed(state: dict[str, Any], key: str, *, now: datetime) -> None:
    # Left un-advanced on failure (state[key] untouched) so the duty stays
    # "due"; _in_retry_wait is what actually paces the retries.
    state[_failure_key(key)] = now.isoformat()


def _acquire_lock(root: Path, now: datetime) -> bool:
    """Single non-blocking attempt. A lock older than ``_LOCK_STALE_SECONDS``
    (crashed holder) is reclaimed; otherwise a busy lock means "skip this
    tick", never "wait for it"."""
    path = _lock_path(root)
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = now.timestamp() - path.stat().st_mtime
            except OSError:
                return False
            if age < _LOCK_STALE_SECONDS:
                return False
            with suppress(OSError):
                path.unlink()
            continue
        except OSError:
            return False
        with suppress(OSError):
            os.write(fd, f"{os.getpid()} {now.isoformat()}".encode())
        with suppress(OSError):
            os.close(fd)
        return True
    return False


def _release_lock(root: Path) -> None:
    with suppress(OSError):
        _lock_path(root).unlink()


# ---------------------------------------------------------------------------
# Subprocess-backed duties (import, recall index, prune) -- out-of-process on
# purpose, same as the retired servicectl tick: keeps JSON parsing, embedding
# work and rmtree walks off the caller's heap/thread budget.
# ---------------------------------------------------------------------------


def _run_cli_subprocess(cmd: list[str], *, timeout: int, what: str) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("%s subprocess timed out after %ds", what, timeout)
        return False
    except OSError:
        logger.exception("failed to launch %s subprocess", what)
        return False
    if result.returncode != 0:
        logger.warning(
            "%s subprocess failed (rc=%d): %s",
            what,
            result.returncode,
            result.stderr[-500:].decode("utf-8", errors="replace").strip(),
        )
        return False
    return True


def _maybe_run_subprocess_duty(
    root: Path,
    state: dict[str, Any],
    *,
    key: str,
    interval_seconds: int,
    timeout: int,
    cmd: list[str],
    what: str,
    now: datetime,
) -> str:
    if not _due(state, key, interval_seconds=interval_seconds, now=now):
        return "not_due"
    if _in_retry_wait(state, key, now=now):
        return "retry_wait"
    try:
        ok = _run_cli_subprocess(cmd, timeout=timeout, what=what)
    except Exception:
        logger.warning("%s duty raised", what, exc_info=True)
        _mark_failed(state, key, now=now)
        return "error"
    if ok:
        _mark_succeeded(state, key, now=now)
        return "ran"
    # Do not mark the duty successful, but also do not hammer a broken CLI on
    # every ~30s worker tick: _in_retry_wait paces retries to once per
    # _FAILURE_RETRY_SECONDS until one attempt succeeds.
    _mark_failed(state, key, now=now)
    return "failed"


# ---------------------------------------------------------------------------
# Job-queue-backed duties (consolidation, retention cleanup, optimize) -- just
# enqueue; the job-drain loop already running in _run_worker_tick_safe (or the
# next tick's) processes them in-process, no extra subprocess needed.
# ---------------------------------------------------------------------------


def _maybe_enqueue_job_duty(
    store: Any,
    state: dict[str, Any],
    *,
    key: str,
    interval_seconds: int,
    job_type: str,
    payload: dict[str, Any],
    now: datetime,
) -> str:
    if not _due(state, key, interval_seconds=interval_seconds, now=now):
        return "not_due"
    if _in_retry_wait(state, key, now=now):
        return "retry_wait"
    try:
        active = [
            job
            for job in store.jobs.list_jobs(job_type=job_type, limit=200)
            if job["status"] in {"pending", "running", "failed"}
        ]
        if active:
            # Already has unfinished work queued; don't pile on a duplicate.
            _mark_succeeded(state, key, now=now)
            return "already_queued"
        store.jobs.enqueue_job(job_type, payload)
        _mark_succeeded(state, key, now=now)
        return "enqueued"
    except Exception:
        # Same backoff as the subprocess duties: a DB that's locked/unreachable
        # gets retried every _FAILURE_RETRY_SECONDS, not every ~30s tick.
        logger.warning("%s enqueue raised", job_type, exc_info=True)
        _mark_failed(state, key, now=now)
        return "error"


def run_maintenance_tick(root: str | Path, *, store: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Run whichever of the six retired periodic duties are due.

    Never raises. Lock acquisition is non-blocking, and callers run this in a
    daemon worker thread so maintenance cannot delay the MCP response; due
    subprocess duties themselves run synchronously inside that worker thread.
    """
    root_path = Path(root)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if not _acquire_lock(root_path, now):
        return {"ran": False, "reason": "locked"}
    try:
        state = _read_state(root_path)
        results: dict[str, str] = {
            SESSION_IMPORT_KEY: _maybe_run_subprocess_duty(
                root_path,
                state,
                key=SESSION_IMPORT_KEY,
                interval_seconds=SESSION_IMPORT_INTERVAL_SECONDS,
                timeout=SESSION_IMPORT_TIMEOUT_SECONDS,
                cmd=[sys.executable, "-m", "lemoncrow.gateway.cli", "--root", str(root_path), "import", "--json"],
                what="session import",
                now=now,
            ),
            RECALL_INDEX_KEY: _maybe_run_subprocess_duty(
                root_path,
                state,
                key=RECALL_INDEX_KEY,
                interval_seconds=MAINTENANCE_INTERVAL_SECONDS,
                timeout=RECALL_INDEX_TIMEOUT_SECONDS,
                cmd=[
                    sys.executable,
                    "-m",
                    "lemoncrow.gateway.cli",
                    "--root",
                    str(root_path),
                    "session",
                    "recall",
                    "index",
                    "--json",
                ],
                what="recall index",
                now=now,
            ),
            WORKSPACE_PRUNE_KEY: _maybe_run_subprocess_duty(
                root_path,
                state,
                key=WORKSPACE_PRUNE_KEY,
                interval_seconds=WORKSPACE_PRUNE_INTERVAL_SECONDS,
                timeout=WORKSPACE_PRUNE_TIMEOUT_SECONDS,
                cmd=[
                    sys.executable,
                    "-m",
                    "lemoncrow.gateway.cli",
                    "--root",
                    str(root_path),
                    "code",
                    "prune",
                    "--store-root",
                    str(root_path),
                    "--max-age-days",
                    str(WORKSPACE_PRUNE_MAX_AGE_DAYS),
                    "--json",
                ],
                what="workspace prune",
                now=now,
            ),
        }
        if store is not None:
            results[CONSOLIDATE_KEY] = _maybe_enqueue_job_duty(
                store,
                state,
                key=CONSOLIDATE_KEY,
                interval_seconds=MAINTENANCE_INTERVAL_SECONDS,
                job_type=JOB_CONSOLIDATE_BLOCKS,
                payload={"dry_run": False, "source": "maintenance_tick"},
                now=now,
            )
            results[RETENTION_KEY] = _maybe_enqueue_job_duty(
                store,
                state,
                key=RETENTION_KEY,
                interval_seconds=MAINTENANCE_INTERVAL_SECONDS,
                job_type=JOB_RETENTION_CLEANUP,
                payload={"days": DEFAULT_JOB_RETENTION_DAYS, "source": "maintenance_tick"},
                now=now,
            )
            automation_enabled = False
            with suppress(Exception):
                from lemoncrow.pro.capabilities.optimization.policy import load_automation_config

                automation_enabled = bool(load_automation_config(root_path).enabled)
            if automation_enabled:
                results[OPTIMIZE_KEY] = _maybe_enqueue_job_duty(
                    store,
                    state,
                    key=OPTIMIZE_KEY,
                    interval_seconds=MAINTENANCE_INTERVAL_SECONDS,
                    job_type=JOB_OPTIMIZE,
                    payload={"days": 7, "host": None, "source": "maintenance_tick"},
                    now=now,
                )
        _write_state(root_path, state)
        return {"ran": True, "results": results}
    finally:
        _release_lock(root_path)
