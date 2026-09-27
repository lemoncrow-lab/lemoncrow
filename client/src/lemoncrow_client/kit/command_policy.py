"""What the bash tool does with a command before it runs: allow, rewrite or block.

One policy for the thin client and the main package. It blocks the
destructive git operations (``git reset --hard``, ``git clean -fd``) however
they are wrapped -- chained, substituted, behind ``sudo``/``timeout``/``env``,
inside ``bash -c``, ``eval`` or an on-disk script -- and shell writes outside
the allowed write roots. It rewrites a few read-only commands to cheaper
equivalents: ``cat``/``head``/``tail``/``wc`` on literal files run in-process
(:func:`execute_inline_op`), and ``od <big file> | tail`` seeks. Other rewrite
targets (``read``, ``read_range``, ``grep``, ``search``, ``find_glob``,
``web_fetch``) name a tool the caller serves or declines; a caller that
cannot serve one runs the command as written.

Standard library only, no side effects beyond reading the files a command
names (and a bounded prefix of a script it runs).
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "INLINE_OPS",
    "CommandPolicyDecision",
    "PolicyFallback",
    "classify_command",
    "execute_inline_op",
]

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


@dataclass(frozen=True)
class CommandPolicyDecision:
    category: str
    action: str
    reason: str = ""
    rewrite_target: str | None = None
    rewrite_payload: dict[str, Any] | None = None


#: Called with ``(tokens, command)`` when no rule claimed a single command.
PolicyFallback = Callable[[list[str], str], CommandPolicyDecision | None]

#: Rewrite targets :func:`execute_inline_op` serves in-process.
INLINE_OPS = frozenset({"cat", "head", "tail", "wc"})


_LITERAL_UNSAFE_CHARS = "$`\\*?["


def _literal_operand(tok: str) -> bool:
    """True when *tok* is a plain literal path operand -- something the shell
    would pass through unchanged. A variable ($VAR), tilde or glob operand is
    expanded by a real shell but would be taken literally by a rewrite, and
    ``-`` means stdin; any of those must fall back to real execution.
    """
    if not tok or tok.startswith(("~", "-")):
        return False
    return not any(ch in tok for ch in _LITERAL_UNSAFE_CHARS)


def _rewrite_cat(tokens: list[str]) -> CommandPolicyDecision:
    """Rewrite literal-file cat, including multi-file and numbering forms."""
    files: list[str] = []
    number_mode = ""
    seen_double_dash = False
    for tok in tokens[1:]:
        if tok == "--" and not seen_double_dash:
            seen_double_dash = True
            continue
        if tok.startswith("-") and not seen_double_dash:
            if tok in {"-n", "--number"}:
                number_mode = "all"
                continue
            if tok in {"-b", "--number-nonblank"}:
                number_mode = "nonblank"
                continue
            return CommandPolicyDecision(category="file-read", action="allow")
        files.append(tok)
    if not files or not all(_literal_operand(file) for file in files):
        return CommandPolicyDecision(category="file-read", action="allow")
    if len(files) == 1 and not number_mode:
        return CommandPolicyDecision(
            category="file-read",
            action="rewrite",
            reason="LemonCrow read for file content access",
            rewrite_target="read",
            rewrite_payload={"file_path": files[0]},
        )
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        reason="LemonCrow inline cat for literal file concatenation",
        rewrite_target="cat",
        rewrite_payload={"files": files, "number_mode": number_mode},
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
    if len(files) != 1 or not _literal_operand(files[0]):
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
    if len(files) != 1 or not _literal_operand(files[0]):
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
    if len(files) != 1 or not _literal_operand(files[0]):
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

    Covers ``cat``, ``head``, ``tail``, and ``wc``. No subprocess is spawned; latency
    is O(microseconds) rather than O(30-50 ms) for fork+exec of bash+head.
    Called from both ``run_command`` and the MCP adapter so they share the same
    implementation.
    """
    if rewrite_target == "cat":
        files = [str(file) for file in payload.get("files") or []]
        number_mode = str(payload.get("number_mode") or "")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0
        line_number = 1
        for file_arg in files:
            path = Path(file_arg)
            if not path.is_absolute() and cwd:
                path = Path(cwd) / path
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                stderr_parts.append(f"cat: {file_arg}: No such file or directory\n")
                exit_code = 1
                continue
            except PermissionError:
                stderr_parts.append(f"cat: {file_arg}: Permission denied\n")
                exit_code = 1
                continue
            except OSError as exc:
                stderr_parts.append(f"cat: {file_arg}: {exc}\n")
                exit_code = 1
                continue
            if number_mode:
                numbered: list[str] = []
                for line in text.splitlines(keepends=True):
                    should_number = number_mode == "all" or bool(line.rstrip("\r\n"))
                    if should_number:
                        numbered.append(f"{line_number:6}\t{line}")
                        line_number += 1
                    else:
                        numbered.append(line)
                text = "".join(numbered)
            stdout_parts.append(text)
        return "".join(stdout_parts), "".join(stderr_parts), exit_code

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
        "-q",
        "--quiet",
        "--silent",
        "-P",
        "--perl-regexp",
        "-z",
        "--null-data",
        "-Z",
        "-p",  # rg --replace short (conflicts with safe ignore above, but -p is rare)
        "--replace",
    }
)


def _rewrite_search(tokens: list[str], command_name: str, cwd: Path | None = None) -> CommandPolicyDecision:
    # A pipe consumer downstream (`grep ... | wc -l`, `| cut -d: -f1`) parses
    # REAL grep's output format; the internal grep tool renders differently
    # (grouped/ranked), so feeding its output through the pipe would compute
    # wrong results. Piped searches run verbatim. Checked per-token substring
    # so a glued pipe (`f|wc`) is caught too; a `|` inside a quoted regex
    # false-positives, which only skips the optimization.
    if any("|" in tok for tok in tokens[1:]):
        return CommandPolicyDecision(category="search", action="allow")
    grep_tokens = tokens

    ignore_case = False
    file_type: str | None = None
    lines_after = 0
    lines_before = 0
    globs: list[str] = []
    list_files_only = False
    count_only = False
    fixed_strings = False
    word_regexp = False
    line_regexp = False
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
            if command_name == "grep" and flag_stem in {"-L", "--files-without-match"}:
                # grep -L = files WITHOUT match (rg's -L is --follow); can't replicate.
                return CommandPolicyDecision(category="search", action="allow")
            if tok in {"-c", "--count"}:
                count_only = True
            elif tok in {"-F", "--fixed-strings"}:
                fixed_strings = True
            elif tok in {"-w", "--word-regexp"}:
                word_regexp = True
            elif tok in {"-x", "--line-regexp"}:
                line_regexp = True
            elif tok in {"-i", "--ignore-case"}:
                ignore_case = True
            # Safe structural/formatting flags — skip quietly.
            elif tok in _GREP_SAFE_IGNORE_FLAGS or flag_stem in _GREP_SAFE_IGNORE_FLAGS:
                i += 1
                continue
            # --type=python or --type python or -t python
            elif tok.startswith("--type="):
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
            # Bundled short flags (-rn, -rnicFw …): expand char-by-char.
            # Any fallback char → fall back; any unknown char → fall back;
            # all safe → continue.  Value-in-flag forms (-A90) are already
            # handled above and never reach this branch.
            elif not tok.startswith("--") and len(tok) > 2:
                for ch in tok[1:]:
                    single = f"-{ch}"
                    if single in _GREP_FALLBACK_FLAGS:
                        return CommandPolicyDecision(category="search", action="allow")
                    if command_name == "grep" and single == "-L":
                        # grep -L = files WITHOUT match; can't replicate.
                        return CommandPolicyDecision(category="search", action="allow")
                    if ch == "i":
                        ignore_case = True
                    elif ch == "c":
                        count_only = True
                    elif ch == "F":
                        fixed_strings = True
                    elif ch == "w":
                        word_regexp = True
                    elif ch == "x":
                        line_regexp = True
                    elif single not in _GREP_SAFE_IGNORE_FLAGS:
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
    if len(cleaned) > 2:
        # More than pattern + one path: real grep searches every path given;
        # the internal rewrite takes exactly one. Fall back to real grep.
        return CommandPolicyDecision(category="search", action="allow")

    pattern = cleaned[0]
    # GNU grep BRE treats \| as alternation (extension); rg uses Rust regex
    # where \| is a literal backslash+pipe.  Convert so patterns like
    # "foo\|bar" work as expected via the rg backend.
    if command_name == "grep" and not fixed_strings and r"\|" in pattern:
        pattern = pattern.replace(r"\|", "|")
    if fixed_strings:
        pattern = re.escape(pattern)
    if word_regexp:
        pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
    if line_regexp:
        pattern = rf"^(?:{pattern})$"
    path = cleaned[1] if len(cleaned) > 1 else "."
    if len(cleaned) > 1 and not _literal_operand(path):
        # $VAR/~/glob path operands are expanded by a real shell; the rewrite
        # would take them literally and search the wrong place.
        return CommandPolicyDecision(category="search", action="allow")
    # Single-file targets: fall through to shell grep/rg.  The Python rewrite
    # adds value only for directory-wide searches (ranking, context, file caps).
    # For a specific file, real grep is faster, handles pipes/redirections
    # natively, and avoids any Python overhead or GIL contention.
    resolved_path = Path(path)
    if cwd is not None and not resolved_path.is_absolute():
        resolved_path = cwd / resolved_path
    if path != "." and resolved_path.is_file():
        return CommandPolicyDecision(category="search", action="allow")
    if (
        command_name == "rg"
        and not ignore_case
        and file_type is None
        and not globs
        and not list_files_only
        and not count_only
        and not fixed_strings
        and not word_regexp
        and not line_regexp
        and lines_after == 0
        and lines_before == 0
        and len(cleaned) <= 2
        and not _SEARCH_REGEX_METACHARS.search(pattern)
    ):
        return CommandPolicyDecision(
            category="search",
            action="rewrite",
            reason="LemonCrow search for search-first grounding",
            rewrite_target="search",
            rewrite_payload={"query": pattern, "path": path},
        )
    output_mode = (
        "file_paths_with_match_count"
        if count_only
        else "file_paths_only" if list_files_only else "file_paths_with_content"
    )
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
    return CommandPolicyDecision(
        category="search",
        action="rewrite",
        reason=f"LemonCrow grep for {command_name} pattern search",
        rewrite_target="grep",
        rewrite_payload=payload,
    )


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
    ``LEMONCROW_ADDITIONAL_DIRS``). A relative target resolves against the first
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


def _inline_shell_payload(tokens: list[str]) -> str | None:
    """The command string of ``bash -c '...'`` (first non-option arg after a
    ``-c`` cluster), or ``None`` for stdin (``-s``) / no inline payload.

    Used to re-tokenize and block-check the payload so the remaining
    destructive-git guards can't be laundered through an inline shell.
    """
    saw_c = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok == "-o":
            i += 2  # -o consumes its option value
            continue
        if tok.startswith("-"):
            if not tok.startswith("--") and "c" in tok[1:]:
                saw_c = True
            i += 1
            continue
        break
    if not saw_c or i >= len(tokens):
        return None
    return tokens[i]


_SCRIPT_SCAN_MAX_BYTES = 64 * 1024


def _script_file_target(tokens: list[str], *, cwd: Path | None) -> Path | None:
    """Resolved script path for ``bash <existing script> [args...]``, else None.

    ``bash -c '...'`` (inline) and ``bash -s`` (stdin) stay blocked: their
    command text is opaque to the per-segment blocklist. A script that exists
    on disk is an auditable on-disk artifact — the same risk class as
    ``python file.py`` or ``make``, which the policy already allows — and its
    contents still get the blocklist scan (_scan_script_for_blocked). A
    missing path yields None so the caller blocks (catches ``bash
    <(curl ...)`` styles and typos).
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok.startswith("-"):
            if not tok.startswith("--") and _SHELL_INLINE_SHORT_RE.match(tok):
                return None  # -c / -s (possibly bundled): inline or stdin code
            if tok == "-o":
                i += 2  # -o consumes its option value
                continue
            i += 1
            continue
        break
    if i >= len(tokens):
        return None  # bare `bash`: interactive / stdin
    script = Path(tokens[i])
    if not script.is_absolute():
        if cwd is None:
            return None
        script = cwd / script
    try:
        return script if script.is_file() else None
    except OSError:
        return None


def _scan_script_for_blocked(script: Path, *, cwd: Path | None, visited: set[Path]) -> CommandPolicyDecision | None:
    """Block-check an on-disk script's contents before letting ``bash script`` run.

    Without this, the blocklist is bypassed by writing the dangerous command
    into a file first — ``bash cleanup.sh`` never had its contents inspected.
    Reads a bounded prefix (_SCRIPT_SCAN_MAX_BYTES); this is a static gate,
    not an interpreter. Fail-open on an unreadable file: pre-scan behavior let
    bash surface its own runtime error, and a file this process cannot read
    would fail the same way under bash. ``visited`` breaks the cycle when
    scripts invoke each other (a.sh -> b.sh -> a.sh).
    """
    try:
        resolved = script.resolve()
    except OSError:
        return None
    if resolved in visited:
        return None
    visited.add(resolved)
    try:
        with open(resolved, encoding="utf-8", errors="replace") as handle:
            text = handle.read(_SCRIPT_SCAN_MAX_BYTES)
    except OSError:
        return None  # unreadable: same as pre-scan behavior — bash reports it at runtime
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue  # shebang / full-line comment
        for segment in _split_command_segments(line):
            decision = _block_check_segment(segment, cwd=cwd, visited=visited)
            if decision is not None:
                return CommandPolicyDecision(
                    category=decision.category,
                    action="block",
                    reason=f"blocked command inside script {script}: {decision.reason}",
                )
    return None


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
    tokens: list[str],
    *,
    cwd: Path | None = None,
    visited: set[Path] | None = None,
) -> CommandPolicyDecision | None:
    """Return a block decision if *tokens* (one segment) is dangerous, else None.

    Only the destructive-git guards remain (``git reset --hard`` / ``git clean
    -fd``). The former inline-shell (``bash -c``) and ``rm -rf`` blocks were
    removed: benchmark transcripts showed each block costing 1-5 recovery
    turns per trial while protecting a disposable working tree, and every
    inline-shell payload is still block-checked below so the git guards can't
    be laundered through ``bash -c 'git reset --hard'``.
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
        return _block_check_segment(tokens[1:], cwd=cwd, visited=visited)
    # ``eval``/``exec <words>``: the remaining words run as a fresh command line,
    # so re-tokenize and block-check them (catches ``eval \"git reset --hard\"``).
    if head in _EVAL_WRAPPERS and len(tokens) > 1:
        for inner in _split_command_segments(" ".join(tokens[1:])):
            decision = _block_check_segment(inner, cwd=cwd, visited=visited)
            if decision is not None:
                return decision
        return None
    if head in _SHELL_INTERPRETERS:
        if _is_noexec_shell(tokens):
            return None  # `bash -n` / `-o noexec`: parse-only, runs nothing
        script = _script_file_target(tokens, cwd=cwd)
        if script is not None:
            # `bash existing-script.sh`: on-disk artifact, like `python file.py`
            # — but its contents get the same blocklist scan the command line
            # gets, so a dangerous command can't be laundered through a file.
            return _scan_script_for_blocked(
                script,
                cwd=cwd,
                visited=visited if visited is not None else set(),
            )
        # Inline (`bash -c '...'`) and stdin (`-s`) shells run; the -c payload
        # is re-tokenized and block-checked so the remaining git guards hold.
        payload = _inline_shell_payload(tokens)
        if payload:
            for inner in _split_command_segments(payload):
                decision = _block_check_segment(inner, cwd=cwd, visited=visited)
                if decision is not None:
                    return decision
        return None
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
_FETCH_URL_RE = re.compile(r"https?://[^\s'\"|>;)]+", re.IGNORECASE)
_FETCH_SETUP_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|pip[0-9]*|python[0-9.]*|tar|unzip|gunzip|apt|apt-get|brew|npm|node|tee)\b"
    r"|\s-[oO]\b|\s--output\b|>\s*\S"
    r"|&&\s*(?:tar|unzip|pip|sh|bash|make|python|\./)",
    re.IGNORECASE,
)
# curl/wget flags that carry request semantics — headers, method, body/data,
# auth, forms, uploads, cookies — which the plain-URL web_fetch rewrite would
# silently drop. Their presence disables the rewrite; the command runs as-is
# (a false positive here only means no rewrite, never a block).
_FETCH_REQUEST_FLAGS_RE = re.compile(
    r"(?:^|\s)(?:"
    r"--(?:header|request|method|data[-a-z]*|json|user|form(?:-string)?|upload-file"
    r"|cookie(?:-jar)?|user-agent|referer|post-data|post-file|body-data|body-file|head)(?:=|\s|$)"
    r"|-[A-Za-z]*[HXduFTbIA][A-Za-z]*(?=\s|$)"
    r")"
)
# A find -name pattern the internal glob engine reproduces exactly: a basename
# glob with no path separator and no shell-expansion/quoting characters.
_FIND_PATTERN_RE = re.compile(r"^[A-Za-z0-9._*?\[\]-]+$")
# sed line-print expression: A[,B]p and nothing else -- scripts, regex
# addresses, multiple expressions and extra flags all run for real.
_SED_EXPR_RE = re.compile(r"^(\d+)(?:,(\d+))?p$")
# A rewrite replaces the ENTIRE command with an internal-tool equivalent, so it
# is only sound when the command IS that single invocation. Chaining (&&, ||,
# ;, &), redirection (>, <), substitution (`...` or $(...)) or a newline means
# a rewrite would silently drop every other part -- e.g.
# `sed -i ... && sed -n ...` must NOT collapse into a read of the (unedited)
# file. A *quoted* operator false-positives here, which only skips the
# optimization: the command then runs verbatim, never wrongly. Bare `|` stays
# allowed -- _rewrite_pipeline and _rewrite_search handle pipe tails explicitly.
_REWRITE_UNSAFE_RE = re.compile(r"[;&<>`\n]|\$\(")


def _rewrite_sed_read(tokens: list[str]) -> CommandPolicyDecision | None:
    """Recognize quiet single-expression numeric line reads across sed spellings."""
    quiet = False
    expressions: list[str] = []
    operands: list[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"-n", "--quiet", "--silent"}:
            quiet = True
        elif tok in {"-e", "--expression"}:
            if i + 1 >= len(tokens):
                return None
            i += 1
            expressions.append(tokens[i])
        elif tok.startswith("--expression="):
            expressions.append(tok.split("=", 1)[1])
        elif tok in {"-ne", "-en"}:
            quiet = True
            if i + 1 >= len(tokens):
                return None
            i += 1
            expressions.append(tokens[i])
        elif tok.startswith("-"):
            return None
        else:
            operands.append(tok)
        i += 1
    if not quiet:
        return None
    if not expressions and len(operands) == 2:
        expressions.append(operands.pop(0))
    if len(expressions) != 1 or len(operands) != 1:
        return None
    expression, file_arg = expressions[0], operands[0]
    match = _SED_EXPR_RE.match(expression)
    if match is None or not _literal_operand(file_arg) or ":" in file_arg:
        return None
    start = match.group(1)
    end = match.group(2) or start
    return CommandPolicyDecision(
        category="sed-read",
        action="rewrite",
        rewrite_target="read_range",
        rewrite_payload={"spec": f"{file_arg}:L{start}-L{end}"},
    )


def _redirect_known_bad(command: str) -> CommandPolicyDecision | None:
    """Rewrite known-bad read-only calls to the right tool (executed inline). Never blocks.

    Only called for a single unchained, unpiped command (``classify_command``
    gates on ``_REWRITE_UNSAFE_RE`` and ``|``). Each rewrite anchors on the
    head token and requires the exact, fully-understood shape -- anything else
    runs verbatim. A skipped rewrite is always safe; a wrong one never is.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    head_tok = tokens[0].lower()
    if head_tok == "curl" and not _FETCH_SETUP_RE.search(command):
        # curl only: its default prints the body to stdout, which web_fetch
        # reproduces. wget's default DOWNLOADS to a file a later command may
        # depend on, so wget always runs for real.
        if _FETCH_REQUEST_FLAGS_RE.search(command):
            # Headers/method/auth/body present: a plain-URL web_fetch rewrite
            # would silently drop them and fetch the wrong thing — run as-is.
            return None
        urls = _FETCH_URL_RE.findall(command)
        if len(urls) == 1 and "$" not in urls[0]:
            # Exactly one literal URL: a second URL or a $VAR would be
            # dropped/taken literally by the rewrite.
            return CommandPolicyDecision(
                category="web-fetch",
                action="rewrite",
                rewrite_target="web_fetch",
                rewrite_payload={"url": urls[0]},
            )
        return None  # no/multiple/variable URL -> just allow
    if head_tok == "find":
        # Exact shape only: find [PATH] -name PATTERN -type f (any predicate
        # order). Everything else diverges from the internal glob listing:
        # -iname (case), -wholename (path match), -maxdepth/-mtime/... (extra
        # filters), -delete/-exec (side effects), no -type f (real find also
        # lists matching DIRECTORIES) -- all of those run for real.
        rest = tokens[1:]
        path = "."
        if rest and not rest[0].startswith("-"):
            path, rest = rest[0], rest[1:]
        valid = len(rest) == 4
        opts: dict[str, str] = {}
        if valid:
            for flag, val in zip(rest[::2], rest[1::2], strict=True):
                if flag in opts:
                    valid = False
                    break
                opts[flag] = val
        if (
            valid
            and set(opts) == {"-name", "-type"}
            and opts["-type"] == "f"
            and (path == "." or _literal_operand(path))
            and _FIND_PATTERN_RE.match(opts["-name"])
        ):
            return CommandPolicyDecision(
                category="find",
                action="rewrite",
                rewrite_target="find_glob",
                rewrite_payload={"glob": opts["-name"], "path": path},
            )
        return None
    if head_tok == "sed":
        return _rewrite_sed_read(tokens)
    return None  # sed -i / other sed / other find / wget / git navigation -> ALLOW


# Pipeline-aware rewrite (tier 1 detect + tier 2 safe rewrite). A streaming hex
# formatter over a large file piped into `tail` forces the formatter to process
# the ENTIRE file -- `tail` can't SIGPIPE-abort it early the way `head` can, so
# `od bigfile | tail -60` hex-formats all N bytes just to show the end. Rewrite
# to an in-place seek (`od -j <offset>`), which preserves od's absolute byte
# addresses. The naive `tail -c N file | od` does NOT: its addresses restart at
# 0. Deliberately narrow -- od only, one seekable regular file, tail-only
# consumer, no geometry-changing od flags; every other shape returns None and
# runs unchanged (no silent rewrite of a command we can't prove equivalent).
_PIPELINE_SEEK_MIN_BYTES = 8 * 1024 * 1024
_OD_BYTES_PER_LINE = 16  # od/hexdump default row width; the line-mode seek assumes it


def _split_top_level_pipeline(command: str) -> list[list[str]] | None:
    """The ``|``-separated stages of a simple single-line pipeline as token
    lists, or None if *command* is anything more complex -- a newline, ``;``,
    ``&&``, ``||``, ``&``, a redirect, a subshell, or command substitution. Used
    only to spot a narrow, safely-rewritable shape; when in doubt, return None
    and leave the command untouched.
    """
    if any(marker in command for marker in ("\n", "`", "$(")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        toks = list(lexer)
    except ValueError:
        return None
    forbidden = {"&&", "||", "&", ";", "(", ")", "<", ">", ">>", "<<", "|&", "<>", "&>"}
    if any(tok in forbidden for tok in toks):
        return None
    if "|" not in toks:
        return None
    stages: list[list[str]] = []
    current: list[str] = []
    for tok in toks:
        if tok == "|":
            if not current:
                return None
            stages.append(current)
            current = []
        else:
            current.append(tok)
    if not current:
        return None
    stages.append(current)
    return stages


def _parse_tail_bound(tokens: list[str]) -> tuple[str | None, int]:
    """``('lines'|'bytes', count)`` for a plain bounded ``tail`` reading stdin,
    or ``(None, 0)`` for any shape whose output can't be reproduced by a byte
    seek (``-f``/follow, suffixed or ``+N`` counts, a file operand, ...).
    """
    mode = "lines"
    count = 10
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"-c", "--bytes", "-n", "--lines"}:
            if i + 1 >= len(tokens):
                return None, 0
            val = tokens[i + 1]
            if not val.isdigit():
                return None, 0
            mode = "bytes" if tok in {"-c", "--bytes"} else "lines"
            count = int(val)
            i += 2
            continue
        if tok.startswith(("--bytes=", "--lines=")):
            val = tok.split("=", 1)[1]
            if not val.isdigit():
                return None, 0
            mode = "bytes" if tok.startswith("--bytes=") else "lines"
            count = int(val)
            i += 1
            continue
        if tok.startswith("-c") and tok[2:].isdigit():
            mode, count, i = "bytes", int(tok[2:]), i + 1
            continue
        if tok.startswith("-n") and tok[2:].isdigit():
            mode, count, i = "lines", int(tok[2:]), i + 1
            continue
        if re.fullmatch(r"-\d+", tok):  # `tail -60` == `tail -n 60`
            mode, count, i = "lines", int(tok[1:]), i + 1
            continue
        # any other flag (-f/--follow, -q, ...) or a file operand: the consumer
        # isn't a plain bounded stdin tail -- don't rewrite.
        return None, 0
    return mode, count


def _rewrite_pipeline(command: str, cwd: str | Path | None) -> CommandPolicyDecision | None:
    stages = _split_top_level_pipeline(command)
    if stages is None or len(stages) != 2:
        return None
    producer, consumer = stages
    if not producer or not consumer:
        return None
    if producer[0].lower() != "od" or consumer[0].lower() != "tail":
        return None
    # Bail on od flags that change row geometry or already seek/limit the read --
    # the offset math and the printed addresses would no longer line up.
    for tok in producer[1:]:
        if tok.startswith(("-w", "--width", "-N", "--read-bytes", "-j", "--skip-bytes", "-S", "--strings")):
            return None
    # Producer operands that exist as regular files (a flag value like `-A`'s
    # `d` or `-t`'s `x1` doesn't name a real file, so statting also tells the
    # file apart from option arguments without parsing od's grammar). od over
    # multiple files concatenates their dumps -- which a single-file seek can't
    # reproduce -- so require exactly one, and it must be large enough that
    # formatting the whole thing is the actual waste we're avoiding.
    file_operands: list[tuple[str, int]] = []
    for tok in producer[1:]:
        if tok.startswith("-") and tok != "-":
            continue
        path = Path(tok)
        if not path.is_absolute() and cwd is not None:
            path = Path(cwd) / tok
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        file_operands.append((tok, size))
    if len(file_operands) != 1:
        return None
    file_tok, size = file_operands[0]
    if size <= _PIPELINE_SEEK_MIN_BYTES:
        return None
    mode, count = _parse_tail_bound(consumer)
    if mode is None or count <= 0:
        return None
    if mode == "bytes":
        offset = size - count
    else:
        # Seek one extra row back so we never return FEWER rows than asked (od
        # also prints a trailing address-only line): a safe superset of `tail -n`.
        offset = size - (count + 1) * _OD_BYTES_PER_LINE
    if offset <= 0:
        return None  # requested tail already spans (nearly) the whole file
    # Rebuild: original od invocation minus the file operand (and any lone `--`),
    # plus our skip and the file at the end behind a fresh `--`.
    rebuilt = ["od"]
    for tok in producer[1:]:
        if tok == file_tok or tok == "--":
            continue
        rebuilt.append(tok)
    rebuilt += ["-j", str(offset), "--", file_tok]
    rewritten = " ".join(shlex.quote(tok) for tok in rebuilt)
    note = (
        f"[LemonCrow: `od … | tail` over a {size // (1024 * 1024)}MB file would hex-format the "
        f"whole file; seeked to byte {offset} instead (od -j; absolute addresses preserved)]"
    )
    return CommandPolicyDecision(
        category="file-read",
        action="rewrite",
        reason=note,
        rewrite_target="pipeline_seek",
        rewrite_payload={"command": rewritten, "note": note},
    )


def classify_command(
    command: str,
    *,
    allowed_write_roots: list[Path] | None = None,
    cwd: str | Path | None = None,
    fallback: PolicyFallback | None = None,
) -> CommandPolicyDecision:
    """Decide whether *command* runs as written, is rewritten, or is blocked.

    *fallback(tokens, command)* gets the last word on a single command no rule
    claimed; the main package plugs its external compactors in there.
    """
    resolved_cwd = Path(cwd).resolve() if cwd else None
    # Block checks run per segment: bash -c executes the whole line, so chaining
    # and command substitution must not slip a dangerous segment past tokens[0].
    for segment in _split_command_segments(command):
        blocked = _block_check_segment(segment, cwd=resolved_cwd)
        if blocked is not None:
            return blocked

    # Shell file-writes (`cat > file`, inline `python -c` open/write) must stay
    # inside the allowed write roots; outside them the edit tool is the right
    # surface. The OS temp dir is additionally allowed for the same reason
    # `rm -rf` is allowed there: scratch files are the agent's own.
    if allowed_write_roots and _is_shell_file_write(command):
        targets = _extract_write_targets(command)
        # Fail-open when no write target can be resolved statically (targets
        # is None: a variable/f-string path or a .write_text receiver, or an
        # empty list). Blocking what we cannot parse would break legitimate
        # $TMPDIR-style writes; the destructive blocklist above still applies,
        # and a genuinely wrong opaque path just fails at runtime.
        if targets and not _file_write_within_allowed(command, [*allowed_write_roots, Path(tempfile.gettempdir())]):
            return CommandPolicyDecision(
                category="file-write",
                action="block",
                reason=(
                    "shell write outside the allowed write roots blocked "
                    f"(target(s): {', '.join(targets)}) — use the edit tool to "
                    "create or modify files, or target a path inside the workspace"
                ),
            )

    # Rewrites replace the whole command -- only sound for a single simple
    # invocation (see _REWRITE_UNSAFE_RE). When the command chains, redirects,
    # or substitutes, skip every rewrite and run it verbatim.
    rewrite_safe = _REWRITE_UNSAFE_RE.search(command) is None

    bad = _redirect_known_bad(command) if rewrite_safe and "|" not in command else None
    if bad is not None:
        return bad

    # Pipeline-aware rewrite runs before the single-command tokens[0] dispatch
    # (which never sees into `producer | consumer`). Only fires for the narrow
    # `od <bigfile> | tail` shape; returns None -> normal flow -> untouched.
    pipeline_rewrite = _rewrite_pipeline(command, resolved_cwd) if rewrite_safe else None
    if pipeline_rewrite is not None:
        return pipeline_rewrite

    try:
        tokens = shlex.split(command)
    except ValueError:
        return CommandPolicyDecision(category="generic", action="allow")
    if not tokens:
        return CommandPolicyDecision(category="generic", action="allow")

    head = tokens[0].lower()
    if rewrite_safe:
        if head == "cat":
            return _rewrite_cat(tokens)
        if head == "head":
            return _rewrite_head(tokens)
        if head == "tail":
            return _rewrite_tail(tokens)
        if head == "wc":
            return _rewrite_wc(tokens)
        if head in {"rg", "grep"}:
            return _rewrite_search(tokens, head, resolved_cwd)
    if fallback is not None:
        decision = fallback(tokens, command)
        if decision is not None:
            return decision
    return CommandPolicyDecision(category="generic", action="allow")
