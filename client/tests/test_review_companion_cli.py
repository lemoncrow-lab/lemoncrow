"""Review Companion diagnostics never claim work or trust rotated signing keys."""

from __future__ import annotations

import io
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lemoncrow_client import review_companion_cli as companion
from lemoncrow_client.review import review_main
from lemoncrow_client.review_device_connect import save_pairing
from lemoncrow_client.review_worker_client import WorkerBinding
from lemoncrow_client.transport import Response


def payload() -> dict[str, Any]:
    return {
        "org_id": "orgA",
        "repo_id": "repoA",
        "worker_id": "machineA",
        "machine_token": "separate-review-worker-machine-token-12345678",
        "kind": "vps",
        "execution_classes": ["runtime_validation"],
        "expires_at": time.time() + 3600,
        "worker_signing_keys": [
            {
                "key_id": "signerA",
                "algorithm": "Ed25519",
                "status": "active",
                "public_key_hex": "11" * 32,
            }
        ],
    }


def binding() -> WorkerBinding:
    data = payload()
    return WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id=data["org_id"],
        repo_id=data["repo_id"],
        worker_id=data["worker_id"],
        bearer=data["machine_token"],
        trusted_key_id="signerA",
        trusted_public_key_hex="11" * 32,
    )


@pytest.mark.parametrize(
    ("key_override", "expect_valid"),
    [
        ({}, True),
        ({"status": "retired"}, False),
        ({"status": "compromised"}, False),
        ({"public_key_hex": "22" * 32}, False),
        ({"key_id": "another"}, False),
        ({"algorithm": "RSA"}, False),
    ],
)
def test_server_signing_key_pin_is_fail_closed(
    key_override: dict[str, str],
    expect_valid: bool,
) -> None:
    machine = binding()
    sent: list[dict[str, str]] = []

    class Transport:
        def get(self, path: str, *, headers: dict[str, str]) -> Response:
            assert path == "/v1/review-workers/keys"
            sent.append(headers)
            return Response(
                200,
                {
                    "algorithm": "Ed25519",
                    "keys": [
                        {
                            "key_id": "signerA",
                            "status": "active",
                            "algorithm": "Ed25519",
                            "public_key_hex": "11" * 32,
                            **key_override,
                        }
                    ],
                },
            )

    if expect_valid:
        companion.check_machine_keys(machine, transport=Transport())  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError):
            companion.check_machine_keys(machine, transport=Transport())  # type: ignore[arg-type]
    assert sent == [{"Authorization": f"Bearer {machine.bearer}"}]


def test_companion_status_is_local_and_check_only_uses_fixed_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = tmp_path / "review-state"
    machine = payload()
    save_pairing(repo, state, machine, "https://app.lemoncrow.com")
    config = SimpleNamespace(
        repo_root=repo,
        review_state_dir=state,
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
    )
    monkeypatch.setattr(companion, "load_config", lambda **_kwargs: config)
    import lemoncrow_client.review_device_connect as connector

    monkeypatch.setattr(connector, "load_config", lambda **_kwargs: config)
    requests: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == config.url and timeout_s == 15.0

        def get(self, route: str, *, headers: dict[str, str]) -> Response:
            requests.append(route)
            assert headers["Authorization"] == f"Bearer {machine['machine_token']}"
            return Response(200, {"algorithm": "Ed25519", "keys": machine["worker_signing_keys"]})

        def post(self, route: str, *, body: dict[str, object], headers: dict[str, str]) -> Response:
            assert route == "/v1/review-workers/presence" and body == {}
            assert headers["Authorization"] == "Bearer " + str(machine["machine_token"])
            requests.append(route)
            return Response(200, {"worker_id": "machineA", "last_seen_at": 1_900_000_001.0})

        def close(self) -> None:
            requests.append("closed")

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    status = io.StringIO()
    assert review_main(["companion", "status"], sink=status) == 0
    assert "machineA" in status.getvalue()
    assert not requests
    checked = io.StringIO()
    assert review_main(["companion", "check"], sink=checked) == 0
    assert requests == ["/v1/review-workers/keys", "closed"]
    assert "Work execution is not enabled" in checked.getvalue()
    assert machine["machine_token"] not in checked.getvalue()
    unsupported = io.StringIO()
    # Unattended start needs an explicit model route and source approval before anything is claimed.
    assert review_main(["companion", "start"], sink=unsupported) == 2
    assert requests == ["/v1/review-workers/keys", "closed"]
    route = ["--route", "r", "--provider", "openai", "--model", "m", "--generation", "g"]
    route += ["--input-price", "1", "--output-price", "1", "--max-cost", "1000"]
    assert review_main(["companion", "start", *route], sink=unsupported) == 2
    assert "--allow-external-source" in unsupported.getvalue()
    assert requests == ["/v1/review-workers/keys", "closed"]
    once = io.StringIO()
    assert review_main(["companion", "online", "--once"], sink=once) == 0
    assert requests[-3:] == ["/v1/review-workers/keys", "/v1/review-workers/presence", "closed"]
    assert "No jobs will be executed" in once.getvalue()
    stopped = io.StringIO()
    monkeypatch.setattr(companion.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert review_main(["companion", "online"], sink=stopped) == 0
    assert "presence stopped" in stopped.getvalue()
    assert requests[-3:] == ["/v1/review-workers/keys", "/v1/review-workers/presence", "closed"]


def test_no_machine_does_not_touch_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = SimpleNamespace(
        repo_root=repo,
        review_state_dir=tmp_path / "review-state",
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
    )
    monkeypatch.setattr(companion, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(companion, "HttpTransport", lambda *_a, **_k: pytest.fail("network"))
    output = io.StringIO()
    assert companion.companion_main(["check"], sink=output) == 1
    assert "No Review machine" in output.getvalue()


def test_presence_must_match_signed_machine_not_server_claim() -> None:
    machine = binding()

    class Transport:
        def post(self, _route: str, **_kwargs: Any) -> Response:
            return Response(200, {"worker_id": "another-machine", "last_seen_at": 1_900_000_002.0})

    with pytest.raises(ValueError, match="does not match"):
        companion.presence_heartbeat(machine, transport=Transport())  # type: ignore[arg-type]


def test_authentication_failure_does_not_print_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = tmp_path / "review-state"
    save_pairing(repo, state, payload(), "https://app.lemoncrow.com")
    monkeypatch.setattr(
        companion,
        "load_config",
        lambda **_kwargs: SimpleNamespace(
            repo_root=repo,
            review_state_dir=state,
            url="https://app.lemoncrow.com",
            request_timeout_s=15.0,
        ),
    )

    class Transport:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def get(self, *_a: Any, **_kw: Any) -> Response:
            return Response(
                401,
                {
                    "error": {
                        "code": "unauthenticated",
                        "message": "secret-token-should-not-appear",
                        "retryable": False,
                        "action": "reauthenticate",
                    }
                },
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    output = io.StringIO()
    assert companion.companion_main(["check"], sink=output) == 1
    assert "secret-token" not in output.getvalue()
    assert "refused" in output.getvalue()


def test_companion_watch_requires_paired_machine_and_reuses_foreground_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the original watch publishes; presence sends no task or agent command."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = tmp_path / "review"
    record = payload()
    save_pairing(repo, state, record, "https://app.lemoncrow.com")
    config = SimpleNamespace(
        repo_root=repo,
        review_state_dir=state,
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
        hosted=True,
        authenticated=True,
    )
    monkeypatch.setattr(companion, "load_config", lambda **_kwargs: config)
    calls: list[str] = []
    ticks = iter([100.0, 101.0, 110.0, 170.0, 171.0])
    monkeypatch.setattr(companion.time, "monotonic", lambda: next(ticks))

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == config.url and timeout_s == 15.0

        def get(self, route: str, *, headers: dict[str, str]) -> Response:
            assert headers["Authorization"] == "Bearer " + str(record["machine_token"])
            calls.append(route)
            return Response(200, {"algorithm": "Ed25519", "keys": record["worker_signing_keys"]})

        def post(self, route: str, *, body: dict[str, object], headers: dict[str, str]) -> Response:
            assert body == {} and headers["Authorization"] == "Bearer " + str(record["machine_token"])
            calls.append(route)
            return Response(200, {"worker_id": "machineA", "last_seen_at": 1_900_000_000.0})

        def close(self) -> None:
            calls.append("closed")

    import lemoncrow_client.review_watch as watch_module

    monkeypatch.setattr(companion, "HttpTransport", Transport)

    def fake_watch(args: list[str], *, sink: Any, on_poll: Any, config_override: Any) -> int:
        assert args == ["--once"] and config_override is config
        on_poll()
        on_poll()  # no duplicate check-in before the next minute
        on_poll()
        sink.write("Existing delta capture watcher invoked.\n")
        return 0

    monkeypatch.setattr(watch_module, "watch_main", fake_watch)
    output = io.StringIO()
    assert review_main(["companion", "watch", "--once"], sink=output) == 0
    assert calls == [
        "/v1/review-workers/keys",
        "/v1/review-workers/presence",
        "/v1/review-workers/presence",
        "closed",
    ]
    assert record["machine_token"] not in output.getvalue()
    assert "no jobs or agents" in output.getvalue()
    assert "Existing delta capture watcher invoked." in output.getvalue()


@pytest.mark.parametrize("surface", ["watch", "companion"])
def test_companion_watch_captures_real_git_delta_gate_and_stops_if_revoked(
    surface: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import lemoncrow_client.review_watch as watch_module

    repo = tmp_path / "checkout"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "watch-test@example.invalid"),
        ("config", "user.name", "Watch Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    source = repo / "source.py"
    source.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True, capture_output=True)
    state = tmp_path / "review-state"
    record = payload()
    save_pairing(repo, state, record, "https://app.lemoncrow.com")
    config = SimpleNamespace(
        repo_root=repo,
        review_state_dir=state,
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
        hosted=True,
        authenticated=True,
    )
    monkeypatch.setattr(companion, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(watch_module, "load_config", lambda **_kwargs: config)
    argv = ["watch", "--once"] if surface == "watch" else ["companion", "watch", "--once"]
    captures: list[str] = []
    refused = False
    requests: list[str] = []

    class Transport:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def get(self, path: str, *, headers: dict[str, str]) -> Response:
            assert headers["Authorization"] == "Bearer " + str(record["machine_token"])
            return Response(200, {"algorithm": "Ed25519", "keys": record["worker_signing_keys"]})

        def post(self, path: str, *, body: dict[str, object], headers: dict[str, str]) -> Response:
            assert body == {} and headers["Authorization"] == "Bearer " + str(record["machine_token"])
            requests.append(path)
            if refused:
                return Response(401, {"error": {"code": "unauthenticated", "message": "private error"}})
            return Response(200, {"worker_id": "machineA", "last_seen_at": 1_900_000_000.0})

        def close(self) -> None:
            pass

    monkeypatch.setattr(companion, "HttpTransport", Transport)

    def capture(_config: Any, _rng: Any, **_kw: Any) -> tuple[dict[str, object], str]:
        captures.append("captured")
        return {}, "https://app.lemoncrow.com/r/review-1"

    monkeypatch.setattr(watch_module, "_capture", capture)
    # Clean worktree still checks in once, but never creates an empty Review.
    clean = io.StringIO()
    assert review_main(argv, sink=clean) == 0
    assert not captures and len(requests) == 1
    source.write_text("x = 2\n", encoding="utf-8")
    dirty = io.StringIO()
    assert review_main(argv, sink=dirty) == 0
    assert captures == ["captured"]
    assert "https://app.lemoncrow.com/r/review-1" in dirty.getvalue()
    # Revocation terminates combined capture BEFORE the source can be sent
    # using the independent human-authenticated capture channel.
    refused = True
    source.write_text("x = 3\n", encoding="utf-8")
    denied = io.StringIO()
    assert review_main(argv, sink=denied) == 1
    assert captures == ["captured"]
    assert "check-in refused; capture stopped" in denied.getvalue()
    assert "private error" not in denied.getvalue()
    assert record["machine_token"] not in denied.getvalue()


def test_default_watch_rejects_expired_paired_machine_without_source_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import lemoncrow_client.review_watch as watch_module

    repo = tmp_path / "checkout"
    repo.mkdir()
    state_dir = tmp_path / "review"
    record = payload()
    credentials = save_pairing(repo, state_dir, record, "https://app.lemoncrow.com")
    saved = json.loads(credentials.read_text(encoding="utf-8"))
    saved["expires_at"] = time.time() - 10
    credentials.write_text(json.dumps(saved), encoding="utf-8")
    config = SimpleNamespace(
        repo_root=repo,
        review_state_dir=state_dir,
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
        hosted=True,
        authenticated=True,
    )
    monkeypatch.setattr(watch_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(watch_module, "_capture", lambda *_a, **_kw: pytest.fail("unexpected source upload"))
    monkeypatch.setattr(companion, "HttpTransport", lambda *_a, **_kw: pytest.fail("expired machine used HTTP"))
    output = io.StringIO()
    assert review_main(["watch", "--once", "--json"], sink=output) == 1
    assert json.loads(output.getvalue()) == {"event": "connection_refused"}
    assert record["machine_token"] not in output.getvalue()


def test_companion_watch_refuses_missing_pairing_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.setattr(
        companion,
        "load_config",
        lambda **_kwargs: SimpleNamespace(
            repo_root=repo,
            review_state_dir=tmp_path / "state",
            url="https://app.lemoncrow.com",
            request_timeout_s=10.0,
            hosted=True,
            authenticated=True,
        ),
    )
    monkeypatch.setattr(companion, "HttpTransport", lambda *_a, **_kw: pytest.fail("network before pairing"))
    out = io.StringIO()
    assert review_main(["companion", "watch", "--once"], sink=out) == 1
    assert "No Review machine is paired" in out.getvalue()


@pytest.mark.parametrize(
    "reply",
    [
        {"execution_class": "runtime_validation", "automatic": 2, "manual": 1, "poll_after_s": 10},
    ],
)
def test_pending_machine_hint_never_contains_tasks_or_claims(reply: dict[str, Any]) -> None:
    machine = binding()
    calls: list[str] = []

    class Transport:
        def post(self, path: str, *, body: dict[str, str], headers: dict[str, str]) -> Response:
            calls.append(path)
            assert path == "/v1/review-workers/pending"
            assert body == {"execution_class": "runtime_validation"}
            assert headers["Authorization"] == "Bearer " + machine.bearer
            return Response(200, reply)

    assert companion.pending_work_hint(machine, transport=Transport(), execution_class="runtime_validation") == (
        2,
        1,
        10,
    )  # type: ignore[arg-type]
    assert calls == ["/v1/review-workers/pending"]


@pytest.mark.parametrize(
    "bad",
    [
        {"automatic": True, "manual": 0, "poll_after_s": 10},
        {"automatic": 101, "manual": 0, "poll_after_s": 10},
        {"automatic": 1, "manual": -1, "poll_after_s": 10},
        {"automatic": 0, "manual": 0, "poll_after_s": 2},
        {"automatic": 0, "manual": 0, "poll_after_s": "10"},
        {"automatic": 0, "manual": 0, "poll_after_s": 10, "execution_class": "review_intelligence"},
    ],
)
def test_pending_rejects_invalid_or_cross_class_server_reply(bad: dict[str, Any]) -> None:
    class Transport:
        def post(self, *args: Any, **kwargs: Any) -> Response:
            return Response(200, {"execution_class": "runtime_validation", **bad})

    with pytest.raises(ValueError):
        companion.pending_work_hint(binding(), transport=Transport(), execution_class="runtime_validation")  # type: ignore[arg-type]


def test_pending_command_is_read_only_and_class_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = binding()
    config = SimpleNamespace(request_timeout_s=15.0)
    monkeypatch.setattr(companion, "load_config", lambda **_: config)
    monkeypatch.setattr(companion, "_paired_binding", lambda _: machine)
    seen: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == machine.origin

        def close(self) -> None:
            seen.append("close")

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    monkeypatch.setattr(companion, "check_machine_keys", lambda *a, **kw: seen.append("keys"))
    monkeypatch.setattr(
        companion,
        "pending_work_hint",
        lambda machine, *, transport, execution_class: seen.append(execution_class) or (3, 1, 10),
    )
    output = io.StringIO()
    assert companion.companion_main(["pending", "--class", "review_intelligence"], sink=output) == 0
    assert "3 automatic queued" in output.getvalue()
    assert "1 manually approved" in output.getvalue()
    assert "no work claimed" in output.getvalue().lower()
    assert machine.bearer not in output.getvalue()
    assert seen == ["keys", "review_intelligence", "close"]
    seen.clear()
    assert companion.companion_main(["pending", "--class", "arbitrary"], sink=io.StringIO()) == 2
    assert seen == []


def test_pending_watch_reports_only_changes_and_stops_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = binding()
    monkeypatch.setattr(companion, "load_config", lambda **_: SimpleNamespace(request_timeout_s=5.0))
    monkeypatch.setattr(companion, "_paired_binding", lambda _: machine)
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == machine.origin

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    monkeypatch.setattr(companion, "check_machine_keys", lambda *a, **kw: None)
    observations = iter([(0, 0, 10), (0, 0, 10), (1, 2, 10)])
    monkeypatch.setattr(
        companion,
        "pending_work_hint",
        lambda machine, *, transport, execution_class: next(observations),
    )
    waits = 0

    def sleep(interval: float) -> None:
        nonlocal waits
        assert interval == 10
        waits += 1
        if waits == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(companion.time, "sleep", sleep)
    output = io.StringIO()
    assert companion.companion_main(["pending", "--watch"], sink=output) == 0
    assert output.getvalue().count("Review runtime_validation:") == 2
    assert "0 automatic queued" in output.getvalue()
    assert "1 automatic queued" in output.getvalue()
    assert "watch stopped" in output.getvalue()
    assert events == ["closed"]


def test_pending_watch_prefers_wss_and_never_calls_claim_or_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = binding()
    monkeypatch.setattr(companion, "load_config", lambda **_: SimpleNamespace(request_timeout_s=5.0))
    monkeypatch.setattr(companion, "_paired_binding", lambda _: machine)
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == machine.origin

        def websocket_hints(self, path: str, *, bearer: str, timeout_s: int, max_messages: int) -> Any:
            events.append("wss")
            assert path == "/v1/review-workers/wakeup?execution_class=runtime_validation"
            assert bearer == machine.bearer
            assert max_messages == 10
            yield {"execution_class": "runtime_validation", "automatic": 1, "manual": 0, "poll_after_s": 10}
            raise KeyboardInterrupt

        def post(self, *_a: Any, **_kw: Any) -> Any:
            pytest.fail("WSS never issues an HTTPS claim or uses fallback when healthy")

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    monkeypatch.setattr(companion, "check_machine_keys", lambda *_a, **_kw: events.append("keys"))
    output = io.StringIO()
    assert companion.companion_main(["pending", "--watch"], sink=output) == 0
    assert events == ["keys", "wss", "closed"]
    assert "1 automatic queued" in output.getvalue()
    assert "no work claimed" in output.getvalue().lower()
    assert machine.bearer not in output.getvalue()


def test_pending_wss_upgrade_failure_uses_https_without_trusting_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = binding()
    monkeypatch.setattr(companion, "load_config", lambda **_: SimpleNamespace(request_timeout_s=5.0))
    monkeypatch.setattr(companion, "_paired_binding", lambda _: machine)
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == machine.origin

        def websocket_hints(self, path: str, *, bearer: str, timeout_s: int, max_messages: int) -> Any:
            events.append("upgrade")
            raise ValueError("upgrade unavailable")
            yield {}  # makes this an iterator, whose failure happens during iteration

        def post(self, path: str, *, body: dict[str, object], headers: dict[str, str]) -> Response:
            assert path == "/v1/review-workers/pending"
            assert body == {"execution_class": "runtime_validation"}
            assert headers == {"Authorization": "Bearer " + machine.bearer}
            events.append("https")
            return Response(
                200, {"execution_class": "runtime_validation", "automatic": 2, "manual": 1, "poll_after_s": 10}
            )

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(companion, "HttpTransport", Transport)
    monkeypatch.setattr(companion, "check_machine_keys", lambda *_a, **_kw: events.append("keys"))
    monkeypatch.setattr(companion.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    output = io.StringIO()
    assert companion.companion_main(["pending", "--watch"], sink=output) == 0
    assert events == ["keys", "upgrade", "https", "closed"]
    assert "HTTPS polling" in output.getvalue()
    assert "2 automatic queued" in output.getvalue()
    assert "no work claimed" in output.getvalue().lower()
    assert machine.bearer not in output.getvalue()


def test_public_lcr_pending_dispatches_without_companion_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import review_companion_cli as legacy

    routes: list[list[str]] = []
    monkeypatch.setattr(legacy, "companion_main", lambda args, *, sink: routes.append(args) or 0)
    for arguments, target in (
        (["pending"], ["pending"]),
        (["pending", "--watch"], ["pending", "--watch"]),
        (["pending", "--class", "review_intelligence"], ["pending", "--class", "review_intelligence"]),
        (["device", "online", "--once"], ["online", "--once"]),
        (["device", "check"], ["check"]),
        (["device", "status"], ["status"]),
        (["task", "run", "job-123", "--help"], ["run", "job-123", "--help"]),
        (["companion", "pending"], ["pending"]),
    ):
        assert review_main(arguments, sink=io.StringIO()) == 0
        assert routes[-1] == target
    assert len(routes) == 8
    bad = io.StringIO()
    assert review_main(["device", "run", "job-123"], sink=bad) == 2
    assert "lcr device" in bad.getvalue()
    assert review_main(["task", "run"], sink=io.StringIO()) == 0  # parser receives it


def test_lcr_pending_help_does_not_access_hosted_network_or_paired_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(companion, "load_config", lambda **_: pytest.fail("pending help accessed configuration"))
    monkeypatch.setattr(companion, "HttpTransport", lambda *_a, **_k: pytest.fail("pending help accessed network"))
    out = io.StringIO()
    assert review_main(["pending", "--help"], sink=out) == 0
    assert "lcr pending" in out.getvalue()
    assert "No job is claimed" in out.getvalue()
    user_help = io.StringIO()
    assert review_main(["--help"], sink=user_help) == 0
    assert "lcr pending --watch" in user_help.getvalue()
    assert "lcr task run" in user_help.getvalue()
    assert "lcr companion" not in user_help.getvalue()
    # This old internal namespace remains executable only for compatibility.
    # It must not appear in the actual manual AI task argparse help either.
    from lemoncrow_client.review_agent_task_cli import run_manual_agent_task

    with pytest.raises(SystemExit) as done:
        run_manual_agent_task(["--help"], sink=io.StringIO())
    assert done.value.code == 0
