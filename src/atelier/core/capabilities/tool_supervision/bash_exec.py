"""Shell command execution with token-aware output compaction."""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atelier.core.capabilities.tool_supervision import output_delta
from atelier.core.capabilities.tool_supervision.external_compactors import (
    compactor_for_command,
    external_compactors_enabled,
    resolve_compactor,
)
from atelier.core.foundation.redaction import redact_tool_output

_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: colors, cursor movement, erase
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC (title, hyperlinks), BEL- or ST-terminated
    r"|\x1b[@-Z\\-_]"  # bare two-byte escapes (incl. unterminated openers)
)
_SEARCH_REGEX_METACHARS = re.compile(r"[][{}()|^$*+?\\]")
# Shell file-write patterns: cat > file or cat >> file (write redirect)
_SHELL_FILE_WRITE_RE = re.compile(r"\bcat\s+>>?", re.IGNORECASE)
# Inline interpreter writes: python -c / heredoc scripts that write workspace
# files (open(...,'w'), .write_text(...)) — same edit-tool bypass as cat >.
_INTERP_WRITE_RE = re.compile(
    r"""\bpython[0-9.]*\b.*(?:
        open\([^)]*,\s*['"][wax]b?\+?['"]   # open(path, 'w'/'a'/'x')
        | \.write_text\(
        | \.write_bytes\(
    )""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
# Literal write-target extraction for the allowed-roots escape hatch (below).
# A write is permitted only when every target is an absolute literal path inside
# an allowed root; any opaque target (variable, f-string, ``.write_text``
# receiver) yields ``None`` so the guard blocks what it cannot verify.
_OPEN_WRITE_TARGET_RE = re.compile(
    r"""open\(\s*(?P<arg>[^,]+?)\s*,\s*['"][wax]b?\+?['"]""",
    re.IGNORECASE | re.VERBOSE,
)
_CAT_REDIRECT_TARGET_RE = re.compile(r"""\bcat\s+>>?\s*(?P<tgt>'[^']*'|"[^"]*"|[^\s'"|;&>]+)""")
_WRITE_METHOD_RE = re.compile(r"\.write_(?:text|bytes)\(", re.IGNORECASE)
_QUOTED_LITERAL_RE = re.compile(r"""^(?P<q>['"])(?P<v>.*)(?P=q)$""", re.DOTALL)
# A shell short-option cluster requesting no-exec parse mode (``-n``, ``-nx``).
# Among bash/sh/zsh/fish single-char invocation options only ``-n`` contains an
# 'n', so a single-dash cluster containing 'n' implies syntax-check-only.
_SHELL_NOEXEC_SHORT_RE = re.compile(r"^-[a-zA-Z]*n[a-zA-Z]*$")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


# Hard ceiling on how many bytes of stdout/stderr are materialized into memory
# from a single command. A runaway child (`cat /dev/zero`, `yes`, `gzip -dc`,
# a chatty build) would otherwise fill the temp file to disk and OOM on a full
# `.read()`. `max_lines` truncation only runs *after* materialization, so the
# cap must happen at read time. Configurable via env, with a 64KiB floor so it
# can never be set so low that ordinary output is mangled.
_MAX_OUTPUT_BYTES = max(
    64 * 1024,
    int(os.environ.get("ATELIER_SHELL_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024))),
)

# On-disk ceiling for a managed command's temp spool. `subprocess.Popen` writes
# the child's output straight to the temp file's fd, so the read-time
# `_MAX_OUTPUT_BYTES` cap cannot bound it -- `cat /dev/zero` would fill the disk
# before any poll runs. The spool pump (`_pump_capped`) stops appending once
# this ceiling is reached. Defaults to the output cap (read side then catches
# every truncated spool); a larger value retains more for later inspection.
_MAX_SPOOL_BYTES = max(
    _MAX_OUTPUT_BYTES,
    int(os.environ.get("ATELIER_SHELL_MAX_SPOOL_BYTES", str(_MAX_OUTPUT_BYTES))),
)

# Read granularity for `_pump_capped`; large enough to keep the drain loop cheap
# without buffering an unbounded amount per iteration, and used as the size hint
# for `readline()` so a single pathological line (no trailing newline) still has
# a bounded worst-case read instead of buffering unboundedly.
_PUMP_CHUNK_CHARS = 64 * 1024


def _cap_text(text: str) -> tuple[str, bool]:
    """Bound *text* to the output-byte ceiling, returning (text, truncated).

    Truncation is measured in UTF-8 bytes to mirror on-disk size; the returned
    string is cut on a character boundary at or just under the cap.
    """
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text, False
    capped = encoded[:_MAX_OUTPUT_BYTES].decode("utf-8", "ignore")
    return capped, True


def _read_capped(handle: Any) -> tuple[str, bool]:
    """Read at most the output-byte ceiling from a seeked temp-file *handle*.

    Reads one character past the cap to detect a larger file without slurping
    it whole, so memory stays bounded regardless of on-disk size. Returns
    (text, truncated).
    """
    chunk = handle.read(_MAX_OUTPUT_BYTES + 1)
    if len(chunk) <= _MAX_OUTPUT_BYTES:
        return chunk, False
    return chunk[:_MAX_OUTPUT_BYTES], True


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
    accounting mirrors `_cap_text`, cutting a straddling chunk on a character
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
        prefix = encoded[: cap - written].decode("utf-8", "ignore")
        if prefix:
            write(prefix)
        written = cap
        truncated = True
    return truncated


_OUTPUT_CAP_NOTICE = (
    "\n... (output exceeded {cap} bytes and was truncated by Atelier; narrow the command or redirect to a file) ..."
)


def _head_tail_lines(lines: list[str], head: int, tail: int) -> tuple[str, int, int]:
    if len(lines) <= head + tail:
        return "\n".join(lines), 0, 0
    omitted_lines = lines[head : len(lines) - tail]
    omitted = len(omitted_lines)
    omitted_chars = sum(len(line) for line in omitted_lines)
    parts = [*lines[:head], f"... ({omitted} lines omitted) ...", *lines[-tail:]]
    return "\n".join(parts), omitted, omitted_chars


def _bash_spill_enabled() -> bool:
    """Mirrors the MCP dispatch layer's T7 kill switch (``ATELIER_TOOL_OUTPUT_SPILL``)."""
    return os.environ.get("ATELIER_TOOL_OUTPUT_SPILL", "1").strip().lower() in {"1", "true", "yes", "on"}


def _spill_hint(full_text: str, kept_chars: int) -> str:
    """Persist the full pre-compaction *full_text* and return the canonical footer.

    Head+tail compaction below (``_head_tail_lines``) keeps a "(N lines
    omitted)" marker but discards the omitted lines for good -- there was no way
    to recover them without re-running the (often expensive, non-idempotent)
    command. Mirrors ``web_fetch._truncate_with_spill``: persist the untouched
    original to the shared T7 spill store and name the path in the canonical
    ``[atelier: ...]`` footer (see ``tool_output_spill.spill_notice``).
    *kept_chars* is the char count of the body actually shown (post-compaction),
    for the footer's ORIG->KEPT accounting. Returns "" when spill is disabled,
    *full_text* is empty, or the write fails, so the caller falls back to the
    bare marker.
    """
    if not full_text or not _bash_spill_enabled():
        return ""
    from atelier.core.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(full_text, tool_name="bash", kind="original")
    if record is None:
        return ""
    return tool_output_spill.spill_notice(
        verb="shrunk",
        original_chars=len(full_text),
        kept_chars=kept_chars,
        path=record.path,
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


@dataclass(frozen=True)
class CommandPolicyDecision:
    category: str
    action: str
    reason: str = ""
    rewrite_target: str | None = None
    rewrite_payload: dict[str, Any] | None = None


@dataclass
class _ManagedCommand:
    command: str
    proc: subprocess.Popen[str]
    stdout_file: Any
    stderr_file: Any
    started: float
    timeout: float
    max_lines: int
    max_chars: int | None = None
    # Set by update_managed_command (bash action="update"): once True,
    # `timeout` is the exact, enforced kill deadline instead of only ever
    # being a soft response budget floored at _MANAGED_COMMAND_HARD_CAP_S --
    # see _effective_deadline_s.
    deadline_explicit: bool = False
    state: str = "running"
    # First-line provenance when _inject_stable_flags modified the executed
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
_MAX_EXPLICIT_TIMEOUT_S = 86400.0
# stdout_file/stderr_file (the managed command's only spool -- no separate
# tee/mirror) live here when writable, instead of an anonymous tempfile with
# no filesystem path. One write per stream, on a real path a user can
# `tail -f`. Deliberately NOT deleted at reap -- a command that just finished
# is exactly when someone is most likely to want to look back at its log; it
# survives until this process's own cleanup (_cleanup_all_log_files, atexit)
# or the staleness sweep below.
_BASH_LOG_DIR = Path(tempfile.gettempdir()) / "atelier-bash"
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
        _sweep_stale_log_files(_BASH_LOG_DIR)
        path = _BASH_LOG_DIR / f"{session_id}.{stream_name}.txt"
        handle = open(path, "w+", encoding="utf-8")  # lives on managed, closed at reap
    except OSError:
        return tempfile.TemporaryFile(mode="w+", encoding="utf-8"), ""
    with _LOG_PATHS_LOCK:
        _ALL_LOG_PATHS.add(str(path))
    return handle, str(path)


def _rewrite_cat(tokens: list[str]) -> CommandPolicyDecision:
    if len(tokens) != 2:
        return CommandPolicyDecision(category="file-read", action="allow")
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        reason="Atelier read for file content access",
        rewrite_target="read",
        rewrite_payload={"file_path": tokens[1]},
    )


def _parse_head_tail_n(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Parse a ``-n N`` / ``--lines=N`` / ``-N`` count from *tokens* at position *i*.

    Returns ``(n, new_i)`` where *n* is ``None`` when the token is unrecognised
    (caller should fall back to subprocess) and *new_i* is the next index to
    process.  Negative N and ``+N`` (from-line) forms return ``None``.
    """
    tok = tokens[i]
    if tok in {"-n", "--lines"}:
        if i + 1 >= len(tokens):
            return None, i + 1
        val = tokens[i + 1]
        if val.startswith("+") or not val.lstrip("-").isdigit():
            return None, i + 2
        n = int(val)
        return (None if n < 0 else n), i + 2
    if tok.startswith("--lines="):
        val = tok.split("=", 1)[1]
        if val.startswith("+") or not val.lstrip("-").isdigit():
            return None, i + 1
        n = int(val)
        return (None if n < 0 else n), i + 1
    if tok.startswith("-n") and tok[2:].isdigit():
        # Bundled form: -n80
        return int(tok[2:]), i + 1
    if len(tok) >= 2 and tok[1:].isdigit():
        # GNU legacy short form: head -80 file
        return int(tok[1:]), i + 1
    return None, i + 1  # unrecognised


def _rewrite_head(tokens: list[str]) -> CommandPolicyDecision:
    """Rewrite ``head [-n N] file`` to a Python inline op (no subprocess)."""
    n = 10
    files: list[str] = []
    i = 1
    seen_double_dash = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" and not seen_double_dash:
            seen_double_dash = True
            i += 1
            continue
        if tok.startswith("-") and not seen_double_dash:
            if tok in {"-q", "--quiet", "--silent", "-v", "--verbose", "-z", "--zero-terminated", "-c", "--bytes"}:
                return CommandPolicyDecision(category="file-read", action="allow")
            parsed_n, i = _parse_head_tail_n(tokens, i)
            if parsed_n is None:
                return CommandPolicyDecision(category="file-read", action="allow")
            n = parsed_n
            continue
        files.append(tok)
        i += 1
    if len(files) != 1:
        return CommandPolicyDecision(category="file-read", action="allow")
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        rewrite_target="head",
        rewrite_payload={"file": files[0], "n": n},
    )


def _rewrite_tail(tokens: list[str]) -> CommandPolicyDecision:
    """Rewrite ``tail [-n N] file`` to a Python inline op (no subprocess)."""
    n = 10
    files: list[str] = []
    i = 1
    seen_double_dash = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" and not seen_double_dash:
            seen_double_dash = True
            i += 1
            continue
        if tok.startswith("-") and not seen_double_dash:
            # -f/--follow, -s, --pid, --retry, --sleep-interval and byte-mode
            # all require real tail behaviour.
            if tok in {
                "-f",
                "-F",
                "--follow",
                "--retry",
                "-q",
                "--quiet",
                "--silent",
                "-v",
                "--verbose",
                "-z",
                "--zero-terminated",
                "-c",
                "--bytes",
                "-s",
                "--sleep-interval",
                "--pid",
            }:
                return CommandPolicyDecision(category="file-read", action="allow")
            parsed_n, i = _parse_head_tail_n(tokens, i)
            if parsed_n is None:
                return CommandPolicyDecision(category="file-read", action="allow")
            n = parsed_n
            continue
        files.append(tok)
        i += 1
    if len(files) != 1:
        return CommandPolicyDecision(category="file-read", action="allow")
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        rewrite_target="tail",
        rewrite_payload={"file": files[0], "n": n},
    )


def _rewrite_wc(tokens: list[str]) -> CommandPolicyDecision:
    """Rewrite ``wc [-l|-c|-w] file`` to a Python inline op (no subprocess)."""
    count_lines = False
    count_bytes = False
    count_words = False
    files: list[str] = []
    i = 1
    seen_double_dash = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" and not seen_double_dash:
            seen_double_dash = True
            i += 1
            continue
        if tok.startswith("-") and not seen_double_dash:
            if tok in {"-l", "--lines"}:
                count_lines = True
            elif tok in {"-c", "--bytes"}:
                count_bytes = True
            elif tok in {"-w", "--words"}:
                count_words = True
            elif tok in {"-m", "--chars"}:
                # Character vs byte count differs for multibyte content;
                # fall back to subprocess for correctness.
                return CommandPolicyDecision(category="file-read", action="allow")
            elif not tok.startswith("--") and len(tok) > 1:
                # Bundled short flags: -lw, -lc, -lwc …
                for ch in tok[1:]:
                    if ch == "l":
                        count_lines = True
                    elif ch == "c":
                        count_bytes = True
                    elif ch == "w":
                        count_words = True
                    else:
                        return CommandPolicyDecision(category="file-read", action="allow")
            else:
                return CommandPolicyDecision(category="file-read", action="allow")
            i += 1
            continue
        files.append(tok)
        i += 1
    if len(files) != 1:
        # stdin or multiple files → subprocess
        return CommandPolicyDecision(category="file-read", action="allow")
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        rewrite_target="wc",
        rewrite_payload={
            "file": files[0],
            "count_lines": count_lines,
            "count_bytes": count_bytes,
            "count_words": count_words,
        },
    )


def execute_inline_op(
    rewrite_target: str,
    payload: dict[str, Any],
    cwd: str | None = None,
) -> tuple[str, str, int]:
    """Execute a fast-path file-read op in Python, returning (stdout, stderr, exit_code).

    Covers ``head``, ``tail``, and ``wc``.  No subprocess is spawned; latency
    is O(microseconds) rather than O(30-50 ms) for fork+exec of bash+head.
    Called from both ``run_command`` and the MCP adapter so they share the same
    implementation.
    """
    file_arg = str(payload.get("file") or "")
    path = Path(file_arg)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path

    if rewrite_target == "head":
        n = int(payload.get("n") or 10)
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                lines: list[str] = []
                for _ in range(n):
                    line = fh.readline()
                    if not line:
                        break
                    lines.append(line)
            return "".join(lines), "", 0
        except FileNotFoundError:
            return "", f"head: cannot open '{file_arg}' for reading: No such file or directory\n", 1
        except PermissionError:
            return "", f"head: cannot open '{file_arg}' for reading: Permission denied\n", 1
        except OSError as exc:
            return "", f"head: {file_arg}: {exc}\n", 1

    if rewrite_target == "tail":
        n = int(payload.get("n") or 10)
        _TAIL_CHUNK = 65536
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                if size == 0:
                    return "", "", 0
                buf = b""
                pos = size
                # Read backward in chunks until we have n+1 newlines
                # (+1 because the first chunk may start mid-line).
                while pos > 0:
                    chunk = min(_TAIL_CHUNK, pos)
                    pos -= chunk
                    fh.seek(pos)
                    buf = fh.read(chunk) + buf
                    if buf.count(b"\n") >= n + 1:
                        break
            text = buf.decode("utf-8", errors="replace")
            lines = text.splitlines()
            tail_lines = lines[-n:] if len(lines) >= n else lines
            return ("\n".join(tail_lines) + "\n") if tail_lines else "", "", 0
        except FileNotFoundError:
            return "", f"tail: cannot open '{file_arg}' for reading: No such file or directory\n", 1
        except PermissionError:
            return "", f"tail: cannot open '{file_arg}' for reading: Permission denied\n", 1
        except OSError as exc:
            return "", f"tail: {file_arg}: {exc}\n", 1

    if rewrite_target == "wc":
        count_lines = bool(payload.get("count_lines"))
        count_bytes = bool(payload.get("count_bytes"))
        count_words = bool(payload.get("count_words"))
        # No flags → report lines, words, and bytes (GNU wc default).
        all_counts = not (count_lines or count_bytes or count_words)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            parts: list[str] = []
            if all_counts or count_lines:
                parts.append(str(text.count("\n")))
            if all_counts or count_words:
                parts.append(str(len(text.split())))
            if all_counts or count_bytes:
                parts.append(str(len(raw)))
            parts.append(file_arg)
            return " ".join(parts) + "\n", "", 0
        except FileNotFoundError:
            return "", f"wc: {file_arg}: No such file or directory\n", 1
        except PermissionError:
            return "", f"wc: {file_arg}: Permission denied\n", 1
        except OSError as exc:
            return "", f"wc: {file_arg}: {exc}\n", 1

    raise ValueError(f"Unknown inline op: {rewrite_target!r}")


# Flags that are structural/formatting and safe to silently ignore during rewrite
# (they don't change what lines are matched or how many).
_GREP_SAFE_IGNORE_FLAGS: frozenset[str] = frozenset(
    {
        "-r",
        "-R",
        "--recursive",
        "--dereference-recursive",
        "-n",
        "--line-number",
        "-H",
        "--with-filename",
        "-h",
        "--no-filename",
        "-E",
        "--extended-regexp",
        "-P",
        "--perl-regexp",
        "-s",
        "--no-messages",
        "-a",
        "--text",
        "-I",  # rg: skip binary
        "-u",
        "-uu",
        "-uuu",  # rg: --unrestricted
        "--no-ignore",
        "--no-ignore-vcs",
        "--no-ignore-parent",
        "--hidden",
        "--follow",
        "-L",
        "--color",
        "--colour",
        "--colors",
        "--no-color",
        "--no-colour",
        "--color=never",
        "--color=always",
        "--color=auto",
        "--null",  # grep: NUL-terminate output paths (not content)
        "-p",  # rg: --no-ignore-parent short form
    }
)

# Flags that alter which lines are output or their format — we can't faithfully
# replicate these in the MCP grep tool, so fall back to real shell execution.
_GREP_FALLBACK_FLAGS: frozenset[str] = frozenset(
    {
        "-o",
        "--only-matching",
        "-v",
        "--invert-match",
        "-c",
        "--count",
        "-q",
        "--quiet",
        "--silent",
        "-x",
        "--line-regexp",
        "-w",
        "--word-regexp",
        "-F",
        "--fixed-strings",
        "-z",
        "--null-data",
        "-Z",
        "-p",  # rg --replace short (conflicts with safe ignore above, but -p is rare)
        "--replace",
    }
)


def _rewrite_search(tokens: list[str], command_name: str) -> CommandPolicyDecision:
    # Split on the first shell pipe token so we can rewrite the grep part and
    # feed its output through the remainder (e.g. ``grep -A5 pat file | head -20``).
    pipe_idx: int | None = None
    for idx, tok in enumerate(tokens):
        if tok == "|":
            pipe_idx = idx
            break
    if pipe_idx is not None:
        grep_tokens = tokens[:pipe_idx]
        # Reconstruct the pipe tail as a single shell string.
        pipe_remainder: str | None = (
            " ".join(shlex.quote(t) for t in tokens[pipe_idx + 1 :]) if pipe_idx + 1 < len(tokens) else None
        )
    else:
        grep_tokens = tokens
        pipe_remainder = None

    ignore_case = False
    file_type: str | None = None
    lines_after = 0
    lines_before = 0
    globs: list[str] = []
    list_files_only = False
    cleaned: list[str] = []
    seen_double_dash = False
    i = 1
    while i < len(grep_tokens):
        tok = grep_tokens[i]
        if tok == "--":
            seen_double_dash = True
            i += 1
            continue
        if tok.startswith("-") and not seen_double_dash:
            # Flags that alter output semantics we can't replicate → fall back so
            # the agent gets correct (not silently wrong) results.
            flag_stem = tok.split("=", 1)[0]  # strip =value suffix for lookup
            if flag_stem in _GREP_FALLBACK_FLAGS or tok in _GREP_FALLBACK_FLAGS:
                return CommandPolicyDecision(category="search", action="allow")
            # Safe structural/formatting flags — skip quietly.
            if tok in _GREP_SAFE_IGNORE_FLAGS or flag_stem in _GREP_SAFE_IGNORE_FLAGS:
                i += 1
                continue
            # --type=python or --type python or -t python
            if tok.startswith("--type="):
                file_type = tok.split("=", 1)[1]
            elif tok in {"--type", "-t"} and i + 1 < len(grep_tokens):
                i += 1
                file_type = grep_tokens[i]
            # -A N / --after-context N  (lines after match)
            elif tok in {"-A", "--after-context"} and i + 1 < len(grep_tokens):
                i += 1
                try:
                    lines_after = int(grep_tokens[i])
                except ValueError:
                    pass
            elif tok.startswith("-A") and tok[2:].isdigit():
                lines_after = int(tok[2:])
            elif tok.startswith("--after-context="):
                try:
                    lines_after = int(tok.split("=", 1)[1])
                except ValueError:
                    pass
            # -B N / --before-context N  (lines before match)
            elif tok in {"-B", "--before-context"} and i + 1 < len(grep_tokens):
                i += 1
                try:
                    lines_before = int(grep_tokens[i])
                except ValueError:
                    pass
            elif tok.startswith("-B") and tok[2:].isdigit():
                lines_before = int(tok[2:])
            elif tok.startswith("--before-context="):
                try:
                    lines_before = int(tok.split("=", 1)[1])
                except ValueError:
                    pass
            # -C N / --context N  (symmetric context)
            elif tok in {"-C", "--context"} and i + 1 < len(grep_tokens):
                i += 1
                try:
                    n = int(grep_tokens[i])
                    lines_before = lines_after = n
                except ValueError:
                    pass
            elif tok.startswith("-C") and tok[2:].isdigit():
                n = int(tok[2:])
                lines_before = lines_after = n
            elif tok.startswith("--context="):
                try:
                    n = int(tok.split("=", 1)[1])
                    lines_before = lines_after = n
                except ValueError:
                    pass
            # -l / --files-with-matches / --files-with-match (rg)
            elif tok in {"-l", "--files-with-matches", "--files-with-match"}:
                list_files_only = True
            # --include=glob (grep) or -g glob (rg)
            elif tok.startswith("--include="):
                globs.append(tok.split("=", 1)[1])
            elif tok in {"-g", "--glob"} and i + 1 < len(grep_tokens):
                i += 1
                globs.append(grep_tokens[i])
            elif tok.startswith("-g") and len(tok) > 2:
                globs.append(tok[2:])
            # -i case-insensitive (guard: not a multi-char flag like --include)
            elif "i" in tok and len(tok) <= 3:
                ignore_case = True
            # Bundled short flags (-rn, -rni, -rniA …): expand char-by-char.
            # Any fallback char → fall back; any unknown char → fall back;
            # all safe → continue.  Value-in-flag forms (-A90) are already
            # handled above and never reach this branch.
            elif not tok.startswith("--") and len(tok) > 2:
                for ch in tok[1:]:
                    single = f"-{ch}"
                    if single in _GREP_FALLBACK_FLAGS:
                        return CommandPolicyDecision(category="search", action="allow")
                    if single not in _GREP_SAFE_IGNORE_FLAGS:
                        # Check -i specially (case-insensitive embedded in bundle)
                        if ch == "i":
                            ignore_case = True
                        else:
                            return CommandPolicyDecision(category="search", action="allow")
            # Unknown flag: fall back so we don't silently produce wrong output.
            else:
                return CommandPolicyDecision(category="search", action="allow")
            i += 1
            continue
        cleaned.append(tok)
        i += 1

    if not cleaned:
        return CommandPolicyDecision(category="search", action="allow")

    pattern = cleaned[0]
    # GNU grep BRE treats \| as alternation (extension); rg uses Rust regex
    # where \| is a literal backslash+pipe.  Convert so patterns like
    # "foo\|bar" work as expected via the rg backend.
    if command_name == "grep" and r"\|" in pattern:
        pattern = pattern.replace(r"\|", "|")
    path = cleaned[1] if len(cleaned) > 1 else "."
    # Single-file targets: fall through to shell grep/rg.  The Python rewrite
    # adds value only for directory-wide searches (ranking, context, file caps).
    # For a specific file, real grep is faster, handles pipes/redirections
    # natively, and avoids any Python overhead or GIL contention.
    if path != "." and Path(path).is_file():
        return CommandPolicyDecision(category="search", action="allow")
    if (
        command_name == "rg"
        and not ignore_case
        and file_type is None
        and not globs
        and not list_files_only
        and lines_after == 0
        and lines_before == 0
        and pipe_remainder is None
        and len(cleaned) <= 2
        and not _SEARCH_REGEX_METACHARS.search(pattern)
    ):
        return CommandPolicyDecision(
            category="search",
            action="rewrite",
            reason="Atelier search for search-first grounding",
            rewrite_target="search",
            rewrite_payload={"query": pattern, "path": path},
        )
    output_mode = "file_paths_only" if list_files_only else "file_paths_with_content"
    payload: dict[str, Any] = {
        "file_path": path,
        "content_regex": pattern,
        "ignore_case": ignore_case,
        "output_mode": output_mode,
        "lines_after": lines_after,
        "lines_before": lines_before,
    }
    if file_type:
        payload["type"] = file_type
    if globs:
        payload["glob"] = globs
    if pipe_remainder:
        payload["pipe_remainder"] = pipe_remainder
    return CommandPolicyDecision(
        category="search",
        action="rewrite",
        reason=f"Atelier grep for {command_name} pattern search",
        rewrite_target="grep",
        rewrite_payload=payload,
    )


def _is_rm_family(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "rm":
        return False
    recursive = force = False
    for tok in tokens[1:]:
        if not tok.startswith("-"):
            continue
        if tok.startswith("--"):
            if tok == "--recursive":
                recursive = True
            elif tok == "--force":
                force = True
            continue
        # Short flags may be bundled (-rf) or split (-r -f).
        if "r" in tok or "R" in tok:
            recursive = True
        if "f" in tok:
            force = True
    return recursive and force


# A shell redirection operator, optionally glued to its target (``2>/dev/null``,
# ``>>out.log``, ``&>err``) or left bare (``>``, in which case the *next*
# token is its target, not an rm argument). ``rm -rf x 2>/dev/null`` is a
# common idiom and must not have ``2>/dev/null`` mistaken for a delete target.
_RM_REDIRECT_RE = re.compile(r"^(?:\d*>>?|\d*<|&>>?)(.*)$")


def _rm_target_paths(tokens: list[str]) -> list[str] | None:
    """Positional (non-flag, non-redirect) ``rm`` arguments, or ``None`` if any
    is opaque.

    A shell variable (``$X``), glob (``*``/``?``/``[``), or ``~`` expansion
    can't be resolved to a literal path without actually running the shell, so
    its presence makes the whole invocation opaque -- the safe-root check
    below must then fail closed (treat as not confined).
    """
    targets: list[str] = []
    seen_double_dash = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" and not seen_double_dash:
            seen_double_dash = True
            i += 1
            continue
        if not seen_double_dash:
            redirect = _RM_REDIRECT_RE.match(tok)
            if redirect is not None:
                # Bare operator (``>``) consumes the next token as its target;
                # a glued form (``2>/dev/null``) carries its own target.
                i += 1 if redirect.group(1) else 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
        if "$" in tok or "*" in tok or "?" in tok or "[" in tok or tok.startswith("~"):
            return None
        targets.append(tok)
        i += 1
    return targets


def _rm_confined_to_safe_roots(tokens: list[str], *, cwd: Path | None, safe_roots: list[Path]) -> bool:
    """True iff every ``rm`` target resolves strictly inside one of *safe_roots*.

    Lets an agent clean up its own scratch/temp files without the hard
    ``rm -rf`` block below, while every other path (the project, home dir,
    ``/``, or a safe root's own mount point) stays blocked exactly as before.
    A relative target needs a known *cwd* to resolve against; without one it's
    treated as unconfined. Requiring a *strict* descendant (not the root
    itself) stops ``rm -rf /tmp`` from wiping the whole scratch filesystem.
    """
    targets = _rm_target_paths(tokens)
    if not targets:
        return False
    for raw in targets:
        path = Path(raw)
        if not path.is_absolute():
            if cwd is None:
                return False
            path = cwd / path
        resolved = path.resolve()
        if not any(resolved != root and resolved.is_relative_to(root) for root in safe_roots):
            return False
    return True


def _git_subcommand_index(tokens: list[str]) -> int:
    """Index of the git subcommand, skipping leading global options.

    ``git -C <dir> reset --hard`` and ``git --git-dir=x clean -fd`` place the
    subcommand after global options, so a hardcoded ``tokens[1]`` misses it.
    """
    _takes_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        tok = tokens[i]
        # ``--git-dir=x`` carries its value inline; bare forms consume the next token.
        if tok in _takes_value and "=" not in tok:
            i += 2
        else:
            i += 1
    return i


def _is_git_reset_hard(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git":
        return False
    idx = _git_subcommand_index(tokens)
    return idx < len(tokens) and tokens[idx] == "reset" and "--hard" in tokens[idx + 1 :]


def _is_git_clean_fd(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git":
        return False
    idx = _git_subcommand_index(tokens)
    if idx >= len(tokens) or tokens[idx] != "clean":
        return False
    joined_flags = "".join(tok for tok in tokens[idx + 1 :] if tok.startswith("-"))
    return "f" in joined_flags and "d" in joined_flags


def _is_shell_file_write(command: str) -> bool:
    """Return True for shell file-write patterns that should use the edit tool instead.

    Catches ``cat > file``, ``cat >> file``, and inline interpreter writes
    (``python -c "...open(f,'w').write(...)"`` or python heredocs) before
    shlex.split, which chokes on heredoc syntax.
    """
    return bool(_SHELL_FILE_WRITE_RE.search(command)) or bool(_INTERP_WRITE_RE.search(command))


def _extract_write_targets(command: str) -> list[str] | None:
    """Literal write targets in *command*, or ``None`` if any write op is opaque.

    Returns ``None`` (caller must block) when a detected write cannot be tied to
    a literal path: a ``.write_text``/``.write_bytes`` call, or an ``open`` whose
    first argument is a variable or expression rather than a string literal.
    """
    if _WRITE_METHOD_RE.search(command):
        return None
    targets: list[str] = []
    for match in _OPEN_WRITE_TARGET_RE.finditer(command):
        literal = _QUOTED_LITERAL_RE.match(match.group("arg").strip())
        if literal is None:
            return None
        targets.append(literal.group("v"))
    for match in _CAT_REDIRECT_TARGET_RE.finditer(command):
        target = match.group("tgt").strip().strip("'\"")
        if not target:
            return None
        targets.append(target)
    return targets


def _file_write_within_allowed(command: str, allowed_roots: list[Path] | None) -> bool:
    """True if every write target is a literal path inside *allowed_roots*.

    *allowed_roots* are the directories writes may target — the workspace root
    plus any opt-in directories (``additionalDirectories`` /
    ``ATELIER_ADDITIONAL_DIRS``). A relative target resolves against the first
    root (the workspace root). Any opaque target (variable, f-string, or
    ``.write_text`` receiver) makes ``_extract_write_targets`` return ``None``,
    so the guard blocks what it cannot verify.
    """
    if not allowed_roots:
        return False
    targets = _extract_write_targets(command)
    if not targets:
        return False
    roots = [Path(root).resolve() for root in allowed_roots]
    base = roots[0]
    for raw in targets:
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if not any(path == root or path.is_relative_to(root) for root in roots):
            return False
    return True


def _split_command_segments(command: str) -> list[list[str]]:
    """Split a command line into segments on shell control operators.

    ``bash -c`` runs the whole line, so blocklist checks that only inspect
    ``tokens[0]`` are bypassed by chaining (``ok && rm -rf x``) or command
    substitution (``$(rm -rf x)``). Tokenizing the full line and breaking on
    ``; & | && ||``, newlines, and substitution/brace markers yields each
    segment's own leading token for the blocklist checks.
    """
    operators = {";", "&", "|", "&&", "||", "$(", ")", "`", "{", "}"}
    # Newlines separate statements under ``bash -c``, but shlex.split discards
    # them as whitespace -- which would merge a post-newline command into the
    # previous segment and hide its leading token (``echo hi\nrm -rf x``).
    # Convert them to an explicit ``;`` separator before tokenizing.
    command = command.replace("\n", " ; ")
    # Pad control operators and substitution/brace boundaries with whitespace so
    # shlex isolates them even when glued to a token (``a&&rm``, ``true;rm``) and
    # the command inside ``$(...)`` / ``\`...\``` starts a fresh segment.
    # Over-splitting inside a quoted literal only yields extra benign segments;
    # it can never mask a dangerous leading token.
    normalized = re.sub(r"(\$\(|\)|`|\{|\}|&&|\|\||;|&|\|)", r" \1 ", command)
    try:
        tokens = shlex.split(normalized, comments=False)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in operators:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _is_noexec_shell(tokens: list[str]) -> bool:
    """True if a shell interpreter is invoked purely to syntax-check, not run.

    ``bash -n file`` / ``sh -n`` parse the script and exit without executing any
    command, so unlike ``bash -c '...'`` they cannot smuggle a destructive
    command past the per-segment blocklist. Detects ``-n`` standalone or bundled
    (``-nx``) and the ``-o noexec`` long form. Scans options only up to the first
    non-option token (the script path), so ``bash script.sh -n`` — where ``-n``
    belongs to the script, not the shell — is correctly NOT treated as no-exec.
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or tok == "--":
            break
        if tok == "-o":
            if i + 1 < len(tokens) and tokens[i + 1] == "noexec":
                return True
            i += 2
            continue
        if not tok.startswith("--") and _SHELL_NOEXEC_SHORT_RE.match(tok):
            return True
        i += 1
    return False


# A shell short-option cluster carrying inline (`-c`) or stdin (`-s`) code.
_SHELL_INLINE_SHORT_RE = re.compile(r"^-[a-zA-Z]*[cs]")


def _is_script_file_run(tokens: list[str], *, cwd: Path | None) -> bool:
    """True for ``bash <existing script> [args...]`` — no inline code on the line.

    ``bash -c '...'`` (inline) and ``bash -s`` (stdin) stay blocked: their
    command text is opaque to the per-segment blocklist. A script that exists
    on disk is an auditable on-disk artifact — the same risk class as
    ``python file.py`` or ``make``, which the policy already allows. A missing
    path falls through to the block (catches ``bash <(curl ...)`` styles and
    typos).
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok.startswith("-"):
            if not tok.startswith("--") and _SHELL_INLINE_SHORT_RE.match(tok):
                return False  # -c / -s (possibly bundled): inline or stdin code
            if tok == "-o":
                i += 2  # -o consumes its option value
                continue
            i += 1
            continue
        break
    if i >= len(tokens):
        return False  # bare `bash`: interactive / stdin
    script = Path(tokens[i])
    if not script.is_absolute():
        if cwd is None:
            return False
        script = cwd / script
    try:
        return script.is_file()
    except OSError:
        return False


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Wrappers that run a following command; stripping them exposes the real head so
# the blocklist isn't bypassed by ``timeout 5 rm -rf x`` / ``nice rm -rf x`` /
# ``xargs rm -rf x``. After the wrapper we also skip its option flags and numeric
# option-values / durations (``nice -n 10``, ``timeout 5``), but never a
# non-option word, so a real command head (incl. ``/bin/rm``) is never skipped.
_COMMAND_WRAPPERS = frozenset(
    {
        "env",
        "command",
        "sudo",
        "doas",
        "nohup",
        "setsid",
        "stdbuf",
        "unbuffer",
        "nice",
        "ionice",
        "time",
        "timeout",
        "chrt",
        "xargs",
        "watch",
        "proot",
        "flock",
    }
)
# Shell interpreters whose direct execution is blocked (they run arbitrary
# commands). ``busybox`` is handled separately: its applet is the real head.
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "fish", "dash", "ash", "ksh", "mksh", "rbash", "csh", "tcsh"})
# Heads that evaluate their remaining arguments as a fresh command line.
_EVAL_WRAPPERS = frozenset({"eval", "exec"})
# Option flags (``-x``) and numeric option-values / durations (``10``, ``5s``) a
# wrapper may carry before its command; safe to skip (never a command name).
_WRAPPER_SKIP_RE = re.compile(r"^(?:-.*|[+-]?\d+(?:\.\d+)?[smhdkKMGT]?)$")


def _strip_command_prefixes(tokens: list[str]) -> list[str]:
    """Strip leading ``VAR=value`` assignments and pass-through wrappers
    (``env``/``sudo``/``timeout``/``nice``/...) to a fixed point so the real
    command head is checked.

    ``env A=1 bash -c`` / ``command rm -rf`` / ``timeout 5 rm -rf`` would
    otherwise hide a dangerous head behind a wrapper token. After a wrapper we
    also skip its option flags and numeric option-values / durations
    (``nice -n 10``, ``timeout 5``) -- but never a non-option word, so a real
    command head (incl. a path-qualified ``/bin/rm``) is never skipped past.
    """
    i = 0
    changed = True
    while changed:
        changed = False
        while i < len(tokens) and _ASSIGN_RE.match(tokens[i]):
            i += 1
            changed = True
        if i < len(tokens) and os.path.basename(tokens[i]).lower() in _COMMAND_WRAPPERS:
            i += 1
            changed = True
            while i < len(tokens) and _WRAPPER_SKIP_RE.match(tokens[i]):
                i += 1
    return tokens[i:]


def _block_check_segment(
    tokens: list[str], *, cwd: Path | None = None, rm_safe_roots: list[Path] | None = None
) -> CommandPolicyDecision | None:
    """Return a block decision if *tokens* (one segment) is dangerous, else None.

    ``cwd``/``rm_safe_roots`` carve the one exception to the destructive-rm
    block: ``rm -rf`` whose every target resolves inside ``rm_safe_roots``
    (the OS temp directory) is allowed, so an agent can clean up its own
    scratch files without the hard block -- everywhere else stays blocked.
    """
    if not tokens:
        return None
    tokens = _strip_command_prefixes(tokens)
    if not tokens:
        return None
    # Normalize the head to its basename so path-qualified invocations
    # (``/bin/bash``, ``/usr/bin/git``) are matched like their bare names.
    tokens = [os.path.basename(tokens[0]), *tokens[1:]]
    head = tokens[0].lower()
    # ``busybox <applet> ...``: the applet (sh/rm/...) is the effective head.
    if head == "busybox" and len(tokens) > 1:
        return _block_check_segment(tokens[1:], cwd=cwd, rm_safe_roots=rm_safe_roots)
    # ``eval``/``exec <words>``: the remaining words run as a fresh command line,
    # so re-tokenize and block-check them (catches ``eval \"rm -rf x\"``).
    if head in _EVAL_WRAPPERS and len(tokens) > 1:
        for inner in _split_command_segments(" ".join(tokens[1:])):
            decision = _block_check_segment(inner, cwd=cwd, rm_safe_roots=rm_safe_roots)
            if decision is not None:
                return decision
        return None
    if head in _SHELL_INTERPRETERS:
        if _is_noexec_shell(tokens):
            return None  # `bash -n` / `-o noexec`: parse-only, runs nothing
        if _is_script_file_run(tokens, cwd=cwd):
            return None  # `bash existing-script.sh`: on-disk artifact, like `python file.py`
        return CommandPolicyDecision(
            category="shell-interpreter",
            action="block",
            reason=(
                f"inline {head} execution blocked — use Atelier tools "
                f"(allowed: existing script file `{head} path/to/script.sh`; "
                f"syntax check `{head} -n`)"
            ),
        )
    if _is_rm_family(tokens):
        if rm_safe_roots and _rm_confined_to_safe_roots(tokens, cwd=cwd, safe_roots=rm_safe_roots):
            return None  # confined to temp/scratch space -- allow
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="destructive rm -rf blocked",
        )
    if _is_git_reset_hard(tokens):
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="git reset --hard blocked",
        )
    if _is_git_clean_fd(tokens):
        return CommandPolicyDecision(
            category="destructive",
            action="block",
            reason="git clean -fd blocked",
        )
    return None


# Known-bad shell calls -> ALLOW or REDIRECT-and-execute, never block/message.
# Where a read-only equivalent exists we REWRITE: the equivalent runs behind the
# scenes and its result is returned in the SAME turn (like grep->grep_tool), so no
# turn is wasted. Everything else (incl. sed -i replacements, git navigation) is
# ALLOWED to run -- a git-archaeology *spiral* is caught by the convergence escalation.
_FETCH_RE = re.compile(r"\b(?:curl|wget)\b", re.IGNORECASE)
_FETCH_URL_RE = re.compile(r"https?://[^\s'\"|>;)]+", re.IGNORECASE)
_FETCH_SETUP_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|pip[0-9]*|python[0-9.]*|tar|unzip|gunzip|apt|apt-get|brew|npm|node|tee)\b"
    r"|\s-[oO]\b|\s--output\b|>\s*\S"
    r"|&&\s*(?:tar|unzip|pip|sh|bash|make|python|\./)",
    re.IGNORECASE,
)
_FIND_NAME_RE = re.compile(r"\bfind\s+(?:(\S+)\s+)?-(?:i?name|wholename)\s+['\"]?([^'\"\s|>;]+)", re.IGNORECASE)
_SED_PRINT_RE = re.compile(r"\bsed\s+-n\s+['\"]?(\d+)(?:,(\d+))?\s*p['\"]?\s+(\S+)", re.IGNORECASE)


def _redirect_known_bad(command: str) -> CommandPolicyDecision | None:
    """Rewrite known-bad read-only calls to the right tool (executed inline). Never blocks."""
    if _FETCH_RE.search(command) and not _FETCH_SETUP_RE.search(command):
        m = _FETCH_URL_RE.search(command)
        if m:  # plain content fetch -> run web_fetch behind the scenes
            return CommandPolicyDecision(
                category="web-fetch",
                action="rewrite",
                rewrite_target="web_fetch",
                rewrite_payload={"url": m.group(0)},
            )
        return None  # no URL to fetch -> just allow
    mf = _FIND_NAME_RE.search(command)
    if mf:  # find -name PATTERN -> internal file glob, returned inline
        path = mf.group(1) or "."
        if path.startswith("-"):
            path = "."
        return CommandPolicyDecision(
            category="find",
            action="rewrite",
            rewrite_target="find_glob",
            rewrite_payload={"glob": mf.group(2), "path": path},
        )
    ms = _SED_PRINT_RE.search(command)
    if ms:  # sed -n 'A,Bp' FILE  (read-only print) -> read that exact range, inline
        a = ms.group(1)
        b = ms.group(2) or a
        return CommandPolicyDecision(
            category="sed-read",
            action="rewrite",
            rewrite_target="read_range",
            rewrite_payload={"spec": f"{ms.group(3)}:L{a}-L{b}"},
        )
    return None  # sed -i / other sed / other find / git navigation -> ALLOW


def classify_command(
    command: str, *, allowed_write_roots: list[Path] | None = None, cwd: str | Path | None = None
) -> CommandPolicyDecision:
    # `rm -rf` confined entirely to the OS temp directory is allowed -- an
    # agent cleaning up its own scratch files shouldn't hit the same hard
    # block meant to stop a catastrophic delete of the project/home/root.
    rm_safe_roots = [Path(tempfile.gettempdir()).resolve()]
    resolved_cwd = Path(cwd).resolve() if cwd else None
    # Block checks run per segment: bash -c executes the whole line, so chaining
    # and command substitution must not slip a dangerous segment past tokens[0].
    for segment in _split_command_segments(command):
        blocked = _block_check_segment(segment, cwd=resolved_cwd, rm_safe_roots=rm_safe_roots)
        if blocked is not None:
            return blocked

    bad = _redirect_known_bad(command)
    if bad is not None:
        return bad

    try:
        tokens = shlex.split(command)
    except ValueError:
        return CommandPolicyDecision(category="generic", action="allow")
    if not tokens:
        return CommandPolicyDecision(category="generic", action="allow")

    head = tokens[0].lower()
    if head == "cat":
        return _rewrite_cat(tokens)
    if head == "head":
        return _rewrite_head(tokens)
    if head == "tail":
        return _rewrite_tail(tokens)
    if head == "wc":
        return _rewrite_wc(tokens)
    if head in {"rg", "grep"}:
        return _rewrite_search(tokens, head)
    if external_compactors_enabled():
        compactor = compactor_for_command(tokens)
        if compactor is not None:
            resolution = resolve_compactor(compactor.name)
            if resolution.available and resolution.path is not None:
                return CommandPolicyDecision(
                    category="external-compactor",
                    action="rewrite",
                    reason=f"passed through installed `{compactor.name}` binary for compact output",
                    rewrite_target="external_compactor",
                    rewrite_payload={
                        "compactor": compactor.name,
                        "binary_path": str(resolution.path),
                        "original_command": command,
                    },
                )
    return CommandPolicyDecision(category="generic", action="allow")


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


# Bash output is re-read as cache_read on EVERY later turn, so a fat test/log dump
# is paid for once per remaining turn -- the dominant cost on long tasks. Cap the
# char size (line caps miss long-line output like ``git log --format``) and, for
# test runs, keep the actionable failure section + summary instead of head/tail
# (the FAILURES block sits in the middle and head/tail would drop it).
_BASH_STDOUT_CHAR_CAP = 6000
# Test-runner detection spans the mainstream ecosystems (Python, JS/TS, Rust,
# Go, Ruby, PHP, JVM, .NET, Elixir): the failure-extraction path below is what
# keeps a red run actionable inside the char budget, so missing a runner
# silently downgrades its output to blind head/tail.
_TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|runtests|nosetests|tox|jest|vitest|mocha|rspec|phpunit|ctest)\b"
    r"|python[0-9.]*\s+-m\s+(unittest|pytest)"
    r"|manage\.py\s+test"
    r"|\b(cargo|go|dotnet|mix)\s+test\b"
    r"|\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b"
    r"|\bgradlew?\s+[\w:]*test\b"
    r"|\bmvn\b[^|;&]*\btest\b"
    r"|\bplaywright\s+test\b"
)
# Extra fail-start markers only ever move the cut point EARLIER (the first
# matching line wins), so a false positive keeps more context, never less.
_TEST_FAIL_START_RE = re.compile(
    r"^(=+\s*(FAILURES|ERRORS)\s*=+"  # pytest section header
    r"|=+\s*short test summary"  # pytest -q
    r"|FAIL:|ERROR:|FAILED\b"  # unittest / nose
    r"|FAIL\b"  # jest/vitest per-file, go test per-package
    r"|\s*--- FAIL:"  # go test per-test
    r"|\s*failures?:"  # cargo test / rspec section header
    r"|\s*[✕✗●]"  # jest/vitest failure bullets (✕ ✗ ●)
    r"|\s*\d+\)\s"  # mocha / rspec numbered failures
    r")",
    re.IGNORECASE,
)
_TEST_SUMMARY_RE = re.compile(
    r"\d+\s+(passed|failed|failures?|errors?|skipped|passing|failing|pending|examples?)"
    r"|Ran\s+\d+\s+test"
    r"|^(OK|PASS|FAILED?)\b"  # unittest OK, go test ok/PASS/FAIL
    r"|test result:"  # cargo test
    r"|Tests?(\s+Suites)?\s*:\s*\d+",  # jest/vitest "Tests: ...", dotnet "Total tests: N"
    re.IGNORECASE,
)


def _cap_chars(text: str, max_chars: int) -> str:
    """Keep head + tail of *text* within *max_chars* (long-line safe)."""
    if len(text) <= max_chars:
        return text
    h = max_chars * 3 // 4
    t = max_chars - h
    return f"{text[:h]}\n... ({len(text) - max_chars:,} chars trimmed) ...\n{text[-t:]}"


def _extract_test_output(text: str, max_chars: int = _BASH_STDOUT_CHAR_CAP) -> str:
    """From a test run, keep the actionable failures + summary; drop pass/collection noise."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _TEST_FAIL_START_RE.search(ln)), None)
    if start is not None:  # there are failures -- keep from the first failure marker on
        return _cap_chars("\n".join(lines[start:]), max_chars)
    summary = [ln for ln in lines if _TEST_SUMMARY_RE.search(ln)]
    if summary:  # all green -- the summary line is all the agent needs
        return "\n".join(summary[-3:])
    return _cap_chars(text, max_chars)


# Dedup-with-count: log-style output (retry loops, polling waits, repeated
# warnings, stack frames) often repeats one line dozens or hundreds of times.
# Collapsing a run of identical lines to one copy plus an annotated count is
# information-preserving, so it can run before every other compaction path.
# Runs shorter than the threshold are left alone -- a 2x repeat may be
# meaningful sequence data and saves almost nothing.
_DEDUP_MIN_REPEATS = 3


def _dedupe_repeated_lines(text: str) -> tuple[str, int]:
    """Collapse runs of >= ``_DEDUP_MIN_REPEATS`` identical lines.

    Returns ``(deduped_text, chars_saved)``. Non-blank runs keep one copy plus
    an explicit ``(line repeated N times)`` marker; blank runs collapse
    silently to a single blank line. Unchanged text is returned as-is.
    """
    lines = text.splitlines()
    if len(lines) < _DEDUP_MIN_REPEATS:
        return text, 0
    out: list[str] = []
    changed = False
    i = 0
    n = len(lines)
    while i < n:
        j = i + 1
        while j < n and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= _DEDUP_MIN_REPEATS:
            out.append(lines[i])
            if lines[i].strip():
                out.append(f"... (line repeated {run} times) ...")
            changed = True
        else:
            out.extend(lines[i:j])
        i = j
    if not changed:
        return text, 0
    deduped = "\n".join(out)
    if text.endswith("\n"):
        deduped += "\n"
    return deduped, max(0, len(text) - len(deduped))


# Per-command-kind stdout budgets. Bare listings (ls/tree/du/git status ...) are
# enumerations -- mostly noise -- so they get a lean cap; test runs keep more
# (failures are the actionable signal, and truncating them forces a costly
# re-run); everything else keeps the default head+tail cap.
_BASH_LISTING_RE = re.compile(
    r"^\s*(?:cd\s+[^&|;]+&&\s*)?(?:ls|tree|du|df|find|stat|env|printenv|ps"
    r"|git\s+status|git\s+ls-files|git\s+branch)\b",
    re.IGNORECASE,
)
_BASH_LISTING_CHAR_CAP = 2000
_BASH_TEST_CHAR_CAP = 8000


def _bash_output_budget(command: str) -> int:
    """Stdout char budget keyed by command kind (test / listing / generic)."""
    if _TEST_CMD_RE.search(command):
        return _BASH_TEST_CHAR_CAP
    if _BASH_LISTING_RE.search(command):
        return _BASH_LISTING_CHAR_CAP
    return _BASH_STDOUT_CHAR_CAP


# Generalizes _extract_test_output beyond test runners. Any long-running
# command (build, migration, deploy script, linter) can bury its one actionable
# line in the middle of an otherwise-routine log, and blind head/tail -- like
# the FAILURES block for test runs -- would drop exactly that line.
_ANOMALY_LINE_RE = re.compile(
    r"\b(error|exception|traceback|fatal|panic|denied|refused|failed|failure|"
    r"segfault|deadlock|cannot|can't|unable to)\b",
    re.IGNORECASE,
)


def _extract_anomaly_windows(text: str, max_chars: int, *, context: int = 3) -> str | None:
    """For a non-test command, keep a window of context lines around each
    anomaly-marker line instead of blind head/tail. Returns ``None`` when no
    marker is found anywhere in *text*, so the caller falls back to the
    existing head+tail path unchanged -- a clean run's output is untouched.
    """
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if _ANOMALY_LINE_RE.search(ln)]
    if not hits:
        return None
    windows: list[list[int]] = []
    for i in hits:
        start, end = max(0, i - context), min(len(lines), i + context + 1)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    parts: list[str] = []
    prev_end = 0
    for start, end in windows:
        if start > prev_end:
            parts.append(f"... ({start - prev_end} lines omitted) ...")
        parts.extend(lines[start:end])
        prev_end = end
    if prev_end < len(lines):
        parts.append(f"... ({len(lines) - prev_end} lines omitted) ...")
    return _cap_chars("\n".join(parts), max_chars)


# Suppress-on-success for the rtk-excluded zone. External compactors only wrap
# read-only/idempotent commands (see external_compactors: "a compactor
# invocation must never be the thing that decides whether a side-effecting
# command runs once or twice"), so noisy *mutating* commands -- git push/pull,
# package installs, docker builds -- always reach this module raw. On success
# their output is almost entirely progress/boilerplate: collapse it to an
# `ok:` one-liner post-hoc (the command already ran exactly once) and keep the
# full text recoverable via the spill store. Failures never take this path.
_SUPPRESS_SUCCESS_RE = re.compile(
    r"^\s*(?:cd\s+[^&|;]+&&\s*)?(?:"
    r"git\s+(?:commit|push|pull|fetch|clone)"
    r"|(?:uv|pip3?|pipx|poetry)\s+(?:pip\s+)?(?:install|sync|add|update)"
    r"|(?:npm|pnpm)\s+(?:install|ci|i|add|update)"
    r"|yarn\s+(?:install|add)"
    r"|bundle\s+install"
    r"|cargo\s+(?:install|fetch)"
    r"|docker\s+(?:build|pull|push)"
    r"|docker\s+compose\s+(?:build|pull)"
    r"|make"
    r")\b",
    re.IGNORECASE,
)
# Below this combined size the output is already cheap; collapsing it would
# save little and the `ok:` line plus marker could even inflate it.
_SUPPRESS_SUCCESS_MIN_CHARS = 600
_SUPPRESS_SALIENT_MAX_CHARS = 200
# git commit's summary is its *first* line ("[main abc1234] message"); for
# everything else (installs, pushes, builds) the last line is the summary.
_LEADING_SUMMARY_RE = re.compile(r"^\[[^\]]+\]")


def _suppress_success_summary(command: str, stdout: str, stderr: str, exit_code: int) -> str | None:
    """``ok: <salient line>`` for a noisy side-effecting command that succeeded.

    Returns None (no suppression) unless ALL hold: exit 0, the command is a
    known noisy mutator, the output is big enough to be worth collapsing, and
    nothing in it looks like an error. An error-looking line on exit 0 falls
    through to ``_extract_anomaly_windows``, which keeps context around it.
    """
    if exit_code != 0 or not _SUPPRESS_SUCCESS_RE.search(command):
        return None
    if len(stdout) + len(stderr) <= _SUPPRESS_SUCCESS_MIN_CHARS:
        return None
    if _ANOMALY_LINE_RE.search(stdout) or _ANOMALY_LINE_RE.search(stderr):
        return None
    # git push/clone write everything to stderr; fall back to it when stdout
    # is empty.
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return None
    salient = lines[0] if _LEADING_SUMMARY_RE.match(lines[0]) else lines[-1]
    return f"ok: {salient[:_SUPPRESS_SALIENT_MAX_CHARS]}"


# Upstream flag injection -- the lossless tier: when a command has a
# machine-stable compact format behind a flag (`git status --porcelain`,
# `pytest -q --tb=short`), asking the tool for it up front beats any post-hoc
# trimming because nothing has to be thrown away afterwards. Only exact bare
# forms (plus an optional leading `cd X &&`) are touched, only single-segment
# commands, and only when the agent expressed no formatting intent of its
# own; the injected invocation is announced in the first output line so the
# agent knows which format it is reading. The caller executes the injected
# command but keys every ledger (discipline, delta, budgets, spill) on the
# original -- the mapping is deterministic, so semantics stay consistent.
# Kill switch: ATELIER_BASH_FLAG_INJECTION=0.
_FLAG_INJECTION_ENV = "ATELIER_BASH_FLAG_INJECTION"
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+[^&|;]+&&\s*")
_PYTEST_RUNNER_TOKENS = frozenset({"uv", "run", "poetry", "pipenv", "hatch", "exec", "-m", "sudo", "time"})
_PYTEST_FORMAT_FLAG_RE = re.compile(r"(?:^|\s)(?:-q|--quiet|-v+\b|--verbose|-r[A-Za-z]+|--tb(?:=|\s))")


def _flag_injection_enabled() -> bool:
    return os.environ.get(_FLAG_INJECTION_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _is_pytest_invocation(body: str) -> bool:
    """True when *body* actually invokes pytest -- not merely mentions it
    (``pip install pytest`` must never get test flags appended)."""
    try:
        tokens = shlex.split(body)
    except ValueError:
        return False
    for idx, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] in {"pytest", "py.test"}:
            return all(
                t in _PYTEST_RUNNER_TOKENS or "=" in t or re.fullmatch(r"python[0-9.]*", t) is not None
                for t in tokens[:idx]
            )
    return False


def _inject_stable_flags(command: str) -> tuple[str, str]:
    """``(exec_command, note)``: append machine-stable output flags when safe.

    Returns *command* unchanged (note == "") unless it is a bare ``git
    status`` / ``git log`` or a single-segment pytest invocation without its
    own verbosity/traceback flags.
    """
    if not _flag_injection_enabled():
        return command, ""
    body = _CD_PREFIX_RE.sub("", command).strip()
    if any(ch in body for ch in ("|", ";", "&", ">", "<")):
        return command, ""
    injected: str | None = None
    if body == "git status":
        injected = f"{command.rstrip()} --porcelain=v1 -b"
    elif body == "git log":
        injected = f"{command.rstrip()} --oneline -n 50"
    elif _is_pytest_invocation(body) and not _PYTEST_FORMAT_FLAG_RE.search(body):
        injected = f"{command.rstrip()} -q --tb=short"
    if injected is None:
        return command, ""
    return injected, f"[ran: {injected}]"


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
    if exit_code != 0:
        head = 20
        tail = max(max_lines - head, 50)
    else:
        head = max(20, max_lines // 4)
        tail = max(max_lines - head, 0)
    clean_stdout = _strip_ansi(raw_stdout)
    clean_stderr = _strip_ansi(raw_stderr)
    # Stripped ANSI escapes are real payload bytes removed before any budget or
    # truncation accounting; fold them into chars_omitted so the savings sink
    # (_bash_omitted_tokens_saved) credits them like any other trimmed output.
    ansi_chars_stripped = (len(raw_stdout) - len(clean_stdout)) + (len(raw_stderr) - len(clean_stderr))
    # Dedup-with-count next: the annotated collapse is information-preserving,
    # so every later path (test extraction, suppress-on-success, anomaly
    # windows, head+tail) works on the smaller text, and the saved chars are
    # credited to the ledger like any other trimmed output.
    clean_stdout, dedup_stdout_chars = _dedupe_repeated_lines(clean_stdout)
    clean_stderr, dedup_stderr_chars = _dedupe_repeated_lines(clean_stderr)
    budget = max_chars if max_chars is not None else _bash_output_budget(command)
    stderr_folded = False
    if _TEST_CMD_RE.search(command):
        compact = _extract_test_output(clean_stdout, max_chars=budget)
        stdout_omitted = 0
        stdout_chars = max(0, len(clean_stdout) - len(compact))
        stdout_compact = compact
    elif (suppressed := _suppress_success_summary(command, clean_stdout, clean_stderr, exit_code)) is not None:
        # Successful noisy mutator: stdout AND stderr collapse to the one-liner.
        # All original lines count as omitted so the spill below always runs
        # and the savings ledger is credited.
        src_lines = len(clean_stdout.splitlines()) + len(clean_stderr.splitlines())
        stdout_compact = f"{suppressed}\n... ({src_lines} output lines suppressed on success) ..."
        stdout_omitted = len(clean_stdout.splitlines())
        stdout_chars = max(0, len(clean_stdout) - len(stdout_compact))
        stderr_folded = True
    else:
        anomaly = _extract_anomaly_windows(clean_stdout, budget)
        if anomaly is not None:
            stdout_compact = anomaly
            stdout_omitted = 0
            stdout_chars = max(0, len(clean_stdout) - len(anomaly))
        else:
            stdout_compact, stdout_omitted, stdout_chars = _head_tail_lines(clean_stdout.splitlines(), head, tail)
            capped = _cap_chars(stdout_compact, budget)
            if capped != stdout_compact:
                stdout_chars += len(stdout_compact) - len(capped)
                stdout_compact = capped
    if stderr_folded:
        stderr_compact = ""
        stderr_omitted = len(clean_stderr.splitlines())
        stderr_chars = len(clean_stderr)
    else:
        stderr_compact, stderr_omitted, stderr_chars = _head_tail_lines(clean_stderr.splitlines(), 100, 100)
    lines_omitted = stdout_omitted + stderr_omitted
    chars_omitted = stdout_chars + stderr_chars + ansi_chars_stripped + dedup_stdout_chars + dedup_stderr_chars
    spill_hint = ""
    if lines_omitted > 0:
        # The head+tail markers above are lossy; spill the untouched raw
        # stdout/stderr so the omitted lines stay recoverable via `read`.
        full_text = clean_stdout
        if clean_stderr.strip():
            full_text = f"{full_text}\n\n--- stderr ---\n{clean_stderr}" if full_text else clean_stderr
        kept_text = stdout_compact
        if stderr_compact.strip():
            kept_text = f"{kept_text}\n\n--- stderr ---\n{stderr_compact}" if kept_text else stderr_compact
        spill_hint = _spill_hint(full_text, len(kept_text))
    # Live tool-output redaction (G8): scrub secrets from command output
    # before it reaches the model. Honors the ATELIER_OUTPUT_REDACTION
    # kill-switch and is a no-op on already-clean text.
    return RunResult(
        stdout=redact_tool_output(stdout_compact),
        stderr=redact_tool_output(stderr_compact),
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=lines_omitted > 0,
        lines_omitted=lines_omitted,
        chars_omitted=chars_omitted,
        command=command,
        spill_hint=spill_hint,
    )


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


def _close_managed_process_pipes(managed: _ManagedCommand) -> None:
    for stream in (managed.proc.stdout, managed.proc.stderr):
        with contextlib.suppress(Exception):
            if stream is not None:
                stream.close()


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
    """Seconds since `managed.started` at which the watcher kills the process.

    Default (nobody has called action="update" yet): `managed.timeout` is only
    the caller's *soft* response budget (see the module docstring on
    _MANAGED_COMMAND_HARD_CAP_S) -- it never kills anything on its own, so a
    short/default timeout on a deliberately-backgrounded command (start a
    server, keep it running) doesn't get the service killed out from under
    the task; the real kill deadline floors at the much larger hard cap
    instead. Once update_managed_command has set `managed.deadline_explicit`,
    `managed.timeout` IS the exact, enforced deadline -- no more floor --
    since installing it that way (bash action="update") is a deliberate act
    rather than the passive default nobody thought about.
    """
    if managed.deadline_explicit:
        return float(managed.timeout)
    return max(float(managed.timeout), _MANAGED_COMMAND_HARD_CAP_S)


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


def start_managed_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: int = 30,
    max_lines: int = 200,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Start a command without blocking the MCP request."""
    policy = classify_command(command, cwd=cwd)
    if policy.action == "block":
        return {
            "status": "blocked",
            "stderr": policy.reason,
            "exit_code": -1,
            "blocked": True,
            "blocked_reason": policy.reason,
        }

    exec_command, injected_note = _inject_stable_flags(command)
    session_id = uuid.uuid4().hex
    stdout_file, stdout_path = _open_stream_file(session_id, "stdout")
    stderr_file, stderr_path = _open_stream_file(session_id, "stderr")
    try:
        # Pipe the child's output through drain threads rather than handing the
        # temp-file fds straight to the kernel. A direct fd lets a runaway
        # producer (`cat /dev/zero`) fill the disk before any poll reads it; the
        # spool pump caps each temp file at `_MAX_SPOOL_BYTES` instead.
        # stdin=DEVNULL: the MCP server's stdin is an open JSON-RPC pipe, so
        # inheriting it causes any child that reads stdin (e.g. `sys.stdin.read()`
        # in a python -c snippet) to block forever instead of failing fast.
        # start_new_session=True calls setsid() in the child, placing it in
        # its own session and process group.  This has two effects:
        #   1. The child is detached from the MCP server's process group --
        #      if the MCP process dies the child is NOT sent SIGHUP and keeps
        #      running (safe background / long-lived server use-case).
        #   2. _terminate_process_group() can cleanly kill the whole subtree
        #      via SIGTERM/SIGKILL to the child's own pgid.
        proc = subprocess.Popen(
            ["bash", "-c", exec_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception:
        stdout_file.close()
        stderr_file.close()
        raise

    managed = _ManagedCommand(
        command=command,
        proc=proc,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        started=time.perf_counter(),
        timeout=timeout,
        max_lines=max_lines,
        max_chars=max_chars,
        injected_note=injected_note,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    managed.readers = [
        threading.Thread(target=_spool_managed_stream, args=(proc.stdout, stdout_file, managed), daemon=True),
        threading.Thread(target=_spool_managed_stream, args=(proc.stderr, stderr_file, managed), daemon=True),
    ]
    for reader in managed.readers:
        reader.start()
    with _MANAGED_COMMANDS_LOCK:
        _MANAGED_COMMANDS[session_id] = managed
    threading.Thread(
        target=_watch_managed_command,
        args=(session_id,),
        daemon=True,
        name=f"atelier-shell-{session_id[:8]}",
    ).start()
    started_payload = {
        "status": "running",
        "session_id": session_id,
        "pid": proc.pid,
        "timeout": timeout,
    }
    if managed.stdout_path:
        started_payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        started_payload["log_file_stderr"] = managed.stderr_path
    return started_payload


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
    elapsed_ms = int((time.perf_counter() - managed.started) * 1000)
    stdout_tail, stderr_tail = _tail_managed_output(managed, tail_lines)
    payload: dict[str, Any] = {
        "status": "running" if running else managed.state,
        "session_id": session_id,
        "pid": managed.proc.pid,
        "duration_ms": elapsed_ms,
        "stdout": _strip_ansi("\n".join(stdout_tail)),
        "stderr": _strip_ansi("\n".join(stderr_tail)),
        "tail_lines": tail_lines,
    }
    if managed.stdout_path:
        payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        payload["log_file_stderr"] = managed.stderr_path
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
    output_byte_capped = stdout_capped or stderr_capped or managed.spool_truncated
    if stdout_capped:
        raw_stdout += _OUTPUT_CAP_NOTICE.format(cap=_MAX_OUTPUT_BYTES)
    if stderr_capped:
        raw_stderr += _OUTPUT_CAP_NOTICE.format(cap=_MAX_OUTPUT_BYTES)

    if managed.state == "timed_out":
        exit_code = -1
        raw_stderr = f"Command timed out after {int(_effective_deadline_s(managed))}s"
    elif managed.state == "cancelled":
        exit_code = -1
        raw_stderr = "Command cancelled"
    else:
        exit_code = managed.proc.returncode
    if reader_wedged:
        wedged_note = (
            "note: a child process may still be running and holding this command's "
            "output stream open (e.g. a backgrounded server); output above reflects "
            "everything captured before this command's own process exited."
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
    }
    if managed.stdout_path:
        payload["log_file"] = managed.stdout_path
    if managed.stderr_path:
        payload["log_file_stderr"] = managed.stderr_path
    return payload


def update_managed_command(session_id: str, timeout: float) -> dict[str, Any]:
    """Install (or move) a running managed command's *enforced* kill deadline
    (bash action="update").

    Before any update, `timeout` is only ever the soft response budget -- the
    real kill floors at the ~1hr hard cap (see _effective_deadline_s). Calling
    update is a deliberate act: from here on `timeout` is the exact, enforced
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
    max_lines: int = 200,
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
        and policy.rewrite_target in {"head", "tail", "wc"}
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

    exec_command, injected_note = _inject_stable_flags(command)
    started = time.perf_counter()
    proc: subprocess.Popen[str] | None = None
    output_byte_capped = False
    try:
        proc = subprocess.Popen(
            ["bash", "-c", exec_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        # Drain both pipes concurrently into bounded in-memory buffers. A plain
        # `communicate()` slurps the child's *entire* output into RAM before any
        # cap runs, so a runaway producer (`yes`, `cat /dev/zero`) OOMs the host.
        # `_pump_capped` stops accumulating at `_MAX_OUTPUT_BYTES` per stream but
        # keeps reading to EOF, and running one thread per stream avoids the
        # pipe-buffer deadlock when both stdout and stderr are large.
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        capped = {"stdout": False, "stderr": False}

        def _drain(stream: Any, buf: list[str], key: str) -> None:
            with contextlib.suppress(Exception):
                capped[key] = _pump_capped(stream, buf.append, _MAX_OUTPUT_BYTES)

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, "stdout"), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the group first so the child's pipes close; otherwise the
            # reader joins below would block forever on a still-open pipe.
            _terminate_process_group(proc)
            for reader in readers:
                reader.join()
            raise
        for reader in readers:
            reader.join()
        exit_code = proc.returncode
        raw_stdout = _strip_ansi("".join(stdout_buf))
        raw_stderr = _strip_ansi("".join(stderr_buf))
        stdout_capped = capped["stdout"]
        stderr_capped = capped["stderr"]
        output_byte_capped = stdout_capped or stderr_capped
        if stdout_capped:
            raw_stdout += _OUTPUT_CAP_NOTICE.format(cap=_MAX_OUTPUT_BYTES)
        if stderr_capped:
            raw_stderr += _OUTPUT_CAP_NOTICE.format(cap=_MAX_OUTPUT_BYTES)
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_stdout = ""
        raw_stderr = f"Command timed out after {timeout}s"
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        exit_code = -1
        raw_stdout = ""
        raw_stderr = str(exc)

    duration_ms = int((time.perf_counter() - started) * 1000)

    if output_delta.observe(command, cwd=cwd, stdout=raw_stdout, stderr=raw_stderr, exit_code=exit_code):
        # Run-and-dedup (execution NEVER skipped): the command really ran and
        # produced byte-identical output to its previous run this session, so
        # every byte is already in the model's context -- ship a marker. The
        # full text is still spilled for recovery, because re-running the
        # command would just yield the marker again.
        total_lines = len(raw_stdout.splitlines()) + len(raw_stderr.splitlines())
        total_chars = len(raw_stdout) + len(raw_stderr)
        anchor_src = raw_stdout if raw_stdout.strip() else raw_stderr
        anchor = next((ln.strip() for ln in anchor_src.splitlines() if ln.strip()), "")
        marker = (
            "unchanged: output byte-identical to this command's previous run this session "
            f'(exit 0, {total_lines} lines, {total_chars} chars; first line: "{anchor[:120]}")'
        )
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
    result.truncated = result.truncated or output_byte_capped
    result.policy_category = policy.category
    result.policy_action = policy.action
    result.policy_reason = policy.reason
    result.rewrite_target = policy.rewrite_target
    result.rewrite_payload = policy.rewrite_payload
    return result


__all__ = [
    "CommandPolicyDecision",
    "RunResult",
    "classify_command",
    "execute_inline_op",
    "poll_managed_command",
    "register_completion",
    "run_command",
    "start_managed_command",
]
