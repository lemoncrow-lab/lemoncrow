"""Regression tests for #38 -- ``lc import`` hanging on huge session blobs.

The original hang was catastrophic backtracking inside ``redact()`` (fixed in
4a13c8256 and covered by ``tests/core/test_redaction.py``). Two residual gaps
are covered here:

1. The SQLite-backed opencode/lemoncode importers had **no** size guard: the
   500MB ``_SIZE_LIMIT_BYTES`` skip is enforced only by the file-based
   ``import_paths_with_progress``, which those importers never go through. An
   arbitrarily large serialized session therefore fed straight into
   ``redact()`` + a per-line JSON parse while the import write transaction
   held the SQLite lock.
2. The XML-envelope regexes in the import path (``<task>``/``<prompt>``/...,
   ``<user_query>``, the ``key: value`` agent-settings scrape) still paired an
   unbounded quantifier with a required trailing literal -- the same bug class
   that was just fixed for ``<think>``.
3. The size guard itself then had to be correct: it must never emit a
   byte-truncated (unparseable) line, must not silently destroy a session's
   token accounting by dropping every ``step-finish`` record after the cut,
   must keep ``sha256_original``/``byte_count_original`` describing the true
   original bytes, and must cover *every* in-memory importer (cursor too).

The timing assertions are deliberately loose (seconds, not milliseconds); they
only need to separate "linear" from "quadratic", which on these inputs differ
by two to four orders of magnitude.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from lemoncrow.gateway.hosts.session_parsers import _common
from lemoncrow.gateway.hosts.session_parsers._common import (
    _MAX_SERIALIZED_SESSION_BYTES,
    _RECORD_TRUNCATED_KEY,
    _TRUNCATION_MARKER_TYPE,
    build_normalized_jsonl,
    extract_task_wrapper,
    record_normalized_session,
    truncate_serialized_session,
)
from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
from lemoncrow.gateway.hosts.session_parsers.cursor import CursorImporter, _extract_user_query
from lemoncrow.gateway.hosts.session_parsers.opencode import (
    OpenCodeImporter,
    serialize_opencode_session,
)
from lemoncrow.infra.storage.bundle import StoreBundle

TS_MS = 1746787200000


def _make_opencode_db(db_path: Path, *, tool_output: str, filler_parts: int = 0) -> dict[str, object]:
    """Build a minimal opencode.db holding one session with *tool_output*.

    *filler_parts* appends N small text parts, for exercising the whole-session
    backstop (large in aggregate, no single oversized record).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT
            );
        """)
        conn.execute(
            "INSERT INTO session (id, title, time_created) VALUES (?, ?, ?)",
            ("huge", "huge opencode session", TS_MS),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("m1", "huge", TS_MS, json.dumps({"role": "assistant", "modelID": "m", "providerID": "p"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            (
                "p1",
                "m1",
                "huge",
                TS_MS,
                json.dumps({"type": "tool", "tool": "Bash", "state": {"input": {"command": "cat report.pdf"}}}),
            ),
        )
        # The pathological payload: one oversized tool-output part.
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            ("p2", "m1", "huge", TS_MS + 1, json.dumps({"type": "text", "text": tool_output})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            (
                "p3",
                "m1",
                "huge",
                TS_MS + 2,
                json.dumps({"type": "step-finish", "tokens": {"input": 10, "output": 5, "cache": {}}}),
            ),
        )
        for i in range(filler_parts):
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (
                    f"f{i}",
                    "m1",
                    "huge",
                    TS_MS + 3 + i,
                    json.dumps({"type": "text", "text": f"filler {i} " + "z" * 200}),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return {"id": "huge", "title": "huge opencode session", "time_created": TS_MS}


# ---------------------------------------------------------------------------
# 1. Size guard on the SQLite-backed import path
# ---------------------------------------------------------------------------


def test_truncate_serialized_session_is_a_no_op_under_the_limit() -> None:
    text = '{"_type": "message"}\n{"_type": "part"}'
    result = truncate_serialized_session(text, source="opencode", session_id="s")

    assert result.text is text
    assert result.truncated is False
    assert result.byte_count_original == len(text.encode("utf-8"))
    assert result.sha256_original == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_truncate_serialized_session_cuts_on_a_line_boundary_and_marks_it() -> None:
    lines = [json.dumps({"_type": "part", "id": f"p{i}", "pad": "x" * 200}) for i in range(50)]
    text = "\n".join(lines)
    result = truncate_serialized_session(text, source="opencode", session_id="s", limit=1000)

    assert result.truncated is True
    assert len(result.text) < len(text)
    # Every retained line must still be whole, parseable JSON -- a mid-record
    # cut would silently drop the turn it belongs to.
    events = [json.loads(line) for line in result.text.splitlines()]
    assert all(isinstance(ev, dict) for ev in events)
    assert events[0]["id"] == "p0"

    marker = events[-1]
    assert marker["_type"] == _TRUNCATION_MARKER_TYPE
    assert marker["original_byte_count"] == len(text.encode("utf-8"))
    assert marker["limit_bytes"] == 1000
    assert marker["session_id"] == "s"


def test_truncate_serialized_session_never_emits_a_partial_record() -> None:
    """A first record that alone blows the cap leaves no line boundary to cut on.

    ``head.rfind("\\n")`` returns -1 there; keeping the byte-truncated head emits
    a fragment that fails ``json.loads`` ("Unterminated string"), contradicting
    the whole point of a line-boundary cut. This is the #38 shape exactly:
    opencode writes one ``part`` per line and the report was a single ~20MB
    inlined tool output.
    """
    record = json.dumps({"_type": "part", "id": "p1", "data": {"text": "A" * 5000}})
    assert len(record.encode("utf-8")) > 1000  # one line, over the cap

    result = truncate_serialized_session(record, source="opencode", session_id="s", limit=1000)

    emitted = result.text.splitlines()
    # EVERY emitted line must parse -- json.loads raises on a partial record.
    events = [json.loads(line) for line in emitted]
    assert len(events) == 1
    assert events[0]["_type"] == _TRUNCATION_MARKER_TYPE
    assert result.byte_count_original == len(record.encode("utf-8"))


def test_oversized_opencode_session_is_capped_before_redaction(
    store: StoreBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: an over-limit session imports (no hang) and lands truncated.

    The cap is lowered rather than materializing a real 32MB fixture; the
    default resolves at call time precisely so it stays overridable.
    """
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 8192)
    session_row = _make_opencode_db(tmp_path / "opencode.db", tool_output="A" * 200_000)

    start = time.monotonic()
    trace_id = OpenCodeImporter(store).import_session(session_row, tmp_path / "opencode.db", force=True)
    elapsed = time.monotonic() - start

    assert trace_id == "opencode-huge"
    assert elapsed < 10.0, f"import took {elapsed:.2f}s on a capped session"

    artifact = store.history.get_raw_artifact("opencode-huge")
    assert artifact is not None
    content = store.history.read_raw_artifact_content(artifact)
    # What is *stored* is capped well below the 200KB payload...
    assert len(content.encode("utf-8")) < 8192 + 1024
    # ...and the loss is visible in the artifact itself.
    assert _RECORD_TRUNCATED_KEY in content

    # The retained content still yields a usable trace.
    trace = store.history.get_trace("opencode-huge")
    assert trace is not None
    assert trace.task == "huge opencode session"
    assert "Bash" in {tool.name for tool in trace.tools_called}


def test_capped_session_keeps_token_accounting(
    store: StoreBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized *part* must not cost the session its token/cost accounting.

    Dropping the session tail at the cap also drops every ``step-finish`` record
    after it, so tokens/cost silently read zero -- and because dedup is keyed on
    ``time_updated``, a re-import never recovers it. The oversized payload is
    elided inside its own record instead, so later records survive.
    """
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 4096)
    db_path = tmp_path / "opencode.db"
    session_row = _make_opencode_db(db_path, tool_output="C" * 200_000)

    serialized = serialize_opencode_session("huge", db_path)
    # No malformed lines, and the token record after the oversized part survives.
    events = [json.loads(line) for line in serialized.splitlines()]
    assert any(ev.get("data", {}).get("type") == "step-finish" for ev in events)

    turns = parse_session_turns(serialized, "opencode")
    assert len(turns) > 0, "every turn was dropped with the truncated tail"

    trace_id = OpenCodeImporter(store).import_session(session_row, db_path, force=True)
    assert trace_id == "opencode-huge"
    trace = store.history.get_trace("opencode-huge")
    assert trace is not None
    assert trace.input_tokens == 10
    assert trace.output_tokens == 5


def test_capped_session_artifact_reports_true_original_bytes(
    store: StoreBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``*_original`` must describe the original, not the post-cap text."""
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 4096)
    db_path = tmp_path / "opencode.db"
    session_row = _make_opencode_db(db_path, tool_output="D" * 200_000)

    OpenCodeImporter(store).import_session(session_row, db_path, force=True)
    artifact = store.history.get_raw_artifact("opencode-huge")
    assert artifact is not None

    # Re-serialize with the cap effectively lifted to get the true original.
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 1 << 40)
    original = serialize_opencode_session("huge", db_path)
    assert len(original.encode("utf-8")) > 200_000  # the payload really was huge

    assert artifact.byte_count_original == len(original.encode("utf-8"))
    assert artifact.sha256_original == hashlib.sha256(original.encode("utf-8")).hexdigest()


def test_lemoncode_inherits_the_same_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LemonCode reuses the opencode serializer, so it must inherit the guard.

    Uses many small parts: no single record is oversized, so this exercises the
    whole-session backstop (and its ``source`` stamp) rather than record eliding.
    """
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 4096)
    _make_opencode_db(tmp_path / "opencode.db", tool_output="ok", filler_parts=60)

    out = serialize_opencode_session("huge", tmp_path / "opencode.db", source="lemoncode")

    assert len(out.encode("utf-8")) < 4096 + 512
    events = [json.loads(line) for line in out.splitlines()]
    marker = events[-1]
    assert marker["_type"] == _TRUNCATION_MARKER_TYPE
    assert marker["source"] == "lemoncode"


def test_cursor_ide_session_is_capped(store: StoreBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cursor builds its payload in memory from sqlite too (#38 gap parity).

    ``_import_ide_db`` never passes through ``import_paths_with_progress``, and
    assistant bubble text is stored uncapped, so nothing bounded what reached
    ``redact()`` and the per-line JSON parse.
    """
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 8192)
    db_path = tmp_path / "state.vscdb"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (
                "bubbleId:huge-composer:u1",
                json.dumps({"type": 1, "createdAt": "2026-05-14T09:00:00Z", "text": "summarize the log"}),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (
                "bubbleId:huge-composer:a1",
                json.dumps({"type": 2, "createdAt": "2026-05-14T09:01:00Z", "text": "E" * 200_000}),
            ),
        )

    imported = CursorImporter(store).import_all(db_path, force=True)
    assert "cursor-huge-composer" in imported

    artifact = store.history.get_raw_artifact("cursor-huge-composer")
    assert artifact is not None
    content = store.history.read_raw_artifact_content(artifact)
    assert len(content.encode("utf-8")) < 8192 + 512
    events = [json.loads(line) for line in content.splitlines()]
    assert events[-1]["_type"] == _TRUNCATION_MARKER_TYPE
    assert events[-1]["source"] == "cursor"
    # ...while the artifact still reports the true original size.
    assert artifact.byte_count_original > 200_000


def test_cursor_agent_cli_session_is_capped(
    store: StoreBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cursor-agent CLI importer builds its payload in memory too."""
    monkeypatch.setattr(_common, "_MAX_SERIALIZED_SESSION_BYTES", 8192)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    session_dir = tmp_path / "cursor" / "chats" / "proj" / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text(
        json.dumps({"hasConversation": True, "cwd": str(tmp_path), "createdAtMs": TS_MS, "updatedAtMs": TS_MS}),
        encoding="utf-8",
    )
    with sqlite3.connect(session_dir / "store.db") as conn:
        conn.execute("CREATE TABLE blobs (data BLOB)")
        for i in range(6):
            blob = json.dumps({"role": "assistant", "content": [{"type": "text", "text": f"{i}" + "F" * 6000}]})
            conn.execute("INSERT INTO blobs (data) VALUES (?)", (blob.encode("utf-8"),))

    imported = CursorImporter(store)._import_cursor_agent_chats(force=True)
    assert "cursor-sess-1" in imported

    artifact = store.history.get_raw_artifact("cursor-sess-1")
    assert artifact is not None
    content = store.history.read_raw_artifact_content(artifact)
    assert len(content.encode("utf-8")) < 8192 + 512
    events = [json.loads(line) for line in content.splitlines()]
    assert events[-1]["_type"] == _TRUNCATION_MARKER_TYPE
    assert artifact.byte_count_original > 8192


def test_adversarial_multi_megabyte_session_imports_quickly(store: StoreBundle, tmp_path: Path) -> None:
    """The #38 shape at real scale, under the real cap: a multi-MB session of
    base64-ish output seeded with unmatched ``<think>``/``<task>`` openers.
    Before the fix this class of input pegged a core for 9-12+ minutes while
    holding the import write transaction."""
    payload = ("<think><task>-----BEGIN A PRIVATE KEY-----ab_-cd" + "e" * 16) * 40_000  # ~2.6MB
    assert len(payload.encode("utf-8")) < _MAX_SERIALIZED_SESSION_BYTES  # no truncation: pure regex cost
    session_row = _make_opencode_db(tmp_path / "opencode.db", tool_output=payload)

    start = time.monotonic()
    trace_id = OpenCodeImporter(store).import_session(session_row, tmp_path / "opencode.db", force=True)
    elapsed = time.monotonic() - start

    assert trace_id == "opencode-huge"
    assert elapsed < 30.0, f"import took {elapsed:.2f}s on a ~2.6MB adversarial session"


# ---------------------------------------------------------------------------
# 2. Bounded + guarded XML-envelope regexes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<task>do the thing</task>", "do the thing"),
        ("<task id='1' priority='high'>do the thing</task>", "do the thing"),
        ("<PROMPT>\nmulti\nline\n</PROMPT>", "multi\nline"),
        ("lead <request>payload</request> trail", "payload"),
        ("<question>why?</question>", "why?"),
        ("no wrapper at all", None),
        ("<task>never closed", None),
    ],
)
def test_extract_task_wrapper_still_extracts_normal_input(text: str, expected: str | None) -> None:
    assert extract_task_wrapper(text) == expected


def test_extract_task_wrapper_is_fast_on_dense_unclosed_openers() -> None:
    # ~2MB seeded with an unmatched "<task>" every 32 bytes. Unbounded, this is
    # quadratic (~3.8s for a mere 200KB); bounded-but-unguarded it is linear
    # with a ~13s/MB constant, because every opener still scans a 64KB window.
    blob = ("<task>" + "x" * 26) * 62_500

    start = time.monotonic()
    result = extract_task_wrapper(blob)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 2.0, f"extract_task_wrapper took {elapsed:.2f}s on 2MB of unclosed openers"


def test_extract_task_wrapper_still_matches_alongside_a_wall_of_openers() -> None:
    # The cheap closing-literal pre-check is a fast path, not a filter: a real
    # wrapper must still be found when unmatched openers surround it.
    blob = "<task>real payload</task>" + ("<task>" + "x" * 26) * 20_000
    assert extract_task_wrapper(blob) == "real payload"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<user_query>fix the bug</user_query>", "fix the bug"),
        ("<user_query>\nmulti\nline\n</user_query>", "multi\nline"),
        ("plain text", None),
        ("<user_query>never closed", None),
    ],
)
def test_extract_user_query_still_extracts_normal_input(text: str, expected: str | None) -> None:
    assert _extract_user_query(text) == expected


def test_extract_user_query_is_fast_on_dense_unclosed_openers() -> None:
    blob = ("<user_query>" + "x" * 20) * 62_500  # ~2MB, zero closing tags

    start = time.monotonic()
    result = _extract_user_query(blob)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 2.0, f"_extract_user_query took {elapsed:.2f}s on 2MB of unclosed openers"


def _record_agent_settings_session(store: StoreBundle, text: str) -> str | None:
    content = build_normalized_jsonl(
        [
            {"type": "session", "title": "settings scrape", "timestamp": "2026-05-09T12:00:00+00:00"},
            {
                "type": "message",
                "id": "e1",
                "message": {"role": "system", "content": [{"type": "text", "text": text}]},
            },
        ]
    )
    return record_normalized_session(
        store,
        source="hermes",
        session_id="settings",
        relative_path="settings.jsonl",
        content_path="raw/hermes/settings.jsonl",
        raw_content=content,
        source_mtime=None,
        force=True,
    )


def test_agent_settings_scrape_still_parses_normal_input(store: StoreBundle) -> None:
    assert _record_agent_settings_session(store, "Agent settings\nmodel: opus\nmode: plan") is not None
    trace = store.history.get_trace("hermes-settings")
    assert trace is not None
    assert trace.agent_settings["model"] == "opus"
    assert trace.agent_settings["mode"] == "plan"


def test_agent_settings_scrape_is_fast_on_a_long_colon_free_run(store: StoreBundle) -> None:
    # ``(\w+):\s*(.+)`` re-scans the whole word run at every start offset when
    # no ``:`` follows -- 110s on a mere 200KB run. Bounding ``\w{1,64}``
    # caps the per-position retry, making this linear.
    text = "agent settings\n" + "w" * 2_000_000

    start = time.monotonic()
    assert _record_agent_settings_session(store, text) is not None
    elapsed = time.monotonic() - start

    assert elapsed < 20.0, f"normalized import took {elapsed:.2f}s on a 2MB colon-free run"
