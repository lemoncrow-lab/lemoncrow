from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _touch(path: Path, body: str = "\n", *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def test_status_script_uses_current_host_artifacts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    status_script = repo_root / "scripts" / "status.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("claude", "codex", "cursor"):
        _touch(fake_bin / command, "#!/bin/sh\nexit 0\n", executable=True)

    _touch(
        tmp_path / ".claude.json",
        '{"mcpServers":{"lc":{"command":"lemoncrow","args":["mcp","--host","claude"]}}}\n',
    )
    _touch(tmp_path / ".lemoncrow/claude-plugin/.claude-plugin/plugin.json", '{"name":"lemoncrow"}\n')

    _touch(tmp_path / ".codex/plugins/lemoncrow/.codex-plugin/plugin.json", '{"name":"lemoncrow"}\n')
    _touch(tmp_path / ".codex/plugins/lemoncrow/.mcp.json", '{"mcpServers":{"lc":{"command":"lemoncrow"}}}\n')
    _touch(tmp_path / ".codex/AGENTS.md", "<!-- lemoncrow:code -->\n")

    _touch(tmp_path / ".config/opencode/opencode.json", '{"mcp":{"lc":{"command":["lemoncrow"]}}}\n')
    _touch(tmp_path / ".lemoncrow/bin/pi-host", "#!/bin/sh\n", executable=True)
    _touch(tmp_path / ".cursor/mcp.json", '{"mcpServers":{"lemoncrow":{"command":"lc"}}}\n')

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    # Hermetic: the host's own opencode install or XDG config must not answer for this fake home.
    for name in ("XDG_CONFIG_HOME", "OPENCODE_CONFIG_HOME"):
        env.pop(name, None)
    host_path = [entry for entry in env["PATH"].split(os.pathsep) if not (Path(entry) / "opencode").exists()]
    env["PATH"] = os.pathsep.join([str(fake_bin), *host_path])

    result = subprocess.run(
        ["bash", str(status_script), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    status = json.loads(result.stdout)
    assert status["claude"] == "installed"
    assert status["codex"] == "installed"
    assert status["opencode"] == "configured, CLI not found"
    assert status["pi"] == "installed"
    assert "lemoncode" not in status
    assert status["cursor"] == "installed"

    subprocess.run(
        ["bash", str(status_script), "--write"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    cached = json.loads((tmp_path / ".lemoncrow/hosts/status.json").read_text(encoding="utf-8"))
    for host in ("claude", "codex", "opencode", "pi", "cursor"):
        assert cached[host] == "installed"
