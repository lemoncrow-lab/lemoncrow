"""Exercise published client archives without checkout imports or real user settings.

Run after stage_release.sh: python scripts/verify_client_distribution.py STAGED_RELEASE
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def verify(release: Path, root: Path) -> None:
    home, state, work = root / "home", root / "state", root / "repo"
    home.mkdir()
    work.mkdir()
    (work / ".git").mkdir()
    (work / "probe.py").write_text("def release_probe():\n    return 1\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LEMONCROW_", "CLAUDE_", "CODEX_", "PI_"))
        and k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "XDG_CONFIG_HOME"}
    }
    env.update(
        HOME=str(home),
        LEMONCROW_HOME=str(state),
        LEMONCROW_BIN_DIR=str(state / "bin"),
        LEMONCROW_RELEASE_BASE_URL=release.as_uri(),
        LEMONCROW_NO_BROWSER="1",
    )
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    # Exercise native Claude registration, without installing or launching Claude.
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(f"#!{sys.executable}\n" + r"""
import json, sys
from pathlib import Path
p = Path.home() / ".claude.json"
d = json.loads(p.read_text()) if p.exists() else {}
if sys.argv[1:3] == ["mcp", "add"]:
    i = sys.argv.index("--")
    d.setdefault("mcpServers", {})["lc"] = {"type": "stdio", "command": sys.argv[i+1], "args": sys.argv[i+2:]}
    p.write_text(json.dumps(d))
""")
    fake_claude.chmod(0o755)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

    def run(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=work, env=env, input=input, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result

    def install(product: str) -> None:
        helper = "hosted" if product == "review" else "runtime"
        options = ["--url", "https://release-test.invalid"] if product == "review" else ["--lite"]
        run(
            "bash",
            str(release / f"{helper}.sh"),
            "--from-build",
            str(release / f"lemoncrow-{product}-client.tar.gz"),
            *options,
        )

    def probe(command: list[str]) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"files": ["probe.py:head=2"]}},
            },
        ]
        result = run(*command, input="".join(json.dumps(r) + "\n" for r in requests))
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        assert any(
            r.get("id") == 1 and r.get("result", {}).get("serverInfo", {}).get("name") == "lemoncrow-runtime-lite"
            for r in responses
        ), responses
        assert any(
            r.get("id") == 2
            and r.get("result")
            and not r["result"].get("isError")
            and "release_probe" in json.dumps(r["result"])
            for r in responses
        ), responses

    if root.name == "runtime-first":
        install("runtime")
        assert not (home / ".claude.json").exists(), "fresh install must not opt users into hosts"
        probe([str(state / "bin" / "lc"), "runtime", "lite-mcp", "--host", "pi"])
        install("review")
    else:
        install("review")
        # The Review-only public lc must fetch Runtime and then enable the host.
        run(str(state / "bin" / "lc"), "rt", "enable", "claude")
        server = json.loads((home / ".claude.json").read_text())["mcpServers"]["lc"]
        probe([server["command"], *server["args"]])
    probe([str(state / "bin" / "lc"), "runtime", "lite-mcp", "--host", "cursor"])

    # Model upgrade from old absolute paths and a leftover full installation.
    legacy = state / "uv-tools/lemoncrow/bin/lc"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\nexit 99\n")
    legacy.chmod(0o755)
    old_alias = home / ".local/bin/lc"
    old_alias.parent.mkdir(parents=True)
    old_alias.symlink_to(legacy)
    for relative, array, host in [
        (".claude.json", False, "claude"),
        (".cursor/mcp.json", False, "cursor"),
        (".config/opencode/opencode.json", True, "opencode"),
        (".config/lemoncode/opencode.jsonc", True, "lemoncode"),
    ]:
        old = [str(state / "uv-tools/lemoncrow/bin/lc"), "runtime", "lite-mcp", "--host", host]
        entry = {"command": old if array else old[0], "enabled": False, "env": {"PRESERVE": "yes"}}
        if not array:
            entry["args"] = old[1:]
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcp" if array else "mcpServers": {"lc": entry}, "custom": "keep"}))
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers.lc]\ncommand="lc"\nargs=["runtime","lite-mcp","--host","codex"]\nenabled=false\n')
    runtime_python = str(state / "runtime/client/bin/python")
    run(runtime_python, "-c", "from lemoncrow_client.pi_integration import install; install()")
    pi = home / ".pi/agent/lemoncrow/lite.mjs"
    source = pi.read_text()
    source = re.sub(
        r"^const installedInvocation = .*;$",
        'const installedInvocation = {"command":"/old/lc","args":["runtime","lite-mcp","--host","pi"]};',
        source,
        count=1,
        flags=re.M,
    )
    pi.write_text(source)

    # lc update must work with legacy files present and use the staged release.
    run(str(state / "bin/lc"), "update")
    probe([str(old_alias), "runtime", "lite-mcp", "--host", "claude"])
    expected = str(state / "runtime/client/bin/lemoncrow-runtime-client")
    for relative, group in [
        (".claude.json", "mcpServers"),
        (".cursor/mcp.json", "mcpServers"),
        (".config/opencode/opencode.json", "mcp"),
        (".config/lemoncode/opencode.jsonc", "mcp"),
    ]:
        document = json.loads((home / relative).read_text())
        entry = document[group]["lc"]
        assert document["custom"] == "keep" and entry["enabled"] is False and entry["env"] == {"PRESERVE": "yes"}
        command = entry["command"] if isinstance(entry["command"], list) else [entry["command"], *entry["args"]]
        assert command[0] == expected, command
        probe(command)
    entry = tomllib.loads(codex.read_text())["mcp_servers"]["lc"]
    assert entry["command"] == expected and entry["enabled"] is False
    probe([entry["command"], *entry["args"]])
    invocation = json.loads(re.search(r"^const installedInvocation = (.*);$", pi.read_text(), re.M).group(1))
    assert invocation["command"] == expected
    probe([invocation["command"], *invocation["args"]])
    probe([str(state / "bin/lc"), "runtime", "lite-mcp", "--host", "claude"])
    print(f"{root.name}: clean install, lazy enable, upgrade and six host connections passed")


if __name__ == "__main__":
    release = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="lc-client-distribution-") as directory:
        for order in ("runtime-first", "review-first"):
            root = Path(directory) / order
            root.mkdir()
            verify(release, root)
