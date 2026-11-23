"""One-way Bash search upgrade for standalone source discovery.

Pipelines with exact-output consumers and compound shell programs stay Bash.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LITERAL_QUERY = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]*\Z")
_ALT_QUERY = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]*(?:\|[A-Za-z_][A-Za-z0-9_.:/-]*)+\Z")
_GREP_FLAGS = frozenset({"r", "R", "n", "H", "E"})
_RG_FLAGS = frozenset({"n", "H"})
_GIT_GREP_FLAGS = frozenset({"n", "H", "E"})
_FLAGS = {"grep": _GREP_FLAGS, "rg": _RG_FLAGS, "git grep": _GIT_GREP_FLAGS}
_LONG_FLAGS = {"--recursive": "r", "--line-number": "n", "--with-filename": "H", "--extended-regexp": "E"}


def _tokens(command: str) -> list[str] | None:
    if any(char in command for char in ("\n", "\r", "\x00", "`", "$(", "# lc:allow-shell-search")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return None


def _git_root(path: Path) -> Path | None:
    return next((parent for parent in (path, *path.parents) if (parent / ".git").exists()), None)


def bash_code_search_batch_args(
    arguments: Mapping[str, Any], workspace: Path
) -> list[tuple[str, dict[str, Any]]] | None:
    """Batch read-only source lookups separated by ';', with optional literal echo labels.

    Never split an arbitrary shell program: the entire command must consist of
    independently upgradeable lookups, plus optional labels. If any part needs
    exact shell semantics, execute the original command instead.
    """
    command = arguments.get("command")
    if not isinstance(command, str) or "$" in command:
        return None
    tokens = _tokens(command)
    if not tokens or ";" not in tokens:
        return None
    parts: list[list[str]] = [[]]
    for token in tokens:
        if token == ";":
            if not parts[-1]:
                return None
            parts.append([])
        else:
            parts[-1].append(token)
    if not parts[-1] or len(parts) > 8:
        return None
    searches: list[tuple[str, dict[str, Any]]] = []
    label = ""
    initial_cwd = Path(str(arguments.get("cwd") or workspace)).expanduser()
    current_cwd = (initial_cwd if initial_cwd.is_absolute() else workspace / initial_cwd).resolve()
    for part in parts:
        # A leading literal cd affects subsequent commands in the same shell.
        if part[:1] == ["cd"] and len(part) >= 4 and part[2] == "&&":
            if any(char in part[1] for char in "$`*?[]") or part[1] == "-":
                return None
            destination = Path(part[1]).expanduser()
            current_cwd = (destination if destination.is_absolute() else current_cwd / destination).resolve()
            part = part[3:]
            if not part:
                return None
        if part[0] == "echo":
            if label or len(part) < 2 or any(token in {"|", "&&", "&", ">", ">>"} for token in part):
                return None
            label = " ".join(part[1:])
            continue
        candidate = bash_code_search_args(
            {**arguments, "command": shlex.join(part), "cwd": str(current_cwd)}, workspace
        )
        if candidate is None:
            return None
        searches.append((label, candidate))
        label = ""
    return searches if len(searches) >= 2 and not label else None


_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".cc",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".java",
        ".kt",
        ".swift",
        ".sql",
        ".proto",
        ".sh",
        ".toml",
        ".yml",
        ".yaml",
        ".json",
        ".md",
    }
)
_SOURCE_DIRS = frozenset({"src", "backend", "frontend", "client", "server", "packages", "tests", "docs"})
_READ_ONLY_COMMANDS = frozenset(
    {"cd", "grep", "rg", "git", "head", "tail", "echo", "printf", "sed", "cat", "ls", "pwd"}
)
_READ_ONLY_GIT = frozenset({"grep", "status", "diff", "log", "show", "ls-files", "rev-parse"})
_EXACT_GREP_FLAGS = frozenset(
    {"-q", "-c", "-l", "-L", "-v", "--quiet", "--count", "--files-with-matches", "--invert-match"}
)


def bash_source_search_redirect(arguments: Mapping[str, Any], workspace: Path) -> bool:
    """Identify read-only compound *source discovery* that Claude should split.

    This is a deliberately narrow policy guard, not a shell parser: if any
    command, operand or separator is uncertain, preserve Bash unchanged.
    Commands with mutations, tests, dynamic shell expansion, log filtering or
    output consumers must never be blocked or partially executed.
    """
    command = arguments.get("command")
    if not isinstance(command, str) or "$" in command or "# lc:allow-shell-search" in command:
        return False
    if not re.search(r"(?<![\w-])(?:grep|rg)(?![\w-])", command):
        return False
    if arguments.get("bg") or arguments.get("background") or arguments.get("interactive"):
        return False
    if arguments.get("id") is not None or arguments.get("action") not in (None, "", "run"):
        return False
    tokens = _tokens(command)
    if not tokens or len(tokens) > 160:
        return False
    if any(token in {"||", "&", ">", ">>", "<", "<<", "(", ")"} for token in tokens):
        return False
    # The only conditional operator allowed is 'cd PATH && <lookup>'.
    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in {";", "&&", "|"}:
            if not segments[-1]:
                return False
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return False
    cwd_value = Path(str(arguments.get("cwd") or workspace)).expanduser()
    cwd = (cwd_value if cwd_value.is_absolute() else workspace / cwd_value).resolve()
    found_source = False
    for number, part in enumerate(segments):
        verb = part[0]
        if verb not in _READ_ONLY_COMMANDS:
            return False
        if verb == "git" and (len(part) < 2 or part[1] not in _READ_ONLY_GIT):
            return False
        if verb == "sed" and (len(part) != 4 or part[1] != "-n" or not re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", part[2])):
            return False
        if verb == "cd":
            if len(part) != 2 or any(char in part[1] for char in "*?[]`"):
                return False
            path = Path(part[1]).expanduser()
            cwd = (path if path.is_absolute() else cwd / path).resolve()
            if not cwd.is_dir():
                return False
        if number < len(separators) and separators[number] == "&&" and verb != "cd":
            return False
        if number > 0 and separators[number - 1] == "|" and verb in {"grep", "rg"}:
            # grep on another command's stdout is an output filter.
            return False
        is_search = verb in {"grep", "rg"} or part[:2] == ["git", "grep"]
        if not is_search:
            continue
        if any(
            flag in _EXACT_GREP_FLAGS
            or (flag.startswith("-") and not flag.startswith("--") and any(x in flag[1:] for x in "qclLv"))
            for flag in part[1:]
        ):
            return False
        if number < len(separators) and separators[number] == "|":
            if number + 1 >= len(segments) or segments[number + 1][0] != "head":
                return False
        if _git_root(cwd) is None:
            return False
        # Require a real, Git-scoped code/document path. Search over arbitrary
        # command output, temporary logs and generated artifacts stays Bash.
        operands = [token for token in part[1:] if not token.startswith("-") and token != "--"]
        candidate_paths = [] if part[:2] == ["git", "grep"] else operands[1:]
        if part[:2] == ["git", "grep"]:
            candidate_paths = operands[2:] if len(operands) >= 3 else ["."]
        source_path = False
        for name in candidate_paths:
            if any(char in name for char in "*?[]$`"):
                continue
            path = Path(name).expanduser()
            path = (path if path.is_absolute() else cwd / path).resolve()
            if not path.exists() or _git_root(path) != _git_root(cwd):
                continue
            if path.is_file():
                source_path = path.suffix in _SOURCE_EXTENSIONS or path.name in {"Makefile", "Dockerfile"}
            elif path.is_dir():
                source_path = path.name in _SOURCE_DIRS or name == "."
            if source_path:
                break
        if not source_path:
            return False
        found_source = True
    return found_source


def bash_code_search_args(arguments: Mapping[str, Any], workspace: Path) -> dict[str, Any] | None:
    """Return code_search args, or None to execute the original shell exactly.

    This is intentionally NOT invoked when handling code_search itself. If the
    index must fall back to grep, that grep runs directly and cannot upgrade back.
    """
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if arguments.get("bg") or arguments.get("background") or arguments.get("interactive"):
        return None
    if arguments.get("id") is not None or arguments.get("action") not in (None, "", "run"):
        return None
    tokens = _tokens(command)
    if not tokens:
        return None

    raw_cwd = arguments.get("cwd") or str(workspace)
    if not isinstance(raw_cwd, str) or any(ch in raw_cwd for ch in "$`*?[]"):
        return None
    cwd = Path(raw_cwd).expanduser()
    cwd = (cwd if cwd.is_absolute() else workspace / cwd).resolve()
    if len(tokens) >= 5 and tokens[:1] == ["cd"] and tokens[2] == "&&":
        if not tokens[1] or any(ch in tokens[1] for ch in "$`*?[]") or tokens[1] == "-":
            return None
        destination = Path(tokens[1]).expanduser()
        cwd = (destination if destination.is_absolute() else cwd / destination).resolve()
        tokens = tokens[3:]
    # Do not redirect external/unregistered repositories to the wrong index.
    if workspace != cwd and workspace not in cwd.parents:
        return None
    if not cwd.is_dir() or _git_root(cwd) is None or not tokens:
        return None
    # A final 'head -N' is just an output cap; code_search is bounded already.
    # Other pipes (wc, cut, grep, ...) consume exact shell output: do not rewrite.
    head_only = False
    if "|" in tokens:
        if tokens.count("|") != 1:
            return None
        split = tokens.index("|")
        tail = tokens[split + 1 :]
        head_only = bool(
            (len(tail) == 2 and tail[0] == "head" and re.fullmatch(r"-[0-9]+", tail[1]))
            or (len(tail) == 3 and tail[:2] == ["head", "-n"] and tail[2].isdigit())
        )
        if not head_only:
            return None
        tokens = tokens[:split]
    if any(token in {";", "&&", "||", "&", ">", "<", ">>", "<<", "(", ")"} for token in tokens):
        return None
    # Git's grep is also source discovery, but only its plain read-only form.
    # Do not rewrite other git subcommands or grep operations on generated output.
    tool = "git grep" if tokens[:2] == ["git", "grep"] else tokens[0]
    if tool not in _FLAGS:
        return None
    flags = set()
    operands: list[str] = []
    options_done = False
    for token in tokens[2:] if tool == "git grep" else tokens[1:]:
        if token == "--" and not options_done:
            options_done = True
            continue
        if token.startswith("-") and not options_done:
            if token in _LONG_FLAGS and _LONG_FLAGS[token] in _FLAGS[tool]:
                flags.add(_LONG_FLAGS[token])
            elif len(token) > 1 and not token.startswith("--") and set(token[1:]) <= _FLAGS[tool]:
                flags.update(token[1:])
            else:
                return None
        else:
            operands.append(token)
    if not 1 <= len(operands) <= 5:
        return None
    query = operands[0]
    if not _LITERAL_QUERY.fullmatch(query):
        # Only simple literal OR expressions, never arbitrary regex syntax.
        expression = query
        if tool in {"grep", "git grep"} and "E" not in flags:
            if r"\|" not in expression:
                return None
            expression = expression.replace(r"\|", "|")
        if not _ALT_QUERY.fullmatch(expression):
            return None
        query = " ".join(expression.split("|"))
    paths = operands[1:] or ["."]
    resolved: list[Path] = []
    for path in paths:
        if any(ch in path for ch in "$`*?[]") or path.startswith("-"):
            return None
        target = Path(path).expanduser()
        target = (target if target.is_absolute() else cwd / target).resolve()
        if not target.exists() or _git_root(target) != _git_root(cwd):
            return None
        if target.is_dir() and tool == "grep" and not flags.intersection({"r", "R"}):
            return None
        # A standalone file search has no shell output consumer, so return
        # indexed source with its path and line evidence just like a directory
        # search. Counts, redirects and shell pipelines were rejected above.
        resolved.append(target)
    # Absolute scopes route to the correct checkout even with cd prefixes.
    return {"query": query, "paths": str(resolved[0]) if len(resolved) == 1 else [str(p) for p in resolved]}
