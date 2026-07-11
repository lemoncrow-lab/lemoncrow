"""Tests for live enrichment: real LemonCrow tool calls in session replay.

Verifies the safety contract (edit/bash never mutate/execute), real code_search
hit parsing + endpoint match, and real read outline. code_search uses a fake
engine so the test is deterministic and needs no index.
"""

from __future__ import annotations

from pathlib import Path

import lemoncrow.core.capabilities.session_replay_live as live
from lemoncrow.core.capabilities.session_replay import Episode, Replay, build_replay


class _FakeEngine:
    def __init__(self, hits: list[dict[str, object]]) -> None:
        self._hits = hits

    def tool_explore(self, query: str, **_: object) -> dict[str, object]:
        return {"exact_match": True, "entry_points": self._hits, "files": []}


def _replay_with_edit() -> Replay:
    turns = [
        {"kind": "user_message", "content": "fix it"},
        {"kind": "tool_call", "tool_name": "Grep", "arguments": {"pattern": "Foo"}},
        {"kind": "tool_call", "tool_name": "Read", "arguments": {"file_path": "foo.py"}},
        {"kind": "file_edit", "tool_name": "Edit", "path": "foo.py", "diff": "--- a/foo.py\n+++ b/foo.py\n-x\n+y"},
        {"kind": "shell_command", "tool_name": "Bash", "content": "rm -rf build/"},
    ]
    # Manually flag the grep+read as one collapsed episode ending at index 2.
    ep = Episode(id=1, turn_indices=[1, 2], grep_count=1, read_count=1, query="Foo", after_index=2)
    return Replay(
        host="claude",
        session_id="s",
        model="",
        task="fix it",
        turns=turns,
        collapsed_indices=[1, 2],
        episodes=[ep],
        summary=build_replay("", host="claude", session_id="s").summary,
    )


def test_edit_is_preview_never_written(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine([]))
    target = tmp_path / "foo.py"
    target.write_text("original", encoding="utf-8")
    replay = _replay_with_edit()
    live.enrich_replay(replay, tmp_path)
    edit_turn = replay.turns[3]
    assert edit_turn["lemoncrow"]["mode"] == "preview"
    assert "diff" in edit_turn["lemoncrow"]
    # The file on disk is untouched.
    assert target.read_text(encoding="utf-8") == "original"


def test_bash_is_preview_never_executed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine([]))
    sentinel = tmp_path / "build"
    sentinel.mkdir()
    replay = _replay_with_edit()
    replay.turns[4]["content"] = f"rm -rf {sentinel}"
    live.enrich_replay(replay, tmp_path)
    assert replay.turns[4]["lemoncrow"]["mode"] == "preview"
    assert sentinel.exists()  # command was NOT run


def test_code_search_real_hits_and_endpoint_match(tmp_path: Path, monkeypatch) -> None:
    hits = [{"path": "foo.py", "line": 10, "end_line": 20, "name": "Foo", "kind": "class", "score": 99.0}]
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine(hits))
    replay = _replay_with_edit()
    live.enrich_replay(replay, tmp_path)
    lemoncrow = replay.episodes[0].lemon
    assert lemoncrow is not None
    assert lemoncrow["mode"] == "real"
    assert lemoncrow["hits"][0]["path"] == "foo.py"
    assert lemoncrow["hits"][0]["name"] == "Foo"
    # endpoint = the file edited right after the loop; code_search hit the same file
    assert lemoncrow["endpoint"] == "foo.py"
    assert lemoncrow["matched_endpoint"] is True


def test_real_read_outline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine([]))
    src = tmp_path / "mod.py"
    src.write_text("class A:\n    def m(self):\n        pass\n\ndef top():\n    return 1\n", encoding="utf-8")
    turns = [
        {"kind": "user_message", "content": "read mod"},
        {"kind": "tool_call", "tool_name": "Read", "arguments": {"file_path": "mod.py"}},
    ]
    replay = Replay(
        host="claude",
        session_id="s",
        model="",
        task="",
        turns=turns,
        collapsed_indices=[],
        episodes=[],
        summary=build_replay("", host="claude", session_id="s").summary,
    )
    live.enrich_replay(replay, tmp_path)
    a = replay.turns[1]["lemoncrow"]
    assert a["tool"] == "read"
    assert a["mode"] == "real"
    assert any("class A" in line for line in a["outline"])
    assert any("def top" in line for line in a["outline"])


def test_helpers() -> None:
    assert live._paths_match("src/a/foo.py", "foo.py")
    assert live._paths_match("foo.py", "x/foo.py")
    assert not live._paths_match("foo.py", "bar.py")
    outline = live._py_outline("class C:\n    def f(self): pass\ndef g(): pass\n")
    assert outline == ["class C  (L1)", "  def f  (L2)", "def g  (L3)"]


# --------------------------------------------------------------------------- #
# Bash output compaction + batching (simulate LemonCrow on RECORDED output)
# --------------------------------------------------------------------------- #

import json as _json  # noqa: E402


def _claude(events: list[dict]) -> str:
    return "\n".join(_json.dumps(e) for e in events)


def test_bash_output_compacted_not_rerun(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine([]))
    big = "\n".join(f"line {i}: verbose build output blah blah" for i in range(400))
    events = [
        {"type": "user", "sessionId": "s", "message": {"content": "run tests"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "uv run pytest -q"}}],
                "usage": {},
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}]}},
    ]
    replay = build_replay(_claude(events), host="claude", session_id="s")
    live.enrich_replay(replay, tmp_path)
    bash_turn = next(t for t in replay.turns if t.get("kind") == "shell_command")
    a = bash_turn["lemoncrow"]
    assert a["mode"] == "simulated"
    assert a["before_chars"] > a["after_chars"]  # real compaction happened
    assert a["chars_omitted"] > 0
    assert "not re-run" in a["note"].lower()


def test_edit_batch_enriched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live, "_build_engine", lambda root: _FakeEngine([]))

    def edit(mid, tid, path):
        return {
            "type": "assistant",
            "message": {
                "id": mid,
                "content": [
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": "Edit",
                        "input": {"file_path": path, "old_string": "a", "new_string": "b"},
                    }
                ],
                "usage": {},
            },
        }

    events = [
        {"type": "user", "sessionId": "s", "message": {"content": "update configs"}},
        edit("m1", "t1", "a.yaml"),
        edit("m2", "t2", "b.yaml"),
        edit("m3", "t3", "c.yaml"),
    ]
    replay = build_replay(_claude(events), host="claude", session_id="s")
    assert replay.summary.batch_count == 1
    live.enrich_replay(replay, tmp_path)
    batch = replay.batches[0]
    assert batch.kind == "edit"
    assert batch.lemon["mode"] == "batch"
    assert batch.lemon["count"] == 3
    assert "a.yaml" in batch.lemon["files"]
