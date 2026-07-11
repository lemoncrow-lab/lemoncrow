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
    from lemoncrow.core.foundation.paths import workspace_key

    fired = _patch_fire(monkeypatch)
    store_root = tmp_path / "store"
    monkeypatch.setattr(code_warm, "default_store_root", lambda: store_root)
    ws = tmp_path / "idx_ws_bench"
    ws.mkdir()
    db_dir = store_root / "workspaces" / workspace_key(ws.resolve())
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
    # Skip the account-activation gate: no need to exercise the real OAuth
    # flow (network + browser) just to test fail-open error handling here.
    monkeypatch.setattr(mcp_server, "_ensure_account_activated", lambda root: True, raising=True)
    # Must not raise.
    mcp_server._warm_stdio_code_index()
