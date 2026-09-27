"""Versioned Review preview renderers and the host that runs them.

Three independent extension points cooperate for one previewable surface:

* a surface provider (``surfaces.py``) discovers *what* is reviewable and may
  attach a :class:`~lemoncrow.pro.capabilities.review.surfaces.PreviewDescriptor`;
* a preview renderer (this module) produces the old/new Snapshot artifacts for
  that descriptor;
* the Reader presents the artifact from ``descriptor.display`` alone, so a new
  platform needs no Reader change.

The host owns everything that must never be delegated to a plugin: it pins the
requested immutable revision side, materializes it into a private workspace,
executes reviewed code only through the bubblewrap sandbox, enforces time and
size limits, confines artifact paths, validates media types, caches artifacts
per (revision, side) and turns every plugin failure into a specific reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol, runtime_checkable

from lemoncrow.core.foundation.run_file_io import RunFileLock
from lemoncrow.pro.capabilities.review.sandbox_limits import (
    SandboxCapacityBusy,
    limit_note,
    limit_prefix,
    sandbox_slot,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    PREVIEW_CONTRACT_VERSION,
    SUPPORTED_PREVIEW_CONTRACT_VERSIONS,
    PluginIssue,
    PreviewDescriptor,
    ReviewSurface,
    SurfaceConfigEntry,
    SurfaceContext,
    _entry_points,
    _safe_relative_path,
)

PREVIEW_RENDERER_ENTRYPOINT_GROUP = "lemoncrow.review_preview_renderers"

# Host-owned ceilings. ``.lemoncrow/review.yaml`` may lower them, never raise them.
MAX_PREVIEW_SECONDS = 300
DEFAULT_PREVIEW_SECONDS = 120
MAX_PREVIEW_BYTES = 25 * 1024 * 1024
MIN_PREVIEW_BYTES = 1024
MAX_LOG_CHARS = 4000
# Rendered artifacts are keyed by side content, not by Snapshot, so Snapshots
# that share a side reuse its workspace and artifacts. Keep the most recently
# used side caches of a Review and drop the rest once they have been idle a while.
KEEP_SIDE_CACHES_PER_REVIEW = 8
_PRUNE_IDLE_SECONDS = 3600
_SIDES_DIR = "sides"
# DOM returned by a page capture is read for render state only, never stored.
_MAX_CAPTURE_DOM_BYTES = 2 * 1024 * 1024

_DISPLAY_MEDIA_TYPES: dict[str, frozenset[str]] = {
    # SVG is deliberately absent: it is a script-capable document, not an image.
    "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
    "html": frozenset({"text/html"}),
    "text": frozenset({"text/plain", "text/markdown", "text/csv", "text/x-diff"}),
    "json": frozenset({"application/json"}),
}
_IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_RESERVED_ENV = frozenset({"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"})
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CAPTURE_PORT = 8787


class PreviewUnavailable(RuntimeError):
    """This artifact cannot be produced; the message is shown to the reviewer verbatim."""


@dataclass(frozen=True)
class PreviewLimits:
    timeout_seconds: int = DEFAULT_PREVIEW_SECONDS
    max_bytes: int = MAX_PREVIEW_BYTES


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    output: str
    duration_ms: int
    # Set when the host limits most likely killed the command (e.g. memory).
    limit_note: str = ""


@dataclass(frozen=True)
class PageCapture:
    """One headless-Chrome capture: the PNG and the DOM it rendered."""

    path: Path
    dom: str


def _tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    compact = text.strip()
    return compact[-limit:] if len(compact) > limit else compact


def _confined(root: Path, relative: str, *, field_name: str) -> Path:
    try:
        safe = _safe_relative_path(relative, field_name=field_name)
    except ValueError as exc:
        raise PreviewUnavailable(str(exc)) from exc
    base = root.resolve()
    candidate = (base / safe).resolve() if safe != "." else base
    if candidate != base and base not in candidate.parents:
        raise PreviewUnavailable(f"{field_name} escapes the reviewed snapshot")
    return candidate


class PreviewSandbox:
    """The only way a renderer may execute reviewed code.

    Commands run in the same bubblewrap policy as web previews and command
    runtimes: no network namespace, read-only system and dependency mounts,
    the private revision workspace at ``/workspace``, the artifact directory at
    ``/capture``, a tmpfs ``/tmp``, a cleared environment, and a per-call
    timeout bounded by the render's overall time budget.
    """

    OUTPUT_MOUNT = "/capture"
    WORKSPACE_MOUNT = "/workspace"

    def __init__(self, *, workspace: Path, output_dir: Path, trusted_checkout: Path, deadline: float) -> None:
        self.workspace = workspace
        self.output_dir = output_dir
        self.trusted_checkout = trusted_checkout
        self.deadline = deadline

    def remaining_seconds(self) -> int:
        return max(0, int(self.deadline - time.monotonic()))

    def node_modules(self, project_root: str = ".") -> Path:
        """Return checkout dependencies proven to match the reviewed lockfile.

        Dependencies are borrowed read-only from the trusted checkout and only
        when the reviewed ``package.json``/lockfile equals the checkout's, so a
        change that edits dependencies is refused instead of rendered with the
        wrong graph. Missing dependencies are installed once from the lockfile
        with lifecycle scripts disabled.
        """

        from lemoncrow.pro.capabilities.review.web_preview import (
            WebPreviewUnavailable,
            _dependency_signature,
            _ensure_checkout_dependencies,
        )

        project = _confined(self.workspace, project_root, field_name="preview project root")
        live_project = _confined(self.trusted_checkout, project_root, field_name="preview project root")
        if not (project / "package.json").is_file():
            raise PreviewUnavailable(f"project root {project_root} has no package.json in the reviewed snapshot")
        if not (live_project / "package.json").is_file():
            raise PreviewUnavailable(f"project root {project_root} has no package.json in the proven checkout")
        if _dependency_signature(project) != _dependency_signature(live_project):
            raise PreviewUnavailable(
                "the reviewed dependency graph differs from the checkout; refusing automatic dependency provisioning"
            )
        project_modules = live_project / "node_modules"
        modules = project_modules if project_modules.is_dir() else self.trusted_checkout / "node_modules"
        if not modules.is_dir():
            try:
                modules = _ensure_checkout_dependencies(self.trusted_checkout, live_project)
            except WebPreviewUnavailable as exc:
                raise PreviewUnavailable(str(exc)) from exc
        checkout = self.trusted_checkout.resolve()
        resolved = modules.resolve()
        if resolved != checkout and checkout not in resolved.parents:
            raise PreviewUnavailable("dependency directory escapes the trusted checkout")
        return modules

    def _timeout(self, requested: int | None) -> int:
        remaining = self.remaining_seconds()
        if remaining < 1:
            raise PreviewUnavailable("preview time budget exhausted")
        if requested is None:
            return remaining
        return max(1, min(int(requested), remaining))

    def _prefix(self, *, cwd: str, node_modules: Path | None, browser: bool) -> list[str]:
        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, _bubblewrap_prefix

        try:
            return _bubblewrap_prefix(
                self.workspace,
                node_modules,
                repo_root=self.trusted_checkout if node_modules is not None else None,
                project_root=cwd,
                include_chrome=browser,
                capture_dir=self.output_dir,
            )
        except WebPreviewUnavailable as exc:
            raise PreviewUnavailable(str(exc)) from exc

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout: int | None = None,
        env: Mapping[str, str] | None = None,
        node_modules: Path | None = None,
        browser: bool = False,
    ) -> SandboxResult:
        command = [str(item) for item in argv]
        if not command or not command[0]:
            raise PreviewUnavailable("sandboxed command is empty")
        cwd_path = _confined(self.workspace, cwd, field_name="sandbox cwd")
        if not cwd_path.is_dir():
            raise PreviewUnavailable(f"sandbox cwd does not exist on this side: {cwd}")
        env_args: list[str] = []
        for key, value in (env or {}).items():
            if not _ENV_NAME_RE.match(key) or key in _RESERVED_ENV:
                raise PreviewUnavailable(f"sandbox environment variable is not allowed: {key}")
            env_args.extend(("--setenv", key, str(value)))
        seconds = self._timeout(timeout)
        prefix = self._prefix(
            cwd=_safe_relative_path(cwd, field_name="sandbox cwd"), node_modules=node_modules, browser=browser
        )
        full = [*prefix[:-2], *env_args, *prefix[-2:], *command]
        started = time.monotonic()
        try:
            with sandbox_slot(seconds):
                # Waiting for a host slot spends this render's budget like any other work.
                seconds = self._timeout(timeout)
                proc = subprocess.run(
                    [*limit_prefix(), *full],
                    cwd=self.workspace,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=seconds,
                    env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"},
                )
        except SandboxCapacityBusy as exc:
            raise PreviewUnavailable(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise PreviewUnavailable(f"sandboxed command timed out after {seconds}s: {command[0]}") from exc
        except OSError as exc:
            raise PreviewUnavailable(f"sandboxed command could not run: {exc}") from exc
        note = limit_note(proc.returncode)
        output = _tail(proc.stdout or "")
        return SandboxResult(
            exit_code=proc.returncode,
            output=f"{output}\n[{note}]".strip() if note else output,
            duration_ms=int((time.monotonic() - started) * 1000),
            limit_note=note,
        )

    def screenshot(
        self,
        site: Path,
        page: str = "/",
        *,
        width: int = 1280,
        height: int = 900,
        filename: str = "preview.png",
        timeout: int | None = None,
    ) -> Path:
        """Serve a static directory inside the sandbox and capture one page as PNG."""

        return self.capture(site, page, width=width, height=height, filename=filename, timeout=timeout).path

    def capture(
        self,
        site: Path,
        page: str = "/",
        *,
        width: int = 1280,
        height: int = 900,
        filename: str = "preview.png",
        timeout: int | None = None,
    ) -> PageCapture:
        """Like :meth:`screenshot`, plus the DOM Chrome rendered, from one browser run.

        A renderer uses the DOM to tell a page that rendered from one that shows
        an error or a loading state, so a crash is reported as a failure instead
        of as a screenshot of an error screen.
        """

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, _chrome_executable

        root = self.workspace.resolve()
        site_path = site.resolve()
        if site_path != root and root not in site_path.parents:
            raise PreviewUnavailable("screenshot site must be inside the reviewed workspace")
        if not site_path.is_dir():
            raise PreviewUnavailable("screenshot site directory does not exist")
        if not page.startswith("/") or page.startswith("//") or any(ch in page for ch in "\r\n\t "):
            raise PreviewUnavailable("screenshot page must be a local absolute path")
        name = _FILENAME_RE.sub("-", filename).strip(".-") or "preview.png"
        if not name.endswith(".png"):
            name += ".png"
        width = max(320, min(int(width), 2560))
        height = max(240, min(int(height), 8000))
        try:
            chrome = _chrome_executable()
        except WebPreviewUnavailable as exc:
            raise PreviewUnavailable(str(exc)) from exc
        chrome_in_sandbox = "/opt/google/chrome/google-chrome" if chrome.startswith("/opt/google/chrome/") else chrome
        relative = site_path.relative_to(root).as_posix()
        served = self.WORKSPACE_MOUNT if relative in {"", "."} else f"{self.WORKSPACE_MOUNT}/{relative}"
        target = self.output_dir / name
        dom_file = self.output_dir / f".{name}.dom.html"
        target.unlink(missing_ok=True)
        dom_file.unlink(missing_ok=True)
        script = (
            f"python3 -m http.server {_CAPTURE_PORT} --bind 127.0.0.1 --directory {shlex.quote(served)} "
            ">/tmp/lc-preview-http.log 2>&1 & srv=$!; trap 'kill $srv 2>/dev/null || true' EXIT; sleep .15; "
            f"{shlex.quote(chrome_in_sandbox)} --headless=new --no-sandbox --disable-gpu "
            "--disable-background-networking --no-first-run --no-default-browser-check "
            "--run-all-compositor-stages-before-draw --virtual-time-budget=2500 "
            f"--window-size={width},{height} --screenshot={shlex.quote(self.OUTPUT_MOUNT + '/' + name)} "
            f"--dump-dom {shlex.quote(f'http://127.0.0.1:{_CAPTURE_PORT}{page}')} "
            f">{shlex.quote(self.OUTPUT_MOUNT + '/' + dom_file.name)} 2>/tmp/lc-preview-chrome.log; "
            "rc=$?; kill $srv 2>/dev/null || true; exit $rc"
        )
        result = self.run(["sh", "-c", script], timeout=timeout, browser=True)
        try:
            with dom_file.open("rb") as handle:
                dom = handle.read(_MAX_CAPTURE_DOM_BYTES).decode("utf-8", errors="replace")
        except OSError:
            dom = ""
        dom_file.unlink(missing_ok=True)
        if result.exit_code != 0 or not target.is_file():
            raise PreviewUnavailable(f"preview capture failed:\n{result.output}")
        return PageCapture(path=target, dom=dom)


@dataclass(frozen=True)
class PreviewRenderRequest:
    """Everything a renderer may use to produce one side's artifact.

    ``workspace`` is a private, host-materialized copy of exactly this
    revision side (restricted to ``descriptor.scope``); it persists across
    renders of the same (revision, side, renderer, scope) so builds can be
    reused, and never mixes sides or revisions. Write artifacts only into
    ``output_dir`` and execute reviewed code only through ``sandbox``.
    """

    context: SurfaceContext
    surface: ReviewSurface
    descriptor: PreviewDescriptor
    side: str
    identity: str
    workspace: Path
    output_dir: Path
    limits: PreviewLimits
    sandbox: PreviewSandbox
    config: SurfaceConfigEntry | None = None

    def path(self, relative: str) -> Path:
        """Resolve a repository-relative path inside this side's workspace."""

        return _confined(self.workspace, relative, field_name="preview path")


@dataclass(frozen=True)
class PreviewArtifact:
    """A renderer's answer for one side: a file in ``output_dir`` or a reason."""

    status: str
    reason: str = ""
    path: Path | None = None
    media_type: str = ""
    filename: str = ""
    summary: str = ""
    output: str = ""

    @classmethod
    def ready(cls, path: Path, media_type: str, *, filename: str = "", summary: str = "") -> PreviewArtifact:
        return cls("ready", path=path, media_type=media_type, filename=filename, summary=summary)

    @classmethod
    def unavailable(cls, reason: str, *, output: str = "") -> PreviewArtifact:
        return cls("unavailable", reason=reason, output=output)

    @classmethod
    def failed(cls, reason: str, *, output: str = "") -> PreviewArtifact:
        return cls("failed", reason=reason, output=output)


@runtime_checkable
@runtime_checkable
class ReviewPreviewRenderer(Protocol):
    """Produces one Snapshot side's artifact for a preview descriptor."""

    renderer_id: ClassVar[str]
    contract_version: ClassVar[int]

    def render(self, request: PreviewRenderRequest) -> PreviewArtifact: ...


def renderer_problem(renderer: object) -> str:
    version = getattr(renderer, "contract_version", None)
    if version not in SUPPORTED_PREVIEW_CONTRACT_VERSIONS:
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_PREVIEW_CONTRACT_VERSIONS))
        return (
            f"preview renderer {getattr(renderer, 'renderer_id', '?')!r} implements contract version "
            f"{version!r}; this LemonCrow supports {supported}"
        )
    return ""


class PreviewRendererRegistry:
    def __init__(
        self,
        renderers: Iterable[ReviewPreviewRenderer] = (),
        *,
        issues: Iterable[PluginIssue] = (),
    ) -> None:
        self._renderers: dict[str, ReviewPreviewRenderer] = {}
        self.issues: tuple[PluginIssue, ...] = tuple(issues)
        for renderer in renderers:
            self.register(renderer)

    def register(self, renderer: ReviewPreviewRenderer) -> None:
        renderer_id = str(renderer.renderer_id).strip()
        if not renderer_id:
            raise ValueError("review preview renderer_id must be non-empty")
        if renderer_id in self._renderers:
            raise ValueError(f"duplicate review preview renderer: {renderer_id}")
        self._renderers[renderer_id] = renderer

    def renderers(self) -> tuple[ReviewPreviewRenderer, ...]:
        return tuple(self._renderers[key] for key in sorted(self._renderers))

    def get(self, renderer_id: str) -> ReviewPreviewRenderer | None:
        return self._renderers.get(renderer_id)


def load_entrypoint_renderers_isolated() -> tuple[tuple[ReviewPreviewRenderer, ...], tuple[PluginIssue, ...]]:
    found: list[ReviewPreviewRenderer] = []
    issues: list[PluginIssue] = []
    for point in _entry_points(PREVIEW_RENDERER_ENTRYPOINT_GROUP):
        try:
            candidate = point.load()
            renderer = candidate() if isinstance(candidate, type) else candidate
        except Exception as exc:
            issues.append(PluginIssue(point.name, "renderer", f"plugin failed to load: {type(exc).__name__}: {exc}"))
            continue
        if not isinstance(renderer, ReviewPreviewRenderer):
            issues.append(PluginIssue(point.name, "renderer", "entry point does not implement ReviewPreviewRenderer"))
            continue
        found.append(renderer)
    return tuple(found), tuple(issues)


def built_in_preview_registry(*, include_entrypoints: bool = True) -> PreviewRendererRegistry:
    from lemoncrow.pro.capabilities.review.storybook_preview import StorybookPreviewRenderer

    renderers: list[ReviewPreviewRenderer] = [StorybookPreviewRenderer()]
    issues: list[PluginIssue] = []
    if include_entrypoints:
        plugins, load_issues = load_entrypoint_renderers_isolated()
        issues.extend(load_issues)
        taken = {renderer.renderer_id for renderer in renderers}
        for plugin in plugins:
            plugin_id = str(getattr(plugin, "renderer_id", "")).strip()
            if not plugin_id or plugin_id in taken:
                reason = "empty renderer_id" if not plugin_id else f"renderer id {plugin_id!r} is already registered"
                issues.append(PluginIssue(plugin_id or "?", "renderer", f"plugin ignored: {reason}"))
                continue
            taken.add(plugin_id)
            renderers.append(plugin)
    return PreviewRendererRegistry(renderers, issues=issues)


def _surface_config(context: SurfaceContext, surface: ReviewSurface) -> SurfaceConfigEntry | None:
    configured = str(surface.metadata.get("surface_id") or "")
    if not configured:
        return None
    return next((entry for entry in context.config.surfaces if entry.id == configured), None)


def _preview_settings(config: SurfaceConfigEntry | None) -> Mapping[str, Any]:
    raw = config.values.get("preview") if config is not None else None
    return raw if isinstance(raw, Mapping) else {}


def preview_limits(config: SurfaceConfigEntry | None) -> PreviewLimits:
    """Resolve configured limits, clamped to the host ceilings."""

    settings = _preview_settings(config)
    try:
        timeout = max(5, min(int(settings.get("timeout", DEFAULT_PREVIEW_SECONDS)), MAX_PREVIEW_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_PREVIEW_SECONDS
    try:
        max_bytes = max(MIN_PREVIEW_BYTES, min(int(settings.get("max_bytes", MAX_PREVIEW_BYTES)), MAX_PREVIEW_BYTES))
    except (TypeError, ValueError):
        max_bytes = MAX_PREVIEW_BYTES
    return PreviewLimits(timeout_seconds=timeout, max_bytes=max_bytes)


def preview_availability(
    surface: ReviewSurface,
    renderers: PreviewRendererRegistry,
    context: SurfaceContext | None = None,
) -> str:
    """Return why this surface's preview cannot render at all, or ``""``."""

    descriptor = surface.preview
    if descriptor is None:
        return "surface declares no preview"
    problem = descriptor.problem()
    if problem:
        return problem
    if context is not None and _preview_settings(_surface_config(context, surface)).get("enabled") is False:
        return "preview is disabled for this surface in .lemoncrow/review.yaml"
    renderer = renderers.get(descriptor.renderer)
    if renderer is None:
        return (
            f"preview renderer {descriptor.renderer!r} is not installed; install the plugin that provides it "
            f"(entry point group {PREVIEW_RENDERER_ENTRYPOINT_GROUP})"
        )
    return renderer_problem(renderer)


@dataclass(frozen=True)
class PreviewResult:
    status: str
    reason: str
    revision_id: str
    side: str
    renderer: str
    display: str
    media_type: str = ""
    filename: str = ""
    path: Path | None = None
    summary: str = ""
    output: str = ""
    identity: str = ""
    cached: bool = False


_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _lock(key: str) -> threading.Lock:
    # lc-debt: one process-local lock per workspace key is never evicted; bounded
    # by (revision, side, renderer, scope) tuples actually rendered in this process.
    with _GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[
        :24
    ]


def _normalized_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _validate_bytes(display: str, media_type: str, payload: bytes) -> str:
    if display == "download":
        return "" if media_type and "/" in media_type else "artifact media type is missing"
    allowed = _DISPLAY_MEDIA_TYPES.get(display, frozenset())
    if media_type not in allowed:
        return f"media type {media_type or '(none)'} cannot be displayed as {display}"
    if display == "image":
        if media_type == "image/webp":
            ok = payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
        else:
            ok = any(payload.startswith(signature) for signature in _IMAGE_SIGNATURES.get(media_type, ()))
        return "" if ok else f"artifact is not a valid {media_type} image"
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return f"{display} artifact is not UTF-8 text"
    if display == "json":
        try:
            json.loads(text)
        except ValueError:
            return "json artifact is not valid JSON"
    return ""


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _prune_review_cache(review_root: Path, current_side: Path) -> None:
    """Closer for the artifact cache, including the retired per-Snapshot layout."""

    from lemoncrow.pro.capabilities.review.web_preview import prune_cache_entries

    prune_cache_entries(
        review_root / _SIDES_DIR,
        current_side,
        keep_count=KEEP_SIDE_CACHES_PER_REVIEW,
        idle_seconds=_PRUNE_IDLE_SECONDS,
    )
    # Before artifacts were content-keyed, each Snapshot had its own directory here.
    prune_cache_entries(review_root, review_root / _SIDES_DIR, keep_count=1, idle_seconds=_PRUNE_IDLE_SECONDS)


def _result(
    context: SurfaceContext,
    descriptor: PreviewDescriptor,
    side: str,
    status: str,
    reason: str,
    **extra: Any,
) -> PreviewResult:
    return PreviewResult(
        status=status,
        reason=reason,
        revision_id=context.revision.id,
        side=side,
        renderer=descriptor.renderer,
        display=descriptor.display,
        **extra,
    )


def render_surface_preview(
    context: SurfaceContext,
    surface: ReviewSurface,
    *,
    side: str,
    renderers: PreviewRendererRegistry | None = None,
    source_repo: Path | None = None,
) -> PreviewResult:
    """Produce (or reuse) one revision-pinned preview artifact for one side.

    Never raises for plugin problems: an uninstalled, incompatible, crashing,
    slow or misbehaving renderer yields ``status`` ``unavailable``/``failed``
    with a specific ``reason``, leaving the rest of the Review untouched.
    """

    if side not in {"old", "new"}:
        raise ValueError("preview side must be old or new")
    descriptor = surface.preview
    if descriptor is None:
        raise ValueError(f"review surface declares no preview: {surface.provider}:{surface.id}")
    registry = renderers if renderers is not None else built_in_preview_registry()
    problem = preview_availability(surface, registry, context)
    if problem:
        return _result(context, descriptor, side, "unavailable", problem)
    if side not in descriptor.sides:
        return _result(context, descriptor, side, "unavailable", f"this preview has no {side} side")
    renderer = registry.get(descriptor.renderer)
    assert renderer is not None  # preview_availability proved it

    from lemoncrow.pro.capabilities.review.web_preview import (
        dependency_state,
        revision_side_identity,
        touch_cache_entry,
    )

    revision = context.revision
    config = _surface_config(context, surface)
    config_values = dict(config.values) if config is not None else {}
    limits = preview_limits(config)
    scope = _safe_relative_path(descriptor.scope, field_name="preview scope")
    trusted_checkout = (context.dependency_root or source_repo or context.repo_root).resolve()
    materialize_repo = source_repo or context.dependency_root or context.repo_root
    expected_identity = revision_side_identity(revision, side)
    review_root = context.store_root / "review" / "surface-previews" / revision.review_id
    # Keyed by content, not by Snapshot: every Snapshot whose side has this
    # content (usually the unchanged old side) reuses the workspace, any build a
    # renderer made inside it, and the rendered artifacts.
    side_root = (
        review_root
        / _SIDES_DIR
        / _digest({"identity": expected_identity, "renderer": descriptor.renderer, "side": side})
    )
    # Builds inside the workspace also depend on the checkout's dependencies and
    # on renderer config (e.g. a custom build command).
    workspace_key = _digest(
        {"config": config_values, "dependencies": dependency_state(trusted_checkout, scope), "scope": scope}
    )
    workspace = side_root / "workspaces" / workspace_key
    workspace_marker = side_root / "workspaces" / f"{workspace_key}.json"
    artifact_dir = (
        side_root
        / "artifacts"
        / _digest(
            {
                "descriptor": descriptor.to_payload(),
                "limits": [limits.timeout_seconds, limits.max_bytes],
                "provider": surface.provider,
                "surface": surface.id,
                "workspace": workspace_key,
            }
        )
    )
    artifact_marker = artifact_dir / "artifact.json"
    output_dir = artifact_dir / "output"
    touch_cache_entry(side_root)

    cached = _cached_result(context, descriptor, side, artifact_marker, output_dir, limits, expected_identity)
    if cached is not None:
        return cached

    with _lock(str(workspace)), RunFileLock(workspace_marker):
        cached = _cached_result(context, descriptor, side, artifact_marker, output_dir, limits, expected_identity)
        if cached is not None:
            return cached
        try:
            identity = _ensure_workspace(
                context, materialize_repo, side, scope, workspace, workspace_marker, expected_identity
            )
        except PreviewUnavailable as exc:
            return _result(context, descriptor, side, "unavailable", str(exc))
        artifact_marker.unlink(missing_ok=True)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        request = PreviewRenderRequest(
            context=context,
            surface=surface,
            descriptor=descriptor,
            side=side,
            identity=identity,
            workspace=workspace,
            output_dir=output_dir,
            limits=limits,
            sandbox=PreviewSandbox(
                workspace=workspace,
                output_dir=output_dir,
                trusted_checkout=trusted_checkout,
                deadline=time.monotonic() + limits.timeout_seconds,
            ),
            config=config,
        )
        try:
            artifact = renderer.render(request)
        except PreviewUnavailable as exc:
            return _result(context, descriptor, side, "unavailable", str(exc), identity=identity)
        except Exception as exc:
            return _result(
                context,
                descriptor,
                side,
                "failed",
                f"preview renderer {descriptor.renderer!r} raised {type(exc).__name__}: {exc}",
                identity=identity,
            )
        result = _accept_artifact(context, descriptor, surface, side, artifact, output_dir, limits, identity)
        if result.status == "ready" and result.path is not None:
            _write_json_atomic(
                artifact_marker,
                {
                    "contract_version": PREVIEW_CONTRACT_VERSION,
                    "file": result.path.relative_to(output_dir).as_posix(),
                    "filename": result.filename,
                    "identity": identity,
                    "media_type": result.media_type,
                    "renderer": descriptor.renderer,
                    "revision_id": revision.id,
                    "side": side,
                    "summary": result.summary,
                },
            )
    _prune_review_cache(review_root, side_root)
    return result


def _ensure_workspace(
    context: SurfaceContext,
    repo: Path,
    side: str,
    scope: str,
    workspace: Path,
    marker: Path,
    expected_identity: str,
) -> str:
    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side

    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        # Reuse only content that materialized as exactly the identity this
        # directory is keyed by (a dirty submodule, for example, never does).
        if data.get("identity") == expected_identity and data.get("side") == side and workspace.is_dir():
            return expected_identity
    except (OSError, TypeError, ValueError):
        pass
    marker.unlink(missing_ok=True)
    if workspace.exists():
        shutil.rmtree(workspace)
    try:
        identity = materialize_review_side(
            context.store_root,
            repo,
            context.revision,
            side=side,
            target=workspace,
            scope=scope,
            store=context.store,
        )
    except ReviewSnapshotUnavailable as exc:
        raise PreviewUnavailable(f"exact Snapshot {side} side is unavailable: {exc}") from exc
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise PreviewUnavailable(f"exact Snapshot {side} side could not be materialized: {exc}") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(marker, {"identity": identity, "revision_id": context.revision.id, "scope": scope, "side": side})
    return identity


def _cached_result(
    context: SurfaceContext,
    descriptor: PreviewDescriptor,
    side: str,
    marker: Path,
    output_dir: Path,
    limits: PreviewLimits,
    expected_identity: str,
) -> PreviewResult | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if data.get("identity") != expected_identity or data.get("side") != side:
        return None
    path = (output_dir / str(data.get("file") or "")).resolve()
    root = output_dir.resolve()
    try:
        if root not in path.parents or not path.is_file() or path.stat().st_size > limits.max_bytes:
            return None
    except OSError:
        return None
    return _result(
        context,
        descriptor,
        side,
        "ready",
        "",
        media_type=str(data.get("media_type") or ""),
        filename=str(data.get("filename") or path.name),
        path=path,
        summary=str(data.get("summary") or ""),
        identity=str(data.get("identity") or ""),
        cached=True,
    )


def _accept_artifact(
    context: SurfaceContext,
    descriptor: PreviewDescriptor,
    surface: ReviewSurface,
    side: str,
    artifact: object,
    output_dir: Path,
    limits: PreviewLimits,
    identity: str,
) -> PreviewResult:
    label = f"preview renderer {descriptor.renderer!r}"
    if not isinstance(artifact, PreviewArtifact):
        return _result(context, descriptor, side, "failed", f"{label} returned no PreviewArtifact", identity=identity)
    if artifact.status in {"unavailable", "failed"}:
        reason = artifact.reason or f"{label} reported the {side} side {artifact.status}"
        return _result(
            context, descriptor, side, artifact.status, reason, output=_tail(artifact.output), identity=identity
        )
    if artifact.status != "ready" or artifact.path is None:
        return _result(context, descriptor, side, "failed", f"{label} returned no artifact file", identity=identity)
    root = output_dir.resolve()
    resolved = Path(artifact.path).resolve()
    if resolved == root or root not in resolved.parents:
        return _result(
            context,
            descriptor,
            side,
            "failed",
            f"{label} wrote its artifact outside the preview output directory",
            identity=identity,
        )
    if not resolved.is_file():
        return _result(context, descriptor, side, "failed", f"{label} artifact file does not exist", identity=identity)
    size = resolved.stat().st_size
    if size > limits.max_bytes:
        return _result(
            context,
            descriptor,
            side,
            "failed",
            f"{label} artifact is {size} bytes; the limit is {limits.max_bytes}",
            identity=identity,
        )
    media_type = _normalized_media_type(artifact.media_type or descriptor.media_type)
    problem = _validate_bytes(descriptor.display, media_type, resolved.read_bytes())
    if problem:
        return _result(context, descriptor, side, "failed", f"{label}: {problem}", identity=identity)
    fallback = f"{surface.id}-{side}{PurePosixPath(resolved.name).suffix}"
    filename = _FILENAME_RE.sub("-", artifact.filename or fallback).strip(".-") or f"preview-{side}"
    return _result(
        context,
        descriptor,
        side,
        "ready",
        "",
        media_type=media_type,
        filename=filename,
        path=resolved,
        summary=artifact.summary,
        identity=identity,
    )


__all__ = [
    "DEFAULT_PREVIEW_SECONDS",
    "MAX_PREVIEW_BYTES",
    "MAX_PREVIEW_SECONDS",
    "PREVIEW_RENDERER_ENTRYPOINT_GROUP",
    "PreviewArtifact",
    "PreviewLimits",
    "PreviewRenderRequest",
    "PreviewRendererRegistry",
    "PreviewResult",
    "PreviewSandbox",
    "PreviewUnavailable",
    "ReviewPreviewRenderer",
    "SandboxResult",
    "built_in_preview_registry",
    "load_entrypoint_renderers_isolated",
    "preview_availability",
    "preview_limits",
    "render_surface_preview",
    "renderer_problem",
]
