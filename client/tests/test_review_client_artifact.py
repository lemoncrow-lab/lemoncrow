"""The Review-only client is the files in ``client/review-files.txt`` and nothing else.

``lc review`` once ended in ``ModuleNotFoundError: lemoncrow_client.session``: a function-level import of a
module the Review wheel does not ship. Every test ran against the whole source tree, where that module exists.
These tests run against the shipped file list instead, two ways:

* a static scan: every first-party module a shipped file can import (at any depth) is shipped, or is named in
  ``_ABSENT_ON_PURPOSE`` with the reason it is safe to be missing;
* a copy of exactly the shipped files, in a fresh interpreter that sees nothing else, imports every module and
  runs the commands that need no server.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "lemoncrow_client"
SHIPPED_LIST = ROOT / "review-files.txt"

#: Modules the Review wheel leaves out, that a shipped file can still name. Each is a Runtime or hosted-device
#: module behind a guard (``TYPE_CHECKING``, a product check, or a message that says the command is not part of this
#: install); a new unshipped import that is not listed here is the bug this test exists to catch.
_ABSENT_ON_PURPOSE: dict[str, str] = {
    "mcpserver": "Runtime: `lc rt mcp` says Runtime is not installed first",
    "routing": "Runtime: `lc rt` verbs say Runtime is not installed first",
    "host_install": "Runtime: host registration is `lc rt enable`",
    "host_trial": "Runtime: `lc with`",
    "pi_host": "Runtime: `lc rt host`",
    "pi_integration": "Runtime: `lc rt host`",
    "lite/index": "Runtime Lite index (TYPE_CHECKING in cli.py, Runtime verbs elsewhere)",
    "lite/mcp": "Runtime Lite",
    "remote/cli": "Runtime: the opt-in remote MCP gateway",
    "walkcache": "TYPE_CHECKING only in manifest.py",
    "review_delta": "hosted `watch` publication; TYPE_CHECKING in review.py",
    "review_watch": "hosted `lc review watch`",
    "review_service": "hosted `lc review service`",
    "review_hooks": "hosted agent-checkpoint `lc review hook after-edit`",
    "review_device_connect": "hosted `lc review connect`",
    "review_companion_cli": "hosted `lc review pending|device|task`",
}


def _shipped() -> list[str]:
    return [
        line.strip()
        for line in SHIPPED_LIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _module_name(relative: str) -> str:
    parts = relative.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["lemoncrow_client", *parts])


def _imported_files(relative: str) -> set[str]:
    """Package-relative files (``name.py`` or ``name/__init__.py``) a module names in any import statement."""

    package = _module_name(relative).split(".")
    if not relative.endswith("__init__.py"):
        package = package[:-1]
    wanted: set[str] = set()
    for node in ast.walk(ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))):
        bases: list[list[str]] = []
        if isinstance(node, ast.Import):
            bases = [alias.name.split(".") for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                base = [*base, *(node.module.split(".") if node.module else [])]
            else:
                base = node.module.split(".") if node.module else []
            bases = [base, *([*base, alias.name] for alias in node.names)]
        for parts in bases:
            if parts[:1] != ["lemoncrow_client"] or len(parts) < 2:
                continue
            inner = parts[1:]
            for depth in range(1, len(inner) + 1):
                stem = "/".join(inner[:depth])
                for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                    if (PACKAGE / candidate).is_file():
                        wanted.add(candidate)
    return wanted


def test_every_module_a_shipped_file_can_import_is_shipped_or_absent_on_purpose() -> None:
    shipped = set(_shipped())
    missing: dict[str, list[str]] = {}
    for relative in sorted(shipped):
        if not relative.endswith(".py"):
            continue
        for target in _imported_files(relative):
            if target in shipped:
                continue
            name = target.removesuffix(".py").removesuffix("/__init__")
            if name in _ABSENT_ON_PURPOSE or any(absent.startswith(f"{name}/") for absent in _ABSENT_ON_PURPOSE):
                continue
            missing.setdefault(target, []).append(relative)
    assert missing == {}, (
        "shipped code imports modules the Review wheel does not ship; ship them in review-files.txt or, for a "
        f"Runtime-only path that a message guards, list them in _ABSENT_ON_PURPOSE: {missing}"
    )


def test_modules_absent_on_purpose_exist_and_are_still_imported() -> None:
    shipped = set(_shipped())
    imported = {target for relative in shipped if relative.endswith(".py") for target in _imported_files(relative)}
    for name in _ABSENT_ON_PURPOSE:
        files = [f for f in (f"{name}.py", f"{name}/__init__.py") if (PACKAGE / f).is_file()]
        assert files, f"{name} is not a module of the client any more"
        assert not set(files) & shipped, f"{name} is shipped; drop it from _ABSENT_ON_PURPOSE"
        assert set(files) & imported, f"{name} is not imported by a shipped file; drop it from _ABSENT_ON_PURPOSE"


def test_every_shipped_file_exists() -> None:
    assert [name for name in _shipped() if not (PACKAGE / name).is_file()] == []


@pytest.fixture
def shipped_tree(tmp_path: Path) -> Path:
    """Exactly the shipped files, as the wheel lays them out."""

    target = tmp_path / "site" / "lemoncrow_client"
    for relative in _shipped():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((PACKAGE / relative).read_bytes())
    return tmp_path / "site"


def _isolated(site: Path, code: str, *arguments: str, home: Path) -> subprocess.CompletedProcess[str]:
    program = f"import sys; sys.path.insert(0, {str(site)!r})\n{code}"
    environment = {"HOME": str(home), "LEMONCROW_HOME": str(home / ".lemoncrow"), "LEMONCROW_NO_LAZY_INSTALL": "1"}
    return subprocess.run(
        [sys.executable, "-I", "-c", program, *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=home,
        timeout=120,
        check=False,
    )


def test_the_shipped_files_import_on_their_own(shipped_tree: Path, tmp_path: Path) -> None:
    modules = [_module_name(name) for name in _shipped() if name.endswith(".py") and not name.endswith("__main__.py")]
    done = _isolated(
        shipped_tree,
        "import importlib, sys\nfor name in sys.argv[1:]:\n    importlib.import_module(name)\nprint(len(sys.argv) - 1)",
        *modules,
        home=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(len(modules))


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["--help"], 0),
        (["--version"], 0),
        (["review", "--help"], 0),
        (["status"], 0),
        (["review", "--bogus"], 2),
        # Hosted machine-companion commands are not in this wheel: a sentence and 2, never a traceback.
        (["review", "watch"], 2),
        (["review", "connect"], 2),
        (["review", "service"], 2),
        (["review", "pending"], 2),
        (["review", "hook", "after-edit"], 2),
        # No engine in this temp HOME: the model-backed commands say how to get it.
        (["review", "--ai"], 2),
        (["review", "hook", "install"], 2),
        (["review", "config"], 2),
        (["review", "init-action"], 2),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else str(value),
)
def test_the_commands_that_need_no_server_run_from_the_shipped_files_alone(
    shipped_tree: Path, tmp_path: Path, arguments: list[str], code: int
) -> None:
    done = _isolated(
        shipped_tree,
        "import sys\nfrom lemoncrow_client.cli import main\nraise SystemExit(main(sys.argv[1:]))",
        *arguments,
        home=tmp_path,
    )
    assert "Traceback" not in done.stderr + done.stdout, done.stderr
    assert done.returncode == code, done.stdout + done.stderr


def test_a_review_capture_with_no_server_is_a_message_not_a_traceback(shipped_tree: Path, tmp_path: Path) -> None:
    """The capture path reaches the lazy imports a bare ``--help`` does not."""

    repository = tmp_path / "repo"
    repository.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "a@example.invalid"],
        ["git", "config", "user.name", "A"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    (repository / "a.py").write_text("one = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True, capture_output=True)
    (repository / "a.py").write_text("one = 2\n", encoding="utf-8")
    program = (
        f"import os, sys\nsys.path.insert(0, {str(shipped_tree)!r})\nos.chdir({str(repository)!r})\n"
        "from lemoncrow_client.cli import main\nraise SystemExit(main(['review', '--open']))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "LEMONCROW_HOME": str(tmp_path / ".lemoncrow"),
            "LEMONCROW_URL": "http://127.0.0.1:9",
            "LEMONCROW_INSTALL_MODE": "local",
            "LEMONCROW_NO_LAZY_INSTALL": "1",
        },
        timeout=120,
        check=False,
    )
    assert "Traceback" not in done.stderr + done.stdout, done.stderr
