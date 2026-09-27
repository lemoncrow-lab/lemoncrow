from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _stub import StubServer
from conftest import make_config
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode
from lemoncrow_client.routing import ROUTES, Effect
from lemoncrow_client.session import RemoteSession
from lemoncrow_client.trace import BULK_CONTENT_KEYS, ClientTraceWriter, new_trace_id, sanitize_arguments


def _records(state_dir: Path) -> list[dict]:
    out = []
    for path in sorted((state_dir / "traces").glob("*/client-*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return out


def test_every_call_gets_a_trace_and_server_calls_share_its_id(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, state_dir: Path
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    remote = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    local = dispatcher.call("bash", {"command": "printf hi"})
    assert remote.trace_id.startswith("lt_") and local.trace_id.startswith("lt_")
    assert stub.state.tool_request_ids[-1] == remote.trace_id  # one id, client -> server
    by_id = {r["trace_id"]: r for r in _records(state_dir)}
    assert by_id[remote.trace_id]["execution"]["site"] == "server"
    assert by_id[local.trace_id]["execution"]["site"] == "client"  # local tools are traced too
    assert by_id[local.trace_id]["writer"] == "client"


def test_hosted_clients_hash_arguments_and_bulk_content_is_never_stored(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir)  # hosted stub
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=10.0).ok
    Dispatcher(config, session).call("edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'SECRET'"}]})
    text = json.dumps(_records(state_dir))
    assert "SECRET" not in text and "pkg/alpha.py" not in text


def test_raw_mode_keeps_paths_but_still_drops_bulk_content(state_dir: Path) -> None:
    raw = sanitize_arguments({"path": "a.py", "new": "body" * 50, "query": "q"}, "raw")
    assert raw["path"] == "a.py" and raw["query"] == "q" and raw["new"]["omitted"] == "bulk_content"
    assert {"content", "new", "old", "diff"} <= BULK_CONTENT_KEYS


def test_off_writes_nothing(stub: StubServer, worktree: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir, LEMONCROW_RUNTIME_TRACE="off")
    monkeypatch.setenv("LEMONCROW_RUNTIME_TRACE", "off")
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=10.0).ok
    Dispatcher(config, session).call("bash", {"command": "true"})
    assert not (state_dir / "traces").exists()


def test_writer_prunes_by_age_and_size(tmp_path: Path) -> None:
    writer = ClientTraceWriter(tmp_path, env={"LEMONCROW_TRACE_RETENTION_DAYS": "3", "LEMONCROW_TRACE_MAX_MB": "1"})
    today = datetime(2026, 9, 23, tzinfo=UTC)
    for offset in range(6):
        day = writer.dir / (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        day.mkdir(parents=True)
        (day / "client-1.jsonl").write_text("x" * 10)
    # Age: only days older than the 3-day window go.
    assert set(writer.prune(today=today)) == {"2026-09-18", "2026-09-19"}
    # Size: 4 days x 10 B = 40 B over a 25 B cap -> the two oldest go (20 B fits).
    writer.max_bytes = 25
    assert writer.prune(today=today) == ["2026-09-20", "2026-09-21"]
    assert sorted(p.name for p in writer.dir.iterdir()) == ["2026-09-22", "2026-09-23"]
    writer.max_bytes = 1
    writer.prune(today=today)
    assert [p.name for p in writer.dir.iterdir()] == ["2026-09-23"]


def test_client_trace_files_are_owner_only(tmp_path: Path) -> None:
    import stat

    writer = ClientTraceWriter(tmp_path, env={})
    writer.record(
        trace_id=new_trace_id(),
        tool="grep",
        arguments={"pattern": "x"},
        started_at=datetime.now(UTC),
        duration_ms=1,
        site="client",
        is_error=False,
        degraded=False,
        degraded_reason="",
        output_chars=0,
        session_id=None,
        view_revision=None,
    )
    (file,) = list((tmp_path / "traces").glob("*/client-*.jsonl"))
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "traces").stat().st_mode) == 0o700


def test_trace_id_format_matches_the_runtime_contract() -> None:
    from lemoncrow.core.foundation.runtime_trace import is_trace_id

    assert is_trace_id(new_trace_id())


# --------------------------------------------------------------------------- #
# R5: effect classes and transparent recovery                                 #
# --------------------------------------------------------------------------- #


def test_every_route_has_an_explicit_fail_safe_effect_class() -> None:
    for tool in ("code_search", "read", "grep", "blame", "scan", "relations", "graph", "search", "web_fetch"):
        assert ROUTES[tool].effect is Effect.PURE, tool
    for tool in ("edit", "bash", "codemod", "sql", "memory", "trace", "compact", "rescue", "cache"):
        assert ROUTES[tool].effect is Effect.SIDE_EFFECTING and not ROUTES[tool].retry_safe, tool
    assert ROUTES["index"].retry_safe


def test_pure_call_is_retried_transparently_after_the_server_returns(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    original = RemoteSession._post_tool
    attempts = {"n": 0}

    def flaky(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ClientError(
                ErrorCode.SERVER_UNREACHABLE, "connection refused", retryable=True, action=AgentAction.RETRY_LATER
            )
        return original(self, tool, arguments, reuse_validator=reuse_validator)

    monkeypatch.setattr(RemoteSession, "_post_tool", flaky)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    outcome = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert not outcome.is_error and attempts["n"] == 3
    events = [e for e in bootstrapped.state.recovery_events if e.kind == "reconnect_retry"]
    assert [e.outcome for e in events] == ["retrying", "recovered"]
    record = next(r for r in _records(config.state_dir) if r["trace_id"] == outcome.trace_id)
    assert record["execution"]["recoveries"] == ["reconnect_retry:retrying", "reconnect_retry:recovered"]


def test_a_timeout_is_never_resent(
    bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}

    def timeout(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        attempts["n"] += 1
        raise ClientError(
            ErrorCode.SERVER_UNREACHABLE,
            "timed out",
            details={"timed_out": True},
            retryable=True,
            action=AgentAction.RETRY_LATER,
        )

    monkeypatch.setattr(RemoteSession, "_post_tool", timeout)
    outcome = Dispatcher(config, bootstrapped).call("read", {"files": ["pkg/alpha.py"]})
    assert outcome.is_error and attempts["n"] == 1


def test_side_effecting_server_tools_are_never_resent(
    bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}

    def gone(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        attempts["n"] += 1
        raise ClientError(ErrorCode.SERVER_UNREACHABLE, "refused", retryable=True, action=AgentAction.RETRY_LATER)

    monkeypatch.setattr(RemoteSession, "_post_tool", gone)
    Dispatcher(config, bootstrapped).call("memory", {"action": "list"})
    assert attempts["n"] == 1


def test_reconnect_gives_up_after_its_budget(
    bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}
    clock = {"t": 0.0}

    def gone(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        attempts["n"] += 1
        raise ClientError(ErrorCode.SERVER_UNREACHABLE, "refused", retryable=True, action=AgentAction.RETRY_LATER)

    monkeypatch.setattr(RemoteSession, "_post_tool", gone)
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    outcome = Dispatcher(config, bootstrapped).call("code_search", {"query": "alpha"})
    assert outcome.is_error and 3 <= attempts["n"] <= 40
    assert bootstrapped.state.recovery_events[-1].outcome == "failed"


def test_state_token_changes_with_binding() -> None:
    from lemoncrow_client.state import SessionState

    a = SessionState(session_id="s", view_id="v", view_revision=1)
    b = SessionState(session_id="s", view_id="v", view_revision=1, generation=1)
    assert (
        a.state_token() != b.state_token()
        and a.state_token() == SessionState(session_id="s", view_id="v", view_revision=1).state_token()
    )
