from __future__ import annotations

from typing import Any

from atelier.infra.internal_llm import ollama_client


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


def test_chat_json_schema_uses_ollama_format_parameter(monkeypatch: Any) -> None:
    fake = _FakeOllama()
    monkeypatch.setattr(ollama_client, "_ollama_module", lambda: fake)

    payload = ollama_client.chat(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert payload == {"procedural": True}
    assert fake.calls[0]["format"] == "json"


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
