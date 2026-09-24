"""Shell command execution with token-aware output compaction."""

from __future__ import annotations

import atexit
import codecs
import contextlib
import logging
import os
import secrets
import signal
import string
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lemoncrow_client.kit import output_delta
from lemoncrow_client.kit.bash_output import (
    DEFAULT_MAX_LINES,
    MAX_OUTPUT_BYTES,
    OUTPUT_CAP_NOTICE,
    cap_chars,
    compact_command_output,
    dedupe_repeated_lines,
    inject_stable_flags,
    output_budget,
    spill_footer,
    strip_ansi,
    unchanged_marker,
)
from lemoncrow_client.kit.bash_output_compression import compact_bash_stream
from lemoncrow_client.kit.bash_output_profiles import compact_profiled_output
from lemoncrow_client.kit.command_policy import CommandPolicyDecision, execute_inline_op
from lemoncrow_client.kit.command_policy import classify_command as _kit_classify_command
from lemoncrow_client.kit.redaction import redact_tool_output

from lemoncrow.core.environment import tool_output_spill_enabled
from lemoncrow.pro.capabilities.tool_supervision.external_compactors import (
    compactor_for_command,
    external_compactors_enabled,
    resolve_compactor,
    rewrite_compactor_command,
)

# On-disk ceiling for a managed command's temp spool. `subprocess.Popen` writes
# the child's output straight to the temp file's fd, so the read-time
# `MAX_OUTPUT_BYTES` cap cannot bound it -- `cat /dev/zero` would fill the disk
# before any poll runs. The spool pump (`_pump_capped`) stops appending once
# this ceiling is reached. Defaults to the output cap (read side then catches
# every truncated spool); a larger value retains more for later inspection.
_MAX_SPOOL_BYTES = max(
    MAX_OUTPUT_BYTES,
    int(os.environ.get("LEMONCROW_SHELL_MAX_SPOOL_BYTES", str(MAX_OUTPUT_BYTES))),
)

# Read granularity for `_pump_capped`; large enough to keep the drain loop cheap
# without buffering an unbounded amount per iteration, and used as the size hint
# for `readline()` so a single pathological line (no trailing newline) still has
# a bounded worst-case read instead of buffering unboundedly.
_PUMP_CHUNK_CHARS = 64 * 1024


def _read_capped(handle: Any) -> tuple[str, bool]:
    """Read at most the output-byte ceiling from a seeked temp-file *handle*.

    Reads one character past the cap to detect a larger file without slurping
    it whole, so memory stays bounded regardless of on-disk size. Returns
    (text, truncated).
    """
    chunk = handle.read(MAX_OUTPUT_BYTES + 1)
    if len(chunk) <= MAX_OUTPUT_BYTES:
        return chunk, False
    return chunk[:MAX_OUTPUT_BYTES], True


def _tail_lines_from_file(handle: Any, n: int) -> list[str]:
    """Return up to the last *n* lines currently written to *handle*.

    Text-mode file cursors only support seeking to 0 or a value previously
    returned by `tell()` -- arithmetic offsets (e.g. "tell() - 4096") are
    undefined for encoded streams -- so this reads from the start rather than
    seeking backward from the end, bounded by the same output-byte ceiling as
    a finished read. Restores the writer's append position before returning;
    callers must hold the file's `output_lock` so a concurrent write can't
    land between the seek(0) and the seek-back.
    """
    if n <= 0:
        return []
    pos = handle.tell()
    if pos == 0:
        return []
    handle.seek(0)
    text, _ = _read_capped(handle)
    handle.seek(pos)
    return text.splitlines()[-n:]


def _pump_capped(src: Any, write: Callable[[str], Any], cap: int) -> bool:
    """Copy text from *src* into *write*, appending at most *cap* UTF-8 bytes.

    Reads line-by-line (bounded by `_PUMP_CHUNK_CHARS` per call) until EOF.
    `readline()` returns as soon as a line is available instead of blocking
    until a full fixed-size chunk is read (as a plain buffered `.read(n)` does
    on a non-interactive pipe) -- required so a `status` peek on a still-running
    command sees output as it's produced rather than only once `_PUMP_CHUNK_CHARS`
    has accumulated or the process exits. The size hint still bounds a single
    call's read for pathological output with no newlines. Once the running byte
    count reaches *cap* the overflow is read and discarded rather than written,
    so the source pipe keeps draining (no deadlock when both stdout and stderr
    are large) while the in-memory or on-disk sink stays bounded. Byte
    accounting mirrors `cap_output_bytes`, cutting a straddling chunk on a character
    boundary at or just under the cap. Returns True if the stream exceeded the
    cap.
    """
    written = 0
    truncated = False
    while True:
        chunk = src.readline(_PUMP_CHUNK_CHARS)
        if not chunk:
            break
        if written >= cap:
            truncated = True
            continue
        encoded = chunk.encode("utf-8", "replace")
        if written + len(encoded) <= cap:
            write(chunk)
            written += len(encoded)
            continue
        prefix = encoded[: cap - written].decode("utf-8", "replace")
        if prefix:
            write(prefix)
        written = cap
        truncated = True
    return truncated


class _LossyStreamReader:
    """Line-oriented text view over a child's *binary* pipe with exact
    lossy-decode detection and raw-byte recovery.

    Decodes utf-8 with ``errors="replace"`` -- a strict decode would raise in
    the reader thread on the first invalid byte, silently emptying the whole
    stream (the pre-fix failure mode). Substitution is detected *exactly* by a
    parallel strict incremental checker over the same bytes: a genuine U+FFFD
    already present in valid utf-8 input does NOT flag the stream, which a
    "count U+FFFD in the decoded text" heuristic would misreport.

    When *raw_file* is given, the raw bytes are teed to it (capped at
    *raw_cap*) so a lossy stream stays byte-exact recoverable; ``finalize()``
    drops the copy when the stream decoded cleanly, so clean commands leave no
    extra file behind.
    """

    def __init__(self, raw: Any, *, raw_file: Any = None, raw_path: str = "", raw_cap: int = 0) -> None:
        self._raw = raw
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._checker: Any = codecs.getincrementaldecoder("utf-8")("strict")
        self._raw_file = raw_file
        self.raw_path = raw_path
        self._raw_cap = raw_cap
        self._raw_written = 0
        self._buf = ""
        self._eof = False
        self._finalized = False
        self.lossy = False

    def _observe(self, chunk: bytes, *, final: bool = False) -> None:
        if self._checker is None:
            return
        try:
            self._checker.decode(chunk, final)
        except UnicodeDecodeError:
            self.lossy = True
            self._checker = None  # one flag suffices; stop paying for the strict pass

    def _tee(self, chunk: bytes) -> None:
        if self._raw_file is None or self._raw_written >= self._raw_cap:
            return
        take = chunk[: self._raw_cap - self._raw_written]
        with contextlib.suppress(Exception):
            self._raw_file.write(take)
            self._raw_written += len(take)

    def readline(self, size: int = -1) -> str:
        """Next line (or at most *size* chars), "" at EOF -- the same contract
        `_pump_capped` relies on for text-mode pipes."""
        limit = size if size > 0 else _PUMP_CHUNK_CHARS
        while True:
            newline = self._buf.find("\n")
            if 0 <= newline < limit:
                line, self._buf = self._buf[: newline + 1], self._buf[newline + 1 :]
                return line
            if len(self._buf) >= limit:
                line, self._buf = self._buf[:limit], self._buf[limit:]
                return line
            if self._eof:
                line, self._buf = self._buf, ""
                return line
            chunk = self._raw.readline(limit)
            if not chunk:
                self._eof = True
                self._observe(b"", final=True)  # a truncated multi-byte seq at EOF is lossy too
                self._buf += self._decoder.decode(b"", True)
                continue
            self._tee(chunk)
            self._observe(chunk)
            self._buf += self._decoder.decode(chunk)

    def finalize(self) -> None:
        """Close the raw tee; drop the copy when the stream decoded cleanly.
        Idempotent -- called from the spool thread at EOF and again from reap
        paths to cover the wedged-reader case."""
        if self._finalized:
            return
        self._finalized = True
        if self._raw_file is not None:
            with contextlib.suppress(Exception):
                self._raw_file.close()
        if not self.lossy and self.raw_path:
            with contextlib.suppress(OSError):
                Path(self.raw_path).unlink(missing_ok=True)
            with _LOG_PATHS_LOCK:
                _ALL_LOG_PATHS.discard(self.raw_path)
            self.raw_path = ""


def _lossy_notice(stream: str, raw_path: str) -> str:
    """One-line integrity warning prepended to a stream that decoded lossily,
    so U+FFFD substitution is loud (with a byte-exact recovery path) instead
    of silently readable as real output."""
    recovery = f"; exact bytes: {raw_path}" if raw_path else "; raw bytes were not captured"
    return (
        f"[lc: WARNING {stream} contained non-UTF8 bytes -- invalid bytes shown as U+FFFD, "
        f"text is NOT byte-exact{recovery}]"
    )


def _spill_to_store(full_text: str) -> str | None:
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(full_text, tool_name="bash", kind="original")
    return None if record is None else str(record.path)


def _spill_hint(full_text: str, kept_chars: int) -> str:
    """Persist the untrimmed *full_text* to the shared spill store; return the footer.

    Head+tail trimming keeps an "(N lines omitted)" marker but drops the lines;
    without the spill there is no way back to them short of re-running an often
    expensive, non-idempotent command. Returns "" when spill is disabled, the
    trim is too small, or the write fails.
    """
    if not tool_output_spill_enabled():
        return ""
    return spill_footer(full_text, kept_chars, _spill_to_store)


def _compact_result(
    *,
    command: str,
    raw_stdout: str,
    raw_stderr: str,
    exit_code: int,
    duration_ms: int,
    max_lines: int,
    max_chars: int | None = None,
) -> RunResult:
    """The shared output pipeline, with this package's spill store behind it."""
    compacted = compact_command_output(
        command,
        raw_stdout,
        raw_stderr,
        exit_code,
        max_lines=max_lines,
        max_chars=max_chars,
        spill=_spill_hint,
    )
    return RunResult(
        stdout=compacted.stdout,
        stderr=compacted.stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=compacted.truncated,
        lines_omitted=compacted.lines_omitted,
        chars_omitted=compacted.chars_omitted,
        command=command,
        spill_hint=compacted.spill_hint,
    )


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool
    lines_omitted: int
    command: str
    chars_omitted: int = 0
    policy_category: str = "generic"
    policy_action: str = "allow"
    policy_reason: str = ""
    rewrite_target: str | None = None
    rewrite_payload: dict[str, Any] | None = None
    spill_hint: str = ""


@dataclass
class _ManagedCommand:
    command: str
    proc: subprocess.Popen[bytes]
    stdout_file: Any
    stderr_file: Any
    started: float
    timeout: float
    max_lines: int
    max_chars: int | None = None
    # Only an explicit MCP `bg=true` command may survive MCP shutdown. A
    # foreground command that merely exceeded its soft response budget remains
    # owned by the MCP session and is terminated when that session exits.
    explicit_background: bool = False
    # Host session/bridge that launched this command. Lets a single session's
    # foreground shells be reaped when it disconnects from a shared (singleton)
    # MCP daemon, without touching other sessions' commands. "" on the stdio
    # path (one process per session), where process-exit cleanup covers all.
    owner: str = ""
    # Set only by update_managed_command (bash action="update"). Once
    # true, `timeout` is the exact kill deadline instead of a soft response
    # budget; see _effective_deadline_s.
    deadline_explicit: bool = False
    state: str = "running"
    # First-line provenance when inject_stable_flags modified the executed
    # command; prepended to the compacted stdout at poll time.
    injected_note: str = ""
    reaped: bool = False
    # Drain threads spooling the child's piped output into the temp files, and a
    # flag set when either spool hit the on-disk ceiling. Joining the threads
    # before a read guarantees all surviving bytes are flushed to disk.
    readers: list[threading.Thread] = field(default_factory=list)
    spool_truncated: bool = False
    # Guards stdout_file/stderr_file cursor + write operations shared between
    # the spool drain threads and a `status` peek (the peek repositions the
    # cursor to read a tail without disturbing the writer's append position).
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    # Phase 2 deferred bash: completion callbacks the watcher fires once the
    # process finishes. Snapshotted+cleared under the lock, invoked outside it.
    on_complete: list[Callable[[], None]] = field(default_factory=list)
    # Real filesystem paths for stdout_file/stderr_file when they were opened
    # on-disk (see _BASH_LOG_DIR below) -- "" when a stream fell back to an
    # anonymous tempfile (e.g. the log dir wasn't writable), which has no path.
    stdout_path: str = ""
    stderr_path: str = ""
    # Text-view readers wrapping the child's binary pipes (exact lossy-decode
    # detection + raw-byte tee; see _LossyStreamReader). Their raw spool file
    # is kept only when a stream actually contained invalid UTF-8.
    stdout_reader: Any = None
    stderr_reader: Any = None
    # Interactive REPL session (bash interactive=true): stdin stays open as a
    # private pipe, action="send" (send_managed_input) feeds it, and the
    # session dies once it has gone `idle_ttl` seconds without input -- a
    # sliding window measured from `last_input` (see _effective_deadline_s).
    interactive: bool = False
    idle_ttl: float = 0.0
    last_input: float = 0.0
    # Opaque text-mode tell() cookies consumed by the previous send, so each
    # send returns only the output produced since the last one (including
    # anything that arrived between sends).
    stdout_read_offset: int = 0
    stderr_read_offset: int = 0


_MANAGED_COMMANDS: dict[str, _ManagedCommand] = {}
_MANAGED_COMMANDS_LOCK = threading.Lock()
# Grace period before the watcher reaps a finished-but-never-polled session,
# so a poll that arrives just after completion still finds its output.
_DETACHED_REAP_GRACE_S = 300.0
# How long poll_managed_command / the watcher's own reap wait for the spool
# drain threads to hit EOF once the wrapped process itself has already
# exited, before giving up and shipping whatever's on disk so far. A detached
# descendant that still holds a duplicate of the output pipe open (e.g. a
# server a task explicitly asked to "keep running in the background") would
# otherwise wedge that join forever even though the command we actually ran
# is long dead.
_READER_JOIN_GRACE_S = 2.0
# Real kill deadline for a managed command, independent of the per-call
# `timeout` a caller passes. `timeout` is a *soft* budget -- see
# _run_bash_tool's deferred branch in mcp_server.py -- for how long the MCP
# tool call waits before handing the model a "still running" session handle
# instead of blocking further; it is NOT how long the command itself is
# allowed to keep running. A command a task deliberately backgrounds (start a
# server, `mailman start`, ...) must survive well past a short/default
# `timeout` or every such task loses its own service partway through. This
# hard cap is the actual backstop against a forgotten/orphaned process
# running forever.
_MANAGED_COMMAND_HARD_CAP_S = 3600.0
# Absolute ceiling on an action="update"-installed explicit deadline (see
# _ManagedCommand.deadline_explicit and update_managed_command): the real
# backstop against a caller granting a forgotten background job unbounded
# life one update at a time -- every update is clamped to this no matter how
# many times a session gets extended.
_MAX_EXPLICIT_TIMEOUT_S = 604800.0  # 7 days
# Sliding idle-TTL default for an interactive session (bash interactive=true):
# after this long without a send the watcher kills the session. Every
# action="send" resets the window; the value is clamped to
# [1, _MANAGED_COMMAND_HARD_CAP_S] at session start.
_DEFAULT_IDLE_TTL_S = 300.0
# A send returns once neither stream has grown for this long (quiescence
# framing -- REPL-agnostic, no sentinel injected into the child's language).
_SEND_QUIESCENCE_S = 0.25
_SEND_POLL_SLICE_S = 0.05
# stdout_file/stderr_file (the managed command's only spool -- no separate
# tee/mirror) live here when writable, instead of an anonymous tempfile with
# no filesystem path. One write per stream, on a real path a user can
# `tail -f`. Deliberately NOT deleted at reap -- a command that just finished
# is exactly when someone is most likely to want to look back at its log; it
# survives until this process's own cleanup (_cleanup_all_log_files, atexit)
# or the staleness sweep below.
_BASH_LOG_DIR = Path(tempfile.gettempdir()) / "lemoncrow-bash"
# Every real path this process has opened under _BASH_LOG_DIR, so process-exit
# cleanup removes exactly its own files and nothing a concurrent gateway
# process (a second Claude Code session on the same machine) is still writing.
_ALL_LOG_PATHS: set[str] = set()
_LOG_PATHS_LOCK = threading.Lock()


def _cleanup_all_log_files() -> None:
    """Remove every on-disk log file this process opened. Registered via
    atexit -- this is this process's "session stop": once it exits, nothing
    can tail a log it owned, so there is nothing left to preserve. Doesn't run
    on a hard kill (SIGKILL/crash); the staleness sweep below is the backstop.
    """
    with _LOG_PATHS_LOCK:
        paths = list(_ALL_LOG_PATHS)
    for raw_path in paths:
        with contextlib.suppress(OSError):
            Path(raw_path).unlink(missing_ok=True)


atexit.register(_cleanup_all_log_files)


# Backstop for log files that outlive this process without going through
# _cleanup_all_log_files -- a hard kill/crash (no atexit ever runs), or a
# stale leftover from a previous crashed process sharing the same tmp dir.
# The age cutoff is well past any real command's runtime, so it never touches
# a file another still-running process is actively writing to.
_STALE_LOG_MAX_AGE_S = 3600.0


def _sweep_stale_log_files(directory: Path, *, max_age_s: float = _STALE_LOG_MAX_AGE_S) -> None:
    with contextlib.suppress(OSError):
        cutoff = time.time() - max_age_s
        for entry in directory.iterdir():
            with contextlib.suppress(OSError):
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()


def _open_stream_file(session_id: str, stream_name: str) -> tuple[Any, str]:
    """Open the real, named spool file for one stream of a managed command.

    Falls back to an anonymous tempfile (no path, not tailable) if the named
    file can't be opened -- logging must never block a command from running.
    """
    try:
        _BASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Command output can carry secrets and is spooled unredacted, so keep the
        # dir and files owner-only rather than world-readable in a shared /tmp.
        # chmod is best-effort (a dir owned by another session's user can't be
        # re-permed, and that's fine — we only created our own).
        with contextlib.suppress(OSError):
            os.chmod(_BASH_LOG_DIR, 0o700)
        _sweep_stale_log_files(_BASH_LOG_DIR)
        path = _BASH_LOG_DIR / f"{session_id}.{stream_name}.txt"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        handle = os.fdopen(fd, "w+", encoding="utf-8")  # lives on managed, closed at reap
    except OSError:
        return tempfile.TemporaryFile(mode="w+", encoding="utf-8"), ""
    with _LOG_PATHS_LOCK:
        _ALL_LOG_PATHS.add(str(path))
    return handle, str(path)


def _open_raw_stream_file(session_id: str, stream_name: str) -> tuple[Any, str]:
    """Best-effort binary spool for the raw (pre-decode) bytes of one stream.

    Kept only when the stream turns out lossy (see _LossyStreamReader.finalize);
    a clean stream's copy is unlinked at EOF. Returns (None, "") when the log
    dir isn't writable -- raw recovery is then unavailable, never blocking.
    """
    try:
        _BASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(_BASH_LOG_DIR, 0o700)
        path = _BASH_LOG_DIR / f"{session_id}.{stream_name}.bin"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        handle = os.fdopen(fd, "wb")
    except OSError:
        return None, ""
    with _LOG_PATHS_LOCK:
        _ALL_LOG_PATHS.add(str(path))
    return handle, str(path)


_SESSION_ID_ALPHABET = string.ascii_lowercase + string.digits  # base-36


def _new_session_id() -> str:
    """Short, typeable id for a managed bash session.

    6-char base-36 (a-z, 0-9) gives 36^6 ≈ 2.2e9 space — plenty to avoid
    collisions with the live-session table checked below.  Shorter and
    more readable than the old 8-char hex slice.  Falls back to a longer
    id only after repeated clashes (effectively unreachable).
    """
    for width in (6, 6, 10, 16):
        candidate = "".join(secrets.choice(_SESSION_ID_ALPHABET) for _ in range(width))
        with _MANAGED_COMMANDS_LOCK:
            if candidate not in _MANAGED_COMMANDS:
                return candidate
    return "".join(secrets.choice(_SESSION_ID_ALPHABET) for _ in range(32))


def _external_compactor(tokens: list[str], command: str) -> CommandPolicyDecision | None:
    """Pass a command through an installed external compactor (``rtk``), if any."""
    if not external_compactors_enabled():
        return None
    compactor = compactor_for_command(tokens)
    if compactor is None:
        return None
    resolution = resolve_compactor(compactor.name)
    rewritten_command = rewrite_compactor_command(compactor, resolution, command)
    if not rewritten_command:
        return None
    return CommandPolicyDecision(
        category="external-compactor",
        action="rewrite",
        reason=f"passed through installed `{compactor.name}` binary for compact output",
        rewrite_target="external_compactor",
        rewrite_payload={
            "compactor": compactor.name,
            "binary_path": str(resolution.path),
            "original_command": command,
            "command": rewritten_command,
        },
    )


def classify_command(
    command: str, *, allowed_write_roots: list[Path] | None = None, cwd: str | Path | None = None
) -> CommandPolicyDecision:
    """The kit's command policy, with this package's external compactors."""
    return _kit_classify_command(
        command, allowed_write_roots=allowed_write_roots, cwd=cwd, fallback=_external_compactor
    )


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def compact_host_bash_output(command: str, stdout: str, stderr: str, exit_code: int | None) -> RunResult:
    """Post-hoc compaction entry for HOST-lane (builtin Bash tool) output.

    The same pipeline the MCP bash lane applies after execution -- ANSI strip,
    dedup-with-count, test-failure extraction, suppress-on-success, anomaly
    windows, per-kind char budgets, spill recovery, secret redaction. Used by
    the Claude plugin's PostToolUse hook (bash_output_shrink.py), which owns
    the result of a command that already ran exactly once -- so unlike
    wrapper-style compactors this is safe for side-effecting commands.

    An unknown exit code is treated as failure: suppress-on-success must never
    collapse a run we cannot prove succeeded.
    """
    return _compact_result(
        command=command,
        raw_stdout=stdout,
        raw_stderr=stderr,
        exit_code=exit_code if exit_code is not None else 1,
        duration_ms=0,
        max_lines=200,
    )


def _neutralize_pipe_fds(*streams: Any) -> None:
    """Release the read ends of a wedged reader's pipes without the double-close
    hazard of os.close(fd).

    A reader stuck in readline() still owns the TextIOWrapper wrapping the fd;
    that wrapper's finalizer (or subprocess teardown) closes the fd *again*
    later -- and if the raw fd number was recycled in the meantime, that second
    close() silently closes an unrelated resource (surfaced as stray
    "Bad file descriptor" noise at best, a wrong-fd close at worst). dup2 of
    /dev/null onto the fd instead keeps the number *allocated* (now pointing at
    /dev/null), so the wrapper's eventual close is a valid single close of a
    still-live fd, and a stuck reader's next read sees EOF.

    fileno()/dup2 never touch the BufferedReader lock the reader holds while
    blocked in readline(), so -- unlike stream.close() -- this can't deadlock
    against the in-flight read. The fd is stably owned by the live wrapper, so
    it can't be recycled between fileno() and dup2 either.
    """
    null_fd: int | None = None
    try:
        null_fd = os.open(os.devnull, os.O_RDONLY)
        for stream in streams:
            if stream is None:
                continue
            with contextlib.suppress(Exception):
                os.dup2(null_fd, stream.fileno())
    finally:
        if null_fd is not None:
            with contextlib.suppress(Exception):
                os.close(null_fd)


def _close_managed_process_pipes(managed: _ManagedCommand) -> None:
    _neutralize_pipe_fds(managed.proc.stdout, managed.proc.stderr)


def _finish_managed_readers(managed: _ManagedCommand, grace_s: float) -> bool:
    reader_wedged = _join_readers_within(managed.readers, grace_s)
    if reader_wedged:
        _close_managed_process_pipes(managed)
        _join_readers_within(managed.readers, 0.2)
    return reader_wedged


def _join_readers_within(readers: list[threading.Thread], grace_s: float) -> bool:
    """Join every reader thread against one shared deadline, not `grace_s`
    per reader -- a naive `for r in readers: r.join(timeout=grace_s)` lets N
    readers extend the real bound to N * grace_s, and lets a reader that
    already timed out quietly finish in the background while a later
    reader's join keeps running, making it look non-wedged by the time
    anyone checks. Returns True if any reader is still alive once the
    shared deadline passes.
    """
    deadline = time.monotonic() + grace_s
    for reader in readers:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            reader.join(timeout=remaining)
    return any(reader.is_alive() for reader in readers)


def _effective_deadline_s(managed: _ManagedCommand) -> float:
    """Seconds since start at which the watcher kills the process.

    A start-time ``timeout`` never kills early -- but a caller who explicitly
    asked to wait LONGER than the one-hour backstop must be respected: the
    process lives at least ``timeout`` seconds (a 6h build with timeout=21600
    must not be killed out from under its own waiting call at 1h). For
    default/shorter timeouts the fixed backstop still reaps forgotten
    processes; ``action="update"`` installs an exact deadline.

    An interactive session instead expires ``idle_ttl`` seconds after its most
    recent input (send_managed_input bumps ``last_input``). Each send is a
    deliberate act -- like ``action="update"`` -- so an actively-fed session
    may outlive the fixed hard cap; an *idle* one always dies within
    ``idle_ttl``. The watcher re-reads this every poll slice, so a send moves
    the deadline live.
    """
    if managed.deadline_explicit:
        return float(managed.timeout)
    if managed.interactive and managed.idle_ttl > 0:
        return (managed.last_input - managed.started) + managed.idle_ttl
    return max(_MANAGED_COMMAND_HARD_CAP_S, float(managed.timeout or 0.0))


def _watch_managed_command(session_id: str) -> None:
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
    if managed is None:
        return
    # Polled in short slices rather than one blocking `proc.wait(timeout=...)`
    # so an action="update" call can move the deadline (managed.timeout /
    # managed.deadline_explicit) without restarting this wait -- each
    # iteration re-reads it live.
    poll_slice_s = 1.0
    while True:
        with _MANAGED_COMMANDS_LOCK:
            deadline = managed.started + _effective_deadline_s(managed)
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            try:
                managed.proc.wait(timeout=0)
            except subprocess.TimeoutExpired:
                # Set the terminal state *before* signalling the process --
                # otherwise a concurrent poll_managed_command can observe the
                # (already-dead) process first and mark it "completed" before
                # this thread gets to flag it "timed_out" (same ordering the
                # cancel path already uses, for the same reason).
                with _MANAGED_COMMANDS_LOCK:
                    if managed.state == "running":
                        managed.state = "timed_out"
                _terminate_process_group(managed.proc)
            else:
                with _MANAGED_COMMANDS_LOCK:
                    if managed.state == "running":
                        managed.state = "completed"
            break
        try:
            managed.proc.wait(timeout=min(remaining, poll_slice_s))
        except subprocess.TimeoutExpired:
            continue
        else:
            with _MANAGED_COMMANDS_LOCK:
                if managed.state == "running":
                    managed.state = "completed"
            break

    # Phase 2 deferred bash: the process has finished, so fire any registered
    # completion callbacks now. A callback collects the result and writes the MCP
    # response (it calls poll_managed_command, which reaps the session), so the
    # grace-sleep+reap below then no-ops. Snapshot under the lock; invoke outside
    # it (the callback re-enters poll_managed_command's lock).
    with _MANAGED_COMMANDS_LOCK:
        cbs = list(managed.on_complete)
        managed.on_complete.clear()
    for cb in cbs:
        with contextlib.suppress(Exception):
            cb()

    # The process has finished. If no one polls the result, its temp files and
    # dict entry would leak forever, so reap it after a grace window. A poll that
    # arrives first reaps it under the lock and clears the entry; this then no-ops.
    time.sleep(_DETACHED_REAP_GRACE_S)
    with _MANAGED_COMMANDS_LOCK:
        if _MANAGED_COMMANDS.get(session_id) is not managed or managed.reaped:
            return
        managed.reaped = True
        _MANAGED_COMMANDS.pop(session_id, None)
    # Let the spool drains finish before closing their temp files; the process
    # has already exited, so the pipes normally EOF and the joins return at
    # once. Bounded for the same reason as poll_managed_command's join below --
    # a still-open duplicate of the pipe (e.g. a detached backgrounded server)
    # must not wedge this cleanup thread forever.
    _finish_managed_readers(managed, _READER_JOIN_GRACE_S)
    with contextlib.suppress(Exception):
        managed.stdout_file.close()
    with contextlib.suppress(Exception):
        managed.stderr_file.close()
    for stream_reader in (managed.stdout_reader, managed.stderr_reader):
        if stream_reader is not None:
            stream_reader.finalize()


def _spool_managed_stream(stream: Any, dst_file: Any, managed: _ManagedCommand) -> None:
    """Drain *stream* into *dst_file*, capped at the on-disk spool ceiling.

    Runs for the command's lifetime in a daemon thread; `_pump_capped` stops
    appending once `_MAX_SPOOL_BYTES` is reached but keeps reading to EOF so the
    child never blocks on a full pipe. Flags the session as spool-truncated when
    either stream overflows. Writes go through `managed.output_lock` so a
    concurrent `status` peek can't interleave a cursor move with a write.
    """

    def _locked_write(text: str) -> None:
        with managed.output_lock:
            dst_file.write(text)

    with contextlib.suppress(Exception):
        truncated = _pump_capped(stream, _locked_write, _MAX_SPOOL_BYTES)
        if truncated:
            with _MANAGED_COMMANDS_LOCK:
                managed.spool_truncated = True
    if isinstance(stream, _LossyStreamReader):
        stream.finalize()


def start_managed_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: int = 30,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars: int | None = None,
    note: str = "",
    explicit_background: bool = False,
    interactive: bool = False,
    idle_ttl: float | None = None,
    owner: str = "",
) -> dict[str, Any]:
    """Start a command without blocking the MCP request.

    ``timeout`` is only the soft response budget. The process uses the fixed
    one-hour safety cap unless ``action="update"`` installs an exact deadline.
    Foreground commands remain MCP-session-owned after that budget; only
    ``explicit_background=True`` commands survive MCP shutdown.

    ``interactive=True`` opens the child with a live stdin pipe and a sliding
    ``idle_ttl`` kill window instead of the hard cap -- a long-lived REPL fed
    via ``send_managed_input`` that dies on its own once it goes unused.

    *note*, when given, seeds the managed command's ``injected_note`` (prepended
    to the compacted stdout at poll time) -- used by a caller that rewrote the
    command (e.g. a pipeline seek) to tell the model what actually ran.
    """
    policy = classify_command(command, cwd=cwd)
    if policy.action == "block":
        return {
            "status": "blocked",
            "stderr": policy.reason,
            "exit_code": -1,
            "blocked": True,
            "blocked_reason": policy.reason,
        }

    exec_command, injected_note = inject_stable_flags(command)
    # A caller-supplied note (e.g. a pipeline rewrite) wins: the rewritten
    # command won't trigger flag injection, and the note explains the transform.
    injected_note = note or injected_note
    session_id = _new_session_id()
    stdout_file, stdout_path = _open_stream_file(session_id, "stdout")
    stderr_file, stderr_path = _open_stream_file(session_id, "stderr")
    stdout_raw, stdout_raw_path = _open_raw_stream_file(session_id, "stdout")
    stderr_raw, stderr_raw_path = _open_raw_stream_file(session_id, "stderr")
    try:
        # Pipe the child's output through drain threads rather than handing the
        # temp-file fds straight to the kernel. A direct fd lets a runaway
        # producer (`cat /dev/zero`) fill the disk before any poll reads it; the
        # spool pump caps each temp file at `_MAX_SPOOL_BYTES` instead.
        # stdin=DEVNULL: the MCP server's stdin is an open JSON-RPC pipe, so
        # inheriting it causes any child that reads stdin (e.g. `sys.stdin.read()`
        # in a python -c snippet) to block forever instead of failing fast.
        # Interactive sessions instead get a private PIPE that action="send"
        # (send_managed_input) feeds -- no inheritance hazard.
        # start_new_session=True calls setsid() in the child, placing it in
        # its own session and process group.  This has two effects:
        #   1. The child is detached from the MCP server's process group --
        #      if the MCP process dies the child is NOT sent SIGHUP and keeps
        #      running (safe background / long-lived server use-case).
        #   2. _terminate_process_group() can cleanly kill the whole subtree
        #      via SIGTERM/SIGKILL to the child's own pgid.
        proc = subprocess.Popen(
            ["bash", "-c", exec_command],
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Binary pipes: decoding happens in _LossyStreamReader (utf-8 with
            # errors="replace" -- a strict decode would raise in the reader
            # thread on the first invalid byte, silently emptying the whole
            # stream), so substitution is detected exactly and the raw bytes
            # stay recoverable when a stream turns out non-UTF8.
            cwd=cwd,
            start_new_session=True,
        )
    except Exception:
        stdout_file.close()
        stderr_file.close()
        for raw_handle in (stdout_raw, stderr_raw):
            if raw_handle is not None:
                with contextlib.suppress(Exception):
                    raw_handle.close()
        raise
    stdout_reader = _LossyStreamReader(
        proc.stdout, raw_file=stdout_raw, raw_path=stdout_raw_path, raw_cap=_MAX_SPOOL_BYTES
    )
    stderr_reader = _LossyStreamReader(
        proc.stderr, raw_file=stderr_raw, raw_path=stderr_raw_path, raw_cap=_MAX_SPOOL_BYTES
    )

    now = time.perf_counter()
    managed = _ManagedCommand(
        command=command,
        proc=proc,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        started=now,
        timeout=timeout,
        max_lines=max_lines,
        max_chars=max_chars,
        explicit_background=explicit_background,
        injected_note=injected_note,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_reader=stdout_reader,
        stderr_reader=stderr_reader,
        interactive=interactive,
        idle_ttl=(
            min(max(float(idle_ttl if idle_ttl is not None else _DEFAULT_IDLE_TTL_S), 1.0), _MANAGED_COMMAND_HARD_CAP_S)
            if interactive
            else 0.0
        ),
        last_input=now,
    )
    managed.owner = owner
    managed.readers = [
        threading.Thread(target=_spool_managed_stream, args=(stdout_reader, stdout_file, managed), daemon=True),
        threading.Thread(target=_spool_managed_stream, args=(stderr_reader, stderr_file, managed), daemon=True),
    ]
    for reader in managed.readers:
        reader.start()
    with _MANAGED_COMMANDS_LOCK:
        _MANAGED_COMMANDS[session_id] = managed
    threading.Thread(
        target=_watch_managed_command,
        args=(session_id,),
        daemon=True,
        name=f"lemoncrow-shell-{session_id[:8]}",
    ).start()
    started_payload = {
        "status": "running",
        "session_id": session_id,
        "pid": proc.pid,
        "timeout": timeout,
        "explicit_background": managed.explicit_background,
    }
    if interactive:
        started_payload["interactive"] = True
        started_payload["idle_ttl"] = managed.idle_ttl
    if managed.stdout_path:
        started_payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        started_payload["log_file_stderr"] = managed.stderr_path
    return started_payload


def cleanup_managed_commands() -> dict[str, list[dict[str, Any]]]:
    """Terminate live MCP-owned commands and preserve explicit background jobs.

    Idempotent and suitable for both the MCP server's ``finally`` block and
    interpreter ``atexit`` cleanup. Process-group termination covers every
    descendant of each foreground shell, not only its immediate PID.
    """
    terminated: list[tuple[str, _ManagedCommand]] = []
    preserved: list[dict[str, Any]] = []
    with _MANAGED_COMMANDS_LOCK:
        for session_id, managed in _MANAGED_COMMANDS.items():
            if managed.proc.poll() is not None:
                continue
            details: dict[str, Any] = {
                "session_id": session_id,
                "pid": managed.proc.pid,
            }
            if managed.stdout_path:
                details["log_file"] = managed.stdout_path
            if managed.stderr_path:
                details["log_file_stderr"] = managed.stderr_path
            if managed.explicit_background:
                preserved.append(details)
                continue
            if managed.state == "running":
                managed.state = "cancelled"
            terminated.append((session_id, managed))

    terminated_details: list[dict[str, Any]] = []
    for session_id, managed in terminated:
        _terminate_process_group(managed.proc)
        details = {"session_id": session_id, "pid": managed.proc.pid}
        if managed.stdout_path:
            details["log_file"] = managed.stdout_path
        if managed.stderr_path:
            details["log_file_stderr"] = managed.stderr_path
        terminated_details.append(details)
    return {"terminated": terminated_details, "preserved": preserved}


def cleanup_commands_for_owner(owner: str) -> dict[str, list[dict[str, Any]]]:
    """Terminate one owner's live foreground commands; preserve explicit bg.

    Called when a single host session (bridge) disconnects from a shared
    singleton MCP daemon: only that session's foreground shells are reaped --
    other sessions' commands and any ``bg=true`` jobs keep running (bg is
    expected to outlive its launching session, matching stdio semantics). No-op
    for a falsy owner (e.g. the stdio path, where nothing tags an owner).
    """
    if not owner:
        return {"terminated": [], "preserved": []}
    terminated: list[tuple[str, _ManagedCommand]] = []
    preserved: list[dict[str, Any]] = []
    with _MANAGED_COMMANDS_LOCK:
        for session_id, managed in _MANAGED_COMMANDS.items():
            if managed.owner != owner or managed.proc.poll() is not None:
                continue
            if managed.explicit_background:
                preserved.append({"session_id": session_id, "pid": managed.proc.pid})
                continue
            if managed.state == "running":
                managed.state = "cancelled"
            terminated.append((session_id, managed))
    terminated_details: list[dict[str, Any]] = []
    for session_id, managed in terminated:
        _terminate_process_group(managed.proc)
        terminated_details.append({"session_id": session_id, "pid": managed.proc.pid})
    return {"terminated": terminated_details, "preserved": preserved}


atexit.register(cleanup_managed_commands)


def _tail_managed_output(managed: _ManagedCommand, n: int) -> tuple[list[str], list[str]]:
    """(stdout_tail, stderr_tail): up to the last *n* lines of each stream."""
    with managed.output_lock:
        return (
            _tail_lines_from_file(managed.stdout_file, n),
            _tail_lines_from_file(managed.stderr_file, n),
        )


_STATUS_TAIL_LINES = 10


def peek_managed_command(session_id: str, *, tail_lines: int = _STATUS_TAIL_LINES) -> dict[str, Any]:
    """Non-blocking status snapshot: state, pid, timing, and a `tail`-style
    look at output collected so far.

    Unlike `poll_managed_command`, this never blocks on the command finishing,
    never reaps the session, and never closes its spool files -- a later
    `poll`/`cancel` on the same session_id still behaves normally.
    """
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
        if managed is None:
            raise KeyError(f"unknown shell session: {session_id}")
        running = managed.proc.poll() is None
        state = managed.state
    # The watcher records "completed" immediately after wait() returns, but a
    # fast command can exit between register_completion() and that state write.
    # Never expose that finished process as still running; poll() will perform
    # the authoritative terminal transition and reader drain when it reaps.
    status = "running" if running else ("completed" if state == "running" else state)
    elapsed_ms = int((time.perf_counter() - managed.started) * 1000)
    stdout_tail, stderr_tail = _tail_managed_output(managed, tail_lines)
    payload: dict[str, Any] = {
        "status": status,
        "session_id": session_id,
        "pid": managed.proc.pid,
        "duration_ms": elapsed_ms,
        "stdout": strip_ansi("\n".join(stdout_tail)),
        "stderr": strip_ansi("\n".join(stderr_tail)),
        "tail_lines": tail_lines,
        "explicit_background": managed.explicit_background,
    }
    if managed.stdout_path:
        payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        payload["log_file_stderr"] = managed.stderr_path
    if any(r is not None and r.lossy for r in (managed.stdout_reader, managed.stderr_reader)):
        payload["output_lossy"] = True
    if running:
        remaining_ms = max(0, managed.timeout * 1000 - elapsed_ms)
        payload["timeout_remaining_ms"] = remaining_ms
        # Distinct signal from a plain mid-flight peek: this command has
        # already burned through its requested soft-timeout budget -- once
        # action="update" has installed an explicit deadline (see
        # _ManagedCommand.deadline_explicit), this is also exactly how long
        # until the real kill, not just a soft nudge.
        payload["over_budget"] = remaining_ms <= 0
    else:
        payload["exit_code"] = managed.proc.returncode
    return payload


def poll_managed_command(session_id: str, *, cancel: bool = False) -> dict[str, Any]:
    """Poll or cancel a managed command."""
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
        if managed is None:
            raise KeyError(f"unknown shell session: {session_id}")
        if cancel and managed.state == "running":
            managed.state = "cancelled"

    if cancel and managed.proc.poll() is None:
        _terminate_process_group(managed.proc)

    if managed.proc.poll() is None:
        elapsed_ms = int((time.perf_counter() - managed.started) * 1000)
        timeout_remaining_ms = max(0, managed.timeout * 1000 - elapsed_ms)
        running_payload = {
            "status": "running",
            "session_id": session_id,
            "pid": managed.proc.pid,
            "duration_ms": elapsed_ms,
            "timeout_remaining_ms": timeout_remaining_ms,
            "over_budget": timeout_remaining_ms <= 0,
            "explicit_background": managed.explicit_background,
        }
        if managed.stdout_path:
            running_payload["log_file"] = managed.stdout_path
        if managed.stderr_path:
            running_payload["log_file_stderr"] = managed.stderr_path
        return running_payload

    # Join the spool drains before reading -- the process is done, so the pipes
    # normally EOF and the threads exit promptly, leaving every surviving byte
    # on disk. Bounded: a detached descendant that still holds the pipe open
    # (e.g. a backgrounded server a task explicitly asked to be left running)
    # would otherwise wedge this join forever even though the command we
    # actually ran has already exited -- ship whatever's on disk so far
    # instead. Join outside the lock: a drain takes the lock to flag
    # truncation. One shared deadline across every reader (not one grace
    # window each) -- see _join_readers_within.
    reader_wedged = _finish_managed_readers(managed, _READER_JOIN_GRACE_S)

    with _MANAGED_COMMANDS_LOCK:
        if managed.reaped:
            # The watcher already reaped this finished session; its temp files are
            # closed. Report completion without re-reading or double-closing.
            raise KeyError(f"unknown shell session: {session_id}")
        if managed.state == "running":
            managed.state = "completed"
        managed.reaped = True
        _MANAGED_COMMANDS.pop(session_id, None)
        # A reader thread may still be alive here (the bounded join above timed
        # out) -- output_lock serializes our read against any in-flight write
        # so we never see a torn buffer.
        with managed.output_lock:
            managed.stdout_file.flush()
            managed.stderr_file.flush()
            managed.stdout_file.seek(0)
            managed.stderr_file.seek(0)
            raw_stdout, stdout_capped = _read_capped(managed.stdout_file)
            raw_stderr, stderr_capped = _read_capped(managed.stderr_file)
            managed.stdout_file.close()
            managed.stderr_file.close()
    for stream_reader in (managed.stdout_reader, managed.stderr_reader):
        if stream_reader is not None:
            stream_reader.finalize()
    output_byte_capped = stdout_capped or stderr_capped or managed.spool_truncated
    if stdout_capped:
        raw_stdout += OUTPUT_CAP_NOTICE.format(cap=MAX_OUTPUT_BYTES)
    if stderr_capped:
        raw_stderr += OUTPUT_CAP_NOTICE.format(cap=MAX_OUTPUT_BYTES)

    if managed.state == "timed_out":
        exit_code = -1
        if managed.interactive and not managed.deadline_explicit:
            raw_stderr = f"Interactive session idle-expired after {int(managed.idle_ttl)}s without input"
        else:
            raw_stderr = f"Command timed out after {int(_effective_deadline_s(managed))}s"
    elif managed.state == "cancelled":
        exit_code = -1
        raw_stderr = "Command cancelled"
    else:
        exit_code = managed.proc.returncode
    if reader_wedged:
        wedged_note = (
            "note: output ends where this command exited; a child may still be " "running and holding the stream open."
        )
        raw_stderr = f"{raw_stderr}\n{wedged_note}" if raw_stderr else wedged_note
    result = _compact_result(
        command=managed.command,
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        exit_code=exit_code,
        duration_ms=int((time.perf_counter() - managed.started) * 1000),
        max_lines=managed.max_lines,
        max_chars=managed.max_chars,
    )
    if managed.injected_note:
        result.stdout = f"{managed.injected_note}\n{result.stdout}" if result.stdout else managed.injected_note
    out_lossy = bool(managed.stdout_reader is not None and managed.stdout_reader.lossy)
    err_lossy = bool(managed.stderr_reader is not None and managed.stderr_reader.lossy)
    if out_lossy:
        notice = _lossy_notice("stdout", managed.stdout_reader.raw_path)
        result.stdout = f"{notice}\n{result.stdout}" if result.stdout else notice
    if err_lossy:
        notice = _lossy_notice("stderr", managed.stderr_reader.raw_path)
        result.stderr = f"{notice}\n{result.stderr}" if result.stderr else notice
    payload = {
        "status": managed.state,
        "session_id": session_id,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated or output_byte_capped,
        "lines_omitted": result.lines_omitted,
        "chars_omitted": result.chars_omitted,
        "spill_hint": result.spill_hint,
        "explicit_background": managed.explicit_background,
    }
    if managed.stdout_path:
        payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        payload["log_file_stderr"] = managed.stderr_path
    if out_lossy or err_lossy:
        payload["output_lossy"] = True
        if out_lossy and managed.stdout_reader.raw_path:
            payload["raw_log_file"] = managed.stdout_reader.raw_path
        if err_lossy and managed.stderr_reader.raw_path:
            payload["raw_log_file_stderr"] = managed.stderr_reader.raw_path
    return payload


def update_managed_command(session_id: str, timeout: float) -> dict[str, Any]:
    """Install (or move) a running managed command's *enforced* kill deadline
    (bash action="update").

    Before any update, `timeout` is only the soft response budget and the
    fixed one-hour safety cap controls process lifetime. Calling update is a
    deliberate act: from here on `timeout` is the exact, enforced
    deadline for this session, seconds since it started -- not "N more
    seconds from now" -- so a caller can read `timeout_remaining_ms` off a
    prior peek/poll and reason about the new absolute budget directly.
    Clamped to `_MAX_EXPLICIT_TIMEOUT_S` regardless of how many times a
    session gets extended: that ceiling is the real backstop against a task
    granting a forgotten background job unbounded life one update at a time.
    """
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
        if managed is None:
            raise KeyError(f"unknown shell session: {session_id}")
        if managed.state != "running" or managed.proc.poll() is not None:
            return {"status": managed.state, "session_id": session_id, "updated": False}
        managed.timeout = min(float(timeout), _MAX_EXPLICIT_TIMEOUT_S)
        managed.deadline_explicit = True
        applied = managed.timeout
        elapsed_ms = int((time.perf_counter() - managed.started) * 1000)
    return {
        "status": "running",
        "session_id": session_id,
        "updated": True,
        "timeout": applied,
        "timeout_remaining_ms": max(0, int(applied * 1000) - elapsed_ms),
    }


def _read_stream_delta(handle: Any, offset: int) -> tuple[str, int, bool]:
    """Read everything written to *handle* past *offset*, restoring the
    writer's append position. Returns (text, new_offset, capped).

    Offsets are opaque text-mode tell() cookies (0 or a prior tell() value),
    never arithmetic -- same constraint as _tail_lines_from_file. Caller must
    hold the session's output_lock so a concurrent spool write can't land
    between the seeks.
    """
    handle.flush()
    pos = handle.tell()
    if pos == offset:
        return "", pos, False
    handle.seek(offset)
    text, capped = _read_capped(handle)
    handle.seek(pos)
    return text, pos, capped


def send_managed_input(session_id: str, text: str, *, wait: float = 30.0) -> dict[str, Any]:
    """Feed *text* to an interactive session's stdin and return the output
    delta it produced (bash action="send").

    Framing is quiescence-based: after writing, wait until neither stream has
    grown for `_SEND_QUIESCENCE_S` (or *wait* runs out, or the process exits),
    then return only the bytes produced since the previous send -- output that
    arrived *between* sends is included, nothing is dropped. A child still
    computing past *wait* returns whatever arrived so far; a follow-up *empty*
    send blocks until more output actually arrives (growth-gated, no
    zero-growth quiescence exit) and drains it.

    Every send -- including an empty drain -- resets the session's idle-TTL
    clock (see _effective_deadline_s).
    """
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
        if managed is None:
            raise KeyError(f"unknown shell session: {session_id}")
        if not managed.interactive:
            raise ValueError(f"session {session_id} is not interactive; start it with interactive=true")
        alive = managed.state == "running" and managed.proc.poll() is None

    # The child usually isn't a shell, but the input often is shell-shaped (an
    # interactive bash, a REPL shelling out); the same policy gate as a
    # top-level command costs nothing and closes the obvious escape hatch.
    # Only "block" is honored -- rewrites target one-shot shell commands.
    if text.strip():
        policy = classify_command(text)
        if policy.action == "block":
            return {
                "status": "running" if alive else managed.state,
                "session_id": session_id,
                "blocked": True,
                "blocked_reason": policy.reason,
                "stderr": policy.reason,
                "exit_code": -1,
                "interactive": True,
            }

    sent = False
    if alive and text:
        stdin = managed.proc.stdin
        if stdin is None:
            alive = False
        else:
            try:
                data = text if text.endswith("\n") else text + "\n"
                stdin.write(data.encode("utf-8"))  # binary pipe: see start_managed_command's Popen
                stdin.flush()
                sent = True
            except (OSError, ValueError):
                alive = False  # pipe closed under us: the child just died
    with _MANAGED_COMMANDS_LOCK:
        # Reset the idle clock on every send -- an empty drain is activity too.
        managed.last_input = time.perf_counter()

    def _sizes() -> tuple[Any, Any]:
        with managed.output_lock:
            return managed.stdout_file.tell(), managed.stderr_file.tell()

    send_started = time.perf_counter()
    deadline = send_started + max(0.0, float(wait))
    baseline = _sizes()
    prev = baseline
    last_growth = send_started
    while managed.proc.poll() is None:
        now = time.perf_counter()
        if now >= deadline:
            break
        # An empty send is a pure drain: zero-growth quiescence just means
        # "nothing yet", so keep waiting for output until the budget runs out.
        if (sent or prev != baseline) and now - last_growth >= _SEND_QUIESCENCE_S:
            break
        time.sleep(min(_SEND_POLL_SLICE_S, deadline - now))
        cur = _sizes()
        if cur != prev:
            prev = cur
            last_growth = time.perf_counter()

    with managed.output_lock:
        stdout_delta, managed.stdout_read_offset, out_capped = _read_stream_delta(
            managed.stdout_file, managed.stdout_read_offset
        )
        stderr_delta, managed.stderr_read_offset, err_capped = _read_stream_delta(
            managed.stderr_file, managed.stderr_read_offset
        )

    budget = managed.max_chars if managed.max_chars is not None else output_budget(managed.command)

    def _compact_delta(delta: str) -> tuple[str, int, int, bool]:
        cleaned = strip_ansi(delta)
        deduped, dedup_chars = dedupe_repeated_lines(cleaned)
        profiled = compact_profiled_output(
            managed.command,
            deduped,
            budget=budget,
            exit_code=managed.proc.returncode,
        )
        compacted = compact_bash_stream(profiled.text, budget=budget)
        body = compacted.text
        omitted = profiled.lines_omitted + compacted.lines_omitted
        chars = dedup_chars + profiled.chars_saved + compacted.chars_saved + max(0, len(delta) - len(cleaned))
        lossy = profiled.lossy or compacted.lossy
        lines = body.splitlines()
        if len(lines) > managed.max_lines:
            omitted += len(lines) - managed.max_lines
            trimmed = "\n".join(lines[-managed.max_lines :])
            chars += max(0, len(body) - len(trimmed))
            body = trimmed
            lossy = True
        capped = cap_chars(body, budget)
        if capped != body:
            chars += len(body) - len(capped)
            body = capped
            lossy = True
        return body.rstrip("\n"), omitted, chars, lossy

    stdout_text, out_omitted, out_chars, out_lossy = _compact_delta(stdout_delta)
    stderr_text, err_omitted, err_chars, err_lossy = _compact_delta(stderr_delta)

    running = managed.proc.poll() is None
    with _MANAGED_COMMANDS_LOCK:
        state = managed.state
        idle_deadline = managed.last_input + managed.idle_ttl
    truncated = out_capped or err_capped or out_lossy or err_lossy
    payload: dict[str, Any] = {
        "status": "running" if running else ("completed" if state == "running" else state),
        "session_id": session_id,
        "pid": managed.proc.pid,
        "stdout": redact_tool_output(stdout_text),
        "stderr": redact_tool_output(stderr_text),
        "duration_ms": int((time.perf_counter() - send_started) * 1000),
        "interactive": True,
        "sent": sent,
        "truncated": truncated,
        "lines_omitted": out_omitted + err_omitted,
        "chars_omitted": out_chars + err_chars,
    }
    if running:
        payload["idle_ttl_remaining_ms"] = max(0, int((idle_deadline - time.perf_counter()) * 1000))
    else:
        payload["exit_code"] = managed.proc.returncode
    if managed.stdout_path:
        payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        payload["log_file_stderr"] = managed.stderr_path
    if truncated:
        logs = ", ".join(path for path in (managed.stdout_path, managed.stderr_path) if path)
        if logs:
            payload["spill_hint"] = f"[lc: interactive output compacted; full session logs: {logs}]"
    return payload


def register_completion(session_id: str, callback: Callable[[], None]) -> bool:
    """Arm a completion callback for a running managed command (Phase 2).

    Returns ``True`` and appends the callback (the watcher fires it once the
    process finishes) only if the session is known and still running. Returns
    ``False`` if the session is unknown or already finished/reaped -- the caller
    must then fire its own continuation immediately.
    """
    with _MANAGED_COMMANDS_LOCK:
        managed = _MANAGED_COMMANDS.get(session_id)
        if managed is None:
            return False
        if managed.state != "running" or managed.proc.poll() is not None:
            return False
        managed.on_complete.append(callback)
        return True


def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: int = 30,
    max_lines: int = DEFAULT_MAX_LINES,
) -> RunResult:
    """Execute *command* in bash, return token-compact structured output.

    Optimizations vs. raw subprocess:
    - ANSI escape codes stripped (progress bars, colors → garbage tokens).
    - stdout truncated head+tail: first 25% for context, last 75% for results/errors.
    - stderr head+tail compacted (100/100 lines); omitted lines recoverable via spill.
    - Structured return: LLM checks exit_code first, reads output only if needed.
    """
    policy = classify_command(command, cwd=cwd)
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

    # Fast-path: execute head/tail/wc directly in Python — no fork, no exec,
    # no gate check (we're not spawning a shell).  Latency drops from ~40 ms
    # to <1 ms for these common file-inspection commands.
    if (
        policy.action == "rewrite"
        and policy.rewrite_target in {"cat", "head", "tail", "wc"}
        and policy.rewrite_payload is not None
    ):
        _t0 = time.perf_counter()
        _stdout, _stderr, _exit = execute_inline_op(policy.rewrite_target, policy.rewrite_payload, cwd)
        _dur = int((time.perf_counter() - _t0) * 1000)
        _result = _compact_result(
            command=command,
            raw_stdout=_stdout,
            raw_stderr=_stderr,
            exit_code=_exit,
            duration_ms=_dur,
            max_lines=max_lines,
        )
        _result.policy_category = policy.category
        _result.policy_action = policy.action
        _result.policy_reason = policy.reason
        _result.rewrite_target = policy.rewrite_target
        _result.rewrite_payload = policy.rewrite_payload
        return _result

    exec_source = command
    if policy.action == "rewrite" and policy.rewrite_target == "external_compactor" and policy.rewrite_payload:
        exec_source = str(policy.rewrite_payload.get("command") or command)
    exec_command, injected_note = inject_stable_flags(exec_source)
    started = time.perf_counter()
    proc: subprocess.Popen[bytes] | None = None
    output_byte_capped = False
    stdout_reader: _LossyStreamReader | None = None
    stderr_reader: _LossyStreamReader | None = None
    try:
        proc = subprocess.Popen(
            ["bash", "-c", exec_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Binary pipes; _LossyStreamReader decodes utf-8 with errors="replace"
            # (a strict decode would raise inside _drain's suppress() on the first
            # invalid byte, silently emptying the captured stream -- measured:
            # doom's non-UTF8 boot log came back as "" on exit 0) while exactly
            # flagging substitution and teeing raw bytes for recovery.
            cwd=cwd,
            start_new_session=True,
        )
        raw_sid = _new_session_id()
        out_raw, out_raw_path = _open_raw_stream_file(raw_sid, "stdout")
        err_raw, err_raw_path = _open_raw_stream_file(raw_sid, "stderr")
        stdout_reader = _LossyStreamReader(
            proc.stdout, raw_file=out_raw, raw_path=out_raw_path, raw_cap=MAX_OUTPUT_BYTES
        )
        stderr_reader = _LossyStreamReader(
            proc.stderr, raw_file=err_raw, raw_path=err_raw_path, raw_cap=MAX_OUTPUT_BYTES
        )
        # Drain both pipes concurrently into bounded in-memory buffers. A plain
        # `communicate()` slurps the child's *entire* output into RAM before any
        # cap runs, so a runaway producer (`yes`, `cat /dev/zero`) OOMs the host.
        # `_pump_capped` stops accumulating at `MAX_OUTPUT_BYTES` per stream but
        # keeps reading to EOF, and running one thread per stream avoids the
        # pipe-buffer deadlock when both stdout and stderr are large.
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        capped = {"stdout": False, "stderr": False}

        def _drain(stream: Any, buf: list[str], key: str) -> None:
            with contextlib.suppress(Exception):
                capped[key] = _pump_capped(stream, buf.append, MAX_OUTPUT_BYTES)

        readers = [
            threading.Thread(target=_drain, args=(stdout_reader, stdout_buf, "stdout"), daemon=True),
            threading.Thread(target=_drain, args=(stderr_reader, stderr_buf, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()

        def _finish_run_readers() -> None:
            # Bounded join, same hazard the managed path guards against in
            # _finish_managed_readers: a self-daemonizing child can fork a
            # grandchild that inherits and holds this pipe open, leaving _drain
            # blocked in readline() long after `proc` (the wrapping bash) has
            # exited -- an unbounded join here hangs the whole synchronous call
            # forever. Join with a grace; on wedge neutralize the raw fds (dup2
            # /dev/null, never stream.close -- that serializes with the reader's
            # in-flight readline on the BufferedReader lock and would deadlock
            # this thread) and ship whatever was captured before the command's
            # own process exited.
            if proc is None:
                return
            if _join_readers_within(readers, _READER_JOIN_GRACE_S):
                _neutralize_pipe_fds(proc.stdout, proc.stderr)
                _join_readers_within(readers, 0.2)

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the group first so the child's pipes close (the common case);
            # a grandchild that setsid'd out of the group can still hold the pipe
            # open, so bound the join the same way the success path does.
            _terminate_process_group(proc)
            _finish_run_readers()
            raise
        _finish_run_readers()
        exit_code = proc.returncode
        raw_stdout = strip_ansi("".join(stdout_buf))
        raw_stderr = strip_ansi("".join(stderr_buf))
        stdout_capped = capped["stdout"]
        stderr_capped = capped["stderr"]
        output_byte_capped = stdout_capped or stderr_capped
        if stdout_capped:
            raw_stdout += OUTPUT_CAP_NOTICE.format(cap=MAX_OUTPUT_BYTES)
        if stderr_capped:
            raw_stderr += OUTPUT_CAP_NOTICE.format(cap=MAX_OUTPUT_BYTES)
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_stdout = ""
        raw_stderr = f"Command timed out after {timeout}s"
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        exit_code = -1
        raw_stdout = ""
        raw_stderr = str(exc)

    for sync_reader in (stdout_reader, stderr_reader):
        if sync_reader is not None:
            sync_reader.finalize()
    duration_ms = int((time.perf_counter() - started) * 1000)

    if output_delta.observe(command, cwd=cwd, stdout=raw_stdout, stderr=raw_stderr, exit_code=exit_code):
        # Run-and-dedup (execution NEVER skipped): the command really ran and
        # produced byte-identical output to its previous run this session, so
        # every byte is already in the model's context -- ship a marker. The
        # full text is still spilled for recovery, because re-running the
        # command would just yield the marker again.
        total_lines = len(raw_stdout.splitlines()) + len(raw_stderr.splitlines())
        total_chars = len(raw_stdout) + len(raw_stderr)
        marker = unchanged_marker(raw_stdout, raw_stderr)
        full_text = raw_stdout
        if raw_stderr.strip():
            full_text = f"{full_text}\n\n--- stderr ---\n{raw_stderr}" if full_text else raw_stderr
        result = RunResult(
            stdout=marker,
            stderr="",
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated=True,
            lines_omitted=total_lines,
            chars_omitted=max(0, total_chars - len(marker)),
            command=command,
            spill_hint=_spill_hint(full_text, len(marker)),
        )
    else:
        result = _compact_result(
            command=command,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            max_lines=max_lines,
        )
        if injected_note:
            result.stdout = f"{injected_note}\n{result.stdout}" if result.stdout else injected_note
    if stdout_reader is not None and stdout_reader.lossy:
        notice = _lossy_notice("stdout", stdout_reader.raw_path)
        result.stdout = f"{notice}\n{result.stdout}" if result.stdout else notice
    if stderr_reader is not None and stderr_reader.lossy:
        notice = _lossy_notice("stderr", stderr_reader.raw_path)
        result.stderr = f"{notice}\n{result.stderr}" if result.stderr else notice
    result.truncated = result.truncated or output_byte_capped
    result.policy_category = policy.category
    result.policy_action = policy.action
    result.policy_reason = policy.reason
    result.rewrite_target = policy.rewrite_target
    result.rewrite_payload = policy.rewrite_payload
    return result


__all__ = [
    "RunResult",
    "classify_command",
    "poll_managed_command",
    "register_completion",
    "run_command",
    "start_managed_command",
]
