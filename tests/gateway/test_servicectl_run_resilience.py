"""Regression test: the detached ``servicectl run`` loop must survive a tick
subprocess that legitimately exceeds its timeout.

Verified finding (CRITICAL, servicectl.py `servicectl_run`): the loop's
``subprocess.run(cmd, timeout=600)`` was guarded only by an outer
``except KeyboardInterrupt`` -- a tick that exceeded 600s raised an uncaught
``TimeoutExpired`` that killed the whole detached controller process,
orphaning any grandchildren it had spawned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import servicectl as servicectl_cmd


class _StopLoop(Exception):
    """Sentinel used to break the (intentionally infinite) run loop deterministically."""


def test_servicectl_run_survives_tick_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"run": 0}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls["run"] += 1
        if calls["run"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))
        return subprocess.CompletedProcess(cmd, 0)

    def fake_sleep(_secs: float) -> None:
        if calls["run"] >= 2:
            raise _StopLoop()

    monkeypatch.setattr(servicectl_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(servicectl_cmd.time, "sleep", fake_sleep)

    runner = CliRunner()
    result = runner.invoke(
        servicectl_cmd.servicectl_run,
        ["--interval-seconds", "0"],
        obj={"root": tmp_path},
    )

    # The loop must have survived the first TimeoutExpired (not crashed on
    # it) and reached a second subprocess.run call before our sentinel broke
    # the loop -- proving the timeout was caught, not left to propagate.
    assert calls["run"] >= 2, "loop died on the first TimeoutExpired instead of continuing"
    assert isinstance(result.exception, _StopLoop), result.output
