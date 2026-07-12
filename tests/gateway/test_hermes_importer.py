"""HermesImporter: state.db (documented schema v11) -> normalized Trace."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
from lemoncrow.gateway.hosts.session_parsers.hermes import (
    HermesImporter,
    find_hermes_sessions,
    serialize_hermes_session,
)
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT
);
"""


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, reasoning_tokens, title)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_abc",
            "cli",
            "nousresearch/hermes-4-405b",
            1_780_000_000.0,
            1_780_000_900.0,
            12_000,
            3_000,
            8_000,
            500,
            700,
            "Fix Docker Build",
        ),
    )
    rows = [
        (
            "sess_abc",
            "user",
            "How do I fix the docker build failing on arm64 machines",
            None,
            None,
            1_780_000_010.0,
            None,
        ),
        (
            "sess_abc",
            "assistant",
            "The base image lacks an arm64 manifest; pin a multi-arch tag.",
            json.dumps(
                [{"id": "call_1", "function": {"name": "terminal", "arguments": '{"command": "docker buildx ls"}'}}]
            ),
            None,
            1_780_000_020.0,
            "Considering manifest availability...",
        ),
        ("sess_abc", "tool", '{"exit": 0}', None, "terminal", 1_780_000_030.0, None),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, reasoning)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_find_hermes_sessions_orders_by_last_active(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    sessions = find_hermes_sessions(db)
    assert [s["id"] for s in sessions] == ["sess_abc"]
    # last_active = newest message timestamp, not started_at
    assert sessions[0]["last_active"] == 1_780_000_030.0


def test_serialize_parses_as_normalized_turns(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    row = find_hermes_sessions(db)[0]
    content = serialize_hermes_session(row, db)
    turns = parse_session_turns(content, "hermes")
    kinds = [t["kind"] for t in turns]
    assert "user_message" in kinds
    assert "agent_message" in kinds
    user = next(t for t in turns if t["kind"] == "user_message")
    assert "docker build" in user["content"]


def test_import_builds_trace_with_session_totals(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = build_sqlite_store_bundle(tmp_path / "store")
    store.init()
    imported = HermesImporter(store).import_all(db)
    assert len(imported) == 1
    trace = store.history.get_trace(imported[0])
    assert trace is not None
    assert trace.host == "hermes"
    assert trace.session_id == "sess_abc"
    assert trace.model == "nousresearch/hermes-4-405b"
    assert trace.input_tokens == 12_000
    assert trace.output_tokens == 3_000
    assert trace.cached_input_tokens == 8_000
    assert trace.cache_creation_input_tokens == 500
    assert trace.task == "Fix Docker Build"
    tool_names = {t.name for t in trace.tools_called}
    assert "terminal" in tool_names


def test_reimport_dedups_until_new_activity(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = build_sqlite_store_bundle(tmp_path / "store")
    store.init()
    importer = HermesImporter(store)
    assert len(importer.import_all(db)) == 1
    # Unchanged session: second run is a no-op.
    assert importer.import_all(db) == []
    # New message under the same session id -> re-imported.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess_abc", "user", "also check the CI runner architecture please", 1_780_100_000.0),
    )
    conn.commit()
    conn.close()
    assert len(importer.import_all(db)) == 1


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    store = build_sqlite_store_bundle(tmp_path / "store")
    store.init()
    assert HermesImporter(store).import_all(tmp_path / "absent.db") == []
