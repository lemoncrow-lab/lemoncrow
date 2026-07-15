"""Reliable global-mode dormancy swap: AGENTS.md block, skills dir, agent files."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.plugin_runtime import reset_lemoncrow_global_dormancy

START = "<!-- LEMONCROW START -->"
END = "<!-- LEMONCROW END -->"


def _seed_codex_global(home: Path) -> Path:
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "lemoncrow.code.toml").write_text("ours\n", encoding="utf-8")
    (home / "agents" / "myown.toml").write_text("user\n", encoding="utf-8")  # NOT ours
    (home / "plugins" / "lemoncrow" / "skills" / "code").mkdir(parents=True)
    (home / "plugins" / "lemoncrow" / "skills" / "code" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (home / "AGENTS.md").write_text(f"# my own notes\n\n{START}\nLemonCrow guide body\n{END}\n", encoding="utf-8")
    return home


def test_codex_global_dormant_strips_and_stashes(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _seed_codex_global(Path(tmp_path) / "codex_home")  # type: ignore[arg-type]
    monkeypatch.setenv("CODEX_HOME", str(home))

    reset_lemoncrow_global_dormancy("codex", dormant=True)

    # agents: ours stashed, user's kept
    assert not (home / "agents" / "lemoncrow.code.toml").exists()
    assert (home / "agents" / "myown.toml").exists()
    # skills: whole dir moved aside
    assert not (home / "plugins" / "lemoncrow" / "skills").exists()
    assert (home / "plugins" / "lemoncrow" / "skills.lemoncrow-dormant" / "code" / "SKILL.md").exists()
    # AGENTS.md: block stripped, user content kept
    text = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert START not in text and END not in text
    assert "# my own notes" in text


def test_codex_global_active_restores_from_source(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _seed_codex_global(Path(tmp_path) / "codex_home")  # type: ignore[arg-type]
    monkeypatch.setenv("CODEX_HOME", str(home))

    reset_lemoncrow_global_dormancy("codex", dormant=True)
    reset_lemoncrow_global_dormancy("codex", dormant=True)  # idempotent dormant
    reset_lemoncrow_global_dormancy("codex", dormant=False)

    assert (home / "agents" / "lemoncrow.code.toml").exists()  # restored
    assert (home / "plugins" / "lemoncrow" / "skills" / "code" / "SKILL.md").exists()
    assert not (home / "plugins" / "lemoncrow" / "skills.lemoncrow-dormant").exists()
    text = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert START in text and END in text  # block regenerated from source
    assert "# my own notes" in text  # user content preserved


def test_codex_global_double_active_is_idempotent(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _seed_codex_global(Path(tmp_path) / "codex_home")  # type: ignore[arg-type]
    monkeypatch.setenv("CODEX_HOME", str(home))
    reset_lemoncrow_global_dormancy("codex", dormant=True)
    reset_lemoncrow_global_dormancy("codex", dormant=False)
    first = (home / "AGENTS.md").read_text(encoding="utf-8")
    out = reset_lemoncrow_global_dormancy("codex", dormant=False)  # already active
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == first  # no churn
    assert "noop" in out


def test_global_reset_noop_when_no_global_install(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(tmp_path) / "empty_codex"  # type: ignore[arg-type]
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    out = reset_lemoncrow_global_dormancy("codex", dormant=True)
    assert out == "agents:noop skills:noop agents_md:noop"
    assert not (home / "AGENTS.md").exists()  # never created for a workspace-only install


def test_opencode_global_agents_swap(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path(tmp_path) / "oc_home"  # type: ignore[arg-type]
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "lemoncrow.code.md").write_text("ours\n", encoding="utf-8")
    (home / "agents" / "user.md").write_text("keep\n", encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG_HOME", str(home))

    reset_lemoncrow_global_dormancy("opencode", dormant=True)
    assert not (home / "agents" / "lemoncrow.code.md").exists()
    assert (home / "agents" / "user.md").exists()

    reset_lemoncrow_global_dormancy("opencode", dormant=False)
    assert (home / "agents" / "lemoncrow.code.md").exists()
