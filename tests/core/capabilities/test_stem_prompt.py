from __future__ import annotations

from lemoncrow.core.capabilities.owned_agent_session.phase_runner import _phase_user_message
from lemoncrow.core.capabilities.owned_agent_session.stem_prompt import (
    STEM_SYSTEM_PROMPT,
    stem_prompt_for_mode,
)


def test_stem_prompt_is_mode_invariant() -> None:
    assert stem_prompt_for_mode("code") == stem_prompt_for_mode("explore")
    assert stem_prompt_for_mode("research") == stem_prompt_for_mode("plan")


def test_stem_prompt_contains_generic_phrases() -> None:
    for mode in ("code", "explore", "research", "plan", "anything"):
        prompt = stem_prompt_for_mode(mode)
        assert prompt
        assert "coding assistant" in prompt
        assert "Tool usage" in prompt
        # Not mode-specific: the same generic prompt for every mode
        assert prompt == STEM_SYSTEM_PROMPT


def test_phase_user_message_injects_explore_mode() -> None:
    msg = _phase_user_message("survey", "task", "explore")
    assert "[MODE: explore" in msg


def test_phase_user_message_injects_code_mode() -> None:
    msg = _phase_user_message("survey", "task", "code")
    assert "[MODE: code" in msg


def test_stem_prompt_is_comprehensive() -> None:
    assert len(STEM_SYSTEM_PROMPT) > 500
    assert "additional discovery must answer a named unresolved question" in STEM_SYSTEM_PROMPT
    assert "execute each necessary check once" in STEM_SYSTEM_PROMPT
    assert "at most three bullets" in STEM_SYSTEM_PROMPT
