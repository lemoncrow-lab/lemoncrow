"""Anonymous feature counts: what is counted, what stops it, and what leaves the machine."""

from __future__ import annotations

import io
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lemoncrow_client import cli, usage, usage_cli

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
YESTERDAY = datetime(2026, 11, 17, 12, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "LEMONCROW_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "LEMONCROW_TELEMETRY_POLICY": str(tmp_path / "policy.toml"),
        "LEMONCROW_TELEMETRY_ALLOW_IN_TESTS": "1",
    }


def _notified(env: dict[str, str]) -> None:
    usage.announce(io.StringIO(), env)


def test_the_notice_comes_once_and_nothing_is_counted_before_it(env: dict[str, str]) -> None:
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    assert usage.pending(env) == {}
    first, second = io.StringIO(), io.StringIO()
    usage.announce(first, env)
    usage.announce(second, env)
    assert first.getvalue() == usage.NOTICE + "\n"
    assert second.getvalue() == ""
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    assert usage.pending(env)["2026-11-18"][0]["dim"] == "review-local"


@pytest.mark.parametrize(
    "switch",
    [{"DO_NOT_TRACK": "1"}, {"LEMONCROW_TELEMETRY": "off"}, {"CI": "true"}],
)
def test_every_switch_stops_the_notice_and_the_counting(env: dict[str, str], switch: dict[str, str]) -> None:
    env.update(switch)
    stream = io.StringIO()
    usage.announce(stream, env)
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    assert stream.getvalue() == ""
    assert usage.pending(env) == {}


@pytest.mark.parametrize("policy", ['[telemetry]\ndestination = "off"\n', "not toml ["])
def test_a_policy_that_is_not_lemoncrow_or_cannot_be_read_turns_it_off(
    env: dict[str, str], tmp_path: Path, policy: str
) -> None:
    (tmp_path / "policy.toml").write_text(policy)
    assert usage.off_reason(env) == "administrator policy"


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ({"LEMONCROW_INSTALL_MODE": "hosted"}, True),
        ({"LEMONCROW_URL": "https://review.example.test"}, True),
        ({"LEMONCROW_URL": "http://127.0.0.1:7420"}, False),
    ],
)
def test_a_hosted_install_is_not_counted(env: dict[str, str], setting: dict[str, str], expected: bool) -> None:
    env.update(setting)
    assert usage_cli.hosted_install(env) is expected
    assert bool(usage_cli.off_reason(env)) is expected


def test_the_engine_side_of_counting_imports_nothing_else_from_the_client() -> None:
    source = Path(usage.__file__).read_text(encoding="utf-8")
    assert "from ." not in source
    assert "urllib" not in source


def test_turning_it_off_deletes_what_is_waiting_and_keeps_other_settings(env: dict[str, str]) -> None:
    _notified(env)
    config = Path(env["XDG_CONFIG_HOME"]) / "lemoncrow" / "telemetry.toml"
    config.write_text('[telemetry]\nendpoint = "https://example.test"\n')
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    usage.set_enabled(False, env)
    assert usage.pending(env) == {}
    assert usage.off_reason(env) == "turned off"
    assert tomllib.loads(config.read_text())["telemetry"]["endpoint"] == "https://example.test"
    usage.set_enabled(True, env)
    assert usage.off_reason(env) == ""


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["review"], ("review-local", "cli")),
        (["review", "--ai", "--agent", "claude"], ("review-local-ai", "cli")),
        (["review", "--pr", "12", "--ai"], ("review-local-ai", "pr")),
        (["review", "config", "import", "/secret/path"], ("review-config-import", "cli")),
        (["review", "status", "--json"], None),
        (["review", "hook", "after-edit"], None),
        (["review", "--help"], None),
        (["with", "claude"], ("runtime-lite", "cli")),
        (["rt", "lite-mcp"], None),
        (["audit"], None),
    ],
)
def test_commands_are_classified_by_verb_and_flag_name(arguments: list[str], expected: object) -> None:
    assert usage.classify(arguments) == expected


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/reviews/r1/ask/stream", "review-ask"),
        ("GET", "/api/reviews/r1/ask", None),
        ("GET", "/v1/reviews/r1/revisions/2/map", "review-map"),
        ("POST", "/api/reviews/r1/threads/t1/summary", "review-discussion-summaries"),
        ("GET", "/api/review-analytics", "review-analytics"),
        ("GET", "/api/reviews/r1", None),
    ],
)
def test_reader_requests_map_to_features(method: str, path: str, expected: str | None) -> None:
    assert usage.classify_reader_request(method, path) == expected


def test_only_finished_days_are_sent_and_the_payload_holds_only_the_allowlist(env: dict[str, str]) -> None:
    _notified(env)
    usage.record("review-local-ai", surface="cli", ok=False, seconds=42, environ=env, now=YESTERDAY)
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    day_file = usage.usage_dir(env) / "2026-11-17.jsonl"
    with day_file.open("a") as handle:
        handle.write('{"f":"/Users/me/secret","s":"cli","o":"ok","b":"<1"}\nnot json\n')
    calls: list[dict[str, object]] = []

    def post(_url: str, payload: dict[str, object], _timeout: float) -> int:
        calls.append(payload)
        return 200

    assert usage.flush("1.2.3", post, environ=env, now=NOW) == 1
    assert usage.flush("1.2.3", post, environ=env, now=NOW) == 0
    assert len(calls) == 1
    assert calls[0]["day"] == "2026-11-17"
    assert calls[0]["rows"] == [
        {
            "event": "feature_used",
            "dim": "review-local-ai",
            "context": "cli",
            "outcome": "error",
            "bucket": "10-60",
            "n": 1,
        }
    ]
    assert "secret" not in json.dumps(calls)
    assert list(usage.pending(env)) == ["2026-11-18"]


def test_a_day_with_nothing_due_does_not_use_up_the_daily_attempt(env: dict[str, str]) -> None:
    _notified(env)
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=NOW)
    assert usage.flush("1", lambda *_: 200, environ=env, now=NOW) == 0
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=YESTERDAY)
    assert usage.flush("1", lambda *_: 200, environ=env, now=NOW) == 1


def test_a_failed_post_keeps_the_day_for_a_later_attempt(env: dict[str, str]) -> None:
    _notified(env)
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=YESTERDAY)
    assert usage.flush("1", lambda *_: 503, environ=env, now=NOW) == 0
    assert "2026-11-17" in usage.pending(env)


def test_the_install_key_is_not_the_full_engines(env: dict[str, str]) -> None:
    import hashlib

    _notified(env)
    usage.record("review-local", surface="cli", ok=True, seconds=1, environ=env, now=YESTERDAY)
    keys: list[object] = []
    usage.flush("1", lambda _u, payload, _t: keys.append(payload["install_key"]) or 200, environ=env, now=NOW)
    identity = (Path(env["XDG_CONFIG_HOME"]) / "lemoncrow" / "telemetry_id").read_text().strip()
    assert keys and keys[0] != hashlib.sha256(identity.encode()).hexdigest()


def test_a_failure_while_counting_never_changes_the_command(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda _arguments, _sink: 7)
    monkeypatch.setattr(usage_cli, "announce", lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")))
    assert cli.main(["review"], io.StringIO()) == 7


def test_friction_metrics_are_counted_like_features_and_become_the_same_rows(env: dict[str, str]) -> None:
    _notified(env)
    usage.record("review-friction-wait-author", surface="reader", ok=True, seconds=120, environ=env, now=NOW)
    usage.record("review-friction-not-listed", surface="reader", ok=True, seconds=1, environ=env, now=NOW)
    assert usage.pending(env)[NOW.strftime("%Y-%m-%d")] == [
        {
            "event": "feature_used",
            "dim": "review-friction-wait-author",
            "context": "reader",
            "outcome": "ok",
            "bucket": "60-300",
            "n": 1,
        }
    ]


def test_every_counted_feature_is_a_capability() -> None:
    index = Path(__file__).resolve().parents[2] / "docs" / "status" / "capabilities.toml"
    if not index.exists():
        pytest.skip("the status index ships in the private checkout only")
    data = tomllib.loads(index.read_text(encoding="utf-8"))
    known = {item["id"] for item in data.get("capability") or data["capabilities"]}
    assert usage.FEATURES <= known
