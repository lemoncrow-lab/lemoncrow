from __future__ import annotations

import ast
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.identity import get_anon_id
from lemoncrow.core.service.telemetry import emit_product
from lemoncrow.core.service.telemetry import public_rollup as public_rollup_mod
from lemoncrow.core.service.telemetry.banner import maybe_show_banner
from lemoncrow.core.service.telemetry.config import load_telemetry_config, save_telemetry_config
from lemoncrow.core.service.telemetry.frustration import match_frustration
from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore
from lemoncrow.core.service.telemetry.public_rollup import (
    _payload,
    flush_daily_public_rollup,
    maybe_flush_public_rollup,
    publish_public_savings_rollup,
    read_public_rollup_state,
)
from lemoncrow.core.service.telemetry.schema import EVENTS
from lemoncrow.core.service.telemetry.scrubber import scrub_string


@pytest.fixture()
def telemetry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "telemetry.db"
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(db_path))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(tmp_path / "telemetry_ack"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY", "0")
    return db_path


def test_emit_product_allowlists_scrubs_and_keeps_local_store(
    telemetry_env: Path,
) -> None:
    emit_product(
        "cli_command_invoked",
        command_name="context",
        session_id="00000000-0000-4000-8000-000000000000",
        anon_id="11111111-1111-4111-8111-111111111111",
        cwd="/home/example/private/repo",
        email="person@example.com",
    )

    events = LocalTelemetryStore(telemetry_env).list_events(limit=10)
    assert len(events) == 1
    props = events[0]["props"]
    assert props == {
        "anon_id": "11111111-1111-4111-8111-111111111111",
        "command_name": "context",
        "session_id": "00000000-0000-4000-8000-000000000000",
    }
    assert events[0]["exported"] is False


def test_public_rollup_payload_is_minimal_and_session_scoped(telemetry_env: Path) -> None:
    payload = _payload(
        session_id="session-1",
        saved_usd=0.1234567,
        tokens_saved=9240,
        calls_avoided=3,
        turn_count=5,
        source="codex",
        occurred_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
    )

    assert payload is not None
    # Privacy: only one-way hashed keys leave the machine, never raw ids.
    assert "anon_id" not in payload
    assert "session_id" not in payload
    anon_id = get_anon_id()
    assert payload["install_key"] == hashlib.sha256(anon_id.encode()).hexdigest()
    assert payload["session_key"] == hashlib.sha256(f"{anon_id}:session-1".encode()).hexdigest()
    assert payload["lemoncrow_version"]
    assert payload["source"] == "codex"
    assert payload["saved_usd"] == 0.123457
    assert payload["tokens_saved"] == 9240
    assert payload["calls_avoided"] == 3
    assert payload["turn_count"] == 5
    assert payload["turns_avoided"] == 0  # default when the caller omits it
    assert payload["occurred_at"] == "2026-06-16T10:00:00Z"
    assert payload["domain"] == "code"  # default vertical


def test_public_rollup_payload_carries_real_totals(telemetry_env: Path) -> None:
    """tokens_processed/calls_made/time_spent_seconds pair with the existing
    tokens_saved/calls_avoided/time_saved_seconds deltas -- default to 0
    (omitted by older callers) rather than being dropped from the payload."""
    payload = _payload(
        session_id="session-totals",
        saved_usd=0.5,
        tokens_saved=100,
        calls_avoided=1,
        turn_count=2,
        source="claude",
        occurred_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
        tokens_processed=5_000,
        calls_made=12,
        time_spent_seconds=321.5,
    )
    assert payload is not None
    assert payload["tokens_processed"] == 5_000
    assert payload["calls_made"] == 12
    assert payload["time_spent_seconds"] == 321.5


def test_public_rollup_payload_carries_turns_avoided(telemetry_env: Path) -> None:
    """Whole avoided turns (turn_cut credit) ride their own field, distinct
    from the raw turn_count."""
    payload = _payload(
        session_id="session-turns",
        saved_usd=0.5,
        tokens_saved=100,
        calls_avoided=9,
        turn_count=20,
        turns_avoided=7,
        source="claude",
        occurred_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
    )
    assert payload is not None
    assert payload["turn_count"] == 20
    assert payload["turns_avoided"] == 7

    default_payload = _payload(
        session_id="session-no-totals",
        saved_usd=0.5,
        tokens_saved=100,
        calls_avoided=1,
        turn_count=2,
        source="claude",
        occurred_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
    )
    assert default_payload is not None
    assert default_payload["tokens_processed"] == 0
    assert default_payload["calls_made"] == 0
    assert default_payload["time_spent_seconds"] == 0


def test_public_rollup_payload_tags_custom_domain(telemetry_env: Path) -> None:
    payload = _payload(
        session_id="session-docs",
        saved_usd=0.5,
        tokens_saved=100,
        calls_avoided=1,
        turn_count=2,
        source="claude",
        occurred_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC),
        domain="docs",
    )
    assert payload is not None
    assert payload["domain"] == "docs"


def test_public_rollup_is_gated_by_remote_opt_in(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public savings rollup is remote telemetry: it must NOT leave the
    machine unless the user opted into remote telemetry, and it must fire once
    they have."""
    calls: list[dict[str, Any]] = []

    def fake_post(endpoint: str, payload: dict[str, Any], *, timeout_s: float, attempts: int = 1) -> bool:
        calls.append(payload)
        return True

    monkeypatch.setattr("lemoncrow.core.service.telemetry.public_rollup._post_json", fake_post)
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", "https://example.test/rollup")

    # Remote telemetry OFF (fixture sets LEMONCROW_TELEMETRY=0): must not fire.
    assert (
        publish_public_savings_rollup(
            session_id="session-off",
            saved_usd=0.5,
            tokens_saved=500,
            calls_avoided=1,
            turn_count=4,
            source="claude",
        )
        is False
    )
    assert calls == []

    # Opt in, then it fires exactly once with a hashed session key.
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)
    save_telemetry_config(remote_enabled=True)
    result = publish_public_savings_rollup(
        session_id="session-on",
        saved_usd=0.5,
        tokens_saved=500,
        calls_avoided=1,
        turn_count=4,
        source="claude",
    )
    assert result is True
    assert len(calls) == 1
    anon_id = get_anon_id()
    assert "session_id" not in calls[0]
    assert calls[0]["session_key"] == hashlib.sha256(f"{anon_id}:session-on".encode()).hexdigest()


def test_public_rollup_posts_correct_payload(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def fake_post(endpoint: str, payload: dict[str, Any], *, timeout_s: float, attempts: int = 1) -> bool:
        calls.append((endpoint, payload, timeout_s))
        return True

    monkeypatch.setattr("lemoncrow.core.service.telemetry.public_rollup._post_json", fake_post)
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", "https://example.test/rollup")
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_TIMEOUT_MS", "250")
    # Opt into remote telemetry so the rollup network path is exercised.
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)
    save_telemetry_config(remote_enabled=True)

    assert publish_public_savings_rollup(
        session_id="session-1",
        saved_usd=1.25,
        tokens_saved=1000,
        calls_avoided=4,
        turn_count=7,
        source="claude",
    )
    assert len(calls) == 1
    endpoint, payload, timeout_s = calls[0]
    assert endpoint == "https://example.test/rollup"
    anon_id = get_anon_id()
    assert "session_id" not in payload
    assert payload["session_key"] == hashlib.sha256(f"{anon_id}:session-1".encode()).hexdigest()
    assert payload["saved_usd"] == 1.25
    assert payload["tokens_saved"] == 1000
    assert payload["calls_avoided"] == 4
    assert payload["source"] == "claude"
    assert timeout_s == 0.25


def test_scrubber_removes_realistic_pii_fixture() -> None:
    samples: list[str] = []
    for i in range(25):
        samples.extend(
            [
                f"email user{i}@example.com in payload",
                f"path /home/user{i}/secret/project/file.py should scrub",
                f"repo https://github.com/acme/private-{i}.git should scrub",
                f"token sk-{i:02d}abcdefghijklmnopqrstuvwxyz should scrub",
            ]
        )

    assert len(samples) == 100
    forbidden = re.compile(r"(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|/home/user|github\.com|sk-[A-Za-z0-9])")
    for sample in samples:
        assert not forbidden.search(scrub_string(sample))


def test_remote_export_suppressed_in_tests_but_local_store_records(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Telemetry is mandatory in production, but the pytest guard suppresses
    # remote export so the suite never phones home. Local store still records.
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_export(event: str, props: dict[str, Any]) -> bool:
        calls.append((event, props))
        return True

    monkeypatch.setattr(
        "lemoncrow.core.service.telemetry.exporters.otel.emit_product_log",
        fake_export,
    )
    emit_product("session_end", session_id="s", duration_s_bucket="<10", exit_reason="success")

    assert calls == []
    events = LocalTelemetryStore(telemetry_env).list_events(limit=10)
    assert [event["event"] for event in events] == ["session_end"]


def test_config_round_trip_and_lexical_matcher_never_emits_input_text(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)
    # Remote telemetry is mandatory (always on); only the lexical-frustration
    # flag round-trips through the config now.
    save_telemetry_config(lexical_frustration_enabled=True)
    assert load_telemetry_config().lexical_frustration_enabled is True

    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_emit(event: str, **props: Any) -> None:
        captured.append((event, props))

    monkeypatch.setattr("lemoncrow.core.service.telemetry.emit.emit_product", fake_emit)
    category = match_frustration(
        "No, I said this is broken in /home/me/private/file.py",
        surface="cli_input",
        session_id="session-1",
    )

    assert category == "explicit_negative"
    assert captured == [
        (
            "frustration_signal_lexical",
            {"category": "explicit_negative", "surface": "cli_input", "session_id": "session-1"},
        )
    ]
    assert "broken" not in str(captured)
    assert "/home/me" not in str(captured)


def test_first_run_banner_shows_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(tmp_path / "ack"))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Stream:
        def __init__(self) -> None:
            self.value = ""

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            self.value += text
            return len(text)

        def flush(self) -> None:
            pass

    stream = Stream()
    assert maybe_show_banner(stream) is True
    assert "LemonCrow collects anonymous usage telemetry" in stream.value
    stream.value = ""
    assert maybe_show_banner(stream) is False
    assert stream.value == ""


def test_banner_auto_acknowledges_in_non_tty_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When telemetry is enabled but the stream is not a TTY (e.g. MCP subprocess),
    the banner should not be shown, but the ack should still be written silently
    so the frontend/CLI don't keep showing it."""
    ack_file = tmp_path / "ack"
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(ack_file))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Ensure LEMONCROW_TELEMETRY is not set (telemetry is enabled by default)
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)

    class NonTtyStream:
        def __init__(self) -> None:
            self.value = ""

        def isatty(self) -> bool:
            return False  # not a terminal

        def write(self, text: str) -> int:
            self.value += text
            return len(text)

        def flush(self) -> None:
            pass

    stream = NonTtyStream()
    # Should return False (no banner shown in non-TTY), but ack file should be created
    assert maybe_show_banner(stream) is False
    assert stream.value == "", "no banner text should be written"
    assert ack_file.exists(), "ack file should have been created in non-TTY mode"
    assert ack_file.read_text(encoding="utf-8") == "acknowledged\n"

    # Second call: ack exists, so no banner and still no output
    assert maybe_show_banner(stream) is False
    assert stream.value == ""


def test_emit_product_call_sites_use_allowlisted_props() -> None:
    roots = [
        Path("src/lemoncrow/gateway/adapters"),
        Path("src/lemoncrow/core/runtime"),
        Path("src/lemoncrow/core/service/api.py"),
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))

    failures: list[str] = []
    for file_path in files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_emit_product_call(node):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            event = node.args[0].value
            if not isinstance(event, str):
                continue
            allowed = set(EVENTS[event].props)
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                if keyword.arg not in allowed:
                    failures.append(f"{file_path}:{node.lineno} {event}.{keyword.arg}")
    assert failures == []


def test_telemetry_summary_reports_cache_hit_rate(telemetry_env: Path) -> None:
    emit_product(
        "value_estimate",
        session_id="session-1",
        tokens_saved_estimate=120,
        cache_hits=3,
        total_tool_calls=12,
        cache_hit_rate=0.25,
        blocks_applied=2,
    )
    emit_product(
        "value_estimate",
        session_id="session-2",
        tokens_saved_estimate=80,
        cache_hits=1,
        total_tool_calls=8,
        cache_hit_rate=0.125,
        blocks_applied=1,
    )

    summary = LocalTelemetryStore(telemetry_env).summary()

    assert summary["value_estimate"]["cache_hits"] == 4
    assert summary["value_estimate"]["total_tool_calls"] == 20
    assert summary["value_estimate"]["cache_hit_rate"] == 0.2


def _is_emit_product_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "emit_product"


def test_async_emit_stays_off_hot_path_and_persists_after_flush(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async (default, non-pytest) mode: emit enqueues without touching SQLite on
    the caller; the background worker drains it and flush() makes it observable."""
    from lemoncrow.core.service.telemetry.emit import flush_product_telemetry

    # Force the async path (pytest normally forces synchronous emission).
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LEMONCROW_TELEMETRY_SYNC", "0")

    emit_product(
        "cli_command_invoked",
        command_name="context",
        session_id="00000000-0000-4000-8000-000000000000",
    )
    flush_product_telemetry(timeout=5.0)

    events = LocalTelemetryStore(telemetry_env).list_events(limit=10)
    assert [event["event"] for event in events] == ["cli_command_invoked"]


# ---------------------------------------------------------------------------
# Daily public rollup: per-day posts, durable retry schedule, no double count
# ---------------------------------------------------------------------------


def _enable_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", "https://example.test/rollup")
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)
    save_telemetry_config(remote_enabled=True)


def test_daily_flush_stamps_each_day_and_reports_its_own_real_totals(
    telemetry_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One post per day, stamped with THAT day (not the flush time), carrying
    that day's carry_tokens and its own real-usage totals."""
    from lemoncrow.core.capabilities import savings_summary as ss

    posts: list[tuple[dict[str, Any], float, int]] = []

    def fake_post(endpoint: str, payload: dict[str, Any], *, timeout_s: float, attempts: int = 1) -> bool:
        posts.append((payload, timeout_s, attempts))
        return True

    monkeypatch.setattr(public_rollup_mod, "_post_json", fake_post)
    _enable_remote(monkeypatch)

    by_day = {
        "2026-07-30": {
            "saved_usd": 299.82,
            "tokens_saved": 61_623_274,
            "calls_avoided": 1433,
            "turn_count": 179,
            "turns_avoided": 1158,
            "est_cost_usd": 466.05,
            "carry_usd": 20.18,
            "carry_tokens": 4242,
            "output_saved_tokens": 23_318,
            "output_saved_usd": 1.2,
        },
        "2026-07-31": {
            "saved_usd": 156.92,
            "tokens_saved": 37_535_446,
            "calls_avoided": 1417,
            "turn_count": 664,
            "turns_avoided": 224,
            "est_cost_usd": 155.93,
            "carry_usd": 71.44,
            "carry_tokens": 11,
            "output_saved_tokens": 8718,
            "output_saved_usd": 0.46,
        },
    }
    usage_by_day = {
        "2026-07-30": {"tokens_processed": 900_000_000, "calls_made": 4000, "time_spent_seconds": 1_000_000.0},
        "2026-07-31": {"tokens_processed": 412_202_178, "calls_made": 2908, "time_spent_seconds": 871_896.7},
    }
    monkeypatch.setattr(ss, "aggregate_savings_by_day", lambda root, *, since_day, today: by_day)
    monkeypatch.setattr(ss, "aggregate_usage_totals_by_day", lambda root, *, since_day, today: usage_by_day)

    result, checkpoint = flush_daily_public_rollup(tmp_path, checkpoint_day="2026-07-29")

    assert result == {"flushed": True, "through_day": "2026-07-31"}
    assert checkpoint == "2026-07-31"
    assert len(posts) == 2
    first, second = posts[0][0], posts[1][0]
    assert first["occurred_at"] == "2026-07-30T12:00:00Z"
    assert second["occurred_at"] == "2026-07-31T12:00:00Z"
    # carry_tokens used to be dropped on this path (always 0).
    assert first["carry_tokens"] == 4242
    assert second["carry_tokens"] == 11
    # Real usage is per-day, not the whole window piled onto the newest day.
    assert first["tokens_processed"] == 900_000_000
    assert second["tokens_processed"] == 412_202_178
    assert second["calls_made"] == 2908
    # Background flush gets a realistic budget and real retries.
    assert posts[0][1] == public_rollup_mod.FLUSH_TIMEOUT_SECONDS
    assert posts[0][2] == public_rollup_mod.FLUSH_ATTEMPTS


def test_maybe_flush_retries_in_minutes_after_failure_and_daily_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped POST must not cost a whole day: reschedule in 15 min, doubling
    to a ceiling, and reset to the 24 h cadence once a flush lands."""
    from datetime import timedelta

    root = tmp_path / ".lemoncrow"
    root.mkdir()
    outcome: dict[str, Any] = {"value": ({"flushed": False, "reason": "post_failed", "stuck_day": "2026-07-30"}, None)}

    def fake_flush(target: Any, *, checkpoint_day: str | None) -> tuple[dict[str, Any], str | None]:
        result, new_checkpoint = outcome["value"]
        return result, (new_checkpoint if new_checkpoint is not None else checkpoint_day)

    monkeypatch.setattr(public_rollup_mod, "flush_daily_public_rollup", fake_flush)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    first = maybe_flush_public_rollup(root, legacy_checkpoint_day="2026-07-29", now=now)
    assert first["reason"] == "post_failed"
    assert first["consecutive_failures"] == 1
    # Legacy servicectl checkpoint is migrated, not re-baselined.
    assert first["checkpoint_day"] == "2026-07-29"
    assert read_public_rollup_state(root)["next_attempt_at"] == (now + timedelta(seconds=900)).isoformat()

    # Not due yet: no attempt, schedule untouched.
    skipped = maybe_flush_public_rollup(root, now=now + timedelta(seconds=300))
    assert skipped["reason"] == "not_due"

    second = maybe_flush_public_rollup(root, now=now + timedelta(seconds=900))
    assert second["consecutive_failures"] == 2
    assert (
        read_public_rollup_state(root)["next_attempt_at"]
        == (now + timedelta(seconds=900) + timedelta(seconds=1800)).isoformat()
    )

    outcome["value"] = ({"flushed": True, "through_day": "2026-08-01"}, "2026-08-01")
    done = maybe_flush_public_rollup(root, now=now + timedelta(seconds=3000))
    assert done["checkpoint_day"] == "2026-08-01"
    assert done["consecutive_failures"] == 0
    state = read_public_rollup_state(root)
    assert state["checkpoint_day"] == "2026-08-01"
    assert (
        state["next_attempt_at"]
        == (now + timedelta(seconds=3000 + public_rollup_mod.FLUSH_INTERVAL_SECONDS)).isoformat()
    )


def test_maybe_flush_survives_a_raising_flush_and_reschedules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()

    def boom(target: Any, *, checkpoint_day: str | None) -> tuple[dict[str, Any], str | None]:
        raise RuntimeError("transcript store exploded")

    monkeypatch.setattr(public_rollup_mod, "flush_daily_public_rollup", boom)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    result = maybe_flush_public_rollup(root, legacy_checkpoint_day="2026-07-29", now=now)

    assert result["reason"] == "error"
    assert result["consecutive_failures"] == 1
    # Checkpoint preserved, lock released, retry scheduled soon.
    assert result["checkpoint_day"] == "2026-07-29"
    assert not (root / "telemetry" / "public_rollup.lock").exists()


def test_post_json_retries_transient_failures_but_not_a_rejected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.setattr(public_rollup_mod.time, "sleep", lambda _seconds: None)

    class _Response:
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    attempts: list[int] = []

    def flaky(request: Any, timeout: float) -> Any:
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("connection reset")
        return _Response()

    monkeypatch.setattr(public_rollup_mod.urllib.request, "urlopen", flaky)
    assert public_rollup_mod._post_json("https://example.test/rollup", {}, timeout_s=1.0, attempts=3) is True
    assert len(attempts) == 3

    rejected: list[int] = []

    def bad_request(request: Any, timeout: float) -> Any:
        rejected.append(1)
        raise urllib.error.HTTPError("https://example.test/rollup", 400, "Bad Request", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(public_rollup_mod.urllib.request, "urlopen", bad_request)
    assert public_rollup_mod._post_json("https://example.test/rollup", {}, timeout_s=1.0, attempts=3) is False
    # 4xx is a verdict on the bytes; resending them is pointless.
    assert len(rejected) == 1


def test_codex_stop_hook_does_not_publish_its_own_public_rollup() -> None:
    """The daily flush already folds codex sessions into the day's aggregate;
    a live per-session push from the codex Stop hook double counted them."""
    source = (Path(__file__).resolve().parents[2] / "src/lemoncrow/core/capabilities/plugin_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "publish_public_savings_rollup(" not in source
