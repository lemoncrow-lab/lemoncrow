"""Tests for the stdio MCP single-workspace code warmer (Workstream 6 / G10)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lemoncrow.core.service import code_warm


@pytest.fixture(autouse=True)
def _reset_stdio_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level stdio warmer state and enable warming."""
    monkeypatch.delenv("LEMONCROW_SERVICE_CODE_WARM", raising=False)
    monkeypatch.setattr(code_warm, "_stdio_warmed", None, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_fired_workspaces: list[Path] = []


def _patch_fire(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace _fire_index_subprocess with a no-op that records calls."""
    fired: list[Path] = []

    def _fake(workspace: Path) -> None:
        fired.append(workspace)

    monkeypatch.setattr(code_warm, "_fire_index_subprocess", _fake)
    return fired


# ---------------------------------------------------------------------------
# warm_stdio_workspace tests
# ---------------------------------------------------------------------------


def test_warm_invoked_once_per_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fired = _patch_fire(monkeypatch)

    assert code_warm.warm_stdio_workspace(tmp_path) is True
    assert len(fired) == 1

    # Second call for the same workspace is a no-op: subprocess fired exactly once.
    assert code_warm.warm_stdio_workspace(tmp_path) is False
    assert len(fired) == 1


def test_warm_skips_ephemeral_workspace_with_existing_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Temp workspaces are indexed once, never re-warmed (issue: daemon
    restarts re-fired multi-hour index builds for /tmp bench clones)."""
    fired = _patch_fire(monkeypatch)
    ws = tmp_path / "idx_ws_bench"
    ws.mkdir()
    db_dir = ws.resolve() / ".lemoncrow" / "workspace"
    db_dir.mkdir(parents=True)
    (db_dir / "code_context.sqlite").touch()
    assert code_warm.warm_stdio_workspace(ws) is False  # already indexed: skip
    assert fired == []

    ws2 = tmp_path / "idx_ws_fresh"
    ws2.mkdir()
    code_warm._stdio_warmed = None
    assert code_warm.warm_stdio_workspace(ws2) is True  # first index still allowed
    assert fired == [ws2.resolve()]


def test_warm_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _boom(workspace: Path) -> None:
        raise RuntimeError("index subprocess exploded")

    monkeypatch.setattr(code_warm, "_fire_index_subprocess", _boom)

    # Must NOT raise -- stdio startup must survive a warming failure.
    assert code_warm.warm_stdio_workspace(tmp_path) is False
    assert code_warm._stdio_warmed is None


def test_warm_disabled_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fired = _patch_fire(monkeypatch)
    monkeypatch.setenv("LEMONCROW_SERVICE_CODE_WARM", "0")

    assert code_warm.warm_stdio_workspace(tmp_path) is False
    assert len(fired) == 0


def test_warm_skips_missing_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fired = _patch_fire(monkeypatch)
    missing = tmp_path / "does-not-exist"

    assert code_warm.warm_stdio_workspace(missing) is False
    assert len(fired) == 0


def test_prepare_stdio_workspace_waits_for_incremental_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Completed:
        returncode = 0
        stdout = (
            json.dumps(
                {
                    "files_indexed": 2,
                    "generation": 3,
                    "cold": True,
                    "index_path": str(tmp_path / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3"),
                }
            )
            + "\n"
        )
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> _Completed:
        calls.append((command, kwargs))
        return _Completed()

    monkeypatch.setattr(code_warm.subprocess, "run", fake_run)
    report = code_warm.prepare_stdio_workspace(tmp_path)

    assert report["ok"] is True
    assert report["cold"] is True
    assert report["files_indexed"] == 2
    assert report["generation"] == 3
    assert report["index_version"] == 3
    assert report["index_path"] == str(tmp_path / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3")
    command, kwargs = calls[0]
    assert command[-6:] == [
        "runtime",
        "index",
        "--repo-root",
        str(tmp_path.resolve()),
        "--no-stats",
        "--json",
    ]
    assert "--reindex" not in command
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False


def test_prepare_stdio_workspace_real_index_is_persistent_and_incremental(tmp_path: Path) -> None:
    import subprocess

    from lemoncrow_client.lite.index import LiteIndex
    from lemoncrow_client.lite.mcp import LiteMcp

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "trial.py"
    source.write_text("def alpha_trial_symbol():\n    return 1\n", encoding="utf-8")

    first = code_warm.prepare_stdio_workspace(tmp_path)
    assert first["ok"] is True, first
    db = tmp_path / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3"
    assert db.is_file()
    index = LiteIndex(tmp_path)
    assert any(hit["name"] == "alpha_trial_symbol" for hit in index.exact_symbols("alpha_trial_symbol"))

    source.write_text(
        "def alpha_trial_symbol():\n    return 1\n\ndef beta_trial_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    second = code_warm.prepare_stdio_workspace(tmp_path)
    assert second["ok"] is True, second
    assert second["cold"] is False
    assert int(second["files_indexed"]) == 1
    index = LiteIndex(tmp_path)
    assert any(hit["name"] == "beta_trial_symbol" for hit in index.exact_symbols("beta_trial_symbol"))

    # The actual Runtime Lite MCP surface consumes the same persistent index.
    runtime = LiteMcp(environment={"LEMONCROW_WORKSPACE_ROOT": str(tmp_path), "LEMONCROW_LITE_PREPARED": "1"})
    try:
        result = runtime.call("code_search", {"query": "beta_trial_symbol", "limit": 8})
        structured = result.get("structuredContent") or {}
        assert any(item.get("path") == "trial.py" for item in structured.get("files", []))
    finally:
        runtime.close()


def test_stdio_warm_prepared_trial_skips_duplicate_index_but_warms_serving_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    from lemoncrow.gateway.adapters import mcp_server

    # The warmer skips a directory that is no recognized workspace; tmp_path is
    # one only when it happens to sit inside a repository.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    class _Engine:
        def __init__(self) -> None:
            self.ensure_calls = 0
            self.warm_calls = 0

        def _ensure_indexed(self) -> None:
            self.ensure_calls += 1

        def warm_query_path(self) -> dict[str, float]:
            self.warm_calls += 1
            return {"page_cache_s": 0.0}

    engine = _Engine()
    monkeypatch.setenv("LEMONCROW_CODE_PREPARED", "1")
    monkeypatch.setattr(mcp_server, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_code_context_engine", lambda _root: engine)
    monkeypatch.setattr(
        code_warm, "warm_stdio_workspace", lambda _root: (_ for _ in ()).throw(AssertionError("duplicate"))
    )

    mcp_server._warm_stdio_code_index()
    assert engine.ensure_calls == 1
    assert engine.warm_calls == 1


# ---------------------------------------------------------------------------
# discover_workspaces tests
# ---------------------------------------------------------------------------


def test_discover_workspaces_prunes_dead_mcp_sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    sessions_dir = store_root / "mcp_sessions"
    sessions_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dead_workspace = tmp_path / "dead-workspace"
    dead_workspace.mkdir()
    live_file = sessions_dir / "live.json"
    dead_file = sessions_dir / "dead.json"
    duplicate_file = sessions_dir / "duplicate.json"
    live_payload = {
        "pid": os.getpid(),
        "workspace": str(workspace),
    }
    live_file.write_text(json.dumps(live_payload), encoding="utf-8")
    duplicate_file.write_text(json.dumps(live_payload), encoding="utf-8")
    dead_file.write_text(
        json.dumps(
            {
                "pid": os.getpid() + 1,
                "workspace": str(dead_workspace),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(code_warm, "default_store_root", lambda: store_root)
    monkeypatch.setattr(code_warm, "_registered_mcp_pid_is_live", lambda pid: pid == os.getpid())

    assert code_warm.discover_workspaces() == [workspace.resolve()]
    assert live_file.exists()
    assert duplicate_file.exists()
    assert not dead_file.exists()


def test_discover_workspaces_prunes_reused_non_mcp_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    sessions_dir = store_root / "mcp_sessions"
    sessions_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stale_file = sessions_dir / "stale.json"
    stale_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "workspace": str(workspace),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(code_warm, "default_store_root", lambda: store_root)
    monkeypatch.setattr(code_warm, "_registered_mcp_pid_is_live", lambda pid: False)

    assert code_warm.discover_workspaces() == []
    assert not stale_file.exists()


def test_stdio_warm_hook_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mcp_server startup hook swallows warming errors."""
    from lemoncrow.gateway.adapters import mcp_server

    def _boom(workspace: object) -> bool:
        raise RuntimeError("warm exploded")

    monkeypatch.setattr(code_warm, "warm_stdio_workspace", _boom, raising=True)
    # Must not raise.
    mcp_server._warm_stdio_code_index()
