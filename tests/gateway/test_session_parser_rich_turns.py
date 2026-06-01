from __future__ import annotations

import json

from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns


def test_parse_copilot_rich_turns_handles_pasted_content_subagents_and_string_patch() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "type": "user.message",
                    "timestamp": "2026-05-16T00:00:00Z",
                    "data": {
                        "content": '<pasted_content file="/tmp/paste.txt" size="12 KB" lines="7" />',
                        "attachments": [
                            {
                                "type": "selection",
                                "filePath": "/tmp/spec.md",
                                "displayName": "Selection in spec.md",
                                "text": "# Selection\n\nhello",
                            }
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant.message",
                    "timestamp": "2026-05-16T00:00:05Z",
                    "data": {
                        "model": "gpt-5.4",
                        "toolRequests": [
                            {
                                "name": "TodoWrite",
                                "arguments": {
                                    "todos": [
                                        {
                                            "content": "Ship richer session cards",
                                            "status": "in_progress",
                                            "priority": "high",
                                        }
                                    ]
                                },
                            },
                            {
                                "name": "apply_patch",
                                "arguments": "*** Begin Patch\n*** Update File: /tmp/demo.py\n+print('hello')\n*** End Patch\n",
                            },
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "subagent.started",
                    "timestamp": "2026-05-16T00:00:06Z",
                    "id": "evt-1",
                    "agentId": "agent-123",
                    "data": {
                        "agentName": "explore",
                        "agentDisplayName": "Explore Agent",
                        "agentDescription": "Investigate session parsers",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "subagent.completed",
                    "timestamp": "2026-05-16T00:00:08Z",
                    "id": "evt-2",
                    "agentId": "agent-123",
                    "data": {
                        "agentName": "explore",
                        "agentDisplayName": "Explore Agent",
                    },
                }
            ),
        ]
    )

    turns = parse_session_turns(content, "copilot")

    assert [turn["kind"] for turn in turns] == [
        "pasted_content",
        "attachment",
        "todo_write",
        "file_edit",
        "subagent_event",
        "subagent_event",
    ]
    assert turns[0]["attachments"][0]["path"] == "/tmp/paste.txt"
    assert turns[1]["attachments"][0]["content"] == "# Selection\n\nhello"
    assert turns[2]["todos"][0]["content"] == "Ship richer session cards"
    assert turns[3]["path"] == "/tmp/demo.py"
    assert turns[2]["model"] == "gpt-5.4"
    assert turns[4]["subagent_status"] == "started"
    assert turns[5]["subagent_status"] == "completed"


def test_parse_opencode_todowrite_and_attached_file_contexts() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "_type": "message",
                    "timestamp": 1778891594191,
                    "data": {
                        "role": "user",
                        "summary": {
                            "diffs": [
                                {
                                    "file": "frontend/src/pages/Sessions.tsx",
                                    "patch": "Index: frontend/src/pages/Sessions.tsx",
                                    "after": "export default function Sessions() {}",
                                }
                            ]
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "_type": "part",
                    "role": "assistant",
                    "timestamp": 1778891735304,
                    "data": {
                        "type": "tool",
                        "tool": "todowrite",
                        "state": {
                            "input": {
                                "todos": [
                                    {
                                        "content": "Render TodoWrite as a card",
                                        "status": "pending",
                                        "priority": "high",
                                    }
                                ]
                            }
                        },
                    },
                }
            ),
        ]
    )

    turns = parse_session_turns(content, "opencode")

    assert [turn["kind"] for turn in turns] == ["attachment", "todo_write"]
    assert turns[0]["attachments"][0]["path"] == "frontend/src/pages/Sessions.tsx"
    assert turns[1]["todos"][0]["priority"] == "high"


def test_parse_copilot_tool_execution_complete_keeps_result_content() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool.execution_start",
                    "timestamp": "2026-05-16T00:00:00Z",
                    "data": {
                        "toolCallId": "call-1",
                        "toolName": "view",
                        "arguments": {
                            "path": "/tmp/demo.tsx",
                            "view_range": [10, 12],
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2026-05-16T00:00:01Z",
                    "data": {
                        "toolCallId": "call-1",
                        "result": {
                            "content": "10. first line\n11. second line",
                            "detailedContent": "10. first line\n11. second line",
                        },
                        "toolTelemetry": {"metrics": {"resultForLlmLength": 88}},
                    },
                }
            ),
        ]
    )

    turns = parse_session_turns(content, "copilot")

    assert [turn["kind"] for turn in turns] == ["tool_call"]
    assert turns[0]["tool_name"] == "view"
    assert turns[0]["content"] == "10. first line\n11. second line"


def test_parse_copilot_tool_request_compacts_json_fallback_content() -> None:
    content = json.dumps(
        {
            "type": "assistant.message",
            "timestamp": "2026-05-16T00:00:00Z",
            "data": {
                "toolRequests": [
                    {
                        "name": "view",
                        "arguments": {
                            "path": "/tmp/demo.tsx",
                            "view_range": [10, 12],
                        },
                    }
                ]
            },
        }
    )

    turns = parse_session_turns(content, "copilot")

    assert [turn["kind"] for turn in turns] == ["tool_call"]
    assert turns[0]["content"] == '{"path":"/tmp/demo.tsx","view_range":[10,12]}'


def test_parse_claude_diagnostics_attachment_compacts_json_content() -> None:
    content = json.dumps(
        {
            "type": "attachment",
            "id": "attachment-1",
            "timestamp": "2026-05-16T00:00:00Z",
            "attachment": {
                "type": "diagnostics",
                "files": {
                    "src/app.py": [
                        {
                            "severity": "error",
                            "message": "Missing import",
                        }
                    ]
                },
            },
        }
    )

    turns = parse_session_turns(content, "claude")

    assert [turn["kind"] for turn in turns] == ["attachment"]
    assert turns[0]["content"] == '{"src/app.py":[{"severity":"error","message":"Missing import"}]}'
    assert turns[0]["attachments"][0]["content"] == ('{"src/app.py":[{"severity":"error","message":"Missing import"}]}')


def test_parse_claude_assigns_usage_to_only_one_visible_turn() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-05-16T00:00:05Z",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": 120,
                            "output_tokens": 45,
                            "cache_read_input_tokens": 30,
                            "cache_creation_input_tokens": 10,
                        },
                        "content": [
                            {"type": "thinking", "thinking": "Planning"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                            {"type": "text", "text": "Done."},
                        ],
                    },
                }
            )
        ]
    )

    turns = parse_session_turns(content, "claude")

    tokenized_turns = [turn for turn in turns if any((turn.get("tokens") or {}).values())]
    assert [turn["kind"] for turn in turns] == ["thinking", "shell_command", "agent_message"]
    assert len(tokenized_turns) == 1
    assert tokenized_turns[0]["tokens"] == {
        "in": 120,
        "out": 45,
        "thinking": 0,
        "cache_read": 30,
        "cache_write": 10,
    }


def test_parse_copilot_assigns_output_tokens_once_across_sibling_turns() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant.message",
                    "timestamp": "2026-05-16T00:00:05Z",
                    "data": {
                        "model": "gpt-5.4",
                        "content": "Patched the file.",
                        "reasoningText": "Inspecting the file first",
                        "outputTokens": 88,
                        "toolRequests": [
                            {
                                "name": "edit_file",
                                "arguments": {"path": "/tmp/demo.py", "content": "print('hi')"},
                            }
                        ],
                    },
                }
            )
        ]
    )

    turns = parse_session_turns(content, "copilot")

    tokenized_turns = [turn for turn in turns if any((turn.get("tokens") or {}).values())]
    assert [turn["kind"] for turn in turns] == ["thinking", "agent_message", "file_edit"]
    assert len(tokenized_turns) == 1
    assert tokenized_turns[0]["tokens"] == {"out": 88}


def test_parse_gemini_assigns_usage_to_only_one_turn() -> None:
    content = "\n".join(
        [
            json.dumps(
                {
                    "id": "gemini-msg-1",
                    "type": "gemini",
                    "timestamp": "2026-05-16T00:00:02Z",
                    "model": "gemini-2.5-pro",
                    "tokens": {"input": 120, "output": 35, "thoughts": 8, "cached": 25},
                    "content": "Applied the requested edit.",
                    "toolCalls": [
                        {
                            "name": "write_file",
                            "args": {
                                "path": "frontend/src/pages/Sessions.tsx",
                                "content": "updated",
                            },
                        }
                    ],
                }
            )
        ]
    )

    turns = parse_session_turns(content, "gemini")

    tokenized_turns = [turn for turn in turns if any((turn.get("tokens") or {}).values())]
    assert [turn["kind"] for turn in turns] == ["file_edit", "agent_message"]
    assert len(tokenized_turns) == 1
    assert tokenized_turns[0]["tokens"] == {
        "in": 120,
        "out": 35,
        "thinking": 8,
        "cache_read": 25,
        "cache_write": 0,
    }
