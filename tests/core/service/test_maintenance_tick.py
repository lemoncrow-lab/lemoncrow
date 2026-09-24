from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.core.service import maintenance_tick as mt


def test_live_long_running_lock_is_not_stolen(tmp_path: Path) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    lock = mt._lock_path(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("holder", encoding="utf-8")

    # Twenty minutes is still a legitimate tick: the three bounded subprocesses
    # can consume 5m + 5m + 10m sequentially.
    live_mtime = now.timestamp() - 20 * 60
    os.utime(lock, (live_mtime, live_mtime))
    assert mt._acquire_lock(tmp_path, now) is False
    assert lock.exists()

    stale_mtime = now.timestamp() - (mt._LOCK_STALE_SECONDS + 1)
    os.utime(lock, (stale_mtime, stale_mtime))
    assert mt._acquire_lock(tmp_path, now) is True
    mt._release_lock(tmp_path)


def test_failed_subprocess_duty_waits_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    state: dict[str, object] = {}
    calls: list[list[str]] = []

    def fail(cmd: list[str], *, timeout: int, what: str) -> bool:
        del timeout, what
        calls.append(cmd)
        return False

    monkeypatch.setattr(mt, "_run_cli_subprocess", fail)
    kwargs = dict(
        root=tmp_path,
        state=state,
        key=mt.SESSION_IMPORT_KEY,
        interval_seconds=mt.SESSION_IMPORT_INTERVAL_SECONDS,
        timeout=mt.SESSION_IMPORT_TIMEOUT_SECONDS,
        cmd=["lc", "import"],
        what="session import",
    )

    assert mt._maybe_run_subprocess_duty(**kwargs, now=now) == "failed"
    assert mt._maybe_run_subprocess_duty(**kwargs, now=now + timedelta(seconds=30)) == "retry_wait"
    assert len(calls) == 1
    assert (
        mt._maybe_run_subprocess_duty(
            **kwargs,
            now=now + timedelta(seconds=mt._FAILURE_RETRY_SECONDS + 1),
        )
        == "failed"
    )
    assert len(calls) == 2


def test_success_clears_failure_and_restores_normal_cadence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    failure_key = f"{mt.SESSION_IMPORT_KEY}_last_failure_at"
    state: dict[str, object] = {failure_key: (now - timedelta(minutes=10)).isoformat()}
    calls = 0

    def succeed(cmd: list[str], *, timeout: int, what: str) -> bool:
        nonlocal calls
        del cmd, timeout, what
        calls += 1
        return True

    monkeypatch.setattr(mt, "_run_cli_subprocess", succeed)
    kwargs = dict(
        root=tmp_path,
        state=state,
        key=mt.SESSION_IMPORT_KEY,
        interval_seconds=mt.SESSION_IMPORT_INTERVAL_SECONDS,
        timeout=mt.SESSION_IMPORT_TIMEOUT_SECONDS,
        cmd=["lc", "import"],
        what="session import",
    )

    assert mt._maybe_run_subprocess_duty(**kwargs, now=now) == "ran"
    assert failure_key not in state
    assert mt.SESSION_IMPORT_KEY in state
    assert mt._maybe_run_subprocess_duty(**kwargs, now=now + timedelta(minutes=5)) == "not_due"
    assert calls == 1


def test_cli_timeout_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs["timeout"]))

    monkeypatch.setattr(mt.subprocess, "run", timeout)
    assert mt._run_cli_subprocess(["lc", "import"], timeout=7, what="session import") is False


def test_tick_continues_after_one_subprocess_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def run(cmd: list[str], *, timeout: int, what: str) -> bool:
        del cmd, timeout
        calls.append(what)
        return what != "session import"

    monkeypatch.setattr(mt, "_run_cli_subprocess", run)
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    result = mt.run_maintenance_tick(tmp_path, now=now)

    assert result["ran"] is True
    assert result["results"] == {
        mt.SESSION_IMPORT_KEY: "failed",
        mt.RECALL_INDEX_KEY: "ran",
        mt.WORKSPACE_PRUNE_KEY: "ran",
    }
    assert calls == ["session import", "recall index", "workspace prune"]
    state = mt._read_state(tmp_path)
    assert mt.SESSION_IMPORT_KEY not in state
    assert state[f"{mt.SESSION_IMPORT_KEY}_last_failure_at"] == now.isoformat()
    assert state[mt.RECALL_INDEX_KEY] == now.isoformat()
    assert state[mt.WORKSPACE_PRUNE_KEY] == now.isoformat()
