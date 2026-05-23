from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.adapters.cli import cli


def _invoke(root: Path, *args: str) -> tuple[int, str]:
    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(root), *args])
    return res.exit_code, res.output


def test_bench_runtime(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    code, out = _invoke(root, "init")
    assert code == 0, out

    code, out = _invoke(root, "benchmark", "runtime", "--json")
    assert code == 0, out
    metrics = json.loads(out)
    assert "total_tool_calls" in metrics


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

    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

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
    payload = json.loads(out)
    assert payload["language"] == "python"

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
    edit_payload = json.loads(out)
    assert len(edit_payload["applied"]) == 1
    assert "return 2" in target.read_text(encoding="utf-8")
