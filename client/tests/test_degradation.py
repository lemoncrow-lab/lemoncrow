"""The degradation table, row by row.

The distinction the design draws, and the one this module exists to hold, is
between a server that was **never there** and one that **went away**:

* never there -> client-side tools work, ``read`` falls back to a bounded file
  read flagged degraded, and one visible line says why;
* went away -> server-side tools return the typed error the agent can act on,
  and client-side tools are unaffected.

And in neither case is a local index built. There is no code path in the
package that could build one, which is asserted here as well as claimed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _audit_source import code_only, iter_modules
from _stub import StubServer
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ErrorCode
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession


@pytest.fixture
def unreachable_config(worktree: Path, state_dir: Path) -> ClientConfig:
    """A configuration pointing at a port nothing is listening on."""
    # Bind and immediately release, so the port is real and certainly closed.
    import socket

    from conftest import make_config

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return make_config(
        url=f"http://127.0.0.1:{port}",
        worktree=worktree,
        state_dir=state_dir,
        LEMONCROW_REQUEST_TIMEOUT_S="1",
    )


# --------------------------------------------------------------------------- #
# Server unreachable at MCP initialize                                       #
# --------------------------------------------------------------------------- #


def test_an_unreachable_server_leaves_client_tools_working(unreachable_config: ClientConfig) -> None:
    session = RemoteSession(unreachable_config)
    report = session.bootstrap(deadline_s=2.0)
    assert not report.ok
    assert not session.bootstrapped

    dispatcher = Dispatcher(unreachable_config, session)
    outcome = dispatcher.call("bash", {"command": "printf offline-ok"})
    assert not outcome.is_error
    assert "offline-ok" in outcome.content[0]["text"]
    assert dispatcher.call("grep", {"regex": "def alpha"}).content


def test_an_unreachable_server_produces_exactly_one_visible_line(
    unreachable_config: ClientConfig,
) -> None:
    report = RemoteSession(unreachable_config).bootstrap(deadline_s=2.0)
    line = report.one_line(unreachable_config.url)
    assert line.count("\n") == 0
    assert "client-side tools still work" in line
    assert "server_unreachable" in report.reason


def test_read_falls_back_to_disk_and_is_flagged_degraded(unreachable_config: ClientConfig) -> None:
    """The one row of the table with a local answer, and it says so."""
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("read", {"files": ["pkg/alpha.py"]})
    assert not outcome.is_error
    assert outcome.degraded
    assert outcome.site == "client_offline_fallback"
    assert "def alpha" in outcome.content[0]["text"]
    assert outcome.to_mcp()["_meta"]["lemoncrow/degraded"] is True


def test_online_absolute_read_stays_local(
    bootstrapped: RemoteSession,
    config: ClientConfig,
    worktree: Path,
) -> None:
    target = worktree.parent / "outside-skill.md"
    target.write_text("absolute local read\n", encoding="utf-8")
    outcome = Dispatcher(config, bootstrapped).call("read", {"files": [f"{target}:full"]})
    assert not outcome.is_error
    assert not outcome.degraded
    assert outcome.site == "client"
    assert "absolute local read" in outcome.content[0]["text"]


def test_online_absolute_read_entries_use_the_read_grammar(
    bootstrapped: RemoteSession,
    config: ClientConfig,
    worktree: Path,
) -> None:
    target = worktree.parent / "outside-notes.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    dispatcher = Dispatcher(config, bootstrapped)
    ranged = dispatcher.call("read", {"files": [f"{target}:2-3"]})
    assert ranged.site == "client"
    assert "2\ttwo\n3\tthree" in ranged.content[0]["text"]
    keyed = dispatcher.call("read", {"files": [{"file_path": str(target), "range": "L1"}]})
    assert keyed.site == "client"
    assert "1\tone" in keyed.content[0]["text"]


def test_every_other_server_tool_refuses_with_a_typed_error(
    unreachable_config: ClientConfig,
) -> None:
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("code_search", {"query": "alpha"})
    assert outcome.is_error
    body = outcome.content[0]["text"]
    assert ErrorCode.SERVER_SESSION_UNAVAILABLE.value in body
    assert "action=retry_later" in body
    payload = json.loads(body.splitlines()[-1])
    assert payload["retryable"] is True


def test_index_refuses_rather_than_building_one_locally(unreachable_config: ClientConfig) -> None:
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("index", {})
    assert outcome.is_error
    assert "never builds a local index" in outcome.content[0]["text"]


def test_nothing_in_the_package_can_build_a_local_index() -> None:
    """A structural check behind the behavioural one.

    An index needs somewhere to put itself. The package imports no persistence
    module and no numeric or model runtime anywhere except the ``sql`` tool's
    engine (``kit/sql.py``), which connects to the *developer's own* database
    and, on a read, creates nothing; and it issues no DDL at all.
    """
    import ast

    persistence = {"sqlite3", "dbm", "shelve", "pickle", "marshal", "numpy", "torch"}
    for path in iter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        offending = imported & persistence
        if path.name == "sql.py" and path.parent.name == "kit":
            # The one exception, and it is narrow: the developer's database.
            assert offending == {"sqlite3"}, f"kit/sql.py imports {offending}"
            continue
        assert not offending, f"{path.name} imports {sorted(offending)}"

    for path in iter_modules():
        body = code_only(path).upper()
        for statement in ("CREATE TABLE", "CREATE INDEX", "CREATE VIRTUAL TABLE"):
            assert statement not in body, f"{path.name} issues {statement!r}"


def test_an_unreachable_server_writes_nothing_into_the_worktree(
    unreachable_config: ClientConfig, worktree: Path
) -> None:
    before = {path for path in worktree.rglob("*")}
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    Dispatcher(unreachable_config, session).call("read", {"files": ["pkg/alpha.py"]})
    assert {path for path in worktree.rglob("*")} == before


def test_mcp_initialize_stays_alive_and_reports_one_process_degradation(unreachable_config: ClientConfig) -> None:
    server = McpServer(unreachable_config)
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert answer is not None
    instructions = str(answer["result"]["instructions"])
    assert "server-side tools unavailable" in instructions
    assert "client-side tools still work" in instructions
    assert server.dispatcher.session.bootstrapped is False
    assert server.dispatcher.session.state.reason
    server.close()


# --------------------------------------------------------------------------- #
# Server lost mid-session                                                     #
# --------------------------------------------------------------------------- #


def test_a_server_lost_mid_session_returns_typed_errors_not_a_local_answer(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    """``read`` must **not** quietly switch to reading from disk here.

    The session came up, so the agent has been told the index is available; a
    silent change of answer source is exactly the kind of drift the typed error
    exists to prevent.
    """
    dispatcher = Dispatcher(config, bootstrapped)
    assert not dispatcher.call("read", {"files": ["pkg/alpha.py"]}).is_error

    stub.close()
    outcome = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert outcome.is_error
    assert outcome.site == "refused"
    assert ErrorCode.SERVER_UNREACHABLE.value in outcome.content[0]["text"]
    assert not outcome.degraded


def test_client_tools_are_unaffected_by_a_mid_session_loss(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    stub.close()
    dispatcher = Dispatcher(config, bootstrapped)
    assert "still-here" in dispatcher.call("bash", {"command": "printf still-here"}).content[0]["text"]
    assert dispatcher.call("grep", {"regex": "def"}).content


def test_an_edit_that_cannot_be_pushed_keeps_the_write_and_says_the_view_is_behind(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, worktree: Path
) -> None:
    """The file is already written; losing that would be worse than a warning."""
    stub.close()
    outcome = Dispatcher(config, bootstrapped).call(
        "edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'LOCAL'"}]}
    )
    assert (worktree / "pkg/alpha.py").read_text() == "def alpha():\n    return 'LOCAL'\n"
    assert outcome.is_error
    assert "the server's view is behind" in outcome.content[0]["text"]


def test_a_degraded_server_answer_is_carried_through_unchanged(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold link layer flags the answer; the client must not hide the flag."""
    from lemoncrow_client import session as session_module

    original = session_module._unwrap

    def flagged(tool, payload, fallback):  # type: ignore[no-untyped-def]
        answer = original(tool, dict(payload) | {"degraded": True, "degraded_reason": "link:cold"}, fallback)
        return answer

    monkeypatch.setattr(session_module, "_unwrap", flagged)
    outcome = Dispatcher(config, bootstrapped).call("relations", {"path": "pkg/beta.py"})
    assert outcome.degraded
    assert outcome.degraded_reason == "link:cold"


def test_session_unknown_reopens_once_and_retries_the_tool(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    original_post_tool = RemoteSession._post_tool
    original_bootstrap = RemoteSession.bootstrap
    calls = {"post_tool": 0, "bootstrap": 0}

    def stale_once(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        calls["post_tool"] += 1
        if calls["post_tool"] == 1:
            from lemoncrow_client.errors import AgentAction, ClientError

            raise ClientError(
                ErrorCode.SESSION_UNKNOWN,
                "session is not available",
                action=AgentAction.REOPEN_SESSION,
            )
        return original_post_tool(self, tool, arguments, reuse_validator=reuse_validator)

    def counted_bootstrap(self: RemoteSession, **kwargs):
        calls["bootstrap"] += 1
        return original_bootstrap(self, **kwargs)

    monkeypatch.setattr(RemoteSession, "_post_tool", stale_once)
    monkeypatch.setattr(RemoteSession, "bootstrap", counted_bootstrap)
    outcome = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert not outcome.is_error
    assert calls == {"post_tool": 2, "bootstrap": 1}
    assert bootstrapped.state.online is True
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "session_reopen"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
    assert events[-1].tool == "read"


def test_session_unknown_rebootstrap_failure_returns_retryable_typed_error(
    bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)

    from lemoncrow_client.errors import AgentAction, ClientError
    from lemoncrow_client.session import BootstrapReport

    monkeypatch.setattr(
        RemoteSession,
        "_post_tool",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(
            ClientError(ErrorCode.SESSION_UNKNOWN, "gone", action=AgentAction.REOPEN_SESSION)
        ),
    )
    monkeypatch.setattr(
        RemoteSession,
        "bootstrap",
        lambda self, **kwargs: BootstrapReport(
            ok=False,
            reason="server still starting",
            session_id="",
            view_id="",
            view_revision=0,
            warm=False,
            files=0,
            uploaded_blobs=0,
            uploaded_bytes=0,
            manifest_chunks_sent=0,
            elapsed_s=0.0,
        ),
    )
    outcome = dispatcher.call("code_search", {"query": "alpha"})
    assert outcome.is_error
    body = outcome.content[0]["text"]
    assert ErrorCode.SERVER_SESSION_UNAVAILABLE.value in body
    assert "retry_later" in body
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "session_reopen"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "failed"]
