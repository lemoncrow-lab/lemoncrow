from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from atelier.gateway.adapters.mcp_server import _handle
from atelier.gateway.cli import cli


def _seed_store(tmp_path: Path, monkeypatch: Any) -> Path:
    root = tmp_path / ".atelier"
    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    return root


def _smart_read(args: dict[str, Any]) -> str:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read", "arguments": args},
    }
    resp = _handle(req)
    assert resp is not None
    assert "result" in resp, resp
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    return text


def test_smart_read_outline_first_for_large_python_file(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "large_module.py"
    lines = ["import os", "", "class Demo:", "    def run(self):", "        return 1", ""]
    lines.extend(f"value_{i} = {i}" for i in range(1, 620))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    outline_md = _smart_read({"path": str(target), "include_meta": True})
    assert "(outline)" in outline_md
    assert "Demo" in outline_md

    full_md = _smart_read({"path": str(target), "expand": True})
    assert "(outline)" not in full_md
    assert "value_619 = 619" in full_md

    # outline is shorter than full read (tokens saved)
    assert len(outline_md) < len(full_md)

    range_md = _smart_read({"path": str(target), "range": "42-118"})
    assert "(42-118)" in range_md
    # range 42-118 contains value_36..value_112 = 77 value_ lines
    value_lines = [ln for ln in range_md.splitlines() if ln.startswith("value_")]
    assert len(value_lines) == 77


def test_smart_read_tolerates_open_ended_and_malformed_end_ranges(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "range_target.py"
    target.write_text("\n".join(f"line_{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

    open_ended = _smart_read({"path": str(target), "range": "L6-"})
    assert "(6-10)" in open_ended
    parts = open_ended.split("```")
    assert len(parts) >= 3
    content_lines = parts[1].splitlines()[1:]  # skip language identifier line
    assert content_lines == [f"line_{i}" for i in range(6, 11)]

    malformed_end = _smart_read({"path": str(target), "range": "L6-foo"})
    assert "(6-10)" in malformed_end
    parts = malformed_end.split("```")
    assert len(parts) >= 3
    content_lines = parts[1].splitlines()[1:]
    assert content_lines == [f"line_{i}" for i in range(6, 11)]


def test_smart_read_small_file_defaults_to_full(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "small.py"
    target.write_text("def ping():\n    return 'pong'\n", encoding="utf-8")

    payload = _smart_read({"path": str(target)})
    assert "(outline)" not in payload
    assert "def ping()" in payload
