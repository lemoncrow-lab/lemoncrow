"""Benchmark cases for the `shell` MCP tool.

Savings come from:
- ANSI stripping (no garbage tokens from terminal color codes)
- head+tail truncation (agent sees structure, not 10k lines of logs)
- Transparent rewrites: cat→read, rg/grep→grep (cheaper tools, better ranking)
- Structured response: agent checks exit_code first, reads output only if needed

Baseline estimates:
  - echo/simple: raw subprocess + parse stdout (~80 tokens framing)
  - cat rewrite: cat would read raw file content; read tool is outline-first (~300 token baseline vs atelier)
  - rg rewrite: rg prints raw matches; grep tool returns ranked, budget-capped (~250 tokens baseline)
  - blocked: agent calls rm -rf, catches -1 exit; structured blocked=True makes it unambiguous
  - truncated: seq 1..500 without atelier would dump 500 lines; atelier caps at max_lines

SHELL_WORKSPACE env var injected by bench_shell.py.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _assert_ran_ok(result: dict[str, Any]) -> None:
    assert result.get("exit_code") == 0, f"expected exit_code=0, got: {result}"
    assert "stdout" in result, f"response must have stdout, got: {list(result)}"
    assert "truncated" in result, f"response must have truncated field, got: {list(result)}"
    assert "duration_ms" in result, f"response must have duration_ms, got: {list(result)}"


def _assert_echo_content(result: dict[str, Any]) -> None:
    _assert_ran_ok(result)
    assert "bench_hello" in result["stdout"], f"expected 'bench_hello' in stdout, got: {result['stdout']!r}"


def _assert_cat_rewritten(result: dict[str, Any]) -> None:
    assert result.get("exit_code") == 0, f"cat rewrite must succeed, got: {result}"
    assert "sentinel_content" in result.get(
        "stdout", ""
    ), f"cat rewrite must return file content, got stdout={result.get('stdout', '')!r}"


def _assert_rg_rewritten(result: dict[str, Any]) -> None:
    assert result.get("exit_code") == 0, f"rg rewrite must succeed, got: {result}"
    assert "needle_token" in result.get(
        "stdout", ""
    ), f"rewritten grep must find needle_token, got stdout={result.get('stdout', '')!r}"


def _assert_rg_type_rewritten(result: dict[str, Any]) -> None:
    assert result.get("exit_code") == 0, f"rg --type rewrite must succeed, got: {result}"
    assert "needle_token" in result.get(
        "stdout", ""
    ), f"rg --type rewrite must find needle_token, got stdout={result.get('stdout', '')!r}"


def _assert_blocked(result: dict[str, Any]) -> None:
    assert result.get("exit_code") == -1, f"blocked command must return exit_code=-1, got: {result}"
    assert result.get("blocked") is True, f"blocked command must have blocked=True, got: {result}"
    assert result.get("blocked_reason"), f"blocked command must have blocked_reason, got: {result}"


def _assert_truncated(result: dict[str, Any]) -> None:
    _assert_ran_ok(result)
    assert result.get("truncated") is True, f"long output must be truncated, got truncated={result.get('truncated')}"
    assert result.get("lines_omitted", 0) > 0, f"lines_omitted must be >0, got: {result.get('lines_omitted')}"
    assert "lines omitted" in result.get("stdout", ""), "truncation marker missing from stdout"


def _assert_exit_nonzero(result: dict[str, Any]) -> None:
    assert result.get("exit_code") != 0, f"expected non-zero exit_code, got: {result}"
    assert "stderr" in result, "response must have stderr"


# ---------------------------------------------------------------------------
# Cases  (__SHELL_WORKSPACE__ is patched in by bench_shell.py)
# ---------------------------------------------------------------------------

SHELL_CASES: list[BenchCase] = [
    BenchCase(
        op="shell",
        label="shell/echo",
        args={"command": "echo bench_hello"},
        assert_keys=["stdout", "exit_code", "truncated", "duration_ms"],
        custom_assert=_assert_echo_content,
        # baseline: raw subprocess call + string framing
        baseline_tokens=80,
    ),
    BenchCase(
        op="shell",
        label="shell/cat-rewrite",
        args={"command": "cat __SHELL_FILE__"},
        assert_keys=["stdout", "exit_code"],
        custom_assert=_assert_cat_rewritten,
        # baseline: raw cat dumps entire file content to stdout without outline
        baseline_tokens=300,
    ),
    BenchCase(
        op="shell",
        label="shell/rg-rewrite",
        args={"command": "rg needle_token __SHELL_WORKSPACE__"},
        assert_keys=["stdout", "exit_code"],
        custom_assert=_assert_rg_rewritten,
        # baseline: rg prints raw match lines without budget cap
        baseline_tokens=250,
    ),
    BenchCase(
        op="shell",
        label="shell/rg-type-rewrite",
        args={"command": "rg --type py needle_token __SHELL_WORKSPACE__"},
        assert_keys=["stdout", "exit_code"],
        custom_assert=_assert_rg_type_rewritten,
        # baseline: same as rg rewrite
        baseline_tokens=250,
    ),
    BenchCase(
        op="shell",
        label="shell/blocked-rm",
        args={"command": "rm -rf /tmp/atelier_bench_never_runs"},
        assert_keys=["exit_code"],
        custom_assert=_assert_blocked,
        # correctness only — no savings baseline for blocked commands
        baseline_tokens=0,
    ),
    BenchCase(
        op="shell",
        label="shell/blocked-bash",
        args={"command": "bash -c 'echo no'"},
        assert_keys=["exit_code"],
        custom_assert=_assert_blocked,
        baseline_tokens=0,
    ),
    BenchCase(
        op="shell",
        label="shell/truncated-output",
        args={"command": "seq 1 500", "max_lines": 50},
        assert_keys=["stdout", "exit_code", "truncated", "lines_omitted"],
        custom_assert=_assert_truncated,
        # baseline: 500 lines raw output vs 50 lines capped
        baseline_tokens=500,
    ),
    BenchCase(
        op="shell",
        label="shell/nonzero-exit",
        args={"command": "exit 1"},
        assert_keys=["exit_code", "stderr"],
        custom_assert=_assert_exit_nonzero,
        baseline_tokens=0,
    ),
]
