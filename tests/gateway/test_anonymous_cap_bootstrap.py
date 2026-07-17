"""Account lifecycle transitions: the cap-verdict bootstrap is a neutralized no-op.

`_bootstrap_cap_verdict` no longer mints a signed verdict, phones home, refreshes
subscription.json, or pops any host `agent` override — nothing is ever dormant.
Login/logout still invoke the (now inert) hook at the same call sites; `lc init`
no longer does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import admin


def test_bootstrap_is_a_noop_and_never_mints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The OSS runtime has no signed cap verdict: identity transitions never mint
    # anything and never touch the network. _bootstrap_cap_verdict is a no-op that
    # always returns True and never calls the usage reporter.
    from lemoncrow.core.capabilities.licensing import usage_report

    calls: list[tuple[Path, bool]] = []

    def _report(root: Path, *, force: bool = False, **_kw: object) -> bool:
        calls.append((Path(root), force))
        return True

    monkeypatch.setattr(usage_report, "report_usage_once", _report)
    assert admin._bootstrap_cap_verdict(tmp_path) is True
    assert calls == []

    # Even if the reporter would blow up, bootstrap never calls it -> still True.
    monkeypatch.setattr(
        usage_report,
        "report_usage_once",
        lambda _root, **_kw: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert admin._bootstrap_cap_verdict(tmp_path) is True


def test_bootstrap_leaves_the_agent_override_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Nothing is ever dormant now, so there is no dormant-agent pop: bootstrap
    # leaves both the global and workspace-local `agent` override exactly as-is,
    # even if a legacy cap_exhausted gate is forced True.
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

    # A leftover over-cap gate no longer pops the agent -- the agent is preserved.
    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: True)
    assert admin._bootstrap_cap_verdict(tmp_path) is True
    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    assert (
        json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    )

    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: False)
    assert admin._bootstrap_cap_verdict(tmp_path) is True
    assert json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    assert (
        json.loads((workspace / ".claude" / "settings.json").read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"
    )


def test_bootstrap_never_flips_subscription_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # bootstrap is a no-op now: it does not rewrite subscription.json and, with
    # no cap anywhere, it can never flip savingsOverCap on -- not even with a
    # trailing-window total far above the former $100 anonymous cap.
    import json

    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store, usage_report

    root = tmp_path / "root"
    root.mkdir()
    (root / "subscription.json").write_text(json.dumps({"plan": "pro", "savingsOverCap": False}), encoding="utf-8")

    from lemoncrow.core.capabilities import plugin_runtime

    monkeypatch.setattr(usage_report, "report_usage_once", lambda _root, **_kw: True)
    monkeypatch.setattr(store, "load_auth_token", lambda: None)
    monkeypatch.setattr(plugin_runtime, "resolve_subscription", lambda _root: {"plan": "LOCAL"})

    class _Win:
        saved_usd = 105.0  # would have exceeded the old $100 anonymous cap
        spend_usd = 0.0

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win())

    admin._bootstrap_cap_verdict(root)
    data = json.loads((root / "subscription.json").read_text(encoding="utf-8"))
    assert data["savingsOverCap"] is False  # never flipped on
    assert data["plan"] == "pro"  # untouched: bootstrap did not rewrite the file


def test_bootstrap_leaves_codex_and_opencode_agents_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Codex/OpenCode agent files are never stashed now -- nothing is ever dormant,
    # so bootstrap leaves the host agent files in place regardless of any legacy
    # cap_exhausted gate.
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

    assert admin._bootstrap_cap_verdict(tmp_path) is True
    assert (codex_agents / "lemoncrow.code.toml").read_text(encoding="utf-8") == "x"  # not stashed

    monkeypatch.setattr(licensing_gate, "cap_exhausted", lambda _root: False)
    assert admin._bootstrap_cap_verdict(tmp_path) is True
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
    # `lc init` runs fully locally now and no longer bootstraps a cap verdict.
    assert calls == []

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


def test_oauth_login_does_not_bootstrap_cap_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Linking an OPTIONAL hosted account gates nothing: OAuth login no longer
    # bootstraps a cap verdict, and the JSON output is now just email/device/mode
    # (no cap_verdict_verified flag).
    from lemoncrow.core.capabilities.licensing import oauth_flow

    root = tmp_path / ".lemoncrow"
    calls: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oauth_flow, "run_oauth_login", lambda **_kw: _fake_oauth_result())
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda value: calls.append(Path(value)) or True)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "login", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == []

    import json

    payload = json.loads(result.output)
    assert payload["email"] == "pro@example.com"
    assert payload["mode"] == "oauth"
    assert "cap_verdict_verified" not in payload


def test_oauth_login_never_warns_about_disabled_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Login never gates tools now, so it never prints the "tools will stay
    # disabled" warning -- even if a legacy bootstrap hook would report failure.
    from lemoncrow.core.capabilities.licensing import oauth_flow

    root = tmp_path / ".lemoncrow"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(oauth_flow, "run_oauth_login", lambda **_kw: _fake_oauth_result())
    monkeypatch.setattr(admin, "_bootstrap_cap_verdict", lambda _root: False)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "account", "login"])
    assert result.exit_code == 0, result.output
    assert "pro@example.com" in result.output
    assert "Warning" not in result.output
    assert "tools will stay disabled" not in result.output


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
