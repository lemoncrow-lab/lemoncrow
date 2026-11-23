from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from click.testing import CliRunner

from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.commands import runtime as runtime_commands


def test_runtime_lite_entrypoint_accepts_all_shipped_host_identifiers(monkeypatch) -> None:
    import os

    from lemoncrow_client.lite import mcp

    seen = []
    monkeypatch.setattr(mcp, "serve", lambda: seen.append(os.environ["LEMONCROW_AGENT"]) or 0)
    for host in ("claude", "codex", "opencode", "cursor", "pi"):
        result = CliRunner().invoke(cli, ["runtime", "lite-mcp", "--host", host])
        assert result.exit_code == 0, result.output
    assert seen == ["claude", "codex", "opencode", "cursor", "pi"]


def test_runtime_status_is_a_first_class_product_namespace(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_commands,
        "_query_integration",
        lambda host, workspace: {
            "host": host,
            "installed": True,
            "enabled": host == "claude",
            "mode": "local_lite" if host == "claude" else "unknown",
            "detail": "",
        },
    )

    result = CliRunner().invoke(cli, ["runtime", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["product"] == "runtime"
    assert payload["installed"] is True
    assert [row["host"] for row in payload["integrations"]] == ["claude", "codex", "opencode"]
    assert payload["integrations"][0]["enabled"] is True
    assert payload["integrations"][0]["mode"] == "local_lite"
    assert {row["mode"] for row in payload["integrations"]} <= {"local_lite", "hosted", "unknown"}


def test_runtime_status_labels_local_and_hosted_modes(monkeypatch) -> None:
    modes = {
        "claude": "local_lite",
        "codex": "hosted",
        "opencode": "unknown",
    }
    monkeypatch.setattr(
        runtime_commands,
        "_query_integration",
        lambda host, workspace: {
            "host": host,
            "installed": True,
            "enabled": True,
            "mode": modes[host],
            "detail": "",
        },
    )

    result = CliRunner().invoke(cli, ["runtime", "status"])

    assert result.exit_code == 0, result.output
    assert "✓ claude     enabled · Lite" in result.output
    assert "✓ codex      enabled · Hosted" in result.output
    assert "⚠ opencode   enabled · Unknown mode" in result.output
    assert "lc rt enable" not in result.output


def test_runtime_integration_mode_parser() -> None:
    assert runtime_commands._integration_mode("args: runtime lite-mcp --host claude", enabled=True) == "local_lite"
    assert runtime_commands._integration_mode('{"args":["runtime","mcp","--host","codex"]}', enabled=True) == "hosted"
    assert runtime_commands._integration_mode("connected", enabled=True) == "unknown"
    assert runtime_commands._integration_mode("runtime lite-mcp", enabled=False) == "unknown"


def test_runtime_enable_reuses_existing_host_installer(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    installer = scripts / "install_claude.sh"
    installer.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(runtime_commands, "_repo_root", lambda: tmp_path)
    run = MagicMock()
    monkeypatch.setattr(runtime_commands.subprocess, "run", run)

    result = CliRunner().invoke(cli, ["runtime", "enable", "claude", "--dry-run"])

    assert result.exit_code == 0, result.output
    argv = run.call_args.args[0]
    assert argv == ["bash", str(installer), "--dry-run", "--strict"]
    assert run.call_args.kwargs["check"] is True


def test_runtime_enable_hosted_is_explicit(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    installer = scripts / "install_claude.sh"
    installer.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(runtime_commands, "_repo_root", lambda: tmp_path)
    run = MagicMock()
    monkeypatch.setattr(runtime_commands.subprocess, "run", run)

    result = CliRunner().invoke(cli, ["runtime", "enable", "claude", "--hosted", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert run.call_args.args[0] == [
        "bash",
        str(installer),
        "--dry-run",
        "--strict",
        "--hosted",
    ]


def test_runtime_disable_reuses_existing_host_uninstaller(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    uninstaller = scripts / "uninstall_opencode.sh"
    uninstaller.write_text("#!/usr/bin/env bash\n")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(runtime_commands, "_repo_root", lambda: tmp_path)
    run = MagicMock()
    monkeypatch.setattr(runtime_commands.subprocess, "run", run)

    result = CliRunner().invoke(
        cli,
        ["runtime", "disable", "opencode", "--workspace", str(workspace), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    argv = run.call_args.args[0]
    assert argv == [
        "bash",
        str(uninstaller),
        "--dry-run",
        "--workspace",
        str(workspace.resolve()),
    ]


def test_runtime_status_does_not_require_review(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_commands,
        "_query_integration",
        lambda host, workspace: {
            "host": host,
            "installed": False,
            "enabled": False,
            "mode": "unknown",
            "detail": "host CLI not found",
        },
    )

    result = CliRunner().invoke(cli, ["runtime", "status"])

    assert result.exit_code == 0, result.output
    assert "LemonCrow Workspaces" in result.output
    assert "host not installed" in result.output
    assert "Review" not in result.output


def test_runtime_status_suggests_enabling_an_installed_host(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_commands,
        "_query_integration",
        lambda host, workspace: {
            "host": host,
            "installed": host != "opencode",
            "enabled": host == "claude",
            "mode": "local_lite" if host == "claude" else "unknown",
            "detail": "",
        },
    )

    result = CliRunner().invoke(cli, ["runtime", "status"])

    assert result.exit_code == 0, result.output
    assert "○ codex      not enabled" in result.output
    assert "○ opencode   host not installed" in result.output
    assert "→ Give an agent LemonCrow's tools: lc rt enable codex" in result.output


def test_runtime_enable_confirms_what_changed(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install_codex.sh").write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(runtime_commands, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runtime_commands.subprocess, "run", MagicMock())

    result = CliRunner().invoke(cli, ["runtime", "enable", "codex"])

    assert result.exit_code == 0, result.output
    assert "✓ Workspaces enabled for Codex · Lite" in result.output
    assert "Restart Codex to load the lc tools" in result.output
