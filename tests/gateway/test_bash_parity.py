"""Client ``bash`` and the main package's bash show the model the same text.

Both compact through ``lemoncrow_client.kit.bash_output``. This pins the glue
around it on each side (byte cap, recovery footer, spill) so the two cannot
drift: the same combined output, run through the client executor's path and
through ``bash_exec._compact_result`` plus the main renderer, gives the same
body.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.localtools import LocalContext, shell

from lemoncrow.gateway.tools.rendering import render_bash_text
from lemoncrow.pro.capabilities.tool_supervision import bash_exec

_COMMANDS = (
    "uv run pytest -q",
    "cd app && npm test",
    "cargo test",
    "ls -la",
    "git status --porcelain=v1 -b",
    "find . -name '*.py'",
    "npm install",
    "uv sync",
    "make",
    "cat big.log",
    "echo hi",
    "pytest && git status",
    "tail -n 400 app.log",
)
_LINES = (
    "ok",
    "compiling module",
    "warning: deprecated call",
    "ERROR: boom",
    "Traceback (most recent call last):",
    '  File "x.py", line 3, in f',
    "AssertionError",
    "tests/test_a.py::test_x PASSED",
    "FAILED tests/test_a.py::test_y - assert 1 == 2",
    "\x1b[32mgreen\x1b[0m",
    "progress 10%",
    "progress 10%",
    "token=ghp_abcdefghijklmnopqrstuvwx",
    "",
    "   indented",
    "unicode ✓ ✗",
    "=================== FAILURES ===================",
    "1 failed, 2 passed in 0.5s",
)
_SPILL_PATH = re.compile(r"full: [^\]\n]+")


def _cases(count: int) -> Iterator[tuple[str, str, int]]:
    rng = random.Random(20260922)
    for _ in range(count):
        size = rng.choice((0, 1, 5, 40, 200, 800, 3000))
        lines = ["x" * rng.randint(60, 1200) if rng.random() < 0.1 else rng.choice(_LINES) for _ in range(size)]
        raw = "\n".join(lines) + ("\n" if lines and rng.random() < 0.8 else "")
        yield rng.choice(_COMMANDS), raw, rng.choice((0, 0, 0, 1, 2))


def _normalized(text: str) -> str:
    return _SPILL_PATH.sub("full: PATH", text).strip()


@pytest.fixture()
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalContext:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    monkeypatch.setenv("LEMONCROW_BASH_UNCHANGED_DELTA", "0")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    state = tmp_path / "home" / "lemoncrow"
    state.mkdir(parents=True)
    config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=root)
    return LocalContext(config=config, repo_root=root, sync=None, environment={})


def _main_body(command: str, raw: str, exit_code: int) -> str:
    result = bash_exec._compact_result(
        command=command,
        raw_stdout=bash_exec.strip_ansi(raw),
        raw_stderr="",
        exit_code=exit_code,
        duration_ms=0,
        max_lines=bash_exec.DEFAULT_MAX_LINES,
    )
    rendered = render_bash_text(
        {
            "exit_code": exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
            "lines_omitted": result.lines_omitted,
            "chars_omitted": result.chars_omitted,
            "spill_hint": result.spill_hint,
        }
    )
    # The renderer shows the exit code for a failure or an empty success; the
    # client ends every section with ``[exit N]`` instead.
    if rendered == f"exit_code={exit_code}":
        return ""
    return rendered.removeprefix(f"exit_code={exit_code}") if exit_code else rendered


def test_client_and_main_bash_show_the_same_output(context: LocalContext) -> None:
    diverged = []
    spilled = 0
    for index, (command, raw, exit_code) in enumerate(_cases(300)):
        client = shell._present(context, command, raw, exit_code, context.repo_root, "")
        main = _main_body(command, raw, exit_code)
        spilled += "[lc: shrunk " in main
        if _normalized(client) != _normalized(main):
            diverged.append((index, command, exit_code))
    assert not diverged, f"{len(diverged)} cases diverge, first: {diverged[:5]}"
    assert spilled >= 50, "the corpus must exercise the trim-and-spill path"
