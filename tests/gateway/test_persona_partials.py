"""Persona composition drift gates.

``bare.md`` deliberately inlines one core-discipline bullet instead of
``{{CORE_DISCIPLINE}}`` (leanest possible persona — the full partial would pull
in the telegraphic-default and response-economy appendages it already gets via
its register). The copy must stay byte-identical to the shared source.

``auto.md`` composes the shared partials like every other executor mode; its
only bespoke text is the unattended override. Stale inline copies of shared
bullets must not reappear.
"""

from __future__ import annotations

from pathlib import Path

_AGENTS = Path(__file__).resolve().parents[2] / "integrations" / "agents"


def _text(name: str) -> str:
    return (_AGENTS / name).read_text(encoding="utf-8")


def _bullet(name: str, prefix: str) -> str:
    matches = [line for line in _text(name).splitlines() if line.startswith(f"- **{prefix}")]
    assert len(matches) == 1, f"{name}: expected exactly one '{prefix}' bullet, found {len(matches)}"
    return matches[0]


def test_bare_inlines_act_dont_announce_verbatim() -> None:
    bullet = _bullet("shared/core-discipline.md", "Act, don't announce.")
    assert bullet in _text("bare.md"), "bare.md drifted from core-discipline.md: Act, don't announce"


def test_auto_composes_shared_partials() -> None:
    auto = _text("auto.md")
    for token in (
        "{{CORE_DISCIPLINE}}",
        "{{CHANGE_DISCIPLINE}}",
        "{{CODING_GUIDELINES}}",
        "{{TOOL_DISCIPLINE}}",
        "{{REPLY_REGISTER}}",
    ):
        assert token in auto, f"auto.md missing {token}"
    for stale in ("- **Act, don't announce", "- **Approach fails", "- **FIXME", "- **One answer"):
        assert stale not in auto, f"auto.md re-grew an inline copy: {stale}"
    assert (
        "{{DESTRUCTIVE_GUARD}}" not in auto
    ), "auto is unattended: composing the confirmation guard would contradict it"


def test_interactive_executor_modes_compose_destructive_guard() -> None:
    for name in ("bare.md", "code.md", "execute.md", "general.md", "solve.md"):
        assert "{{DESTRUCTIVE_GUARD}}" in _text(name), f"{name} missing {{{{DESTRUCTIVE_GUARD}}}}"
