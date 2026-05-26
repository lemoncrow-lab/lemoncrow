"""Unit tests for token-extraction logic in host session importers.

Covers all 5 importers: Claude, Codex, Copilot, OpenCode, Gemini.
Each test builds a synthetic fixture with one tool turn + one no-tool turn,
runs the importer, and asserts every token field on the resulting Trace.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

import yaml

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers.antigravity import AntigravityImporter
from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter
from atelier.gateway.hosts.session_parsers.codex import CodexImporter
from atelier.gateway.hosts.session_parsers.copilot import CopilotImporter
from atelier.gateway.hosts.session_parsers.cursor import CursorImporter
from atelier.gateway.hosts.session_parsers.gemini import GeminiImporter
from atelier.gateway.hosts.session_parsers.opencode import OpenCodeImporter
from atelier.infra.runtime.session_report import load_report

# =========================================================================
# Helpers
# =========================================================================


def _get_trace(store: ContextStore, host: str) -> Trace:
    """Return the most recent trace for *host*."""

    traces = store.list_traces(host=host, limit=1)
    assert len(traces) == 1, f"Expected 1 trace for host={host}, got {len(traces)}"
    return traces[0]


def _assert_tool_tokens(trace: Trace, tool_name: str, input_t: int, output_t: int) -> None:
    """Assert a specific tool has the expected token counts."""
    matches = [t for t in trace.tools_called if t.name == tool_name]
    assert len(matches) >= 1, f"Tool '{tool_name}' not found in trace.tools_called"
    assert (
        matches[0].input_tokens == input_t
    ), f"Tool '{tool_name}'.input_tokens: expected {input_t}, got {matches[0].input_tokens}"
    assert (
        matches[0].output_tokens == output_t
    ), f"Tool '{tool_name}'.output_tokens: expected {output_t}, got {matches[0].output_tokens}"


# =========================================================================
# Claude
# =========================================================================


class TestClaudeImporterTokens:
    """Claude: Anthropic's disjoint-cache convention.

    usage.input_tokens, .cache_read_input_tokens, .cache_creation_input_tokens
    are DISJOINT buckets — input_tokens does NOT include cache.

    Per-tool swap: tool.input_tokens = share of LLM output_tokens,
    tool.output_tokens = share of effective input (in + cache_read + cache_create).
    """

    FIXTURE_EVENTS: ClassVar[list[dict[str, Any]]] = [
        # Turn 1 — tool call (Bash)
        {
            "type": "assistant",
            "message": {
                "id": "msg_turn1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
                "content": [{"type": "tool_use", "name": "Bash", "id": "tu1", "input": {"command": "ls"}}],
            },
        },
        # Turn 2 — no tool call (plain text response)
        {
            "type": "assistant",
            "message": {
                "id": "msg_turn2",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "No tools this turn."}],
            },
        },
    ]

    def test_claude_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "test-session-uuid.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in self.FIXTURE_EVENTS))

        importer = ClaudeImporter(store)
        result = importer.import_session("test-slug", jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "claude")

        # Totals from disjoint Anthropic buckets
        assert trace.input_tokens == 100 + 80  # = 180
        assert trace.output_tokens == 50 + 30  # = 80
        assert trace.cached_input_tokens == 20 + 0  # = 20  (cache_read)
        assert trace.cache_creation_input_tokens == 10 + 0  # = 10  (cache_create)
        assert trace.thinking_tokens == 0  # Claude parser does not set thinking_tokens
        assert trace.model == "claude-sonnet-4-6"

        # Turn 1: effective_in = 100 + 20 + 10 = 130, n_tools = 1
        #   tool.input_tokens (Bash) = dist_out = 50 // 1 = 50
        #   tool.output_tokens (Bash) = dist_in = 130 // 1 = 130
        _assert_tool_tokens(trace, "Bash", input_t=50, output_t=130)
        assert trace.usage_entries
        assert trace.usage_entries[0].cost_usd > 0

        report = load_report("test-session-uuid", store.root)
        assert report is not None
        assert report.total_cost_usd > 0
        assert report.started_model == "claude-sonnet-4-6"

    def test_claude_prefers_embedded_session_id_over_filename(self, store: ContextStore, tmp_path: Path) -> None:
        logical_session_id = "logical-session-claude-1"
        filename_session_id = "filename-session-uuid"
        fixture_events = [
            {"type": "meta", "sessionId": logical_session_id},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ]

        jsonl_path = tmp_path / f"{filename_session_id}.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in fixture_events))

        importer = ClaudeImporter(store)
        result = importer.import_session("test-slug", jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "claude")
        assert trace.session_id == logical_session_id
        assert trace.id == f"claude-test-slug-{filename_session_id}"

        artifacts = store.list_raw_artifacts(source="claude", source_session_id=logical_session_id, limit=10)
        assert len(artifacts) == 1
        assert artifacts[0].relative_path == f"{filename_session_id}.jsonl"
        assert artifacts[0].source_path == str(jsonl_path)

    def test_claude_ignores_synthetic_usage_tokens(self, store: ContextStore, tmp_path: Path) -> None:
        fixture_events = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg-real",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 5,
                    },
                    "content": [{"type": "text", "text": "real response"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg-synth",
                    "model": "<synthetic>",
                    "usage": {
                        "input_tokens": 999,
                        "output_tokens": 777,
                        "cache_read_input_tokens": 333,
                        "cache_creation_input_tokens": 111,
                    },
                    "content": [{"type": "text", "text": "synthetic follow-up"}],
                },
            },
        ]
        jsonl_path = tmp_path / "test-session-synthetic.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in fixture_events))

        importer = ClaudeImporter(store)
        result = importer.import_session("test-slug", jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "claude")
        assert trace.input_tokens == 100
        assert trace.output_tokens == 40
        assert trace.cached_input_tokens == 20
        assert trace.cache_creation_input_tokens == 5


# =========================================================================
# Codex (event_msg format)
# =========================================================================


class TestCodexImporterTokens:
    """Codex event_msg format: OpenAI-style accounting.

    total_token_usage.input_tokens includes cached_input_tokens (cached is
    a SUBSET of input). Per-tool distribution uses last_token_usage deltas.

    Per-tool swap: tool.input_tokens = share of LLM output_tokens,
    tool.output_tokens = share of LLM input_tokens.
    """

    FIXTURE_LINES: ClassVar[list[str]] = [
        # Session meta (required for format detection)
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "test-session-id", "timestamp": "2026-05-09T12:00:00Z"},
            }
        ),
        # Set model for this turn
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"model": "codex-model-v1"},
            }
        ),
        # Turn 1 — function call (tool)
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd": "ls"}',
                },
            }
        ),
        # Turn 1 — token_count (distributes to curr_tool_calls)
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 200, "output_tokens": 60},
                        "total_token_usage": {
                            "input_tokens": 200,
                            "output_tokens": 60,
                            "reasoning_output_tokens": 5,
                            "cached_input_tokens": 30,
                        },
                    },
                },
            }
        ),
        # Turn 2 — no tool call (token_count only, no prior function_call)
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 100, "output_tokens": 30},
                        "total_token_usage": {
                            "input_tokens": 300,
                            "output_tokens": 90,
                            "reasoning_output_tokens": 7,
                            "cached_input_tokens": 40,
                        },
                    },
                },
            }
        ),
    ]

    def test_codex_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "rollout-2026-05-09T12-00-00-test-session.jsonl"
        jsonl_path.write_text("\n".join(self.FIXTURE_LINES))

        importer = CodexImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "codex")

        # Totals from the FINAL total_token_usage.
        # input_tokens excludes cached_input_tokens, which are stored separately.
        assert trace.input_tokens == 260
        assert trace.output_tokens == 90
        assert trace.thinking_tokens == 0
        assert trace.cached_input_tokens == 40  # cached_input_tokens (subset of input)
        assert trace.cache_creation_input_tokens == 0  # hard-coded
        assert trace.model == "codex-model-v1"

        # Turn 1: last_token_usage = {in: 200, out: 60}, 1 tool
        #   tool.input_tokens (exec_command) = dist_out = 60 // 1 = 60
        #   tool.output_tokens (exec_command) = dist_in = 200 // 1 = 200
        _assert_tool_tokens(trace, "exec_command", input_t=60, output_t=200)

    def test_codex_flat_recovers_model_and_usage(self, store: ContextStore, tmp_path: Path) -> None:
        fixture_lines = [
            json.dumps(
                {
                    "id": "flat-session-id",
                    "timestamp": "2026-05-10T09:00:00Z",
                    "cwd": "/tmp/project",
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Investigate failing analytics import"}],
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "model": "gpt-5-mini",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 45,
                        "reasoning_tokens": 7,
                        "input_tokens_details": {"cached_tokens": 20},
                    },
                    "content": [{"type": "output_text", "text": "Working on it."}],
                }
            ),
            json.dumps(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd": "sqlite3 analytics.db "select 1""}',
                }
            ),
        ]

        jsonl_path = tmp_path / "rollout-2026-05-10T09-00-00-flat-session.jsonl"
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = CodexImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "codex")

        assert trace.model == "gpt-5-mini"
        assert trace.input_tokens == 100
        assert trace.output_tokens == 45
        assert trace.thinking_tokens == 0
        assert trace.cached_input_tokens == 20
        assert trace.cache_creation_input_tokens == 0
        assert trace.user_prompt_tokens > 0

    def test_codex_mixed_model_sessions_leave_trace_model_blank(self, store: ContextStore, tmp_path: Path) -> None:
        fixture_lines = [
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "test-session-id", "timestamp": "2026-05-09T12:00:00Z"},
                }
            ),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4"}}),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 200, "output_tokens": 60}},
                    },
                }
            ),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4-mini"}}),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 30}},
                    },
                }
            ),
        ]

        jsonl_path = tmp_path / "rollout-2026-05-09T13-00-00-mixed-session.jsonl"
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = CodexImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "codex")
        usage_by_model = {usage.model: usage for usage in trace.model_usages}

        assert trace.model == ""
        assert len(trace.usage_entries) == 2
        assert usage_by_model["gpt-5.4"].output_tokens == 60
        assert usage_by_model["gpt-5.4-mini"].output_tokens == 30

    def test_codex_event_msg_dedupes_repeated_token_rows(self, store: ContextStore, tmp_path: Path) -> None:
        first_turn = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 200, "output_tokens": 60}},
                },
            }
        )
        fixture_lines = [
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "test-session-id", "timestamp": "2026-05-09T12:00:00Z"},
                }
            ),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4"}}),
            first_turn,
            first_turn,
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4-mini"}}),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 30}},
                    },
                }
            ),
        ]

        jsonl_path = tmp_path / "rollout-2026-05-09T14-00-00-duplicate-token-session.jsonl"
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = CodexImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "codex")
        usage_by_model = {usage.model: usage for usage in trace.model_usages}

        assert len(trace.usage_entries) == 2
        assert trace.output_tokens == 90
        assert usage_by_model["gpt-5.4"].input_tokens == 200
        assert usage_by_model["gpt-5.4-mini"].output_tokens == 30


# =========================================================================
# Copilot
# =========================================================================


class TestCopilotImporterTokens:
    """Copilot: modelMetrics totals from session.shutdown + per-turn distribution.

    session.shutdown.data.modelMetrics[].usage has:
      inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, reasoningTokens

    Per-turn: assistant.message.data.outputTokens distributed across toolRequests.
    tool.execution_complete.data.toolTelemetry.metrics.resultForLlmLength // 4
    gives per-tool output_tokens.

    Per-tool swap: tool.input_tokens = share of outputTokens,
    tool.output_tokens = resultForLlmLength // 4.
    """

    EVENTS: ClassVar[list[str]] = [
        # Turn 1 — tool execution start (registers tool in tools_called)
        json.dumps(
            {
                "type": "tool.execution_start",
                "data": {"toolName": "edit", "arguments": {"file_path": "/tmp/test.py"}},
            }
        ),
        # Turn 1 — assistant message announces output + tool request
        json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "outputTokens": 80,
                    "toolRequests": [{"toolCallId": "tc1", "name": "edit"}],
                },
            }
        ),
        # Turn 1 — tool execution complete brings telemetry
        json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc1",
                    "toolTelemetry": {"metrics": {"resultForLlmLength": 400}},
                },
            }
        ),
        # Turn 2 — no tool (plain assistant message, no toolRequests)
        json.dumps(
            {
                "type": "assistant.message",
                "data": {"outputTokens": 30},
            }
        ),
        # Session shutdown — authoritative per-model totals
        json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "modelMetrics": {
                        "copilot-gpt-4": {
                            "usage": {
                                "inputTokens": 300,
                                "outputTokens": 110,
                                "cacheReadTokens": 40,
                                "cacheWriteTokens": 15,
                                "reasoningTokens": 10,
                            }
                        }
                    }
                },
            }
        ),
    ]

    WORKSPACE_YAML = yaml.dump(
        {
            "summary": "test copilot session",
            "created_at": "2026-05-09T12:00:00Z",
        }
    )

    def test_copilot_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        session_dir = tmp_path / "copilot-session-abc123"
        session_dir.mkdir(parents=True)

        (session_dir / "events.jsonl").write_text("\n".join(self.EVENTS))
        (session_dir / "workspace.yaml").write_text(self.WORKSPACE_YAML)

        importer = CopilotImporter(store)
        result = importer.import_session(session_dir, force=True)
        assert result is not None

        trace = _get_trace(store, "copilot")

        # Totals from modelMetrics (Copilot: cached is a SUBSET of input)
        assert trace.input_tokens == 300
        assert trace.output_tokens == 110
        assert trace.thinking_tokens == 10  # reasoningTokens
        assert trace.cached_input_tokens == 40  # cacheReadTokens (subset of input)
        assert trace.cache_creation_input_tokens == 15  # cacheWriteTokens
        assert trace.model == "copilot-gpt-4"

        # Turn 1: outputTokens = 80, 1 tool
        #   tool.input_tokens (edit) = dist_out = 80 // 1 = 80
        #   tool.output_tokens (edit) = resultForLlmLength // 4 = 400 // 4 = 100
        _assert_tool_tokens(trace, "edit", input_t=80, output_t=100)

    def test_copilot_dedupes_repeated_event_rows(self, store: ContextStore, tmp_path: Path) -> None:
        session_dir = tmp_path / "copilot-session-duplicate-rows"
        session_dir.mkdir(parents=True)

        events = [self.EVENTS[0], self.EVENTS[0], *self.EVENTS[1:], self.EVENTS[-1]]
        (session_dir / "events.jsonl").write_text("\n".join(events))
        (session_dir / "workspace.yaml").write_text(self.WORKSPACE_YAML)

        importer = CopilotImporter(store)
        result = importer.import_session(session_dir, force=True)
        assert result is not None

        trace = _get_trace(store, "copilot")
        edit_tools = [tool for tool in trace.tools_called if tool.name == "edit"]

        assert trace.input_tokens == 300
        assert trace.output_tokens == 110
        assert len(trace.usage_entries) == 1
        assert edit_tools and edit_tools[0].count == 1

    def test_copilot_falls_back_to_assistant_output_tokens(self, store: ContextStore, tmp_path: Path) -> None:
        session_dir = tmp_path / "copilot-session-fallback"
        session_dir.mkdir(parents=True)

        events = [
            json.dumps(
                {
                    "type": "session.start",
                    "data": {"startTime": "2026-05-09T12:00:00Z"},
                }
            ),
            json.dumps(
                {
                    "type": "session.model_change",
                    "data": {"newModel": "gpt-5.5", "reasoningEffort": "high"},
                }
            ),
            json.dumps(
                {
                    "type": "user.message",
                    "data": {"content": "x" * 500},
                }
            ),
            json.dumps(
                {
                    "type": "tool.execution_start",
                    "data": {"toolName": "edit", "arguments": {"file_path": "/tmp/test.py"}},
                }
            ),
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {
                        "outputTokens": 80,
                        "toolRequests": [{"toolCallId": "tc1", "name": "edit"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": "tc1",
                        "toolTelemetry": {"metrics": {"resultForLlmLength": 400}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"outputTokens": 30},
                }
            ),
        ]

        (session_dir / "events.jsonl").write_text("\n".join(events))
        (session_dir / "workspace.yaml").write_text(self.WORKSPACE_YAML)

        importer = CopilotImporter(store)
        result = importer.import_session(session_dir, force=True)
        assert result is not None

        trace = _get_trace(store, "copilot")

        # Copilot's assistant.message events only carry outputTokens — no
        # input/cache fields exist in the per-turn payload. Output is captured
        # accurately (80 + 30 = 110). input_tokens=0 reflects the absence of
        # per-turn input data; we no longer fabricate it from user-prompt char/4
        # because that produces a billable number disconnected from real usage.
        assert trace.input_tokens == 0
        assert trace.output_tokens == 110
        assert trace.thinking_tokens == 0
        assert trace.cached_input_tokens == 0
        assert trace.cache_creation_input_tokens == 0
        assert trace.model == "gpt-5.5"
        # The user-prompt char/4 estimate is still surfaced separately for
        # analytics/UX (it's not used for cost computation).
        assert trace.user_prompt_tokens == 125

        _assert_tool_tokens(trace, "edit", input_t=80, output_t=100)

    def test_copilot_uses_assistant_message_model_when_selected_model_is_auto(
        self, store: ContextStore, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "copilot-session-auto-model"
        session_dir.mkdir(parents=True)

        events = [
            json.dumps(
                {
                    "type": "session.start",
                    "data": {
                        "startTime": "2026-05-09T12:00:00Z",
                        "selectedModel": "auto",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user.message",
                    "data": {"content": "x" * 400},
                }
            ),
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {
                        "model": "claude-sonnet-4.6",
                        "outputTokens": 64,
                    },
                }
            ),
        ]

        (session_dir / "events.jsonl").write_text("\n".join(events))
        (session_dir / "workspace.yaml").write_text(self.WORKSPACE_YAML)

        importer = CopilotImporter(store)
        result = importer.import_session(session_dir, force=True)
        assert result is not None

        trace = _get_trace(store, "copilot")

        assert trace.model == "claude-sonnet-4.6"
        # No shutdown/compaction in this fixture, so input_tokens=0 — per-turn
        # assistant.message doesn't expose input. user_prompt char/4 stays out
        # of the billable input field. See test_copilot_falls_back_to… above.
        assert trace.input_tokens == 0
        assert trace.output_tokens == 64
        assert trace.user_prompt_tokens == 100
        assert len(trace.usage_entries) == 1
        assert trace.usage_entries[0].model == "claude-sonnet-4.6"

    def test_copilot_mixed_model_sessions_leave_trace_model_blank(self, store: ContextStore, tmp_path: Path) -> None:
        session_dir = tmp_path / "copilot-session-mixed"
        session_dir.mkdir(parents=True)

        events = [
            json.dumps(
                {
                    "type": "session.shutdown",
                    "timestamp": "2026-05-09T12:00:00Z",
                    "data": {
                        "modelMetrics": {
                            "gpt-5.4": {
                                "usage": {
                                    "inputTokens": 300,
                                    "outputTokens": 110,
                                    "cacheReadTokens": 40,
                                    "cacheWriteTokens": 15,
                                    "reasoningTokens": 10,
                                }
                            },
                            "gpt-5.4-mini": {
                                "usage": {
                                    "inputTokens": 120,
                                    "outputTokens": 30,
                                    "cacheReadTokens": 20,
                                    "cacheWriteTokens": 5,
                                    "reasoningTokens": 2,
                                }
                            },
                        }
                    },
                }
            )
        ]

        (session_dir / "events.jsonl").write_text("\n".join(events))
        (session_dir / "workspace.yaml").write_text(self.WORKSPACE_YAML)

        importer = CopilotImporter(store)
        result = importer.import_session(session_dir, force=True)
        assert result is not None

        trace = _get_trace(store, "copilot")
        usage_by_model = {usage.model: usage for usage in trace.model_usages}

        assert trace.model == ""
        assert len(trace.usage_entries) == 2
        assert set(usage_by_model) == {"gpt-5.4", "gpt-5.4-mini"}
        assert usage_by_model["gpt-5.4"].input_tokens == 300
        assert usage_by_model["gpt-5.4-mini"].output_tokens == 30

    def test_copilot_transcript_without_verified_parent_is_raw_only(self, store: ContextStore, tmp_path: Path) -> None:
        transcript_path = tmp_path / "orphan-transcript.jsonl"
        transcript_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session.start",
                            "data": {
                                "sessionId": "orphan-transcript",
                                "startTime": "2026-05-09T12:05:00Z",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {
                                "toolRequests": [
                                    {
                                        "toolCallId": "call-1",
                                        "name": "read_file",
                                        "arguments": json.dumps(
                                            {
                                                "filePath": "/outside/workspace/src/app.py",
                                            }
                                        ),
                                    }
                                ]
                            },
                        }
                    ),
                ]
            )
        )

        store.record_trace(
            Trace(
                id="copilot-transcript-orphan-transcript",
                session_id="orphan-transcript",
                agent="atelier:code",
                host="copilot",
                domain="coding",
                task="legacy standalone transcript",
                status="success",
            ),
            write_json=False,
        )

        importer = CopilotImporter(store)
        result = importer.import_transcript_file(transcript_path, force=True)

        assert result is None
        assert store.list_traces(host="copilot", limit=10) == []
        assert store.get_trace("copilot-transcript-orphan-transcript") is None

        artifacts = store.list_raw_artifacts(source="copilot", source_session_id="orphan-transcript", limit=10)
        assert [artifact.id for artifact in artifacts] == ["copilot-transcript-orphan-transcript"]

    def test_copilot_transcript_attaches_after_parent_session_is_imported(
        self,
        store: ContextStore,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        (workspace_root / "src").mkdir(parents=True)

        transcript_path = tmp_path / "attached-transcript.jsonl"
        transcript_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session.start",
                            "data": {
                                "sessionId": "attached-transcript",
                                "startTime": "2026-05-09T12:05:00Z",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {
                                "content": "Attach this transcript to the matching session.",
                                "toolRequests": [
                                    {
                                        "toolCallId": "call-2",
                                        "name": "read_file",
                                        "arguments": json.dumps(
                                            {
                                                "filePath": str(workspace_root / "src" / "app.py"),
                                            }
                                        ),
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
        )

        importer = CopilotImporter(store)
        assert importer.import_transcript_file(transcript_path, force=True) is None

        session_dir = tmp_path / "copilot-session-parent"
        session_dir.mkdir(parents=True)
        (session_dir / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.start",
                    "data": {"startTime": "2026-05-09T12:00:00Z"},
                }
            )
        )
        (session_dir / "workspace.yaml").write_text(
            yaml.dump(
                {
                    "summary": "parent copilot session",
                    "created_at": "2026-05-09T12:00:00Z",
                    "cwd": str(workspace_root),
                    "mc_session_id": "logical-session-1",
                }
            )
        )

        parent_id = importer.import_session(session_dir, force=True)
        transcript_id = importer.import_transcript_file(transcript_path)

        assert parent_id is not None
        assert transcript_id == "copilot-transcript-attached-transcript"

        parent_trace = store.get_trace(parent_id)
        assert parent_trace is not None
        assert parent_trace.workspace_path == str(workspace_root)

        transcript_trace = store.get_trace(transcript_id)
        assert transcript_trace is not None
        assert transcript_trace.session_id == "logical-session-1"
        assert transcript_trace.workspace_path == str(workspace_root)
        assert transcript_trace.input_tokens == 0
        assert transcript_trace.output_tokens == 0
        assert transcript_trace.usage_entries == []

        traces = store.list_traces(host="copilot", limit=10)
        assert len(traces) == 2
        assert {trace.session_id for trace in traces} == {"logical-session-1"}

    def test_copilot_import_all_reconciles_stored_orphan_transcript(
        self,
        store: ContextStore,
        tmp_path: Path,
    ) -> None:
        transcript_path = tmp_path / "stale-transcript.jsonl"
        transcript_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session.start",
                            "data": {
                                "sessionId": "stale-transcript",
                                "startTime": "2026-05-09T12:05:00Z",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {
                                "toolRequests": [
                                    {
                                        "toolCallId": "call-3",
                                        "name": "read_file",
                                        "arguments": json.dumps(
                                            {
                                                "filePath": "/outside/workspace/src/app.py",
                                            }
                                        ),
                                    }
                                ]
                            },
                        }
                    ),
                ]
            )
        )

        importer = CopilotImporter(store)
        assert importer.import_transcript_file(transcript_path, force=True) is None

        store.record_trace(
            Trace(
                id="copilot-transcript-stale-transcript",
                session_id="stale-transcript",
                agent="atelier:code",
                host="copilot",
                domain="coding",
                task="legacy standalone transcript",
                status="success",
            ),
            write_json=False,
        )

        transcript_path.unlink()

        importer.import_all(tmp_path, force=True)

        assert store.get_trace("copilot-transcript-stale-transcript") is None


# =========================================================================
# Antigravity
# =========================================================================


class TestAntigravityImporterTokens:
    def test_antigravity_dedupes_repeated_cache_calls(self, store: ContextStore, tmp_path: Path) -> None:
        call = {
            "id": "call-1",
            "timestamp": "2026-05-14T09:00:00Z",
            "userMessage": "Run the build",
            "model": "claude-sonnet-4-6",
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 20,
            "cacheCreationInputTokens": 10,
            "tools": ["bash"],
            "bashCommands": ["pytest"],
            "outputSummary": "Build passed",
        }
        cache_path = tmp_path / "antigravity-results.json"
        cache_path.write_text(json.dumps({"cascades": {"cascade-1": {"calls": [call, call]}}}))

        importer = AntigravityImporter(store)
        result = importer.import_all(root=tmp_path, force=True)
        assert result == ["antigravity-cascade-1"]

        trace = _get_trace(store, "antigravity")
        tools = [tool for tool in trace.tools_called if tool.name == "bash"]

        assert trace.input_tokens == 100
        assert trace.output_tokens == 50
        assert trace.cached_input_tokens == 20
        assert trace.cache_creation_input_tokens == 10
        assert len(trace.usage_entries) == 1
        assert tools and tools[0].count == 1


# =========================================================================
# Cursor
# =========================================================================


class TestCursorImporterTokens:
    def test_cursor_uses_rich_text_and_normalizes_placeholder_models(self, store: ContextStore, tmp_path: Path) -> None:
        db_path = tmp_path / "state.vscdb"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    "bubbleId:test-composer:user-bubble",
                    json.dumps(
                        {
                            "type": 1,
                            "createdAt": "2026-05-14T09:00:00Z",
                            "richText": json.dumps(
                                {
                                    "root": {
                                        "children": [
                                            {
                                                "type": "paragraph",
                                                "children": [
                                                    {
                                                        "type": "text",
                                                        "text": "Find the failing Cursor billing rows.",
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            ),
                            "tokenCount": {"inputTokens": 0, "outputTokens": 0},
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    "bubbleId:test-composer:assistant-bubble",
                    json.dumps(
                        {
                            "type": 2,
                            "createdAt": "2026-05-14T09:01:00Z",
                            "modelInfo": {"modelName": "composer-2"},
                            "richText": json.dumps(
                                {
                                    "root": {
                                        "children": [
                                            {
                                                "type": "paragraph",
                                                "children": [
                                                    {
                                                        "type": "text",
                                                        "text": "Estimated output from rich text.",
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            ),
                            "tokenCount": {"inputTokens": 0, "outputTokens": 0},
                            "codeBlocks": [],
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) SELECT key, value FROM cursorDiskKV WHERE key = ?",
                ("bubbleId:test-composer:assistant-bubble",),
            )
            conn.commit()

        importer = CursorImporter(store)
        results = importer.import_all(root=db_path, force=True)

        assert len(results) == 1

        trace = _get_trace(store, "cursor")

        assert trace.model == "claude-sonnet-4-5"
        assert trace.output_tokens > 0
        assert trace.user_prompt_tokens > 0
        assert len(trace.usage_entries) == 1
        assert trace.usage_entries[0].model == "claude-sonnet-4-5"
        assert trace.usage_entries[0].source_id == "a-assistant-bubble"


# =========================================================================
# OpenCode (SQLite)
# =========================================================================


class TestOpenCodeImporterTokens:
    """OpenCode: step-finish parts carry per-step usage.

    tokens = {input, output, reasoning, cache: {read, write}}.
    input and cache.read are DISJOINT (OpenCode convention, like Anthropic).
    reasoning is OUTSIDE output.

    Per-tool swap: tool.input_tokens = share of output_tokens,
    tool.output_tokens = share of effective input (input + cache.read + cache.write).
    """

    # Milliseconds since epoch for 2026-05-09T12:00:00 UTC
    TS_MS = 1746787200000

    def _create_db(self, db_path: Path) -> None:
        """Create an in-memory-analogue SQLite file with schema + data."""
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    slug TEXT,
                    directory TEXT,
                    title TEXT,
                    version INTEGER,
                    time_created INTEGER,
                    time_updated INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    time_updated INTEGER,
                    data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY,
                    message_id TEXT,
                    session_id TEXT,
                    time_created INTEGER,
                    time_updated INTEGER,
                    data TEXT
                );
            """)

            conn.execute(
                "INSERT INTO session (id, title, time_created) VALUES (?, ?, ?)",
                ("test-session", "test opencode session", self.TS_MS),
            )
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
                (
                    "msg1",
                    "test-session",
                    self.TS_MS,
                    json.dumps(
                        {
                            "role": "assistant",
                            "modelID": "claude-sonnet-4-6",
                            "providerID": "anthropic",
                        }
                    ),
                ),
            )
            # Turn 1 — tool part
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (
                    "p1",
                    "msg1",
                    "test-session",
                    self.TS_MS,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "Bash",
                            "state": {"input": {"command": "ls"}},
                        }
                    ),
                ),
            )
            # Turn 1 — step-finish with tokens
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (
                    "p2",
                    "msg1",
                    "test-session",
                    self.TS_MS + 1,
                    json.dumps(
                        {
                            "type": "step-finish",
                            "tokens": {
                                "input": 100,
                                "output": 50,
                                "reasoning": 5,
                                "cache": {"read": 20, "write": 10},
                            },
                        }
                    ),
                ),
            )
            # Turn 2 — no tool (step-finish only)
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (
                    "p3",
                    "msg1",
                    "test-session",
                    self.TS_MS + 2,
                    json.dumps(
                        {
                            "type": "step-finish",
                            "tokens": {
                                "input": 80,
                                "output": 30,
                                "reasoning": 2,
                                "cache": {"read": 5, "write": 3},
                            },
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_opencode_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        db_path = tmp_path / "opencode.db"
        self._create_db(db_path)

        importer = OpenCodeImporter(store)
        session_row = {
            "id": "test-session",
            "title": "test opencode session",
            "time_created": self.TS_MS,
        }
        result = importer._import_session(session_row, db_path, force=True)
        assert result is not None

        trace = _get_trace(store, "opencode")

        # Totals: sum over both step-finish parts
        #   input = 100 + 80 = 180
        #   output = 50 + 30 = 80
        #   reasoning = 5 + 2 = 7
        #   cache.read = 20 + 5 = 25
        #   cache.write = 10 + 3 = 13
        assert trace.input_tokens == 180
        assert trace.output_tokens == 80
        assert trace.thinking_tokens == 7
        assert trace.cached_input_tokens == 25  # cache.read (disjoint from input)
        assert trace.cache_creation_input_tokens == 13  # cache.write (disjoint)
        assert trace.model == "anthropic/claude-sonnet-4-6"
        assert [entry.source_id for entry in trace.usage_entries] == ["p2", "p3"]

        # Turn 1: effective_in = 100 + 20 + 10 = 130, n_tools = 1
        #   tool.input_tokens (Bash) = dist_out = 50 // 1 = 50
        #   tool.output_tokens (Bash) = dist_in = 130 // 1 = 130
        _assert_tool_tokens(trace, "Bash", input_t=50, output_t=130)


# =========================================================================
# Gemini
# =========================================================================


class TestGeminiImporterTokens:
    """Gemini: Google's cached-as-subset convention.

    tokens.input INCLUDES tokens.cached (cached is a SUBSET).
    tokens.thoughts are reasoning tokens tracked separately from output.

    Per-tool swap: tool.input_tokens = share of output_tokens,
    tool.output_tokens = share of input_tokens.
    """

    FIXTURE_LINES: ClassVar[list[str]] = [
        json.dumps(
            {
                "startTime": "2026-05-09T12:00:00Z",
                "sessionId": "test-session",
            }
        ),
        json.dumps(
            {
                "type": "user",
                "content": [{"text": "List files please"}],
            }
        ),
        # Turn 1 — with tool call
        json.dumps(
            {
                "type": "gemini",
                "model": "gemini-3-flash",
                "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                "toolCalls": [{"name": "run_shell_command", "args": {"command": "ls"}}],
            }
        ),
        # Turn 2 — no tool call
        json.dumps(
            {
                "type": "gemini",
                "model": "gemini-3-flash",
                "tokens": {"input": 80, "output": 30, "thoughts": 2, "cached": 5},
            }
        ),
    ]

    def test_gemini_token_fields(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "session-test-session.jsonl"
        jsonl_path.write_text("\n".join(self.FIXTURE_LINES))

        importer = GeminiImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "gemini")

        # Totals: sum across both gemini events, with cached tokens split out.
        #   non-cached input = (100 - 20) + (80 - 5) = 155
        #   output = 50 + 30 = 80
        #   thoughts = 5 + 2 = 7
        #   cached = 20 + 5 = 25  (subset of input, stored separately)
        assert trace.input_tokens == 155
        assert trace.output_tokens == 80
        assert trace.thinking_tokens == 7
        assert trace.cached_input_tokens == 25
        assert trace.cache_creation_input_tokens == 0  # hard-coded
        assert trace.model == "gemini-3-flash"

        # Turn 1: in=100, out=50, 1 tool
        #   tool.input_tokens (run_shell_command) = dist_out = 50 // 1 = 50
        #   tool.output_tokens (run_shell_command) = dist_in = 100 // 1 = 100
        _assert_tool_tokens(trace, "run_shell_command", input_t=50, output_t=100)

    def test_gemini_counts_same_id_events_when_payload_differs(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "session-duplicate-ids.jsonl"
        fixture_lines = [
            json.dumps(
                {
                    "startTime": "2026-05-09T12:00:00Z",
                    "sessionId": "duplicate-session",
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "content": [{"text": "List files please"}],
                }
            ),
            json.dumps(
                {
                    "id": "turn-1",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                }
            ),
            json.dumps(
                {
                    "id": "turn-1",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                    "toolCalls": [{"name": "run_shell_command", "args": {"command": "ls"}}],
                }
            ),
            json.dumps(
                {
                    "id": "turn-2",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 80, "output": 30, "thoughts": 2, "cached": 5},
                }
            ),
            json.dumps(
                {
                    "id": "turn-2",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 80, "output": 30, "thoughts": 2, "cached": 5},
                }
            ),
        ]
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = GeminiImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "gemini")

        # Same-id events with different payloads should both count.
        # Exact duplicate lines should still collapse.
        assert trace.input_tokens == 235
        assert trace.output_tokens == 130
        assert trace.thinking_tokens == 12
        assert trace.cached_input_tokens == 45
        assert trace.model == "gemini-3-flash"

        _assert_tool_tokens(trace, "run_shell_command", input_t=50, output_t=100)

    def test_gemini_dedupes_same_event_id_and_timestamp(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "session-duplicate-event-keys.jsonl"
        fixture_lines = [
            json.dumps(
                {
                    "startTime": "2026-05-09T12:00:00Z",
                    "sessionId": "duplicate-event-session",
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "content": [{"text": "List files please"}],
                }
            ),
            json.dumps(
                {
                    "id": "turn-1",
                    "timestamp": "2026-05-09T12:01:00Z",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                }
            ),
            json.dumps(
                {
                    "id": "turn-1",
                    "timestamp": "2026-05-09T12:01:00Z",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                    "toolCalls": [{"name": "run_shell_command", "args": {"command": "ls"}}],
                }
            ),
            json.dumps(
                {
                    "id": "turn-2",
                    "timestamp": "2026-05-09T12:02:00Z",
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 80, "output": 30, "thoughts": 2, "cached": 5},
                }
            ),
        ]
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = GeminiImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "gemini")

        assert trace.input_tokens == 155
        assert trace.output_tokens == 80
        assert trace.thinking_tokens == 7
        assert trace.cached_input_tokens == 25
        assert trace.model == "gemini-3-flash"

        _assert_tool_tokens(trace, "run_shell_command", input_t=50, output_t=100)

    def test_gemini_tracks_per_model_usage_for_mixed_model_sessions(self, store: ContextStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "session-mixed-models.jsonl"
        fixture_lines = [
            json.dumps({"startTime": "2026-05-09T12:00:00Z", "sessionId": "mixed-model-session"}),
            json.dumps({"type": "user", "content": [{"text": "Compare models"}]}),
            json.dumps(
                {
                    "type": "gemini",
                    "model": "gemini-3-flash-preview",
                    "tokens": {"input": 100, "output": 50, "thoughts": 5, "cached": 20},
                }
            ),
            json.dumps(
                {
                    "type": "gemini",
                    "model": "gemini-3.1-pro-preview",
                    "tokens": {"input": 80, "output": 30, "thoughts": 2, "cached": 5},
                }
            ),
        ]
        jsonl_path.write_text("\n".join(fixture_lines))

        importer = GeminiImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "gemini")
        usage_by_model = {usage.model: usage for usage in trace.model_usages}

        assert trace.model == ""
        assert len(trace.usage_entries) == 2
        assert set(usage_by_model) == {"gemini-3-flash-preview", "gemini-3.1-pro-preview"}
        assert usage_by_model["gemini-3-flash-preview"].input_tokens == 80
        assert usage_by_model["gemini-3-flash-preview"].cached_input_tokens == 20
        assert usage_by_model["gemini-3-flash-preview"].output_tokens == 50
        assert usage_by_model["gemini-3.1-pro-preview"].input_tokens == 75
        assert usage_by_model["gemini-3.1-pro-preview"].cached_input_tokens == 5
        assert usage_by_model["gemini-3.1-pro-preview"].output_tokens == 30

    def test_gemini_reimports_when_content_changes_without_newer_mtime(
        self,
        store: ContextStore,
        tmp_path: Path,
    ) -> None:
        jsonl_path = tmp_path / "session-growing.jsonl"
        jsonl_path.write_text("\n".join(self.FIXTURE_LINES))
        original_stat = jsonl_path.stat()

        importer = GeminiImporter(store)
        first_result = importer.import_session(jsonl_path, force=False)
        assert first_result is not None

        first_trace = _get_trace(store, "gemini")
        assert first_trace.output_tokens == 80

        updated_lines = [
            *self.FIXTURE_LINES,
            json.dumps(
                {
                    "type": "gemini",
                    "model": "gemini-3-flash",
                    "tokens": {"input": 40, "output": 10, "thoughts": 1, "cached": 5},
                }
            ),
        ]
        jsonl_path.write_text("\n".join(updated_lines))
        os.utime(jsonl_path, (original_stat.st_atime, original_stat.st_mtime))

        second_result = importer.import_session(jsonl_path, force=False)
        assert second_result == first_result

        updated_trace = _get_trace(store, "gemini")
        assert updated_trace.input_tokens == 190
        assert updated_trace.output_tokens == 90
        assert updated_trace.cached_input_tokens == 30
