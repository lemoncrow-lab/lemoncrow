"""Anonymous lifecycle transitions bootstrap their signed cap authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import admin


def test_bootstrap_helper_forces_a_fresh_mint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An explicit identity transition must bypass BOTH the 30-minute reporting
    # throttle and the unchanged-totals short-circuit: a re-login right after a
    # report would otherwise skip the mint and leave the new identity dormant.
    from lemoncrow.core.capabilities.licensing import usage_report

    calls: list[tuple[Path, bool]] = []

    def _report(root: Path, *, force: bool = False, **_kw: object) -> bool:
        calls.append((Path(root), force))
        return True

    monkeypatch.setattr(usage_report, "report_usage_once", _report)
    assert admin._bootstrap_cap_verdict(tmp_path) is True
    assert calls == [(tmp_path, True)]

    monkeypatch.setattr(
        usage_report,
        "report_usage_once",
        lambda _root, **_kw: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert admin._bootstrap_cap_verdict(tmp_path) is False


def test_bootstrap_syncs_the_agent_override_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Layer 2's SessionStart-driven sync only runs on the NEXT session; an
    # identity transition (login/logout) must not have to wait for that --
    # _bootstrap_cap_verdict has to flip the settings.json `agent` override
    # itself, immediately, for both the global and workspace-local file.
    import json

    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.pro.capabilities import licensing_gate

    config_dir = tmp_path / "claude_home"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(usage_report, "report_usage_once", lambda _root, **_kw: True)

    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: True)
    admin._bootstrap_cap_verdict(tmp_path)
    assert "agent" not in json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert "agent" not in json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: False)
    admin._bootstrap_cap_verdict(tmp_path)
    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    assert (
        json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    )


def test_bootstrap_refreshes_subscription_json_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # subscription.json is what the statusline (a bash script) reads for the
    # plan icon / capped dot -- it must not wait for the next SessionStart to
    # reflect an identity transition made via `lc account login`/`logout`.
    import json

    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store, usage_report

    root = tmp_path / "root"
    root.mkdir()
    (root / "subscription.json").write_text(json.dumps({"plan": "pro", "savingsOverCap": False}), encoding="utf-8")

    from lemoncrow.core.capabilities import plugin_runtime

    monkeypatch.setattr(usage_report, "report_usage_once", lambda _root, **_kw: True)
    monkeypatch.setattr(store, "load_auth_token", lambda: None)
    # The CURRENT identity (post-logout) is anonymous/local, distinct from the
    # stale "pro" blob already on disk -- this is what actually distinguishes
    # "the cache got refreshed" from "the stale file was echoed back unchanged".
    monkeypatch.setattr(plugin_runtime, "resolve_subscription", lambda _root: {"plan": "LOCAL"})

    class _Win:
        saved_usd = 55.0  # > $50 anonymous cap
        spend_usd = 0.0

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win())

    admin._bootstrap_cap_verdict(root)
    data = json.loads((root / "subscription.json").read_text(encoding="utf-8"))
    assert data["savingsOverCap"] is True  # stale cache from the prior (pro) identity is gone


def test_bootstrap_syncs_codex_and_opencode_agents_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Same lag as the Claude agent/settings.json cases, for the OTHER hosts:
    # reset_host_agents_for_dormancy (workspace) is normally hook-driven only,
    # one session behind. _bootstrap_cap_verdict must not skip Codex/OpenCode
    # just because it can't be sure which hosts are installed -- both helpers
    # are safe no-ops when their target directories are absent.
    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.pro.capabilities import licensing_gate

    workspace = tmp_path / "workspace"
    codex_agents = workspace / ".codex" / "agents"
    codex_agents.mkdir(parents=True)
    (codex_agents / "lemoncrow.code.toml").write_text("x", encoding="utf-8")

    monkeypatch.chdir(workspace)
    monkeypatch.delenv("CLAUDE_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(usage_report, "report_usage_once", lambda _root, **_kw: True)
    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: True)

    admin._bootstrap_cap_verdict(tmp_path)
    assert not (codex_agents / "lemoncrow.code.toml").exists()  # stashed immediately, no session start needed

    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: False)
    admin._bootstrap_cap_verdict(tmp_path)
    assert (codex_agents / "lemoncrow.code.toml").read_text(encoding="utf-8") == "x"


def test_anonymous_cli_transitions_bootstrap_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".lemoncrow"
    calls: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        admin,
        "_bootstrap_cap_verdict",
        lambda value: calls.append(Path(value)) or True,
    )
    runner = CliRunner()

    initialized = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "init",
            "--no-login",
            "--no-index",
            "--no-seed",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert calls == [root]

    calls.clear()
    logged_in = runner.invoke(
        cli,
        ["--root", str(root), "account", "login", "--anonymous", "--json"],
    )
    assert logged_in.exit_code == 0, logged_in.output
    assert calls == [root]

    calls.clear()
    logged_out = runner.invoke(
        cli,
        ["--root", str(root), "account", "logout", "--json"],
    )
    assert logged_out.exit_code == 0, logged_out.output
    assert calls == [root]

    calls.clear()
    no_trial = runner.invoke(
        cli,
        ["--root", str(root), "account", "logout", "--no-trial", "--json"],
    )
    assert no_trial.exit_code == 0, no_trial.output
    assert calls == []


def test_logout_warns_when_bootstrap_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A silent "✓ Logged out" while the fresh anonymous verdict mint failed
    # (offline/server unreachable) is exactly how a user ends up staring at
    # an empty MCP tool list with no idea why -- this must be surfaced.
    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda root: False)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "logout"])
    assert result.exit_code == 0, result.output
    assert "Logged out" in result.output
    assert "Warning" in result.output
    assert "tools will stay disabled" in result.output


def test_logout_no_warning_when_bootstrap_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda root: True)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "logout"])
    assert result.exit_code == 0, result.output
    assert "Logged out" in result.output
    assert "Warning" not in result.output


def test_logout_no_trial_never_warns_even_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # --no-trial skips the bootstrap call entirely -- nothing to warn about.
    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda root: False)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "logout", "--no-trial"])
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output


def _fake_oauth_result() -> object:
    from lemoncrow.core.capabilities.licensing.oauth_flow import OAuthLoginResult

    return OAuthLoginResult(
        token="tok-oauth",
        email="pro@example.com",
        plan="pro",
        plan_verified=True,
        device_id="dev123456789",
    )


def test_oauth_login_bootstraps_cap_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.licensing import oauth_flow

    root = tmp_path / ".lemoncrow"
    calls: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oauth_flow, "run_oauth_login", lambda **_kw: _fake_oauth_result())
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda value: calls.append(Path(value)) or True)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "login", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [root]

    import json

    assert json.loads(result.output)["cap_verdict_verified"] is True


def test_oauth_login_warns_when_bootstrap_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # "✓ Logged in as ... (pro)" followed by an empty MCP tool list with no
    # explanation is exactly the confusion this warning exists to prevent.
    from lemoncrow.core.capabilities.licensing import oauth_flow

    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oauth_flow, "run_oauth_login", lambda **_kw: _fake_oauth_result())
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda _root: False)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "login"])
    assert result.exit_code == 0, result.output
    assert "Logged in as pro@example.com" in result.output
    assert "Warning" in result.output
    assert "tools will stay disabled" in result.output


def test_logout_json_reports_verdict_verified_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json

    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda root: False)
    runner = CliRunner()
    failed = runner.invoke(cli, ["--root", str(root), "account", "logout", "--json"])
    assert failed.exit_code == 0, failed.output
    assert json.loads(failed.output)["anonymous_verdict_verified"] is False

    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda root: True)
    succeeded = runner.invoke(cli, ["--root", str(root), "account", "logout", "--json"])
    assert succeeded.exit_code == 0, succeeded.output
    assert json.loads(succeeded.output)["anonymous_verdict_verified"] is True
