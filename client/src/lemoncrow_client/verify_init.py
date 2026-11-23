"""``lc verify init``: write ``.lemoncrow/environment.toml`` from the checks a repository already runs.

Where checks come from, strongest first:

1. the Makefile's targets named like a check (``test``, ``lint``, ``check``, ``typecheck``, ``format-check`` ...);
2. ``package.json`` scripts named like one, run with the package manager the repository uses;
3. the ``run:`` steps of GitHub Actions workflows: one check per command a known check tool runs (pytest, ruff,
   eslint, ``go test``, ``cargo clippy``, ``mvn verify``, ``rake`` ...);
4. the tools ``pyproject.toml`` configures or lists as development dependencies (pytest, ruff, mypy, pyright, black),
   only for a kind of check (test, lint ...) no other source gave: a configured tool may be one nothing runs. With a
   lockfile the tool runs in the project's environment, so it must be one of its dependencies (``uv run --group G``
   when only a group uv does not install by default has it);
5. the build manifest at the root (``go.mod``, ``Cargo.toml``, ``pom.xml``, a Gradle build): its test command, only
   when no other source gave a test.

A check that a stronger source already runs is not added again: a CI step ``pytest`` is left out when the Makefile's
``test`` runs pytest, and a ``check`` target that only runs ``lint`` and ``test`` is left out when both are in. An
entry point that rewrites files (``ruff --fix``, ``black`` without ``--check``, a ``format`` target) is never a check,
since ``lc verify`` runs in the working tree. A linter, formatter or type checker gets ``paths`` for its language's
files and configuration; every other check gets ``.``, so it runs on any change.

A root ``package-lock.json`` or ``uv.lock`` becomes a ``[[dependencies]]`` entry (``npm ci``, ``uv sync --frozen``),
which a Workspaces server installs before the checks run. Any other lockfile is only named in a note: Review
provisions those two and refuses a run whose file declares another.

The standard library has no YAML parser, so workflows are read line by line. A step is skipped when its command
cannot be read safely: it branches or loops, uses another shell, ``continue-on-error`` or a working directory that is
unclear or missing; within a step, a command with an expression or ``$`` variable, a pipe, redirection or subshell is
skipped.

It never rewrites an existing file, with one exception: ``--toolchain IMAGE`` adds or replaces only the
``[toolchain]`` table, shows the change first, and checks that the rest of the definition reads the same.
``--toolchain`` pins IMAGE to the digest its registry serves (:mod:`.image_registry`); without it the file has no
``[toolchain]`` and a comment says a server needs one. ``--print`` writes the result to standard output and nothing
else. Whatever it writes loads with :func:`.environment.parse_environment` first.
"""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, TextIO

from .environment import (
    ENVIRONMENT_FILE,
    Check,
    DefinitionError,
    Dependency,
    Environment,
    Toolchain,
    parse_environment,
)
from .image_registry import RegistryError, parse_reference, resolve_digest

__all__ = ["INIT_USAGE", "Detected", "Detection", "detect", "init_main", "render", "with_toolchain"]

INIT_USAGE: Final[str] = """usage:
  lc verify init [--print]                    write .lemoncrow/environment.toml from the checks this repository
                                              already runs: Makefile targets, package.json scripts, the run steps
                                              of GitHub Actions workflows, the tools pyproject.toml configures and
                                              the test command of go.mod, Cargo.toml, pom.xml or a Gradle build
  lc verify init --toolchain IMAGE [--print]  the same with [toolchain] pinned to IMAGE's digest; when the file
                                              exists, add or replace only its [toolchain], showing the change first

An existing file is otherwise left alone. --print writes the result to standard output and nothing else. IMAGE's
digest comes from its registry, anonymously (public images only): --toolchain python:3.12-slim.
"""

_MAX_SOURCE_BYTES: Final[int] = 1 << 20
_MAX_WORKFLOWS: Final[int] = 64
#: How deep a target's prerequisites and a script's ``npm run`` references are followed.
_MAX_DEPTH: Final[int] = 8
_MAKEFILES: Final[tuple[str, ...]] = ("GNUmakefile", "makefile", "Makefile")
_KIND_ORDER: Final[tuple[str, ...]] = ("format", "lint", "typecheck", "test", "build", "precommit")

#: Makefile targets and package.json scripts that are checks, by name (lowercase, ``:`` and ``_`` read as ``-``).
_ENTRY_KINDS: Final[dict[str, str]] = {
    "test": "test",
    "tests": "test",
    "unit": "test",
    "unittest": "test",
    "unit-test": "test",
    "unit-tests": "test",
    "test-unit": "test",
    "check": "test",
    "lint": "lint",
    "lint-check": "lint",
    "check-lint": "lint",
    "vet": "lint",
    "typecheck": "typecheck",
    "type-check": "typecheck",
    "types": "typecheck",
    "check-types": "typecheck",
    "check-type": "typecheck",
    "types-check": "typecheck",
    "typing": "typecheck",
    "mypy": "typecheck",
    "tsc": "typecheck",
    "format-check": "format",
    "fmt-check": "format",
    "check-format": "format",
    "check-fmt": "format",
    "prettier-check": "format",
    "pre-commit": "precommit",
    "precommit": "precommit",
}

_PYTHON_FILES: Final[tuple[str, ...]] = ("*.py", "*.pyi", "pyproject.toml", "setup.cfg")
_JS_FILES: Final[tuple[str, ...]] = (
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.tsx",
    "*.mts",
    "*.cts",
    "*.vue",
    "*.svelte",
)
_GO_FILES: Final[tuple[str, ...]] = ("*.go", "go.mod", "go.sum")
_RUST_FILES: Final[tuple[str, ...]] = ("*.rs", "Cargo.toml", "Cargo.lock", "clippy.toml", ".clippy.toml")
#: The files a linter, formatter or type checker reads. Any other tool gets ``.``: the whole repository.
_TOOL_PATHS: Final[dict[str, tuple[str, ...]]] = {
    "ruff": (*_PYTHON_FILES, "ruff.toml", ".ruff.toml"),
    "ruff-format": (*_PYTHON_FILES, "ruff.toml", ".ruff.toml"),
    "black": _PYTHON_FILES,
    "isort": (*_PYTHON_FILES, ".isort.cfg"),
    "flake8": (*_PYTHON_FILES, "tox.ini", ".flake8"),
    "pylint": (*_PYTHON_FILES, ".pylintrc", "pylintrc"),
    "mypy": (*_PYTHON_FILES, "mypy.ini", ".mypy.ini"),
    "pyright": (*_PYTHON_FILES, "pyrightconfig.json"),
    "eslint": (*_JS_FILES, "package.json", "eslint.config.*", ".eslintrc*"),
    "tsc": (*_JS_FILES, "package.json", "tsconfig*.json"),
    "go-vet": _GO_FILES,
    "golangci-lint": (*_GO_FILES, ".golangci.*"),
    "staticcheck": _GO_FILES,
    "cargo-clippy": _RUST_FILES,
    "cargo-check": _RUST_FILES,
    "cargo-fmt": (*_RUST_FILES, "rustfmt.toml", ".rustfmt.toml"),
    "shellcheck": ("*.sh", "*.bash", ".shellcheckrc"),
}

_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON: Final[re.Pattern[str]] = re.compile(r"python[0-9.]*|py|\$[({][A-Z_]*PY[A-Z_]*[)}]")
_RUN_WRAPPERS: Final[frozenset[str]] = frozenset({"uv", "poetry", "pdm", "pipenv", "rye"})
#: Options of ``uv run`` and the like that take a value as the next word.
_WRAPPER_VALUES: Final[frozenset[str]] = frozenset(
    {"--with", "--python", "-p", "--extra", "--group", "--package", "--project", "--directory", "--env-file"}
)
_PACKAGE_MANAGER_COMMANDS: Final[frozenset[str]] = frozenset(
    {"add", "cache", "config", "dedupe", "dlx", "exec", "global", "i", "info", "init", "install", "link", "pack"}
    | {"publish", "remove", "set", "unlink", "up", "update", "upgrade", "version", "why", "workspace", "workspaces"}
)
_RUFF_COMMANDS: Final[frozenset[str]] = frozenset(
    {"rule", "config", "linter", "clean", "format", "server", "analyze", "version", "help"}
)
_WATCH: Final[re.Pattern[str]] = re.compile(r"--watch(?:All)?(?!=false)\b")
_SAFE_DIR: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*/?")
_CHANGES_DIR: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s;&|(])(?:cd|pushd|popd)(?:\s|$)")
_LENIENT_SPLIT: Final[re.Pattern[str]] = re.compile(r"&&|\|\||[;|&]")
#: A line that starts a branch, a loop or a function: which commands then run is not known from the lines alone.
_CONTROL: Final[re.Pattern[str]] = re.compile(
    r"(?:if|then|elif|else|fi|case|esac|for|while|until|do|done|select|function)(?:\s|;|$)|[\w.-]+\s*\(\s*\)"
)

_HEADER: Final[str] = (
    "# How changes to this repository are checked, written by `lc verify init` from what the repository\n"
    "# already runs; edit it freely. `lc verify` runs each check whose paths (Git pathspecs) a change\n"
    "# touches, and `lc verify hook install` makes that the pre-push hook. See `lc verify --help`.\n"
)
#: Stands where ``[toolchain]`` goes; ``--toolchain`` replaces exactly these lines.
_NO_TOOLCHAIN: Final[str] = (
    "# No [toolchain]: `lc verify` runs the checks with your own tools. A Workspaces server runs them in\n"
    "# a base image pinned by digest and refuses a task without one. Add it with\n"
    "#   lc verify init --toolchain IMAGE\n"
)
_DEPENDENCIES_NOTE: Final[str] = (
    "# A Workspaces server runs each install before the checks; Review provisions the lockfile itself.\n"
    "# `lc verify` runs neither: it uses what you installed.\n"
)
_TABLE_HEADER: Final[re.Pattern[str]] = re.compile(r"^\s*\[")
_TOOLCHAIN_HEADER: Final[re.Pattern[str]] = re.compile(r"^\s*\[\s*toolchain\s*\]\s*(?:#.*)?$")
_IMAGE_KEY: Final[re.Pattern[str]] = re.compile(r"^(\s*)image\s*=")
#: An ``image = "..."`` line's trailing comment, which stays when the image is replaced.
_IMAGE_COMMENT: Final[re.Pattern[str]] = re.compile(r"""\s*image\s*=\s*(?:"(?:[^"\\]|\\.)*"|'[^']*')(\s*#.*)?""")


@dataclass(frozen=True)
class Detected:
    """One check found in the repository, and where.

    ``covers`` names what running it runs: tools (``pytest``), Makefile targets (``make:test``) and package.json
    scripts (``npm:lint``), so a check another one already runs is left out. ``own`` is its own entry point.
    """

    id: str
    kind: str
    run: str
    paths: tuple[str, ...]
    source: str
    family: str
    covers: frozenset[str] = frozenset()
    own: str = ""


@dataclass
class Detection:
    checks: list[Detected]
    notes: list[str] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# What a shell command runs
# --------------------------------------------------------------------------- #


def _program(word: str) -> str:
    if word in {"$(MAKE)", "${MAKE}"}:
        return "make"
    return word.rsplit("/", 1)[-1]


def _after_options(words: Sequence[str], valued: Iterable[str]) -> list[str]:
    takes_value = set(valued)
    index = 0
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "--":
            return list(words[index + 1 :])
        index += 2 if words[index] in takes_value else 1
    return list(words[index:])


def _unwrap(words: Sequence[str]) -> list[str]:
    """The command under environment assignments and runners (``uv run``, ``npx``, ``python -m`` ...)."""

    rest = list(words)
    for _ in range(6):
        while rest and _ASSIGNMENT.match(rest[0]):
            rest = rest[1:]
        if not rest:
            return []
        head = _program(rest[0])
        if head in _RUN_WRAPPERS and rest[1:2] == ["run"]:
            rest = _after_options(rest[2:], _WRAPPER_VALUES)
        elif head in {"uvx", "npx", "bunx", "pnpx"}:
            rest = _after_options(rest[1:], {"-p", "--package", "--from", "--with"})
        elif head in {"npm", "pnpm", "yarn", "bun"} and rest[1:2] in (["exec"], ["dlx"], ["x"]):
            rest = _after_options(rest[2:], {"-p", "--package"})
        elif head == "bundle" and rest[1:2] == ["exec"]:
            rest = rest[2:]
        elif _PYTHON.fullmatch(head) and rest[1:2] == ["-m"] and len(rest) > 2:
            rest = rest[2:]
        elif head in {"time", "env", "command", "exec", "nice", "xvfb-run"}:
            rest = _after_options(rest[1:], ())
        else:
            return rest
    return rest


def _make_invocation(arguments: Sequence[str]) -> tuple[tuple[str, ...], str] | None:
    directory, targets = "", []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        index += 1
        if item in {"-C", "--directory"}:
            directory = arguments[index] if index < len(arguments) else ""
            index += 1
        elif item.startswith("--directory="):
            directory = item.partition("=")[2]
        elif item in {"-f", "--file", "--makefile"} or item.startswith(("--file=", "--makefile=")):
            return None  # another Makefile: its targets are not this one's
        elif item in {"-j", "--jobs", "-l", "--load-average", "--max-load"}:
            # The count is optional: `make -j -C tests test` runs `test` in tests, not targets `-C` and `tests`.
            if index < len(arguments) and re.fullmatch(r"\d+(?:\.\d+)?", arguments[index]):
                index += 1
        elif item in {"-o", "-W", "-I", "--old-file", "--assume-old", "--new-file", "--assume-new", "--what-if"}:
            index += 1
        elif not item.startswith("-") and "=" not in item:
            targets.append(item)
    if not targets:
        return None
    prefix = f"{directory.strip('/')}/" if directory else ""
    kind = next((_ENTRY_KINDS[_entry_name(t)] for t in targets if _entry_name(t) in _ENTRY_KINDS), "")
    return tuple(f"make:{prefix}{target}" for target in targets), kind


def _script_invocation(head: str, arguments: Sequence[str]) -> tuple[tuple[str, ...], str] | None:
    sub = arguments[0] if arguments else ""
    names: list[str] = []
    if head in {"run-s", "run-p", "npm-run-all"}:
        names = list(arguments)
    elif sub in {"run", "run-script"} and len(arguments) > 1:
        names = [arguments[1]]
    elif sub in {"test", "t", "tst"} and head != "bun":
        names = ["test"]
    elif head == "bun" and sub == "test":
        return ("bun-test",), "test"
    elif head in {"yarn", "pnpm"} and sub and sub not in _PACKAGE_MANAGER_COMMANDS:
        names = [sub]
    if not names:
        return None
    kind = "" if len(names) > 1 else _ENTRY_KINDS.get(_entry_name(names[0]), "")
    return tuple(f"npm:{name}" for name in names), kind


def _invocation(words: Sequence[str]) -> tuple[tuple[str, ...], str] | None:
    """What *words* run: the tools or entry points (first one primary) and the check kind, ``""`` when not a check.

    A command that rewrites files (``--fix``, ``--write``, a formatter without ``--check``) is ``rewrite:<tool>``, so
    an entry point that runs one is never made a check: ``lc verify`` runs in the working tree. ``None`` when the
    command is none of these.
    """

    command = _unwrap(words)
    if not command:
        return None
    head, rest = _program(command[0]), command[1:]
    flags = {word.split("=", 1)[0] for word in rest if word.startswith("-")}
    args = [word for word in rest if not word.startswith("-")]
    sub = args[0] if args else ""

    def tool(name: str, kind: str) -> tuple[tuple[str, ...], str]:
        return (name,), kind

    rewrite = tool(f"rewrite:{head}", "")
    if flags & {"--fix", "--write", "--apply", "--apply-unsafe"} or (head in {"prettier", "gofmt"} and "-w" in flags):
        return rewrite
    if head in {"pytest", "py.test"}:
        return tool("pytest", "test")
    if head in {"tox", "nox"}:
        return tool(head, "test")
    if head == "ruff":
        if sub == "format":
            return tool("ruff-format", "format") if flags & {"--check", "--diff"} else rewrite
        return tool("ruff", "lint") if sub and sub not in _RUFF_COMMANDS else None
    if head in {"black", "isort"}:
        if flags & {"--check", "--check-only", "-c"}:
            return tool(head, "format")
        return None if "--diff" in flags else rewrite
    if head in {"flake8", "pylint", "shellcheck", "staticcheck"}:
        return tool(head, "lint")
    if head == "mypy":
        return tool("mypy", "typecheck")
    if head in {"pyright", "basedpyright"}:
        return tool("pyright", "typecheck")
    if head == "pre-commit":
        return tool("pre-commit", "precommit") if sub == "run" else None
    if head == "eslint":
        return tool("eslint", "lint")
    if head == "prettier":
        return tool("prettier", "format") if flags & {"--check", "-c", "--list-different", "-l"} else None
    if head in {"tsc", "vue-tsc"}:
        return tool("tsc", "typecheck") if "--noEmit" in flags else None
    if head == "svelte-check":
        return tool("svelte-check", "typecheck")
    if head == "biome":
        return tool("biome", "lint") if sub in {"check", "lint", "ci"} else None
    if head in {"jest", "mocha", "ava"}:
        return None if flags & {"--watch", "--watchAll"} else tool(head, "test")
    if head == "vitest":
        return tool("vitest", "test") if sub in {"", "run"} and "--watch" not in flags else None
    if head == "go":
        return {"test": tool("go-test", "test"), "vet": tool("go-vet", "lint"), "fmt": rewrite}.get(sub)
    if head == "golangci-lint":
        return tool("golangci-lint", "lint") if sub == "run" else None
    if head == "cargo":
        sub = next((word for word in args if not word.startswith("+")), "")
        if sub in {"test", "nextest"}:
            return tool("cargo-test", "test")
        if sub == "fmt":
            return tool("cargo-fmt", "format") if "--check" in flags else rewrite
        return {"clippy": tool("cargo-clippy", "lint"), "check": tool("cargo-check", "typecheck")}.get(sub)
    if head in {"mvn", "mvnw"}:
        goals = {word.rsplit(":", 1)[-1] for word in args}
        return tool("maven", "test") if goals & {"test", "verify"} else None
    if head in {"gradle", "gradlew"}:
        tasks = {word.rsplit(":", 1)[-1] for word in args}
        if "spotlessApply" in tasks:
            return rewrite
        if tasks & {"test", "check"}:
            return tool("gradle", "test")
        return tool("gradle", "build") if "build" in tasks else None
    if head == "deno":
        if sub == "fmt":
            return tool("deno-fmt", "format") if "--check" in flags else rewrite
        return {"test": tool("deno-test", "test"), "lint": tool("deno-lint", "lint")}.get(sub)
    if head in {"dotnet", "swift", "mix"}:
        return tool(f"{head}-test", "test") if sub == "test" else None
    if head in {"ctest", "rspec"}:
        return tool(head, "test")
    if head == "rake":  # its default task, or test or spec, is the test suite by Ruby convention
        tasks = {word for word in args if "=" not in word}
        return tool("rake", "test") if tasks <= {"test", "spec", "default"} else None
    if head == "make":
        return _make_invocation(rest)
    if head in {"npm", "pnpm", "yarn", "bun", "run-s", "run-p", "npm-run-all"}:
        return _script_invocation(head, args)
    return None


def _line_keys(line: str) -> set[str]:
    """Everything a line of a recipe or script runs, read leniently: only for leaving out what is already in."""

    keys: set[str] = set()
    for piece in _LENIENT_SPLIT.split(line):
        try:
            words = shlex.split(piece)
        except ValueError:
            words = piece.split()
        found = _invocation(words)
        if found is not None:
            keys.update(found[0])
    return keys


def _segments(line: str) -> list[str] | None:
    """*line* split at its top-level ``&&`` and ``;``; ``None`` when it has anything else at the top level (a pipe,
    ``||``, redirection, a background job, a subshell or group, command substitution) or an unclosed quote."""

    pieces: list[str] = []
    start = index = 0
    quote = ""
    end = len(line)
    while index < end:
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char == "\\":
                index += 1
            elif quote == '"' and (char == "`" or line.startswith("$(", index)):
                return None
        elif char in "'\"":
            quote = char
        elif char == "\\":
            index += 1
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            end = index
            break
        elif line.startswith("&&", index):
            pieces.append(line[start:index])
            index += 2
            start = index
            continue
        elif char == ";":
            pieces.append(line[start:index])
            start = index + 1
        elif char in "|&<>(){}`" or line.startswith("$(", index):
            return None
        index += 1
    if quote:
        return None
    pieces.append(line[start:end])
    return [piece.strip() for piece in pieces if piece.strip()]


def _logical_lines(text: str) -> list[str]:
    """Lines with backslash continuations joined."""

    lines: list[str] = []
    pending: str | None = None
    for raw in text.splitlines():
        piece = raw if pending is None else pending + raw.lstrip()
        if piece.endswith("\\"):
            pending = piece[:-1] + " "
            continue
        lines.append(piece)
        pending = None
    if pending is not None:
        lines.append(pending)
    return lines


def _entry_name(name: str) -> str:
    return name.lower().replace(":", "-").replace("_", "-")


def _paths_for(tool: str, directory: str) -> tuple[str, ...]:
    specs = _TOOL_PATHS.get(tool, (".",))
    if not directory:
        return specs
    return tuple(directory if spec == "." else f"{directory}/{spec}" for spec in specs)


def _scoped(key: str, directory: str) -> str:
    if not directory:
        return key
    family, colon, name = key.partition(":")
    return f"{family}:{directory}/{name}" if colon else f"{directory}/{key}"


def _read(top: Path, path: Path, notes: list[str]) -> str | None:
    where = path.relative_to(top).as_posix()
    try:
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            notes.append(f"{where}: larger than {_MAX_SOURCE_BYTES >> 20} MiB, so it was not read")
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"{where}: could not be read ({exc.__class__.__name__})")
        return None


# --------------------------------------------------------------------------- #
# Makefile targets
# --------------------------------------------------------------------------- #

_RULE: Final[re.Pattern[str]] = re.compile(r"^([^\s:=#][^:=#]*?)\s*(::?)(?!=)(.*)$")
_DEFINE: Final[re.Pattern[str]] = re.compile(r"^(?:override\s+|export\s+)?define\b")


def _make_rules(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """Each explicit target's prerequisites and recipe lines; pattern rules and variable names are left out."""

    rules: dict[str, tuple[list[str], list[str]]] = {}
    current: list[str] = []
    in_define = False
    for line in _logical_lines(text):
        stripped = line.strip()
        if in_define:
            in_define = not stripped.startswith("endef")
            continue
        if line.startswith("\t"):
            for target in current:
                rules[target][1].append(stripped.lstrip("@-+ "))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        current = []
        if _DEFINE.match(stripped):
            in_define = True
            continue
        match = _RULE.match(line)
        if match is None:
            continue
        names, _colon, rest = match.groups()
        prerequisites, _semicolon, inline = rest.partition(";")
        prerequisites = prerequisites.partition("#")[0]  # a comment, such as `## help text`
        targets = names.split()
        if "=" in prerequisites or any("%" in target or "$" in target for target in targets):
            continue  # a target-specific variable, a pattern rule or a computed name
        needed = [name for name in prerequisites.replace("|", " ").split() if "$" not in name]
        for target in targets:
            entry = rules.setdefault(target, ([], []))
            entry[0].extend(needed)
            if inline.strip():
                entry[1].append(inline.strip())
        current = targets
    return rules


def _make_covers(rules: dict[str, tuple[list[str], list[str]]], target: str) -> frozenset[str]:
    covers: set[str] = set()
    visited: set[str] = set()
    pending = [(target, 0)]
    while pending:
        name, depth = pending.pop()
        if name in visited or depth > _MAX_DEPTH:
            continue
        visited.add(name)
        covers.add(f"make:{name}")
        prerequisites, recipe = rules.get(name, ([], []))
        for line in recipe:
            for key in _line_keys(line):
                covers.add(key)
                if key.startswith("make:") and key[5:] in rules:
                    pending.append((key[5:], depth + 1))
        pending.extend((needed, depth + 1) for needed in prerequisites if needed in rules)
    return frozenset(covers)


def _from_makefile(top: Path, notes: list[str]) -> list[Detected]:
    try:
        present = set(os.listdir(top))  # the name as written: a case-insensitive file system answers for any case
    except OSError:
        return []
    name = next((name for name in _MAKEFILES if name in present), None)
    path = top / name if name is not None else None
    text = _read(top, path, notes) if path is not None and path.is_file() else None
    if path is None or text is None:
        return []
    rules = _make_rules(text)
    found: list[Detected] = []
    for target, (prerequisites, recipe) in rules.items():
        kind = _ENTRY_KINDS.get(_entry_name(target))
        if kind is None or not (prerequisites or recipe):
            continue
        found.append(
            Detected(
                id=target,
                kind=kind,
                run=f"make {shlex.quote(target)}",
                paths=(".",),
                source=f'{path.name} target "{target}"',
                family="make",
                covers=_make_covers(rules, target),
                own=f"make:{target}",
            )
        )
    return found


# --------------------------------------------------------------------------- #
# package.json scripts
# --------------------------------------------------------------------------- #


def _package_manager(top: Path, manifest: dict[str, Any]) -> str:
    declared = manifest.get("packageManager")
    if isinstance(declared, str) and declared.split("@", 1)[0] in {"npm", "pnpm", "yarn", "bun"}:
        return declared.split("@", 1)[0]
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    ):
        if (top / lockfile).is_file():
            return manager
    return "npm"


def _script_covers(scripts: dict[str, str], name: str) -> frozenset[str]:
    covers: set[str] = set()
    pending = [(name, 0)]
    while pending:
        script, depth = pending.pop()
        if f"npm:{script}" in covers or depth > _MAX_DEPTH:
            continue
        covers.add(f"npm:{script}")
        for key in _line_keys(scripts.get(script, "")):
            covers.add(key)
            if key.startswith("npm:") and key[4:] in scripts:
                pending.append((key[4:], depth + 1))
    return frozenset(covers)


def _from_package_json(top: Path, notes: list[str]) -> list[Detected]:
    path = top / "package.json"
    text = _read(top, path, notes) if path.is_file() else None
    if text is None:
        return []
    try:
        manifest = json.loads(text)
    except ValueError:
        notes.append("package.json: not valid JSON, so its scripts were not read")
        return []
    raw = manifest.get("scripts") if isinstance(manifest, dict) else None
    if not isinstance(raw, dict):
        return []
    scripts = {name: body for name, body in raw.items() if isinstance(name, str) and isinstance(body, str)}
    manager = _package_manager(top, manifest)
    found: list[Detected] = []
    for name, body in scripts.items():
        kind = _ENTRY_KINDS.get(_entry_name(name))
        if kind is None or not body.strip() or "no test specified" in body or _WATCH.search(body):
            continue
        found.append(
            Detected(
                id=name,
                kind=kind,
                run=f"{manager} run {shlex.quote(name)}",
                paths=(".",),
                source=f'package.json script "{name}"',
                family="npm",
                covers=_script_covers(scripts, name),
                own=f"npm:{name}",
            )
        )
    return found


# --------------------------------------------------------------------------- #
# GitHub Actions workflows, read line by line
# --------------------------------------------------------------------------- #

_YAML_KEY: Final[re.Pattern[str]] = re.compile(r"^( *)(- +)?([A-Za-z0-9_][A-Za-z0-9_.-]*) *:(?=\s|$)\s*(.*)$")
_YAML_ITEM: Final[re.Pattern[str]] = re.compile(r"^( *)- +(.*)$")
_BLOCK_INDICATOR: Final[re.Pattern[str]] = re.compile(r"[|>][-+]?")


@dataclass
class _Entry:
    """One significant line: a key (``name``) or a sequence item, with its value as written."""

    indent: int
    column: int
    dash: bool
    name: str | None
    value: str = ""
    block: list[str] | None = None
    unreadable: bool = False


def _plain_value(value: str) -> tuple[str, bool]:
    """*value* without a trailing comment, and whether it was read whole (a quoted value closes on its line)."""

    if value[:1] in {'"', "'"}:
        quote, index = value[0], 1
        while index < len(value):
            if quote == '"' and value[index] == "\\":
                index += 2
                continue
            if value[index] == quote:
                if quote == "'" and value[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                rest = value[index + 1 :].strip()
                return value[: index + 1], not rest or rest.startswith("#")
            index += 1
        return value, False
    if value.startswith("#"):
        return "", True
    cut = re.search(r"\s#", value)
    return (value[: cut.start()] if cut else value).strip(), True


def _yaml_entries(text: str) -> list[_Entry]:
    lines = text.splitlines()
    entries: list[_Entry] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line.startswith("\t"):
            continue
        key = _YAML_KEY.match(line)
        if key is not None:
            indent, dash, name, raw = len(key.group(1)), key.group(2) or "", key.group(3), key.group(4)
            entry = _Entry(indent=indent, column=indent + len(dash), dash=bool(dash), name=name)
        else:
            item = _YAML_ITEM.match(line)
            indent = len(line) - len(line.lstrip(" "))
            raw = item.group(2) if item is not None else stripped
            entry = _Entry(indent=indent, column=indent + (2 if item else 0), dash=item is not None, name=None)
        entry.value, whole = _plain_value(raw.strip())
        entries.append(entry)
        if not entry.value:
            continue
        # A value may go on below its line: a block scalar's content, or a quoted or plain value that wraps.
        following: list[str] = []
        while index < len(lines) and (
            not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip(" ")) > entry.column
        ):
            following.append(lines[index])
            index += 1
        while following and not following[-1].strip():
            following.pop()
        if _BLOCK_INDICATOR.fullmatch(entry.value):
            entry.block = following
        elif not whole or any(item.strip() and not item.strip().startswith("#") for item in following):
            entry.unreadable = True
    return entries


def _steps(entries: list[_Entry]) -> list[dict[str, _Entry]]:
    """The mappings that are sequence items (workflow steps among them), each as its keys at its own level."""

    done: list[dict[str, _Entry]] = []
    open_items: list[tuple[int, dict[str, _Entry]]] = []
    for entry in entries:
        while open_items and entry.indent < open_items[-1][0]:
            done.append(open_items.pop()[1])
        if entry.dash and entry.name is not None:
            open_items.append((entry.column, {entry.name: entry}))
        elif entry.name is not None and open_items and entry.indent == open_items[-1][0]:
            open_items[-1][1].setdefault(entry.name, entry)
    done.extend(item for _column, item in reversed(open_items))
    return done


def _defaults(entries: list[_Entry]) -> tuple[bool, bool]:
    """Whether a ``defaults:`` block sets a working directory, and whether it sets a shell other than bash or sh."""

    directory = shell = False
    for position, entry in enumerate(entries):
        if entry.name != "defaults":
            continue
        for inner in entries[position + 1 :]:
            if inner.indent <= entry.indent:
                break
            directory = directory or inner.name == "working-directory"
            shell = shell or (inner.name == "shell" and not _posix_shell(_scalar(inner) or ""))
    return directory, shell


def _posix_shell(value: str) -> bool:
    return re.match(r"(?:bash|sh)(?:\s|$)", value) is not None


def _scalar(entry: _Entry) -> str | None:
    """A one-line value as YAML reads it, or ``None`` when it is not one read here."""

    value = entry.value
    if entry.unreadable or entry.block is not None or not value or value[0] in "[{&*!%@`|>":
        return None
    if value[0] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return None
        return decoded if isinstance(decoded, str) else None
    if value[0] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _run_script(entry: _Entry) -> str | None:
    if entry.block is None:
        return _scalar(entry)
    content = [line for line in entry.block if line.strip()]
    if not content or any(char.isdigit() for char in entry.value):
        return None
    indent = len(content[0]) - len(content[0].lstrip(" "))
    if any(len(line) - len(line.lstrip(" ")) < indent for line in content):
        return None
    body = [line[indent:] if line.strip() else "" for line in entry.block]
    if entry.value.startswith("|"):
        return "\n".join(body)
    if any(line[:1].isspace() for line in body):
        return None  # a folded scalar's more indented lines keep their line breaks: not read here
    paragraphs: list[list[str]] = [[]]
    for line in body:
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return "\n".join(" ".join(paragraph) for paragraph in paragraphs if paragraph)


def _subdirectory(top: Path, current: str, target: str) -> str | None:
    """*target* relative to *current*, as a directory of the repository; ``None`` when it is not one."""

    if _SAFE_DIR.fullmatch(target) is None:
        return None
    joined = posixpath.normpath(posixpath.join(current or ".", target))
    if joined == ".":
        return ""
    if joined.startswith("../") or joined == ".." or not (top / joined).is_dir():
        return None
    return joined


def _script_checks(top: Path, script: str, directory: str) -> list[tuple[str, str, tuple[str, ...], str]] | None:
    """The checks a step's script runs, as (command, directory, keys, kind); a ``cd`` holds for the lines after it,
    as it does in the step's shell.

    ``None`` when the script branches or loops (``if``, ``case``, ``for`` ...): which commands run is not known
    here. It stops at a ``cd`` it cannot follow, and at ``exit``: nothing after them runs where, or as, written.
    """

    lines = [line.strip() for line in _logical_lines(script)]
    lines = [line for line in lines if line and not line.startswith("#")]
    if any(_CONTROL.match(line) for line in lines):
        return None
    found: list[tuple[str, str, tuple[str, ...], str]] = []
    for line in lines:
        segments = _segments(line)
        if segments is None:
            if _CHANGES_DIR.search(line):
                return found
            continue
        for position, segment in enumerate(segments):
            try:
                words = shlex.split(segment)
            except ValueError:
                break
            if words[:1] in (["exit"], ["return"]):
                return found
            if words[:1] in (["["], ["[["], ["test"], ["!"]) and position + 1 < len(segments):
                break  # what follows runs only when the test passes
            if words[:1] in (["cd"], ["pushd"], ["popd"]):
                moved = _subdirectory(top, directory, words[1]) if len(words) == 2 and words[0] == "cd" else None
                if moved is None:
                    return found
                directory = moved
                continue
            if "$" in segment:
                continue  # a variable the workflow sets is not set here
            invocation = _invocation(words)
            if invocation is None or not invocation[1]:
                continue
            command = f"cd {shlex.quote(directory)} && {segment}" if directory else segment
            found.append((command, directory, *invocation))
    return found


def _workflow_checks(top: Path, path: Path, text: str, notes: list[str]) -> list[Detected]:
    where = path.relative_to(top).as_posix()
    entries = _yaml_entries(text)
    default_directory, default_shell = _defaults(entries)
    found: list[Detected] = []
    skipped = 0
    for step in _steps(entries):
        run = step.get("run")
        if run is None:
            continue
        script = _run_script(run)
        shell = step.get("shell")
        shell_value = _scalar(shell) if shell is not None else None
        stop = step.get("continue-on-error")
        directory: str | None = ""
        if "working-directory" in step:
            value = _scalar(step["working-directory"])
            directory = _subdirectory(top, "", value) if value else None
        elif default_directory:
            directory = None
        commands = None if script is None or directory is None else _script_checks(top, script, directory)
        if (
            commands is None
            or (shell is not None and not _posix_shell(shell_value or ""))
            or (shell is None and default_shell)
            or (stop is not None and (_scalar(stop) or "true") != "false")
        ):
            skipped += 1
            continue
        for command, cwd, keys, kind in commands:
            name = keys[0].split(":", 1)[-1]
            found.append(
                Detected(
                    id=f"{cwd}/{name}" if cwd else name,
                    kind=kind,
                    run=command,
                    paths=_paths_for(name, cwd),
                    source=where,
                    family="ci",
                    covers=frozenset(_scoped(key, cwd) for key in keys),
                )
            )
    if skipped:
        notes.append(
            f"{where}: {skipped} run step(s) not read (a branch or loop, another shell, an expression, "
            "continue-on-error, or a working directory that is unclear or missing)"
        )
    return found


def _from_workflows(top: Path, notes: list[str]) -> list[Detected]:
    folder = top / ".github" / "workflows"
    if not folder.is_dir():
        return []
    try:
        files = sorted(path for path in folder.iterdir() if path.suffix in {".yml", ".yaml"} and path.is_file())
    except OSError:
        return []
    if len(files) > _MAX_WORKFLOWS:
        notes.append(f".github/workflows: read the first {_MAX_WORKFLOWS} of {len(files)} workflows, by name")
        files = files[:_MAX_WORKFLOWS]
    found: list[Detected] = []
    for path in files:
        text = _read(top, path, notes)
        if text is not None:
            found += _workflow_checks(top, path, text, notes)
    return found


# --------------------------------------------------------------------------- #
# pyproject.toml tools
# --------------------------------------------------------------------------- #


def _requirement_names(items: object) -> set[str]:
    names: set[str] = set()
    for item in items if isinstance(items, list) else ():
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", item) if isinstance(item, str) else None
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1).lower()))
    return names


def _table(value: object, *keys: str) -> dict[str, Any]:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def _development_requirements(data: dict[str, Any]) -> set[str]:
    """Names in the project's dependencies, optional dependencies and dependency groups, and in the uv, PDM and Poetry
    dependency lists."""

    names = _requirement_names(_table(data, "project").get("dependencies"))
    for group in (
        *_table(data, "project", "optional-dependencies").values(),
        *_table(data, "dependency-groups").values(),
        *_table(data, "tool", "pdm", "dev-dependencies").values(),
        _table(data, "tool", "uv").get("dev-dependencies"),
    ):
        names |= _requirement_names(group)
    poetry = _table(data, "tool", "poetry")
    for group in (
        _table(poetry, "dependencies"),
        _table(poetry, "dev-dependencies"),
        *(_table(g, "dependencies") for g in _table(poetry, "group").values()),
    ):
        names |= _requirement_names(list(group))  # Poetry names its dependencies as keys
    return names


def _dependency_groups(data: dict[str, Any]) -> dict[str, set[str]]:
    """Each dependency group's requirement names, those of the groups it includes among them."""

    groups = _table(data, "dependency-groups")

    def names(group: str, seen: frozenset[str]) -> set[str]:
        items = groups.get(group)
        found = _requirement_names(items)
        for item in items if isinstance(items, list) else ():
            included = item.get("include-group") if isinstance(item, dict) else None
            if isinstance(included, str) and included not in seen:
                found |= names(included, seen | {included})
        return found

    return {group: names(group, frozenset({group})) for group in groups}


def _uv_selector(data: dict[str, Any], name: str) -> str | None:
    """What ``uv run`` needs to have *name*: ``""`` when the project's dependencies or a default group have it,
    ``--group G `` or ``--extra E `` when only a group or an extra uv leaves out by default does, else ``None``."""

    uv = _table(data, "tool", "uv")
    groups = _dependency_groups(data)
    groups.setdefault("dev", set()).update(_requirement_names(uv.get("dev-dependencies")))
    chosen = uv.get("default-groups", ["dev"])
    if chosen == "all":
        defaults = {_normalized(group) for group in groups}
    else:
        defaults = (
            {_normalized(group) for group in chosen if isinstance(group, str)} if isinstance(chosen, list) else set()
        )
    runtime = _requirement_names(_table(data, "project").get("dependencies"))
    if name in runtime or any(name in members for group, members in groups.items() if _normalized(group) in defaults):
        return ""
    extras = {
        extra: _requirement_names(items) for extra, items in _table(data, "project", "optional-dependencies").items()
    }
    for option, places in (("--group", groups), ("--extra", extras)):
        place = next((place for place, members in places.items() if name in members), None)
        if place is not None:
            return f"{option} {shlex.quote(place)} "
    return None


def _python_runner(top: Path) -> str:
    for lockfile, runner in (("uv.lock", "uv run "), ("poetry.lock", "poetry run "), ("pdm.lock", "pdm run ")):
        if (top / lockfile).is_file():
            return runner
    return "python3 -m "


def _mypy_targets(top: Path, data: dict[str, Any], config: object) -> str:
    if isinstance(config, dict) and any(config.get(key) for key in ("files", "packages", "modules")):
        return ""
    if (top / "src").is_dir():
        return " src"
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    package = re.sub(r"[-.]+", "_", name.lower()) if isinstance(name, str) else ""
    return f" {package}" if package and _SAFE_DIR.fullmatch(package) and (top / package).is_dir() else " ."


def _from_pyproject(top: Path, notes: list[str]) -> list[Detected]:
    path = top / "pyproject.toml"
    text = _read(top, path, notes) if path.is_file() else None
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        notes.append("pyproject.toml: not valid TOML, so its tools were not read")
        return []
    raw_tools = data.get("tool")
    tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
    development = _development_requirements(data)
    runner = _python_runner(top)
    found: list[Detected] = []

    def prefix(package: str) -> str | None:
        """What runs *package*'s command, or ``None`` when the environment it runs in would not have it."""

        if runner == "python3 -m ":
            return runner if package in tools or package in development else None
        if package not in development:
            return None  # configured, but no dependency list names it: the locked environment does not have it
        if runner == "uv run ":
            selector = _uv_selector(data, package)
            return None if selector is None else runner + selector
        return runner

    def add(name: str, kind: str, command: str, via: str) -> bool:
        run = prefix(via)
        if run is None:
            return False
        why = f"[tool.{via}]" if via in tools else f"{via} as a development dependency"
        found.append(
            Detected(
                id=name,
                kind=kind,
                run=run + command,
                paths=_paths_for(name, ""),
                source=f"pyproject.toml {why}",
                family="pyproject",
                covers=frozenset({name}),
            )
        )
        return True

    add("pytest", "test", "pytest", "pytest")
    add("ruff", "lint", "ruff check .", "ruff")
    if isinstance(tools.get("ruff"), dict) and "format" in tools["ruff"]:
        add("ruff-format", "format", "ruff format --check .", "ruff")
    add("black", "format", "black --check .", "black")
    add("mypy", "typecheck", "mypy" + _mypy_targets(top, data, tools.get("mypy")), "mypy")
    for checker in ("pyright", "basedpyright"):
        if add("pyright", "typecheck", checker, checker):
            break
    return found


# --------------------------------------------------------------------------- #
# Build manifests
# --------------------------------------------------------------------------- #

_GRADLE_BUILDS: Final[tuple[str, ...]] = ("build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle")


def _from_manifests(top: Path) -> list[Detected]:
    """The test command of a Go module, a Cargo package, a Maven or a Gradle build at the root."""

    found: list[Detected] = []

    def add(tool: str, run: str, manifest: str) -> None:
        found.append(
            Detected(
                id=tool,
                kind="test",
                run=run,
                paths=_paths_for(tool, ""),
                source=manifest,
                family="manifest",
                covers=frozenset({tool}),
            )
        )

    if (top / "go.mod").is_file():
        add("go-test", "go test ./...", "go.mod")
    if (top / "Cargo.toml").is_file():
        add("cargo-test", "cargo test", "Cargo.toml")
    if (top / "pom.xml").is_file():
        add("maven", f"{'./mvnw' if (top / 'mvnw').is_file() else 'mvn'} -B test", "pom.xml")
    gradle = next((name for name in _GRADLE_BUILDS if (top / name).is_file()), None)
    if gradle is not None:
        add("gradle", f"{'./gradlew' if (top / 'gradlew').is_file() else 'gradle'} check", gradle)
    return found


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #

#: Root lockfiles written as ``[[dependencies]]``, with what installs them: only those Review provisions, since it
#: refuses a run whose file declares any other lockfile.
_DECLARED_LOCKFILES: Final[dict[str, str]] = {"package-lock.json": "npm ci", "uv.lock": "uv sync --frozen"}
#: Root lockfiles that are only named in a note.
_OTHER_LOCKFILES: Final[tuple[str, ...]] = (
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "pdm.lock",
    "Gemfile.lock",
)


def _lockfile(top: Path, name: str) -> bool:
    path = top / name
    return path.is_file() and not path.is_symlink()


def _dependencies(top: Path, notes: list[str]) -> list[Dependency]:
    others = [name for name in _OTHER_LOCKFILES if _lockfile(top, name)]
    if others:
        notes.append(
            f"{', '.join(others)}: not written as [[dependencies]]: Review provisions only package-lock.json and "
            "uv.lock and refuses a run that declares another, so a server installs nothing for the checks"
        )
    return [
        Dependency(lockfile=name, install=install)
        for name, install in _DECLARED_LOCKFILES.items()
        if _lockfile(top, name)
    ]


# --------------------------------------------------------------------------- #
# Choosing, writing
# --------------------------------------------------------------------------- #


def _check_id(hint: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", hint.lower()).strip("-")
    text = re.sub(r"^[^a-z0-9]+", "", text)
    return text or "check"


def detect(top: Path) -> Detection:
    """The checks the repository at *top* already runs, strongest source first; see the module docstring."""

    notes: list[str] = []
    # pyproject.toml says which tools are set up, not that anything runs them, and a build manifest only says how its
    # tests would run: they only fill a kind nothing else does.
    sources = (
        (_from_makefile(top, notes), False),
        (_from_package_json(top, notes), False),
        (_from_workflows(top, notes), False),
        (_from_pyproject(top, notes), True),
        (_from_manifests(top), True),
    )
    # What each Makefile target and package.json script runs, so a CI step `npm run check` is read as what `check` runs.
    entries = {c.own: c.covers - {c.own} for candidates, _gaps in sources for c in candidates if c.own}

    def runs(candidate: Detected) -> set[str]:
        keys: set[str] = set()
        for key in candidate.covers - {candidate.own}:
            keys |= entries.get(key) or {key}
        return keys

    chosen: list[Detected] = []
    covered: set[str] = set()
    for candidates, gaps_only in sources:
        kinds = {item.kind for item in chosen}
        # Narrow entry points before the ones that run several of them, so an aggregate is left out.
        for candidate in sorted(candidates, key=lambda item: len(item.covers)):
            others = runs(candidate)
            if (
                (others and others <= covered)
                or any(key.startswith("rewrite:") for key in candidate.covers)
                or any(item.run == candidate.run for item in chosen)
                or (gaps_only and candidate.kind in kinds)
            ):
                continue
            chosen.append(candidate)
            covered |= candidate.covers
    taken: set[str] = set()
    named: list[Detected] = []
    for candidate in chosen:
        base = _check_id(candidate.id)
        options = [base, f"{candidate.family}-{base}", *(f"{base}-{n}" for n in range(2, len(chosen) + 2))]
        check_id = next(option for option in options if option not in taken)
        taken.add(check_id)
        named.append(replace(candidate, id=check_id))
    named.sort(key=lambda item: _KIND_ORDER.index(item.kind) if item.kind in _KIND_ORDER else len(_KIND_ORDER))
    return Detection(checks=named, notes=notes, dependencies=_dependencies(top, notes))


def _toml_string(value: str) -> str:
    printable = all(char == "\t" or (ord(char) >= 32 and char != "\x7f") for char in value)
    if printable and '"' not in value and "\\" not in value:
        return f'"{value}"'
    if printable and "'" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _comment(text: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in text)


def render(checks: Sequence[Detected], toolchain: str | None = None, dependencies: Sequence[Dependency] = ()) -> str:
    """The definition: ``version``, ``[toolchain]`` (or the comment that stands for it), the dependencies and the
    checks."""

    parts = [_HEADER, "version = 1\n", "\n"]
    if toolchain:
        parts += ["[toolchain]\n", f"image = {_toml_string(toolchain)}\n"]
    else:
        parts.append(_NO_TOOLCHAIN)
    if dependencies:
        parts += ["\n", _DEPENDENCIES_NOTE]
    for position, dependency in enumerate(dependencies):
        parts += [
            "\n" if position else "",
            "[[dependencies]]\n",
            f"lockfile = {_toml_string(dependency.lockfile)}\n",
            f"install = {_toml_string(dependency.install)}\n",
        ]
    for check in checks:
        parts += [
            "\n",
            f"# From {_comment(check.source)}\n",
            "[[checks]]\n",
            f"id = {_toml_string(check.id)}\n",
            f"kind = {_toml_string(check.kind)}\n",
            f"paths = [{', '.join(_toml_string(spec) for spec in check.paths)}]\n",
            f"run = {_toml_string(check.run)}\n",
        ]
    return "".join(parts)


def _same_except_toolchain(before: Environment, after: Environment) -> bool:
    return replace(after, toolchain=before.toolchain) == before


def with_toolchain(text: str, image: str) -> str | None:
    """*text* with only its ``[toolchain]`` image set to *image*: one line replaced, or the table added where the
    comment ``lc verify init`` leaves stands, else before the first table. ``None`` when no such edit gives a
    definition that reads the same apart from the toolchain (a toolchain written inline, say).

    Raises :class:`DefinitionError` when *text* itself does not load.
    """

    before = parse_environment(text)
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    entry = f"image = {_toml_string(image)}"
    table = [f"[toolchain]{newline}", f"{entry}{newline}"]
    candidates: list[list[str]] = []
    if before.toolchain is not None:
        for start, line in enumerate(lines):
            if not _TOOLCHAIN_HEADER.match(line):
                continue
            for position in range(start + 1, len(lines)):
                if _TABLE_HEADER.match(lines[position]):
                    break
                key = _IMAGE_KEY.match(lines[position])
                if key is not None:
                    body = lines[position].rstrip("\r\n")
                    ending = lines[position][len(body) :] or newline
                    kept = _IMAGE_COMMENT.fullmatch(body)
                    comment = kept.group(1) or "" if kept is not None else ""
                    replaced = f"{key.group(1)}{entry}{comment}{ending}"
                    candidates.append([*lines[:position], replaced, *lines[position + 1 :]])
                    break
    else:
        note = [f"{line}{newline}" for line in _NO_TOOLCHAIN.splitlines()]
        for start in range(len(lines) - len(note) + 1):
            if lines[start : start + len(note)] == note:
                candidates.append([*lines[:start], *table, *lines[start + len(note) :]])
        first = next((n for n, line in enumerate(lines) if _TABLE_HEADER.match(line)), None)
        if first is not None:
            while first > 0 and lines[first - 1].lstrip().startswith("#"):
                first -= 1  # keep a table's comment with its table
            gap = [newline] if first > 0 and lines[first - 1].strip() else []
            candidates.append([*lines[:first], *gap, *table, newline, *lines[first:]])
        unended = [newline] if lines and not lines[-1].endswith("\n") else []
        candidates.append([*lines, *unended, newline, *table])
    for candidate in candidates:
        result = "".join(candidate)
        try:
            after = parse_environment(result)
        except DefinitionError:
            continue
        if after.toolchain == Toolchain(image=image) and _same_except_toolchain(before, after):
            return result
    return None


# --------------------------------------------------------------------------- #
# lc verify init
# --------------------------------------------------------------------------- #


def _parse(arguments: Sequence[str]) -> tuple[bool, str | None] | str:
    show = False
    image: str | None = None
    items = iter(arguments)
    for item in items:
        if item == "--print":
            show = True
        elif item == "--toolchain" or item.startswith("--toolchain="):
            value = item.partition("=")[2] if "=" in item else next(items, "")
            if not value or value.startswith("-"):
                return "--toolchain needs an image, such as python:3.12-slim"
            if image is not None:
                return "--toolchain is given twice"
            image = value
        else:
            return f"unknown argument: {item}"
    return show, image


def _pin(image: str) -> str:
    """*image* pinned to the digest its registry serves; raises :class:`RegistryError`."""

    reference = parse_reference(image)
    probe = f"version = 1\n[toolchain]\nimage = {_toml_string(reference.pinned('sha256:' + '0' * 64))}\n"
    try:
        parse_environment(probe)
    except DefinitionError:
        raise RegistryError(f"{image} is not an image reference {ENVIRONMENT_FILE} accepts") from None
    return reference.pinned(resolve_digest(reference))


def _diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{ENVIRONMENT_FILE}",
        tofile=f"b/{ENVIRONMENT_FILE}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _replace_file(path: Path, before: bytes, after: bytes) -> None:
    """Atomically replace *path*, unless it changed since it was read as *before*."""

    mode = path.stat().st_mode & 0o7777
    handle, temporary = tempfile.mkstemp(prefix=".environment-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(after)
        os.chmod(temporary, mode)
        if path.read_bytes() != before:
            raise OSError("it changed while the toolchain was being resolved; run the command again")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _set_toolchain(path: Path, image: str, *, show: bool, sink: TextIO) -> int:
    if path.is_symlink() or not path.is_file():
        sink.write(f"lc verify: {ENVIRONMENT_FILE} is not a regular file, so it was left alone\n")
        return 2
    try:
        before = path.read_bytes()
        text = before.decode("utf-8")
        parse_environment(text)
    except (OSError, UnicodeDecodeError) as exc:
        sink.write(f"lc verify: cannot read {ENVIRONMENT_FILE}: {exc}\n")
        return 2
    except DefinitionError as exc:
        sink.write(f"lc verify: {exc}\nlc verify: fix the file before setting its [toolchain]\n")
        return 2
    try:
        pinned = _pin(image)
    except RegistryError as exc:
        sink.write(f"lc verify: could not pin {image}: {exc}. Nothing was written.\n")
        return 2
    result = with_toolchain(text, pinned)
    if result is None:
        sink.write(
            f"lc verify: {ENVIRONMENT_FILE} names its toolchain in a form only a person should edit, so it was left "
            f"alone. Set it to:\n[toolchain]\nimage = {_toml_string(pinned)}\n"
        )
        return 2
    if show:
        sink.write(result)
        return 0
    if result == text:
        sink.write(f"lc verify: [toolchain] is already {pinned}; nothing changed\n")
        return 0
    sink.write(_diff(text, result))
    try:
        _replace_file(path, before, result.encode("utf-8"))
    except OSError as exc:
        sink.write(f"lc verify: {ENVIRONMENT_FILE} was not changed: {exc}\n")
        return 2
    sink.write(f"lc verify: [toolchain] in {ENVIRONMENT_FILE} is now {pinned}; commit it\n")
    return 0


def _top(cwd: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=False
    )
    return Path(completed.stdout.strip()) if completed.returncode == 0 and completed.stdout.strip() else None


def init_main(arguments: Sequence[str], sink: TextIO, *, cwd: Path | None = None) -> int:
    if any(item in {"-h", "--help", "help"} for item in arguments):
        sink.write(INIT_USAGE)
        return 0
    parsed = _parse(arguments)
    if isinstance(parsed, str):
        sink.write(f"lc verify init: {parsed}\n{INIT_USAGE}")
        return 2
    show, image = parsed
    where = cwd or Path.cwd()
    top = _top(where)
    if top is None:
        sink.write(f"lc verify: {where} is not inside a Git working tree\n")
        return 2
    path = top / ENVIRONMENT_FILE
    if os.path.lexists(path):
        if image is not None:
            return _set_toolchain(path, image, show=show, sink=sink)
        sink.write(
            f"lc verify: {ENVIRONMENT_FILE} already exists, so it was left alone. "
            "`lc verify init --toolchain IMAGE` adds or replaces only its [toolchain].\n"
        )
        return 0

    detection = detect(top)
    if not detection.checks:
        sink.write(
            "lc verify: found no checks this repository runs (Makefile targets such as test or lint, package.json "
            "scripts, GitHub Actions run steps, pytest, ruff or mypy in pyproject.toml, or a go.mod, Cargo.toml, "
            "pom.xml or Gradle build), so nothing was written. "
            f"Write {ENVIRONMENT_FILE} by hand: lc verify --help\n"
        )
        sink.writelines(f"lc verify: note: {note}\n" for note in detection.notes)
        return 2
    pinned: str | None = None
    if image is not None:
        try:
            pinned = _pin(image)
        except RegistryError as exc:
            sink.write(f"lc verify: could not pin {image}: {exc}. Nothing was written.\n")
            return 2
    text = render(detection.checks, pinned, detection.dependencies)
    expected = tuple(Check(id=c.id, paths=c.paths, run=c.run, kind=c.kind) for c in detection.checks)
    try:
        written = parse_environment(text)
        if written.checks != expected or written.dependencies != tuple(detection.dependencies):
            raise DefinitionError(f"{ENVIRONMENT_FILE}: the checks or dependencies did not read back as written")
    except DefinitionError as exc:
        sink.write(f"lc verify: the definition would not load, so nothing was written: {exc}\n")
        return 2
    if show:
        sink.write(text)
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:  # exclusive: never over a file written meanwhile
            stream.write(text.encode("utf-8"))
    except OSError as exc:
        sink.write(f"lc verify: could not write {ENVIRONMENT_FILE}: {exc}\n")
        return 2
    width = max(len(check.id) for check in detection.checks)
    sink.write(f"lc verify: wrote {ENVIRONMENT_FILE} with {len(detection.checks)} check(s):\n")
    sink.writelines(f"  {check.id:<{width}}  {check.run}\n" for check in detection.checks)
    sink.writelines(
        f"lc verify: a server installs dependencies first: {item.install} ({item.lockfile})\n"
        for item in detection.dependencies
    )
    sink.writelines(f"lc verify: note: {note}\n" for note in detection.notes)
    if pinned is None:
        sink.write("lc verify: no [toolchain]; a Workspaces server needs one: lc verify init --toolchain IMAGE\n")
    sink.write("lc verify: review the checks, commit the file, then run `lc verify hook install`\n")
    return 0
