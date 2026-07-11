"""Tests for the on-demand agent/skill install CLI (`lemon agent|skill|install`).

Covers: list's installed/available split and token costs; install/remove at
Claude/Codex/OpenCode workspace scope (direct Python calls, no real host CLI
needed); the new `write_opencode_agents` generalization used for OpenCode
global installs; refusal to remove a default role; hidden-skill rejection;
cross-dimension (agent vs skill) non-clobbering; and `install optionals`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from lemoncrow.core.capabilities.workspace_host_overrides import write_opencode_agents
from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.commands import agents_skills as m

REPO_ROOT = Path(__file__).resolve().parents[2]


def _invoke(*args: str) -> object:
    runner = CliRunner()
    return runner.invoke(cli, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- #
# agent list
# --------------------------------------------------------------------------- #


def test_agent_list_shows_installed_vs_available_with_costs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("agent", "install", "explore", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output

    result = _invoke("agent", "list", "--host", "claude", "--workspace", str(ws), "--json")
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["host"] == "claude"
    assert payload["scope"] == "workspace"
    by_role = {row["role_id"]: row for row in payload["roles"]}
    assert by_role["explore"]["installed"] is True
    assert by_role["plan"]["installed"] is False
    # code is always-default; never shown as an optional role.
    assert "code" not in by_role
    assert all(row["token_cost"] > 0 for row in payload["roles"])


def test_agent_list_requires_host_when_ambiguous_or_absent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    result = _invoke("agent", "list", "--workspace", str(ws))
    assert result.exit_code != 0
    assert "--host" in result.output


# --------------------------------------------------------------------------- #
# agent install / remove -- workspace scope (Claude, Codex, OpenCode)
# --------------------------------------------------------------------------- #


def test_agent_install_claude_workspace_writes_namespaced_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("agent", "install", "explore", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert (ws / ".claude" / "agents" / "lemoncrow.explore.md").exists()
    assert (ws / ".claude" / "agents" / "lemoncrow.code.md").exists()  # default role always present


def test_agent_install_codex_workspace_writes_toml(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".codex").mkdir(parents=True)
    result = _invoke("agent", "install", "review", "--host", "codex", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert (ws / ".codex" / "agents" / "lemoncrow.review.toml").exists()
    assert (ws / ".codex" / "agents" / "lemoncrow.code.toml").exists()


def test_agent_install_opencode_workspace_writes_namespaced_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".opencode").mkdir(parents=True)
    result = _invoke("agent", "install", "plan", "--host", "opencode", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert (ws / ".opencode" / "agents" / "lemoncrow.plan.md").exists()
    assert (ws / ".opencode" / "agents" / "lemoncrow.code.md").exists()


def test_agent_remove_deletes_the_role_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    assert _invoke("agent", "install", "explore", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    role_file = ws / ".claude" / "agents" / "lemoncrow.explore.md"
    assert role_file.exists()
    result = _invoke("agent", "remove", "explore", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert not role_file.exists()
    assert (ws / ".claude" / "agents" / "lemoncrow.code.md").exists()


def test_agent_remove_refuses_to_remove_default_role(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("agent", "remove", "code", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code != 0
    assert "uninstall" in result.output


def test_agent_install_rejects_unknown_and_default_role(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("agent", "install", "code", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code != 0
    result = _invoke("agent", "install", "not-a-role", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# write_opencode_agents generalization -- OpenCode global install mechanism
# --------------------------------------------------------------------------- #


def test_write_opencode_agents_writes_real_per_role_files_for_global_target(tmp_path: Path) -> None:
    target = tmp_path / "global-agents"
    written = write_opencode_agents(target, repo_root=REPO_ROOT, role_ids=("code", "explore"))
    names = sorted(p.name for p in written)
    assert names == ["lemoncrow.code.md", "lemoncrow.explore.md"]
    assert (target / "lemoncrow.code.md").exists()
    assert (target / "lemoncrow.explore.md").exists()
    # Legacy bare lemoncrow.md must not linger once the per-role naming is used.
    assert not (target / "lemoncrow.md").exists()


def test_write_opencode_agents_cleans_up_removed_roles(tmp_path: Path) -> None:
    target = tmp_path / "global-agents"
    write_opencode_agents(target, repo_root=REPO_ROOT, role_ids=("code", "explore", "plan"))
    assert (target / "lemoncrow.plan.md").exists()
    write_opencode_agents(target, repo_root=REPO_ROOT, role_ids=("code", "explore"))
    assert not (target / "lemoncrow.plan.md").exists()
    assert (target / "lemoncrow.explore.md").exists()


# --------------------------------------------------------------------------- #
# skill list / install / remove -- Claude workspace
# --------------------------------------------------------------------------- #


def test_skill_install_claude_workspace_writes_skill_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("skill", "install", "benchmark", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert (ws / ".claude" / "skills" / "benchmark" / "SKILL.md").exists()


def test_skill_remove_claude_workspace_deletes_skill_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    assert _invoke("skill", "install", "benchmark", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    result = _invoke("skill", "remove", "benchmark", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    assert not (ws / ".claude" / "skills" / "benchmark" / "SKILL.md").exists()


def test_skill_install_rejects_hidden_dev_skill(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    for hidden_name in ("context", "savings", "rescue", "analyze-failures", "evals", "status", "record"):
        result = _invoke("skill", "install", hidden_name, "--host", "claude", "--workspace", str(ws), "--yes")
        assert result.exit_code != 0, f"{hidden_name} should be rejected"
        assert not (ws / ".claude" / "skills" / hidden_name).exists()


def test_skill_install_opencode_errors_clearly(tmp_path: Path) -> None:
    result = _invoke("skill", "install", "benchmark", "--host", "opencode", "--yes")
    assert result.exit_code != 0
    assert "no skills concept" in result.output


def test_skill_list_json_reports_sane_costs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    result = _invoke("skill", "list", "--host", "claude", "--workspace", str(ws), "--json")
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    rows = {row["name"]: row for row in payload["skills"]}
    assert set(rows) == set(m.PUBLIC_SKILL_NAMES)
    assert all(row["token_cost"] > 0 for row in rows.values())
    # Longer descriptions should cost more tokens -- a loose proportionality
    # sanity check rather than an exact value (which would over-couple to copy).
    costs_by_desc_len = sorted(
        (len(m._skill_description(name, REPO_ROOT)), row["token_cost"]) for name, row in rows.items()
    )
    desc_lens = [d for d, _ in costs_by_desc_len]
    assert desc_lens == sorted(desc_lens)


# --------------------------------------------------------------------------- #
# cross-dimension non-clobbering (agents vs skills share one Claude workspace
# writer function) -- regression coverage for a bug found while building this.
# --------------------------------------------------------------------------- #


def test_installing_a_skill_does_not_reset_previously_installed_agents(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    assert _invoke("agent", "install", "explore", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    assert _invoke("skill", "install", "benchmark", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    assert (ws / ".claude" / "agents" / "lemoncrow.explore.md").exists()
    assert (ws / ".claude" / "skills" / "benchmark" / "SKILL.md").exists()


def test_installing_an_agent_does_not_reset_previously_installed_skills(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    assert _invoke("skill", "install", "recall", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    assert _invoke("agent", "install", "solve", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    assert (ws / ".claude" / "skills" / "recall" / "SKILL.md").exists()
    assert (ws / ".claude" / "agents" / "lemoncrow.solve.md").exists()


# --------------------------------------------------------------------------- #
# install optionals
# --------------------------------------------------------------------------- #


def test_install_optionals_workspace_installs_every_role_and_skill(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    result = _invoke("install", "optionals", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0, result.output
    agent_names = {p.stem for p in (ws / ".claude" / "agents").glob("lemoncrow.*.md")}
    assert agent_names == {f"lemoncrow.{r}" for r in m.INSTALLABLE_ROLE_IDS} | {"lemoncrow.code"}
    skill_names = {p.name for p in (ws / ".claude" / "skills").iterdir()}
    # The always-on `lemon` discovery skill ships alongside every optional
    # public skill; it isn't part of PUBLIC_SKILL_NAMES (it's not opt-in).
    assert skill_names == set(m.PUBLIC_SKILL_NAMES) | {"lemoncrow"}


def test_install_optionals_is_a_noop_when_everything_already_installed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _invoke("install", "optionals", "--host", "claude", "--workspace", str(ws), "--yes").exit_code == 0
    result = _invoke("install", "optionals", "--host", "claude", "--workspace", str(ws), "--yes")
    assert result.exit_code == 0
    assert "nothing to install" in result.output


# --------------------------------------------------------------------------- #
# global scope re-invokes the install script (mocked -- no real host CLI)
# --------------------------------------------------------------------------- #


def test_agent_install_global_scope_reinvokes_install_script(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with patch.object(Path, "home", return_value=fake_home):
        with patch("lemoncrow.gateway.cli.commands.agents_skills._repo_root", return_value=REPO_ROOT):
            with patch("lemoncrow.gateway.cli.commands.agents_skills.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = _invoke("agent", "install", "explore", "--host", "claude", "--yes")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    (cmd,), _ = mock_run.call_args
    assert str(REPO_ROOT / "scripts" / "install_claude.sh") in cmd
    # Space-separated form: the install scripts do not parse --roles=<...>.
    roles_flag = cmd.index("--roles")
    assert cmd[roles_flag + 1] == "code,explore"


def test_skill_install_global_scope_calls_build_host_skills_directly(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with patch.object(Path, "home", return_value=fake_home):
        with patch("lemoncrow.gateway.cli.commands.agents_skills._repo_root", return_value=REPO_ROOT):
            with patch("lemoncrow.gateway.cli.commands.agents_skills.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = _invoke("skill", "install", "benchmark", "--host", "claude", "--yes")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    (cmd,), _ = mock_run.call_args
    assert str(REPO_ROOT / "scripts" / "build_host_skills.sh") in cmd
    assert "--include-skills=benchmark" in cmd
    assert str(fake_home / ".lemoncrow" / "claude-plugin" / "skills") in cmd
