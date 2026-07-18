"""Benchmark cases for the public text-returning `shell` MCP tool."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from benchmarks.mcp_tools.harness import BaselineMeasurement, BenchCase


def _shell_baseline_builder(case: BenchCase) -> BaselineMeasurement:
    cmd = str(case.args["command"])
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    return BaselineMeasurement(payload=out[:200_000], commands=[cmd])


def _as_text(result: object) -> str:
    assert isinstance(result, str), f"shell tool must return text, got: {type(result).__name__}"
    return result


def _contains_assert(*needles: str) -> Callable[[object], None]:
    def _assert(result: object) -> None:
        text = _as_text(result)
        for needle in needles:
            assert needle in text, f"expected {needle!r} in output, got: {text!r}"

    return _assert


def _prefix_assert(prefix: str) -> Callable[[object], None]:
    def _assert(result: object) -> None:
        text = _as_text(result)
        assert text.startswith(prefix), f"expected prefix {prefix!r}, got: {text!r}"

    return _assert


def _assert_nonzero(result: object) -> None:
    text = _as_text(result)
    assert text.startswith("exit_code="), f"expected non-zero exit marker, got: {text!r}"
    assert not text.startswith("exit_code=0"), f"expected non-zero exit marker, got: {text!r}"


def _assert_truncated(result: object) -> None:
    text = _as_text(result)
    assert "[output truncated:" in text, f"truncation marker missing, got: {text!r}"
    assert "lines omitted" in text, f"line omission marker missing, got: {text!r}"


def _assert_allowed_rm(result: object) -> None:
    # rm -rf is no longer blocked; a clean run of a nonexistent -f target exits 0.
    text = _as_text(result)
    assert "blocked" not in text.lower(), f"rm -rf must not be blocked anymore, got: {text!r}"


def _assert_blocked_git_reset(result: object) -> None:
    text = _as_text(result)
    assert "git reset --hard blocked" in text, f"blocked git reset reason missing, got: {text!r}"


def _case(
    label: str,
    command: str,
    custom_assert: Callable[[object], None],
    *,
    baseline_tokens: int = 0,
    baseline_builder: Callable[[BenchCase], BaselineMeasurement] | None = None,
    max_lines: int | None = None,
) -> BenchCase:
    args: dict[str, object] = {"command": command}
    if max_lines is not None:
        args["max_lines"] = max_lines
    return BenchCase(
        op="shell",
        label=label,
        args=args,
        custom_assert=custom_assert,
        baseline_tokens=baseline_tokens,
        baseline_builder=baseline_builder,
        min_baseline_tokens=0,
    )


SHELL_CASES: list[BenchCase] = [
    _case(
        "shell/echo/01", "echo bench_hello", _contains_assert("bench_hello"), baseline_builder=_shell_baseline_builder
    ),
    _case(
        "shell/echo/02",
        "printf 'alpha\\nbeta\\n'",
        _contains_assert("alpha", "beta"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case("shell/pwd", "pwd", _prefix_assert("/"), baseline_builder=_shell_baseline_builder),
    _case(
        "shell/ls-root",
        "ls __SHELL_WORKSPACE__",
        _contains_assert("sentinel.txt", "src"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/ls-src",
        "ls __SHELL_WORKSPACE__/src",
        _contains_assert("module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/cat-sentinel",
        "cat __SHELL_FILE__",
        _contains_assert("sentinel_content line1", "sentinel_content line2"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/cat-module",
        "cat __SHELL_WORKSPACE__/src/module.py",
        _contains_assert("needle_token", "return 42"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/rg-workspace",
        "rg needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/rg-src",
        "rg needle_token __SHELL_WORKSPACE__/src",
        _contains_assert("needle_token", "src/module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/rg-line-number",
        "rg -n needle_token __SHELL_WORKSPACE__/src/module.py",
        _contains_assert("needle_token", "module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/rg-type",
        "rg --type py needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case(
        "shell/rg-glob",
        "rg --glob '*.py' needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_builder=_shell_baseline_builder,
    ),
    _case("shell/seq-short", "seq 1 5", _contains_assert("1", "5"), baseline_builder=_shell_baseline_builder),
    _case(
        "shell/seq-truncated/01",
        "seq 1 200",
        _assert_truncated,
        baseline_builder=_shell_baseline_builder,
        max_lines=20,
    ),
    _case(
        "shell/seq-truncated/02",
        "seq 1 500",
        _assert_truncated,
        baseline_builder=_shell_baseline_builder,
        max_lines=50,
    ),
    _case("shell/nonzero-exit/01", "exit 1", _prefix_assert("exit_code=1"), baseline_tokens=0),
    _case("shell/nonzero-exit/02", "ls /definitely/missing/path", _assert_nonzero, baseline_tokens=0),
    _case(
        "shell/allowed-rm",
        "rm -rf /tmp/lemoncrow_bench_never_runs",
        _assert_allowed_rm,
        baseline_tokens=0,
    ),
    _case("shell/inline-bash", "bash -c 'echo no'", _contains_assert("no"), baseline_tokens=0),
    _case("shell/blocked-git-reset", "git reset --hard", _assert_blocked_git_reset, baseline_tokens=0),
]
