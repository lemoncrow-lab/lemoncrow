"""Tests for the counterfactual session-replay core.

Covers tool classification, grep→read episode detection (host-agnostic),
full parse→build over synthetic Claude and opencode transcripts, tool-result
join, and both renderers. No transcript on disk and no model is ever run.
"""

from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.session_replay import (
    _is_lemoncrow_search,
    _is_grep,
    _is_whole_file_read,
    build_replay,
    detect_episodes,
    load_replays,
)
from lemoncrow.core.capabilities.session_replay_render import render_html, render_text


def _tc(name: str, **args: object) -> dict[str, object]:
    return {"kind": "tool_call", "tool_name": name, "arguments": dict(args)}


# --------------------------------------------------------------------------- #
# Tool classification
# --------------------------------------------------------------------------- #


def test_classifies_grep_read_and_lemoncrow() -> None:
    assert _is_grep(_tc("Grep", pattern="x"))
    assert _is_grep(_tc("Glob", pattern="*.py"))
    assert _is_whole_file_read(_tc("Read", file_path="a.py"))
    assert not _is_whole_file_read(_tc("Read", file_path="a.py", offset=10, limit=20))  # ranged
    assert _is_lemoncrow_search(_tc("mcp__lemon__code_search", query="x"))
    assert not _is_grep(_tc("mcp__lemon__code_search", query="x"))
    assert not _is_grep(_tc("Edit", file_path="a.py"))


def test_lemon_read_is_not_a_wasteful_whole_file_read() -> None:
    # LemonCrow's read is batched/ranged by design -- classifying it as a wasteful
    # whole-file read inflated collapse/batch stats on ran-with-LemonCrow sessions.
    assert not _is_whole_file_read(_tc("mcp__lemon__read", files=["a.py", "b.py:L1-L20"]))
    assert not _is_whole_file_read(_tc("mcp__lemon__read", symbol="fold_line"))
    # files/symbol args are targeted on ANY read tool
    assert not _is_whole_file_read(_tc("read", files=["a.py"]))
    assert not _is_whole_file_read(_tc("read", symbol="foo"))
    # a plain whole-file Read still counts
    assert _is_whole_file_read(_tc("Read", file_path="a.py"))


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


def test_lemoncrow_search_breaks_and_is_not_collapsed() -> None:
    turns = [
        _tc("Grep", pattern="a"),
        _tc("mcp__lemon__code_search", query="a"),
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


def test_codex_replay_parses_current_custom_and_mcp_calls_with_model() -> None:
    lines = [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "inspect the parser"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "await tools.mcp__lemon__read()"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "invocation": {"server": "lemon", "tool": "read", "arguments": {"files": ["a.py"]}},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "await tools.exec_command()"},
        },
    ]

    replay = build_replay("\n".join(json.dumps(line) for line in lines), host="codex", session_id="cx-current")

    assert replay.model == "gpt-5.6-terra"
    assert replay.summary is not None and replay.summary.total_tool_calls == 2
    assert [turn.get("tool_name") for turn in replay.turns if turn.get("kind") == "tool_call"] == ["lemon.read"]
    assert any(
        turn.get("kind") == "shell_command" and turn.get("content") == "await tools.exec_command()"
        for turn in replay.turns
    )


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
    assert "LemonCrow Session Replay" in out
    assert "code_search" in out
    assert "turn cut" in out  # a struck-through loop turn
    assert out.count("<html") == 1 and out.count("</html>") == 1
    assert 'class="tabs"' not in out  # single session -> no tab bar


def test_render_html_tabs_for_multiple_sessions() -> None:
    a = build_replay(_claude_transcript(), host="claude", session_id="s1")
    b = build_replay(_claude_transcript(), host="claude", session_id="s2")
    out = render_html([a, b])
    assert out.count('class="tab-panel') == 2  # one panel per session
    assert out.count('class="tab-btn') == 2  # one button per session
    assert "function selTab" in out


def test_arg_summary_covers_list_and_scalar_args() -> None:
    from lemoncrow.core.capabilities.session_replay_render import _arg_summary

    read = {
        "kind": "tool_call",
        "tool_name": "mcp__lemon__read",
        "arguments": {"files": ["a.py", "b.py:L1-L9", "c.py", "d.py"]},
    }
    edit = {
        "kind": "tool_call",
        "tool_name": "mcp__lemon__edit",
        "arguments": {"edits": [{"path": "x.py:L1-L4", "new": "..."}]},
    }
    assert _arg_summary(read) == "mcp__lemon__read(a.py, b.py:L1-L9, c.py, +1 more)"
    assert _arg_summary(edit) == "mcp__lemon__edit(x.py:L1-L4)"
    # unknown scalar-only tool still surfaces its first value, never a bare ellipsis
    assert _arg_summary({"kind": "tool_call", "tool_name": "X", "arguments": {"n": 42}}) == "X(42)"


def test_load_replays_from_file(tmp_path: Path) -> None:
    f = tmp_path / "session.jsonl"
    f.write_text(_claude_transcript(), encoding="utf-8")
    replays = load_replays(host="claude", file=f)
    assert len(replays) == 1
    assert replays[0].summary is not None
    assert replays[0].summary.episode_count == 1


def test_load_codex_replay_uses_transcript_session_id(tmp_path: Path) -> None:
    f = tmp_path / "rollout-2026-07-11T07-41-57-random.jsonl"
    f.write_text(
        "\n".join(
            json.dumps(line)
            for line in (
                {"type": "session_meta", "payload": {"session_id": "codex-session-id"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "inspect the parser"}},
            )
        ),
        encoding="utf-8",
    )

    replays = load_replays(host="codex", file=f)

    assert len(replays) == 1
    assert replays[0].session_id == "codex-session-id"


def test_load_replays_empty_when_missing() -> None:
    assert load_replays(host="claude", session_id="does-not-exist-xyz") == []


# --------------------------------------------------------------------------- #
# Batch detection (read(files=[...]) / edit(edits=[...]))
# --------------------------------------------------------------------------- #

from lemoncrow.core.capabilities.session_replay import detect_batches  # noqa: E402


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
    # The base opencode transcript is one grep loop: its reads are episode-
    # collapsed (never double-counted as a batch) and the lone edit is no batch.
    r = build_replay(_opencode_transcript(), host="opencode", session_id="oc1")
    assert r.summary is not None
    assert r.summary.batch_count == 0
    assert r.summary.batch_calls_saved == 0

    # A second adjacent edit forms one edit batch (-1 call).
    extra = json.dumps(
        {
            "_type": "part",
            "data": {
                "type": "tool",
                "tool": "edit",
                "state": {"input": {"filePath": "other.py", "old_string": "a", "new_string": "b"}},
            },
        }
    )
    r2 = build_replay(_opencode_transcript() + "\n" + extra, host="opencode", session_id="oc2")
    assert r2.summary is not None
    assert r2.summary.batch_count == 1
    assert r2.summary.batch_calls_saved == 1


# --------------------------------------------------------------------------- #
# Subagent (sidechain) nesting + savings headline
# --------------------------------------------------------------------------- #

from lemoncrow.core.capabilities.session_replay import estimate_savings  # noqa: E402


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

    import lemoncrow.core.capabilities.savings_summary as ss_mod
    from lemoncrow.core.capabilities.session_replay import Replay, estimate_savings

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
        "lemoncrow_cost_usd",
        "lemoncrow_cost_is_measured",
        "saved_usd",
        "saved_pct",
        "saved_is_measured",
        "time_saved_seconds",
        "is_lemoncrow_session",
        "calls_saved",
        "collapsed_output_tokens",
    ):
        assert key in sav
    # A vanilla session (no LemonCrow run, no paired benchmark arm): the LemonCrow
    # cost is an ESTIMATE, never claimed as measured, and never exceeds the cost.
    assert sav["is_lemoncrow_session"] is False
    assert sav["lemoncrow_cost_is_measured"] is False
    assert sav["saved_is_measured"] is False
    assert sav["lemoncrow_cost_usd"] <= sav["total_cost_usd"]
    # structural counterfactual is still surfaced
    assert sav["calls_saved"] >= 1


# --------------------------------------------------------------------------- #
# Shell-grep loops (agents grep via Bash, not the Grep tool)
# --------------------------------------------------------------------------- #

from lemoncrow.core.capabilities.session_replay import _shell_search_query  # noqa: E402


def test_shell_grep_is_collapsible() -> None:
    assert _is_grep({"kind": "shell_command", "content": 'grep -rn "savings" --include=*.py .'})
    assert _is_grep({"kind": "shell_command", "content": "find . -name '*.py' | xargs grep -ln Foo"})
    assert _is_grep({"kind": "shell_command", "content": "rg TokenRefresh src/"})
    assert not _is_grep({"kind": "shell_command", "content": "uv run pytest -q"})


def test_shell_search_query_cleans_regex() -> None:
    assert _shell_search_query('grep -rn "cost_saving\\|savings" .') == "cost_saving"
    assert _shell_search_query("rg detect_episodes src/") == "detect_episodes"


def test_bash_with_description_not_misclassified_as_subagent() -> None:
    # Regression: Bash carries a `description`, which used to trip the subagent
    # heuristic -> shell greps vanished from the timeline -> 0 savings detected.
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns

    bash = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "grep -rn Foo .", "description": "search for Foo"},
                }
            ],
            "usage": {},
        },
    }
    kinds = [t["kind"] for t in parse_session_turns(json.dumps(bash), "claude")]
    assert "shell_command" in kinds
    assert "subagent_event" not in kinds
    # a real Task (subagent_type present) is still detected as a subagent event
    task = {
        "type": "assistant",
        "message": {
            "id": "m2",
            "content": [
                {"type": "tool_use", "id": "t2", "name": "Task", "input": {"subagent_type": "explore", "prompt": "go"}}
            ],
            "usage": {},
        },
    }
    assert any(t["kind"] == "subagent_event" for t in parse_session_turns(json.dumps(task), "claude"))


def test_collapse_saving_fraction_canonical() -> None:
    from lemoncrow.core.capabilities.savings_summary import estimate_collapse_saving_fraction

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
    # Bounded: even an all-loop session never estimates a 100% saving (LemonCrow
    # still costs the code_search rounds it keeps).
    all_loop = estimate_collapse_saving_fraction(rounds, list(range(len(rounds))), "claude-sonnet-5")
    assert all_loop < 1.0


def test_collapse_saving_keeps_one_standin_per_episode() -> None:
    from lemoncrow.core.capabilities.savings_summary import estimate_collapse_saving_fraction

    rounds = [
        {"in": 100, "out": 50, "cache_read": 1000, "cache_write": 200},
        {"in": 2, "out": 60, "cache_read": 1200, "cache_write": 100},
        {"in": 2, "out": 70, "cache_read": 1400, "cache_write": 100},
        {"in": 2, "out": 80, "cache_read": 1600, "cache_write": 100},
    ]
    # Two disjoint single-round loops: each round IS its own code_search
    # stand-in, so nothing is eliminated and nothing is saved.
    assert estimate_collapse_saving_fraction(rounds, [[0], [2]], "claude-sonnet-5") == 0.0
    # A two-round loop plus a single-round loop eliminates exactly one round;
    # the flat form treats all three as ONE loop and (wrongly, for two
    # episodes) eliminates two -- grouped must save strictly less.
    grouped = estimate_collapse_saving_fraction(rounds, [[0, 1], [3]], "claude-sonnet-5")
    flat = estimate_collapse_saving_fraction(rounds, [0, 1, 3], "claude-sonnet-5")
    assert 0.0 < grouped < flat


def test_codex_replay_prices_tokens_for_savings() -> None:
    # codex/opencode transcripts cannot be folded by the Claude-only stats
    # reader -- the replay must price the parsed per-turn usage instead of
    # showing Cost $0.0000.
    lines = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "find the bug"}]},
        {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "grep -rn foo ."})},
        {"type": "function_call", "name": "read_file", "arguments": json.dumps({"path": "a.py"})},
        {
            "type": "message",
            "role": "assistant",
            "model": "gpt-5",
            "usage": {"input_tokens": 9000, "output_tokens": 400, "cached_input_tokens": 2000},
            "content": [{"type": "text", "text": "found it"}],
        },
    ]
    r = build_replay("\n".join(json.dumps(x) for x in lines), host="codex", session_id="cx-usage-zzz")
    sav = estimate_savings(r)
    assert sav["total_cost_usd"] > 0.0  # priced from parsed per-turn tokens
    assert sav["lemoncrow_cost_usd"] <= sav["total_cost_usd"]


def _claude_usage_transcript() -> str:
    def asst(mid: str, tool: str, args: dict[str, object], usage: dict[str, int]) -> dict[str, object]:
        return {
            "type": "assistant",
            "timestamp": f"2026-07-02T00:00:0{mid[-1]}Z",
            "message": {
                "id": mid,
                "model": "claude-sonnet-4-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": f"t-{mid}", "name": tool, "input": args}],
            },
        }

    lines = [
        {"type": "user", "sessionId": "vanilla-frac-x1", "message": {"content": "find TokenRefresh and fix it"}},
        asst("m1", "Grep", {"pattern": "TokenRefresh"}, {"input_tokens": 3000, "output_tokens": 100}),
        asst(
            "m2",
            "Read",
            {"file_path": "auth/middleware.py"},
            {
                "input_tokens": 5,
                "output_tokens": 120,
                "cache_read_input_tokens": 20000,
                "cache_creation_input_tokens": 6000,
            },
        ),
        asst(
            "m3",
            "Read",
            {"file_path": "auth/token.py"},
            {
                "input_tokens": 5,
                "output_tokens": 130,
                "cache_read_input_tokens": 26000,
                "cache_creation_input_tokens": 5000,
            },
        ),
        asst(
            "m4",
            "Edit",
            {"file_path": "auth/token.py", "old_string": "a", "new_string": "b"},
            {
                "input_tokens": 5,
                "output_tokens": 300,
                "cache_read_input_tokens": 31000,
                "cache_creation_input_tokens": 2000,
            },
        ),
    ]
    return "\n".join(json.dumps(x) for x in lines)


def test_vanilla_savings_fraction_applies_to_main_cost_only(tmp_path: Path) -> None:
    # Same MAIN transcript twice; one copy also has a huge-usage subagent
    # transcript. The vanilla fraction must be applied to the main-transcript
    # cost only, so the estimated saving must not grow with the subagent bill.
    from lemoncrow.core.capabilities import savings_summary as ss

    ss._transcript_stats_cache.clear()
    a = tmp_path / "a" / "vanilla-frac-x1.jsonl"
    a.parent.mkdir(parents=True)
    a.write_text(_claude_usage_transcript() + "\n", encoding="utf-8")

    b = tmp_path / "b" / "vanilla-frac-x1.jsonl"
    subdir = tmp_path / "b" / "vanilla-frac-x1" / "subagents"
    subdir.mkdir(parents=True)
    b.write_text(_claude_usage_transcript() + "\n", encoding="utf-8")
    sub = {
        "type": "assistant",
        "timestamp": "2026-07-02T00:01:00Z",
        "message": {
            "id": "sub-m1",
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 500000, "output_tokens": 80000},
            "content": [{"type": "text", "text": "subagent output"}],
        },
    }
    (subdir / "sub1.jsonl").write_text(json.dumps(sub) + "\n", encoding="utf-8")

    sav_main = estimate_savings(load_replays(host="claude", file=a)[0])
    sav_with_sub = estimate_savings(load_replays(host="claude", file=b)[0])

    assert sav_main["saved_usd"] > 0.0  # non-zero usage exercises fraction x cost
    assert sav_main["lemoncrow_cost_usd"] < sav_main["total_cost_usd"]
    # subagent usage is billed to the session...
    assert sav_with_sub["total_cost_usd"] > sav_main["total_cost_usd"]
    # ...but the collapse fraction never multiplies the subagent bill
    assert abs(sav_with_sub["saved_usd"] - sav_main["saved_usd"]) < 1e-6


# --------------------------------------------------------------------------- #
# opencode discovery from opencode.db (current layout)
# --------------------------------------------------------------------------- #

import sqlite3  # noqa: E402


def _make_opencode_db(db: Path) -> None:
    """Tiny opencode.db with the session/message/part schema serialize reads."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, time_created INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
        """)
    conn.execute("INSERT INTO session VALUES ('ses_old', 100)")
    conn.execute("INSERT INTO session VALUES ('ses_abc123', 200)")
    conn.execute(
        "INSERT INTO message VALUES ('m0', 'ses_old', ?, 101)",
        (json.dumps({"role": "user", "text": "older session"}),),
    )
    conn.execute(
        "INSERT INTO message VALUES ('m1', 'ses_abc123', ?, 201)",
        (json.dumps({"role": "user", "text": "find the parser bug"}),),
    )
    conn.execute("INSERT INTO message VALUES ('m2', 'ses_abc123', ?, 202)", (json.dumps({"role": "assistant"}),))
    parts = [
        ("p1", "grep", {"pattern": "parse_session"}, 203),
        ("p2", "read", {"filePath": "parser.py"}, 204),
        ("p3", "read", {"filePath": "other.py"}, 205),
        ("p4", "edit", {"filePath": "parser.py", "old_string": "x", "new_string": "y"}, 206),
    ]
    for pid, tool, inp, ts in parts:
        conn.execute(
            "INSERT INTO part VALUES (?, 'ses_abc123', 'm2', ?, ?)",
            (pid, json.dumps({"type": "tool", "tool": tool, "state": {"input": inp}}), ts),
        )
    conn.commit()
    conn.close()


def test_load_replays_opencode_from_db(tmp_path: Path, monkeypatch) -> None:
    import lemoncrow.core.capabilities.session_replay as sr

    db = tmp_path / "opencode.db"
    _make_opencode_db(db)
    monkeypatch.setattr(sr, "_opencode_db_path", lambda: db)

    replays = load_replays(host="opencode", last=1)
    assert len(replays) == 1
    r = replays[0]
    assert r.session_id == "ses_abc123"  # newest first
    assert r.source_path == str(db)
    assert r.summary is not None and r.summary.episode_count == 1
    assert r.episodes[0].query == "parse_session"

    # --session-id matches DB session ids (substring ok)
    by_id = load_replays(host="opencode", session_id="abc123")
    assert len(by_id) == 1 and by_id[0].session_id == "ses_abc123"

    # --last respects newest-first ordering
    both = load_replays(host="opencode", last=2)
    assert [x.session_id for x in both] == ["ses_abc123", "ses_old"]


def test_load_replays_opencode_no_db_no_crash(monkeypatch) -> None:
    import lemoncrow.core.capabilities.session_replay as sr

    monkeypatch.setattr(sr, "_opencode_db_path", lambda: None)
    monkeypatch.setattr(sr, "_opencode_roots", lambda: [])  # no legacy *.jsonl either
    assert load_replays(host="opencode", last=1) == []


# --------------------------------------------------------------------------- #
# --file format autodetect
# --------------------------------------------------------------------------- #


def _codex_transcript() -> str:
    lines = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "find the bug"}]},
        {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "grep -rn foo ."})},
        {"type": "function_call", "name": "read_file", "arguments": json.dumps({"path": "a.py"})},
        {
            "type": "message",
            "role": "assistant",
            "model": "gpt-5",
            "usage": {"input_tokens": 900, "output_tokens": 40},
            "content": [{"type": "text", "text": "found it"}],
        },
    ]
    return "\n".join(json.dumps(x) for x in lines)


def test_detect_transcript_host() -> None:
    from lemoncrow.core.capabilities.session_replay import detect_transcript_host

    host, count = detect_transcript_host(_codex_transcript())
    assert host == "codex" and count >= 3
    host, count = detect_transcript_host(_claude_transcript())
    assert host == "claude" and count > 0
    assert detect_transcript_host("this is not a transcript\nnope\n") == (None, 0)


# --------------------------------------------------------------------------- #
# Task header skips host-command noise
# --------------------------------------------------------------------------- #


def test_first_task_text_skips_host_command_noise() -> None:
    from lemoncrow.core.capabilities.session_replay import _first_task_text

    turns = [
        {"kind": "user_message", "content": "User ran command: /model"},
        {"kind": "user_message", "content": "<command-name>/clear</command-name>"},
        {"kind": "user_message", "content": "Caveat: the messages below were generated locally"},
        {"kind": "user_message", "content": "fix the flaky auth test"},
    ]
    assert _first_task_text(turns) == "fix the flaky auth test"
    assert _first_task_text(turns[:3]) == ""  # all noise -> no task, never noise


def test_build_replay_task_skips_command_noise() -> None:
    lines = [
        {"type": "user", "sessionId": "s1", "message": {"content": "User ran command: /model"}},
        {"type": "user", "message": {"content": "find TokenRefresh and fix it"}},
    ]
    replay = build_replay("\n".join(json.dumps(x) for x in lines), host="claude", session_id="s1")
    assert replay.task.startswith("find TokenRefresh")


# --------------------------------------------------------------------------- #
# Terminal polish: single summary line, money formatting, 0-turn warning
# --------------------------------------------------------------------------- #


def test_render_text_single_summary_line() -> None:
    replay = build_replay(_claude_transcript(), host="claude", session_id="s1")
    out = render_text(replay, color=False)
    assert "tool calls 4 → 2 · 2 collapsed · 1 search loops · 0 batches" in out
    assert "tool calls: " not in out  # the old duplicated pair is gone


def test_money_formatting_rule() -> None:
    from lemoncrow.core.capabilities.session_replay_render import _money

    assert _money(3.3034) == "$3.30"
    assert _money(1.0) == "$1.00"
    assert _money(0.0421) == "$0.0421"
    assert _money(0.0) == "$0.0000"


def test_render_text_zero_turns_warns() -> None:
    replay = build_replay("", host="claude", session_id="empty")
    out = render_text(replay, color=False)
    assert "no turns parsed" in out


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
