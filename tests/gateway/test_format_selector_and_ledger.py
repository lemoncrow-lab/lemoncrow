"""End-to-end G13 (format selector) + N4 (token ledger) through the MCP dispatch.

Verifies that the ``format`` selector is honored at the tool boundary, that the
default (``auto``) path is byte-compatible with no-selector calls, and that the
per-tool token ledger is populated by real tool invocations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.tool_token_ledger import load_tool_token_ledger
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp_server import _handle
from lemoncrow.pro.capabilities.tool_supervision.compact_output import columnar_decode
from tests.helpers import init_store_at


@pytest.fixture()
def store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_STORE_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_SERVICE_URL", raising=False)
    mcp_server._ledger._current_ledger = None
    mcp_server._ledger._realtime_ctx = None
    return root


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    resp = _handle(req)
    assert isinstance(resp, dict)
    return resp


def _text(resp: dict[str, Any]) -> str:
    assert "result" in resp, resp
    return str(resp["result"]["content"][0]["text"])


def _grep_repo(tmp_path: Path) -> Path:
    target = tmp_path / "sample.py"
    target.write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")
    return target


def test_format_selector_unpublished_but_handler_honors_it(store_root: Path) -> None:
    _ = store_root
    # `format` is a power/CLI/benchmark knob: `auto` (default) already picks the
    # optimal encoding, so it is NOT published in the LLM-facing schema -- that
    # saves resident schema tokens every turn on the busiest tools. The handler
    # still accepts it (it stays in the signature; the dispatcher reads
    # args["format"]), as the byte-compat and json tests below verify.
    for tool in ("read", "search", "grep"):
        props = mcp_server.TOOLS[tool]["inputSchema"]["properties"]
        assert "format" not in props, tool


def test_default_is_byte_compatible_with_explicit_auto(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = _grep_repo(tmp_path)
    args = {"path": str(target), "content_regex": "needle"}
    no_selector = _text(_call("grep", dict(args)))
    explicit_auto = _text(_call("grep", {**args, "format": "auto"}))
    assert no_selector == explicit_auto


def test_format_json_is_accepted_and_returns_json(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = _grep_repo(tmp_path)
    text = _text(_call("grep", {"path": str(target), "content_regex": "needle", "format": "json"}))
    # `json` forces a parseable JSON document (the raw structured result).
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


def test_format_compact_round_trips_for_redundant_rows(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    # Many files each containing the needle -> a redundant row list grep returns
    # under `matches`, which compact encoding can columnar-pack.
    for i in range(40):
        (tmp_path / f"mod_{i}.py").write_text("x = 'needle'\n", encoding="utf-8")
    args = {"path": str(tmp_path), "content_regex": "needle"}
    auto_text = _text(_call("grep", dict(args)))
    compact_text = _text(_call("grep", {**args, "format": "compact"}))
    try:
        payload = json.loads(compact_text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("encoding") == "columnar":
        # Compact form cleared the N6 gate: it must be smaller and reversible.
        assert len(compact_text) < len(auto_text)
        rows = columnar_decode(payload["data"])
        assert isinstance(rows, list) and rows
    else:
        # Below the gate threshold: compact safely fell back to the auto text
        # (never inflated). This is the N6 guard working as intended.
        # rg uses parallel workers, so file order is non-deterministic; compare sorted file lists.
        compact_files = sorted(line for line in compact_text.splitlines() if line.endswith(".py"))
        auto_files = sorted(line for line in auto_text.splitlines() if line.endswith(".py"))
        assert compact_files == auto_files, "compact must not drop or add files vs auto"
        assert len(compact_text) <= len(auto_text) + 200, "compact must not inflate result"


def test_ledger_records_per_tool_in_out_tokens(store_root: Path, tmp_path: Path) -> None:
    target = _grep_repo(tmp_path)
    _call("grep", {"path": str(target), "content_regex": "needle"})
    _call("read", {"path": str(target)})
    # Second read via symbol lookup — 'force' was removed from the tool schema.
    _call("read", {"symbol": "alpha", "files": [str(target)]})

    ledger = load_tool_token_ledger(store_root)
    assert ledger.per_tool["grep"].calls == 1
    assert ledger.per_tool["read"].calls == 2
    assert ledger.per_tool["grep"].input_tokens > 0
    assert ledger.per_tool["grep"].output_tokens > 0
    assert ledger.per_tool["read"].input_tokens > 0
    assert ledger.total_calls() == 3
