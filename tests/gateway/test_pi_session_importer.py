from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
from lemoncrow.gateway.hosts.session_parsers.pi import PiImporter, select_pi_active_branch
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle


def _line(value: dict[str, object]) -> str:
    return json.dumps(value)


def _fixture() -> str:
    return "\n".join(
        [
            _line(
                {
                    "type": "session",
                    "version": 3,
                    "id": "pi-session",
                    "timestamp": "2026-08-21T10:00:00Z",
                    "cwd": "/tmp/project with spaces",
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "u1",
                    "parentId": None,
                    "timestamp": "2026-08-21T10:00:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "find parser bug"},
                            {"type": "image", "data": "abc", "mimeType": "image/png"},
                        ],
                    },
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "a1",
                    "parentId": "u1",
                    "timestamp": "2026-08-21T10:00:02Z",
                    "message": {
                        "role": "assistant",
                        "provider": "lc",
                        "model": "zen/big-pickle",
                        "usage": {"input": 100, "output": 20, "cacheRead": 40, "cacheWrite": 5},
                        "content": [
                            {"type": "thinking", "thinking": "inspect first"},
                            {"type": "toolCall", "id": "t1", "name": "grep", "arguments": {"pattern": "parse"}},
                        ],
                    },
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "old-u",
                    "parentId": "a1",
                    "timestamp": "2026-08-21T10:00:03Z",
                    "message": {"role": "user", "content": "abandoned branch"},
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "old-a",
                    "parentId": "old-u",
                    "timestamp": "2026-08-21T10:00:04Z",
                    "message": {
                        "role": "assistant",
                        "provider": "lc",
                        "model": "zen/big-pickle",
                        "usage": {"input": 20, "output": 10, "cacheRead": 0, "cacheWrite": 0},
                        "content": [
                            {"type": "toolCall", "id": "old-tool", "name": "read", "arguments": {"path": "old.py"}}
                        ],
                    },
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "new-u",
                    "parentId": "a1",
                    "timestamp": "2026-08-21T10:00:05Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "take the new branch"}]},
                }
            ),
            _line(
                {
                    "type": "message",
                    "id": "new-a",
                    "parentId": "new-u",
                    "timestamp": "2026-08-21T10:00:06Z",
                    "message": {
                        "role": "assistant",
                        "provider": "lc",
                        "model": "zen/big-pickle",
                        "usage": {"input": 50, "output": 15, "cacheRead": 10, "cacheWrite": 0},
                        "content": [{"type": "toolCall", "id": "t2", "name": "read", "arguments": {"path": "new.py"}}],
                    },
                }
            ),
            _line(
                {
                    "type": "compaction",
                    "id": "compact",
                    "parentId": "new-a",
                    "timestamp": "2026-08-21T10:00:07Z",
                    "summary": "summary",
                    "tokensBefore": 1000,
                }
            ),
            _line(
                {
                    "type": "session_info",
                    "id": "name",
                    "parentId": "compact",
                    "timestamp": "2026-08-21T10:00:08Z",
                    "name": "Parser repair",
                }
            ),
            _line(
                {
                    "type": "future_entry",
                    "id": "future",
                    "parentId": "name",
                    "timestamp": "2026-08-21T10:00:09Z",
                    "payload": {"kept": True},
                }
            ),
        ]
    )


def test_active_branch_excludes_abandoned_tree_but_keeps_future_entries() -> None:
    active = select_pi_active_branch(_fixture())
    assert "old-u" not in active
    assert "old-tool" not in active
    assert "new-u" in active
    assert "future_entry" in active
    turns = parse_session_turns(_fixture(), "pi")
    assert any(turn.get("kind") == "thinking" and "inspect first" in str(turn.get("content")) for turn in turns)
    assert any((turn.get("arguments") or {}).get("path") == "new.py" for turn in turns)
    assert not any((turn.get("arguments") or {}).get("path") == "old.py" for turn in turns)


def test_import_preserves_full_raw_artifact_and_reimports_on_mtime(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions with spaces"
    session_dir.mkdir()
    session_path = session_dir / "pi session.jsonl"
    session_path.write_text(_fixture(), encoding="utf-8")
    store = build_sqlite_store_bundle(tmp_path / "store")
    store.init()
    importer = PiImporter(store)

    first = importer.import_all(session_dir)
    assert len(first) == 1
    trace = store.history.get_trace(first[0])
    assert trace is not None
    assert trace.host == "pi"
    assert trace.session_id == "pi-session"
    assert trace.task == "Parser repair"
    assert trace.model == "zen/big-pickle"
    assert trace.input_tokens == 150
    assert {tool.name for tool in trace.tools_called} == {"grep", "read"}
    artifact = store.history.get_raw_artifact(trace.raw_artifact_ids[0])
    assert artifact is not None
    assert artifact.source_path == str(session_path)
    raw = store.history.read_raw_artifact_content(artifact)
    assert "old-tool" in raw
    assert "future_entry" in raw

    assert importer.import_all(session_dir) == []
    later = time.time() + 2
    os.utime(session_path, (later, later))
    second = importer.import_all(session_dir)
    assert len(second) == 1
