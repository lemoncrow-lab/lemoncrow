"""Deep rendering diffs: DOM/accessibility, network/console, layout and a bounded matrix (Phase 9R item 13).

Two plan entries share this pipeline:

* ``visual.dom_a11y`` -- every affected route (and scripted flow) at the
  default viewport and theme;
* ``visual.matrix`` -- the viewport x theme matrix of the affected routes,
  bounded by ``render.max_cells`` (depth) and by the entry's budget; dropped
  cells are listed and the result says *Limited by budget*.

Routes: only routes affected by the change render when the component->route
mapping is known (trusted-base ``render.component_routes``, a static site's
own HTML files, or the web preview's import-reachability routes); when it is
not, the configured ``render.routes`` render and the result says so.

Each side renders in the sandboxed driver (:mod:`render_driver`) under fixed
determinism controls; the browser version, the host font set, the controls
and the render configuration enter the R4 environment fingerprint. Base and
head compare only under one fingerprint, otherwise the pair is *not
comparable* and yields no delta (never a regression). Screenshots are masked,
and DOM/AX snapshots, URLs and console text are redacted, *before* they are
stored as raw-retention run artifacts. Differences become ``visual`` behavior
deltas with the evidence (records, screenshots, snapshots) before judgment.

The driver is local by default; a deployment whose worker image can build and
render supplies one through ``RunnerRegistry.render_executor``. A side that did
not render makes the pair not comparable, whatever the reason.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import math
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    BehaviorProduction,
    BehaviorSeverity,
    BehaviorSide,
    ExecutedScope,
    ExecutorClass,
)
from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, build_execution_result, workspace_digest
from lemoncrow.pro.capabilities.review.render_driver import (
    MAX_STEPS,
    DeterminismControls,
    InteractionStep,
    PageCapture,
    RenderCell,
    RenderDriver,
    RenderEnvironment,
    Viewport,
    screenshot_identity,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    MaskingUnavailable,
    MaskRegion,
    ProtectedBytes,
    RunArtifactStore,
    mask_png,
    protect_text,
)
from lemoncrow.pro.capabilities.review.runtime_observation import read_record
from lemoncrow.pro.capabilities.review.runtime_validation import execution_verification, snapshot_semantic_identity
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.store import new_evidence_id
from lemoncrow.pro.environment.execution import (
    ExecutionRecipe,
    ExecutionResult,
    ExecutionSubject,
    StageOutcome,
)
from lemoncrow.pro.environment.execution import ExecutorClass as RunExecutorClass

RENDER_DIFF_MEDIA_TYPE: Final[str] = "application/vnd.lemoncrow.render-diff+json;version=1"
RENDER_DIFF_PRODUCER_ID: Final[str] = "lemoncrow.render_diff"
RENDER_VERSION: Final[int] = 1
RENDER_ENTRIES: Final[tuple[str, ...]] = ("visual.dom_a11y", "visual.matrix")
RenderEntry = Literal["visual.dom_a11y", "visual.matrix"]

MAX_ROUTES: Final[int] = 50
MAX_VIEWPORTS: Final[int] = 6
MAX_INTERACTIONS: Final[int] = 10
MAX_MASKS: Final[int] = 32
MAX_CELLS: Final[int] = 96
DEFAULT_MAX_CELLS: Final[int] = 24
DEFAULT_SECONDS_PER_CELL: Final[float] = 4.0
CLS_TOLERANCE: Final[float] = 0.05
_MAX_SAMPLE: Final[int] = 40
_THEMES: Final[frozenset[str]] = frozenset({"light", "dark", "no-preference"})
_SOURCE: Final = "test"
_SIDE_NAME: Final[Mapping[str, str]] = {"old": "base", "new": "head"}
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DEFAULT_VIEWPORT: Final[Viewport] = Viewport("desktop", 1280, 800)


class RenderRunSuperseded(ValueError):
    """The Snapshot advanced during the run; nothing more is published as current."""


# --------------------------------------------------------------------------- #
# trusted-base configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Interaction:
    id: str
    route: str
    steps: tuple[InteractionStep, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "route": self.route, "steps": [step.to_wire() for step in self.steps]}


@dataclass(frozen=True, slots=True)
class RenderConfig:
    """The trusted-base ``render:`` block (never head-supplied)."""

    static_root: str = ""
    routes: tuple[str, ...] = ("/",)
    component_routes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    viewports: tuple[Viewport, ...] = (_DEFAULT_VIEWPORT,)
    themes: tuple[str, ...] = ("light",)
    interactions: tuple[Interaction, ...] = ()
    controls: DeterminismControls = field(default_factory=DeterminismControls)
    max_cells: int = DEFAULT_MAX_CELLS
    spa: bool = False
    seconds_per_cell: float = DEFAULT_SECONDS_PER_CELL
    errors: tuple[str, ...] = ()
    configured: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "static_root": self.static_root,
            "routes": list(self.routes),
            "component_routes": [[glob, list(routes)] for glob, routes in self.component_routes],
            "viewports": [item.to_wire() for item in self.viewports],
            "themes": list(self.themes),
            "interactions": [item.to_wire() for item in self.interactions],
            "controls": self.controls.to_wire(),
            "max_cells": self.max_cells,
            "spa": self.spa,
            "seconds_per_cell": self.seconds_per_cell,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_wire(), sort_keys=True).encode()).hexdigest()


def _route(value: object) -> str:
    text = str(value or "").strip()
    if not text.startswith("/"):
        text = "/" + text
    return text if _ROUTE_RE.match(text) and ".." not in text.split("/") and len(text) <= 300 else ""


def _relative(value: object) -> str:
    text = str(value or "").strip().strip("/")
    rel = PurePosixPath(text)
    if not text or rel.is_absolute() or ".." in rel.parts:
        return ""
    return rel.as_posix()


def parse_render_config(raw: object) -> RenderConfig:
    """Bounded, validated ``render:`` block; invalid parts are reported and dropped."""

    if not isinstance(raw, Mapping):
        return RenderConfig()
    errors: list[str] = []
    static_root = _relative(raw.get("static_root")) if raw.get("static_root") else ""
    if raw.get("static_root") and not static_root:
        errors.append("render.static_root must be a repository-relative directory")
    routes = [r for r in (_route(item) for item in _list(raw.get("routes"))) if r][:MAX_ROUTES]
    mapping: list[tuple[str, tuple[str, ...]]] = []
    component_routes = raw.get("component_routes")
    if isinstance(component_routes, Mapping):
        for glob, targets in list(component_routes.items())[:200]:
            mapped = tuple(r for r in (_route(item) for item in _list(targets)) if r)[:MAX_ROUTES]
            if str(glob).strip():
                mapping.append((str(glob).strip()[:300], mapped))
    viewports: list[Viewport] = []
    for item in _list(raw.get("viewports"))[:MAX_VIEWPORTS]:
        if not isinstance(item, Mapping):
            continue
        try:
            width, height = int(item.get("width") or 0), int(item.get("height") or 0)
        except (TypeError, ValueError):
            continue
        ident = str(item.get("id") or f"{width}x{height}")
        if _ID_RE.match(ident) and 240 <= width <= 3840 and 240 <= height <= 4000:
            viewports.append(Viewport(ident, width, height))
        else:
            errors.append(f"render viewport {ident!r} is invalid")
    themes = [str(item) for item in _list(raw.get("themes")) if str(item) in _THEMES][:3]
    interactions: list[Interaction] = []
    for item in _list(raw.get("interactions"))[:MAX_INTERACTIONS]:
        if not isinstance(item, Mapping):
            continue
        ident, route = str(item.get("id") or ""), _route(item.get("route") or "/")
        steps = tuple(step for step in (_step(s) for s in _list(item.get("steps"))[:MAX_STEPS]) if step is not None)
        if _ID_RE.match(ident) and route and steps:
            interactions.append(Interaction(ident, route, steps))
        else:
            errors.append(f"render interaction {ident or '?'} is invalid")
    masks = tuple(str(item)[:200] for item in _list(raw.get("mask")) if str(item).strip())[:MAX_MASKS]
    clock_ms = DeterminismControls().clock_ms
    if raw.get("clock"):
        try:
            clock_ms = int(datetime.fromisoformat(str(raw.get("clock")).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            errors.append("render.clock must be an ISO-8601 timestamp")
    locale = str(raw.get("locale") or "en-US")
    timezone = str(raw.get("timezone") or "UTC")
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", locale):
        errors.append("render.locale is invalid")
        locale = "en-US"
    if not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", timezone):
        errors.append("render.timezone is invalid")
        timezone = "UTC"
    try:
        seed = int(raw.get("seed") or 1) & 0xFFFFFFFF
        max_cells = max(1, min(int(raw.get("max_cells") or DEFAULT_MAX_CELLS), MAX_CELLS))
        per_cell = max(0.5, min(float(raw.get("seconds_per_cell") or DEFAULT_SECONDS_PER_CELL), 120.0))
    except (TypeError, ValueError):
        errors.append("render seed/max_cells/seconds_per_cell must be numbers")
        seed, max_cells, per_cell = 1, DEFAULT_MAX_CELLS, DEFAULT_SECONDS_PER_CELL
    return RenderConfig(
        static_root=static_root,
        routes=tuple(dict.fromkeys(routes)) or ("/",),
        component_routes=tuple(mapping),
        viewports=tuple(viewports) or (_DEFAULT_VIEWPORT,),
        themes=tuple(dict.fromkeys(themes)) or ("light",),
        interactions=tuple(interactions),
        controls=DeterminismControls(
            locale=locale, timezone=timezone, clock_ms=clock_ms, seed=seed, mask_selectors=masks
        ),
        max_cells=max_cells,
        spa=raw.get("spa") is True,
        seconds_per_cell=per_cell,
        errors=tuple(errors),
        configured=True,
    )


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _step(raw: object) -> InteractionStep | None:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        return None
    kind, value = next(iter(raw.items()))
    if kind in {"click", "hover"}:
        return InteractionStep(cast(Any, kind), selector=str(value)[:200])
    if kind == "type" and isinstance(value, Mapping):
        return InteractionStep(
            "type", selector=str(value.get("selector") or "")[:200], value=str(value.get("text") or "")[:500]
        )
    if kind == "press":
        return InteractionStep("press", value=str(value)[:32])
    if kind == "wait":
        try:
            return InteractionStep("wait", value=str(max(0, min(int(value), 5000))))
        except (TypeError, ValueError):
            return None
    return None


def render_config_from_review(content: str | bytes | None) -> RenderConfig:
    """The ``render:`` block of the trusted-base ``.lemoncrow/review.yaml`` text."""

    import yaml

    if not content:
        return RenderConfig()
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return RenderConfig(errors=("trusted-base review config is not valid YAML",))
    return parse_render_config(loaded.get("render") if isinstance(loaded, Mapping) else None)


# --------------------------------------------------------------------------- #
# route selection and the cell plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RouteSelection:
    routes: tuple[str, ...]
    mapping: Literal["known", "unknown", "none"]
    note: str = ""
    unmapped: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "routes": list(self.routes),
            "mapping": self.mapping,
            "note": self.note,
            "unmapped": list(self.unmapped),
        }


def _static_route(static_root: str, path: str) -> str | None:
    """The route a static-site HTML file serves, ``None`` for a non-page file under the root."""

    rel = PurePosixPath(path).relative_to(static_root).as_posix()
    if not rel.endswith(".html"):
        return None
    if rel == "index.html":
        return "/"
    if rel.endswith("/index.html"):
        return "/" + rel[: -len("index.html")]
    return "/" + rel


def select_routes(
    config: RenderConfig,
    changed_paths: Sequence[str],
    *,
    infer: Callable[[str], tuple[str, ...] | None] | None = None,
) -> RouteSelection:
    """Affected routes when the component->route mapping is known; the configured set otherwise."""

    from lemoncrow.pro.capabilities.review.web_preview import is_web_preview_path

    affected: list[str] = []
    unmapped: list[str] = []
    relevant = 0
    for path in changed_paths:
        mapped: tuple[str, ...] | None = None
        explicit = [routes for glob, routes in config.component_routes if fnmatch.fnmatchcase(path, glob)]
        if explicit:
            mapped = tuple(route for routes in explicit for route in routes)
        elif config.static_root and (path == config.static_root or path.startswith(config.static_root + "/")):
            route = _static_route(config.static_root, path) if path != config.static_root else None
            mapped = (route,) if route else None
        elif is_web_preview_path(path) or path.endswith(".html"):
            mapped = infer(path) if infer is not None else None
        else:
            continue  # not part of any rendered surface
        relevant += 1
        if mapped is None:
            unmapped.append(path)
        else:
            affected.extend(mapped)
    known = tuple(dict.fromkeys(route for route in affected if route))[:MAX_ROUTES]
    if unmapped:
        routes = tuple(dict.fromkeys((*known, *config.routes)))[:MAX_ROUTES]
        shown = ", ".join(unmapped[:5]) + (" ..." if len(unmapped) > 5 else "")
        note = (
            f"The component->route mapping is unknown for {shown}; the configured routes rendered "
            f"({', '.join(config.routes)})"
        )
        return RouteSelection(routes, "unknown", note, tuple(unmapped[:50]))
    if not relevant:
        return RouteSelection((), "none", "No changed file feeds a rendered page; nothing to render")
    return RouteSelection(known, "known", f"Rendered only the {len(known)} affected route(s)")


@dataclass(frozen=True, slots=True)
class CellPlan:
    cells: tuple[RenderCell, ...]
    dropped: tuple[str, ...] = ()
    limited_by_depth: bool = False
    limited_by_budget: bool = False
    estimate_seconds: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "cells": [cell.to_wire() for cell in self.cells],
            "dropped": list(self.dropped),
            "limited_by_depth": self.limited_by_depth,
            "limited_by_budget": self.limited_by_budget,
            "estimate_seconds": round(self.estimate_seconds, 3),
        }


def plan_cells(
    config: RenderConfig,
    selection: RouteSelection,
    *,
    entry: RenderEntry,
    budget_seconds: float | None,
    sides: int = 2,
) -> CellPlan:
    """The cells to capture: depth sets the matrix, ``max_cells`` and the budget bound it."""

    default_viewport, default_theme = config.viewports[0], config.themes[0]
    flows = [item for item in config.interactions if selection.mapping == "unknown" or item.route in selection.routes]
    wanted: list[RenderCell] = []
    for route in selection.routes:
        if entry == "visual.matrix":
            wanted.extend(
                RenderCell(route, viewport, theme) for viewport in config.viewports for theme in config.themes
            )
        else:
            wanted.append(RenderCell(route, default_viewport, default_theme))
    wanted.extend(RenderCell(item.route, default_viewport, default_theme, item.id, item.steps) for item in flows)
    depth_cap = config.max_cells if entry == "visual.matrix" else min(config.max_cells, MAX_ROUTES + MAX_INTERACTIONS)
    limited_by_depth = len(wanted) > depth_cap
    kept = wanted[:depth_cap]
    limited_by_budget = False
    if budget_seconds is not None:
        affordable = int(max(0.0, budget_seconds) // (config.seconds_per_cell * max(1, sides)))
        if affordable < len(kept):
            limited_by_budget = True
            kept = kept[: max(0, affordable)]
    dropped = tuple(cell.id for cell in wanted[len(kept) :])
    return CellPlan(
        cells=tuple(kept),
        dropped=dropped,
        limited_by_depth=limited_by_depth,
        limited_by_budget=limited_by_budget,
        estimate_seconds=len(kept) * config.seconds_per_cell * max(1, sides),
    )


# --------------------------------------------------------------------------- #
# protected page snapshots
# --------------------------------------------------------------------------- #


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProtectedPage:
    """One capture after masking/redaction; only this is stored or compared."""

    cell: RenderCell
    status: Literal["captured", "failed"]
    reason: str = ""
    screenshot_digest: str = ""
    screenshot_ref: str = ""
    screenshot_withheld: str = ""
    masked_regions: int = 0
    dom: tuple[str, ...] = ()
    dom_ref: str = ""
    ax: tuple[str, ...] = ()
    ax_ref: str = ""
    network: tuple[str, ...] = ()
    console: tuple[str, ...] = ()
    cls: float = 0.0
    lcp_ms: float = 0.0
    metrics: Mapping[str, float] = field(default_factory=dict)
    truncated: bool = False

    @property
    def digest(self) -> str:
        """Visual identity: screenshot, DOM, AX, network and console (performance excluded)."""

        return "page:sha256:" + _digest(
            {
                "status": self.status,
                "screenshot": self.screenshot_digest,
                "dom": _digest(self.dom),
                "ax": _digest(self.ax),
                "network": sorted(self.network),
                "console": sorted(self.console),
            }
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "cell": self.cell.to_wire(),
            "status": self.status,
            "reason": self.reason,
            "screenshot": {
                "digest": self.screenshot_digest,
                "ref": self.screenshot_ref,
                "withheld": self.screenshot_withheld,
                "masked_regions": self.masked_regions,
            },
            "dom": {"digest": _digest(self.dom), "ref": self.dom_ref, "lines": len(self.dom)},
            "ax": {"digest": _digest(self.ax), "ref": self.ax_ref, "lines": len(self.ax)},
            "network": list(self.network),
            "console": list(self.console),
            "layout_shift": round(self.cls, 4),
            "lcp_ms": round(self.lcp_ms, 1),
            "metrics": {key: round(value, 4) for key, value in self.metrics.items()},
            "truncated": self.truncated,
            "digest": self.digest,
        }


_EPHEMERAL_RE = re.compile(r"(?<=[?&])(_|t|ts|v|cb|cachebust)=\d+")


def protect_page(
    capture: PageCapture,
    cell: RenderCell,
    *,
    review_id: str,
    artifacts: RunArtifactStore | None,
    policy: Any,
) -> ProtectedPage:
    """Mask the screenshot and redact every text before anything is stored (fail closed)."""

    if capture.status != "captured":
        return ProtectedPage(cell, "failed", protect_text(capture.reason, policy)[0][:300])
    shot_digest, shot_ref, withheld = "", "", ""
    if capture.screenshot:
        regions = [MaskRegion(x, y, w, h) for x, y, w, h in capture.mask_regions]
        try:
            protected = mask_png(capture.screenshot, regions)
        except MaskingUnavailable as exc:
            withheld = str(exc)[:200]
        else:
            shot_digest = screenshot_identity(protected)
            if artifacts is not None:
                shot_ref = artifacts.put(review_id, protected, role="screenshot", media_type="image/png").ref
    else:
        withheld = "no screenshot was captured"
    dom = tuple(protect_text(line, policy)[0] for line in capture.dom_lines)
    ax = tuple(protect_text(line, policy)[0] for line in capture.ax_lines)
    network = tuple(sorted({_EPHEMERAL_RE.sub(r"\1=#", protect_text(row, policy)[0]) for row in capture.network}))
    console = tuple(sorted({protect_text(row, policy)[0] for row in capture.console}))
    dom_ref = ax_ref = ""
    if artifacts is not None:
        dom_ref = artifacts.put(
            review_id, ProtectedBytes("\n".join(dom).encode()), role="dom_snapshot", media_type="text/plain"
        ).ref
        ax_ref = artifacts.put(
            review_id, ProtectedBytes("\n".join(ax).encode()), role="ax_snapshot", media_type="text/plain"
        ).ref
    return ProtectedPage(
        cell,
        "captured",
        screenshot_digest=shot_digest,
        screenshot_ref=shot_ref,
        screenshot_withheld=withheld,
        masked_regions=len(capture.mask_regions),
        dom=dom,
        dom_ref=dom_ref,
        ax=ax,
        ax_ref=ax_ref,
        network=network,
        console=console,
        cls=capture.cls,
        lcp_ms=capture.lcp_ms,
        metrics=dict(capture.metrics),
        truncated=capture.dom_truncated or capture.ax_truncated,
    )


# --------------------------------------------------------------------------- #
# base vs head
# --------------------------------------------------------------------------- #

_AX_LINE_RE = re.compile(r'^(?P<indent> *)(?P<role>\S+)(?: (?P<name>"(?:[^"\\]|\\.)*"))?(?: \[(?P<props>[^\]]*)\])?$')


def _ax_row(line: str) -> tuple[int, str, str, str]:
    match = _AX_LINE_RE.match(line)
    if match is None:
        return (0, line.strip(), "", "")
    name = match.group("name") or ""
    try:
        name = str(json.loads(name)) if name else ""
    except ValueError:
        pass
    return (len(match.group("indent")) // 2, match.group("role"), name, match.group("props") or "")


def ax_changes(base: Sequence[str], head: Sequence[str], *, limit: int = _MAX_SAMPLE) -> list[dict[str, Any]]:
    """Accessibility-tree changes: changed role, changed accessible name (label), state, added, removed."""

    left, right = [_ax_row(line) for line in base], [_ax_row(line) for line in head]
    matcher = difflib.SequenceMatcher(a=[row[:2] for row in left], b=[row[:2] for row in right], autojunk=False)
    out: list[dict[str, Any]] = []

    def push(kind: str, before: tuple[int, str, str, str] | None, after: tuple[int, str, str, str] | None) -> None:
        if len(out) < limit:
            out.append(
                {
                    "kind": kind,
                    "base": None if before is None else {"role": before[1], "name": before[2], "state": before[3]},
                    "head": None if after is None else {"role": after[1], "name": after[2], "state": after[3]},
                }
            )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for before, after in zip(left[i1:i2], right[j1:j2], strict=True):
                if before[2] != after[2]:
                    push("label", before, after)
                elif before[3] != after[3]:
                    push("state", before, after)
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for before, after in zip(left[i1:i2], right[j1:j2], strict=True):
                push("role" if before[1] != after[1] else "label", before, after)
        else:
            for before in left[i1:i2]:
                push("removed", before, None)
            for after in right[j1:j2]:
                push("added", None, after)
    return out


def line_changes(base: Sequence[str], head: Sequence[str], *, limit: int = _MAX_SAMPLE) -> dict[str, Any]:
    added = removed = 0
    sample: list[str] = []
    for line in difflib.unified_diff(list(base), list(head), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        if len(sample) < limit:
            sample.append(line[:300])
    return {"added": added, "removed": removed, "sample": sample}


def page_changes(base: ProtectedPage, head: ProtectedPage) -> dict[str, Any]:
    """Only what differs between one cell's base and head captures."""

    changes: dict[str, Any] = {}
    if base.status != "captured" or head.status != "captured":
        if base.status != head.status:
            changes["status"] = {"base": base.status, "head": head.status, "reason": head.reason or base.reason}
        return changes
    if base.screenshot_digest and head.screenshot_digest and base.screenshot_digest != head.screenshot_digest:
        changes["screenshot"] = {
            "base": {"digest": base.screenshot_digest, "ref": base.screenshot_ref},
            "head": {"digest": head.screenshot_digest, "ref": head.screenshot_ref},
        }
    if base.dom != head.dom:
        changes["dom"] = {
            **line_changes(base.dom, head.dom),
            "base": {"digest": _digest(base.dom), "ref": base.dom_ref},
            "head": {"digest": _digest(head.dom), "ref": head.dom_ref},
        }
    if base.ax != head.ax:
        changes["a11y"] = {
            "changes": ax_changes(base.ax, head.ax),
            "base": {"digest": _digest(base.ax), "ref": base.ax_ref},
            "head": {"digest": _digest(head.ax), "ref": head.ax_ref},
        }
    for name, before, after in (("network", base.network, head.network), ("console", base.console, head.console)):
        added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
        if added or removed:
            changes[name] = {"added": added[:_MAX_SAMPLE], "removed": removed[:_MAX_SAMPLE]}
    if abs(head.cls - base.cls) >= CLS_TOLERANCE:
        changes["layout_shift"] = {"base": round(base.cls, 4), "head": round(head.cls, 4)}
    before_task, after_task = base.metrics.get("TaskDuration", 0.0), head.metrics.get("TaskDuration", 0.0)
    if after_task > 2 * before_task and after_task - before_task > 0.2:
        changes["performance"] = {
            "base": {key: round(value, 4) for key, value in base.metrics.items()},
            "head": {key: round(value, 4) for key, value in head.metrics.items()},
            "note": "main-thread task time more than doubled (tolerance 200 ms)",
        }
    return changes


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

SiteResolver = Callable[[str, Path], tuple[Path, bool, str]]
"""``(side, work dir) -> (site directory, spa fallback, scope)``; raises ``RenderUnavailable``."""
Meter = Callable[[Mapping[str, Any] | None, float], None]


class RenderUnavailable(RuntimeError):
    """A side cannot be rendered exactly and safely; the result says why (never a pass)."""


@dataclass(frozen=True, slots=True)
class RenderRunInputs:
    store: Any
    revision: ReviewRevision
    entry_id: RenderEntry
    config: RenderConfig
    selection: RouteSelection
    plan: CellPlan
    driver: RenderDriver
    site: SiteResolver
    artifacts: RunArtifactStore | None
    executor_class: RunExecutorClass = RunExecutorClass.AUTHOR_MACHINE
    policy: HermeticPolicy = field(default_factory=HermeticPolicy)
    policy_digest: str = ""
    expected_current_revision_id: str = ""
    execution_intent: str = ""
    timeout: float = 300.0


def _identity(inputs: RenderRunInputs, side: str, environment: RenderEnvironment) -> str:
    return _digest(
        {
            "v": RENDER_VERSION,
            "entry": inputs.entry_id,
            "revision": inputs.revision.id,
            "snapshot": snapshot_semantic_identity(inputs.revision),
            "side": side,
            "config": inputs.config.digest,
            "cells": [cell.id for cell in inputs.plan.cells],
            "browser": environment.browser,
            "fonts": environment.fonts_digest,
            "executor": inputs.executor_class.value,
            "policy": inputs.policy_digest,
            "intent": inputs.execution_intent,
        }
    )


def _execution(
    inputs: RenderRunInputs,
    side: str,
    *,
    environment: RenderEnvironment,
    scope: str,
    tree: str,
    status: Literal["passed", "failed", "unavailable"],
    detail: str,
    duration_ms: int,
    prior_stages: tuple[StageOutcome, ...] = (),
) -> ExecutionResult:
    from lemoncrow.pro.capabilities.review.runner_host import revision_snapshot_identity
    from lemoncrow.pro.environment.execution import cache_namespace

    controls = inputs.config.controls
    return build_execution_result(
        subject=ExecutionSubject(
            review_id=inputs.revision.review_id,
            revision_id=inputs.revision.id,
            side=side,
            snapshot_identity=revision_snapshot_identity(inputs.revision, side),
            scope=scope or ".",
            workspace_digest=tree,
        ),
        executor_class=inputs.executor_class,
        policy=inputs.policy,
        recipe=ExecutionRecipe(
            command=("lemoncrow-render", inputs.entry_id),
            cwd=scope or ".",
            env=(
                ("LANG", controls.locale),
                ("TZ", controls.timezone),
                ("LEMONCROW_RENDER_CONFIG", inputs.config.digest),
                ("LEMONCROW_RENDER_CONTROLS", controls.digest),
            ),
            stages=cast(Any, tuple(getattr(inputs.driver, "recipe_stages", ("run",)))),
            sinks=False,
            policy=inputs.policy.to_wire(),
        ),
        stages=(
            *prior_stages,
            StageOutcome(
                "run",
                status,
                network="denied",
                detail=detail[:500],
                duration_ms=max(0, duration_ms),
            ),
        ),
        namespace=cache_namespace(side, "unknown"),
        content_access="ai_denied",
        toolchain=(("browser", environment.browser or "absent"), ("fonts", environment.fonts_digest or "absent")),
        sandbox=environment.sandbox,
        image_digest=environment.image_digest,
    )


class _Run:
    def __init__(self, inputs: RenderRunInputs, meter: Meter) -> None:
        self.inputs = inputs
        self.meter = meter
        self.executions = 0
        self.reused = 0

    def _publish(self, record: Mapping[str, Any], *, title: str, source_ref: str, detail: str) -> ReviewEvidence:
        inputs = self.inputs
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        evidence_id = new_evidence_id()
        rel, content_hash, size = inputs.store.write_evidence_artifact(
            inputs.revision.review_id, inputs.revision.id, evidence_id, payload
        )
        try:
            added: ReviewEvidence = inputs.store.add_evidence(
                ReviewEvidence(
                    id=evidence_id,
                    review_id=inputs.revision.review_id,
                    revision_id=inputs.revision.id,
                    kind="verification",
                    title=title[:200],
                    artifact_path=rel,
                    content_hash=content_hash,
                    mime_type=RENDER_DIFF_MEDIA_TYPE,
                    bytes=size,
                    source=_SOURCE,
                    source_ref=source_ref,
                    detail=detail[:1200],
                    # Raw observation: no verification status and no stable key, so the screenshots
                    # and snapshots it references follow raw retention unless a judgment cites them.
                    execution_intent=inputs.execution_intent,
                    executor_class=inputs.executor_class.value,
                    execution_verification=execution_verification(record),
                ),
                expected_current_revision_id=inputs.expected_current_revision_id,
            )
        except ValueError as exc:
            inputs.store.discard_artifact(rel)
            raise RenderRunSuperseded("render revision was superseded before publication") from exc
        except Exception:
            inputs.store.discard_artifact(rel)
            raise
        return added

    def side(
        self, side: str, environment: RenderEnvironment
    ) -> tuple[ReviewEvidence, dict[str, Any], list[ProtectedPage]]:
        inputs = self.inputs
        identity = _identity(inputs, side, environment)
        source_ref = f"render:{inputs.entry_id}:{identity}"
        existing: ReviewEvidence | None = inputs.store.get_evidence_by_source(
            inputs.revision.review_id, _SOURCE, source_ref
        )
        if existing is not None and existing.revision_id == inputs.revision.id:
            stored = read_record(inputs.store.read_evidence_artifact(existing), existing.content_hash)
            pages = _pages_from_record(stored, inputs.plan.cells) if stored is not None else None
            if stored is not None and stored.get("identity") == identity and pages is not None:
                self.reused += 1
                return existing, stored, pages
        started = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix="lemoncrow-render-site-"))
        scope, tree = ".", ""
        try:
            try:
                site, spa, scope = inputs.site(side, work)
                tree = workspace_digest(site)
                captures = inputs.driver.capture(
                    site, inputs.plan.cells, inputs.config.controls, spa_fallback=spa, timeout=inputs.timeout
                )
                reason = ""
            except RenderUnavailable as exc:
                captures, reason = [PageCapture(cell.id, "failed", str(exc)[:300]) for cell in inputs.plan.cells], str(
                    exc
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        wall = time.monotonic() - started
        by_id = {capture.cell_id: capture for capture in captures}
        pages = [
            protect_page(
                by_id.get(cell.id) or PageCapture(cell.id, "failed", "the driver returned no capture"),
                cell,
                review_id=inputs.revision.review_id,
                artifacts=inputs.artifacts,
                policy=inputs.policy.redaction,
            )
            for cell in inputs.plan.cells
        ]
        captured = sum(1 for page in pages if page.status == "captured")
        status: Literal["passed", "failed", "unavailable"] = (
            "unavailable" if reason or not captured else "passed" if captured == len(pages) else "failed"
        )
        detail = reason or f"{captured}/{len(pages)} cell(s) captured"
        execution = _execution(
            inputs,
            side,
            environment=environment,
            scope=scope,
            tree=tree,
            status=status,
            detail=detail,
            # Rounded up: a render that happened is a run, however fast (compute skips zero-time stages).
            duration_ms=math.ceil(wall * 1000),
            # What a remote executor did before rendering (dependency fetch, app build).
            prior_stages=tuple(getattr(inputs.driver, "stages", lambda _side: ())(side)),
        )
        self.executions += 1
        wire = execution.to_wire()
        self.meter(wire, wall)
        record: dict[str, Any] = {
            "v": RENDER_VERSION,
            "kind": "render_attempt",
            "identity": identity,
            "entry": inputs.entry_id,
            "side": side,
            "status": "NOT_RUN" if status == "unavailable" else "PASS" if status == "passed" else "FAIL",
            "reason": protect_text(detail, inputs.policy.redaction)[0][:500],
            "browser": environment.browser,
            "pages": [page.to_wire() | {"dom_lines": list(page.dom), "ax_lines": list(page.ax)} for page in pages],
            "digest": "render:sha256:" + _digest([page.digest for page in pages]),
            "data": {"execution": wire},
        }
        evidence = self._publish(
            record,
            title=f"{inputs.entry_id} · {_SIDE_NAME.get(side, side)} · render",
            source_ref=source_ref,
            detail=f"{record['status']}: {record['reason']}",
        )
        return evidence, record, pages


def _pages_from_record(record: Mapping[str, Any], cells: Sequence[RenderCell]) -> list[ProtectedPage] | None:
    rows = record.get("pages")
    if not isinstance(rows, list) or len(rows) != len(cells):
        return None
    pages: list[ProtectedPage] = []
    for cell, row in zip(cells, rows, strict=True):
        if not isinstance(row, Mapping) or _mapping(row.get("cell")).get("id") != cell.id:
            return None
        shot = _mapping(row.get("screenshot"))
        pages.append(
            ProtectedPage(
                cell,
                "captured" if row.get("status") == "captured" else "failed",
                str(row.get("reason") or ""),
                screenshot_digest=str(shot.get("digest") or ""),
                screenshot_ref=str(shot.get("ref") or ""),
                screenshot_withheld=str(shot.get("withheld") or ""),
                masked_regions=int(shot.get("masked_regions") or 0),
                dom=tuple(str(item) for item in row.get("dom_lines") or ()),
                dom_ref=str(_mapping(row.get("dom")).get("ref") or ""),
                ax=tuple(str(item) for item in row.get("ax_lines") or ()),
                ax_ref=str(_mapping(row.get("ax")).get("ref") or ""),
                network=tuple(str(item) for item in row.get("network") or ()),
                console=tuple(str(item) for item in row.get("console") or ()),
                cls=float(row.get("layout_shift") or 0.0),
                lcp_ms=float(row.get("lcp_ms") or 0.0),
                metrics={
                    str(k): float(v) for k, v in _mapping(row.get("metrics")).items() if isinstance(v, (int, float))
                },
                truncated=bool(row.get("truncated")),
            )
        )
    return pages


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(slots=True)
class RenderRunReport:
    entry_id: str
    revision_id: str
    record: dict[str, Any] = field(default_factory=dict)
    evidence_id: str = ""
    executions: int = 0
    reused: int = 0
    notes: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "revision_id": self.revision_id,
            "result": self.record,
            "evidence_id": self.evidence_id,
            "executions": self.executions,
            "reused": self.reused,
            "notes": list(self.notes),
        }


def _fingerprint(record: Mapping[str, Any]) -> tuple[str, ExecutionResult | None]:
    raw = _mapping(record.get("data")).get("execution")
    if not isinstance(raw, Mapping):
        return "", None
    try:
        parsed = ExecutionResult.from_wire(raw)
    except (TypeError, ValueError):
        return "", None
    return parsed.environment_fingerprint, parsed


def _qualification_subject(
    inputs: RenderRunInputs,
    environment: RenderEnvironment,
    sides: Mapping[str, tuple[ReviewEvidence, dict[str, Any], list[ProtectedPage]]],
) -> dict[str, str]:
    from lemoncrow.pro.capabilities.review.evidence_qualification import render_subject_wire

    head = _fingerprint(sides["head"][1])[1] if "head" in sides else None
    return render_subject_wire(
        inputs.entry_id,
        inputs.config.digest,
        browser=environment.browser,
        fonts_digest=environment.fonts_digest,
        sandbox=environment.sandbox,
        executor_class=inputs.executor_class.value,
        image_digest=head.verification.image_digest if head is not None else "",
    )


def run_render_entry(inputs: RenderRunInputs, meter: Meter) -> RenderRunReport:
    """Render the planned cells on base and head and publish the base-vs-head render diff."""

    report = RenderRunReport(entry_id=inputs.entry_id, revision_id=inputs.revision.id)
    run = _Run(inputs, meter)
    environment = inputs.driver.environment()
    sides: dict[str, tuple[ReviewEvidence, dict[str, Any], list[ProtectedPage]]] = {}
    if inputs.plan.cells:
        for side in ("old", "new"):
            # Each side states its own browser/font identity: a browser that changed between the
            # two captures makes the pair not comparable instead of a visual regression.
            sides[_SIDE_NAME[side]] = run.side(side, environment if side == "old" else inputs.driver.environment())
    report.executions, report.reused = run.executions, run.reused
    environments = {name: _fingerprint(record)[0] for name, (_item, record, _pages) in sides.items()}
    reasons: list[str] = list(inputs.config.errors)
    differences: tuple[str, ...] = ()
    same_environment = environments.get("base") == environments.get("head") and bool(environments.get("base"))
    # A side that rendered nothing leaves nothing to compare: not run, never "unchanged".
    not_rendered = [name for name, (_item, record, _pages) in sides.items() if record.get("status") == "NOT_RUN"]
    comparable = bool(sides) and same_environment and not not_rendered
    if sides and not same_environment and not not_rendered:
        base_fp, head_fp = _fingerprint(sides["base"][1])[1], _fingerprint(sides["head"][1])[1]
        differences = (
            base_fp.fingerprint.differences(head_fp.fingerprint)
            if base_fp is not None and head_fp is not None
            else ("environment fingerprint",)
        )
        reasons.append("environment fingerprints differ; rendering is not comparable (never a regression)")
    if not environment.available:
        reasons.append(environment.reason)
    reasons.extend(f"{name} was not rendered: {sides[name][1].get('reason')}" for name in not_rendered)
    pages: list[dict[str, Any]] = []
    unchanged = 0
    uncaptured = 0
    if comparable:
        base_pages, head_pages = sides["base"][2], sides["head"][2]
        for before, after in zip(base_pages, head_pages, strict=True):
            if before.status != "captured" and after.status != "captured":
                uncaptured += 1
                continue
            changes = page_changes(before, after)
            if not changes:
                # Two failed captures agree on nothing: only a page rendered on both sides is unchanged.
                unchanged += int(before.status == "captured" and after.status == "captured")
                continue
            pages.append(
                {
                    "cell": after.cell.to_wire(),
                    "changes": changes,
                    "digest": {"base": before.digest, "head": after.digest},
                }
            )
    if inputs.plan.limited_by_budget:
        reasons.append(f"Limited by budget: {len(inputs.plan.dropped)} cell(s) not rendered")
    elif inputs.plan.limited_by_depth:
        reasons.append(f"Limited by depth (render.max_cells): {len(inputs.plan.dropped)} cell(s) not rendered")
    if inputs.selection.note:
        reasons.append(inputs.selection.note)
    record: dict[str, Any] = {
        "v": RENDER_VERSION,
        "kind": "render_diff",
        "entry": inputs.entry_id,
        "executor_class": inputs.executor_class.value,
        "selection": inputs.selection.to_wire(),
        "plan": inputs.plan.to_wire(),
        "controls": inputs.config.controls.to_wire(),
        "browser": environment.browser,
        "environment": environments,
        "comparable": comparable,
        "differences": list(differences),
        "reasons": reasons,
        "sides": {
            name: {
                "evidence_id": item.id,
                "evidence_ref": f"check:{item.source}:{item.source_ref}",
                "status": record.get("status"),
                "digest": record.get("digest"),
                "pages": [
                    {k: v for k, v in page.items() if k not in {"dom_lines", "ax_lines"}}
                    for page in record.get("pages") or []
                ],
            }
            for name, (item, record, _pages) in sides.items()
        },
        "pages": pages,
        "unchanged": unchanged,
        "not_rendered": uncaptured,
        # Gate H: the surface this diff is a result of (what its determinism is measured as).
        "qualification_subject": _qualification_subject(inputs, environment, sides),
    }
    digest = _digest(record)
    source_ref = f"render-diff:{inputs.entry_id}:{digest}"
    existing: ReviewEvidence | None = inputs.store.get_evidence_by_source(
        inputs.revision.review_id, _SOURCE, source_ref
    )
    if existing is not None and existing.revision_id == inputs.revision.id:
        evidence = existing
    else:
        summary = (
            f"{len(pages)} page(s) differ, {unchanged} unchanged"
            + (f", {uncaptured} not rendered" if uncaptured else "")
            if comparable
            else ("not comparable: " + "; ".join(reasons))[:600] if sides else "nothing rendered: " + "; ".join(reasons)
        )
        evidence = run._publish(
            record,
            title=f"{inputs.entry_id} · rendering · base vs head",
            source_ref=source_ref,
            detail=summary,
        )
    report.record = record
    report.evidence_id = evidence.id
    return report


def latest_render_diffs(store: Any, revision: ReviewRevision) -> dict[str, dict[str, Any]]:
    """Newest verified render diff per entry on the exact Snapshot."""

    out: dict[str, dict[str, Any]] = {}
    rows = sorted(
        (
            item
            for item in store.list_evidence(revision.review_id)
            if item.revision_id == revision.id
            and item.mime_type == RENDER_DIFF_MEDIA_TYPE
            and item.execution_intent != "explicit_historical"
        ),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    for item in rows:
        record = read_record(store.read_evidence_artifact(item), item.content_hash)
        if record is None or record.get("kind") != "render_diff":
            continue
        entry = str(record.get("entry") or "")
        if entry in RENDER_ENTRIES and entry not in out:
            out[entry] = {**record, "evidence_id": item.id, "observed_at": item.created_at}
    return out


# --------------------------------------------------------------------------- #
# behavior producer
# --------------------------------------------------------------------------- #

_ASPECT_TITLE: Final[Mapping[str, str]] = {
    "screenshot": "Screenshot",
    "dom": "DOM",
    "a11y": "Accessibility tree",
    "network": "Page network",
    "console": "Page console",
    "layout_shift": "Layout shift",
    "performance": "Web performance",
    "status": "Render status",
}
_ASPECT_SEVERITY: Final[Mapping[str, BehaviorSeverity]] = {
    "a11y": "high",
    "console": "medium",
    "network": "medium",
    "screenshot": "medium",
    "dom": "medium",
    "layout_shift": "medium",
    "performance": "low",
    "status": "medium",
}
_EXECUTORS: Final[frozenset[str]] = frozenset({"author_machine", "customer_worker", "managed_sandbox"})


def _aspect_summary(aspect: str, change: Mapping[str, Any]) -> str:
    if aspect == "a11y":
        rows = [row for row in change.get("changes") or () if isinstance(row, Mapping)]
        kinds = sorted({str(row.get("kind")) for row in rows})
        top: Mapping[str, Any] = rows[0] if rows else {}
        base, head = _mapping(top.get("base")), _mapping(top.get("head"))
        detail = (
            f"{base.get('role')} {base.get('name')!r} -> {head.get('role')} {head.get('name')!r}"
            if base and head
            else ""
        )
        return f"{len(rows)} accessibility change(s) ({', '.join(kinds)})" + (f": {detail}" if detail else "")
    if aspect == "dom":
        return f"DOM changed: +{change.get('added', 0)} / -{change.get('removed', 0)} line(s)"
    if aspect in {"network", "console"}:
        added, removed = list(change.get("added") or ()), list(change.get("removed") or ())
        first = f"new: {added[0]}" if added else f"gone: {removed[0]}" if removed else ""
        return f"{len(added)} added, {len(removed)} removed" + (f"; {first}" if first else "")
    if aspect == "layout_shift":
        return f"Cumulative layout shift {change.get('base')} -> {change.get('head')}"
    if aspect == "screenshot":
        return "The rendered pixels differ (masked regions excluded)"
    if aspect == "performance":
        return str(change.get("note") or "Web performance changed beyond tolerance")
    return f"Render status {_mapping(change).get('base')} -> {_mapping(change).get('head')}"


class RenderDiffProducer:
    """``visual`` behavior targets from persisted render diffs (newest per entry)."""

    producer_id = RENDER_DIFF_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        newest: dict[str, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
        degraded: list[str] = []
        rows = sorted(
            (
                item
                for item in context.evidence
                if item.mime_type == RENDER_DIFF_MEDIA_TYPE
                and item.revision_id == context.revision.id
                and item.execution_intent != "explicit_historical"
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        for item in rows:
            record = read_record(context.read_artifact(item), item.content_hash)
            if record is None:
                degraded.append(f"render record {item.id} is missing or does not match its digest")
                continue
            entry = str(record.get("entry") or "")
            if record.get("kind") == "render_diff" and entry in RENDER_ENTRIES:
                newest.setdefault(entry, (item, record))
        deltas: list[BehaviorDelta] = []
        executed: list[ExecutedScope] = []
        unchanged = 0
        for entry in sorted(newest):
            item, found = newest[entry]
            deltas.extend(self._entry(entry, item, found, refs_out=executed))
            unchanged += max(0, int(found.get("unchanged") or 0)) if found.get("comparable") is True else 0
        return BehaviorProduction(
            deltas=tuple(deltas), executed=tuple(executed), unchanged=unchanged, degraded=tuple(degraded)
        )

    def _entry(
        self, entry: str, item: ReviewEvidence, record: Mapping[str, Any], *, refs_out: list[ExecutedScope]
    ) -> list[BehaviorDelta]:
        sides = _mapping(record.get("sides"))
        refs: dict[BehaviorSide, str] = {}
        side: BehaviorSide
        for side in ("base", "head"):
            ref = str(_mapping(sides.get(side)).get("evidence_ref") or "")
            if ref:
                refs[side] = ref
                refs_out.append(ExecutedScope(producer=self.producer_id, run_ref=ref, side=side, target_ids=(entry,)))
        if record.get("comparable") is not True:
            return []  # not comparable: no delta, never a regression
        executor_raw = str(record.get("executor_class") or "")
        executor = cast(ExecutorClass, executor_raw if executor_raw in _EXECUTORS else "")
        evidence = (
            BehaviorEvidenceRef(
                ref=f"artifact:{item.id}",
                kind="artifact",
                label=f"{entry} · render diff",
                digest=item.content_hash,
                role="record",
                evidence_id=item.id,
                media_type=item.mime_type,
            ),
            *(
                BehaviorEvidenceRef(ref=ref, kind="check", label=f"{entry} · {name} render", side=name)
                for name, ref in sorted(refs.items())
            ),
        )
        template = BehaviorDelta(
            kind="visual",
            producer=self.producer_id,
            subject="",
            title="",
            summary="",
            evidence=evidence,
            environment_fingerprint=str(_mapping(record.get("environment")).get("head") or ""),
            executor_class=executor,
            target_ids=(entry,),
        )
        deltas: list[BehaviorDelta] = []
        for page in record.get("pages") or ():
            if not isinstance(page, Mapping):
                continue
            cell_id = str(_mapping(page.get("cell")).get("id") or "")[:200]
            for aspect, change in sorted(_mapping(page.get("changes")).items()):
                if aspect not in _ASPECT_TITLE or not isinstance(change, Mapping):
                    continue
                if aspect in {"network", "console"}:
                    base_digest, head_digest = _digest(change.get("removed")), _digest(change.get("added"))
                else:
                    base_digest, head_digest = _digest([aspect, change.get("base")]), _digest(
                        [aspect, change.get("head")]
                    )
                facts: list[tuple[str, str]] = [
                    ("Observed", "rendered in a sandboxed browser"),
                    ("Page", cell_id),
                    ("Aspect", _ASPECT_TITLE[aspect]),
                    ("Entry", entry),
                ]
                if aspect == "screenshot":
                    facts.append(("Before", str(_mapping(change.get("base")).get("ref") or "")))
                    facts.append(("After", str(_mapping(change.get("head")).get("ref") or "")))
                deltas.append(
                    replace(
                        template,
                        subject=f"render:{entry}:{cell_id}:{aspect}",
                        title=f"{_ASPECT_TITLE[aspect]} · {cell_id}",
                        summary=_aspect_summary(aspect, change),
                        base=BehaviorObservation("base", "rendered", base_digest, "", refs.get("base", "")),
                        head=BehaviorObservation("head", "rendered", head_digest, "", refs.get("head", "")),
                        severity=_ASPECT_SEVERITY.get(aspect, "medium"),
                        facts=tuple(facts),
                    )
                )
        return deltas


__all__ = [
    "RENDER_DIFF_MEDIA_TYPE",
    "RENDER_DIFF_PRODUCER_ID",
    "RENDER_ENTRIES",
    "CellPlan",
    "ProtectedPage",
    "RenderConfig",
    "RenderDiffProducer",
    "RenderRunInputs",
    "RenderRunReport",
    "RenderRunSuperseded",
    "RenderUnavailable",
    "RouteSelection",
    "ax_changes",
    "latest_render_diffs",
    "line_changes",
    "page_changes",
    "parse_render_config",
    "plan_cells",
    "protect_page",
    "render_config_from_review",
    "run_render_entry",
    "select_routes",
]
