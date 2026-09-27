"""Workspace-aware routing for supported read-only code-intel operations."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.code_context_contract import (
    UnsupportedWorkspaceOperationError as UnsupportedWorkspaceOperationError,
)
from lemoncrow.core.service.project_registry import project_id_for_root
from lemoncrow.pro.capabilities.code_context.workspace_config import (
    WorkspaceConfig,
    load_workspace_config,
)

SUPPORTED_WORKSPACE_OPS = frozenset({"search", "symbol", "explore"})


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
        if op == "explore":
            return self._route_explore(targets, **kwargs)
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
        provenance_breakdown: dict[str, int] = {}
        mode: str | None = None
        for repo_root, payload in self._payloads(targets, "tool_search", **kwargs):
            repo_name = self._repo_name_for_root(repo_root)
            merged_items.extend(
                self._annotate_items(list(payload.get("items", [])), repo_name=repo_name, repo_root=repo_root)
            )
            total_tokens += int(payload.get("total_tokens", 0))
            tokens_saved += int(payload.get("tokens_saved", 0))
            cache_hit = cache_hit and bool(payload.get("cache_hit", False))
            provenance = str(payload.get("provenance", provenance))
            mode = str(payload.get("mode", mode or "")) or mode
            payload_breakdown = payload.get("provenance_breakdown")
            if isinstance(payload_breakdown, dict):
                for key, value in payload_breakdown.items():
                    provenance_breakdown[str(key)] = provenance_breakdown.get(str(key), 0) + int(value)
            elif payload.get("items"):
                provenance_breakdown[provenance] = provenance_breakdown.get(provenance, 0) + len(
                    list(payload.get("items", []))
                )
        result = {
            "items": merged_items,
            "cache_hit": cache_hit,
            "provenance": provenance,
            "tokens_saved": tokens_saved,
            "total_tokens": total_tokens,
        }
        if mode is not None:
            result["mode"] = mode
        if provenance_breakdown:
            result["provenance_breakdown"] = provenance_breakdown
        return result

    def _route_explore(self, targets: list[Path], **kwargs: Any) -> dict[str, Any]:
        """Merge rich explore payloads while preserving workspace-addressable paths."""
        entry_points: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        tails: dict[str, list[str]] = {
            "additional_relevant_files": [],
            "fused_recall": [],
            "deep_recall": [],
        }
        project_by_path: dict[str, str] = {}
        exact_match = False
        truncated = False
        for repo_root, payload in self._payloads(targets, "tool_explore", **kwargs):
            repo_name = self._repo_name_for_root(repo_root)
            exact_match = exact_match or bool(payload.get("exact_match"))
            truncated = truncated or bool(payload.get("truncated"))
            routed_entry_points = self._annotate_items(
                [item for item in payload.get("entry_points", []) if isinstance(item, dict)],
                repo_name=repo_name,
                repo_root=repo_root,
            )
            routed_files = self._annotate_items(
                [item for item in payload.get("files", []) if isinstance(item, dict)],
                repo_name=repo_name,
                repo_root=repo_root,
            )
            entry_points.extend(routed_entry_points)
            files.extend(routed_files)
            project_id = project_id_for_root(repo_root)
            for item in (*routed_entry_points, *routed_files):
                path = item.get("path") or item.get("file_path")
                if isinstance(path, str) and path:
                    project_by_path[path] = project_id
            for key in tails:
                for value in payload.get(key, []) or []:
                    if not isinstance(value, str):
                        continue
                    rebased = self._workspace_path(repo_root, value)
                    if rebased not in tails[key]:
                        tails[key].append(rebased)
                    project_by_path[rebased] = project_id
        result: dict[str, Any] = {
            "exact_match": exact_match,
            "entry_points": entry_points,
            "files": files,
        }
        for key, values in tails.items():
            if values:
                result[key] = values
        if project_by_path:
            result["project_by_path"] = project_by_path
        if truncated:
            result["truncated"] = True
        return result

    def _payloads(self, targets: list[Path], method: str, **kwargs: Any) -> list[tuple[Path, dict[str, Any]]]:
        """Run one engine method per repo concurrently, preserving target order."""

        def invoke(repo_root: Path) -> dict[str, Any]:
            engine = self.engine_factory(repo_root)
            call = getattr(engine, method)
            return call(**self._kwargs_for_repo(repo_root, kwargs))

        if len(targets) <= 1:
            return [(repo_root, invoke(repo_root)) for repo_root in targets]
        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            payloads = list(executor.map(invoke, targets))
        return list(zip(targets, payloads, strict=True))

    def _kwargs_for_repo(self, repo_root: Path, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Translate workspace-relative seed paths to the target repo's coordinates."""
        routed = dict(kwargs)
        seeds = routed.get("seed_files")
        if not isinstance(seeds, list):
            return routed
        repo_seeds: list[str] = []
        for seed in seeds:
            if not isinstance(seed, str) or not seed.strip():
                continue
            candidate = (self.repo_root / seed).resolve()
            # Nested repos overlap their parent's filesystem. Route an explicit
            # seed only to the most-specific configured repo that contains it,
            # otherwise both engines receive the same scope and duplicate work.
            if self.config is not None:
                owners = [
                    entry.repo_root
                    for entry in self.config.repos
                    if candidate == entry.repo_root or entry.repo_root in candidate.parents
                ]
                if owners:
                    owner = max(owners, key=lambda path: len(path.parts))
                    if owner != repo_root:
                        continue
            try:
                relative = candidate.relative_to(repo_root)
            except ValueError:
                continue
            repo_seeds.append(relative.as_posix())
        routed["seed_files"] = repo_seeds or None
        return routed

    def _route_symbol(self, targets: list[Path], **kwargs: Any) -> dict[str, Any]:
        isolate_failures = len(targets) > 1
        last_error: dict[str, Any] | None = None
        for repo_root in targets:
            try:
                payload = self.engine_factory(repo_root).tool_symbol(**kwargs)
            except LookupError:
                last_error = {"error": "symbol_not_found"}
                continue
            except Exception:
                if not isolate_failures:
                    raise
                last_error = {"error": "symbol_not_found"}
                continue
            if "error" not in payload:
                return self._annotate_item(payload, repo_name=self._repo_name_for_root(repo_root), repo_root=repo_root)
            last_error = payload
        return last_error or {"error": "symbol_not_found"}

    def _repo_name_for_root(self, repo_root: Path) -> str | None:
        if self.config is None:
            return None
        for entry in self.config.repos:
            if entry.repo_root == repo_root:
                return entry.name
        return None

    def _workspace_path(self, repo_root: Path, value: str) -> str:
        path = Path(value)
        candidate = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
        try:
            return candidate.relative_to(self.repo_root).as_posix()
        except ValueError:
            # A workspace config may deliberately name a sibling repo. Keep the
            # path honest and addressable from the workspace rather than silently
            # pretending it belongs to the root repo.
            return Path(os.path.relpath(candidate, self.repo_root)).as_posix()

    def _annotate_items(
        self,
        items: list[dict[str, Any]],
        *,
        repo_name: str | None,
        repo_root: Path,
    ) -> list[dict[str, Any]]:
        return [self._annotate_item(item, repo_name=repo_name, repo_root=repo_root) for item in items]

    def _annotate_item(self, item: dict[str, Any], *, repo_name: str | None, repo_root: Path) -> dict[str, Any]:
        annotated = dict(item)
        if repo_name is not None:
            annotated["repo_name"] = repo_name
        annotated["project_id"] = project_id_for_root(repo_root)
        # External-provider paths are not workspace files and must retain their
        # provider coordinates. Internal paths are rebased so read/edit can use
        # the merged result directly from the workspace root.
        if annotated.get("origin") != "external":
            for key in ("path", "file_path", "src_path", "dst_path"):
                value = annotated.get(key)
                if isinstance(value, str) and value:
                    annotated[key] = self._workspace_path(repo_root, value)
        return annotated
