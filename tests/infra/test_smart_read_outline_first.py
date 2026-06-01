from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from atelier.core.capabilities.semantic_file_memory.capability import (
    _claude_read_baseline_text,
    _count_tokens,
)
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
    assert "hint:" in outline_md
    assert "Demo" in outline_md

    full_md = _smart_read({"path": str(target), "expand": True})
    assert "hint:" not in full_md
    assert "value_619 = 619" in full_md

    # outline is shorter than full read (tokens saved)
    assert len(outline_md) < len(full_md)

    range_md = _smart_read({"path": str(target), "range": "42-118"})
    # range 42-118 contains value_36..value_112 = 77 value_ lines
    value_lines = [ln for ln in range_md.splitlines() if ln.startswith("value_")]
    assert len(value_lines) == 77


def test_smart_read_tolerates_open_ended_and_malformed_end_ranges(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "range_target.py"
    target.write_text("\n".join(f"line_{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

    open_ended = _smart_read({"path": str(target), "range": "L6-"})
    expected = [f"line_{i}" for i in range(6, 11)]
    for line in expected:
        assert line in open_ended

    malformed_end = _smart_read({"path": str(target), "range": "L6-foo"})
    for line in expected:
        assert line in malformed_end


def test_smart_read_small_file_defaults_to_full(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "small.py"
    target.write_text("def ping():\n    return 'pong'\n", encoding="utf-8")

    payload = _smart_read({"path": str(target)})
    assert "(outline)" not in payload
    assert "def ping()" in payload


def test_smart_read_range_claims_no_savings_against_builtin_range_read(tmp_path: Path) -> None:
    target = tmp_path / "range_target.py"
    target.write_text("\n".join(f"value_{idx} = {idx}" for idx in range(300)), encoding="utf-8")

    payload = SemanticFileMemoryCapability(tmp_path).smart_read(target, range_spec="10-20")

    assert payload["mode"] == "range"


def test_smart_read_large_file_savings_use_claude_read_cap(tmp_path: Path) -> None:
    target = tmp_path / "large_module.py"
    source = "\n".join(
        ["class Demo:", "    def run(self):", "        return 1"] + [f"value_{idx} = {idx}" for idx in range(2600)]
    )
    target.write_text(source, encoding="utf-8")

    payload = SemanticFileMemoryCapability(tmp_path).smart_read(target, outline_threshold=10)
    baseline_tokens = _count_tokens(_claude_read_baseline_text(source))
    full_file_tokens = _count_tokens(source)

    assert payload["mode"] == "outline"
    assert payload["tokens_saved"] <= baseline_tokens
    assert payload["tokens_saved"] < full_file_tokens
