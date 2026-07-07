"""Regression tests: a periodic tick subprocess timing out must not abort
``_servicectl_tick`` before its periodic-job timestamp and state.json are
written.

Verified finding (CRITICAL, servicectl_lifecycle.py `_servicectl_import_sessions`):
``subprocess.run(timeout=300)`` had no try/except, so a ``TimeoutExpired``
propagated out of ``_servicectl_tick`` before ``periodic[SESSION_IMPORT_KEY]``
and ``_write_servicectl_state`` ran -- every subsequent tick re-ran the same
doomed import and crashed again, so recall indexing / pruning / job
processing never ran either.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from atelier.infra.runtime import servicectl_lifecycle as svc


def test_servicectl_import_sessions_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_servicectl_import_sessions must swallow TimeoutExpired and return {},
    not propagate it."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_import_sessions(tmp_path) == {}


def test_servicectl_index_recall_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard applies to the sibling recall-index subprocess call."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_index_recall(tmp_path) == {}


def test_servicectl_prune_workspaces_survives_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard applies to the sibling workspace-prune subprocess call."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._servicectl_prune_workspaces(tmp_path) == {}


def test_tick_advances_periodic_key_and_writes_state_when_import_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out import subprocess must not abort the tick before the
    periodic key / state.json are written -- otherwise every subsequent tick
    re-runs the same doomed import forever and nothing else ever runs."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "import" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))
        if "run-once" in cmd:
            # Worker queue empty -> break the job-processing loop immediately.
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"processed": false}', stderr=b"")
        # recall index / workspace prune: nothing to do.
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    result = svc._servicectl_tick(
        tmp_path,
        maintenance_interval_seconds=300,
        session_import_interval_seconds=0,
    )

    # The tick ran to completion (not aborted mid-way by the timeout) and
    # recorded that the import attempt happened.
    assert result["session_import_ran"] is True
    assert result["imported_sessions"] == {}

    state = svc._read_servicectl_state(tmp_path)
    assert state["last_tick_at"] is not None
    assert "import_host_sessions" in state["periodic_jobs"]


def test_second_tick_does_not_re_run_import_immediately_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Because the periodic key advances despite the timeout, a second tick
    within the interval must not re-attempt the import (it would otherwise
    hammer the same doomed subprocess on every tick)."""
    calls = {"import": 0}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "import" in cmd:
            calls["import"] += 1
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))
        if "run-once" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"processed": false}', stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    svc._servicectl_tick(tmp_path, maintenance_interval_seconds=300, session_import_interval_seconds=3600)
    assert calls["import"] == 1

    result2 = svc._servicectl_tick(tmp_path, maintenance_interval_seconds=300, session_import_interval_seconds=3600)
    assert calls["import"] == 1  # not re-run within the interval
    assert result2["session_import_ran"] is False
