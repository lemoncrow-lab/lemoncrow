"""Regression tests: a periodic tick subprocess timing out must not abort
``_servicectl_tick`` -- and must not silently advance the periodic key either.

Original finding (CRITICAL, servicectl_lifecycle.py `_servicectl_import_sessions`):
``subprocess.run(timeout=300)`` had no try/except, so a ``TimeoutExpired``
propagated out of ``_servicectl_tick`` before state.json was written.

Follow-up finding: advancing the periodic key on timeout permanently starved
large stores (the import never got another chance until the next interval,
and always with the same too-small budget). Now a timeout keeps the key
un-advanced, escalates the budget (2x per consecutive timeout, capped 4x),
records a ``subprocess_timeouts`` health counter in state.json, and only
backs off to the normal interval after 3 consecutive timeouts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from atelier.infra.runtime import servicectl_lifecycle as svc


def test_servicectl_import_sessions_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_servicectl_import_sessions must swallow TimeoutExpired and signal it
    with None, not propagate it."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_import_sessions(tmp_path) is None


def test_servicectl_index_recall_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard applies to the sibling recall-index subprocess call."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_index_recall(tmp_path) is None


def test_servicectl_prune_workspaces_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard applies to the sibling workspace-prune subprocess call."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_prune_workspaces(tmp_path) is None


def _fake_run_with_import_timeout(calls: list[float]) -> Any:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "import" in cmd:
            calls.append(float(kwargs.get("timeout") or 0))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))
        if "run-once" in cmd:
            # Worker queue empty -> break the job-processing loop immediately.
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"processed": false}', stderr=b"")
        # recall index / workspace prune: nothing to do.
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    return fake_run


def test_tick_survives_import_timeout_without_advancing_periodic_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out import must not abort the tick (state.json still written),
    must not advance the periodic key (the work never happened), and must
    leave a health signal in state."""
    calls: list[float] = []
    monkeypatch.setattr(svc.subprocess, "run", _fake_run_with_import_timeout(calls))

    result = svc._servicectl_tick(
        tmp_path,
        maintenance_interval_seconds=300,
        session_import_interval_seconds=3600,
    )

    # The tick ran to completion (not aborted mid-way by the timeout) and
    # recorded that the import attempt happened.
    assert result["session_import_ran"] is True
    assert result["imported_sessions"] == {}

    state = svc._read_servicectl_state(tmp_path)
    assert state["last_tick_at"] is not None
    # Key NOT advanced: the import gets retried (with a bigger budget).
    assert "import_host_sessions" not in state["periodic_jobs"]
    assert state["subprocess_timeouts"]["import_host_sessions"] == 1


def test_import_retries_with_escalated_timeout_then_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Consecutive timeouts escalate the budget (2x, then 4x capped); after
    3 consecutive timeouts the key advances anyway so the rest of the tick's
    duties are not starved, and the health counter survives in state."""
    calls: list[float] = []
    monkeypatch.setattr(svc.subprocess, "run", _fake_run_with_import_timeout(calls))

    for _ in range(3):
        svc._servicectl_tick(tmp_path, maintenance_interval_seconds=300, session_import_interval_seconds=3600)
    assert calls == [300.0, 600.0, 1200.0]

    state = svc._read_servicectl_state(tmp_path)
    # Backed off: after 3 consecutive timeouts the key advances so the import
    # is deferred to the next interval instead of hammering every tick.
    assert "import_host_sessions" in state["periodic_jobs"]
    assert state["subprocess_timeouts"]["import_host_sessions"] == 3

    result4 = svc._servicectl_tick(tmp_path, maintenance_interval_seconds=300, session_import_interval_seconds=3600)
    assert result4["session_import_ran"] is False
    assert len(calls) == 3  # not re-run within the interval
