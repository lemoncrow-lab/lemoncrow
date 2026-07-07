"""Reply-register level resolution + application across generated host surfaces.

Guards the `atelier set telegraphic <strict|mild|off>` pipeline: the strict
register must be baked verbatim into every generated persona surface (so the
level swap is a deterministic text replacement), and the swap itself must be
clean for every level on every surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier.core.reply_register import (
    apply_reply_register_level,
    reply_register_body,
    reply_register_level,
)

_REPO = Path(__file__).resolve().parents[2]
_SHARED = _REPO / "integrations" / "agents" / "shared"
_STRICT = (_SHARED / "reply-register.md").read_text(encoding="utf-8").strip()
_MILD = (_SHARED / "reply-register-mild.md").read_text(encoding="utf-8").strip()
_BULLET = (_SHARED / "telegraphic-default.md").read_text(encoding="utf-8").strip()

_GENERATED_PATTERNS = (
    "integrations/claude/plugin/agents/*.md",
    "integrations/antigravity/plugin/agents/*.md",
    "integrations/copilot/agents/*.agent.md",
    "integrations/cursor/rules/atelier.*.mdc",
    "integrations/opencode/agents/*.md",
    "integrations/codex/plugin/skills/*/SKILL.md",
)


def _generated_files_with_register() -> list[Path]:
    out: list[Path] = []
    for pattern in _GENERATED_PATTERNS:
        for path in sorted(_REPO.glob(pattern)):
            if _STRICT in path.read_text(encoding="utf-8"):
                out.append(path)
    return out


def test_level_resolution_env_settings_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ATELIER_TELEGRAPHIC", raising=False)
    monkeypatch.delenv("ATELIER_ROOT", raising=False)
    assert reply_register_level() == "strict"

    settings_file = tmp_path / ".atelier" / "plugin_settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"cli.telegraphic": "mild"}), encoding="utf-8")
    assert reply_register_level() == "mild"

    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "off")  # env beats settings
    assert reply_register_level() == "off"
    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "bogus")  # unknown -> strict
    assert reply_register_level() == "strict"


def test_reply_register_body_per_level() -> None:
    assert reply_register_body(_SHARED, "strict") == _STRICT
    assert reply_register_body(_SHARED, "mild") == _MILD
    assert reply_register_body(_SHARED, "off") == ""


def test_apply_strict_is_noop_and_unknown_text_passes_through() -> None:
    text = f"header\n\n{_STRICT}\n\ntail"
    assert apply_reply_register_level(text, _SHARED, "strict") == text
    assert apply_reply_register_level("no register here", _SHARED, "off") == "no register here"


def test_apply_handles_toml_escaped_register() -> None:
    escaped = _STRICT.replace("\\", "\\\\").replace('"', '\\"')
    text = f'developer_instructions = """\nintro\n\n{escaped}\n\ntail\n"""\n'
    out = apply_reply_register_level(text, _SHARED, "off")
    assert escaped not in out
    assert "intro" in out and "tail" in out


def test_strict_register_baked_verbatim_and_swappable_everywhere() -> None:
    files = _generated_files_with_register()
    assert files, "no generated surface contains the strict register verbatim — sync drift?"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert _MILD not in text, f"{path}: generated file already carries the mild register"

        mild = apply_reply_register_level(text, _SHARED, "mild")
        assert _STRICT not in mild and _MILD in mild, f"{path}: mild swap failed"

        off = apply_reply_register_level(text, _SHARED, "off")
        assert _STRICT not in off, f"{path}: off removal failed"
        assert "\n\n\n" not in off, f"{path}: off removal left blank-line runs"


def test_telegraphic_bullet_stripped_at_mild_and_off() -> None:
    """The core-discipline telegraphic bullet (own partial, baked into every
    persona incl. read-only roles without a reply-register) must go for
    mild/off and stay for strict."""
    files = [
        p
        for pattern in _GENERATED_PATTERNS
        for p in sorted(_REPO.glob(pattern))
        if _BULLET in p.read_text(encoding="utf-8")
    ]
    assert files, "no generated surface contains the telegraphic-default bullet — sync drift?"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert apply_reply_register_level(text, _SHARED, "strict") == text
        for lvl in ("mild", "off"):
            out = apply_reply_register_level(text, _SHARED, lvl)
            assert _BULLET not in out, f"{path}: bullet survived level={lvl}"
            assert "\n\n\n" not in out, f"{path}: bullet removal left blank-line runs at level={lvl}"


def test_core_discipline_body_carries_bullet() -> None:
    from atelier.core.capabilities.workspace_host_overrides import core_discipline_body

    body = core_discipline_body(_SHARED)
    assert _BULLET in body, "core_discipline_body must always render the strict/full text"
    assert (
        body.index("Act, don't announce")
        < body.index("Telegraphic by default")
        < body.index("Byte-exact technical content")
    )


def test_codex_render_honors_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from atelier.core.capabilities.workspace_host_overrides import _render_codex_mode_body

    body = "intro\n\n{{REPLY_REGISTER}}\n\ntail"
    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "off")
    off = _render_codex_mode_body(body, _REPO)
    assert "Reply register" not in off and "{{" not in off and "\n\n\n" not in off

    off_core = _render_codex_mode_body("intro\n\n{{CORE_DISCIPLINE}}\n\ntail", _REPO)
    assert "Telegraphic by default" not in off_core, "bullet must be stripped from {{CORE_DISCIPLINE}} at off"

    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "mild")
    mild = _render_codex_mode_body(body, _REPO)
    assert _MILD.splitlines()[0] in mild

    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "strict")
    strict = _render_codex_mode_body(body, _REPO)
    assert "Reply register" in strict


def test_claude_agent_text_honors_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from atelier.core.capabilities.workspace_host_overrides import workspace_claude_agent_text

    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "off")
    text = workspace_claude_agent_text("code", _REPO, repo_root=_REPO)
    assert "Reply register" not in text

    monkeypatch.setenv("ATELIER_TELEGRAPHIC", "strict")
    text = workspace_claude_agent_text("code", _REPO, repo_root=_REPO)
    assert "Reply register" in text
