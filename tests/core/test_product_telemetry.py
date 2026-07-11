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
from lemoncrow.core.service.telemetry.banner import maybe_show_banner
from lemoncrow.core.service.telemetry.config import load_telemetry_config, save_telemetry_config
from lemoncrow.core.service.telemetry.frustration import match_frustration
from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore
from lemoncrow.core.service.telemetry.public_rollup import _payload, publish_public_savings_rollup
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
    assert payload["occurred_at"] == "2026-06-16T10:00:00Z"
    assert payload["domain"] == "code"  # default vertical


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


def test_public_rollup_always_fires_regardless_of_product_telemetry_setting(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public rollup fires unconditionally — no opt-out path."""
    calls: list[dict[str, Any]] = []

    def fake_post(endpoint: str, payload: dict[str, Any], *, timeout_s: float) -> bool:
        calls.append(payload)
        return True

    monkeypatch.setattr("lemoncrow.core.service.telemetry.public_rollup._post_json", fake_post)
    monkeypatch.setenv("LEMONCROW_TELEMETRY", "0")  # product telemetry off — must not affect public rollup
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", "https://example.test/rollup")

    result = publish_public_savings_rollup(
        session_id="session-always",
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
    assert calls[0]["session_key"] == hashlib.sha256(f"{anon_id}:session-always".encode()).hexdigest()


def test_public_rollup_posts_correct_payload(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def fake_post(endpoint: str, payload: dict[str, Any], *, timeout_s: float) -> bool:
        calls.append((endpoint, payload, timeout_s))
        return True

    monkeypatch.setattr("lemoncrow.core.service.telemetry.public_rollup._post_json", fake_post)
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", "https://example.test/rollup")
    monkeypatch.setenv("LEMONCROW_PUBLIC_TELEMETRY_TIMEOUT_MS", "250")

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
