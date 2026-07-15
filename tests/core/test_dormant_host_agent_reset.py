"""Codex/OpenCode dormant agent reset: stash our agent files, restore on active,
never touch a user's own agents. Layer-2 parity with Claude's settings.json pop."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.plugin_runtime import reset_host_agents_for_dormancy as reset


def _codex(ws: Path) -> Path:
    d = ws / ".codex" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_codex_dormant_stashes_only_lemoncrow(tmp_path: Path) -> None:
    d = _codex(tmp_path)
    (d / "lemoncrow.code.toml").write_text("x", encoding="utf-8")
    (d / "lemoncrow.solve.toml").write_text("y", encoding="utf-8")
    (d / "my_own.toml").write_text("mine", encoding="utf-8")  # user agent

    assert reset("codex", tmp_path, dormant=True) == "stashed 2"
    assert not (d / "lemoncrow.code.toml").exists()
    assert not (d / "lemoncrow.solve.toml").exists()
    assert (d / "my_own.toml").exists()  # user agent untouched

    # restore
    assert reset("codex", tmp_path, dormant=False) == "restored 2"
    assert (d / "lemoncrow.code.toml").read_text("utf-8") == "x"
    assert (d / "lemoncrow.solve.toml").read_text("utf-8") == "y"
    assert (d / "my_own.toml").exists()


def test_opencode_dormant_stashes_primary_agent(tmp_path: Path) -> None:
    d = tmp_path / ".opencode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "lemoncrow.code.md").write_text("mode: primary", encoding="utf-8")
    assert reset("opencode", tmp_path, dormant=True) == "stashed 1"
    assert not (d / "lemoncrow.code.md").exists()  # builtin agent now applies
    assert reset("opencode", tmp_path, dormant=False) == "restored 1"
    assert (d / "lemoncrow.code.md").exists()


def test_idempotent(tmp_path: Path) -> None:
    d = _codex(tmp_path)
    (d / "lemoncrow.code.toml").write_text("x", encoding="utf-8")
    assert reset("codex", tmp_path, dormant=True) == "stashed 1"
    assert reset("codex", tmp_path, dormant=True) == "noop"  # already stashed
    assert reset("codex", tmp_path, dormant=False) == "restored 1"
    assert reset("codex", tmp_path, dormant=False) == "noop"  # already restored


def test_noop_when_no_agents_or_unknown_host(tmp_path: Path) -> None:
    assert reset("codex", tmp_path, dormant=True) == "noop"  # dir doesn't exist
    assert reset("claude", tmp_path, dormant=True) == "noop"  # not codex/opencode
    assert reset("opencode", tmp_path, dormant=False) == "noop"  # nothing stashed
