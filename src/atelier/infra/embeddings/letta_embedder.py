"""LettaEmbedder — delegates to the Letta sidecar embedding endpoint."""

from __future__ import annotations

from typing import Any


class LettaEmbedder:
    """Embeds texts by calling the Letta sidecar's embedding endpoint.

    Falls back to raising ``RuntimeError`` if the sidecar is unreachable;
    callers should catch and degrade to a different embedder.
    """

    dim: int
    name: str

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client
        self.dim = 1536  # default; updated after first embed if sidecar reports actual dim
        self.name = "letta:sidecar"

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from atelier.infra.memory_bridges.letta_adapter import LettaAdapter

        if not LettaAdapter.is_available():
            raise RuntimeError("Letta sidecar not available; set ATELIER_LETTA_URL to use LettaEmbedder")
        return LettaAdapter()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        results: list[list[float]] = []
        for text in texts:
            try:
                if hasattr(client, "embed"):
                    vec = client.embed(text)
                elif hasattr(client, "client") and hasattr(client.client, "embed"):
                    vec = client.client.embed(text)
                else:
                    raise RuntimeError("Letta client does not expose an embed() method")
                results.append(list(vec))
            except Exception as exc:
                raise RuntimeError(f"Letta sidecar embedding failed: {exc}") from exc
        return results


__all__ = ["LettaEmbedder"]
