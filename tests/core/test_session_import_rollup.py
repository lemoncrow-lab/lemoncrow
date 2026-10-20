"""Imported sessions are reported through the public savings rollup, nowhere else.

Session import used to POST per-session stats to a separate sync endpoint.
They now go through the existing rollup client, so the destination and every
opt-out are the rollup channel's own.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.identity import get_anon_id
from lemoncrow.core.foundation.models import ToolCall, Trace, UsageEntry
from lemoncrow.core.service.session_import import import_agent_sessions, report_imported_sessions
from lemoncrow.core.service.telemetry import config as config_mod
from lemoncrow.core.service.telemetry.config import save_telemetry_config
from lemoncrow.infra.storage.factory import create_store
from tests.helpers import init_store_at

_ROLLUP = "https://lemoncrow.com/api/telemetry/rollup"
_PROMPT = "PRIVATE-PROMPT fix the billing bug in /home/alice/secret-repo"
_SESSION = "host-session-0001"
_FORBIDDEN = ("PRIVATE-PROMPT", "alice", "secret-repo", "billing", _SESSION, "task")


class _Wire:
    """Stands in for the network: records every request the rollup client makes."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.status = 200

    def urlopen(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        wire = self

        class _Response:
            status = wire.status

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        return _Response()

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.data) for request in self.requests]  # type: ignore[arg-type]


@pytest.fixture()
def wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """Telemetry allowed (the pytest guard lifted), no policy, no server, no overrides."""
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "policy.toml"))
    monkeypatch.setattr(config_mod, "_system_policy_path", lambda: tmp_path / "system-policy.toml")
    client_home = tmp_path / "client-home"
    client_home.mkdir()
    monkeypatch.setenv("LEMONCROW_HOME", str(client_home))
    for name in (
        "LEMONCROW_TELEMETRY",
        "DO_NOT_TRACK",
        "LEMONCROW_URL",
        "LEMONCROW_INSTALL_MODE",
        "LEMONCROW_TOKEN",
        "LEMONCROW_TOKEN_FILE",
        "LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    recorder = _Wire()
    monkeypatch.setattr(urllib.request, "urlopen", recorder.urlopen)
    return recorder


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    root = tmp_path / "store"
    init_store_at(str(root))
    return create_store(root)


def _trace(
    trace_id: str = "claude-proj-0001",
    *,
    session_id: str = _SESSION,
    host: str = "claude",
    age: timedelta = timedelta(minutes=30),
    output_tokens: int = 500,
) -> Trace:
    started = datetime.now(UTC) - age
    return Trace(
        id=trace_id,
        session_id=session_id,
        agent="lemoncrow:code",
        host=host,
        domain="coding",
        task=_PROMPT,
        status="success",
        input_tokens=1_200,
        output_tokens=output_tokens,
        cached_input_tokens=90_000,
        tools_called=[ToolCall(name="Read", args_hash="", count=3), ToolCall(name="Bash", args_hash="", count=2)],
        usage_entries=[
            UsageEntry(kind="llm", model="m", input_tokens=600, output_tokens=250, created_at=started),
            UsageEntry(
                kind="llm", model="m", input_tokens=600, output_tokens=250, created_at=started + timedelta(minutes=10)
            ),
        ],
        created_at=started,
    )


def _record(store: Any, *traces: Trace) -> list[str]:
    for trace in traces:
        store.history.record_trace(trace)
    return [trace.id for trace in traces]


def test_an_imported_session_is_posted_to_the_lemoncrow_rollup_with_hashed_keys(wire: _Wire, store: Any) -> None:
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 1

    assert wire.urls == [_ROLLUP]
    request = wire.requests[0]
    assert request.get_method() == "POST"
    (body,) = wire.bodies()
    anon_id = get_anon_id()
    assert body["install_key"] == hashlib.sha256(anon_id.encode()).hexdigest()
    assert body["session_key"] == hashlib.sha256(f"{anon_id}:{_SESSION}".encode()).hexdigest()
    assert body["source"] == "import:claude"
    # input + output only, like the daily rollup's real totals: cache tokens stay out.
    assert body["tokens_processed"] == 1_700
    assert body["calls_made"] == 5
    assert body["turn_count"] == 2
    assert body["time_spent_seconds"] == 600
    assert body["saved_usd"] == 0 and body["est_cost_usd"] == 0

    # Nothing prompt-derived, nothing raw: no task text, path, or session/install id.
    wire_text = request.data.decode() if isinstance(request.data, bytes) else ""
    for needle in (*_FORBIDDEN, anon_id):
        assert needle not in wire_text, needle
    assert set(body) <= {
        "install_key",
        "session_key",
        "lemoncrow_version",
        "source",
        "domain",
        "saved_usd",
        "tokens_saved",
        "calls_avoided",
        "carry_usd",
        "carry_tokens",
        "turn_count",
        "turns_avoided",
        "est_cost_usd",
        "time_saved_seconds",
        "output_saved_tokens",
        "output_saved_usd",
        "tokens_processed",
        "calls_made",
        "time_spent_seconds",
        "occurred_at",
    }


def test_session_import_itself_triggers_the_report(
    wire: _Wire, store: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Importer:
        def __init__(self, store: Any) -> None:
            self.store = store

        def import_all(self, path: Path | None = None, *, force: bool = False) -> list[str]:
            return _record(self.store, _trace("codex-0001", session_id="codex-session-1", host="codex"))

    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.registry.iter_importer_classes",
        lambda: [("codex", _Importer)],
    )

    assert import_agent_sessions(store.history.root) == {"codex": 1}

    assert wire.urls == [_ROLLUP]
    assert wire.bodies()[0]["source"] == "import:codex"


def test_the_pytest_guard_keeps_the_suite_off_the_wire(
    wire: _Wire, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [("DO_NOT_TRACK", "1"), ("LEMONCROW_TELEMETRY", "off")],
)
def test_the_env_opt_out_sends_and_marks_nothing(
    wire: _Wire, store: Any, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []
    assert store.history.synced_payload_hashes(ids) == {}


def test_the_users_saved_off_destination_sends_nothing(wire: _Wire, store: Any) -> None:
    save_telemetry_config(destination="off")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []


def test_an_administrator_policy_of_off_wins_over_the_users_choice(wire: _Wire, store: Any, tmp_path: Path) -> None:
    save_telemetry_config(destination="lemoncrow")
    (tmp_path / "policy.toml").write_text('[telemetry]\ndestination = "off"\n', encoding="utf-8")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []


def test_an_unusable_policy_fails_closed(wire: _Wire, store: Any, tmp_path: Path) -> None:
    (tmp_path / "policy.toml").write_text("not [valid toml", encoding="utf-8")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []


def test_a_custom_destination_gets_the_rollup_and_lemoncrow_gets_nothing(wire: _Wire, store: Any) -> None:
    # The rollup channel's own rule: an enterprise/custom destination receives
    # the rollup at its own endpoint with its own token -- never lemoncrow.com.
    save_telemetry_config(destination="custom", endpoint="https://telemetry.corp.test", token="org-token")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 1

    assert wire.urls == ["https://telemetry.corp.test/api/telemetry/rollup"]
    assert wire.requests[0].get_header("Authorization") == "Bearer org-token"
    assert all("lemoncrow.com" not in url for url in wire.urls)


def test_a_hosted_server_destination_does_not_send_to_lemoncrow(
    wire: _Wire, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_URL", "https://lc.corp.test")
    ids = _record(store, _trace())

    assert report_imported_sessions(store.history.root, ids, store=store) == 1

    assert wire.urls == ["https://lc.corp.test/api/telemetry/rollup"]


def test_an_already_reported_session_is_not_resent_but_a_changed_one_is(wire: _Wire, store: Any) -> None:
    ids = _record(store, _trace())
    root = store.history.root

    assert report_imported_sessions(root, ids, store=store) == 1
    assert report_imported_sessions(root, ids, store=store) == 0
    assert len(wire.requests) == 1
    assert list(store.history.synced_payload_hashes(ids)) == ids

    # The session kept growing and was re-imported: same session_key, new snapshot.
    _record(store, _trace(output_tokens=900))
    assert report_imported_sessions(root, ids, store=store) == 1
    assert len(wire.requests) == 2
    assert wire.bodies()[0]["session_key"] == wire.bodies()[1]["session_key"]
    assert wire.bodies()[1]["tokens_processed"] == 2_100


def test_a_failed_post_marks_nothing_and_ends_the_run(wire: _Wire, store: Any) -> None:
    wire.status = 503
    ids = _record(
        store,
        _trace("claude-a", session_id="session-a"),
        _trace("claude-b", session_id="session-b"),
    )
    root = store.history.root

    assert report_imported_sessions(root, ids, store=store) == 0

    assert len(wire.requests) == 1  # stopped at the first failure
    assert store.history.synced_payload_hashes(ids) == {}
    wire.status = 200
    assert report_imported_sessions(root, ids, store=store) == 2


def test_sessions_the_daily_rollup_already_reports_are_skipped(wire: _Wire, store: Any) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = store.history.root
    # A LemonCrow-measured session has a savings ledger; the daily rollup counts it.
    ledger = session_dir(root, "claude", _SESSION) / "savings.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "session_id": _SESSION,
                "tool_name": "Read",
                "lever": "structure_map",
                "tokens_saved": 1200,
                "cost_saved_usd": 0.0036,
                "model": "claude-sonnet-4-5",
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ids = _record(
        store,
        _trace("claude-covered", session_id=_SESSION),
        _trace("claude-uncovered", session_id="never-measured"),
    )

    assert report_imported_sessions(root, ids, store=store) == 1
    assert [body["session_key"] for body in wire.bodies()] == [
        hashlib.sha256(f"{get_anon_id()}:never-measured".encode()).hexdigest()
    ]


def test_history_older_than_the_endpoints_window_and_empty_sessions_are_not_reported(wire: _Wire, store: Any) -> None:
    empty = Trace(id="claude-empty", session_id="empty", agent="a", domain="coding", task="t", status="success")
    ids = _record(store, _trace("claude-old", session_id="old", age=timedelta(days=4)), empty)

    assert report_imported_sessions(store.history.root, ids, store=store) == 0
    assert wire.requests == []


def test_at_most_twenty_sessions_go_out_per_run(wire: _Wire, store: Any) -> None:
    ids = _record(store, *(_trace(f"claude-{n}", session_id=f"session-{n}") for n in range(25)))

    assert report_imported_sessions(store.history.root, ids, store=store) == 20
    assert len(wire.requests) == 20
