"""Smoke tests for the LemonCrow SDK middleware layer.

Tests:
- LemonCrowMiddleware: unified entry point
- LangChainMiddleware: on_llm_start/end, cache_read extraction, loop detection
- OpenAIAgentsHooks: on_tool_start loop detection, on_agent_start/end
- make_lemoncrow_tools / dispatch: token recording, loop detection
- PrefixCachePlanner wiring in tool_context
- RunLedger.record_call with stable_prefix_hash
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from lemoncrow.sdk.middleware import LemonCrowMiddleware

# ---------------------------------------------------------------------------
# LemonCrowMiddleware
# ---------------------------------------------------------------------------


class TestLemonCrowMiddleware:
    def test_init(self) -> None:
        mw = LemonCrowMiddleware(agent_name="test", task="do work")
        assert mw.agent_name == "test"
        assert mw.task == "do work"
        assert mw._ledger is not None

    def test_context_manager(self) -> None:
        with LemonCrowMiddleware(agent_name="test", task="test") as mw:
            assert mw._ledger.status == "running"
        assert mw._ledger.status == "complete"

    def test_cost_summary_empty(self) -> None:
        mw = LemonCrowMiddleware(agent_name="test", task="test")
        summary = mw.cost_summary()
        assert summary["turns"] == 0
        assert summary["cost_usd"] == 0.0
        assert summary["cache_hit_ratio"] == 0.0

    def test_loop_not_detected_initially(self) -> None:
        mw = LemonCrowMiddleware(agent_name="test", task="test")
        assert not mw.loop_detected()

    def test_watchdog_events_empty_initially(self) -> None:
        mw = LemonCrowMiddleware(agent_name="test", task="test")
        assert mw.watchdog_events() == []

    def test_langchain_returns_middleware(self) -> None:
        from lemoncrow.sdk.langchain_middleware import LangChainMiddleware

        mw = LemonCrowMiddleware(agent_name="test", task="test")
        lc = mw.langchain()
        assert isinstance(lc, LangChainMiddleware)

    def test_openai_hooks_returns_hooks(self) -> None:
        from lemoncrow.sdk.openai_hooks import OpenAIAgentsHooks

        mw = LemonCrowMiddleware(agent_name="test", task="test")
        hooks = mw.openai_hooks()
        assert isinstance(hooks, OpenAIAgentsHooks)

    def test_anthropic_tools_returns_tuple(self) -> None:
        mw = LemonCrowMiddleware(agent_name="test", task="test")
        tool_specs, dispatch = mw.anthropic_tools()
        assert isinstance(tool_specs, list)
        assert callable(dispatch)

    def test_gemini_adk_returns_hooks(self) -> None:
        from lemoncrow.sdk.gemini_adk import GeminiADKMiddleware

        mw = LemonCrowMiddleware(agent_name="test", task="test")
        hooks = mw.gemini_adk()
        assert isinstance(hooks, GeminiADKMiddleware)


# ---------------------------------------------------------------------------
# LangChainMiddleware
# ---------------------------------------------------------------------------


class TestLangChainMiddleware:
    def _make(self) -> Any:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.langchain_middleware import LangChainMiddleware

        ledger = RunLedger(agent="test", task="test task")
        return LangChainMiddleware(ledger), ledger

    def test_on_llm_start_records_call_start(self) -> None:
        handler, _ = self._make()
        handler.on_llm_start({}, ["hello"], run_id="run1")
        assert "run1" in handler._call_start

    def test_on_llm_end_records_to_ledger(self) -> None:
        handler, ledger = self._make()
        handler.on_llm_start({}, ["hello"], run_id="run1")

        mock_response = MagicMock()
        mock_response.generations = [
            [
                MagicMock(
                    generation_info={
                        "usage": {
                            "input_tokens": 500,
                            "output_tokens": 100,
                            "cache_read_input_tokens": 200,
                        }
                    }
                )
            ]
        ]
        mock_response.llm_output = {"model_name": "claude-haiku-4-5"}
        handler.on_llm_end(mock_response, run_id="run1")

        call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
        assert len(call_events) == 1
        payload = call_events[0].payload
        assert payload["cache_read_tokens"] == 200
        assert payload["input_tokens"] == 500

    def test_on_tool_start_loop_detection(self) -> None:
        handler, ledger = self._make()
        # Call same tool 3 times
        for _ in range(3):
            handler.on_tool_start({"name": "grep"}, "pattern", run_id="r")

        alerts = [e for e in ledger.events if e.kind == "watchdog_alert"]
        assert len(alerts) >= 1
        assert alerts[-1].payload["event_type"] == "REPEATED_TOOL_CALL"
        assert alerts[-1].payload["tool"] == "grep"

    def test_on_llm_error_records_error(self) -> None:
        handler, ledger = self._make()
        handler.on_llm_start({}, [], run_id="run-err")
        handler.on_llm_error(ValueError("rate limit"), run_id="run-err")

        notes = [e for e in ledger.events if e.kind == "note" and "llm_error" in e.summary]
        assert len(notes) == 1

    def test_cache_miss_watchdog_fires_after_3_turns(self) -> None:
        handler, ledger = self._make()
        mock_resp = MagicMock()
        mock_resp.generations = [[MagicMock(generation_info={})]]
        mock_resp.llm_output = {"model_name": "gpt-4o", "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}

        for i in range(3):
            handler.on_llm_start({}, [], run_id=f"r{i}")
            handler.on_llm_end(mock_resp, run_id=f"r{i}")

        alerts = [
            e
            for e in ledger.events
            if e.kind == "watchdog_alert" and e.payload.get("event_type") == "PREFIX_CACHE_MISS"
        ]
        assert len(alerts) >= 1


# ---------------------------------------------------------------------------
# OpenAIAgentsHooks
# ---------------------------------------------------------------------------


class TestOpenAIAgentsHooks:
    def _make(self) -> Any:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.openai_hooks import OpenAIAgentsHooks

        ledger = RunLedger(agent="test", task="test task")
        return OpenAIAgentsHooks(ledger), ledger

    def test_on_tool_start_sync_records_tool(self) -> None:
        hooks, _ledger = self._make()
        mock_tool = MagicMock()
        mock_tool.name = "bash"
        hooks.on_tool_start_sync(None, None, mock_tool)

        assert hooks._tool_call_counts.get("bash") == 1

    def test_on_tool_start_sync_loop_detection(self) -> None:
        hooks, ledger = self._make()
        mock_tool = MagicMock()
        mock_tool.name = "read_file"

        for _ in range(3):
            hooks.on_tool_start_sync(None, None, mock_tool)

        alerts = [e for e in ledger.events if e.kind == "watchdog_alert"]
        assert any(a.payload.get("event_type") == "REPEATED_TOOL_CALL" for a in alerts)

    def test_on_agent_start_sync_records_event(self) -> None:
        hooks, ledger = self._make()
        mock_agent = MagicMock()
        mock_agent.name = "coder"
        hooks.on_agent_start_sync(None, mock_agent)

        agent_events = [e for e in ledger.events if e.kind == "agent_message"]
        assert len(agent_events) == 1

    def test_context_manager(self) -> None:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.openai_hooks import OpenAIAgentsHooks

        ledger = RunLedger(agent="test", task="test")
        with OpenAIAgentsHooks(ledger) as hooks:
            assert hooks._ledger.status == "running"
        assert ledger.status == "complete"


# ---------------------------------------------------------------------------
# make_lemoncrow_tools / dispatch
# ---------------------------------------------------------------------------


class TestLemonCrowAnthropicTools:
    def _make(self) -> Any:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.anthropic_tools import make_lemoncrow_tools

        ledger = RunLedger(agent="test", task="anthropic task")
        tool_specs, dispatch = make_lemoncrow_tools(ledger)
        return tool_specs, dispatch, ledger

    def test_tool_specs_contains_telemetry_tool(self) -> None:
        tool_specs, _, _ = self._make()
        assert any(t["name"] == "lemoncrow_session_status" for t in tool_specs)

    def test_dispatch_records_token_usage(self) -> None:
        _, dispatch, ledger = self._make()

        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 1000
        mock_response.usage.output_tokens = 200
        mock_response.usage.cache_read_input_tokens = 400
        mock_response.model = "claude-sonnet-4-6"
        mock_response.stop_reason = "end_turn"
        mock_response.content = []

        dispatch(mock_response)

        call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
        assert len(call_events) == 1
        assert call_events[0].payload["cache_read_tokens"] == 400
        assert call_events[0].payload["input_tokens"] == 1000

    @staticmethod
    def _tool_response(name: str, tool_input: dict[str, Any]) -> MagicMock:
        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.cache_read_input_tokens = 0
        mock_response.model = "claude-haiku-4-5"
        mock_response.stop_reason = "tool_use"

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = name
        tool_block.input = tool_input
        mock_response.content = [tool_block]
        return mock_response

    def test_dispatch_loop_detection_on_tool_use(self) -> None:
        _, dispatch, ledger = self._make()

        for _ in range(4):
            dispatch(self._tool_response("bash", {"command": "ls"}))

        alerts = [
            e
            for e in ledger.events
            if e.kind == "watchdog_alert" and e.payload.get("event_type") == "REPEATED_TOOL_CALL"
        ]
        # Fires exactly once per streak, not on every call past the threshold
        assert len(alerts) == 1
        assert alerts[0].payload["tool"] == "bash"

    def test_dispatch_no_loop_alert_for_distinct_args(self) -> None:
        _, dispatch, ledger = self._make()

        for i in range(5):
            dispatch(self._tool_response("read_file", {"path": f"file_{i}.py"}))

        alerts = [
            e
            for e in ledger.events
            if e.kind == "watchdog_alert" and e.payload.get("event_type") == "REPEATED_TOOL_CALL"
        ]
        assert alerts == []

    def test_dispatch_loop_alert_rearms_after_streak_breaks(self) -> None:
        _, dispatch, ledger = self._make()

        for _ in range(3):
            dispatch(self._tool_response("bash", {"command": "ls"}))
        dispatch(self._tool_response("bash", {"command": "pwd"}))  # breaks streak
        for _ in range(3):
            dispatch(self._tool_response("bash", {"command": "ls"}))

        alerts = [
            e
            for e in ledger.events
            if e.kind == "watchdog_alert" and e.payload.get("event_type") == "REPEATED_TOOL_CALL"
        ]
        assert len(alerts) == 2

    def test_without_telemetry_tool(self) -> None:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.anthropic_tools import make_lemoncrow_tools

        ledger = RunLedger(agent="test", task="test")
        tool_specs, _ = make_lemoncrow_tools(ledger, include_telemetry_tool=False)
        assert all(t["name"] != "lemoncrow_session_status" for t in tool_specs)


# ---------------------------------------------------------------------------
# RunLedger.record_call with stable_prefix_hash
# ---------------------------------------------------------------------------


class TestRunLedgerPrefixHash:
    def test_record_call_stores_prefix_hash(self) -> None:
        from lemoncrow.infra.runtime.run_ledger import RunLedger

        ledger = RunLedger(agent="test", task="test")
        ledger.record_call(
            operation="chat",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=50,
            stable_prefix_hash="abc123",
            prefix_invalidated_reason="",
        )

        call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
        assert len(call_events) == 1
        assert call_events[0].payload["stable_prefix_hash"] == "abc123"
        assert call_events[0].payload["prefix_invalidated_reason"] == ""

    def test_record_call_stores_invalidation_reason(self) -> None:
        from lemoncrow.infra.runtime.run_ledger import RunLedger

        ledger = RunLedger(agent="test", task="test")
        ledger.record_call(
            operation="chat",
            model="claude-haiku-4-5",
            input_tokens=200,
            output_tokens=80,
            stable_prefix_hash="xyz999",
            prefix_invalidated_reason="tool_schema_changed",
        )
        call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
        assert call_events[0].payload["prefix_invalidated_reason"] == "tool_schema_changed"


# ---------------------------------------------------------------------------
# GeminiADKMiddleware
# ---------------------------------------------------------------------------


class TestGeminiADKMiddleware:
    def _make(self) -> Any:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.gemini_adk import GeminiADKMiddleware

        ledger = RunLedger(agent="test", task="gemini task")
        return GeminiADKMiddleware(ledger), ledger

    def test_on_model_end_records_token_usage(self) -> None:
        hooks, ledger = self._make()
        hooks.on_model_start(run_id="r1")
        response = MagicMock()
        response.usage_metadata = {
            "prompt_token_count": 420,
            "candidates_token_count": 84,
            "cached_content_token_count": 120,
            "model": "gemini-2.5-pro",
        }
        hooks.on_model_end(response, run_id="r1")

        call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
        assert len(call_events) == 1
        assert call_events[0].payload["input_tokens"] == 420
        assert call_events[0].payload["output_tokens"] == 84
        assert call_events[0].payload["cache_read_tokens"] == 120

    def test_on_tool_start_loop_detection(self) -> None:
        hooks, ledger = self._make()
        for _ in range(3):
            hooks.on_tool_start("search")
        alerts = [e for e in ledger.events if e.kind == "watchdog_alert"]
        assert any(a.payload.get("event_type") == "REPEATED_TOOL_CALL" for a in alerts)

    def test_context_manager(self) -> None:
        from lemoncrow.infra.runtime.run_ledger import RunLedger
        from lemoncrow.sdk.gemini_adk import GeminiADKMiddleware

        ledger = RunLedger(agent="test", task="test")
        with GeminiADKMiddleware(ledger) as hooks:
            hooks.on_agent_start(agent_name="gemini-agent")
            assert hooks._ledger.status == "running"
        assert ledger.status == "complete"


# ---------------------------------------------------------------------------
# adapters/__init__.py exports
# ---------------------------------------------------------------------------


class TestAdaptersExports:
    def test_exports_all_public_symbols(self) -> None:
        from lemoncrow.gateway.adapters import (
            GeminiADKMiddleware,
            LangChainMiddleware,
            LemonCrowMiddleware,
            OpenAIAgentsHooks,
            make_lemoncrow_tools,
        )

        # All imports must resolve
        assert LemonCrowMiddleware is not None
        assert GeminiADKMiddleware is not None
        assert LangChainMiddleware is not None
        assert OpenAIAgentsHooks is not None
        assert callable(make_lemoncrow_tools)
