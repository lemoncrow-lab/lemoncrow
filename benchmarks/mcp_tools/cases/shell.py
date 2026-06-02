"""Benchmark cases for the public text-returning `shell` MCP tool."""

from __future__ import annotations

from collections.abc import Callable

from benchmarks.mcp_tools.harness import BenchCase


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


def _assert_blocked_rm(result: object) -> None:
    text = _as_text(result)
    assert text.startswith("blocked (exit_code=-1)"), f"blocked command must show exit_code=-1, got: {text!r}"
    assert "Destructive rm -rf commands are blocked" in text, f"blocked rm reason missing, got: {text!r}"


def _assert_blocked_bash(result: object) -> None:
    text = _as_text(result)
    assert text.startswith("blocked (exit_code=-1)"), f"blocked command must show exit_code=-1, got: {text!r}"
    assert "Direct bash execution is blocked" in text, f"blocked bash reason missing, got: {text!r}"


def _case(
    label: str,
    command: str,
    custom_assert: Callable[[object], None],
    *,
    baseline_tokens: int,
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
    )


SHELL_CASES: list[BenchCase] = [
    _case("shell/echo/01", "echo bench_hello", _contains_assert("bench_hello"), baseline_tokens=80),
    _case(
        "shell/echo/02",
        "printf 'alpha\\nbeta\\n'",
        _contains_assert("alpha", "beta"),
        baseline_tokens=90,
    ),
    _case("shell/pwd", "pwd", _prefix_assert("/"), baseline_tokens=80),
    _case(
        "shell/ls-root",
        "ls __SHELL_WORKSPACE__",
        _contains_assert("sentinel.txt", "src"),
        baseline_tokens=120,
    ),
    _case(
        "shell/ls-src",
        "ls __SHELL_WORKSPACE__/src",
        _contains_assert("module.py"),
        baseline_tokens=100,
    ),
    _case(
        "shell/cat-sentinel",
        "cat __SHELL_FILE__",
        _contains_assert("sentinel_content line1", "sentinel_content line2"),
        baseline_tokens=300,
    ),
    _case(
        "shell/cat-module",
        "cat __SHELL_WORKSPACE__/src/module.py",
        _contains_assert("needle_token", "return 42"),
        baseline_tokens=320,
    ),
    _case(
        "shell/rg-workspace",
        "rg needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_tokens=250,
    ),
    _case(
        "shell/rg-src",
        "rg needle_token __SHELL_WORKSPACE__/src",
        _contains_assert("needle_token", "src/module.py"),
        baseline_tokens=250,
    ),
    _case(
        "shell/rg-line-number",
        "rg -n needle_token __SHELL_WORKSPACE__/src/module.py",
        _contains_assert("needle_token", "module.py"),
        baseline_tokens=260,
    ),
    _case(
        "shell/rg-type",
        "rg --type py needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_tokens=250,
    ),
    _case(
        "shell/rg-glob",
        "rg --glob '*.py' needle_token __SHELL_WORKSPACE__",
        _contains_assert("needle_token", "src/module.py"),
        baseline_tokens=260,
    ),
    _case("shell/seq-short", "seq 1 5", _contains_assert("1", "5"), baseline_tokens=90),
    _case("shell/seq-truncated/01", "seq 1 200", _assert_truncated, baseline_tokens=240, max_lines=20),
    _case("shell/seq-truncated/02", "seq 1 500", _assert_truncated, baseline_tokens=500, max_lines=50),
    _case("shell/nonzero-exit/01", "exit 1", _prefix_assert("exit_code=1"), baseline_tokens=0),
    _case("shell/nonzero-exit/02", "ls /definitely/missing/path", _assert_nonzero, baseline_tokens=0),
    _case(
        "shell/blocked-rm",
        "rm -rf /tmp/atelier_bench_never_runs",
        _assert_blocked_rm,
        baseline_tokens=0,
    ),
    _case("shell/blocked-bash", "bash -c 'echo no'", _assert_blocked_bash, baseline_tokens=0),
]
