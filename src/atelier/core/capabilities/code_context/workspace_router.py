"""Workspace-aware routing for supported read-only code-intel operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from atelier.core.capabilities.code_context.workspace_config import WorkspaceConfig, load_workspace_config


class UnsupportedWorkspaceOperationError(ValueError):
    """Raised when a workspace-wide op is not supported by the router."""


SUPPORTED_WORKSPACE_OPS = frozenset({"search", "symbol"})


class WorkspaceCodeRouter:
    """Fan out supported code-intel calls across configured workspace repos."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        engine_factory: Callable[[Path], Any],
        config: WorkspaceConfig | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.engine_factory = engine_factory
        self.config = config if config is not None else load_workspace_config(self.repo_root)

    @property
    def is_configured(self) -> bool:
        return self.config is not None

    def route(self, op: str, *, repo: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if op not in SUPPORTED_WORKSPACE_OPS:
            raise UnsupportedWorkspaceOperationError(f"Unsupported workspace operation: {op}")
        targets = self._target_repo_roots(repo)
        if op == "search":
            return self._route_search(targets, **kwargs)
        return self._route_symbol(targets, **kwargs)

    def _target_repo_roots(self, repo: str | None) -> list[Path]:
        if self.config is None:
            if repo is not None:
                raise ValueError("Unknown workspace repo: workspace config is not available")
            return [self.repo_root]
        if repo is None:
            return [entry.repo_root for entry in self.config.repos]
        selected = self.config.repo_by_name(repo)
        if selected is None:
            raise ValueError(f"Unknown workspace repo: {repo}")
        return [selected.repo_root]

    def _route_search(self, targets: list[Path], **kwargs: Any) -> dict[str, Any]:
        merged_items: list[dict[str, Any]] = []
        total_tokens = 0
        tokens_saved = 0
        cache_hit = True
        provenance = "local"
        for repo_root in targets:
            payload = self.engine_factory(repo_root).tool_search(**kwargs)
            merged_items.extend(list(payload.get("items", [])))
            total_tokens += int(payload.get("total_tokens", 0))
            tokens_saved += int(payload.get("tokens_saved", 0))
            cache_hit = cache_hit and bool(payload.get("cache_hit", False))
            provenance = str(payload.get("provenance", provenance))
        return {
            "items": merged_items,
            "cache_hit": cache_hit,
            "provenance": provenance,
            "tokens_saved": tokens_saved,
            "total_tokens": total_tokens,
        }

    def _route_symbol(self, targets: list[Path], **kwargs: Any) -> dict[str, Any]:
        last_error: dict[str, Any] | None = None
        for repo_root in targets:
            payload = self.engine_factory(repo_root).tool_symbol(**kwargs)
            if "error" not in payload:
                return payload
            last_error = payload
        return last_error or {"error": "symbol_not_found"}
