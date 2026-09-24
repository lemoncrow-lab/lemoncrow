"""Built-in Review surface providers.

Providers discover *what* is reviewable. They never own process/container
execution; surfaces bind to independently configured runtime runner plugins.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, SetupCandidate, SurfaceConfigEntry, SurfaceContext

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".jj",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".turbo",
        "coverage",
        "deleted",
        "archive",
        "archives",
    }
)
_IGNORED_DIR_PREFIXES = (".next-", ".cache", ".tmp", "tmp-")
_WEB_DEPENDENCIES = ("next", "astro", "vite")
_HTTP_METHOD_RE = re.compile(r"(?m)^\s*(get|post|put|patch|delete|head|options)\s*\{\s*$", re.IGNORECASE)
_URL_RE = re.compile(r"(?m)^\s*url:\s*(.+?)\s*$")
_NAME_RE = re.compile(r"(?m)^\s*name:\s*(.+?)\s*$")


def _walk_named(repo_root: Path, names: set[str], *, max_depth: int = 5) -> tuple[Path, ...]:
    root = repo_root.resolve()
    found: list[Path] = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (
                entry.name in _IGNORED_DIRS or any(entry.name.startswith(prefix) for prefix in _IGNORED_DIR_PREFIXES)
            ):
                continue
            if entry.is_file() and entry.name in names:
                found.append(entry)
            elif entry.is_dir() and depth < max_depth:
                pending.append((entry, depth + 1))
    return tuple(sorted(found))


def _relative(repo_root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    return rel or "."


def _web_framework(package_path: Path) -> str:
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    return next((name for name in _WEB_DEPENDENCIES if name in dependencies), "")


def _configured_surface(context: SurfaceContext, provider: str, *, root: str = ".") -> SurfaceConfigEntry | None:
    candidates = context.config.for_provider(provider)
    matching = [entry for entry in candidates if (entry.string("root", ".") or ".") == root]
    return matching[0] if len(matching) == 1 else None


def _configured_runtime(context: SurfaceContext, provider: str, *, root: str = ".") -> str:
    entry = _configured_surface(context, provider, root=root)
    return entry.string("runtime") if entry is not None else ""


class WebSurfaceProvider:
    provider_id = "web"

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        from lemoncrow.pro.capabilities.review.web_preview import infer_web_preview_descriptor

        surfaces: list[ReviewSurface] = []
        changed_paths = list(context.changed_paths)
        try:
            from lemoncrow.pro.capabilities.review.snapshot import (
                frozen_submodule_changed_paths,
                review_submodule_changed_paths,
            )

            changed_paths.extend(
                path
                for path in frozen_submodule_changed_paths(context.store_root, context.revision)
                if path not in changed_paths
            )
            # A clean commit-range submodule bump appears in the parent packet
            # only as the gitlink path (for example ``landing``). Expand that
            # pointer into exact nested file changes for configured web roots so
            # the same landing preview that works for a dirty submodule also
            # works after the nested change has been committed.
            configured_roots = tuple(
                dict.fromkeys(
                    entry.string("root", ".") or "." for entry in context.config.for_provider(self.provider_id)
                )
            )
            for root in configured_roots:
                if root == "." or root not in context.changed_paths:
                    continue
                changed_paths.extend(
                    path
                    for path in review_submodule_changed_paths(
                        context.store_root, context.repo_root, context.revision, root
                    )
                    if path not in changed_paths
                )
        except (OSError, ValueError):
            pass
        for path in changed_paths:
            try:
                descriptor = infer_web_preview_descriptor(
                    context.repo_root,
                    context.revision,
                    path,
                    store_root=context.store_root,
                    store=context.store,
                )
            except (OSError, ValueError):
                continue
            if descriptor is None:
                continue
            root = getattr(descriptor, "root", ".") or "."
            configured = _configured_surface(context, self.provider_id, root=root)
            runtime = configured.string("runtime") if configured is not None else ""
            for route in descriptor.routes:
                surface_id = f"{root}:{route}" if root != "." else route
                surfaces.append(
                    ReviewSurface(
                        id=surface_id,
                        provider=self.provider_id,
                        kind="web.route",
                        title=route,
                        locator=route,
                        runtime=runtime,
                        affected_paths=(path,),
                        capabilities=("preview", "compare", "source"),
                        metadata={
                            "framework": descriptor.framework,
                            "root": root,
                            "surface_id": configured.id if configured is not None else "",
                        },
                    )
                )
        return tuple(surfaces)

    def setup_candidates(self, repo_root: Path) -> tuple[SetupCandidate, ...]:
        candidates: list[SetupCandidate] = []
        for package in _walk_named(repo_root, {"package.json"}):
            framework = _web_framework(package)
            if not framework:
                continue
            root = _relative(repo_root, package.parent)
            surface_id = "web" if root == "." else "web-" + root.replace("/", "-")
            depth = 0 if root == "." else len(PurePosixPath(root).parts)
            candidates.append(
                SetupCandidate(
                    id=surface_id,
                    provider=self.provider_id,
                    kind="web.route",
                    root=root,
                    confidence=1.0,
                    auto_write=depth <= 2,
                    details={"framework": framework, "routes": "auto"},
                )
            )
        return tuple(candidates)


class ServiceSurfaceProvider:
    """Generic service surfaces; Docker/Python/Node is a runtime concern."""

    provider_id = "service"

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        surfaces: list[ReviewSurface] = []
        for entry in context.config.for_provider(self.provider_id):
            runtime_id = entry.string("runtime")
            runtime = context.config.runtime(runtime_id) if runtime_id else None
            services = entry.strings("services") or (runtime.strings("services") if runtime is not None else ())
            for service in services:
                capabilities = ("source", "execute", "results", "compare") if runtime is not None else ("source",)
                surfaces.append(
                    ReviewSurface(
                        id=f"{entry.id}:{service}",
                        provider=self.provider_id,
                        kind="service",
                        title=service,
                        locator=service,
                        runtime=runtime_id,
                        affected_paths=context.changed_paths,
                        capabilities=capabilities,
                        metadata={
                            "surface_id": entry.id,
                            "service": service,
                            "runner": runtime.runner if runtime is not None else "",
                        },
                    )
                )
        return tuple(surfaces)

    def setup_candidates(self, _repo_root: Path) -> tuple[SetupCandidate, ...]:
        return ()


class BrunoSurfaceProvider:
    provider_id = "bruno"

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        surfaces: list[ReviewSurface] = []
        for entry in context.config.for_provider(self.provider_id):
            collection = entry.string("collection") or entry.string("root")
            if not collection:
                continue
            collection_path = context.repo_root / collection
            if not collection_path.is_dir():
                continue
            runtime_id = entry.string("runtime")
            runtime = context.config.runtime(runtime_id) if runtime_id else None
            for request_path in sorted(collection_path.rglob("*.bru")):
                if request_path.name == "folder.bru" or "environments" in request_path.parts:
                    continue
                parsed = _parse_bru_request(request_path)
                if parsed is None:
                    continue
                method, url, name = parsed
                rel = _relative(context.repo_root, request_path)
                watch_patterns = entry.strings("watch")
                watched_changes = tuple(
                    path
                    for path in context.changed_paths
                    if any(fnmatch.fnmatch(path, pattern) for pattern in watch_patterns)
                )
                affected_paths = tuple(dict.fromkeys((rel, *watched_changes)))
                capabilities = ("source", "execute", "results", "compare") if runtime is not None else ("source",)
                surfaces.append(
                    ReviewSurface(
                        id=rel,
                        provider=self.provider_id,
                        kind="api.request",
                        title=name,
                        locator=f"{method} {url}",
                        runtime=runtime_id,
                        affected_paths=affected_paths,
                        capabilities=capabilities,
                        metadata={
                            "method": method,
                            "url": url,
                            "collection": collection,
                            "environment": entry.string("environment"),
                            "surface_id": entry.id,
                            "runner": runtime.runner if runtime is not None else "",
                            "runtime_required": runtime is None,
                        },
                    )
                )
        return tuple(surfaces)

    def setup_candidates(self, repo_root: Path) -> tuple[SetupCandidate, ...]:
        candidates: list[SetupCandidate] = []
        for bruno_json in _walk_named(repo_root, {"bruno.json"}):
            root = _relative(repo_root, bruno_json.parent)
            environments = bruno_json.parent / "environments"
            env_names = sorted(path.stem for path in environments.glob("*.bru")) if environments.is_dir() else []
            details: dict[str, Any] = {"collection": root}
            if env_names:
                details["environment"] = "development" if "development" in env_names else env_names[0]
            candidates.append(
                SetupCandidate(
                    id="api" if len(candidates) == 0 else f"api-{len(candidates) + 1}",
                    provider=self.provider_id,
                    kind="api.collection",
                    confidence=1.0,
                    details=details,
                )
            )
        return tuple(candidates)


def _parse_bru_request(path: Path) -> tuple[str, str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    method_match = _HTTP_METHOD_RE.search(text)
    url_match = _URL_RE.search(text)
    if method_match is None or url_match is None:
        return None
    name_match = _NAME_RE.search(text)
    name = name_match.group(1).strip() if name_match is not None else path.stem
    return method_match.group(1).upper(), url_match.group(1).strip(), name


__all__ = ["BrunoSurfaceProvider", "ServiceSurfaceProvider", "WebSurfaceProvider"]
