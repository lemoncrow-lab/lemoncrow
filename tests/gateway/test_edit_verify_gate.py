"""Integration tests for the WS1 edit-loop verify gate wired into tool_smart_edit."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server

_CLEAN_TS = "export const x = 1;\n"
_BROKEN_TS = "export const x = ;;;{\n"


def test_verify_gate_rolls_back_syntax_break(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "mod.ts"
    target.write_text(_CLEAN_TS, encoding="utf-8")

    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {"file_path": "mod.ts", "old_string": "export const x = 1;", "new_string": "export const x = ;;;{"}
            ],
            "verify": True,
            "verify_rollback": True,
        }
    )

    assert result.get("rolled_back") is True
    gate = (result.get("FIXME") or {}).get("mechanical_checks", {})
    assert gate.get("passed") is False
    assert gate.get("rolled_back") is True
    assert "verify" not in result
    failures = gate.get("failures") or []
    assert any(c.get("check") == "parse" for c in failures)
    # File restored to its pre-edit content.
    assert target.read_text(encoding="utf-8") == _CLEAN_TS


def test_verify_gate_passes_clean_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "mod.ts"
    target.write_text(_CLEAN_TS, encoding="utf-8")

    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {"file_path": "mod.ts", "old_string": "export const x = 1;", "new_string": "export const x = 2;"}
            ],
            "verify": True,
            "verify_rollback": True,
        }
    )

    # Silent on pass: a passing gate attaches nothing, so a clean verified edit
    # collapses to the success-silent empty body (only internal calls_saved may
    # remain). The rollback-on-failure path is covered by the syntax-break test.
    assert "mechanical_checks" not in result
    assert "counterexamples" not in result
    assert "FIXME" not in result
    assert "rolled_back" not in result
    assert "verify" not in result
    assert "export const x = 2;" in target.read_text(encoding="utf-8")


def test_verified_edit_with_sibling_cluster_collapses_to_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit that passes the verify gate AND shares a rare identifier cluster with
    a sibling file now collapses to the minimal ``"applied path:line"`` echo.

    Pre-trim this carried a ``mechanical_checks``-pass object (~28 tok) and a
    ``sibling_review`` block (~183 tok). The gate is silent on pass and sibling
    review is hard-removed, so the model-facing body is empty. Verified through the
    dispatcher to assert the rendered token (and confirm calls_saved accounting
    survives a single-file edit -- nothing to credit, so it is simply absent).
    """
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    # Two files sharing a distinctive identifier cluster (the old sibling signal).
    (pkg / "scales.ts").write_text(
        "export function buildLegend(locator: number) {\n  const formatter = locator + 1;\n  return formatter;\n}\n",
        encoding="utf-8",
    )
    (pkg / "utils.ts").write_text(
        "export function locatorToLegend(locator: number) {\n"
        "  const formatter = locator + 2;\n"
        "  return formatter;\n"
        "}\n",
        encoding="utf-8",
    )
    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "edit",
                "arguments": {
                    "edits": [
                        {
                            "file_path": "pkg/scales.ts",
                            "old_string": "  const formatter = locator + 1;",
                            "new_string": "  const formatter = locator + 3;",
                        }
                    ],
                    "verify": True,
                    "post_edit_hooks": False,
                },
            },
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert text == "applied pkg/scales.ts:2", text
    assert "const formatter = locator + 3;" in (pkg / "scales.ts").read_text(encoding="utf-8")


def test_default_path_has_no_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # verify defaults to False and LEMONCROW_EDIT_VERIFY is unset: behaviour unchanged.
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_EDIT_VERIFY", raising=False)
    target = tmp_path / "mod.ts"
    target.write_text(_CLEAN_TS, encoding="utf-8")

    result = mcp_server.tool_smart_edit(
        {"edits": [{"file_path": "mod.ts", "old_string": "export const x = 1;", "new_string": "export const x = ;;;{"}]}
    )

    assert "verify" not in result
    assert "mechanical_checks" not in result
    assert "FIXME" not in result
    assert not result.get("rolled_back")
    # No gate -> the (broken) edit is written through.
    assert target.read_text(encoding="utf-8") == _BROKEN_TS
