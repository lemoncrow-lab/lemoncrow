"""Regression checks at the MCP boundary, where source is actually delivered."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from lemoncrow_client.dispatcher import Dispatcher, ToolOutcome
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.search_dedup import _MAX_ENTRIES, _MIN_CHARS
from lemoncrow_client.surface import tool_list

_SOURCE = "= exact\n\n## pkg/alpha.py:L1-L100\n" + "source evidence\n" * 400


@pytest.fixture
def search_server(config, monkeypatch):
    current = {
        "outcome": ToolOutcome(
            content=({"type": "text", "text": _SOURCE},),
            view_revision=1,
            structured={"raw": _SOURCE},
        )
    }
    calls = []

    def dispatch(self, tool, arguments):
        calls.append((tool, dict(arguments)))
        return current["outcome"]

    monkeypatch.setattr(McpServer, "_ensure_session", lambda self, retry=False: None)
    monkeypatch.setattr(Dispatcher, "call", dispatch)
    server = McpServer(config)
    yield server, current, calls
    server.close()


def _call(server, arguments=None, *, name="code_search"):
    answer = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"query": "alpha"} if arguments is None else arguments},
        }
    )
    assert answer is not None and "error" not in answer
    return answer["result"]


def _text(payload):
    return payload["content"][0]["text"]


def test_repeated_search_is_short_and_force_recovers_without_changing_cached_data(search_server):
    server, current, calls = search_server
    original = current["outcome"]
    assert _text(_call(server)) == _SOURCE
    repeated = _call(server)
    assert _text(repeated).startswith("[dedup]")
    assert len(_text(repeated)) < 200
    assert "force=true" in _text(repeated)
    assert "structuredContent" not in repeated
    assert _text(_call(server, {"query": "alpha", "force": True})) == _SOURCE
    assert _text(_call(server)).startswith("[dedup]")
    assert len(calls) == 4  # Every request still executes/validates the search.
    assert calls[2][1]["force"] is True
    assert original.content[0]["text"] == _SOURCE
    assert original.structured == {"raw": _SOURCE}


@pytest.mark.parametrize("change", ["text", "revision", "view", "session", "query", "paths", "limit"])
def test_changed_evidence_or_request_is_returned_in_full(search_server, change):
    server, current, _ = search_server
    _call(server)
    assert _text(_call(server)).startswith("[dedup]")
    arguments = {"query": "alpha"}
    expected = _SOURCE
    if change == "text":
        expected = _SOURCE + "new evidence\n"
        current["outcome"] = replace(current["outcome"], content=({"type": "text", "text": expected},))
    elif change == "revision":
        current["outcome"] = replace(current["outcome"], view_revision=2)
    elif change == "view":
        server.dispatcher.session.state.view_id = "new-view"
    elif change == "session":
        server.dispatcher.session.state.session_id = "new-session"
    else:
        arguments[change] = {"query": "beta", "paths": ["pkg"], "limit": 2}[change]
    assert _text(_call(server, arguments)) == expected
    # Fake session ids above must not cause close to contact the test server.
    server.dispatcher.session.state.session_id = ""
    server.dispatcher.session.state.view_id = ""


@pytest.mark.parametrize("size", [_MIN_CHARS - 1, _MIN_CHARS, _MIN_CHARS + 1])
def test_small_results_stay_inline_at_the_threshold(search_server, size):
    server, current, _ = search_server
    text = "x" * size
    current["outcome"] = replace(current["outcome"], content=({"type": "text", "text": text},))
    assert _text(_call(server)) == text
    repeated = _text(_call(server))
    assert repeated == text if size < _MIN_CHARS else repeated.startswith("[dedup]")


@pytest.mark.parametrize("kind", ["error", "degraded", "mixed"])
def test_errors_degradation_and_nontext_evidence_are_never_hidden(search_server, kind):
    server, current, _ = search_server
    outcome = current["outcome"]
    if kind == "error":
        outcome = replace(outcome, is_error=True)
    elif kind == "degraded":
        outcome = replace(outcome, degraded=True, degraded_reason="index unavailable")
    else:
        outcome = replace(outcome, content=(*outcome.content, {"type": "image", "data": "AA=="}))
    current["outcome"] = outcome
    assert _call(server) == _call(server)
    current["outcome"] = replace(outcome, is_error=False, degraded=False, content=outcome.content[:1])
    assert _text(_call(server)) == _SOURCE


@pytest.mark.parametrize("name", ["read", "bash", "web_fetch"])
def test_other_tool_results_are_unchanged(search_server, name):
    server, _, _ = search_server
    assert _text(_call(server, name=name)) == _SOURCE
    assert _text(_call(server, name=name)) == _SOURCE


def test_compaction_and_host_session_changes_reset_seen_results(search_server, config):
    server, _, _ = search_server
    path = config.repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    path.parent.mkdir(parents=True)
    for identity in (
        {"session_id": "host-a", "compaction_epoch": 0},
        {"session_id": "host-a", "compaction_epoch": 1},
        {"session_id": "host-b", "compaction_epoch": 1},
    ):
        path.write_text(json.dumps(identity))
        assert _text(_call(server)) == _SOURCE
        assert _text(_call(server)).startswith("[dedup]")


@pytest.mark.parametrize("raw", ["{", "[]", '{"compaction_epoch": "bad"}', "x" * (256 * 1024 + 1)])
def test_unreadable_context_state_fails_open(search_server, config, raw):
    server, _, _ = search_server
    _call(server)
    path = config.repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    assert _text(_call(server)) == _SOURCE
    assert _text(_call(server)) == _SOURCE


def test_disable_switch_and_reinitialize_clear_history(search_server, monkeypatch):
    server, _, _ = search_server
    _call(server)
    monkeypatch.setenv("LEMONCROW_CONTEXT_DEDUP", "0")
    assert _text(_call(server)) == _SOURCE
    assert _text(_call(server)) == _SOURCE
    monkeypatch.delenv("LEMONCROW_CONTEXT_DEDUP")
    assert _text(_call(server)) == _SOURCE
    assert _text(_call(server)).startswith("[dedup]")
    server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
    assert _text(_call(server)) == _SOURCE


def test_independent_mcp_sessions_never_share_seen_results(search_server, config):
    server, _, _ = search_server
    _call(server)
    assert _text(_call(server)).startswith("[dedup]")
    other = McpServer(config)
    try:
        assert _text(_call(other)) == _SOURCE
    finally:
        other.close()


def test_seen_history_is_bounded_and_evicted_results_are_reemitted(search_server):
    server, _, _ = search_server
    for index in range(_MAX_ENTRIES):
        assert _text(_call(server, {"query": str(index)})) == _SOURCE
    assert _text(_call(server, {"query": "0"})).startswith("[dedup]")  # keep hottest
    assert _text(_call(server, {"query": str(_MAX_ENTRIES)})) == _SOURCE
    assert _text(_call(server, {"query": "0"})).startswith("[dedup]")
    assert _text(_call(server, {"query": "1"})) == _SOURCE


def test_search_force_recovery_is_advertised_in_both_profiles():
    for profile in ("core", "full"):
        spec = next(
            item for item in tool_list(env={"LEMONCROW_MCP_TOOL_PROFILE": profile}) if item["name"] == "code_search"
        )
        assert spec["inputSchema"]["properties"]["force"]["type"] == "boolean"


def test_hydrated_cache_hits_are_deduped_only_at_the_model_boundary(bootstrapped, monkeypatch):
    from lemoncrow_client.manifest import sha256_hex
    from lemoncrow_client.protocol import RESULT_REUSE_CAPABILITY
    from lemoncrow_client.session import RemoteSession

    data = b'def alpha():\n    """' + b"source evidence " * 350 + b'"""\n    return 1\n'
    target = bootstrapped._config.repo_root / "pkg" / "alpha.py"
    target.write_bytes(data)
    structured = {
        "hits": [
            {
                "path": "pkg/alpha.py",
                "language": "python",
                "line_count": 4,
                "detail": {"definitions": [{"name": "alpha", "kind": "function", "line": 1, "start": 4, "end": 9}]},
            }
        ],
        "_hydration": {"pkg/alpha.py": sha256_hex(data)},
    }
    bootstrapped.state.capabilities |= {RESULT_REUSE_CAPABILITY}
    monkeypatch.setattr(RemoteSession, "_refresh_view_if_source_changed", lambda self, fingerprint: False)
    validators = []

    def dispatch(self, tool, arguments, *, reuse_validator=""):
        validators.append(reuse_validator)
        if reuse_validator:
            return {
                "reuse": True,
                "reuse_validator": reuse_validator,
                "view_revision": self.view_revision,
            }
        return {
            "tool": tool,
            "content": [{"type": "text", "text": "server pointer; hydrate locally"}],
            "structured": structured,
            "view_revision": self.view_revision,
            "reuse_validator": "validated-search",
        }

    monkeypatch.setattr(RemoteSession, "_dispatch", dispatch)
    server = McpServer(bootstrapped._config, session=bootstrapped)
    try:
        first = _text(_call(server))
        assert "def alpha():" in first
        assert len(first) >= _MIN_CHARS
        assert _text(_call(server)).startswith("[dedup]")
        assert validators[:2] == ["", "validated-search"]
        assert _text(_call(server, {"query": "alpha", "force": True})) == first
        programmatic = bootstrapped.call_tool("code_search", {"query": "alpha"})
        assert programmatic.content[0]["text"] == first
        assert programmatic.structured == structured
    finally:
        server.close()
