"""``bash``: bounded, foreground, reaped.

The matrix puts this on the client because of "subprocesses, process groups,
cwd" -- three things that only exist where the developer's files are. What it
deliberately does **not** do is as important:

* **No detached session.** The child gets its own *process group*
  (``process_group=0``, a ``setpgid`` without a ``setsid``) so a timeout can
  kill the whole tree, and it stays inside this session so it dies with it.
  ``start_new_session=True`` is the pattern the security review objected to and
  it does not appear in this package.
* **No background or interactive mode.** The public tool offers ``bg`` and
  ``interactive`` sessions that outlive the call. A client whose whole claim is
  "leaves no long-lived process" cannot also keep a REPL alive between tool
  calls, so both are refused by name with the reason, rather than silently
  ignored.
* **Bounded everything.** A wall-clock deadline, a byte cap on what reaches
  compaction, and a kill that escalates from the group's ``SIGTERM`` to
  ``SIGKILL``.

Before a command runs it passes the main package's command policy
(:mod:`lemoncrow_client.kit.command_policy`): destructive git operations and
shell writes outside the worktree are refused, literal-file ``cat``/``head``/
``tail``/``wc`` run in-process, and ``od <big file> | tail`` seeks.

Output goes through the main package's own pipeline
(:mod:`lemoncrow_client.kit.bash_output`): ANSI stripped, repeats collapsed,
a test run cut to its failures and summary, a long log cut to the lines around
an error, secrets redacted, and an identical rerun replaced by a one-line
``unchanged`` marker. Whatever is trimmed is spilled to a plain-text file
first and named in the footer, so the agent can recover it with
``read <path>``. Spilling is best-effort: a write failure leaves the inline
trim markers without a path rather than breaking the call.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..errors import AgentAction, ClientError, ErrorCode
from . import LocalContext, LocalResult, text_result

if TYPE_CHECKING:
    from ..kit.bash_output import CompactedOutput

__all__ = ["cancel_active_bash_commands", "run_bash", "spill_text"]

_DEFAULT_TIMEOUT_S: Final[float] = 120.0
_MAX_TIMEOUT_S: Final[float] = 3600.0
_TERM_GRACE_S: Final[float] = 2.0
#: Below this many trimmed characters a trim gets no footer; the main
#: package's renderer uses the same threshold.
_TRIVIAL_TRIM_CHARS: Final[int] = 300

# Bounded retention for the spill directory -- nothing else ever deletes these
# files, so an unbounded sweep runs best-effort on every spill write. Same
# names and defaults as the private server's own spill store
# (`lemoncrow.pro.capabilities.tool_supervision.tool_output_spill`) so one env
# var controls both when `LEMONCROW_MCP_SPILL_DIR` points them at the same
# directory; set either axis to 0 to disable it.
_DEFAULT_SPILL_MAX_FILES: Final[int] = 512
_DEFAULT_SPILL_TTL_SECONDS: Final[int] = 24 * 60 * 60

_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()


def cancel_active_bash_commands() -> int:
    """Terminate every foreground bash process currently owned by this process.

    The normal thin-client process exits after the host session, so this is
    mostly useful to embedders such as the central remote-MCP transport. On
    server drain we must not wait up to a caller-selected 3600-second timeout
    before Python can exit; killing each command's process group lets its worker
    thread return promptly and preserves the existing no-orphan guarantee.
    """
    with _ACTIVE_PROCESSES_LOCK:
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _kill_group(process)
    return len(processes)


def _commands(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    raw = arguments.get("command")
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        out = [str(entry) for entry in raw if str(entry).strip()]
        return tuple(out)
    return ()


def _refuse(reason: str, detail: str) -> ClientError:
    return ClientError(
        ErrorCode.LOCAL_TOOL_UNAVAILABLE,
        reason,
        details={"detail": detail},
        action=AgentAction.FIX_REQUEST,
    )


def _spill_dir(state_dir: Path) -> Path:
    """Resolve the spill directory: ``LEMONCROW_MCP_SPILL_DIR`` or ``<state_dir>/spill``.

    Honoring the same env var as the private server's spill store lets the two
    share one directory when they run on the same machine, so a spilled bash
    result is recoverable the same way regardless of which one produced it.
    """
    configured = os.environ.get("LEMONCROW_MCP_SPILL_DIR")
    directory = Path(configured).expanduser().resolve() if configured else state_dir / "spill"
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
    return directory


def _retention_limits() -> tuple[int, int]:
    """Return ``(max_files, max_age_seconds)``; either ``<= 0`` disables that axis."""

    def _read(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            return default

    return (
        _read("LEMONCROW_MCP_SPILL_MAX_FILES", _DEFAULT_SPILL_MAX_FILES),
        _read("LEMONCROW_MCP_SPILL_TTL_SECONDS", _DEFAULT_SPILL_TTL_SECONDS),
    )


def _enforce_retention(directory: Path, pattern: str) -> None:
    """Evict old spills matching *pattern* by age then count. Best-effort; never raises."""
    max_files, max_age = _retention_limits()
    if max_files <= 0 and max_age <= 0:
        return
    try:
        entries = [(p.stat().st_mtime, p) for p in directory.glob(pattern)]
    except OSError:
        return
    now = time.time()
    survivors: list[tuple[float, Path]] = []
    for mtime, p in entries:
        if max_age > 0 and (now - mtime) > max_age:
            with contextlib.suppress(OSError):
                p.unlink()
        else:
            survivors.append((mtime, p))
    if max_files > 0 and len(survivors) > max_files:
        survivors.sort(key=lambda item: item[0])  # oldest first
        for _, p in survivors[: len(survivors) - max_files]:
            with contextlib.suppress(OSError):
                p.unlink()


def spill_text(text: str, state_dir: Path, *, kind: str = "bash", suffix: str = ".txt") -> Path | None:
    """Persist *text* as ``<kind>-<id><suffix>`` and return its path, or ``None`` on any failure.

    Each kind keeps its own bounded retention.
    """
    try:
        directory = _spill_dir(state_dir)
        file_name = f"{kind}-{uuid.uuid4().hex[:8]}{suffix}"
        spill_path = directory / file_name
        tmp_path = directory / f".{file_name}.tmp"
        try:
            tmp_path.write_text(text, encoding="utf-8")
            os.replace(tmp_path, spill_path)
        finally:
            with contextlib.suppress(OSError):
                if tmp_path.exists():
                    tmp_path.unlink()
        _enforce_retention(directory, f"{kind}-*{suffix}")
        return spill_path
    except OSError:
        return None


def _trim_footer(compacted: CompactedOutput) -> str:
    """The recovery line under a trimmed output, by the main package's rule."""
    if not compacted.truncated:
        return ""
    if compacted.spill_hint:
        return compacted.spill_hint
    if 0 <= compacted.chars_omitted < _TRIVIAL_TRIM_CHARS:
        return ""  # the footer would outweigh what it accounts for
    if compacted.lines_omitted > 0:
        return f"[output truncated: {compacted.lines_omitted} lines omitted]"
    return "[output compacted]"


def _present(context: LocalContext, command: str, raw: str, exit_code: int, cwd: Path, ran_note: str) -> str:
    """What the model sees of one command's combined output."""
    from ..kit.bash_output import (
        MAX_OUTPUT_BYTES,
        OUTPUT_CAP_NOTICE,
        cap_output_bytes,
        compact_command_output,
        spill_footer,
        unchanged_marker,
    )

    def store(text: str) -> str | None:
        path = spill_text(text, context.config.state_dir)
        return None if path is None else str(path)

    # Run-and-dedup, not a cache: the command ran. Its output is already in
    # this session's context, so a marker replaces it; the file keeps it.
    if context.memory.command_runs.observe(command, cwd=str(cwd), stdout=raw, stderr="", exit_code=exit_code):
        marker = unchanged_marker(raw, "")
        footer = spill_footer(raw, len(marker), store)
        return f"{marker}\n\n{footer}" if footer else marker

    kept, capped = cap_output_bytes(raw)
    if capped:
        kept += OUTPUT_CAP_NOTICE.format(cap=MAX_OUTPUT_BYTES)

    def spill(full_text: str, kept_chars: int) -> str:
        # Past the byte cap the file still gets the whole output.
        return spill_footer(raw if capped else full_text, kept_chars, store)

    compacted = compact_command_output(command, kept, "", exit_code, spill=spill)
    parts = [part for part in (ran_note, compacted.stdout.rstrip("\n")) if part]
    footer = _trim_footer(compacted)
    if footer:
        parts.extend(("", footer) if parts else (footer,))
    return "\n".join(parts)


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Terminate the whole process group, escalating once."""
    try:
        group = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        group = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if group:
                os.killpg(group, sig)
            else:
                process.send_signal(sig)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=_TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def run_bash(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Run one or more shell commands, each in its own subshell."""
    if arguments.get("bg"):
        raise _refuse(
            "the thin client runs bounded foreground commands only",
            "bg=true would leave a process alive after this short-lived client exits; "
            "start long-running work with your own terminal or a remote workspace",
        )
    if arguments.get("interactive"):
        raise _refuse(
            "the thin client runs bounded foreground commands only",
            "interactive=true keeps a REPL alive between tool calls, which this client has " "nowhere to hold",
        )
    if arguments.get("id") is not None or str(arguments.get("action") or "").strip() not in {"", "poll"}:
        raise _refuse(
            "the thin client has no background session table",
            "action/id address a session from a previous call; every call here runs to completion",
        )

    commands = _commands(arguments)
    if not commands:
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            "command must be a non-empty string or array of strings",
            action=AgentAction.FIX_REQUEST,
        )

    raw_timeout = arguments.get("timeout")
    timeout_s = _DEFAULT_TIMEOUT_S
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and raw_timeout > 0:
        timeout_s = min(float(raw_timeout), _MAX_TIMEOUT_S)

    cwd_argument = arguments.get("cwd")
    cwd = context.resolve(str(cwd_argument)) if cwd_argument else context.repo_root
    if not cwd.is_dir():
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            f"cwd is not a directory: {cwd_argument}",
            action=AgentAction.FIX_REQUEST,
        )

    from ..kit.bash_output import inject_stable_flags
    from ..kit.command_policy import INLINE_OPS, classify_command, execute_inline_op

    sections: list[str] = []
    failed = False
    for index, command in enumerate(commands, start=1):
        header = f"## lc:cmd {index}/{len(commands)}" if len(commands) > 1 else ""
        policy = classify_command(command, allowed_write_roots=[context.repo_root], cwd=cwd)
        timed_out = False
        if policy.action == "block":
            code, body = -1, policy.reason
        elif policy.action == "rewrite" and policy.rewrite_target in INLINE_OPS and policy.rewrite_payload:
            out, err, code = execute_inline_op(policy.rewrite_target, policy.rewrite_payload, str(cwd))
            body = _present(context, command, out + err, code, cwd, "")
        else:
            # Rewrites to a tool this client does not serve here (read, grep,
            # web_fetch, ...) run as written.
            source, seek_note = command, ""
            if policy.action == "rewrite" and policy.rewrite_target == "pipeline_seek" and policy.rewrite_payload:
                source = str(policy.rewrite_payload.get("command") or command)
                seek_note = str(policy.rewrite_payload.get("note") or "")
            exec_command, ran_note = inject_stable_flags(source)
            code, raw, timed_out = _run_one(exec_command, cwd, timeout_s, dict(context.environment))
            notes = "\n".join(note for note in (seek_note, ran_note) if note)
            body = _present(context, command, raw, code, cwd, notes)
        failed = failed or code != 0
        suffix = f"\n[timed out after {timeout_s:g}s; process group terminated]" if timed_out else ""
        sections.append(f"{header}\n{body}{suffix}".strip("\n") + f"\n[exit {code}]")

    return text_result("\n".join(sections), is_error=failed)


def _run_one(command: str, cwd: Path, timeout_s: float, environment: dict[str, str]) -> tuple[int, str, bool]:
    """Return ``(exit code, combined output, timed out)``."""
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            env=environment or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            # Its own process group, so a timeout kills the whole tree. NOT a
            # new session: the child stays attached to this one and dies with it.
            process_group=0,
        )
    except OSError as exc:
        raise ClientError(
            ErrorCode.LOCAL_TOOL_FAILED,
            f"could not start a shell: {exc}",
            action=AgentAction.ABANDON,
        ) from exc
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(process)
    timed_out = False
    try:
        try:
            stdout, _ = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            try:
                stdout, _ = process.communicate(timeout=_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                stdout = ""
    finally:
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.discard(process)
    return int(process.returncode or 0), stdout or "", timed_out
