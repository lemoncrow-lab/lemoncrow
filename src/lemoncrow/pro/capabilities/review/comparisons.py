"""Generic old/new impact comparison for Review surfaces.

A surface provider decides *what* changed and, for executable surfaces, whether
automatic comparison is safe. Preview renderers and runners produce immutable
old/new values. Comparators only measure those values and return one normalized
ImpactScore the Reader can sort without knowing the surface kind.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from lemoncrow.pro.capabilities.review.surfaces import (
    COMPARISON_CONTRACT_VERSION,
    SUPPORTED_COMPARISON_CONTRACT_VERSIONS,
    ComparisonDescriptor,
    PluginIssue,
    ReviewSurface,
    _entry_points,
)

COMPARATOR_ENTRYPOINT_GROUP = "lemoncrow.review_surface_comparators"
_MAX_STRUCTURED_COMPARE_BYTES = 1024 * 1024
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ImpactScore:
    """One transparent, normalized old/new difference measurement."""

    comparator: str
    changed: bool
    magnitude: float
    label: str
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "comparator": self.comparator,
            "changed": self.changed,
            "magnitude": max(0.0, min(1.0, float(self.magnitude))),
            "label": self.label,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class ComparisonValue:
    """One side handed to a comparator.

    Artifact comparisons use ``path``/``media_type``. Runner comparisons use
    ``result`` (a SurfaceRunResult-like object). Custom comparator plugins may
    inspect the surface metadata and descriptor options through the request.
    """

    side: str
    path: Path | None = None
    media_type: str = ""
    result: Any | None = None


@dataclass(frozen=True)
class ComparisonRequest:
    surface: ReviewSurface
    descriptor: ComparisonDescriptor
    old: ComparisonValue
    new: ComparisonValue


@runtime_checkable
class ReviewSurfaceComparator(Protocol):
    comparator_id: str
    contract_version: int

    def compare(self, request: ComparisonRequest) -> ImpactScore: ...


def comparator_problem(comparator: object) -> str:
    version = getattr(comparator, "contract_version", None)
    if version not in SUPPORTED_COMPARISON_CONTRACT_VERSIONS:
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_COMPARISON_CONTRACT_VERSIONS))
        return (
            f"surface comparator {getattr(comparator, 'comparator_id', '?')!r} implements contract version "
            f"{version!r}; this LemonCrow supports {supported}"
        )
    return ""


class ComparatorRegistry:
    def __init__(
        self,
        comparators: Iterable[ReviewSurfaceComparator] = (),
        *,
        issues: Iterable[PluginIssue] = (),
    ) -> None:
        self._comparators: dict[str, ReviewSurfaceComparator] = {}
        self.issues: tuple[PluginIssue, ...] = tuple(issues)
        for comparator in comparators:
            self.register(comparator)

    def register(self, comparator: ReviewSurfaceComparator) -> None:
        comparator_id = str(comparator.comparator_id).strip()
        if not comparator_id:
            raise ValueError("review surface comparator_id must be non-empty")
        if comparator_id in self._comparators:
            raise ValueError(f"duplicate review surface comparator: {comparator_id}")
        problem = comparator_problem(comparator)
        if problem:
            raise ValueError(problem)
        self._comparators[comparator_id] = comparator

    def get(self, comparator_id: str) -> ReviewSurfaceComparator | None:
        return self._comparators.get(comparator_id)

    def comparators(self) -> tuple[ReviewSurfaceComparator, ...]:
        return tuple(self._comparators[key] for key in sorted(self._comparators))


def _artifact_bytes(value: ComparisonValue) -> bytes:
    if value.path is None:
        raise ValueError(f"{value.side} comparison artifact is unavailable")
    try:
        return value.path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{value.side} comparison artifact is unreadable: {exc}") from exc


def _sequence_delta(old: str, new: str) -> float:
    if old == new:
        return 0.0
    if max(len(old.encode("utf-8")), len(new.encode("utf-8"))) > _MAX_STRUCTURED_COMPARE_BYTES:
        return 1.0
    old_lines = old.splitlines() or [old]
    new_lines = new.splitlines() or [new]
    if len(old_lines) == 1 and len(new_lines) == 1:
        old_lines = [old[index : index + 128] for index in range(0, len(old), 128)] or [""]
        new_lines = [new[index : index + 128] for index in range(0, len(new), 128)] or [""]
    return 1.0 - difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=True).ratio()


class ImageComparator:
    comparator_id = "image"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        try:
            from PIL import Image, ImageChops, ImageStat
        except ImportError as exc:  # pragma: no cover - Pillow is normally present via pdfplumber
            raise ValueError("image comparison requires Pillow") from exc
        if request.old.path is None or request.new.path is None:
            raise ValueError("image comparison requires old and new artifacts")
        try:
            with Image.open(request.old.path) as old_image, Image.open(request.new.path) as new_image:
                old = old_image.convert("RGB")
                new = new_image.convert("RGB")
        except OSError as exc:
            raise ValueError(f"image comparison could not read an artifact: {exc}") from exc
        if old.size != new.size:
            return ImpactScore(
                self.comparator_id,
                True,
                1.0,
                f"dimensions changed {old.width}x{old.height} → {new.width}x{new.height}",
                ("image dimensions changed",),
                {"old_width": old.width, "old_height": old.height, "new_width": new.width, "new_height": new.height},
            )
        width, height = old.size
        pixels = max(1, width * height)
        diff = ImageChops.difference(old, new)
        mean_delta = sum(ImageStat.Stat(diff).mean) / (3 * 255.0)
        changed_pixels = diff.convert("L").point(lambda value: 255 if value >= 8 else 0).histogram()[255]
        changed_ratio = changed_pixels / pixels
        return ImpactScore(
            self.comparator_id,
            changed_ratio > 0,
            changed_ratio,
            f"Δ {changed_ratio * 100:.2f}% pixels" if changed_ratio < 0.001 else f"Δ {changed_ratio * 100:.1f}% pixels",
            (("visible pixels changed",) if changed_ratio else ("no material pixel difference",)),
            {"changed_ratio": changed_ratio, "mean_delta": mean_delta, "width": width, "height": height},
        )


class TextComparator:
    comparator_id = "text"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        old = _artifact_bytes(request.old).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        new = _artifact_bytes(request.new).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        delta = _sequence_delta(old, new)
        return ImpactScore(
            self.comparator_id,
            delta > 0,
            delta,
            f"text Δ {delta * 100:.1f}%" if delta else "no text delta",
            (("normalized text changed",) if delta else ("normalized text is unchanged",)),
            {"text_changed_ratio": delta},
        )


class JsonComparator:
    comparator_id = "json"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        try:
            old_value = json.loads(_artifact_bytes(request.old))
            new_value = json.loads(_artifact_bytes(request.new))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"JSON comparison requires valid JSON artifacts: {exc}") from exc
        old = json.dumps(old_value, sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": "))
        new = json.dumps(new_value, sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": "))
        delta = _sequence_delta(old, new)
        return ImpactScore(
            self.comparator_id,
            delta > 0,
            delta,
            f"JSON Δ {delta * 100:.1f}%" if delta else "no JSON delta",
            (("canonical JSON changed",) if delta else ("canonical JSON is unchanged",)),
            {"json_changed_ratio": delta},
        )


class BinaryComparator:
    comparator_id = "binary"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        old = _artifact_bytes(request.old)
        new = _artifact_bytes(request.new)
        old_hash = hashlib.sha256(old).hexdigest()
        new_hash = hashlib.sha256(new).hexdigest()
        changed = old_hash != new_hash
        return ImpactScore(
            self.comparator_id,
            changed,
            1.0 if changed else 0.0,
            "artifact changed" if changed else "artifact unchanged",
            (("artifact SHA-256 changed",) if changed else ("artifact SHA-256 is unchanged",)),
            {"old_sha256": old_hash, "new_sha256": new_hash, "old_bytes": len(old), "new_bytes": len(new)},
        )


class ApiResponseComparator:
    comparator_id = "api-response"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        if request.old.result is None or request.new.result is None:
            raise ValueError("API response comparison requires old and new run results")
        from lemoncrow.pro.environment.runners import api_response_diff_score

        score = api_response_diff_score(request.old.result, request.new.result)
        if score.status_changed:
            magnitude = 1.0
            label = (
                f"HTTP {score.old_status_code}→{score.new_status_code} · body Δ {score.body_changed_ratio * 100:.1f}%"
            )
            reasons = ("HTTP status changed",)
        else:
            magnitude = max(score.body_changed_ratio, score.header_changed_ratio * 0.5)
            label = (
                f"body Δ {score.body_changed_ratio * 100:.1f}%"
                + (f" · headers Δ {score.header_changed_ratio * 100:.1f}%" if score.header_changed_ratio else "")
                if magnitude
                else "no response delta"
            )
            reasons = ("response body or stable headers changed",) if magnitude else ("response is unchanged",)
        return ImpactScore(
            self.comparator_id,
            score.status_changed or score.body_changed_ratio > 0 or score.header_changed_ratio > 0,
            magnitude,
            label,
            reasons,
            score.to_payload(),
        )


class RunResultComparator:
    comparator_id = "run-result"
    contract_version = COMPARISON_CONTRACT_VERSION

    def compare(self, request: ComparisonRequest) -> ImpactScore:
        old = request.old.result
        new = request.new.result
        if old is None or new is None:
            raise ValueError("run-result comparison requires old and new run results")
        status_changed = str(getattr(old, "status", "")) != str(getattr(new, "status", ""))
        exit_changed = getattr(old, "exit_code", None) != getattr(new, "exit_code", None)
        old_payload = json.dumps(dict(getattr(old, "data", {}) or {}), sort_keys=True, indent=2, default=str)
        new_payload = json.dumps(dict(getattr(new, "data", {}) or {}), sort_keys=True, indent=2, default=str)
        data_delta = _sequence_delta(old_payload, new_payload)
        old_output = _WS_RE.sub(" ", str(getattr(old, "output", "") or "")).strip()
        new_output = _WS_RE.sub(" ", str(getattr(new, "output", "") or "")).strip()
        output_delta = _sequence_delta(old_output, new_output)
        magnitude = 1.0 if status_changed or exit_changed else max(data_delta, output_delta * 0.75)
        changed = magnitude > 0
        reasons: list[str] = []
        if status_changed:
            reasons.append("run status changed")
        if exit_changed:
            reasons.append("exit code changed")
        if data_delta:
            reasons.append("structured result changed")
        if output_delta:
            reasons.append("normalized output changed")
        if not reasons:
            reasons.append("run result is unchanged")
        return ImpactScore(
            self.comparator_id,
            changed,
            magnitude,
            f"run Δ {magnitude * 100:.1f}%" if changed else "no run-result delta",
            tuple(reasons),
            {
                "status_changed": status_changed,
                "exit_code_changed": exit_changed,
                "data_changed_ratio": data_delta,
                "output_changed_ratio": output_delta,
            },
        )


def load_entrypoint_comparators_isolated() -> tuple[tuple[ReviewSurfaceComparator, ...], tuple[PluginIssue, ...]]:
    found: list[ReviewSurfaceComparator] = []
    issues: list[PluginIssue] = []
    for point in _entry_points(COMPARATOR_ENTRYPOINT_GROUP):
        try:
            candidate = point.load()
            comparator = candidate() if isinstance(candidate, type) else candidate
        except Exception as exc:
            issues.append(PluginIssue(point.name, "comparator", f"plugin failed to load: {type(exc).__name__}: {exc}"))
            continue
        if not isinstance(comparator, ReviewSurfaceComparator):
            issues.append(
                PluginIssue(point.name, "comparator", "entry point does not implement ReviewSurfaceComparator")
            )
            continue
        found.append(comparator)
    return tuple(found), tuple(issues)


def built_in_comparator_registry(*, include_entrypoints: bool = True) -> ComparatorRegistry:
    comparators: list[ReviewSurfaceComparator] = [
        ImageComparator(),
        JsonComparator(),
        TextComparator(),
        BinaryComparator(),
        ApiResponseComparator(),
        RunResultComparator(),
    ]
    issues: list[PluginIssue] = []
    if include_entrypoints:
        plugins, load_issues = load_entrypoint_comparators_isolated()
        issues.extend(load_issues)
        taken = {item.comparator_id for item in comparators}
        for plugin in plugins:
            plugin_id = str(getattr(plugin, "comparator_id", "")).strip()
            if not plugin_id or plugin_id in taken:
                reason = (
                    "empty comparator_id" if not plugin_id else f"comparator id {plugin_id!r} is already registered"
                )
                issues.append(PluginIssue(plugin_id or "?", "comparator", f"plugin ignored: {reason}"))
                continue
            problem = comparator_problem(plugin)
            if problem:
                issues.append(PluginIssue(plugin_id, "comparator", f"plugin ignored: {problem}"))
                continue
            taken.add(plugin_id)
            comparators.append(plugin)
    return ComparatorRegistry(comparators, issues=issues)


def inferred_preview_comparator(surface: ReviewSurface) -> str:
    descriptor = surface.preview
    if descriptor is None or not descriptor.sides:
        return ""
    if descriptor.display == "image":
        return "image"
    if descriptor.display == "json":
        return "json"
    if descriptor.display in {"html", "text"}:
        return "text"
    if descriptor.display == "download":
        return "binary"
    return ""


def comparison_descriptor(surface: ReviewSurface) -> ComparisonDescriptor | None:
    """Return the explicit declaration or safe host-derived default."""

    if surface.comparison is not None:
        descriptor = surface.comparison
        if descriptor.comparator != "auto":
            return descriptor
        if descriptor.source == "preview":
            inferred = inferred_preview_comparator(surface)
        elif descriptor.source == "web":
            inferred = "image"
        elif descriptor.source == "run":
            inferred = "run-result"
        else:
            inferred = ""
        return replace(descriptor, comparator=inferred) if inferred else descriptor
    inferred = inferred_preview_comparator(surface)
    if not inferred:
        return None
    assert surface.preview is not None
    return ComparisonDescriptor(
        source="preview",
        comparator=inferred,
        label=surface.preview.label or "Preview difference",
        auto=True,
    )


def comparison_payload(
    surface: ReviewSurface,
    registry: ComparatorRegistry | None = None,
) -> dict[str, Any] | None:
    descriptor = comparison_descriptor(surface)
    if descriptor is None:
        return None
    problem = descriptor.problem()
    effective_registry = registry or built_in_comparator_registry()
    comparator = effective_registry.get(descriptor.comparator) if not problem else None
    if not problem and comparator is None:
        problem = f"surface comparator {descriptor.comparator!r} is unavailable"
    payload = descriptor.to_payload()
    payload["status"] = "unavailable" if problem else "available"
    payload["reason"] = problem
    payload["inferred"] = surface.comparison is None
    return payload


__all__ = [
    "COMPARATOR_ENTRYPOINT_GROUP",
    "ComparatorRegistry",
    "ComparisonRequest",
    "ComparisonValue",
    "ImpactScore",
    "ReviewSurfaceComparator",
    "built_in_comparator_registry",
    "comparison_descriptor",
    "comparison_payload",
]
