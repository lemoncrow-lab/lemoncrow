"""Tests for embedder factories and backend-specific helpers."""

from __future__ import annotations

import json
from urllib.request import Request

import pytest

from lemoncrow.infra.embeddings.base import Embedder
from lemoncrow.infra.embeddings.factory import make_code_embedder, make_embedder
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder
from lemoncrow.infra.embeddings.ollama_embedder import OllamaEmbedder
from lemoncrow.infra.embeddings.openai_embedder import OpenAIEmbedder


def test_make_embedder_returns_null_in_stripped_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit pins, defaults to NullEmbedder (FTS-only; the local feature-hashing embedder was removed)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LEMONCROW_LETTA_URL", raising=False)
    monkeypatch.delenv("LEMONCROW_EMBEDDER", raising=False)
    monkeypatch.delenv("LEMONCROW_MEMORY_BACKEND", raising=False)

    e = make_embedder()
    assert isinstance(e, NullEmbedder)
    assert isinstance(e, Embedder)


def test_make_embedder_null_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    e = make_embedder(pin="null")
    assert isinstance(e, NullEmbedder)


def test_make_embedder_openai_raises_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning to openai without OPENAI_API_KEY must raise."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises((ValueError, Exception)):
        make_embedder(pin="openai")


def test_make_embedder_env_pin_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_EMBEDDER", "null")
    e = make_embedder()
    assert isinstance(e, NullEmbedder)
    monkeypatch.delenv("LEMONCROW_EMBEDDER")


def test_make_embedder_bad_pin_raises() -> None:
    with pytest.raises(ValueError, match="Unknown embedder pin"):
        make_embedder(pin="bogus")


def test_openai_embedder_init_fails_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIEmbedder()


def test_all_embedders_satisfy_protocol() -> None:
    from lemoncrow.infra.embeddings.letta_embedder import LettaEmbedder

    for cls in (NullEmbedder, LettaEmbedder):
        instance = cls()
        assert isinstance(instance, Embedder), f"{cls.__name__} does not satisfy Embedder protocol"


def test_null_embedder_dim_and_name() -> None:
    e = NullEmbedder()
    assert e.dim == 0
    assert e.name == "null"


def test_make_code_embedder_defaults_to_null_without_bge_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pins or BGE extras installed, defaults to NullEmbedder (FTS-only)."""
    from lemoncrow.infra.embeddings.bge import BgeEmbedder

    make_code_embedder.cache_clear()
    monkeypatch.delenv("LEMONCROW_CODE_EMBEDDER", raising=False)
    monkeypatch.delenv("LEMONCROW_EMBEDDER", raising=False)
    monkeypatch.delenv("LEMONCROW_OFFLINE", raising=False)
    monkeypatch.setattr(OllamaEmbedder, "is_available", lambda self: True)
    monkeypatch.setattr(BgeEmbedder, "is_available", classmethod(lambda cls: False))

    embedder = make_code_embedder()

    assert isinstance(embedder, NullEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_no_pin_stays_null_even_with_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit pin, semantic search stays OFF (FTS-only) even when the torch extras are installed."""
    from lemoncrow.infra.embeddings.bge import BgeEmbedder

    make_code_embedder.cache_clear()
    monkeypatch.delenv("LEMONCROW_CODE_EMBEDDER", raising=False)
    monkeypatch.delenv("LEMONCROW_EMBEDDER", raising=False)
    monkeypatch.setattr(BgeEmbedder, "is_available", classmethod(lambda cls: True))

    embedder = make_code_embedder()

    assert isinstance(embedder, NullEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_bge_requires_explicit_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """BGE is only selected via an explicit pin (LEMONCROW_CODE_EMBEDDER=bge)."""
    from lemoncrow.infra.embeddings.bge import BgeEmbedder

    make_code_embedder.cache_clear()

    embedder = make_code_embedder(pin="bge")

    assert isinstance(embedder, BgeEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_null_pin_skips_auto_select(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit null pin must NOT trigger BGE auto-select even when the extras are installed."""
    from lemoncrow.infra.embeddings.bge import BgeEmbedder

    make_code_embedder.cache_clear()
    monkeypatch.setattr(BgeEmbedder, "is_available", classmethod(lambda cls: True))

    embedder = make_code_embedder(pin="null")

    assert isinstance(embedder, NullEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_falls_back_to_null_when_pinned_ollama_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_code_embedder.cache_clear()
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "ollama")
    monkeypatch.delenv("LEMONCROW_OFFLINE", raising=False)
    monkeypatch.setattr(OllamaEmbedder, "is_available", lambda self: False)

    embedder = make_code_embedder()

    assert isinstance(embedder, NullEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_uses_ollama_when_pinned_and_available(monkeypatch: pytest.MonkeyPatch) -> None:
    make_code_embedder.cache_clear()
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "ollama")
    monkeypatch.delenv("LEMONCROW_OFFLINE", raising=False)
    monkeypatch.setattr(OllamaEmbedder, "is_available", lambda self: True)

    embedder = make_code_embedder()

    assert isinstance(embedder, OllamaEmbedder)
    make_code_embedder.cache_clear()


def test_make_code_embedder_revalidates_ollama_after_mid_session_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached Ollama embedder must not be returned once Ollama goes down mid-session."""
    make_code_embedder.cache_clear()
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "ollama")
    monkeypatch.delenv("LEMONCROW_OFFLINE", raising=False)

    availability = {"up": True}
    monkeypatch.setattr(OllamaEmbedder, "is_available", lambda self: availability["up"])

    first = make_code_embedder()
    assert isinstance(first, OllamaEmbedder)

    availability["up"] = False
    second = make_code_embedder()
    assert isinstance(second, NullEmbedder)

    make_code_embedder.cache_clear()


def test_ollama_embedder_query_prefixes_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payload: dict[str, object] = {}

    def fake_request(self: OllamaEmbedder, request: Request) -> dict[str, object]:
        del self
        data = request.data
        assert isinstance(data, bytes)
        captured_payload.update(json.loads(data.decode("utf-8")))
        return {"embeddings": [[3.0, 4.0]]}

    monkeypatch.setattr(OllamaEmbedder, "_request_json", fake_request)
    embedder = OllamaEmbedder(model="nomic-embed-text")

    vector = embedder.embed_queries(["match this symbol"])[0]

    assert captured_payload["input"] == ["search_query: match this symbol"]
    assert vector == pytest.approx([0.6, 0.8])


def test_ollama_embedder_document_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload: dict[str, object] = {}

    def fake_request(self: OllamaEmbedder, request: Request) -> dict[str, object]:
        del self
        data = request.data
        assert isinstance(data, bytes)
        captured_payload.update(json.loads(data.decode("utf-8")))
        return {"embeddings": [[1.0, 0.0]]}

    monkeypatch.setattr(OllamaEmbedder, "_request_json", fake_request)
    embedder = OllamaEmbedder(model="nomic-embed-text")

    _ = embedder.embed_documents(["def issue_token(): ..."])

    assert captured_payload["input"] == ["search_document: def issue_token(): ..."]


def test_ollama_embedder_is_unavailable_when_model_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        OllamaEmbedder,
        "_request_json",
        lambda self, request: {"models": [{"name": "other-model:latest"}]},
    )

    embedder = OllamaEmbedder(model="nomic-embed-text")

    assert embedder.is_available() is False
