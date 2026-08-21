from __future__ import annotations

import json

from lemoncrow.core.capabilities.session_replay import SUPPORTED_HOSTS, build_replay


def test_pi_replay_uses_only_active_branch() -> None:
    lines = [
        {"type": "session", "version": 3, "id": "s", "timestamp": "2026-08-21T10:00:00Z", "cwd": "/tmp"},
        {
            "type": "message",
            "id": "u",
            "parentId": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "find parse bug"}]},
        },
        {
            "type": "message",
            "id": "a",
            "parentId": "u",
            "message": {
                "role": "assistant",
                "usage": {},
                "content": [{"type": "toolCall", "name": "grep", "arguments": {"pattern": "parse"}}],
            },
        },
        {
            "type": "message",
            "id": "old",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "usage": {},
                "content": [{"type": "toolCall", "name": "read", "arguments": {"path": "old.py"}}],
            },
        },
        {
            "type": "message",
            "id": "new-u",
            "parentId": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "new branch"}]},
        },
        {
            "type": "message",
            "id": "new",
            "parentId": "new-u",
            "message": {
                "role": "assistant",
                "usage": {},
                "content": [{"type": "toolCall", "name": "read", "arguments": {"path": "new.py"}}],
            },
        },
    ]
    replay = build_replay("\n".join(json.dumps(item) for item in lines), host="pi", session_id="s")
    assert "pi" in SUPPORTED_HOSTS
    paths = {turn.get("path") for turn in replay.turns if turn.get("kind") == "file_edit"}
    contents = "\n".join(str(turn.get("content") or "") for turn in replay.turns)
    assert "old.py" not in contents
    assert "new.py" in contents or "new.py" in paths
