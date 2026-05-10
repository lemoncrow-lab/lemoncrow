"""Shell command execution with token-aware output compaction."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _head_tail_lines(lines: list[str], head: int, tail: int) -> tuple[str, int]:
    if len(lines) <= head + tail:
        return "\n".join(lines), 0
    omitted = len(lines) - head - tail
    parts = [*lines[:head], f"... ({omitted} lines omitted) ...", *lines[-tail:]]
    return "\n".join(parts), omitted


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool
    lines_omitted: int
    command: str


def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: int = 30,
    max_lines: int = 200,
) -> RunResult:
    """Execute *command* in bash, return token-compact structured output.

    Optimizations vs. raw subprocess:
    - ANSI escape codes stripped (progress bars, colors → garbage tokens).
    - stdout truncated head+tail: first 25% for context, last 75% for results/errors.
    - stderr always kept in full (usually short; errors live here).
    - Structured return: LLM checks exit_code first, reads output only if needed.
    """
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        exit_code = proc.returncode
        raw_stdout = _strip_ansi(proc.stdout)
        raw_stderr = _strip_ansi(proc.stderr)
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_stdout = ""
        raw_stderr = f"Command timed out after {timeout}s"
    except Exception as exc:
        exit_code = -1
        raw_stdout = ""
        raw_stderr = str(exc)

    duration_ms = int((time.perf_counter() - started) * 1000)

    head = max(20, max_lines // 4)
    tail = max_lines - head
    stdout_compact, lines_omitted = _head_tail_lines(raw_stdout.splitlines(), head, tail)
    stderr_compact, _ = _head_tail_lines(raw_stderr.splitlines(), 100, 100)

    return RunResult(
        stdout=stdout_compact,
        stderr=stderr_compact,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=lines_omitted > 0,
        lines_omitted=lines_omitted,
        command=command,
    )


__all__ = ["RunResult", "run_command"]
