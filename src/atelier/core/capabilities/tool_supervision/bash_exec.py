"""Shell command execution with token-aware output compaction."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

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
    policy_category: str = "generic"
    policy_action: str = "allow"
    policy_reason: str = ""
    rewrite_target: str | None = None
    rewrite_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommandPolicyDecision:
    category: str
    action: str
    reason: str = ""
    rewrite_target: str | None = None
    rewrite_payload: dict[str, Any] | None = None


def _rewrite_cat(tokens: list[str]) -> CommandPolicyDecision:
    if len(tokens) != 2:
        return CommandPolicyDecision(category="file-read", action="allow")
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        reason="Use Atelier read for file content access",
        rewrite_target="read",
        rewrite_payload={"file_path": tokens[1]},
    )


def _rewrite_search(tokens: list[str], command_name: str) -> CommandPolicyDecision:
    ignore_case = False
    file_type: str | None = None
    cleaned: list[str] = []
    seen_double_dash = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            seen_double_dash = True
            i += 1
            continue
        if tok.startswith("-") and not seen_double_dash:
            # Handle --type=python or --type python or -t python
            if tok.startswith("--type="):
                file_type = tok.split("=", 1)[1]
            elif tok in {"--type", "-t"} and i + 1 < len(tokens):
                i += 1
                file_type = tokens[i]
            elif "i" in tok and tok != "-":
                ignore_case = True
            i += 1
            continue
        cleaned.append(tok)
        i += 1

    if not cleaned:
        return CommandPolicyDecision(category="search", action="allow")

    pattern = cleaned[0]
    path = cleaned[1] if len(cleaned) > 1 else "."
    payload: dict[str, Any] = {
        "file_path": path,
        "content_regex": pattern,
        "ignore_case": ignore_case,
        "output_mode": "file_paths_with_content",
    }
    if file_type:
        payload["type"] = file_type
    return CommandPolicyDecision(
        category="search",
        action="rewrite",
        reason=f"Use Atelier grep for {command_name} pattern search",
        rewrite_target="grep",
        rewrite_payload=payload,
    )


def _is_rm_family(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "rm":
        return False
    return any(tok.startswith("-") and "r" in tok and "f" in tok for tok in tokens[1:])


def _is_git_reset_hard(tokens: list[str]) -> bool:
    return len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "reset" and "--hard" in tokens[2:]


def _is_git_clean_fd(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "clean":
        return False
    joined_flags = "".join(tok for tok in tokens[2:] if tok.startswith("-"))
    return "f" in joined_flags and "d" in joined_flags


def classify_command(command: str) -> CommandPolicyDecision:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return CommandPolicyDecision(category="generic", action="allow")
    if not tokens:
        return CommandPolicyDecision(category="generic", action="allow")

    head = tokens[0].lower()
    if head in {"bash", "sh", "zsh", "fish"}:
        return CommandPolicyDecision(
            category="shell-interpreter",
            action="block",
            reason=f"Direct {head} execution is blocked; use Atelier tools instead",
        )

    if _is_rm_family(tokens):
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="Destructive rm -rf commands are blocked",
        )
    if _is_git_reset_hard(tokens):
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="git reset --hard is blocked",
        )
    if _is_git_clean_fd(tokens):
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="git clean -fd is blocked",
        )

    if head == "cat":
        return _rewrite_cat(tokens)
    if head in {"rg", "grep"}:
        return _rewrite_search(tokens, head)
    return CommandPolicyDecision(category="generic", action="allow")


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
    policy = classify_command(command)
    if policy.action == "block":
        return RunResult(
            stdout="",
            stderr=policy.reason,
            exit_code=-1,
            duration_ms=0,
            truncated=False,
            lines_omitted=0,
            command=command,
            policy_category=policy.category,
            policy_action=policy.action,
            policy_reason=policy.reason,
            rewrite_target=policy.rewrite_target,
            rewrite_payload=policy.rewrite_payload,
        )

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

    if exit_code != 0:
        # For failing commands maximise the tail — that's where tracebacks,
        # test failure lines, and assertion messages live.  Keep a minimal
        # head (20 lines) just for collection / invocation context.
        head = 20
        tail = max(max_lines - head, 50)
    else:
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
        policy_category=policy.category,
        policy_action=policy.action,
        policy_reason=policy.reason,
        rewrite_target=policy.rewrite_target,
        rewrite_payload=policy.rewrite_payload,
    )


__all__ = ["CommandPolicyDecision", "RunResult", "classify_command", "run_command"]
