from __future__ import annotations

import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lemoncrow.infra.embeddings.base import Embedder
from lemoncrow.infra.internal_llm.exceptions import OllamaUnavailable

DEFAULT_CODE_EMBED_MODEL = "nomic-embed-text"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
_QUERY_PREFIX = "search_query: "
_DOCUMENT_PREFIX = "search_document: "
_CODE_QUERY_PREFIX = "Represent this query for retrieving relevant code: "
_DEFAULT_TIMEOUT_SECONDS = 120.0


def _resolve_host(host: str | None = None) -> str:
    candidate = (host or os.getenv("OLLAMA_HOST") or _DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    if candidate.endswith("/api"):
        candidate = candidate[: -len("/api")]
    return candidate or _DEFAULT_OLLAMA_HOST


def _resolve_dim(model: str) -> int:
    normalized = model.strip().lower()
    if normalized.startswith("nomic-embed-text"):
        return 768
    if "nomic-embed-code" in normalized:
        return 3584
    return 768


def _is_code_model(model: str) -> bool:
    return "embed-code" in model.lower()


def _model_name_matches(candidate: str, requested: str) -> bool:
    normalized_candidate = candidate.strip().lower()
    normalized_requested = requested.strip().lower()
    if not normalized_candidate or not normalized_requested:
        return False
    if normalized_candidate == normalized_requested:
        return True
    candidate_base = normalized_candidate.split(":", 1)[0]
    requested_base = normalized_requested.split(":", 1)[0]
    return candidate_base == normalized_requested or candidate_base == requested_base


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def _resolve_timeout_seconds(timeout_seconds: float | None = None) -> float:
    if timeout_seconds is not None:
        return timeout_seconds
    configured = os.getenv("LEMONCROW_OLLAMA_EMBED_TIMEOUT_SECONDS", "").strip()
    if not configured:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(configured)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


class OllamaEmbedder(Embedder):
    def __init__(
        self,
        model: str = DEFAULT_CODE_EMBED_MODEL,
        *,
        host: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model.strip() or DEFAULT_CODE_EMBED_MODEL
        self.host = _resolve_host(host)
        self.dim = _resolve_dim(self.model)
        self.name = f"ollama:{self.model}"
        self._timeout_seconds = _resolve_timeout_seconds(timeout_seconds)

    def is_available(self) -> bool:
        request = Request(f"{self.host}/api/tags", method="GET")
        try:
            payload = self._request_json(request)
        except OllamaUnavailable:
            return False
        models = payload.get("models")
        if not isinstance(models, list):
            return False
        for model in models:
            if not isinstance(model, dict):
                continue
            name = model.get("name")
            if isinstance(name, str) and _model_name_matches(name, self.model):
                return True
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed_role(texts, query=True)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed_role(texts, query=False)

    def _embed_role(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self.model,
            "input": [self._prefix(text, query=query) for text in texts],
        }
        request = Request(
            f"{self.host}/api/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        data = self._request_json(request)
        embeddings = data.get("embeddings")
        if embeddings is None and "embedding" in data:
            embeddings = [data["embedding"]]
        if not isinstance(embeddings, list):
            raise OllamaUnavailable("Unexpected Ollama embedding response")
        vectors: list[list[float]] = []
        for raw_vector in embeddings:
            if not isinstance(raw_vector, list):
                raise OllamaUnavailable("Unexpected Ollama embedding payload")
            vector = [float(value) for value in raw_vector]
            if vector and len(vector) != self.dim:
                self.dim = len(vector)
            vectors.append(_normalize(vector))
        return vectors

    def _prefix(self, text: str, *, query: bool) -> str:
        if _is_code_model(self.model):
            prefix = _CODE_QUERY_PREFIX if query else ""
        else:
            prefix = _QUERY_PREFIX if query else _DOCUMENT_PREFIX
        return f"{prefix}{text}"

    def _request_json(self, request: Request) -> dict[str, object]:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            message = f"Ollama request failed ({exc.code})"
            if detail:
                message = f"{message}: {detail}"
            raise OllamaUnavailable(message) from exc
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise OllamaUnavailable("Ollama is unavailable") from exc
        if not isinstance(payload, dict):
            raise OllamaUnavailable("Unexpected Ollama response payload")
        return payload
