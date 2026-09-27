"""Suppress-on-success: noisy side-effecting commands collapse to ``ok:`` one-liners.

Covers the rtk-excluded zone -- mutating commands (git push, installs, builds)
that external compactors must never wrap. Post-hoc compaction of the already-
finished run is safe, so on exit 0 the whole output becomes a salient one-liner
while failures keep the full anomaly/head-tail paths.
"""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.tool_supervision import bash_exec

_NOISE = "\n".join(f"progress line {i}" for i in range(60))
_INSTALL_OUT = _NOISE + "\nInstalled 12 packages in 1.02s"
_PUSH_STDERR = (
    "\n".join(f"Compressing objects: {i}% ({i}/100)" for i in range(1, 60))
    + "\nTo github.com:me/repo.git\n   abc1234..def5678  main -> main"
)


@pytest.fixture(autouse=True)
def _no_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")


def _compact(command: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> bash_exec.RunResult:
    return bash_exec._compact_result(
        command=command,
        raw_stdout=stdout,
        raw_stderr=stderr,
        exit_code=exit_code,
        duration_ms=1,
        max_lines=200,
    )


def test_install_success_collapses_to_ok_line() -> None:
    result = _compact("uv sync", stdout=_INSTALL_OUT)
    assert result.stdout.startswith("ok: Installed 12 packages in 1.02s")
    assert "suppressed on success" in result.stdout
    assert result.truncated is True
    assert result.lines_omitted > 0
    assert result.chars_omitted > 0


def test_git_push_stderr_only_output_summarised_from_stderr() -> None:
    result = _compact("git push origin main", stderr=_PUSH_STDERR)
    assert result.stdout.startswith("ok: abc1234..def5678  main -> main")
    assert result.stderr == ""
    assert result.lines_omitted > 0


def test_git_commit_prefers_bracket_summary_first_line() -> None:
    stdout = "[main abc1234] my message\n" + "\n".join(f" create mode 100644 file{i}.py" for i in range(40))
    result = _compact("git commit -m 'my message'", stdout=stdout)
    assert result.stdout.startswith("ok: [main abc1234] my message")


def test_failure_never_suppressed() -> None:
    result = _compact("uv sync", stdout=_INSTALL_OUT, exit_code=1)
    assert not result.stdout.startswith("ok:")


def test_error_looking_line_on_success_falls_to_anomaly_path() -> None:
    out = _NOISE + "\nerror: peer dependency conflict\nInstalled 12 packages"
    result = _compact("npm install", stdout=out)
    assert not result.stdout.startswith("ok:")
    assert "error: peer dependency conflict" in result.stdout


def test_small_output_untouched() -> None:
    result = _compact("git push", stderr="Everything up-to-date")
    assert result.stdout == ""
    assert result.stderr == "Everything up-to-date"


def test_non_mutating_command_not_suppressed() -> None:
    result = _compact("ls -la", stdout=_INSTALL_OUT)
    assert not result.stdout.startswith("ok:")


def test_cd_prefix_still_matches() -> None:
    result = _compact("cd /tmp/repo && git pull", stdout=_INSTALL_OUT)
    assert result.stdout.startswith("ok:")


def test_salient_line_clipped() -> None:
    stdout = _NOISE + "\n" + "x" * 1000
    result = _compact("pip install foo", stdout=stdout)
    first_line = result.stdout.splitlines()[0]
    assert first_line == "ok: " + "x" * 200
