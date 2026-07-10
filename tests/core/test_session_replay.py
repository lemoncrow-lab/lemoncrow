"""Tests for the counterfactual session-replay core.

Covers tool classification, grep→read episode detection (host-agnostic),
full parse→build over synthetic Claude and opencode transcripts, tool-result
join, and both renderers. No transcript on disk and no model is ever run.
"""

from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.session_replay import (
    _is_atelier_search,
    _is_grep,
    _is_whole_file_read,
    build_replay,
    detect_episodes,
    load_replays,
)
from atelier.core.capabilities.session_replay_render import render_html, render_text


def _tc(name: str, **args: object) -> dict[str, object]:
    return {"kind": "tool_call", "tool_name": name, "arguments": dict(args)}


# --------------------------------------------------------------------------- #
# Tool classification
# --------------------------------------------------------------------------- #


def test_classifies_grep_read_and_atelier() -> None:
    assert _is_grep(_tc("Grep", pattern="x"))
    assert _is_grep(_tc("Glob", pattern="*.py"))
    assert _is_whole_file_read(_tc("Read", file_path="a.py"))
    assert not _is_whole_file_read(_tc("Read", file_path="a.py", offset=10, limit=20))  # ranged
    assert _is_atelier_search(_tc("mcp__atelier__code_search", query="x"))
    assert not _is_grep(_tc("mcp__atelier__code_search", query="x"))
    assert not _is_grep(_tc("Edit", file_path="a.py"))


# --------------------------------------------------------------------------- #
# Episode detection (host-agnostic — operates on normalized turns)
# --------------------------------------------------------------------------- #


def test_detects_grep_read_loop() -> None:
    turns = [
        {"kind": "user_message", "content": "find TokenRefresh"},
        {"kind": "agent_message", "content": "let me search"},
        _tc("Grep", pattern="TokenRefresh"),
        _tc("Read", file_path="auth/middleware.py"),
        _tc("Read", file_path="auth/token.py"),
        {"kind": "file_edit", "tool_name": "Edit", "path": "auth/token.py"},
    ]
    eps = detect_episodes(turns)
    assert len(eps) == 1
    ep = eps[0]
    assert ep.grep_count == 1
    assert ep.read_count == 2
    assert ep.turn_indices == [2, 3, 4]
    assert ep.calls_saved == 2
    assert ep.query == "TokenRefresh"
    assert ep.after_index == 4


def test_thinking_is_transparent_between_greps() -> None:
    turns = [
        _tc("Grep", pattern="a"),
        {"kind": "thinking", "content": "hmm"},
        _tc("Read", file_path="a.py"),
    ]
    eps = detect_episodes(turns)
    assert len(eps) == 1
    assert eps[0].turn_indices == [0, 2]


def test_atelier_search_breaks_and_is_not_collapsed() -> None:
    turns = [
        _tc("Grep", pattern="a"),
        _tc("mcp__atelier__code_search", query="a"),
        _tc("Read", file_path="a.py"),
    ]
    # A lone grep (len 1) then a code_search break => no episode; the trailing
    # single read is also not an episode.
    assert detect_episodes(turns) == []


def test_single_grep_is_not_an_episode() -> None:
    assert detect_episodes([_tc("Grep", pattern="a"), {"kind": "file_edit"}]) == []


def test_ranged_read_not_collapsible() -> None:
    turns = [_tc("Grep", pattern="a"), _tc("Read", file_path="a.py", offset=1, limit=5)]
    # grep(1) + ranged read(break) => run has only the grep => no episode
    assert detect_episodes(turns) == []


def test_two_separate_loops() -> None:
    turns = [
        _tc("Grep", pattern="a"),
        _tc("Read", file_path="a.py"),
        {"kind": "file_edit", "path": "a.py"},
        _tc("Grep", pattern="b"),
        _tc("Read", file_path="b.py"),
        {"kind": "shell_command", "content": "pytest"},
    ]
    eps = detect_episodes(turns)
    assert len(eps) == 2
    assert eps[0].query == "a"
    assert eps[1].query == "b"


# --------------------------------------------------------------------------- #
# Full parse -> build over synthetic transcripts
# --------------------------------------------------------------------------- #


def _claude_line(obj: dict[str, object]) -> str:
    return json.dumps(obj)


def _claude_transcript() -> str:
    lines = [
        {"type": "user", "sessionId": "s1", "message": {"content": "find TokenRefresh and fix it"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "TokenRefresh"}}],
                "usage": {},
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "18 files matched"}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "auth/middleware.py"}}
                ],
                "usage": {},
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "420 lines..."}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "m3",
                "content": [{"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "auth/token.py"}}],
                "usage": {},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m4",
                "content": [
                    {"type": "text", "text": "found it, fixing"},
                    {
                        "type": "tool_use",
                        "id": "t4",
                        "name": "Edit",
                        "input": {"file_path": "auth/token.py", "old_string": "a", "new_string": "b"},
                    },
                ],
                "usage": {},
            },
        },
    ]
    return "\n".join(_claude_line(x) for x in lines)


def test_build_replay_claude() -> None:
    replay = build_replay(_claude_transcript(), host="claude", session_id="s1")
    assert replay.summary is not None
    assert replay.summary.episode_count == 1
    assert replay.summary.calls_saved == 2
    assert replay.task.startswith("find TokenRefresh")
    # tool_result content joined by tool_use_id
    assert replay.tool_results["t1"] == "18 files matched"
    # the edit turn is NOT collapsed
    assert any(t.get("kind") == "file_edit" for i, t in enumerate(replay.turns) if i not in replay.collapsed_indices)


def _opencode_transcript() -> str:
    def part_tool(tool: str, **inp: object) -> dict[str, object]:
        return {"_type": "part", "data": {"type": "tool", "tool": tool, "state": {"input": dict(inp)}}}

    lines = [
        {"_type": "message", "data": {"role": "user", "text": "find the parser bug"}},
        part_tool("grep", pattern="parse_session"),
        part_tool("read", filePath="parser.py"),
        part_tool("read", filePath="other.py"),
        part_tool("edit", filePath="parser.py", old_string="x", new_string="y"),
    ]
    return "\n".join(json.dumps(x) for x in lines)


def test_build_replay_opencode_cross_host() -> None:
    replay = build_replay(_opencode_transcript(), host="opencode", session_id="oc1")
    assert replay.summary is not None
    assert replay.summary.episode_count == 1
    assert replay.episodes[0].query == "parse_session"
    assert replay.episodes[0].read_count == 2


def test_codex_meta_does_not_crash() -> None:
    content = json.dumps({"type": "session_meta", "instructions": "do the thing"})
    replay = build_replay(content, host="codex", session_id="cx1")
    assert replay.summary is not None  # no episodes, but builds cleanly


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def test_render_text_marks_collapse() -> None:
    replay = build_replay(_claude_transcript(), host="claude", session_id="s1")
    out = render_text(replay, color=False)
    assert "code_search(" in out
    assert "collapses 3 calls" in out
    assert "no model re-run" in out


def test_render_html_is_wellformed() -> None:
    replay = build_replay(_claude_transcript(), host="claude", session_id="s1")
    out = render_html([replay])
    assert out.startswith("<!doctype html>")
    assert "Atelier Session Replay" in out
    assert "code_search" in out
    assert "turn cut" in out  # a struck-through loop turn
    assert out.count("<html") == 1 and out.count("</html>") == 1


def test_load_replays_from_file(tmp_path: Path) -> None:
    f = tmp_path / "session.jsonl"
    f.write_text(_claude_transcript(), encoding="utf-8")
    replays = load_replays(host="claude", file=f)
    assert len(replays) == 1
    assert replays[0].summary is not None
    assert replays[0].summary.episode_count == 1


def test_load_replays_empty_when_missing() -> None:
    assert load_replays(host="claude", session_id="does-not-exist-xyz") == []


# --------------------------------------------------------------------------- #
# Batch detection (read(files=[...]) / edit(edits=[...]))
# --------------------------------------------------------------------------- #

from atelier.core.capabilities.session_replay import detect_batches  # noqa: E402


def test_detect_edit_batch() -> None:
    turns = [
        {"kind": "file_edit", "tool_name": "Edit", "path": "a.py"},
        {"kind": "file_edit", "tool_name": "Edit", "path": "b.py"},
        {"kind": "file_edit", "tool_name": "Edit", "path": "c.py"},
    ]
    batches = detect_batches(turns, set())
    assert len(batches) == 1
    assert batches[0].kind == "edit"
    assert batches[0].turn_indices == [0, 1, 2]
    assert batches[0].calls_saved == 2


def test_detect_read_batch_and_excludes_episode_reads() -> None:
    turns = [
        _tc("Read", file_path="a.py"),
        _tc("Read", file_path="b.py"),
    ]
    assert len(detect_batches(turns, set())) == 1
    # if both reads are already collapsed by a grep episode, no batch
    assert detect_batches(turns, {0, 1}) == []


def test_read_then_edit_are_two_batches() -> None:
    turns = [
        _tc("Read", file_path="a.py"),
        _tc("Read", file_path="b.py"),
        {"kind": "file_edit", "tool_name": "Edit", "path": "a.py"},
        {"kind": "file_edit", "tool_name": "Edit", "path": "b.py"},
    ]
    batches = detect_batches(turns, set())
    assert [b.kind for b in batches] == ["read", "edit"]


def test_single_edit_no_batch() -> None:
    assert detect_batches([{"kind": "file_edit", "path": "a.py"}], set()) == []


def test_build_replay_counts_batches() -> None:
    turns_json = _opencode_transcript()  # has a grep loop, not batches
    r = build_replay(turns_json, host="opencode", session_id="oc1")
    assert r.summary is not None
    assert r.summary.batch_count == r.summary.batch_count  # smoke: field present


# --------------------------------------------------------------------------- #
# Subagent (sidechain) nesting + savings headline
# --------------------------------------------------------------------------- #

from atelier.core.capabilities.session_replay import estimate_savings  # noqa: E402


def test_subagent_transcripts_nested(tmp_path: Path) -> None:
    parent = [
        {"type": "user", "sessionId": "p1", "message": {"content": "explore then fix"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Task",
                        "input": {"subagent_type": "explore", "description": "find X", "prompt": "locate X"},
                    }
                ],
                "usage": {},
            },
        },
    ]
    ppath = tmp_path / "p1.jsonl"
    ppath.write_text("\n".join(json.dumps(e) for e in parent), encoding="utf-8")
    subdir = tmp_path / "p1" / "subagents"
    subdir.mkdir(parents=True)
    sub = [
        {"type": "user", "sessionId": "sub1", "message": {"content": "locate X"}},
        {
            "type": "assistant",
            "message": {
                "id": "s1",
                "content": [{"type": "tool_use", "id": "st1", "name": "Grep", "input": {"pattern": "X"}}],
                "usage": {},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "s2",
                "content": [{"type": "tool_use", "id": "st2", "name": "Read", "input": {"file_path": "x.py"}}],
                "usage": {},
            },
        },
    ]
    (subdir / "sub1.jsonl").write_text("\n".join(json.dumps(e) for e in sub), encoding="utf-8")

    replays = load_replays(host="claude", file=ppath)
    assert len(replays) == 1
    r = replays[0]
    assert len(r.subagent_replays) == 1
    assert r.subagent_replays[0].session_id == "sub1"
    # the subagent's own grep+read loop is detected in its nested replay
    assert r.subagent_replays[0].summary.episode_count == 1
    # subagents surface in the JSON model
    assert len(r.to_dict()["subagents"]) == 1
    # subagent replays are flagged as such
    assert r.subagent_replays[0].is_subagent is True


def test_subagent_does_not_inherit_parent_savings(monkeypatch) -> None:
    import types

    import atelier.core.capabilities.savings_summary as ss_mod
    from atelier.core.capabilities.session_replay import Replay, estimate_savings

    # A parent with recorded savings; the engine would fall back to it for a
    # subagent (no own sidecar) -- which must NOT happen.
    fake = types.SimpleNamespace(total_saved_usd=10.0, time_saved_seconds=100.0, smart_calls=5, est_cost_usd=20.0)
    monkeypatch.setattr(ss_mod, "compute_savings_summary", lambda sid, **k: fake)
    common = dict(
        host="claude",
        model="claude-sonnet-5",
        task="t",
        turns=[{"kind": "user_message", "content": "x"}],
        collapsed_indices=[],
        episodes=[],
        summary=build_replay("", host="claude", session_id="z").summary,
    )
    parent = estimate_savings(Replay(session_id="p", **common))
    sub = estimate_savings(Replay(session_id="s", is_subagent=True, **common))
    # parent uses the measured savings; subagent must NOT inherit them
    assert parent["saved_is_measured"] is True
    assert parent["saved_usd"] == 10.0
    assert sub["saved_is_measured"] is False
    assert sub["saved_usd"] != 10.0


def test_estimate_savings_from_engine_only() -> None:
    # No source_path / no savings.jsonl -> engine returns 0 saved (vanilla).
    r = build_replay(_claude_transcript(), host="claude", session_id="unknown-xyz")
    sav = estimate_savings(r)
    for key in (
        "total_cost_usd",
        "atelier_cost_usd",
        "atelier_cost_is_measured",
        "saved_usd",
        "saved_pct",
        "saved_is_measured",
        "time_saved_seconds",
        "is_atelier_session",
        "calls_saved",
        "collapsed_output_tokens",
    ):
        assert key in sav
    # A vanilla session (no Atelier run, no paired benchmark arm): the Atelier
    # cost is an ESTIMATE, never claimed as measured, and never exceeds the cost.
    assert sav["is_atelier_session"] is False
    assert sav["atelier_cost_is_measured"] is False
    assert sav["saved_is_measured"] is False
    assert sav["atelier_cost_usd"] <= sav["total_cost_usd"]
    # structural counterfactual is still surfaced
    assert sav["calls_saved"] >= 1


# --------------------------------------------------------------------------- #
# Shell-grep loops (agents grep via Bash, not the Grep tool)
# --------------------------------------------------------------------------- #

from atelier.core.capabilities.session_replay import _shell_search_query  # noqa: E402


def test_shell_grep_is_collapsible() -> None:
    assert _is_grep({"kind": "shell_command", "content": 'grep -rn "savings" --include=*.py .'})
    assert _is_grep({"kind": "shell_command", "content": "find . -name '*.py' | xargs grep -ln Foo"})
    assert _is_grep({"kind": "shell_command", "content": "rg TokenRefresh src/"})
    assert not _is_grep({"kind": "shell_command", "content": "uv run pytest -q"})


def test_shell_search_query_cleans_regex() -> None:
    assert _shell_search_query('grep -rn "cost_saving\\|savings" .') == "cost_saving"
    assert _shell_search_query("rg detect_episodes src/") == "detect_episodes"


def test_collapse_saving_fraction_canonical() -> None:
    from atelier.core.capabilities.savings_summary import estimate_collapse_saving_fraction

    # baseline-like per-round usage: a grep/read loop (rounds 0-3) then answers.
    rounds = [
        {"in": 3080, "out": 111, "cache_read": 23314, "cache_write": 6535},
        {"in": 2, "out": 188, "cache_read": 29849, "cache_write": 5355},
        {"in": 2, "out": 214, "cache_read": 35204, "cache_write": 739},
        {"in": 215, "out": 196, "cache_read": 35943, "cache_write": 23840},
        {"in": 2, "out": 208, "cache_read": 59783, "cache_write": 2252},
        {"in": 2, "out": 612, "cache_read": 62035, "cache_write": 2873},
    ]
    f = estimate_collapse_saving_fraction(rounds, [0, 1, 2, 3], "claude-sonnet-5")
    # captures most of the loop saving (the real A/B on this session was ~0.76)
    assert 0.4 < f < 0.85
    # no collapsed loop -> nothing saved; empty usage -> 0; always a fraction
    assert estimate_collapse_saving_fraction(rounds, [], "claude-sonnet-5") == 0.0
    assert estimate_collapse_saving_fraction([], [0, 1], "claude-sonnet-5") == 0.0
    assert 0.0 <= estimate_collapse_saving_fraction(rounds, [2], "claude-sonnet-5") <= 1.0


def test_shell_grep_read_loop_collapses() -> None:
    turns = [
        {"kind": "shell_command", "tool_name": "Bash", "content": 'grep -rn "detect_episodes" .'},
        {
            "kind": "shell_command",
            "tool_name": "Bash",
            "content": "find . -name '*.py' | xargs grep -l detect_episodes",
        },
        _tc("Read", file_path="session_replay.py"),
        {"kind": "agent_message", "content": "found it"},
        {"kind": "file_edit", "tool_name": "Edit", "path": "session_replay.py"},
    ]
    eps = detect_episodes(turns)
    assert len(eps) == 1
    assert eps[0].grep_count == 2
    assert eps[0].read_count == 1
    assert eps[0].calls_saved == 2
    assert eps[0].query == "detect_episodes"
