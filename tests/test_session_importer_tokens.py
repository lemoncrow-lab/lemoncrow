"""Unit tests for token-extraction logic in host session importers.

Covers all 5 importers: Claude, Codex, Copilot, OpenCode, Gemini.
Each test builds a synthetic fixture with one tool turn + one no-tool turn,
runs the importer, and asserts every token field on the resulting Trace.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

import yaml

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ReasoningStore
from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter
from atelier.gateway.hosts.session_parsers.codex import CodexImporter
from atelier.gateway.hosts.session_parsers.copilot import CopilotImporter
from atelier.gateway.hosts.session_parsers.gemini import GeminiImporter
from atelier.gateway.hosts.session_parsers.opencode import OpenCodeImporter

# =========================================================================
# Helpers
# =========================================================================


def _get_trace(store: ReasoningStore, host: str) -> Trace:
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

    def test_claude_token_fields(self, store: ReasoningStore, tmp_path: Path) -> None:
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

    def test_codex_token_fields(self, store: ReasoningStore, tmp_path: Path) -> None:
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
        assert trace.thinking_tokens == 7  # reasoning_output_tokens
        assert trace.cached_input_tokens == 40  # cached_input_tokens (subset of input)
        assert trace.cache_creation_input_tokens == 0  # hard-coded
        assert trace.model == "codex-model-v1"

        # Turn 1: last_token_usage = {in: 200, out: 60}, 1 tool
        #   tool.input_tokens (exec_command) = dist_out = 60 // 1 = 60
        #   tool.output_tokens (exec_command) = dist_in = 200 // 1 = 200
        _assert_tool_tokens(trace, "exec_command", input_t=60, output_t=200)

    def test_codex_flat_recovers_model_and_usage(self, store: ReasoningStore, tmp_path: Path) -> None:
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
        assert trace.thinking_tokens == 7
        assert trace.cached_input_tokens == 20
        assert trace.cache_creation_input_tokens == 0
        assert trace.user_prompt_tokens > 0


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

    def test_copilot_token_fields(self, store: ReasoningStore, tmp_path: Path) -> None:
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

    def test_opencode_token_fields(self, store: ReasoningStore, tmp_path: Path) -> None:
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

    def test_gemini_token_fields(self, store: ReasoningStore, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "session-test-session.jsonl"
        jsonl_path.write_text("\n".join(self.FIXTURE_LINES))

        importer = GeminiImporter(store)
        result = importer.import_session(jsonl_path, force=True)
        assert result is not None

        trace = _get_trace(store, "gemini")

        # Totals: sum across both gemini events
        #   input = 100 + 80 = 180
        #   output = 50 + 30 = 80
        #   thoughts = 5 + 2 = 7
        #   cached = 20 + 5 = 25  (subset of input, not double-counted in trace.input_tokens)
        assert trace.input_tokens == 180
        assert trace.output_tokens == 80
        assert trace.thinking_tokens == 7
        assert trace.cached_input_tokens == 25
        assert trace.cache_creation_input_tokens == 0  # hard-coded
        assert trace.model == "gemini-3-flash"

        # Turn 1: in=100, out=50, 1 tool
        #   tool.input_tokens (run_shell_command) = dist_out = 50 // 1 = 50
        #   tool.output_tokens (run_shell_command) = dist_in = 100 // 1 = 100
        _assert_tool_tokens(trace, "run_shell_command", input_t=50, output_t=100)
