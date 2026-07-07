from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from atelier.gateway.cli import cli


def _invoke(root: Path, *args: str) -> tuple[int, str]:
    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(root), *args])
    return res.exit_code, res.output


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `init` bootstraps a code index over cwd when it is a git repo. Without an
    # isolated cwd the CliRunner inherits the atelier repo and init spends
    # 14-37s indexing the whole codebase. Both tests here drive the actual work
    # through --workspace, so cwd only needs to point away from the atelier repo.
    monkeypatch.chdir(tmp_path)


def test_search_smart_blocks(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    code, out = _invoke(root, "init")
    assert code == 0, out
    target = tmp_path / "shopify.txt"
    target.write_text("shopify publish retry guidance\n", encoding="utf-8")

    code, out = _invoke(
        root,
        "tools",
        "call",
        "grep",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "path": ".",
                "content_regex": "shopify",
                "file_glob_patterns": ["*.txt"],
            }
        ),
        "--json",
    )
    assert code == 0, out
    assert "shopify" in out.lower()


def test_read_smart_and_edit_smart(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    code, out = _invoke(root, "init")
    assert code == 0, out

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)

    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

    subprocess.run(["git", "add", "module.py"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True)

    code, out = _invoke(
        root,
        "tools",
        "call",
        "read",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps({"path": str(target), "max_lines": 20}),
        "--json",
    )
    assert code == 0, out
    # If the tool returned the content directly as a string, just check the content
    assert "def f():" in out
    assert "return 1" in out

    code, out = _invoke(
        root,
        "tools",
        "call",
        "edit",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "edits": [
                    {
                        "path": str(target),
                        "op": "replace",
                        "old_string": "return 1",
                        "new_string": "return 2",
                    }
                ]
            }
        ),
        "--json",
    )
    assert code == 0, out
    # The output might be multiline. Let's try to parse the entire output if possible,
    # or handle the multiline JSON properly.
    try:
        edit_payload = json.loads(out)
    except json.JSONDecodeError:
        # If it failed, maybe there are warnings at the start.
        # Try to find the first '{' and parse from there.
        start = out.find("{")
        if start == -1:
            pytest.fail(f"Could not find JSON in output: {out}")
        edit_payload = json.loads(out[start:])

    # A clean exact-match edit echoes the minimal applied range (orientation only);
    # the file change itself is the confirmation, no diff body.
    assert edit_payload.get("applied")
    assert "failed" not in edit_payload
    assert "return 2" in target.read_text(encoding="utf-8")
