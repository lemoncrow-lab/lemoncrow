from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from atelier.core.service.telemetry import emit_product
from atelier.core.service.telemetry.banner import maybe_show_banner
from atelier.core.service.telemetry.config import load_telemetry_config, save_telemetry_config
from atelier.core.service.telemetry.frustration import match_frustration
from atelier.core.service.telemetry.local_store import LocalTelemetryStore
from atelier.core.service.telemetry.schema import EVENTS
from atelier.core.service.telemetry.scrubber import scrub_string


@pytest.fixture()
def telemetry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "telemetry.db"
    monkeypatch.setenv("ATELIER_TELEMETRY_DB", str(db_path))
    monkeypatch.setenv("ATELIER_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("ATELIER_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("ATELIER_TELEMETRY_ACK", str(tmp_path / "telemetry_ack"))
    monkeypatch.setenv("ATELIER_TELEMETRY", "0")
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


def test_env_opt_out_is_immediate_and_remote_export_is_not_called(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_export(event: str, props: dict[str, Any]) -> bool:
        calls.append((event, props))
        return True

    monkeypatch.setattr(
        "atelier.core.service.telemetry.exporters.otel.emit_product_log",
        fake_export,
    )
    monkeypatch.setenv("ATELIER_TELEMETRY", "0")
    emit_product("session_end", session_id="s", duration_s_bucket="<10", exit_reason="success")

    assert calls == []
    events = LocalTelemetryStore(telemetry_env).list_events(limit=10)
    assert [event["event"] for event in events] == ["session_end"]


def test_config_round_trip_and_lexical_matcher_never_emits_input_text(
    telemetry_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATELIER_TELEMETRY", raising=False)
    save_telemetry_config(remote_enabled=False, lexical_frustration_enabled=True)
    assert load_telemetry_config().remote_enabled is False

    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_emit(event: str, **props: Any) -> None:
        captured.append((event, props))

    monkeypatch.setattr("atelier.core.service.telemetry.emit.emit_product", fake_emit)
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
    monkeypatch.setenv("ATELIER_TELEMETRY_ACK", str(tmp_path / "ack"))
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
    assert "Atelier collects anonymous usage telemetry" in stream.value
    stream.value = ""
    assert maybe_show_banner(stream) is False
    assert stream.value == ""


def test_banner_auto_acknowledges_when_telemetry_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ATELIER_TELEMETRY=0, the banner is never shown and is auto-acknowledged."""
    ack_file = tmp_path / "ack"
    monkeypatch.setenv("ATELIER_TELEMETRY_ACK", str(ack_file))
    monkeypatch.setenv("ATELIER_TELEMETRY", "0")
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
    # With ATELIER_TELEMETRY=0, should return False, not write to stream,
    # AND create the ack file so subsequent calls are no-ops.
    assert maybe_show_banner(stream) is False
    assert stream.value == ""
    assert ack_file.exists(), "ack file should have been created"
    assert ack_file.read_text(encoding="utf-8") == "acknowledged\n"

    # Second call: still no banner because ack exists
    assert maybe_show_banner(stream) is False
    assert stream.value == ""


def test_banner_auto_acknowledges_in_non_tty_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When telemetry is enabled but the stream is not a TTY (e.g. MCP subprocess),
    the banner should not be shown, but the ack should still be written silently
    so the frontend/CLI don't keep showing it."""
    ack_file = tmp_path / "ack"
    monkeypatch.setenv("ATELIER_TELEMETRY_ACK", str(ack_file))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Ensure ATELIER_TELEMETRY is not set (telemetry is enabled by default)
    monkeypatch.delenv("ATELIER_TELEMETRY", raising=False)

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


def test_banner_auto_acknowledges_with_false_env_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify all false-value variants (0, false, off, no) trigger auto-ack."""
    ack_file = tmp_path / "ack"
    monkeypatch.setenv("ATELIER_TELEMETRY_ACK", str(ack_file))
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

    for variant in ("0", "false", "off", "no"):
        ack_file.unlink(missing_ok=True)
        monkeypatch.setenv("ATELIER_TELEMETRY", variant)

        stream = Stream()
        assert maybe_show_banner(stream) is False
        assert stream.value == ""
        assert ack_file.exists(), f"ack should be created with ATELIER_TELEMETRY={variant}"


def test_emit_product_call_sites_use_allowlisted_props() -> None:
    roots = [
        Path("src/atelier/gateway/adapters"),
        Path("src/atelier/core/runtime"),
        Path("src/atelier/core/service/api.py"),
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


def _is_emit_product_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "emit_product"
