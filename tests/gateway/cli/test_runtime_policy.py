from __future__ import annotations

from types import SimpleNamespace

from lemoncrow.pro.capabilities.owned_agent_session.runtime_policy import (
    RuntimeTurnState,
    cache_control,
    choose_mcp_exposure,
    compact_history,
    output_token_limit,
    task_requests_mutation,
)


def _tools(count: int):
    return [
        SimpleNamespace(
            server_name="github" if index < 3 else f"server{index}",
            name=f"tool_{index}",
            description="generic capability",
        )
        for index in range(count)
    ]


def test_mcp_auto_never_forces_search_for_small_or_ambiguous_catalogs() -> None:
    small = choose_mcp_exposure(_tools(4), "do the task", "auto")
    ambiguous = choose_mcp_exposure(_tools(20), "do the task", "auto")
    assert not small.focused and len(small.tools) == 4
    assert not ambiguous.focused and len(ambiguous.tools) == 20


def test_mcp_auto_focuses_only_an_explicit_server_match() -> None:
    exposure = choose_mcp_exposure(_tools(20), "use github to inspect the issue", "auto")
    assert exposure.focused
    assert exposure.tools
    assert {tool.server_name for tool in exposure.tools} == {"github"}


def test_phase_and_output_governors_are_deterministic() -> None:
    state = RuntimeTurnState()
    assert state.phase == "explore"
    state.record("read", True, {})
    assert state.phase == "execute"
    state.record("edit", True, {})
    state.record("bash", True, {"command": "uv run pytest -q"})
    assert state.phase == "finish"
    assert output_token_limit("balanced", "finish") < output_token_limit("balanced", "execute")
    assert cache_control("off") is None
    assert cache_control("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_mutation_intent_does_not_force_tools_for_explanatory_questions() -> None:
    assert task_requests_mutation("Fix the parser")
    assert task_requests_mutation("Find and fix the parser bug")
    assert task_requests_mutation("Make authentication reliable")
    assert not task_requests_mutation("Explain how to fix the parser")
    assert not task_requests_mutation("Review the parser implementation")


def test_context_compaction_keeps_a_complete_recent_user_turn() -> None:
    messages = [{"role": "system", "content": "stable"}]
    for index in range(12):
        messages.extend(
            [
                {"role": "user", "content": f"task {index} " + ("x" * 300)},
                {"role": "assistant", "content": f"answer {index} " + ("y" * 300)},
            ]
        )
    compacted, changed = compact_history(messages, threshold_tokens=500)
    assert changed
    assert compacted[0] == messages[0]
    assert compacted[1]["content"].startswith("[Compacted prior session evidence]")
    assert compacted[3]["role"] == "user"
    assert compacted[-1]["content"].startswith("answer 11")
