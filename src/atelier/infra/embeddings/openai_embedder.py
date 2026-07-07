"""OpenAIEmbedder — uses openai package if available, else raw httpx."""

from __future__ import annotations

import os

_DEFAULT_MODEL = "text-embedding-3-small"
_DEFAULT_DIM = 1536


class OpenAIEmbedder:
    """Embeds texts via the OpenAI embeddings API.

    Uses the ``openai`` package if importable; falls back to raw ``httpx``.
    Reads ``OPENAI_API_KEY`` from environment.
    """

    dim: int
    name: str

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        *,
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self.dim = _DEFAULT_DIM
        self.name = f"openai:{model}"
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is not set; cannot use OpenAIEmbedder")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return self._embed_with_sdk(texts)
        except ImportError:
            return self._embed_with_httpx(texts)

    def _embed_with_sdk(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key)
        response = client.embeddings.create(input=texts, model=self._model)
        return [item.embedding for item in response.data]

    def _embed_with_httpx(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"input": texts, "model": self._model},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]


__all__ = ["OpenAIEmbedder"]
