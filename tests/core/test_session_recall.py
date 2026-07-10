from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atelier.core.capabilities import session_recall
from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns


class _FakeCap:
    def __init__(self, recall_passages: list[Any] | None = None) -> None:
        self.archived: list[dict[str, Any]] = []
        self._recall_passages = recall_passages or []

    def archive(self, *, text: str, source: str, agent_id: str, source_ref: str, tags: list[str]) -> Any:
        self.archived.append(
            {"text": text, "source": source, "agent_id": agent_id, "source_ref": source_ref, "tags": tags}
        )
        return SimpleNamespace()

    def recall(self, *, agent_id: str, query: str, top_k: int, tags: list[str]) -> tuple[list[Any], Any]:
        return self._recall_passages[:top_k], SimpleNamespace()


def _transcript(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"message": {"role": role, "content": [{"type": "text", "text": text}]}}


def test_session_snippets_extracts_user_and_assistant(tmp_path: Path) -> None:
    transcript = _transcript(
        tmp_path / "s.jsonl",
        [
            _msg("user", "Please refactor the auth module thoroughly"),
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "edit"},
                        {"type": "text", "text": "I refactored the auth module and added tests"},
                    ],
                }
            },
            _msg("user", "hi"),  # below the minimum length -> skipped
        ],
    )
    snippets = session_recall._session_snippets(transcript)
    assert any("refactor the auth" in s for s in snippets)
    assert any(s.startswith("[assistant]") for s in snippets)
    assert not any(s == "[user] hi" for s in snippets)


def test_index_sessions_incremental(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    project = tmp_path / "proj"
    project.mkdir()
    transcript = _transcript(
        project / "abc.jsonl",
        [
            _msg("user", "Index this conversation about caching strategy"),
            _msg("assistant", "We chose an LRU cache with a 5 minute TTL"),
        ],
    )
    cap = _FakeCap()
    result = session_recall.index_sessions(root, paths=[transcript], capability=cap)
    assert result["sessions"] == 1
    assert result["indexed"] == 2
    assert result["skipped"] == 0
    assert cap.archived[0]["agent_id"] == "session-recall"
    assert cap.archived[0]["source"] == "trace"
    assert "project:proj" in cap.archived[0]["tags"]
    # "agent:any" lets the existing memory(op=recall) tool surface these for any agent_id
    assert "agent:any" in cap.archived[0]["tags"]

    cap2 = _FakeCap()
    result2 = session_recall.index_sessions(root, paths=[transcript], capability=cap2)
    assert result2["skipped"] == 1
    assert result2["indexed"] == 0
    assert cap2.archived == []


def test_index_reindexes_after_change(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    project = tmp_path / "proj"
    project.mkdir()
    transcript = _transcript(project / "abc.jsonl", [_msg("user", "first version of the session content")])
    session_recall.index_sessions(root, paths=[transcript], capability=_FakeCap())

    _transcript(transcript, [_msg("user", "second version of the session content now")])
    future = time.time() + 10
    os.utime(transcript, (future, future))

    cap = _FakeCap()
    result = session_recall.index_sessions(root, paths=[transcript], capability=cap)
    assert result["sessions"] == 1
    assert result["indexed"] == 1


def test_recall_maps_passages(tmp_path: Path) -> None:
    passage = SimpleNamespace(
        text="LRU cache TTL",
        source_ref="abc",
        tags=["session-recall"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cap = _FakeCap(recall_passages=[passage])
    out = session_recall.recall(tmp_path / ".atelier", "cache strategy", top_k=5, capability=cap)
    assert out == [
        {
            "text": "LRU cache TTL",
            "session": "abc",
            "tags": ["session-recall"],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]


def test_recall_uses_dedicated_recall_db(tmp_path: Path) -> None:
    # Recall must write to its own recall.db, isolated from the main atelier.db.
    cap = session_recall._capability(tmp_path / ".atelier")
    assert cap._store.db_path.name == "recall.db"


def test_recall_fail_open(tmp_path: Path) -> None:
    class _Boom:
        def recall(self, **_kwargs: Any) -> tuple[list[Any], Any]:
            raise RuntimeError("store down")

    assert session_recall.recall(tmp_path, "q", capability=_Boom()) == []


def test_empty_session_marks_state_without_indexing(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    project = tmp_path / "proj"
    project.mkdir()
    transcript = _transcript(project / "empty.jsonl", [{"message": {"role": "system", "content": "noise"}}])
    cap = _FakeCap()
    result = session_recall.index_sessions(root, paths=[transcript], capability=cap)
    assert result["sessions"] == 0
    assert result["indexed"] == 0
    assert cap.archived == []

    result2 = session_recall.index_sessions(root, paths=[transcript], capability=_FakeCap())
    assert result2["skipped"] == 1


def test_index_sessions_budget_prefers_newest_unindexed(tmp_path: Path) -> None:
    # Regression: the per-run cap must apply to *unindexed* sessions, newest
    # first — not to the raw (arbitrary-order) path list, which used to let a
    # backlog of already-indexed sessions starve never-indexed ones.
    root = tmp_path / ".atelier"
    project = tmp_path / "proj"
    project.mkdir()
    paths = []
    for i, name in enumerate(["old", "mid", "new"]):
        p = _transcript(project / f"{name}.jsonl", [_msg("user", f"content for the {name} session number {i}")])
        ts = 1000 + i * 100  # ascending mtime: old < mid < new
        os.utime(p, (ts, ts))
        paths.append(p)

    # Pre-index "old" so it is already current in the state.
    session_recall.index_sessions(root, paths=[paths[0]], capability=_FakeCap())

    # "old" is listed first but already indexed; with budget 1 the run must spend
    # it on the newest *unindexed* session ("new"), not skip everything.
    cap = _FakeCap()
    result = session_recall.index_sessions(root, paths=paths, capability=cap, max_sessions=1)
    assert result["skipped"] == 1  # "old" filtered out, did not consume the budget
    assert result["sessions"] == 1
    assert {a["source_ref"] for a in cap.archived} == {"new"}


def test_snippets_from_turns_keeps_prose_drops_tool_and_thinking() -> None:
    turns = [
        {"kind": "user_message", "content": "please add codex recall coverage now"},
        {"kind": "thinking", "content": "let me think about this carefully"},
        {"kind": "agent_message", "content": "added codex and opencode coverage"},
        {"kind": "shell_command", "content": "rg something"},
        {"kind": "user_message", "content": "ok"},  # below minimum length -> dropped
    ]
    snippets = session_recall._snippets_from_turns(turns)
    assert snippets == [
        "[user] please add codex recall coverage now",
        "[assistant] added codex and opencode coverage",
    ]


def test_codex_prose_snippets_via_parser() -> None:
    # Codex Format A (event_msg-wrapped) must normalize to user/assistant prose.
    content = "\n".join(
        [
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "How do I index codex sessions for recall"},
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "The background indexer now covers codex sessions"},
                }
            ),
            json.dumps({"type": "event_msg", "payload": {"type": "exec_command_end", "command": "rg foo"}}),
        ]
    )
    snippets = session_recall._snippets_from_turns(parse_session_turns(content, "codex"))
    assert any(s.startswith("[user] How do I index codex") for s in snippets)
    assert any(s.startswith("[assistant] The background indexer") for s in snippets)
    assert not any("rg foo" in s for s in snippets)  # shell command excluded


def test_opencode_prose_snippets_via_parser() -> None:
    # OpenCode serialized rows must normalize to user/assistant prose.
    content = "\n".join(
        [
            json.dumps(
                {
                    "_type": "message",
                    "timestamp": 1778891594191,
                    "data": {"role": "user", "text": "How is opencode recall coverage configured"},
                }
            ),
            json.dumps(
                {
                    "_type": "message",
                    "timestamp": 1778891600000,
                    "data": {"role": "assistant", "text": "OpenCode sessions are read from the sqlite db and indexed"},
                }
            ),
            json.dumps(
                {
                    "_type": "part",
                    "role": "assistant",
                    "timestamp": 1778891700000,
                    "data": {"type": "reasoning", "text": "internal reasoning that should be excluded"},
                }
            ),
        ]
    )
    snippets = session_recall._snippets_from_turns(parse_session_turns(content, "opencode"))
    assert any(s.startswith("[user] How is opencode recall") for s in snippets)
    assert any(s.startswith("[assistant] OpenCode sessions are read") for s in snippets)
    assert not any("internal reasoning" in s for s in snippets)  # thinking excluded


def test_copilot_candidates_and_snippets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    session_dir = tmp_path / "cop-sess-1"
    session_dir.mkdir()
    (session_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user.message",
                        "data": {"content": "How do I index copilot sessions for recall coverage"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant.message",
                        "data": {"content": "Copilot sessions are discovered from session-state dirs"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    from atelier.gateway.hosts.session_parsers import copilot as copilot_parser

    monkeypatch.setattr(copilot_parser, "find_copilot_sessions", lambda root=None: iter([session_dir]))

    candidates = session_recall._copilot_candidates(cutoff=time.time() - 3600)
    assert len(candidates) == 1
    cand = candidates[0]
    assert (cand.host, cand.session_id) == ("copilot", "cop-sess-1")
    snippets = cand.load()
    assert any("copilot sessions" in s.lower() for s in snippets)


def test_cursor_candidates_and_snippets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    import time
    from datetime import UTC, datetime

    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:comp-live:u1",
            json.dumps(
                {"type": 1, "text": "How is cursor recall coverage configured in atelier", "createdAt": now_iso}
            ),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:comp-live:a1",
            json.dumps(
                {
                    "type": 2,
                    "text": "Cursor sessions are read from the state.vscdb bubbles",
                    "createdAt": now_iso,
                }
            ),
        ),
    )
    # Stale session outside the window must be skipped.
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:comp-old:u1",
            json.dumps(
                {"type": 1, "text": "old prose long enough to snippet", "createdAt": "2020-01-01T00:00:00+00:00"}
            ),
        ),
    )
    conn.commit()
    conn.close()

    from atelier.gateway.hosts.session_parsers import cursor as cursor_parser

    monkeypatch.setattr(cursor_parser, "find_cursor_db", lambda root=None: db_path)

    candidates = session_recall._cursor_candidates(cutoff=time.time() - 3600)
    assert [c.session_id for c in candidates] == ["comp-live"]
    snippets = candidates[0].load()
    assert any(s.startswith("[user] How is cursor recall") for s in snippets)
    assert any(s.startswith("[assistant] Cursor sessions are read") for s in snippets)


def test_index_sessions_multi_host_tags_and_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".atelier"
    candidates = [
        session_recall._Candidate(
            "codex-1", 100.0, "codex", "codex", lambda: ["[user] codex q", "[assistant] codex a"]
        ),
        session_recall._Candidate("oc-1", 200.0, "opencode", "opencode", lambda: ["[user] opencode q"]),
    ]
    monkeypatch.setattr(session_recall, "_discover_candidates", lambda window_days: list(candidates))

    cap = _FakeCap()
    result = session_recall.index_sessions(root, capability=cap)
    assert result["sessions"] == 2
    assert result["indexed"] == 3
    host_tags = {t for a in cap.archived for t in a["tags"] if t.startswith("host:")}
    assert host_tags == {"host:codex", "host:opencode"}
    assert {a["source_ref"] for a in cap.archived} == {"codex-1", "oc-1"}

    # Second run: change_keys unchanged -> every host session is skipped.
    cap2 = _FakeCap()
    result2 = session_recall.index_sessions(root, capability=cap2)
    assert result2["skipped"] == 2
    assert result2["indexed"] == 0
    assert cap2.archived == []
