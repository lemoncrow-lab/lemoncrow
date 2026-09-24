"""Client ``bash`` and the main package's bash apply the same command policy.

Both call ``lemoncrow_client.kit.command_policy``; the main package only adds
its external compactors through the ``fallback`` hook. This pins that: with
compactors off the two decide identically, and a file read the policy runs
in-process shows the model the same text on both surfaces.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.kit.command_policy import classify_command as kit_classify_command
from lemoncrow_client.localtools import LocalContext, executor_for

from lemoncrow.gateway.tools.rendering import render_bash_text
from lemoncrow.pro.capabilities.tool_supervision import bash_exec

_COMMANDS = (
    "git reset --hard",
    "git -C sub clean -fdx",
    "sudo timeout 5 git reset --hard",
    "echo ok && git clean -fd",
    "echo $(git reset --hard)",
    "bash -c 'git reset --hard'",
    "eval 'git clean -fd'",
    "bash danger.sh",
    "bash ok.sh",
    "bash -n danger.sh",
    "cat > /nonexistent-lemoncrow-dir/x.txt",
    "cat > notes.txt",
    "python -c \"open('/nonexistent-lemoncrow-dir/x','w')\"",
    "python -c \"open('notes.txt','w').write('x')\"",
    "cat small.txt",
    "cat -n small.txt nonl.txt",
    "head -n 3 small.txt",
    "tail -5 small.txt",
    "wc -lw small.txt",
    "sed -n '2,4p' small.txt",
    "grep -rn line .",
    "rg -i line",
    "find . -name '*.txt' -type f",
    "curl https://example.com",
    "od -c big.bin | tail -n 2",
    "ls -la",
    "git status",
    "pytest -q",
)
_SPILL_PATH = re.compile(r"full: [^\]\n]+")
_INLINE = (
    "cat small.txt nonl.txt",
    "cat -n small.txt",
    "cat -b blank.txt",
    "head -n 3 small.txt",
    "head -3 nonl.txt",
    "tail -n 2 small.txt",
    "tail nonl.txt",
    "wc small.txt",
    "wc -l small.txt",
    "wc -c nonl.txt",
    "head -n 2 missing.txt",
    "tail -n 400 long.txt",
    "cat long.txt small.txt",
)


def _fixtures(root: Path) -> None:
    (root / ".git").mkdir(parents=True)
    (root / "small.txt").write_text("".join(f"line {i}\n" for i in range(1, 21)), encoding="utf-8")
    (root / "nonl.txt").write_text("a\nb\nc", encoding="utf-8")
    (root / "blank.txt").write_text("a\n\nb\n\n\nc\n", encoding="utf-8")
    (root / "long.txt").write_text("".join(f"row {i} " + "x" * (i % 70) + "\n" for i in range(3000)), encoding="utf-8")
    (root / "danger.sh").write_text("echo start\ngit reset --hard\n", encoding="utf-8")
    (root / "ok.sh").write_text("echo hi\n", encoding="utf-8")
    with open(root / "big.bin", "wb") as handle:
        handle.truncate(9 * 1024 * 1024)


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEMONCROW_BASH_EXTERNAL_COMPACTORS", "0")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.setenv("LEMONCROW_BASH_UNCHANGED_DELTA", "0")
    repo = tmp_path / "repo"
    _fixtures(repo)
    return repo


def _combined(rng: random.Random) -> str:
    parts = [rng.choice(_COMMANDS) for _ in range(rng.randint(1, 3))]
    command = parts[0]
    for part in parts[1:]:
        command += rng.choice((" && ", "; ", " | ", " || ", "\n")) + part
    return command


def test_both_surfaces_decide_every_command_the_same_way(root: Path) -> None:
    rng = random.Random(20260922)
    corpus = [*_COMMANDS, *(_combined(rng) for _ in range(400))]
    blocked = 0
    for command in corpus:
        main = bash_exec.classify_command(command, allowed_write_roots=[root], cwd=str(root))
        client = kit_classify_command(command, allowed_write_roots=[root], cwd=root)
        assert client == main, command
        blocked += main.action == "block"
    assert blocked >= 100, "the corpus must exercise the block rules"


def test_in_process_file_reads_show_the_same_text(root: Path, tmp_path: Path) -> None:
    state = tmp_path / "home" / "lemoncrow"
    state.mkdir(parents=True)
    config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=root)
    context = LocalContext(config=config, repo_root=root, sync=None, environment={})
    for command in _INLINE:
        result = bash_exec.run_command(command, cwd=str(root))
        assert result.rewrite_target in {"cat", "head", "tail", "wc"}, command
        main = render_bash_text(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "truncated": result.truncated,
                "lines_omitted": result.lines_omitted,
                "chars_omitted": result.chars_omitted,
                "spill_hint": result.spill_hint,
            }
        ).removeprefix(f"exit_code={result.exit_code}")
        client_text = executor_for("bash")(context, {"command": command}).content[0]["text"]
        client = client_text.removesuffix(f"[exit {result.exit_code}]")
        assert _SPILL_PATH.sub("full: PATH", client).strip() == _SPILL_PATH.sub("full: PATH", main).strip(), command
