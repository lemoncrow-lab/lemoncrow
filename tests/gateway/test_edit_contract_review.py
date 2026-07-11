"""Integration tests: post-edit contract-literal review wired into tool_smart_edit.

When an edit removes a quoted contract literal (config key, wire field, kwarg name),
the edit tool surfaces the remaining occurrences in *other* untouched files so the
agent finishes the rename at every parallel consumer -- the multi-site bug class
where a fix is needed in N code paths but only the handed-to file gets edited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server


def _astgrep_available() -> bool:
    try:
        from lemoncrow.infra.code_intel.astgrep import AstGrepAdapter, AstGrepToolUnavailable

        try:
            AstGrepAdapter(Path(".")).search(pattern='"x"', language="python", limit=1)
        except AstGrepToolUnavailable:
            return False
        return True
    except Exception:  # noqa: BLE001
        return True


_requires_astgrep = pytest.mark.skipif(not _astgrep_available(), reason="ast-grep binary unavailable")

_BASE = "def get_connection_params(d):\n    return {'passwd': d['passwd']}\n"
_CLIENT = "def settings_to_args(d):\n    return ['--password', d['passwd']]\n"


def _setup_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "db" / "base.py").write_text(_BASE, encoding="utf-8")
    (tmp_path / "db" / "client.py").write_text(_CLIENT, encoding="utf-8")


@_requires_astgrep
def test_edit_surfaces_parallel_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_workspace(tmp_path, monkeypatch)
    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": "db/base.py",
                    "old_string": "{'passwd': d['passwd']}",
                    "new_string": "{'password': d['password']}",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    assert not result.get("failed")
    # FIXME is the flat sites list (entries carry path:LN / old / new / snippet);
    # when lint diagnostics also fired it is a dict with a "sites" key.
    review = result.get("FIXME")
    assert review is not None, result
    sites = review["sites"] if isinstance(review, dict) else review
    passwd_sites = [s for s in sites if s["old"] == "passwd"]
    assert passwd_sites
    assert passwd_sites[0]["new"] == "password"
    all_paths = {s["path"].split(":L")[0] for s in sites}
    assert "db/client.py" in all_paths  # parallel consumer surfaced
    assert "db/base.py" not in all_paths  # the edited file is excluded


@_requires_astgrep
def test_off_switch_disables_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_CONTRACT_REVIEW", "0")
    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": "db/base.py",
                    "old_string": "{'passwd': d['passwd']}",
                    "new_string": "{'password': d['password']}",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    assert not result.get("failed")
    assert "FIXME" not in result


@_requires_astgrep
def test_no_literal_removed_attaches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_workspace(tmp_path, monkeypatch)
    # Rename a local variable -- no quoted contract literal is removed.
    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": "db/base.py",
                    "old_string": "def get_connection_params(d):",
                    "new_string": "def get_connection_params(conf):",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    assert not result.get("failed")
    assert "FIXME" not in result


@_requires_astgrep
def test_edit_surfaces_symbol_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Symbol counterpart of the literal pass: renaming a module-level constant must
    # surface the file that still imports/uses the old name (not a quoted literal).
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "config.py").write_text("DEFAULT_TIMEOUT = 30\n", encoding="utf-8")
    (tmp_path / "pkg" / "client.py").write_text(
        "from pkg.config import DEFAULT_TIMEOUT\n\n\ndef connect():\n    return DEFAULT_TIMEOUT\n",
        encoding="utf-8",
    )
    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": "pkg/config.py",
                    "old_string": "DEFAULT_TIMEOUT = 30",
                    "new_string": "DEFAULT_DEADLINE = 30",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    assert not result.get("failed")
    review = result.get("FIXME")
    assert review is not None, result
    sites = review["sites"] if isinstance(review, dict) else review
    symbol_sites = [s for s in sites if s["old"] == "DEFAULT_TIMEOUT"]
    assert symbol_sites, sites
    all_paths = {s["path"].split(":L")[0] for s in symbol_sites}
    assert "pkg/client.py" in all_paths  # parallel consumer surfaced
    assert "pkg/config.py" not in all_paths  # edited file excluded


@_requires_astgrep
def test_edit_surfaces_signature_change_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A def that gains a REQUIRED parameter must surface the call sites in other files
    # that don't yet pass it (the call graph's turn -- the symbol survives the edit).
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "svc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "svc" / "core.py").write_text("def apply_policy(rule):\n    return rule\n", encoding="utf-8")
    (tmp_path / "svc" / "caller.py").write_text(
        "from svc.core import apply_policy\n\n\ndef run():\n    return apply_policy('x')\n",
        encoding="utf-8",
    )
    result = mcp_server.tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": "svc/core.py",
                    "old_string": "def apply_policy(rule):",
                    "new_string": "def apply_policy(rule, scope):",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    assert not result.get("failed")
    review = result.get("FIXME")
    assert review is not None, result
    sites = review["sites"] if isinstance(review, dict) else review
    caller_sites = [s for s in sites if s["old"] == "apply_policy(...)"]
    assert caller_sites, sites
    all_paths = {s["path"].split(":L")[0] for s in caller_sites}
    assert "svc/caller.py" in all_paths  # caller missing the new arg surfaced
    assert "svc/core.py" not in all_paths  # edited file excluded
