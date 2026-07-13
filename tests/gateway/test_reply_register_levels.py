"""Reply-register level resolution + application across generated host surfaces.

Guards the `lc set telegraphic <ultra|lite|off>` pipeline: the ultra
(default) register must be baked verbatim into every generated persona
surface (so the level swap is a deterministic text replacement), and the
swap itself must be clean for every level on every surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.persona_partials import markdown_section
from lemoncrow.core.reply_register import (
    apply_reply_register_level,
    reply_register_body,
    reply_register_level,
)

_REPO = Path(__file__).resolve().parents[2]
_SHARED = _REPO / "integrations" / "agents" / "shared"
_REGISTER = _SHARED / "reply-register.md"
_ULTRA = markdown_section(_REGISTER, "ultra")
_LITE = markdown_section(_REGISTER, "lite")
_BULLET = markdown_section(_REGISTER, "telegraphic-default")
_INVARIANTS = markdown_section(_REGISTER, "invariants")

_GENERATED_PATTERNS = (
    "integrations/claude/plugin/agents/*.md",
    "integrations/antigravity/plugin/agents/*.md",
    "integrations/copilot/agents/*.agent.md",
    "integrations/cursor/rules/lemoncrow.*.mdc",
    "integrations/opencode/agents/*.md",
    "integrations/codex/plugin/skills/*/SKILL.md",
)


def _generated_files_with_register() -> list[Path]:
    out: list[Path] = []
    for pattern in _GENERATED_PATTERNS:
        for path in sorted(_REPO.glob(pattern)):
            if _ULTRA in path.read_text(encoding="utf-8"):
                out.append(path)
    return out


def test_level_resolution_env_settings_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_TELEGRAPHIC", raising=False)
    monkeypatch.delenv("LEMONCROW_ROOT", raising=False)
    assert reply_register_level() == "ultra"

    settings_file = tmp_path / ".lemoncrow" / "plugin_settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"cli.telegraphic": "lite"}), encoding="utf-8")
    assert reply_register_level() == "lite"

    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "off")  # env beats settings
    assert reply_register_level() == "off"
    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "bogus")  # unknown -> ultra
    assert reply_register_level() == "ultra"


def test_reply_register_body_per_level() -> None:
    assert reply_register_body(_SHARED, "ultra") == _ULTRA
    assert reply_register_body(_SHARED, "lite") == _LITE
    assert reply_register_body(_SHARED, "off") == ""


def test_consolidated_reply_register_replaces_legacy_partials() -> None:
    for name in ("reply-register-lite.md", "response-economy.md", "telegraphic-default.md"):
        assert not (_SHARED / name).exists()


def test_apply_ultra_is_noop_and_unknown_text_passes_through() -> None:
    text = f"header\n\n{_ULTRA}\n\ntail"
    assert apply_reply_register_level(text, _SHARED, "ultra") == text
    assert apply_reply_register_level("no register here", _SHARED, "off") == "no register here"


def test_apply_handles_toml_escaped_register() -> None:
    escaped = _ULTRA.replace("\\", "\\\\").replace('"', '\\"')
    text = f'developer_instructions = """\nintro\n\n{escaped}\n\ntail\n"""\n'
    out = apply_reply_register_level(text, _SHARED, "off")
    assert escaped not in out
    assert "intro" in out and "tail" in out


def test_ultra_register_baked_verbatim_and_swappable_everywhere() -> None:
    files = _generated_files_with_register()
    assert files, "no generated surface contains the ultra register verbatim — sync drift?"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert _INVARIANTS in text, f"{path}: always-on reply invariants missing"
        assert _LITE not in text, f"{path}: generated file already carries the lite register"
        assert (
            apply_reply_register_level(text, _SHARED, "ultra") == text
        ), f"{path}: ultra should be a no-op (baked default)"

        lite = apply_reply_register_level(text, _SHARED, "lite")
        assert _ULTRA not in lite and _LITE in lite, f"{path}: lite swap failed"
        assert _INVARIANTS in lite, f"{path}: invariants lost at level=lite"

        off = apply_reply_register_level(text, _SHARED, "off")
        assert _ULTRA not in off, f"{path}: off removal failed"
        assert _INVARIANTS in off, f"{path}: invariants lost at level=off"
        assert "\n\n\n" not in off, f"{path}: off removal left blank-line runs"


def test_telegraphic_bullet_stripped_at_lite_and_off() -> None:
    """The core-discipline telegraphic bullet (own partial, baked into every
    persona incl. read-only roles without a reply-register) must go for
    lite/off and stay for ultra."""
    files = [
        p
        for pattern in _GENERATED_PATTERNS
        for p in sorted(_REPO.glob(pattern))
        if _BULLET in p.read_text(encoding="utf-8")
    ]
    assert files, "no generated surface contains the telegraphic-default bullet — sync drift?"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert _INVARIANTS in text, f"{path}: always-on reply invariants missing"
        assert apply_reply_register_level(text, _SHARED, "ultra") == text
        for lvl in ("lite", "off"):
            out = apply_reply_register_level(text, _SHARED, lvl)
            assert _BULLET not in out, f"{path}: bullet survived level={lvl}"
            assert _INVARIANTS in out, f"{path}: invariants lost at level={lvl}"
            assert "\n\n\n" not in out, f"{path}: bullet removal left blank-line runs at level={lvl}"


def test_core_discipline_body_carries_bullet() -> None:
    from lemoncrow.core.capabilities.workspace_host_overrides import core_discipline_body

    body = core_discipline_body(_SHARED)
    assert _BULLET in body, "core_discipline_body must always render the strict/full text"
    assert _INVARIANTS in body, "core_discipline_body must carry always-on reply invariants"
    assert (
        body.index("Act, don't announce")
        < body.index("Telegraphic by default")
        < body.index("Byte-exact technical content")
    )


def test_codex_render_honors_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities.workspace_host_overrides import _render_codex_mode_body

    body = "intro\n\n{{REPLY_REGISTER}}\n\ntail"
    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "off")
    off = _render_codex_mode_body(body, _REPO)
    assert "Reply register" not in off and "{{" not in off and "\n\n\n" not in off

    off_core = _render_codex_mode_body("intro\n\n{{CORE_DISCIPLINE}}\n\ntail", _REPO)
    assert "Telegraphic by default" not in off_core, "bullet must be stripped from {{CORE_DISCIPLINE}} at off"

    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "lite")
    lite = _render_codex_mode_body(body, _REPO)
    assert _LITE.splitlines()[0] in lite

    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "ultra")
    ultra = _render_codex_mode_body(body, _REPO)
    assert "Reply register" in ultra


def test_claude_agent_text_honors_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities.workspace_host_overrides import workspace_claude_agent_text

    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "off")
    text = workspace_claude_agent_text("code", _REPO, repo_root=_REPO)
    assert "Reply register" not in text

    monkeypatch.setenv("LEMONCROW_TELEGRAPHIC", "ultra")
    text = workspace_claude_agent_text("code", _REPO, repo_root=_REPO)
    assert "Reply register" in text
