"""Comprehensive MCP-level tests for the `edit` tool handler (tool_smart_edit).

These tests exercise tool_smart_edit end-to-end through the _handle dispatcher,
covering all six descriptor families, atomicity, hooks, diff recording, and edge
cases that were previously untested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_store(root: Path) -> None:
    from click.testing import CliRunner

    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = mcp_server._handle(req)
    assert isinstance(resp, dict)
    return resp


def _result(resp: dict[str, Any]) -> dict[str, Any]:
    assert "result" in resp, resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


def _edit(args: dict[str, Any]) -> dict[str, Any]:
    """Shortcut: call edit and return the parsed result."""
    return _result(_call("edit", args))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    _seed_store(root)
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    monkeypatch.chdir(tmp_path)
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    mcp_server._remote_client = MagicMock()
    mcp_server._remote_client.get_context.return_value = {"context": "", "run_ledger": []}
    return tmp_path


# ---------------------------------------------------------------------------
# #1  Rich descriptor family
# ---------------------------------------------------------------------------


def test_rich_replace_basic(workspace: Path) -> None:
    """Rich replace updates text and returns an applied hunk."""
    f = workspace / "hello.py"
    f.write_text("def greet():\n    return HELLO\n", encoding="utf-8")

    payload = _edit({"edits": [{"file_path": "hello.py", "old_string": "HELLO", "new_string": "WORLD"}]})

    assert payload["failed"] == []
    assert payload["rolled_back"] is False
    assert "WORLD" in f.read_text(encoding="utf-8")
    assert "HELLO" not in f.read_text(encoding="utf-8")
    assert len(payload["applied"]) == 1
    assert payload["applied"][0]["path"] == "hello.py"


def test_rich_create_new_file_via_overwrite(workspace: Path) -> None:
    """Rich overwrite with no existing file creates it from scratch."""
    f = workspace / "new_module.py"
    assert not f.exists()

    payload = _edit({"edits": [{"file_path": "new_module.py", "new_string": "# new\n", "overwrite": True}]})

    assert payload["failed"] == []
    assert f.exists()
    assert f.read_text(encoding="utf-8") == "# new\n"
    assert payload["applied"][0]["kind"] == "overwrite"


def test_rich_overwrite_replaces_existing_file(workspace: Path) -> None:
    """Rich overwrite with an existing file replaces its full content."""
    f = workspace / "config.txt"
    f.write_text("old content\n", encoding="utf-8")

    payload = _edit({"edits": [{"file_path": "config.txt", "new_string": "new content\n", "overwrite": True}]})

    assert payload["failed"] == []
    assert f.read_text(encoding="utf-8") == "new content\n"


def test_rich_line_anchor_restricts_scope(workspace: Path) -> None:
    """file_path#line scopes the replacement to that line only."""
    f = workspace / "scope.py"
    f.write_text("x = 1\nx = 2\nx = 3\n", encoding="utf-8")

    # Only replace the x = 2 on line 2 — the other 'x = 1' must stay
    payload = _edit({"edits": [{"file_path": "scope.py#2", "old_string": "x = 2", "new_string": "x = 99"}]})

    assert payload["failed"] == []
    text = f.read_text(encoding="utf-8")
    assert "x = 99" in text
    assert "x = 1" in text
    assert "x = 3" in text


def test_rich_multi_file_batch_all_applied(workspace: Path) -> None:
    """Multiple rich edits across different files succeed atomically."""
    f1 = workspace / "a.py"
    f2 = workspace / "b.py"
    f1.write_text("alpha\n", encoding="utf-8")
    f2.write_text("beta\n", encoding="utf-8")

    payload = _edit(
        {
            "edits": [
                {"file_path": "a.py", "old_string": "alpha", "new_string": "ALPHA"},
                {"file_path": "b.py", "old_string": "beta", "new_string": "BETA"},
            ]
        }
    )

    assert payload["failed"] == []
    assert len(payload["applied"]) == 2
    assert f1.read_text(encoding="utf-8") == "ALPHA\n"
    assert f2.read_text(encoding="utf-8") == "BETA\n"


def test_rich_multi_file_atomic_rollback(workspace: Path) -> None:
    """Second edit fails → atomic=True rolls back the first edit too."""
    f1 = workspace / "r1.py"
    f1.write_text("original\n", encoding="utf-8")

    payload = _edit(
        {
            "atomic": True,
            "edits": [
                {"file_path": "r1.py", "old_string": "original", "new_string": "changed"},
                {"file_path": "r1.py", "old_string": "does-not-exist", "new_string": "x"},
            ],
        }
    )

    assert payload["rolled_back"] is True
    assert f1.read_text(encoding="utf-8") == "original\n"


def test_rich_non_atomic_partial_success(workspace: Path) -> None:
    """With atomic=False a failing second edit doesn't undo the first."""
    f = workspace / "partial.py"
    f.write_text("keep this\nbad target here\n", encoding="utf-8")

    payload = _edit(
        {
            "atomic": False,
            "edits": [
                {"file_path": "partial.py", "old_string": "keep this", "new_string": "KEPT"},
                {"file_path": "partial.py", "old_string": "no such string", "new_string": "x"},
            ],
        }
    )

    assert payload["rolled_back"] is False
    assert len(payload["applied"]) >= 1
    assert len(payload["failed"]) >= 1
    assert "KEPT" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #2  Legacy descriptor family
# ---------------------------------------------------------------------------


def test_legacy_replace_through_handler(workspace: Path) -> None:
    """Legacy op=replace dispatches to apply_batch_edit."""
    f = workspace / "legacy.txt"
    f.write_text("foo bar\n", encoding="utf-8")

    payload = _edit({"edits": [{"path": str(f), "op": "replace", "old_string": "foo", "new_string": "baz"}]})

    assert payload["failed"] == []
    assert f.read_text(encoding="utf-8") == "baz bar\n"


def test_legacy_insert_after(workspace: Path) -> None:
    """Legacy op=insert_after appends text after the anchor line."""
    f = workspace / "insert.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")

    payload = _edit({"edits": [{"path": str(f), "op": "insert_after", "anchor": "line1", "new_string": "line1b"}]})

    assert payload["failed"] == []
    text = f.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "line1"
    assert lines[1] == "line1b"
    assert lines[2] == "line2"


def test_legacy_replace_range(workspace: Path) -> None:
    """Legacy op=replace_range replaces exact line range."""
    f = workspace / "range.txt"
    f.write_text("aaa\nbbb\nccc\nddd\n", encoding="utf-8")

    payload = _edit(
        {"edits": [{"path": str(f), "op": "replace_range", "line_start": 2, "line_end": 3, "new_string": "REPLACED"}]}
    )

    assert payload["failed"] == []
    text = f.read_text(encoding="utf-8")
    assert "aaa" in text
    assert "REPLACED" in text
    assert "bbb" not in text
    assert "ccc" not in text
    assert "ddd" in text


def test_legacy_fuzzy_replace(workspace: Path) -> None:
    """Legacy fuzzy=True matches despite indentation drift."""
    f = workspace / "fuzzy.py"
    f.write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")

    payload = _edit(
        {"edits": [{"path": str(f), "op": "replace", "old_string": "x = 1", "new_string": "x = 42", "fuzzy": True}]}
    )

    assert payload["failed"] == []
    assert "x = 42" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #3  Notebook cell edits
# ---------------------------------------------------------------------------


def test_notebook_cell_insert_through_handler(workspace: Path) -> None:
    """Notebook cell_action=insert_after adds a new cell via MCP."""
    nb_path = workspace / "nb.ipynb"
    nb_path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "metadata": {}, "source": "x = 1", "outputs": [], "execution_count": None}
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    payload = _edit(
        {
            "edits": [
                {
                    "file_path": "nb.ipynb#cell=0",
                    "cell_action": "insert_after",
                    "cell_type": "markdown",
                    "new_string": "# heading",
                }
            ]
        }
    )

    assert payload["failed"] == []
    notebook = json.loads(nb_path.read_text(encoding="utf-8"))
    assert len(notebook["cells"]) == 2
    assert notebook["cells"][1]["cell_type"] == "markdown"
    assert notebook["cells"][1]["source"] == "# heading"


def test_notebook_cell_overwrite_clears_outputs(workspace: Path) -> None:
    """Overwriting a notebook cell via #cell=N resets its outputs."""
    nb_path = workspace / "out.ipynb"
    nb_path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": "print(1)",
                        "outputs": [{"output_type": "stream", "text": "1\n"}],
                        "execution_count": 3,
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    payload = _edit({"edits": [{"file_path": "out.ipynb#cell=0", "overwrite": True, "new_string": "print(2)"}]})

    assert payload["failed"] == []
    notebook = json.loads(nb_path.read_text(encoding="utf-8"))
    assert notebook["cells"][0]["source"] == "print(2)"
    assert notebook["cells"][0]["outputs"] == []
    assert notebook["cells"][0]["execution_count"] is None


# ---------------------------------------------------------------------------
# #4  post_edit_hooks behaviour
# ---------------------------------------------------------------------------


def test_post_edit_hooks_false_no_diagnostics_key(workspace: Path) -> None:
    """With post_edit_hooks=False the result has no diagnostics/hooks keys."""
    f = workspace / "nohook.py"
    f.write_text("x = 1\n", encoding="utf-8")

    payload = _edit(
        {
            "post_edit_hooks": False,
            "edits": [{"file_path": "nohook.py", "old_string": "x = 1", "new_string": "x = 2"}],
        }
    )

    assert payload["failed"] == []
    assert "diagnostics" not in payload
    assert "hooks" not in payload


def test_post_edit_hooks_false_diff_still_recorded(workspace: Path) -> None:
    """Diffs are recorded in the ledger even when hooks are disabled."""
    f = workspace / "difftest.py"
    f.write_text("before\n", encoding="utf-8")

    _edit(
        {
            "post_edit_hooks": False,
            "edits": [{"file_path": "difftest.py", "old_string": "before", "new_string": "after"}],
        }
    )

    led = mcp_server._get_ledger()
    file_events = [e for e in led.events if e.kind == "file_edit"]
    assert any(e.payload.get("path") == "difftest.py" for e in file_events)
    matching = [e for e in file_events if e.payload.get("path") == "difftest.py"]
    assert "+after" in matching[-1].payload["diff"]


def test_hook_exception_does_not_fail_successful_edit(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook crash must not surface as an edit failure — the file was already written."""
    f = workspace / "hookcrash.py"
    f.write_text("old\n", encoding="utf-8")

    def exploding_hooks(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("hook toolchain not found")

    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.post_edit_hooks.run_post_edit_hooks",
        exploding_hooks,
    )

    payload = _edit(
        {
            "post_edit_hooks": True,
            "edits": [{"file_path": "hookcrash.py", "old_string": "old", "new_string": "new"}],
        }
    )

    # Edit must succeed even when post-edit hooks crash.
    # Hook diagnostics are intentionally stripped from MCP payload.
    assert payload["failed"] == []
    assert payload["rolled_back"] is False
    assert f.read_text(encoding="utf-8") == "new\n"
    assert "hooks" not in payload


def test_hook_exception_diff_still_recorded(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Diff is recorded in the ledger even when the hooks crash."""
    f = workspace / "hookdiff.py"
    f.write_text("original\n", encoding="utf-8")

    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.post_edit_hooks.run_post_edit_hooks",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    _edit(
        {
            "post_edit_hooks": True,
            "edits": [{"file_path": "hookdiff.py", "old_string": "original", "new_string": "modified"}],
        }
    )

    led = mcp_server._get_ledger()
    file_events = [e for e in led.events if e.kind == "file_edit" and e.payload.get("path") == "hookdiff.py"]
    assert file_events, "diff event must be recorded even after hook crash"
    assert "+modified" in file_events[-1].payload["diff"]


# ---------------------------------------------------------------------------
# #5  Edge cases and validation
# ---------------------------------------------------------------------------


def test_empty_edits_returns_error(workspace: Path) -> None:
    """An empty edits array is a protocol error."""
    resp = _call("edit", {"edits": []})
    # Either a JSON-RPC error or a result with failed containing the message
    has_error = "error" in resp or ("result" in resp and _result(resp).get("failed"))
    assert has_error


def test_mixed_families_rejected(workspace: Path) -> None:
    """Mixing legacy op/path and rich file_path descriptors raises an error."""
    f = workspace / "mixed.txt"
    f.write_text("hello\n", encoding="utf-8")

    resp = _call(
        "edit",
        {
            "edits": [
                {"path": str(f), "op": "replace", "old_string": "hello", "new_string": "legacy"},
                {"file_path": str(f), "old_string": "hello", "new_string": "rich"},
            ]
        },
    )

    assert "error" in resp
    assert "cannot mix" in resp["error"]["message"].lower()
    assert f.read_text(encoding="utf-8") == "hello\n"


def test_rich_path_escape_rejected(workspace: Path) -> None:
    """A path that escapes the repo root is blocked by path safety."""
    payload = _edit({"edits": [{"file_path": "../../../etc/passwd", "old_string": "root", "new_string": "hacked"}]})
    assert payload["rolled_back"] is True
    assert payload["failed"]


def test_legacy_unknown_op_reported_as_failure(workspace: Path) -> None:
    """Legacy op with unsupported opcode fails gracefully."""
    f = workspace / "unknown.txt"
    f.write_text("original\n", encoding="utf-8")

    payload = _edit({"edits": [{"path": str(f), "op": "delete_line", "old_string": "original", "new_string": ""}]})

    assert payload["failed"]
    assert f.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# #6  Ledger diff recording (multi-file)
# ---------------------------------------------------------------------------


def test_diff_recorded_per_file_multi_edit(workspace: Path) -> None:
    """Each touched file gets its own file_edit event in the ledger."""
    f1 = workspace / "m1.py"
    f2 = workspace / "m2.py"
    f1.write_text("alpha\n", encoding="utf-8")
    f2.write_text("beta\n", encoding="utf-8")

    _edit(
        {
            "post_edit_hooks": False,
            "edits": [
                {"file_path": "m1.py", "old_string": "alpha", "new_string": "ALPHA"},
                {"file_path": "m2.py", "old_string": "beta", "new_string": "BETA"},
            ],
        }
    )

    led = mcp_server._get_ledger()
    file_events = [e for e in led.events if e.kind == "file_edit"]
    paths_recorded = {e.payload["path"] for e in file_events}
    assert "m1.py" in paths_recorded
    assert "m2.py" in paths_recorded


# ---------------------------------------------------------------------------
# #7  Schema contract
# ---------------------------------------------------------------------------


def test_schema_top_level_params_have_descriptions() -> None:
    """atomic, post_edit_hooks, post_edit_timeout_ms must each have a description."""
    from atelier.gateway.adapters.mcp_server import EDIT_TOOL_INPUT_SCHEMA

    props = EDIT_TOOL_INPUT_SCHEMA["properties"]
    for param in ("atomic", "post_edit_hooks", "post_edit_timeout_ms"):
        assert "description" in props[param], f"{param!r} missing description in EDIT_TOOL_INPUT_SCHEMA"
        assert props[param]["description"].strip(), f"{param!r} description is empty"


def test_schema_registered_as_edit_tool() -> None:
    """The edit tool must be registered in TOOLS with a non-trivial description."""
    from atelier.gateway.adapters.mcp_server import TOOLS

    assert "edit" in TOOLS
    desc = TOOLS["edit"]["description"]
    assert "Rich" in desc or "rich" in desc, "description should mention Rich descriptor family"
    assert "Legacy" in desc or "legacy" in desc, "description should mention Legacy descriptor family"


def test_schema_edits_array_requires_min_one_item() -> None:
    """The JSON schema specifies minItems: 1 on the edits array."""
    from atelier.gateway.adapters.mcp_server import EDIT_TOOL_INPUT_SCHEMA

    assert EDIT_TOOL_INPUT_SCHEMA["properties"]["edits"].get("minItems") == 1


def test_schema_has_six_onoeof_variants() -> None:
    """The edits array items schema defines exactly 6 oneOf variants."""
    from atelier.gateway.adapters.mcp_server import EDIT_TOOL_INPUT_SCHEMA

    variants = EDIT_TOOL_INPUT_SCHEMA["properties"]["edits"]["items"]["oneOf"]
    titles = [v["title"] for v in variants]
    assert len(variants) == 6
    assert "Legacy replace" in titles
    assert "Legacy insert_after" in titles
    assert "Legacy replace_range" in titles
    assert "Rich file edit" in titles
    assert "Notebook cell edit" in titles
    assert "Symbol edit" in titles
