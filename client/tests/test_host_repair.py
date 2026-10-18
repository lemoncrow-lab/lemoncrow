from __future__ import annotations

import json
import random
import tomllib

import pytest
from lemoncrow_client import host_repair, pi_integration

NEW = "/new/runtime/bin/lemoncrow-runtime-client"


@pytest.mark.parametrize("action", ["lite-mcp", "mcp"])
@pytest.mark.parametrize("array", [False, True])
def test_json_migration_preserves_other_settings_and_is_idempotent(action, array):
    command = ["/old/bin/lc", "runtime", action, "--host", "claude"]
    entry = {"command": command if array else command[0], "env": {"TOKEN": "keep"}, "enabled": False}
    if not array:
        entry["args"] = command[1:]
    original = {"mcpServers": {"lc": entry, "other": {"url": "https://other.test"}}}
    text = "// preserve comment\n" + json.dumps(original, indent=2)
    updated = host_repair._json_repair(text, NEW)
    assert updated.startswith("// preserve comment\n")
    actual = host_repair._parse_jsonc(updated)
    original["mcpServers"]["lc"]["command"] = [NEW, *command[1:]] if array else NEW
    assert actual == original
    assert host_repair._json_repair(updated, NEW) == updated


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "/custom/wrapper", "args": ["runtime", "lite-mcp"]},
        {"command": "lc", "args": ["dev", "mcp"]},
        {"command": "uv", "args": ["run", "lc", "runtime", "mcp"]},
        {"command": "lc", "args": ["review"]},
        {"command": "lc", "args": ["runtime", "mcp", "gateway"]},
        {"type": "http", "url": "https://server.test/mcp"},
    ],
)
def test_custom_and_remote_connections_are_untouched(entry):
    text = json.dumps({"mcpServers": {"lc": entry}})
    assert host_repair._json_repair(text, NEW) == text


def test_jsonc_trailing_commas_and_nested_project_configuration():
    text = """{
      // URL and escaped punctuation must stay untouched
      "url": "https://example.test/{command}",
      "projects": {"repo": {"mcpServers": {
        "lc": {"command": "lc", "args": ["runtime", /* comment */ "lite-mcp",],},
      }}},
    }"""
    updated = host_repair._json_repair(text, NEW)
    assert "// URL and escaped punctuation" in updated
    assert "/* comment */" in updated  # unchanged args retain their comments
    assert host_repair._parse_jsonc(updated)["projects"]["repo"]["mcpServers"]["lc"]["command"] == NEW


def test_json_migration_does_not_change_an_unrelated_command_using_same_launcher():
    text = json.dumps(
        {
            "mcpServers": {
                "lc": {"command": "lc", "args": ["runtime", "lite-mcp"]},
                "review": {"command": "lc", "args": ["review"]},
            }
        }
    )
    actual = json.loads(host_repair._json_repair(text, NEW))
    assert actual["mcpServers"]["review"]["command"] == "lc"


@pytest.mark.parametrize("args", ['["runtime", "lite-mcp", "--host", "codex"]', '["mcp", "--host", "codex"]'])
def test_codex_toml_migration_preserves_env_and_disabled_flag(args):
    text = f"""# custom settings
model = "custom"
[mcp_servers.lc]
command = "/old/bin/lc" # preserve inline comment
args = {args}
enabled = false
[mcp_servers.lc.env]
TOKEN = "keep"
[mcp_servers.other]
command = "other"
"""
    updated = host_repair._toml_repair(text, NEW)
    parsed = tomllib.loads(updated)
    assert "# preserve inline comment" in updated
    assert updated.startswith('# custom settings\nmodel = "custom"')
    assert parsed["mcp_servers"]["lc"] == {
        "command": NEW,
        "args": ["runtime", "lite-mcp" if "lite-mcp" in args else "mcp", "--host", "codex"],
        "enabled": False,
        "env": {"TOKEN": "keep"},
    }
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert host_repair._toml_repair(updated, NEW) == updated


def test_codex_development_command_stays_unchanged():
    text = '[mcp_servers.lc]\ncommand = "/dev/bin/lc"\nargs = ["dev", "--checkout", "/repo", "mcp"]\n'
    assert host_repair._toml_repair(text, NEW) == text


def test_json_migration_differential_over_random_settings():
    rng = random.Random(84)
    for _ in range(200):
        value = "".join(rng.choices('[]{}:,"/\\\\\nabcdef', k=80))
        data = {
            "custom": {"command": value, "args": [value]},
            "mcpServers": {
                "lc": {"command": "lc", "args": ["runtime", "lite-mcp"], "env": {"value": value}},
            },
        }
        text = json.dumps(data, indent=rng.choice([None, 2, 4]), ensure_ascii=rng.choice([True, False]))
        expected = json.loads(text)
        expected["mcpServers"]["lc"]["command"] = NEW
        assert json.loads(host_repair._json_repair(text, NEW)) == expected


def test_repair_existing_only_migrates_enabled_integrations(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    runtime = tmp_path / "runtime" / "bin" / "lemoncrow-runtime-client"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    monkeypatch.setattr(host_repair.sys, "executable", str(runtime.parent / "python"))
    assert host_repair.repair_existing() == 0
    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".pi").exists()

    settings = tmp_path / ".claude.json"
    before = '{"mcpServers":{"lc":{"command":"lc","args":["runtime","lite-mcp"],"alwaysLoad":true}}}'
    settings.write_text(before)
    settings.chmod(0o600)
    monkeypatch.setattr(pi_integration, "_extension_source", lambda: "const installedInvocation = newRuntime;")
    pi_integration.install()
    assert host_repair.repair_existing() == 0
    assert json.loads(settings.read_text())["mcpServers"]["lc"]["command"] == str(runtime)
    backup = settings.with_name(settings.name + ".before-runtime-migration")
    assert backup.read_text() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    assert settings.stat().st_mode & 0o777 == 0o600
    assert host_repair.repair_existing() == 0
    assert backup.read_text() == before


def test_legacy_public_alias_moves_to_stable_launcher(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("LEMONCROW_HOME", str(state))
    monkeypatch.setenv("LEMONCROW_BIN_DIR", str(state / "bin"))
    old = state / "uv-tools/lemoncrow/bin/lc"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\n")
    stable = state / "bin/lc"
    stable.parent.mkdir()
    stable.write_text("#!/bin/sh\n")
    launcher = tmp_path / ".local/bin/lc"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    host_repair._repair_legacy_launcher(tmp_path)
    assert launcher.resolve() == stable
    assert old.exists()
    host_repair._repair_legacy_launcher(tmp_path)
    assert launcher.resolve() == stable


def test_custom_public_alias_is_preserved(tmp_path, monkeypatch):
    custom = tmp_path / "custom/lc"
    custom.parent.mkdir()
    custom.write_text("#!/bin/sh\n")
    launcher = tmp_path / ".local/bin/lc"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(custom)
    host_repair._repair_legacy_launcher(tmp_path)
    assert launcher.resolve() == custom


def test_invalid_configuration_fails_visibly_without_replacing_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    runtime = tmp_path / "bin" / "lemoncrow-runtime-client"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    monkeypatch.setattr(host_repair.sys, "executable", str(runtime.parent / "python"))
    config = tmp_path / ".claude.json"
    config.write_text("{broken")
    assert host_repair.repair_existing() == 1
    assert config.read_text() == "{broken"
    assert "Could not migrate" in capsys.readouterr().err
