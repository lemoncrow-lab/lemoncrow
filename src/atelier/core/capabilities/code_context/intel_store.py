"""Routing contracts for code-intelligence symbol providers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.core.capabilities.code_context.cache import RetrievalCache
from atelier.core.capabilities.code_context.models import SymbolRecord


@dataclass(frozen=True)
class ProviderHealth:
    """Health status for a routed symbol-intelligence provider."""

    status: Literal["ok", "degraded", "unhealthy"] = "ok"
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@runtime_checkable
class SymbolIntelProvider(Protocol):
    """Backend interface for routed symbol lookups."""

    name: str

    def refresh(self) -> bool: ...

    def health(self) -> ProviderHealth | object | None: ...

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]: ...

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any] | None: ...


class SymbolIntelStore:
    """Routes symbol lookups to healthy providers before local fallback."""

    def __init__(
        self,
        *,
        cache: RetrievalCache,
        packer: BudgetPacker,
        local_search: Callable[..., list[SymbolRecord]],
        local_get_symbol: Callable[..., dict[str, Any]],
    ) -> None:
        self._cache = cache
        self._packer = packer
        self._local_search = local_search
        self._local_get_symbol = local_get_symbol
        self._providers: list[SymbolIntelProvider] = []

    @property
    def providers(self) -> tuple[SymbolIntelProvider, ...]:
        return tuple(self._providers)

    def register(self, provider: SymbolIntelProvider) -> None:
        self._providers.append(provider)

    def refresh(self) -> bool:
        changed = False
        for provider in self._providers:
            try:
                changed = provider.refresh() or changed
            except Exception:
                continue
        return changed

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        for provider in self._providers:
            if not self._provider_is_healthy(provider):
                continue
            try:
                hits = provider.search_symbols(query, limit=limit, kind=kind, language=language)
            except Exception:
                continue
            if hits:
                return hits[:limit]
        return self._local_search(query, limit=limit, kind=kind, language=language)

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any]:
        for provider in self._providers:
            if not self._provider_is_healthy(provider):
                continue
            try:
                payload = provider.get_symbol(
                    symbol_id=symbol_id,
                    qualified_name=qualified_name,
                    file_path=file_path,
                    symbol_name=symbol_name,
                )
            except Exception:
                continue
            if payload is not None:
                return payload
        return self._local_get_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            file_path=file_path,
            symbol_name=symbol_name,
        )

    def _provider_is_healthy(self, provider: SymbolIntelProvider) -> bool:
        try:
            health = provider.health()
        except Exception:
            return False
        if isinstance(health, ProviderHealth):
            return health.ok
        return bool(health)


__all__ = ["ProviderHealth", "SymbolIntelProvider", "SymbolIntelStore"]
