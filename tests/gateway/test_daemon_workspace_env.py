"""A daemon pins its own workspace; it never inherits another one.

``_workspace_path`` consults CLAUDE_WORKSPACE_ROOT before anything else, so an
inherited value silently resolves every relative read into a different tree.
Observed in a benchmark run: a window on /tmp/ide-bench/L1 was served reads from
/tmp/ide-bench/t3, which keyed the freshness ledger to the wrong paths and made
every subsequent ranged edit fail as "not served by read/code_search".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp_server import _workspace_path

_WORKSPACE_ENV = ("CLAUDE_WORKSPACE_ROOT", "LEMONCROW_WORKSPACE_ROOT")


def _pin_workspace(workspace: str) -> None:
    """Apply the same pin `run_daemon` applies, without copying its body.

    Asserting the delegation first is the point: if `run_daemon` ever stops
    routing through `pin_workspace_env` and hand-rolls the assignments again,
    this fails here rather than silently testing a helper the daemon no longer
    uses -- which is how the per-variable version of this bug kept coming back.
    """
    import inspect

    from lemoncrow.core.foundation.paths import pin_workspace_env
    from lemoncrow.gateway.adapters import mcp_daemon

    src = inspect.getsource(mcp_daemon.run_daemon)
    assert "pin_workspace_env(workspace)" in src, "run_daemon no longer pins its workspace centrally"
    pin_workspace_env(workspace)


def test_inherited_workspace_does_not_survive_daemon_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = tmp_path / "my-workspace"
    theirs = tmp_path / "another-window"
    for d in (mine, theirs):
        (d / "pkg").mkdir(parents=True)
        (d / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    # The editor was launched from a different project; both vars leak in.
    for var in _WORKSPACE_ENV:
        monkeypatch.setenv(var, str(theirs))

    _pin_workspace(str(mine))

    assert _workspace_path("pkg/mod.py") == mine / "pkg" / "mod.py"
    assert theirs not in _workspace_path("pkg/mod.py").parents


def test_absolute_paths_are_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads outside the workspace stay legal — only relative resolution is pinned."""
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    elsewhere = tmp_path / "sibling" / "notes.md"
    assert _workspace_path(str(elsewhere)) == elsewhere


def test_relative_read_and_edit_agree_on_one_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger is keyed by resolved path: read and edit must land on the same key.

    This is the invariant the benchmark violated — a relative read resolved into
    another workspace while the edit used an absolute path in this one.
    """
    mine = tmp_path / "ws"
    (mine / "pkg").mkdir(parents=True)
    target = mine / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path / "some-other-repo"))

    _pin_workspace(str(mine))

    assert _workspace_path("pkg/mod.py").resolve() == _workspace_path(str(target)).resolve()
