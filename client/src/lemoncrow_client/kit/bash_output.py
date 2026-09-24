"""The post-hoc pipeline for a finished command's output.

Bash output is re-read as cache_read on every later turn, so a fat test or log
dump is paid for once per remaining turn -- the dominant cost on long tasks.
Both ``bash`` surfaces (the thin client's executor and the main package's
managed commands) run this pipeline once a command has run exactly once: ANSI
stripping, repeated-line collapse, output profiles and semantic compression,
test-failure extraction, suppress-on-success for noisy mutators, windows
around error lines, per-command budgets, head+tail caps, secret redaction, and
a footer naming where the untrimmed text went. Where that text is stored is the
caller's business: pass ``spill``.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from .bash_output_compression import compact_bash_stream, terminal_visible_text
from .bash_output_profiles import compact_profiled_output
from .notices import spill_notice
from .redaction import redact_tool_output

__all__ = [
    "ANOMALY_LINE_RE",
    "DEFAULT_MAX_LINES",
    "SPILL_ESCALATION_MIN_OMITTED_CHARS",
    "SPILL_NOTICE_MIN_OMITTED_CHARS",
    "STDOUT_CHAR_CAP",
    "TEST_CMD_RE",
    "CompactedOutput",
    "cap_chars",
    "compact_command_output",
    "dedupe_repeated_lines",
    "extract_anomaly_windows",
    "extract_test_output",
    "head_tail_lines",
    "inject_stable_flags",
    "is_test_only_command",
    "output_budget",
    "spill_footer",
    "strip_ansi",
    "suppress_success_summary",
    "unchanged_marker",
]

_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: colors, cursor movement, erase
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC (title, hyperlinks), BEL- or ST-terminated
    r"|\x1b[@-Z\\-_]"  # bare two-byte escapes (incl. unterminated openers)
)


def strip_ansi(text: str) -> str:
    return terminal_visible_text(_ANSI_ESCAPE.sub("", text))


# Default line bound for compacted output. At (or above) this default the char
# budget alone decides truncation; an explicitly TIGHTENED max_lines from the
# caller stays a hard line bound (see _compact_result).
DEFAULT_MAX_LINES = 200


# Hard ceiling on how many bytes of stdout/stderr are materialized into memory
# from a single command. A runaway child (`cat /dev/zero`, `yes`, `gzip -dc`,
# a chatty build) would otherwise fill the temp file to disk and OOM on a full
# `.read()`, and compaction costs about a second per megabyte of line-heavy
# output, so the cap applies before it. Configurable via env, with a 64KiB
# floor so it can never be set so low that ordinary output is mangled.
MAX_OUTPUT_BYTES = max(
    64 * 1024,
    int(os.environ.get("LEMONCROW_SHELL_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024))),
)

OUTPUT_CAP_NOTICE = (
    "\n... (output exceeded {cap} bytes and was truncated by LemonCrow; narrow the command or redirect to a file) ..."
)


def cap_output_bytes(text: str, cap: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Bound *text* to *cap* UTF-8 bytes, returning ``(text, truncated)``.

    Measured in UTF-8 bytes to mirror on-disk size; the cut lands on a
    character boundary at or just under the cap.
    """
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= cap:
        return text, False
    return encoded[:cap].decode("utf-8", "ignore"), True


def head_tail_lines(lines: list[str], head: int, tail: int) -> tuple[str, int, int]:
    if len(lines) <= head + tail:
        return "\n".join(lines), 0, 0
    omitted_lines = lines[head : len(lines) - tail]
    omitted = len(omitted_lines)
    omitted_chars = sum(len(line) for line in omitted_lines)
    parts = [*lines[:head], f"... ({omitted} lines omitted) ...", *lines[-tail:]]
    return "\n".join(parts), omitted, omitted_chars


# Footer economics: below this many omitted chars the [lc: shrunk ...] footer
# costs more attention than the trim saved -- a "4 passed" result must not
# drag a spill notice for elided progress dots.
SPILL_NOTICE_MIN_OMITTED_CHARS = 300
# Escalate to the read-the-spill instruction only when the elided middle is
# big enough to plausibly hold the answer.
SPILL_ESCALATION_MIN_OMITTED_CHARS = 2000


# Bash output is re-read as cache_read on EVERY later turn, so a fat test/log dump
# is paid for once per remaining turn -- the dominant cost on long tasks. Cap the
# char size (line caps miss long-line output like ``git log --format``) and, for
# test runs, keep the actionable failure section + summary instead of head/tail
# (the FAILURES block sits in the middle and head/tail would drop it).
# Generic stdout budget. 6000 proved a false economy on benchmark transcripts:
# any file view over ~6KB (sed/awk/python-print of a 250-line source file) came
# back middle-elided, and agents burned 6+ turns re-viewing the same file
# instead of reading the spill. 20000 (~5K tokens) ships moderate bodies whole;
# genuinely large output still gets head+tail + spill recovery.
STDOUT_CHAR_CAP = 20000
# Test-runner detection spans the mainstream ecosystems (Python, JS/TS, Rust,
# Go, Ruby, PHP, JVM, .NET, Elixir): the failure-extraction path below is what
# keeps a red run actionable inside the char budget, so missing a runner
# silently downgrades its output to blind head/tail.
TEST_CMD_RE = re.compile(
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
_TEST_CHAIN_RE = re.compile(r"(?:&&|\|\||[;|])")
_TEST_CD_PREFIX_RE = re.compile(r"^\s*cd\s+[^&|;]+&&\s*")


def is_test_only_command(command: str) -> bool:
    """True for one test invocation, allowing only a leading ``cd … &&``.

    Summary extraction is intentionally lossy. Applying it to ``pytest && git
    status`` would silently discard the second command's output, so compound
    commands stay on the generic anomaly/head-tail path.
    """
    body = _TEST_CD_PREFIX_RE.sub("", command, count=1).strip()
    return bool(TEST_CMD_RE.search(body)) and not bool(_TEST_CHAIN_RE.search(body))


def cap_chars(text: str, max_chars: int) -> str:
    """Keep head + tail of *text* within *max_chars* (long-line safe)."""
    if len(text) <= max_chars:
        return text
    h = max_chars * 3 // 4
    t = max_chars - h
    return f"{text[:h]}\n... ({len(text) - max_chars:,} chars trimmed) ...\n{text[-t:]}"


def extract_test_output(text: str, max_chars: int = STDOUT_CHAR_CAP) -> str:
    """From a test run, keep the actionable failures + summary; drop pass/collection noise."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _TEST_FAIL_START_RE.search(ln)), None)
    if start is not None:  # there are failures -- keep from the first failure marker on
        return cap_chars("\n".join(lines[start:]), max_chars)
    summary = [ln for ln in lines if _TEST_SUMMARY_RE.search(ln)]
    if summary:  # all green -- the summary line is all the agent needs
        return "\n".join(summary[-3:])
    return cap_chars(text, max_chars)


# Dedup-with-count: log-style output (retry loops, polling waits, repeated
# warnings, stack frames) often repeats one line dozens or hundreds of times.
# Collapsing a run of identical lines to one copy plus an annotated count is
# information-preserving, so it can run before every other compaction path.
# Runs shorter than the threshold are left alone -- a 2x repeat may be
# meaningful sequence data and saves almost nothing.
_DEDUP_MIN_REPEATS = 3


def dedupe_repeated_lines(text: str) -> tuple[str, int]:
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


def output_budget(command: str) -> int:
    """Stdout char budget keyed by command kind (test / listing / generic)."""
    if TEST_CMD_RE.search(command):
        return _BASH_TEST_CHAR_CAP
    if _BASH_LISTING_RE.search(command):
        return _BASH_LISTING_CHAR_CAP
    return STDOUT_CHAR_CAP


# Generalizes extract_test_output beyond test runners. Any long-running
# command (build, migration, deploy script, linter) can bury its one actionable
# line in the middle of an otherwise-routine log, and blind head/tail -- like
# the FAILURES block for test runs -- would drop exactly that line.
ANOMALY_LINE_RE = re.compile(
    r"\b(error|exception|traceback|fatal|panic|warning|deprecated|denied|refused|"
    r"failed|failure|segfault|deadlock|cannot|can't|unable to|undefined reference|"
    r"not found|timed out|timeout|out of memory|oom|killed|permission|conflict)\b",
    re.IGNORECASE,
)


def extract_anomaly_windows(text: str, max_chars: int, *, context: int = 3) -> str | None:
    """For a non-test command, keep a window of context lines around each
    anomaly-marker line instead of blind head/tail. Returns ``None`` when no
    marker is found anywhere in *text*, so the caller falls back to the
    existing head+tail path unchanged -- a clean run's output is untouched.
    """
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if ANOMALY_LINE_RE.search(ln)]
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
    return cap_chars("\n".join(parts), max_chars)


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


def suppress_success_summary(command: str, stdout: str, stderr: str, exit_code: int) -> str | None:
    """``ok: <salient line>`` for a noisy side-effecting command that succeeded.

    Returns None (no suppression) unless ALL hold: exit 0, the command is a
    known noisy mutator, the output is big enough to be worth collapsing, and
    nothing in it looks like an error. An error-looking line on exit 0 falls
    through to ``extract_anomaly_windows``, which keeps context around it.
    """
    if exit_code != 0 or not _SUPPRESS_SUCCESS_RE.search(command):
        return None
    if len(stdout) + len(stderr) <= _SUPPRESS_SUCCESS_MIN_CHARS:
        return None
    if ANOMALY_LINE_RE.search(stdout) or ANOMALY_LINE_RE.search(stderr):
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
# Kill switch: LEMONCROW_BASH_FLAG_INJECTION=0.
_FLAG_INJECTION_ENV = "LEMONCROW_BASH_FLAG_INJECTION"
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


def inject_stable_flags(command: str) -> tuple[str, str]:
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


@dataclass(frozen=True, slots=True)
class CompactedOutput:
    """What the model sees of one command's output, and what was dropped."""

    stdout: str
    stderr: str
    truncated: bool
    lines_omitted: int
    chars_omitted: int
    spill_hint: str = ""


def spill_footer(full_text: str, kept_chars: int, store: Callable[[str], str | None]) -> str:
    """Store the untrimmed *full_text* via *store* and return the footer naming it.

    *store* persists the text and returns the path a ``read`` can recover it
    from, or ``None`` when it could not. Returns ``""`` when the trim is too
    small to be worth a footer, or nothing was stored. Heavy elision (more than
    70% of the body dropped) escalates to an explicit instruction to read the
    spill: a bare path proved too weak a cue when the answer sat in the elided
    middle.
    """
    if not full_text:
        return ""
    if len(full_text) - kept_chars < SPILL_NOTICE_MIN_OMITTED_CHARS:
        return ""  # trim too small to be worth a footer at all
    path = store(full_text)
    if path is None:
        return ""
    notice = spill_notice(verb="shrunk", original_chars=len(full_text), kept_chars=kept_chars, path=path)
    if kept_chars < 0.3 * len(full_text) and len(full_text) - kept_chars >= SPILL_ESCALATION_MIN_OMITTED_CHARS:
        notice += (
            "\n[lc: mostly elided -- if the answer is in the omitted middle, read the spill file before concluding]"
        )
    return notice


def unchanged_marker(stdout: str, stderr: str) -> str:
    """The one line that replaces a successful rerun's byte-identical output."""
    total_lines = len(stdout.splitlines()) + len(stderr.splitlines())
    total_chars = len(stdout) + len(stderr)
    anchor_source = stdout if stdout.strip() else stderr
    anchor = next((line.strip() for line in anchor_source.splitlines() if line.strip()), "")
    # Redact before cutting: a cut can split a secret so its pattern no longer matches.
    anchor = redact_tool_output(anchor)
    return (
        "unchanged: output byte-identical to this command's previous run this session "
        f'(exit 0, {total_lines} lines, {total_chars} chars; first line: "{anchor[:120]}")'
    )


def compact_command_output(
    command: str,
    raw_stdout: str,
    raw_stderr: str,
    exit_code: int,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars: int | None = None,
    spill: Callable[[str, int], str] | None = None,
) -> CompactedOutput:
    """Trim one finished command's output for the model; see the module docstring.

    ``spill(full_text, kept_chars)`` stores the untrimmed text and returns the
    footer naming it (``""`` when nothing was stored); it runs only when the
    trim was lossy.
    """
    if exit_code != 0:
        head = 20
        tail = max(max_lines - head, 50)
    else:
        head = max(20, max_lines // 4)
        tail = max(max_lines - head, 0)
    # Successful under-budget output can ship whole. Failures still pass through
    # anomaly extraction even below the char cap: hundreds of routine lines
    # around one buried error are technically small but operationally wasteful.
    line_elision_bypass = exit_code == 0 and max_lines >= DEFAULT_MAX_LINES
    budget = max_chars if max_chars is not None else output_budget(command)

    clean_stdout = strip_ansi(raw_stdout)
    clean_stderr = strip_ansi(raw_stderr)
    full_stdout = clean_stdout
    full_stderr = clean_stderr
    terminal_chars_stripped = (len(raw_stdout) - len(clean_stdout)) + (len(raw_stderr) - len(clean_stderr))

    clean_stdout, dedup_stdout_chars = dedupe_repeated_lines(clean_stdout)
    clean_stderr, dedup_stderr_chars = dedupe_repeated_lines(clean_stderr)
    profile_stdout = compact_profiled_output(command, clean_stdout, budget=budget, exit_code=exit_code)
    profile_stderr = compact_profiled_output(command, clean_stderr, budget=budget, exit_code=exit_code)
    semantic_stdout = compact_bash_stream(profile_stdout.text, budget=budget)
    semantic_stderr = compact_bash_stream(profile_stderr.text, budget=budget)
    clean_stdout = semantic_stdout.text
    clean_stderr = semantic_stderr.text

    lines_omitted = (
        profile_stdout.lines_omitted
        + profile_stderr.lines_omitted
        + semantic_stdout.lines_omitted
        + semantic_stderr.lines_omitted
    )
    chars_omitted = (
        terminal_chars_stripped
        + dedup_stdout_chars
        + dedup_stderr_chars
        + profile_stdout.chars_saved
        + profile_stderr.chars_saved
        + semantic_stdout.chars_saved
        + semantic_stderr.chars_saved
    )
    lossy = profile_stdout.lossy or profile_stderr.lossy or semantic_stdout.lossy or semantic_stderr.lossy
    stderr_folded = False

    if is_test_only_command(command):
        stdout_compact = extract_test_output(clean_stdout, max_chars=budget)
        changed = stdout_compact != clean_stdout
        stdout_omitted = max(0, len(clean_stdout.splitlines()) - len(stdout_compact.splitlines()))
        stdout_chars = max(0, len(clean_stdout) - len(stdout_compact))
        lossy = lossy or changed
    elif (suppressed := suppress_success_summary(command, clean_stdout, clean_stderr, exit_code)) is not None:
        src_lines = len(clean_stdout.splitlines()) + len(clean_stderr.splitlines())
        stdout_compact = f"{suppressed}\n... ({src_lines} output lines suppressed on success) ..."
        stdout_omitted = len(clean_stdout.splitlines())
        stdout_chars = max(0, len(clean_stdout) - len(stdout_compact))
        stderr_folded = True
        lossy = True
    elif line_elision_bypass and len(clean_stdout) <= budget:
        stdout_compact = "\n".join(clean_stdout.splitlines())
        stdout_omitted = 0
        stdout_chars = 0
    elif len(clean_stdout) <= budget and len(clean_stdout.splitlines()) <= head + tail:
        # Fits as-is: no anomaly-windowing on output that needs no compaction
        # at all -- chopping a small failing result around its 'FAILED' line
        # deletes the surrounding context the model actually needs.
        stdout_compact = clean_stdout
        stdout_omitted = 0
        stdout_chars = 0
    else:
        anomaly = extract_anomaly_windows(clean_stdout, budget)
        if anomaly is not None:
            stdout_compact = anomaly
            stdout_omitted = max(0, len(clean_stdout.splitlines()) - len(anomaly.splitlines()))
            stdout_chars = max(0, len(clean_stdout) - len(anomaly))
            lossy = lossy or anomaly != clean_stdout
        else:
            stdout_compact, stdout_omitted, stdout_chars = head_tail_lines(clean_stdout.splitlines(), head, tail)
            lossy = lossy or stdout_omitted > 0
            capped = cap_chars(stdout_compact, budget)
            if capped != stdout_compact:
                stdout_chars += len(stdout_compact) - len(capped)
                stdout_compact = capped
                lossy = True

    if stderr_folded:
        stderr_compact = ""
        stderr_omitted = len(clean_stderr.splitlines())
        stderr_chars = len(clean_stderr)
    elif line_elision_bypass and len(clean_stderr) <= budget:
        stderr_compact = "\n".join(clean_stderr.splitlines())
        stderr_omitted = 0
        stderr_chars = 0
    elif len(clean_stderr) <= budget and len(clean_stderr.splitlines()) <= 200:
        stderr_compact = clean_stderr
        stderr_omitted = 0
        stderr_chars = 0
    else:
        stderr_anomaly = extract_anomaly_windows(clean_stderr, budget)
        if stderr_anomaly is not None:
            stderr_compact = stderr_anomaly
            stderr_omitted = max(0, len(clean_stderr.splitlines()) - len(stderr_anomaly.splitlines()))
            stderr_chars = max(0, len(clean_stderr) - len(stderr_anomaly))
            lossy = lossy or stderr_anomaly != clean_stderr
        else:
            stderr_compact, stderr_omitted, stderr_chars = head_tail_lines(clean_stderr.splitlines(), 100, 100)
            lossy = lossy or stderr_omitted > 0
            capped_stderr = cap_chars(stderr_compact, budget)
            if capped_stderr != stderr_compact:
                stderr_chars += len(stderr_compact) - len(capped_stderr)
                stderr_compact = capped_stderr
                lossy = True

    lines_omitted += stdout_omitted + stderr_omitted
    chars_omitted += stdout_chars + stderr_chars
    spill_hint = ""
    if lossy and spill is not None:
        full_text = full_stdout
        if full_stderr.strip():
            full_text = f"{full_text}\n\n--- stderr ---\n{full_stderr}" if full_text else full_stderr
        kept_text = stdout_compact
        if stderr_compact.strip():
            kept_text = f"{kept_text}\n\n--- stderr ---\n{stderr_compact}" if kept_text else stderr_compact
        spill_hint = spill(full_text, len(kept_text))

    return CompactedOutput(
        stdout=redact_tool_output(stdout_compact),
        stderr=redact_tool_output(stderr_compact),
        truncated=lossy,
        lines_omitted=lines_omitted,
        chars_omitted=chars_omitted,
        spill_hint=spill_hint,
    )
