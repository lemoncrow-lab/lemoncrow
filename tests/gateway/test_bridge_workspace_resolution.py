"""The bridge must serve the window's workspace, not an inherited one.

Under a global (all-windows) MCP install the editor launches one bridge per
window with no workspace argument. Cursor/VS Code do state the window's folder
in WORKSPACE_FOLDER_PATHS, but the generic *_WORKSPACE_ROOT vars are inherited
from whatever shell started the editor -- so trusting those attaches every
window to one daemon and resolves relative paths into an unrelated repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.foundation.paths import host_workspace_root
from lemoncrow.gateway.adapters.mcp_bridge import _resolve_workspace

_INHERITED_VARS = (
    "LEMONCROW_WORKSPACE_ROOT",
    "CLAUDE_WORKSPACE_ROOT",
    "CURSOR_WORKSPACE_ROOT",
    "VSCODE_CWD",
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*_INHERITED_VARS, "WORKSPACE_FOLDER_PATHS"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("inherited", _INHERITED_VARS)
def test_window_folder_beats_every_inherited_workspace_var(
    inherited: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear(monkeypatch)
    window = tmp_path / "window-workspace"
    window.mkdir()
    stale = tmp_path / "shell-that-launched-the-editor"
    stale.mkdir()
    monkeypatch.setenv(inherited, str(stale))
    monkeypatch.setenv("WORKSPACE_FOLDER_PATHS", str(window))

    assert host_workspace_root() == window
    assert _resolve_workspace() == str(window)


def test_multi_root_window_takes_the_first_folder_that_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    missing = tmp_path / "deleted-since-the-window-opened"
    present = tmp_path / "still-here"
    present.mkdir()
    for separator in (",", ":"):
        monkeypatch.setenv("WORKSPACE_FOLDER_PATHS", f"{missing}{separator}{present}")
        assert host_workspace_root() == present


def test_inherited_var_still_used_when_host_states_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No per-window signal is not an error -- hosts that set none must still work."""
    _clear(monkeypatch)
    declared = tmp_path / "declared"
    declared.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(declared))

    assert host_workspace_root() == declared


def test_no_signal_at_all_falls_back_to_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert host_workspace_root() is None


def test_blank_window_folder_does_not_shadow_inherited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    declared = tmp_path / "declared"
    declared.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(declared))
    monkeypatch.setenv("WORKSPACE_FOLDER_PATHS", "   ")

    assert host_workspace_root() == declared
