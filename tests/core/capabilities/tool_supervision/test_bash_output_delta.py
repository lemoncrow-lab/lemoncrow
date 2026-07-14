"""Run-and-dedup of byte-identical command outputs (`output_delta`).

Execution is never skipped -- only the *shipping* of a byte-identical re-run
is replaced by an `unchanged` marker. Failures and small outputs always ship
in full.
"""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.tool_supervision import bash_exec, output_delta

_BIG = "\n".join(f"line {i}" for i in range(120))  # > _MIN_CHARS


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    output_delta.reset()
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    monkeypatch.delenv(output_delta._ENV_ENABLED, raising=False)


def _observe(stdout: str = _BIG, stderr: str = "", exit_code: int = 0, command: str = "git status") -> bool:
    return output_delta.observe(command, cwd=None, stdout=stdout, stderr=stderr, exit_code=exit_code)


def test_first_run_never_unchanged() -> None:
    assert _observe() is False


def test_identical_second_run_is_unchanged() -> None:
    _observe()
    assert _observe() is True


def test_changed_output_ships_then_dedups_again() -> None:
    _observe()
    assert _observe(stdout=_BIG + "\nM file.py") is False
    assert _observe(stdout=_BIG + "\nM file.py") is True


def test_nonzero_exit_never_unchanged() -> None:
    _observe(exit_code=1)
    assert _observe(exit_code=1) is False


def test_small_output_never_unchanged() -> None:
    _observe(stdout="tiny")
    assert _observe(stdout="tiny") is False


def test_cwd_is_part_of_identity() -> None:
    output_delta.observe("git status", cwd="/a", stdout=_BIG, stderr="", exit_code=0)
    assert output_delta.observe("git status", cwd="/b", stdout=_BIG, stderr="", exit_code=0) is False


def test_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(output_delta._ENV_ENABLED, "0")
    _observe()
    assert _observe() is False


def test_tracking_map_stays_bounded() -> None:
    for i in range(output_delta._MAX_TRACKED * 2):
        output_delta.observe(f"cmd-{i}", cwd=None, stdout=_BIG, stderr="", exit_code=0)
    assert len(output_delta._last) <= output_delta._MAX_TRACKED


def test_run_command_second_identical_run_ships_marker() -> None:
    first = bash_exec.run_command("seq 1 200", timeout=10)
    second = bash_exec.run_command("seq 1 200", timeout=10)
    assert first.exit_code == 0
    assert "200" in first.stdout
    assert second.stdout.startswith("unchanged: output byte-identical")
    assert 'first line: "1"' in second.stdout
    assert second.stderr == ""
    assert second.truncated is True
    assert second.lines_omitted == 200
    assert second.chars_omitted > 0


def test_run_command_output_change_ships_full_output(tmp_path: object) -> None:
    bash_exec.run_command("seq 1 200", timeout=10)
    third = bash_exec.run_command("seq 1 201", timeout=10)
    assert not third.stdout.startswith("unchanged:")
    assert "201" in third.stdout
