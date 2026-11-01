from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.infra.internal_llm import ollama_client


class _FakeOllama:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"message": {"content": '{"procedural": true}'}}


class _MessageObject:
    content = '{"procedural": true}'


class _ResponseObject:
    message = _MessageObject()


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"procedural": {"type": "boolean"}},
    "required": ["procedural"],
}


def test_chat_json_schema_is_sent_as_the_ollama_format(monkeypatch: Any) -> None:
    """Ollama constrains the answer to a JSON schema passed as ``format``; bare ``"json"`` leaves the keys to chance."""

    fake = _FakeOllama()
    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: fake)

    payload = ollama_client.chat([{"role": "user", "content": "Return JSON"}], json_schema=_SCHEMA)

    assert payload == {"procedural": True}
    assert fake.calls[0]["format"] == _SCHEMA


def test_chat_stream_json_schema_is_sent_as_the_ollama_format(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class StreamingOllama:
        def chat(self, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(kwargs)
            return [{"message": {"content": '{"procedural": '}}, {"message": {"content": "true}"}}]

    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: StreamingOllama())

    payload = ollama_client.chat_stream(
        [{"role": "user", "content": "Return JSON"}], on_delta=lambda _: None, json_schema=_SCHEMA
    )

    assert payload == {"procedural": True}
    assert calls[0]["format"] == _SCHEMA


def test_chat_json_schema_falls_back_to_legacy_options(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class LegacyOllama:
        def chat(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if "format" in kwargs:
                raise TypeError("unexpected keyword")
            return {"message": {"content": '{"procedural": false}'}}

    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: LegacyOllama())

    payload = ollama_client.chat(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert payload == {"procedural": False}
    assert calls[1]["options"]["format"] == "json"


def test_chat_reads_typed_ollama_message_content(monkeypatch: Any) -> None:
    class TypedOllama:
        def chat(self, **kwargs: Any) -> _ResponseObject:
            return _ResponseObject()

    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: TypedOllama())

    payload = ollama_client.chat(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert payload == {"procedural": True}


def test_an_answer_cut_off_while_reasoning_fails_and_says_why(monkeypatch: Any) -> None:
    """A thinking model can spend the whole context window reasoning: no content is a failure naming that cause."""

    class ReasonedOut:
        def chat(self, **kwargs: Any) -> dict[str, Any]:
            return {"done_reason": "length", "message": {"content": "", "thinking": "Let me check the callers..."}}

    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: ReasonedOut())

    with pytest.raises(ollama_client.OllamaUnavailable, match="done_reason=length"):
        ollama_client.chat([{"role": "user", "content": "Return JSON"}], json_schema=_SCHEMA)
