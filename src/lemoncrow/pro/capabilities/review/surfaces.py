"""Review surface discovery and declarative project configuration.

Surfaces answer *what* a human reviews (web route, API request, service, story,
CLI command). Runtimes answer *how* an immutable review revision is executed.
Those extension points are deliberately separate so the same surface provider can
bind to Docker, a plain Python process, npm, or a third-party runner plugin.

A surface may also carry a versioned :class:`PreviewDescriptor`: the provider's
declaration of *what* can be previewed and how the Reader presents it. The
renderer that produces old/new Snapshot artifacts for that descriptor lives in
``previews.py`` and is a third, independent extension point.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml

from lemoncrow.pro.capabilities.review.session_models import ReviewRevision

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

CONFIG_RELATIVE_PATH = Path(".lemoncrow/review.yaml")
PROVIDER_ENTRYPOINT_GROUP = "lemoncrow.review_surface_providers"
SURFACE_CAPABILITIES = frozenset({"preview", "compare", "source", "execute", "results", "auto_compare"})

# Versioned comparison contract. Preview plugins can inherit a host comparator
# automatically; executable surfaces must opt into safe automatic comparison.
COMPARISON_CONTRACT_VERSION = 1
SUPPORTED_COMPARISON_CONTRACT_VERSIONS = frozenset({COMPARISON_CONTRACT_VERSION})
COMPARISON_SOURCES = frozenset({"preview", "run", "web"})

# Versioned preview contract. Bump only for incompatible descriptor/renderer
# changes; the host refuses (with a specific reason) versions it cannot honor.
PREVIEW_CONTRACT_VERSION = 1
SUPPORTED_PREVIEW_CONTRACT_VERSIONS = frozenset({PREVIEW_CONTRACT_VERSION})
# How the Reader presents an artifact. Anything else is refused host-side.
PREVIEW_DISPLAYS = frozenset({"image", "html", "text", "json", "download"})


@dataclass(frozen=True)
class ComparisonDescriptor:
    """How the host may automatically compare one surface across old/new sides.

    ``source`` identifies where the comparable values come from: a preview
    artifact, a runner result, or the built-in web capture compatibility path.
    ``auto`` is an explicit side-effect-safety declaration for executable
    surfaces. Preview artifacts are already produced through the preview sandbox
    and may inherit a safe comparator without an explicit descriptor.
    """

    source: str = "preview"
    comparator: str = "auto"
    label: str = ""
    auto: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)
    version: int = COMPARISON_CONTRACT_VERSION

    def problem(self) -> str:
        if self.version not in SUPPORTED_COMPARISON_CONTRACT_VERSIONS:
            supported = ", ".join(str(item) for item in sorted(SUPPORTED_COMPARISON_CONTRACT_VERSIONS))
            return f"comparison contract version {self.version} is not supported (this LemonCrow supports {supported})"
        if self.source not in COMPARISON_SOURCES:
            return f"comparison source {self.source!r} is not supported"
        if not str(self.comparator).strip():
            return "comparison descriptor names no comparator"
        return ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "comparator": self.comparator,
            "label": self.label,
            "auto": self.auto,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class PreviewDescriptor:
    """A provider's declaration of one previewable artifact for a surface.

    ``renderer`` names the preview renderer plugin that produces the old/new
    Snapshot artifacts. ``display`` tells the Reader how to present them
    without knowing the surface kind. ``scope`` is the repository-relative
    subtree the host materializes for the renderer; ``options`` is opaque,
    renderer-owned JSON (for example a Storybook story id).
    """

    renderer: str
    display: str = "image"
    media_type: str = "image/png"
    label: str = ""
    sides: tuple[str, ...] = ("old", "new")
    scope: str = "."
    options: Mapping[str, Any] = field(default_factory=dict)
    version: int = PREVIEW_CONTRACT_VERSION

    def problem(self) -> str:
        """Return why the host cannot honor this descriptor, or ``""``."""

        if self.version not in SUPPORTED_PREVIEW_CONTRACT_VERSIONS:
            supported = ", ".join(str(item) for item in sorted(SUPPORTED_PREVIEW_CONTRACT_VERSIONS))
            return f"preview contract version {self.version} is not supported (this LemonCrow supports {supported})"
        if not str(self.renderer).strip():
            return "preview descriptor names no renderer"
        if self.display not in PREVIEW_DISPLAYS:
            return f"preview display {self.display!r} is not supported"
        if not self.sides or any(side not in {"old", "new"} for side in self.sides):
            return "preview sides must be old and/or new"
        try:
            _safe_relative_path(self.scope, field_name="preview scope")
        except ValueError as exc:
            return str(exc)
        return ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "renderer": self.renderer,
            "display": self.display,
            "media_type": self.media_type,
            "label": self.label,
            "sides": list(self.sides),
            "scope": self.scope,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class PluginIssue:
    """One extension that could not be loaded or failed while discovering."""

    plugin: str
    kind: str
    reason: str

    def to_payload(self) -> dict[str, str]:
        return {"plugin": self.plugin, "kind": self.kind, "reason": self.reason}


@dataclass(frozen=True)
class ReviewSurface:
    """One product surface discovered for one immutable review revision."""

    id: str
    provider: str
    kind: str
    title: str
    locator: str = ""
    runtime: str = ""
    affected_paths: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("source",)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    preview: PreviewDescriptor | None = None
    comparison: ComparisonDescriptor | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "title": self.title,
            "locator": self.locator,
            "runtime": self.runtime,
            "affected_paths": list(self.affected_paths),
            "capabilities": list(self.capabilities),
            "metadata": dict(self.metadata),
            "preview": self.preview.to_payload() if self.preview is not None else None,
            "comparison": self.comparison.to_payload() if self.comparison is not None else None,
        }


@dataclass(frozen=True)
class SurfaceConfigEntry:
    id: str
    provider: str
    values: Mapping[str, Any]

    def string(self, key: str, default: str = "") -> str:
        value = self.values.get(key, default)
        return value.strip() if isinstance(value, str) else default

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.values.get(key)
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ()


@dataclass(frozen=True)
class RuntimeConfigEntry:
    """One configured runtime instance backed by a self-contained runner plugin."""

    id: str
    runner: str
    values: Mapping[str, Any]

    def string(self, key: str, default: str = "") -> str:
        value = self.values.get(key, default)
        return value.strip() if isinstance(value, str) else default

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.values.get(key)
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ()


@dataclass(frozen=True)
class ReviewSurfaceConfig:
    version: int = 1
    runtimes: tuple[RuntimeConfigEntry, ...] = ()
    surfaces: tuple[SurfaceConfigEntry, ...] = ()

    def for_provider(self, provider: str) -> tuple[SurfaceConfigEntry, ...]:
        return tuple(item for item in self.surfaces if item.provider == provider)

    def runtime(self, runtime_id: str) -> RuntimeConfigEntry | None:
        return next((item for item in self.runtimes if item.id == runtime_id), None)


@dataclass(frozen=True)
class SurfaceContext:
    repo_root: Path
    store_root: Path
    revision: ReviewRevision
    changed_paths: tuple[str, ...]
    config: ReviewSurfaceConfig
    store: ReviewStore | None = None
    scratch_root: Path | None = None
    dependency_root: Path | None = None


@dataclass(frozen=True)
class SetupCandidate:
    id: str
    provider: str
    kind: str
    root: str = "."
    confidence: float = 1.0
    auto_write: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_config(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "provider": self.provider}
        if self.root not in {"", "."}:
            payload["root"] = self.root
        payload.update(dict(self.details))
        return payload


@runtime_checkable
class ReviewSurfaceProvider(Protocol):
    provider_id: str

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]: ...

    def setup_candidates(self, repo_root: Path) -> tuple[SetupCandidate, ...]: ...


class SurfaceRegistry:
    def __init__(
        self,
        providers: Iterable[ReviewSurfaceProvider] = (),
        *,
        issues: Iterable[PluginIssue] = (),
    ) -> None:
        self._providers: dict[str, ReviewSurfaceProvider] = {}
        self.issues: tuple[PluginIssue, ...] = tuple(issues)
        for provider in providers:
            self.register(provider)

    def register(self, provider: ReviewSurfaceProvider) -> None:
        provider_id = str(provider.provider_id).strip()
        if not provider_id:
            raise ValueError("review surface provider_id must be non-empty")
        if provider_id in self._providers:
            raise ValueError(f"duplicate review surface provider: {provider_id}")
        self._providers[provider_id] = provider

    def providers(self) -> tuple[ReviewSurfaceProvider, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def get(self, provider_id: str) -> ReviewSurfaceProvider | None:
        return self._providers.get(provider_id)

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        return self._discover(context, isolate=False)[0]

    def discover_isolated(self, context: SurfaceContext) -> tuple[tuple[ReviewSurface, ...], tuple[PluginIssue, ...]]:
        """Discover while containing each provider's failure to that provider.

        A broken third-party provider must not hide the web/API/service
        surfaces of healthy providers, and must never block source review; its
        failure is returned as a specific issue instead.
        """

        return self._discover(context, isolate=True)

    def _discover(
        self, context: SurfaceContext, *, isolate: bool
    ) -> tuple[tuple[ReviewSurface, ...], tuple[PluginIssue, ...]]:
        surfaces: list[ReviewSurface] = []
        issues: list[PluginIssue] = list(self.issues)
        indexes: dict[tuple[str, str], int] = {}
        providers = tuple(self.providers())
        # A configured provider that no installed plugin supplies would otherwise
        # vanish silently; say so, and keep its files reviewable as source.
        installed = {str(provider.provider_id) for provider in providers}
        reported = {issue.plugin for issue in issues}
        for name in dict.fromkeys(entry.provider for entry in context.config.surfaces):
            if name not in installed and name not in reported:
                issues.append(
                    PluginIssue(
                        plugin=name,
                        kind="provider",
                        reason=f"no installed surface provider is named {name!r}; install the plugin that provides it",
                    )
                )
        for provider in providers:
            try:
                discovered = tuple(provider.discover(context))
                if isolate and any(not isinstance(item, ReviewSurface) for item in discovered):
                    raise TypeError("discover() must return ReviewSurface values")
            except Exception as exc:
                if not isolate:
                    raise
                issues.append(
                    PluginIssue(
                        plugin=str(provider.provider_id),
                        kind="provider",
                        reason=f"surface discovery failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue
            for surface in discovered:
                identity = (surface.provider, surface.id)
                index = indexes.get(identity)
                if index is None:
                    indexes[identity] = len(surfaces)
                    surfaces.append(surface)
                    continue
                previous = surfaces[index]
                surfaces[index] = ReviewSurface(
                    id=previous.id,
                    provider=previous.provider,
                    kind=previous.kind,
                    title=previous.title,
                    locator=previous.locator,
                    runtime=previous.runtime or surface.runtime,
                    affected_paths=tuple(dict.fromkeys((*previous.affected_paths, *surface.affected_paths))),
                    capabilities=tuple(dict.fromkeys((*previous.capabilities, *surface.capabilities))),
                    metadata={**dict(previous.metadata), **dict(surface.metadata)},
                    preview=previous.preview or surface.preview,
                    comparison=previous.comparison or surface.comparison,
                )
        return tuple(surfaces), tuple(issues)

    def setup_candidates(self, repo_root: Path) -> tuple[SetupCandidate, ...]:
        candidates: list[SetupCandidate] = []
        seen: set[tuple[str, str]] = set()
        for provider in self.providers():
            for candidate in provider.setup_candidates(repo_root):
                identity = (candidate.provider, candidate.id)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(candidate)
        return tuple(candidates)


def _safe_relative_path(value: str, *, field_name: str) -> str:
    raw = value.strip().replace("\\", "/") or "."
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must stay inside the repository")
    return path.as_posix()


def _mapping_list(node: object, *, field_name: str) -> list[Mapping[str, Any]]:
    if node is None:
        return []
    if not isinstance(node, list):
        raise ValueError(f"{CONFIG_RELATIVE_PATH.as_posix()} {field_name} must be a list")
    rows: list[Mapping[str, Any]] = []
    for index, raw in enumerate(node):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field_name[:-1]} #{index + 1} must be a mapping")
        rows.append(raw)
    return rows


def load_review_surface_config(repo_root: Path) -> ReviewSurfaceConfig:
    path = repo_root / CONFIG_RELATIVE_PATH
    if not path.is_file():
        return ReviewSurfaceConfig()
    try:
        decoded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid {CONFIG_RELATIVE_PATH.as_posix()}: {exc}") from exc
    if decoded is None:
        return ReviewSurfaceConfig()
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{CONFIG_RELATIVE_PATH.as_posix()} must contain a mapping")
    version = decoded.get("version", 1)
    if not isinstance(version, int) or version != 1:
        raise ValueError(f"{CONFIG_RELATIVE_PATH.as_posix()} version must be 1")

    runtimes: list[RuntimeConfigEntry] = []
    runtime_ids: set[str] = set()
    for index, raw in enumerate(_mapping_list(decoded.get("runtimes", []), field_name="runtimes")):
        runtime_id = str(raw.get("id") or "").strip()
        runner = str(raw.get("runner") or "").strip()
        if not runtime_id or not runner:
            raise ValueError(f"runtime #{index + 1} requires id and runner")
        if runtime_id in runtime_ids:
            raise ValueError(f"duplicate runtime id: {runtime_id}")
        runtime_ids.add(runtime_id)
        values = dict(raw)
        values.pop("id", None)
        values.pop("runner", None)
        if "root" in values:
            values["root"] = _safe_relative_path(str(values["root"]), field_name=f"runtime {runtime_id} root")
        runtimes.append(RuntimeConfigEntry(id=runtime_id, runner=runner, values=values))

    surfaces: list[SurfaceConfigEntry] = []
    surface_ids: set[str] = set()
    for index, raw in enumerate(_mapping_list(decoded.get("surfaces", []), field_name="surfaces")):
        surface_id = str(raw.get("id") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        if not surface_id or not provider:
            raise ValueError(f"surface #{index + 1} requires id and provider")
        if surface_id in surface_ids:
            raise ValueError(f"duplicate surface id: {surface_id}")
        surface_ids.add(surface_id)
        values = dict(raw)
        values.pop("id", None)
        values.pop("provider", None)
        if "root" in values:
            values["root"] = _safe_relative_path(str(values["root"]), field_name=f"surface {surface_id} root")
        raw_runtime = values.get("runtime")
        runtime_id = str(raw_runtime).strip() if raw_runtime is not None else ""
        if runtime_id and runtime_id not in runtime_ids:
            raise ValueError(f"surface {surface_id!r} references unknown runtime {runtime_id!r}")
        surfaces.append(SurfaceConfigEntry(id=surface_id, provider=provider, values=values))

    return ReviewSurfaceConfig(version=version, runtimes=tuple(runtimes), surfaces=tuple(surfaces))


def write_review_surface_config(
    repo_root: Path,
    candidates: Sequence[SetupCandidate],
    *,
    runtime_candidates: Sequence[Mapping[str, Any]] = (),
    overwrite: bool = False,
) -> Path:
    target = repo_root / CONFIG_RELATIVE_PATH
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists")
    payload: dict[str, Any] = {"version": 1}
    if runtime_candidates:
        payload["runtimes"] = [dict(candidate) for candidate in runtime_candidates]
    payload["surfaces"] = [candidate.to_config() for candidate in candidates]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target


def _entry_points(group: str) -> Sequence[Any]:
    try:
        return tuple(metadata.entry_points(group=group))
    except TypeError:  # pragma: no cover
        return tuple(metadata.entry_points().select(group=group))


def load_entrypoint_providers() -> tuple[ReviewSurfaceProvider, ...]:
    found: list[ReviewSurfaceProvider] = []
    for point in _entry_points(PROVIDER_ENTRYPOINT_GROUP):
        candidate = point.load()
        provider = candidate() if isinstance(candidate, type) else candidate
        if not isinstance(provider, ReviewSurfaceProvider):
            raise TypeError(f"entry point {point.name!r} does not implement ReviewSurfaceProvider")
        found.append(provider)
    return tuple(found)


def load_entrypoint_providers_isolated() -> tuple[tuple[ReviewSurfaceProvider, ...], tuple[PluginIssue, ...]]:
    """Load provider plugins, turning each broken one into a specific issue."""

    found: list[ReviewSurfaceProvider] = []
    issues: list[PluginIssue] = []
    for point in _entry_points(PROVIDER_ENTRYPOINT_GROUP):
        try:
            candidate = point.load()
            provider = candidate() if isinstance(candidate, type) else candidate
        except Exception as exc:
            issues.append(PluginIssue(point.name, "provider", f"plugin failed to load: {type(exc).__name__}: {exc}"))
            continue
        if not isinstance(provider, ReviewSurfaceProvider):
            issues.append(PluginIssue(point.name, "provider", "entry point does not implement ReviewSurfaceProvider"))
            continue
        found.append(provider)
    return tuple(found), tuple(issues)


def built_in_registry(*, include_entrypoints: bool = True) -> SurfaceRegistry:
    from lemoncrow.pro.capabilities.review.storybook_preview import StorybookSurfaceProvider
    from lemoncrow.pro.capabilities.review.surface_providers import (
        BrunoSurfaceProvider,
        ServiceSurfaceProvider,
        WebSurfaceProvider,
    )

    providers: list[ReviewSurfaceProvider] = [
        WebSurfaceProvider(),
        ServiceSurfaceProvider(),
        BrunoSurfaceProvider(),
        StorybookSurfaceProvider(),
    ]
    issues: list[PluginIssue] = []
    if include_entrypoints:
        plugins, issues_found = load_entrypoint_providers_isolated()
        issues.extend(issues_found)
        taken = {provider.provider_id for provider in providers}
        for plugin in plugins:
            plugin_id = str(getattr(plugin, "provider_id", "")).strip()
            if not plugin_id or plugin_id in taken:
                reason = "empty provider_id" if not plugin_id else f"provider id {plugin_id!r} is already registered"
                issues.append(PluginIssue(plugin_id or "?", "provider", f"plugin ignored: {reason}"))
                continue
            taken.add(plugin_id)
            providers.append(plugin)
    return SurfaceRegistry(providers, issues=issues)


__all__ = [
    "CONFIG_RELATIVE_PATH",
    "PREVIEW_CONTRACT_VERSION",
    "PREVIEW_DISPLAYS",
    "PROVIDER_ENTRYPOINT_GROUP",
    "SUPPORTED_PREVIEW_CONTRACT_VERSIONS",
    "PluginIssue",
    "PreviewDescriptor",
    "ReviewSurface",
    "ReviewSurfaceConfig",
    "ReviewSurfaceProvider",
    "RuntimeConfigEntry",
    "SetupCandidate",
    "SurfaceConfigEntry",
    "SurfaceContext",
    "SurfaceRegistry",
    "built_in_registry",
    "load_entrypoint_providers",
    "load_entrypoint_providers_isolated",
    "load_review_surface_config",
    "write_review_surface_config",
]
