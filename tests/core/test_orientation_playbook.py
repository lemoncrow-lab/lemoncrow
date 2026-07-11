"""N8: on-demand orientation playbook tests."""

from __future__ import annotations

from lemoncrow.core.capabilities.orientation import available_topics, orientation_playbook


def test_full_playbook_returns_canonical_sequence_and_all_sections() -> None:
    result = orientation_playbook()
    assert result["topic"] is None
    # The canonical sequence is the load-bearing guidance.
    assert result["sequence"] == ["explore", "navigate", "edit", "verify"]
    section_keys = [section["key"] for section in result["sections"]]
    assert section_keys == available_topics()
    # Every phase of the lifecycle is represented in the rendered text.
    for phase in ("explore", "navigate", "edit", "verify"):
        assert phase in result["text"].lower()
    assert "explore -> navigate -> edit -> verify" in result["text"]


def test_topic_filter_returns_only_that_section() -> None:
    result = orientation_playbook("edit")
    assert result["topic"] == "edit"
    assert len(result["sections"]) == 1
    assert result["sections"][0]["key"] == "edit"
    assert "codemod" in result["text"]
    # The focused section must NOT carry sibling sections' bodies.
    assert "reciprocal" not in result["text"].lower()
    assert "callers" not in result["text"] or "edit" in result["text"].lower()


def test_topic_filter_is_case_and_whitespace_insensitive() -> None:
    assert orientation_playbook("  VERIFY ")["topic"] == "verify"


def test_unknown_topic_falls_back_to_overview_not_error() -> None:
    result = orientation_playbook("does-not-exist")
    assert result["topic"] is None
    assert result["unknown_topic"] == "does-not-exist"
    # Caller can recover: the valid topics are advertised in the fallback.
    assert set(result["topics"]) == set(available_topics())
    assert "explore -> navigate -> edit -> verify" in result["text"]


def test_playbook_is_deterministic() -> None:
    assert orientation_playbook() == orientation_playbook()
    assert orientation_playbook("explore") == orientation_playbook("explore")


def test_orient_mcp_tool_registered_hidden_and_returns_playbook() -> None:
    from lemoncrow.core.environment import HIDDEN_LLM_TOOLS, mcp_tool_visible_to_llm
    from lemoncrow.gateway.adapters import mcp_server

    assert "orient" in mcp_server.TOOLS
    # Registered but kept off the advertised surface.
    assert "orient" in HIDDEN_LLM_TOOLS
    assert mcp_tool_visible_to_llm("orient") is False

    handler = mcp_server.TOOLS["orient"]["handler"]
    full = handler({})
    assert full["sequence"] == ["explore", "navigate", "edit", "verify"]
    focused = handler({"topic": "verify"})
    assert focused["topic"] == "verify"
    assert len(focused["sections"]) == 1
