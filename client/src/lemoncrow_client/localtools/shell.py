"""``bash``: bounded, reaped, and never detached from this session.

The matrix puts this on the client because of "subprocesses, process groups,
cwd" -- three things that only exist where the developer's files are. What it
deliberately does **not** do is as important:

* **No detached session.** The child gets its own *process group*
  (``process_group=0``, a ``setpgid`` without a ``setsid``) so a timeout can
  kill the whole tree, and it stays inside this session so it dies with it.
  ``start_new_session=True`` is the pattern the security review objected to and
  it does not appear in this package.
* **Background runs belong to the session.** ``bg=true`` returns an id and the
  command keeps running between calls (``id=`` with ``action`` poll / status /
  kill / update). It is the same child as a foreground one -- own process
  *group*, same session, no ``setsid`` -- so it is killed when the session
  closes (:func:`close_background`), has a hard deadline, and is wrapped in
  ``timeout(1)`` so even a killed client cannot leave it running past six
  hours. There is no thread, ``atexit`` or signal handler behind it: deadlines
  are enforced when the session next touches the run. ``interactive`` REPLs and
  ``action=send`` are still refused by name with the reason: this client has no
  stdin to hold.
* **Bounded everything.** A wall-clock deadline, a byte cap on what reaches
  compaction, and a kill that escalates from the group's ``SIGTERM`` to
  ``SIGKILL``.

Before a command runs it passes the main package's command policy
(:mod:`lemoncrow_client.kit.command_policy`): destructive git operations are
refused, shell writes are confined to the worktree or OS scratch roots,
literal-file ``cat``/``head``/``tail``/``wc`` run in-process, and
``od <big file> | tail`` seeks. Shell execution itself may use those same
scratch roots as ``cwd`` so temporary worktrees and build directories work.

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
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..errors import AgentAction, ClientError, ErrorCode
from ..kit.spill import resolve_spill_dir
from . import LocalContext, LocalResult, text_result

if TYPE_CHECKING:
    from ..kit.bash_output import CompactedOutput

__all__ = ["background_running", "cancel_active_bash_commands", "close_background", "run_bash", "spill_text"]

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

_DEFAULT_BG_TIMEOUT_S: Final[float] = 3600.0
_MAX_BG_TIMEOUT_S: Final[float] = 21600.0
#: One poll never blocks longer than this, so a remote host's request window
#: (a tunnel cuts a response at ~100s) is never the thing that ends a poll.
_DEFAULT_POLL_WAIT_S: Final[float] = 30.0
_MAX_POLL_WAIT_S: Final[float] = 90.0
_MAX_BG_RUNS: Final[int] = 32
_BG_KEEP_FINISHED_S: Final[float] = 3600.0
_STATUS_TAIL_BYTES: Final[int] = 4000

_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()
#: Finished runs whose output a poll already delivered, remembered by id so a
#: second poll says so instead of "unknown id". Oldest dropped past the bound.
_MAX_RETIRED_BG: Final[int] = 64


def _shell() -> str | None:
    """The program commands run under: bash, as this tool's name promises.

    ``None`` keeps ``subprocess``'s platform default (``/bin/sh``, ``cmd.exe``)
    where no bash is installed, and always on Windows.
    """
    return shutil.which("bash") if os.name != "nt" else None


class _BackgroundRun:
    """One ``bash bg=true`` command: a process group writing to a private log."""

    __slots__ = (
        "command",
        "cwd",
        "deadline",
        "done",
        "finished_at",
        "id",
        "killed",
        "log_path",
        "offset",
        "process",
        "started",
        "timed_out",
    )

    def __init__(
        self, run_id: str, command: str, cwd: Path, process: subprocess.Popen[bytes], log_path: Path, deadline: float
    ) -> None:
        self.id = run_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.log_path = log_path
        self.started = time.monotonic()
        self.deadline = self.started + deadline
        self.offset = 0
        self.timed_out = False
        self.killed = False
        self.finished_at = 0.0


_ALL_BACKGROUND: set[_BackgroundRun] = set()
_ALL_BACKGROUND_LOCK = threading.Lock()


def cancel_active_bash_commands() -> int:
    """Terminate every bash process currently owned by this process.

    Foreground commands and ``bg=true`` runs alike.

    The normal thin-client process exits after the host session, so this is
    mostly useful to embedders such as the central remote-MCP transport. On
    server drain we must not wait up to a caller-selected 3600-second timeout
    before Python can exit; killing each command's process group lets its worker
    thread return promptly and preserves the existing no-orphan guarantee.
    """
    with _ACTIVE_PROCESSES_LOCK:
        processes = tuple(_ACTIVE_PROCESSES)
    with _ALL_BACKGROUND_LOCK:
        background = tuple(_ALL_BACKGROUND)
    for process in processes:
        _kill_group(process)
    for run in background:
        _kill_run(run)
    return len(processes) + len(background)


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


def spill_text(text: str, *, kind: str = "bash", suffix: str = ".txt") -> Path | None:
    """Persist *text* in the shared ephemeral spill directory.

    Returns the path, or ``None`` on any failure. Each kind keeps its own bounded
    retention.
    """
    try:
        directory = resolve_spill_dir()
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


def _present(
    context: LocalContext, command: str, raw: str, exit_code: int, cwd: Path, ran_note: str, *, dedupe: bool = True
) -> str:
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
        path = spill_text(text)
        return None if path is None else str(path)

    # Run-and-dedup, not a cache: the command ran. Its output is already in
    # this session's context, so a marker replaces it; the file keeps it.
    if dedupe and context.memory.command_runs.observe(
        command, cwd=str(cwd), stdout=raw, stderr="", exit_code=exit_code
    ):
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


def _kill_group(process: subprocess.Popen[Any]) -> None:
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


def _resolve_bash_cwd(context: LocalContext, raw: object | None) -> Path:
    """Resolve bash ``cwd`` inside the worktree or a conventional temp root.

    Relative paths stay worktree-relative. Absolute scratch paths are useful
    for disposable worktrees, build trees, formatter runs, extracted archives,
    and test fixtures, so they share the same temp-root allowance as shell
    writes. Symlinks are resolved before containment is checked.
    """
    if raw is None or not str(raw).strip():
        return context.repo_root
    spelled = str(raw).strip()
    candidate = Path(spelled)
    if not candidate.is_absolute():
        return context.resolve(spelled)
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            f"cwd is not resolvable: {spelled}",
            action=AgentAction.FIX_REQUEST,
        ) from exc

    from ..kit.command_policy import temporary_roots

    roots = (context.repo_root.resolve(), *temporary_roots())
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            "cwd is outside the worktree and allowed temporary directories",
            details={"path": spelled, "allowed_temp_roots": [str(root) for root in temporary_roots()]},
            action=AgentAction.FIX_REQUEST,
        )
    return resolved


def run_bash(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Run one or more shell commands, each in its own subshell."""
    if arguments.get("interactive"):
        raise _refuse(
            "the thin client has no interactive shell",
            "interactive=true keeps a REPL alive between tool calls, which this client has nowhere to hold; "
            "use bg=true for a long-running command and poll it by id",
        )
    if arguments.get("id") is not None or str(arguments.get("action") or "").strip() not in {"", "poll"}:
        return _background_action(context, arguments)

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
    cwd = _resolve_bash_cwd(context, cwd_argument)
    if not cwd.is_dir():
        # Name the resolved path: with no cwd argument this is the worktree
        # root itself, and printing the missing argument said only "None".
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            f"cwd is not a directory: {cwd}",
            action=AgentAction.FIX_REQUEST,
        )

    if arguments.get("bg"):
        return _start_background(context, arguments, commands, cwd)

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
            executable=_shell(),
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


# --------------------------------------------------------------------------- #
# bg=true: runs that belong to the session                                     #
# --------------------------------------------------------------------------- #
#
# No thread, no atexit, no signal handler: the package's audit forbids all
# three, and a background run needs none of them. Its deadline is enforced when
# the session next touches it, ``close_background`` runs when the session
# closes, and ``timeout(1)`` wraps the command as an OS-level cap that holds
# even if this process is killed outright.

_MAX_DRAIN_BYTES: Final[int] = 2_000_000


def _is_done(run: _BackgroundRun) -> bool:
    """Whether the run has ended; the first observation retires it from the live set."""
    if run.process.poll() is None:
        return False
    if not run.finished_at:
        run.finished_at = time.monotonic()
        with _ALL_BACKGROUND_LOCK:
            _ALL_BACKGROUND.discard(run)
        _reap_leftovers(run.process.pid)
    return True


def _enforce_deadline(run: _BackgroundRun) -> None:
    if run.process.poll() is None and time.monotonic() >= run.deadline:
        run.timed_out = True
        _kill_group(run.process)


def _kill_run(run: _BackgroundRun) -> None:
    run.killed = run.killed or run.process.poll() is None
    _kill_group(run.process)
    _is_done(run)


def _reap_leftovers(pid: int) -> None:
    """The command's shell is gone; take down anything it left in the group."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (OSError, ProcessLookupError):
            return
        if sig == signal.SIGTERM:
            grace_end = time.monotonic() + _TERM_GRACE_S
            while time.monotonic() < grace_end:
                time.sleep(0.05)
                try:
                    os.killpg(pid, 0)
                except (OSError, ProcessLookupError):
                    return


def _wait(run: _BackgroundRun, budget_s: float) -> None:
    """Block until the run ends, the budget is spent, or its deadline kills it."""
    end = time.monotonic() + budget_s
    while not _is_done(run):
        _enforce_deadline(run)
        if _is_done(run) or time.monotonic() >= end:
            return
        with contextlib.suppress(subprocess.TimeoutExpired):
            run.process.wait(timeout=max(0.0, min(1.0, end - time.monotonic())))


def _forget(run: _BackgroundRun) -> None:
    with contextlib.suppress(OSError):
        run.log_path.unlink()


def _purge_finished(context: LocalContext) -> None:
    """Drop finished runs nobody polled for an hour. Caller holds the lock."""
    now = time.monotonic()
    for run_id, run in tuple(context.memory.background.items()):
        if _is_done(run) and now - run.finished_at > _BG_KEEP_FINISHED_S:
            del context.memory.background[run_id]
            _retire(
                context,
                run_id,
                f"[exit {int(run.process.returncode or 0)}; unpolled for over an hour, output discarded]",
            )
            _forget(run)


def _retire(context: LocalContext, run_id: str, state: str) -> None:
    """Remember a finished run's last state line. Caller holds the lock."""
    retired = context.memory.retired_background
    retired.pop(run_id, None)
    retired[run_id] = state
    while len(retired) > _MAX_RETIRED_BG:
        del retired[next(iter(retired))]


def background_running(memory: Any) -> bool:
    """Whether this session still has a live ``bg=true`` run."""
    with memory.background_lock:
        runs = tuple(memory.background.values())
    for run in runs:
        _enforce_deadline(run)
    return any(not _is_done(run) for run in runs)


def close_background(memory: Any) -> int:
    """Kill and forget every ``bg=true`` run of one session; returns how many."""
    with memory.background_lock:
        runs = tuple(memory.background.values())
        memory.background.clear()
        memory.retired_background.clear()
    for run in runs:
        if not _is_done(run):
            _kill_run(run)
        _forget(run)
    return len(runs)


def _capped(exec_command: str) -> str:
    """Wrap the command in ``timeout(1)`` when present: the cap outlives this process."""
    binary = shutil.which("timeout")
    if binary is None:
        return exec_command
    shell = shlex.quote(_shell() or "sh")
    return f"exec {shlex.quote(binary)} --kill-after=5 {int(_MAX_BG_TIMEOUT_S)} {shell} -c {shlex.quote(exec_command)}"


def _start_background(
    context: LocalContext, arguments: Mapping[str, Any], commands: Sequence[str], cwd: Path
) -> LocalResult:
    if len(commands) != 1:
        raise _refuse("bg runs exactly one command", "start each long-running command as its own bg=true call")
    from ..kit.bash_output import inject_stable_flags
    from ..kit.command_policy import classify_command

    command = commands[0]
    policy = classify_command(command, allowed_write_roots=[context.repo_root], cwd=cwd)
    if policy.action == "block":
        return text_result(f"{policy.reason}\n[exit -1]", is_error=True)
    exec_command, _note = inject_stable_flags(command)

    # A bg run lives at least an hour: hosts fill `timeout` with a 120s default,
    # which would otherwise kill exactly the long job bg exists for.
    deadline_s = _DEFAULT_BG_TIMEOUT_S
    raw_timeout = arguments.get("timeout")
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and raw_timeout > 0:
        deadline_s = max(deadline_s, min(float(raw_timeout), _MAX_BG_TIMEOUT_S))

    log_dir = context.config.runtime_state_dir / "bash-bg"
    log_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        log_dir.chmod(0o700)
    with context.memory.background_lock:
        _purge_finished(context)
        if len(context.memory.background) >= _MAX_BG_RUNS:
            raise _refuse(
                "too many background runs in this session",
                f"{_MAX_BG_RUNS} are held; poll or kill one by id first",
            )
        run_id = uuid.uuid4().hex[:8]
        log_path = log_dir / f"{run_id}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            process = subprocess.Popen(
                _capped(exec_command),
                shell=True,
                executable=_shell(),
                cwd=str(cwd),
                env=dict(context.environment) or None,
                stdout=fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Own process *group*, same session -- never setsid -- so the
                # run dies with this client instead of outliving it.
                process_group=0,
            )
        except OSError as exc:
            with contextlib.suppress(OSError):
                log_path.unlink()
            raise ClientError(
                ErrorCode.LOCAL_TOOL_FAILED,
                f"could not start a shell: {exc}",
                action=AgentAction.ABANDON,
            ) from exc
        finally:
            os.close(fd)
        run = _BackgroundRun(run_id, command, cwd, process, log_path, deadline_s)
        context.memory.background[run_id] = run
    with _ALL_BACKGROUND_LOCK:
        _ALL_BACKGROUND.add(run)
    return text_result(
        f"[bg id={run_id} pid={process.pid} deadline={deadline_s:g}s]\n"
        f'Running in the background. bash(id="{run_id}") waits for it; action=status peeks, '
        "action=kill stops it, action=update timeout=N moves the deadline. "
        "It is killed when this session closes."
    )


def _read_delta(run: _BackgroundRun) -> tuple[str, bool]:
    """Output written since the last poll, and whether more is still unread."""
    try:
        with run.log_path.open("rb") as handle:
            handle.seek(run.offset)
            chunk = handle.read(_MAX_DRAIN_BYTES)
            more = bool(handle.read(1))
    except OSError:
        return "", False
    run.offset += len(chunk)
    return chunk.decode("utf-8", errors="replace"), more


def _peek_tail(run: _BackgroundRun) -> str:
    try:
        size = run.log_path.stat().st_size
        with run.log_path.open("rb") as handle:
            handle.seek(max(0, size - _STATUS_TAIL_BYTES))
            return handle.read(_STATUS_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _reply(context: LocalContext, run: _BackgroundRun, *, peek: bool) -> tuple[str, bool]:
    """One run's report: new output plus a state line. Returns ``(text, failed)``."""
    _enforce_deadline(run)
    more = False
    raw = _peek_tail(run) if peek else ""
    if not peek:
        raw, more = _read_delta(run)
    running = not _is_done(run)
    code = 0 if running else int(run.process.returncode or 0)
    body = _present(context, run.command, raw, code, run.cwd, "", dedupe=False) if raw else ""
    elapsed = int(time.monotonic() - run.started)
    if running:
        state = f"[running id={run.id} pid={run.process.pid} elapsed={elapsed}s]"
    else:
        why = "[killed]\n" if run.killed else "[timed out; process group terminated]\n" if run.timed_out else ""
        state = f"{why}[exit {code}]"
    if more:
        state = f"[more output pending -- poll again]\n{state}"
    if not running and not peek and not more:
        with context.memory.background_lock:
            context.memory.background.pop(run.id, None)
            _retire(context, run.id, state)
        _forget(run)
    return f"{body}\n{state}".strip("\n"), not running and code != 0


def _wait_budget(arguments: Mapping[str, Any]) -> float:
    raw = arguments.get("timeout")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), _MAX_POLL_WAIT_S)
    return _DEFAULT_POLL_WAIT_S


def _background_action(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """``bash(id=..., action=poll|status|kill|update)`` on ``bg=true`` runs."""
    raw_ids = arguments.get("id")
    if isinstance(raw_ids, (list, tuple)):
        ids = [str(entry) for entry in raw_ids]
    else:
        ids = [str(raw_ids)] if raw_ids is not None else []
    action = str(arguments.get("action") or "poll").strip() or "poll"
    if action == "send":
        raise _refuse(
            "the thin client has no interactive shell",
            "action=send writes to a REPL's stdin; only bg=true runs exist here",
        )
    if action not in {"poll", "status", "kill", "update"}:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"unknown bash action: {action}", action=AgentAction.FIX_REQUEST)
    if not ids:
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID, f"action={action} needs id=<bg id>", action=AgentAction.FIX_REQUEST
        )

    sections: list[str] = []
    failed = False
    for run_id in ids:
        with context.memory.background_lock:
            run = context.memory.background.get(run_id)
            retired = context.memory.retired_background.get(run_id) if run is None else None
        if run is None and retired is not None:
            # Asking again after the output was delivered is not an error.
            body, bad = f"[bg id {run_id} already finished; an earlier poll delivered its output]\n{retired}", False
        elif run is None:
            body, bad = (
                f"[unknown bg id {run_id}: no run with this id in this session; ids do not survive a restart]",
                True,
            )
        elif action == "status":
            body, bad = _reply(context, run, peek=True)
        elif action == "update":
            raw_timeout = arguments.get("timeout")
            if not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool) or raw_timeout <= 0:
                raise ClientError(
                    ErrorCode.PAYLOAD_INVALID, "action=update needs timeout=<seconds>", action=AgentAction.FIX_REQUEST
                )
            run.deadline = min(time.monotonic() + float(raw_timeout), run.started + _MAX_BG_TIMEOUT_S)
            body, bad = _reply(context, run, peek=True)
            body = f"[deadline moved to {run.deadline - time.monotonic():.0f}s from now]\n{body}"
        else:
            if action == "kill":
                if not _is_done(run):
                    _kill_run(run)
            else:
                _wait(run, _wait_budget(arguments))
            body, bad = _reply(context, run, peek=False)
        failed = failed or bad
        sections.append(f"## lc:bg {run_id}\n{body}" if len(ids) > 1 else body)
    return text_result("\n".join(sections), is_error=failed)
