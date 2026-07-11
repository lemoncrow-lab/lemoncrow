from __future__ import annotations

import json
from datetime import datetime

import pytest

from lemoncrow.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    make_assistant_message,
    make_session_line,
    make_tool_call,
    make_user_message,
    parse_datetime,
)
from lemoncrow.gateway.hosts.session_parsers._session_parser import (
    extract_session_usage_summary,
    parse_session_turns,
)


@pytest.mark.parametrize("value", [10**400, "1" * 30])
def test_parse_datetime_returns_default_for_out_of_range_numeric(value: object) -> None:
    default = datetime(2026, 1, 1)
    # float(10**400) raises OverflowError and a 30-digit ms timestamp overflows
    # datetime.fromtimestamp; both must fall back to the default, not raise.
    assert parse_datetime(value, default=default) == default


@pytest.mark.parametrize("host", ["qwen", "kiro", "roo-code", "antigravity", "goose", "cursor-agent"])
def test_parse_session_turns_supports_normalized_import_hosts(host: str) -> None:
    content = build_normalized_jsonl(
        [
            make_session_line("sess-1", timestamp="2026-05-11T00:00:00Z", title="demo"),
            make_user_message("build a parser", timestamp="2026-05-11T00:00:01Z"),
            make_assistant_message(
                model="gpt-5",
                input_tokens=120,
                output_tokens=40,
                thinking_tokens=10,
                texts=["Running command"],
                thinking_texts=["Need to inspect the schema first."],
                tool_calls=[
                    make_tool_call("bash", {"command": "pytest -q"}),
                    make_tool_call("edit", {"path": "src/app.py", "content": "patch"}),
                ],
                timestamp="2026-05-11T00:00:02Z",
            ),
        ]
    )

    turns = parse_session_turns(content, host)

    assert [turn["kind"] for turn in turns] == [
        "user_message",
        "agent_message",
        "thinking",
        "shell_command",
        "file_edit",
    ]
    assert turns[3]["content"] == "pytest -q"
    assert turns[4]["summary"].startswith("edit(")


def test_extract_session_usage_summary_defaults_missing_normalized_token_buckets() -> None:
    assistant = make_assistant_message(
        model="gpt-5",
        input_tokens=120,
        output_tokens=40,
        texts=["Running command"],
        timestamp="2026-05-11T00:00:02Z",
    )
    usage = assistant["message"]["usage"]
    usage.pop("thinking", None)
    usage.pop("cacheRead", None)
    usage.pop("cacheWrite", None)

    content = build_normalized_jsonl(
        [
            make_session_line("sess-1", timestamp="2026-05-11T00:00:00Z", title="demo"),
            make_user_message("build a parser", timestamp="2026-05-11T00:00:01Z"),
            assistant,
        ]
    )

    summary = extract_session_usage_summary(content, "cursor")

    assert summary["started_model"] == "gpt-5"
    assert summary["models_used"] == {"gpt-5": 1}
    assert summary["total_turns"] == 1
    assert summary["input_tokens"] == 120
    assert summary["output_tokens"] == 40
    assert summary["reasoning_output_tokens"] == 0
    assert summary["thinking_tokens"] == 0
    assert summary["cached_input_tokens"] == 0
    assert summary["cache_creation_input_tokens"] == 0


def test_codex_reasoning_output_is_preserved_as_output_subset() -> None:
    content = "\n".join(
        [
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4"}}),
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "model": "gpt-5.4",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "reasoning_tokens": 12,
                        "input_tokens_details": {"cached_tokens": 20},
                    },
                }
            ),
        ]
    )

    summary = extract_session_usage_summary(content, "codex")

    assert summary["input_tokens"] == 80
    assert summary["cached_input_tokens"] == 20
    assert summary["output_tokens"] == 40
    assert summary["reasoning_output_tokens"] == 12
