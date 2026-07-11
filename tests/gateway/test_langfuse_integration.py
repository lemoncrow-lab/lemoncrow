from __future__ import annotations

import pytest

pytest.importorskip("langfuse")

from lemoncrow.gateway.integrations import langfuse as lf


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.flushed = False
        self.was_shutdown = False

    def create_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.was_shutdown = True


def _fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(lf, "_client", lambda: fake)
    monkeypatch.setattr(lf, "_CLIENT", fake)
    return fake


def test_emit_tool_call_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_LANGFUSE_ENABLED", raising=False)
    fake = _fake(monkeypatch)
    lf.emit_tool_call(tool="read", args={}, duration_ms=1, response_size=2, status="ok")
    assert fake.events == []


def test_emit_tool_call_records_and_scrubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LANGFUSE_ENABLED", "true")
    fake = _fake(monkeypatch)
    lf.emit_tool_call(
        tool="grep",
        args={"content_regex": "x" * 400, "path": "src"},
        duration_ms=5,
        response_size=99,
        status="ok",
        session_id="s1",
    )
    assert len(fake.events) == 1
    ev = fake.events[0]
    assert ev["name"] == "mcp.grep"
    assert str(ev["input"]["content_regex"]).endswith("chars>")  # scrubbed
    assert ev["input"]["path"] == "src"
    assert ev["output"] == {"status": "ok", "response_size_bytes": 99}
    assert ev["level"] == "DEFAULT"


def test_emit_tool_call_redacts_nested_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LANGFUSE_ENABLED", "true")
    fake = _fake(monkeypatch)
    lf.emit_tool_call(
        tool="sql",
        args={
            "sql": "SELECT 1",
            "connection_string": "postgresql://u:p@host/db",
            "api_key": "sk-short",
            "nested": {"DSN": "x", "AuthToken": "y", "ok": 1},
            "items": ["plain", {"Authorization": "Bearer z", "password": "p"}],
        },
        duration_ms=5,
        response_size=1,
        status="ok",
    )
    sent = fake.events[0]["input"]
    # Secret-bearing keys are redacted at every nesting level, case-insensitively,
    # regardless of value length.
    assert sent["connection_string"] == "<redacted>"
    assert sent["api_key"] == "<redacted>"
    assert sent["nested"]["DSN"] == "<redacted>"
    assert sent["nested"]["AuthToken"] == "<redacted>"
    assert sent["items"][1]["Authorization"] == "<redacted>"
    assert sent["items"][1]["password"] == "<redacted>"
    # Non-secret telemetry is preserved intact.
    assert sent["sql"] == "SELECT 1"
    assert sent["nested"]["ok"] == 1
    assert sent["items"][0] == "plain"


def test_emit_tool_call_error_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LANGFUSE_ENABLED", "true")
    fake = _fake(monkeypatch)
    lf.emit_tool_call(tool="edit", args={}, duration_ms=5, response_size=0, status="error", error="Boom")
    ev = fake.events[0]
    assert ev["level"] == "ERROR"
    assert ev["output"] == {"status": "error", "error": "Boom"}
    assert ev["status_message"] == "Boom"


def test_emit_trace_records_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LANGFUSE_ENABLED", "true")
    fake = _fake(monkeypatch)
    lf.emit_trace(
        {"status": "success", "domain": "code", "agent": "a", "session_id": "s", "task": "t", "output_summary": "o"}
    )
    assert len(fake.events) == 1
    assert fake.events[0]["name"] == "lemoncrow.code"


def test_shutdown_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    lf.shutdown()
    assert fake.flushed
    assert fake.was_shutdown
