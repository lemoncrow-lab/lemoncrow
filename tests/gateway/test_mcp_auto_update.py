"""Regression coverage for MCP git auto-update version state."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server


class _Completed:
    def __init__(self, *, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_mcp_auto_update_records_installed_version_before_pull(monkeypatch, tmp_path: Path) -> None:
    """A long-lived MCP process must not overwrite state with its startup version."""
    (tmp_path / ".git").mkdir()
    install_script = tmp_path / "scripts" / "install.sh"
    install_script.parent.mkdir()
    install_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def _run(command: list[str], **_kwargs: object) -> _Completed:
        if command == ["lc", "--version"]:
            return _Completed(stdout=f"lc, version {next(versions)}\n")
        if command[:2] == ["git", "show"]:
            return _Completed(stdout='version = "9.9.9"\n')
        return _Completed()

    recorded: dict[str, object] = {}
    versions = iter(("2.3.4", "9.9.9"))
    import lemoncrow.core.foundation.update_state as update_state

    monkeypatch.setenv("LEMONCROW_INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_server, "lemoncrow_version", "0.2.1")
    monkeypatch.setattr(mcp_server, "_detect_default_branch", lambda _repo: "main")
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(update_state, "write_update_state", lambda **kwargs: recorded.update(kwargs))

    mcp_server._check_auto_update()

    assert recorded["previous_version"] == "2.3.4"
    assert recorded["current_version"] == "9.9.9"
