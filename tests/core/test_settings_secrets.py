"""A key saved with ``lc settings set`` is kept owner-only, and the person is told where it went.

The audit: ``lc settings set llm.openai_api_key ...`` printed "hidden" and wrote the key, in plain text, to a 0644
(world-readable) file. There is no keyring dependency, so the honest fix is the file mode and the sentence. Also covered:
the Anthropic key setting, the remembered agent, and the installed engine's own ``lc settings`` commands.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core import settings as core_settings
from lemoncrow.core.settings import find_by_key, is_secret, secret_location_note, settings_path, write_setting
from lemoncrow.pro.capabilities.review.local_cli import settings_cli
from lemoncrow.pro.capabilities.review.pr_bot import ci_env

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_saved_key_is_owner_only_and_an_old_world_readable_file_is_repaired_on_the_next_write(tmp_path: Path) -> None:
    write_setting(tmp_path, "llm.openai_api_key", "sk-secret")
    assert mode(settings_path(tmp_path)) == 0o600
    # What an earlier install left behind: the same file, readable by everyone.
    settings_path(tmp_path).chmod(0o644)
    write_setting(tmp_path, "llm.backend", "openai")
    assert mode(settings_path(tmp_path)) == 0o600
    stored = json.loads(settings_path(tmp_path).read_text())
    assert stored["llm.openai_api_key"] == "sk-secret" and stored["llm.backend"] == "openai"  # nothing was lost
    core_settings.unset_setting(tmp_path, "llm.backend")
    assert mode(settings_path(tmp_path)) == 0o600
    assert not (tmp_path / ".plugin_settings.json.tmp").exists()


def test_a_leftover_temporary_file_with_loose_permissions_does_not_leak_a_key(tmp_path: Path) -> None:
    leftover = tmp_path / ".plugin_settings.json.tmp"
    leftover.write_text("{}")
    leftover.chmod(0o666)
    write_setting(tmp_path, "llm.openai_api_key", "sk-secret")
    assert mode(settings_path(tmp_path)) == 0o600


def test_the_other_writer_of_the_same_file_keeps_it_owner_only(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import set_recall_settings

    write_setting(tmp_path, "llm.openai_api_key", "sk-secret")
    settings_path(tmp_path).chmod(0o644)
    set_recall_settings(tmp_path, auto_index=True, embedder=None, embed_model=None)
    assert mode(settings_path(tmp_path)) == 0o600
    assert json.loads(settings_path(tmp_path).read_text())["llm.openai_api_key"] == "sk-secret"


def test_the_settings_that_hold_keys_are_the_secret_ones() -> None:
    for key in ("llm.openai_api_key", "llm.anthropic_api_key", "service.api_key"):
        spec = find_by_key(key)
        assert spec is not None and is_secret(spec)
    for key in ("review.agent", "llm.backend", "llm.openai_model"):
        spec = find_by_key(key)
        assert spec is not None and not is_secret(spec)
    secret = find_by_key("llm.openai_api_key")
    assert secret is not None
    note = secret_location_note("/home/me/.lemoncrow", secret)
    assert (
        "plain text" in note and "/home/me/.lemoncrow/plugin_settings.json" in note and "readable only by you" in note
    )
    plain = find_by_key("review.agent")
    assert plain is not None and secret_location_note("/x", plain) == ""


def test_a_saved_anthropic_key_selects_the_anthropic_route_and_an_exported_one_wins() -> None:
    env = {"LEMONCROW_ANTHROPIC_API_KEY": "sk-ant-saved"}
    route = ci_env.resolve_model_route(env)
    assert route is not None and route.provider == "anthropic" and route.credential == "LEMONCROW_ANTHROPIC_API_KEY"
    ci_env.apply_model_route(route, env)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-saved"  # the SDK-free route reads the vendor's own name
    exported = {"LEMONCROW_ANTHROPIC_API_KEY": "sk-ant-saved", "ANTHROPIC_API_KEY": "sk-ant-exported"}
    route = ci_env.resolve_model_route(exported)
    assert route is not None and route.credential == "ANTHROPIC_API_KEY"
    ci_env.apply_model_route(route, exported)
    assert exported["ANTHROPIC_API_KEY"] == "sk-ant-exported"
    assert ci_env.resolve_model_route({}) is None


def test_a_key_exported_in_the_shell_wins_over_a_saved_one_for_both_vendors(tmp_path: Path) -> None:
    """The docs say so for Anthropic and OpenAI alike; OPENAI_API_KEY used to lose to the saved LEMONCROW_OPENAI_API_KEY."""

    write_setting(tmp_path, "llm.openai_api_key", "sk-saved")
    write_setting(tmp_path, "llm.anthropic_api_key", "sk-ant-saved")
    quiet: dict[str, str] = {}
    core_settings.apply_settings_env(tmp_path, env=quiet)
    assert quiet == {"LEMONCROW_OPENAI_API_KEY": "sk-saved", "LEMONCROW_ANTHROPIC_API_KEY": "sk-ant-saved"}
    exported = {"OPENAI_API_KEY": "sk-exported", "ANTHROPIC_API_KEY": "sk-ant-exported"}
    core_settings.apply_settings_env(tmp_path, env=exported)
    assert "LEMONCROW_OPENAI_API_KEY" not in exported and "LEMONCROW_ANTHROPIC_API_KEY" not in exported
    explicit = {"OPENAI_API_KEY": "sk-exported", "LEMONCROW_OPENAI_API_KEY": "sk-namespaced"}
    core_settings.apply_settings_env(tmp_path, env=explicit)
    assert explicit["LEMONCROW_OPENAI_API_KEY"] == "sk-namespaced"  # an explicit LemonCrow variable is untouched


# --------------------------------------------------------------------------- #
# the installed engine's `lc settings`
# --------------------------------------------------------------------------- #


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(where))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LEMONCROW_OPENAI_API_KEY", "LEMONCROW_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return where


def run(*args: str, input: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(settings_cli.settings_group, list(args), input=input)
    return result.exit_code, result.output


def test_lc_settings_with_no_command_shows_the_settings_review_uses(root: Path) -> None:
    code, out = run()
    assert code == 0
    for wanted in ("llm.anthropic_api_key", "llm.openai_api_key", "review.agent", "lc settings set", "show --all"):
        assert wanted in out
    assert "plugin_settings.json" in out and "wins over a saved key" in out
    assert not (root / "plugin_settings.json").exists()  # reading creates nothing


def test_set_a_key_says_where_it_went_and_never_prints_it(root: Path) -> None:
    code, out = run("set", "llm.openai_api_key", "sk-very-secret")
    assert code == 0 and "sk-very-secret" not in out
    assert "✓ llm.openai_api_key saved." in out
    assert f"Stored as plain text in {root / 'plugin_settings.json'}, readable only by you (mode 600)." in out
    assert mode(root / "plugin_settings.json") == 0o600
    assert json.loads((root / "plugin_settings.json").read_text())["llm.openai_api_key"] == "sk-very-secret"
    code, out = run()
    assert "sk-very-secret" not in out and "saved (hidden)" in out
    code, out = run("show", "--json")
    assert "sk-very-secret" not in out and json.loads(out)["llm.openai_api_key"] == "hidden"


def test_a_key_can_be_typed_without_landing_in_shell_history(root: Path) -> None:
    code, out = run("set", "llm.anthropic_api_key", input="sk-ant-typed\n")  # no value: asked, hidden
    assert code == 0 and "sk-ant-typed" not in out and "saved" in out
    assert json.loads((root / "plugin_settings.json").read_text())["llm.anthropic_api_key"] == "sk-ant-typed"
    code, out = run("set", "llm.openai_api_key", "-", input="sk-from-stdin\n")
    assert (
        code == 0 and json.loads((root / "plugin_settings.json").read_text())["llm.openai_api_key"] == "sk-from-stdin"
    )
    code, out = run("set", "llm.anthropic_api_key", input="\n")
    assert code != 0 and "nothing was saved" in out
    code, out = run("set", "llm.backend")  # a value that is not secret must be given
    assert code == 2 and "give a value" in out


def test_an_exported_key_is_said_to_win(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exported")
    code, out = run("set", "llm.anthropic_api_key", "sk-ant-saved")
    assert code == 0 and "your exported ANTHROPIC_API_KEY still wins over the saved key" in out


def test_set_get_reset_and_unknown_keys(root: Path) -> None:
    assert run("set", "review.agent", "claude")[0] == 0
    code, out = run("get", "review.agent")
    assert "review.agent: claude" in out and "one of: claude, codex, opencode" in out
    code, out = run("set", "review.agent", "gemini")
    assert code == 1 and "choices" in out and "gemini" in out
    code, out = run("reset", "review.agent")
    assert code == 0 and "back to its default" in out
    assert run("reset", "review.agent")[1].strip() == "review.agent was already at its default."
    code, out = run("set", "no.such.key", "x")
    assert code == 1 and "unknown setting: no.such.key" in out and "lc settings show --all" in out
    code, out = run("show", "--category", "review")
    assert code == 0 and "review.agent" in out and "review.preview_concurrency" in out


def test_the_full_cli_says_where_the_key_went_instead_of_only_hidden(tmp_path: Path) -> None:
    from lemoncrow.gateway.cli import cli

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "settings", "set", "llm.openai_api_key", "sk-x"])
    assert result.exit_code == 0 and "sk-x" not in result.output
    assert "llm.openai_api_key set to hidden" in result.output
    assert (
        f"Stored as plain text in {tmp_path / 'plugin_settings.json'}, readable only by you (mode 600)."
        in result.output
    )
    assert mode(tmp_path / "plugin_settings.json") == 0o600
