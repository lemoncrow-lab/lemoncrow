"""Landing policy: auto-approval scope, merge-result gate and break-glass (Phase 10, P10-LAND).

One ``landing`` section, read from two kinds of layer:

* **operator** (hosted organization/repository policy): the only layer that may
  *enable* auto-approval or name break-glass responders. Auto-approval is an
  organization/repository decision, never a personal setting and never text a
  Change can carry.
* **trusted_base** (``.lemoncrow/review.yaml`` on the trusted base, never the
  Change's head bytes): may only tighten -- disable auto-approval, add
  exclusions or sensitive paths, lower the criticality ceiling, require
  merge-result checks or shorten the follow-up deadline. An ``enabled: true``
  here is ignored and reported.

::

    landing:
      auto_approve:
        enabled: false
        max_criticality: low          # low | normal | high (critical never)
        require_clean_review: true
        include: {paths: ["docs/**", "tests/**"]}
        exclude: {paths: [...], labels: [security], branches: ["release/**"]}
        sensitive_paths: ["src/payments/**"]
      merge_result:
        required: true
        required_checks:              # typed: implies required; any layer may add one
          - {check: "15368:build", class: provider_ci}   # provider_ci only, for now
      break_glass:
        responders: [alice, bob]      # principal ids; empty = nobody
        follow_up_hours: 72           # 1..720

An invalid section never reads as defaults that could approve: its errors are
kept and :attr:`LandingPolicy.auto_approve_enabled` is ``False`` while any
layer is invalid. Pure: parsing and merging only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import yaml

from lemoncrow.pro.capabilities.review.rules import path_matches

LandingLayer = Literal["operator", "trusted_base"]

#: Criticality ceilings auto-approval may be configured with; ``critical`` is never auto-approvable.
AUTO_APPROVE_CRITICALITY: Final[tuple[str, ...]] = ("low", "normal", "high")
CRITICALITY_RANK: Final[Mapping[str, int]] = {"low": 0, "normal": 1, "high": 2, "critical": 3}
DEFAULT_MAX_CRITICALITY: Final[str] = "low"

MAX_PATTERNS: Final[int] = 64
"""Entries per path/label/branch list; a longer list is invalid (never truncated)."""
MAX_PATTERN_CHARS: Final[int] = 256
MAX_RESPONDERS: Final[int] = 64
FOLLOW_UP_HOURS_MIN: Final[int] = 1
FOLLOW_UP_HOURS_MAX: Final[int] = 720
DEFAULT_FOLLOW_UP_HOURS: Final[int] = 72

_SECTION_KEYS: Final[frozenset[str]] = frozenset({"auto_approve", "merge_result", "break_glass"})
_AUTO_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "max_criticality", "require_clean_review", "include", "exclude", "sensitive_paths"}
)
_INCLUDE_KEYS: Final[frozenset[str]] = frozenset({"paths"})
_EXCLUDE_KEYS: Final[frozenset[str]] = frozenset({"paths", "labels", "branches"})
_MERGE_KEYS: Final[frozenset[str]] = frozenset({"required", "required_checks"})
_REQUIRED_CHECK_KEYS: Final[frozenset[str]] = frozenset({"check", "class"})

#: Who must have produced a required merge-result check (``SideResult.source``). Provider
#: CI is one class among them: a provider row never satisfies a LemonCrow or feed check.
#: Policy accepts only :data:`PRODUCED_MERGE_CHECK_CLASSES`.
MERGE_CHECK_CLASSES: Final[tuple[str, ...]] = ("provider_ci", "lemoncrow_verification", "trusted_security_feed")
#: The classes something actually produces on the merge result today. A required check of
#: any other class could never be satisfied, so a policy naming one is refused rather than
#: left to hold every landing pending forever.
# lc-debt: lemoncrow_verification and trusted_security_feed have no merge-result producer (every
# merge-result row is provider_ci); upgrade: post LemonCrow's typed verification results on the
# merge-group commit, with an end-to-end landing test, then add the class here.
PRODUCED_MERGE_CHECK_CLASSES: Final[tuple[str, ...]] = ("provider_ci",)
_BREAK_KEYS: Final[frozenset[str]] = frozenset({"responders", "follow_up_hours"})


class _PolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AutoApprovePolicy:
    """The effective auto-approval scope (all exclusions already merged)."""

    enabled: bool = False
    max_criticality: str = DEFAULT_MAX_CRITICALITY
    require_clean_review: bool = True
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    exclude_branches: tuple[str, ...] = ()
    sensitive_paths: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_criticality": self.max_criticality,
            "require_clean_review": self.require_clean_review,
            "include": {"paths": list(self.include_paths)},
            "exclude": {
                "paths": list(self.exclude_paths),
                "labels": list(self.exclude_labels),
                "branches": list(self.exclude_branches),
            },
            "sensitive_paths": list(self.sensitive_paths),
        }


@dataclass(frozen=True, slots=True)
class BreakGlassPolicy:
    responders: tuple[str, ...] = ()
    follow_up_hours: int = DEFAULT_FOLLOW_UP_HOURS

    def to_wire(self) -> dict[str, Any]:
        return {"responders": list(self.responders), "follow_up_hours": self.follow_up_hours}


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """One merge-result check that must pass, by stable identity and producer class."""

    check: str
    check_class: str

    def to_wire(self) -> dict[str, str]:
        return {"check": self.check, "class": self.check_class}


@dataclass(frozen=True, slots=True)
class LandingPolicyLayer:
    """One parsed ``landing`` section; ``None`` fields were not set by this layer."""

    layer: LandingLayer
    source: str
    enabled: bool | None = None
    max_criticality: str | None = None
    require_clean_review: bool | None = None
    include_paths: tuple[str, ...] | None = None
    exclude_paths: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    exclude_branches: tuple[str, ...] = ()
    sensitive_paths: tuple[str, ...] = ()
    merge_result_required: bool | None = None
    merge_result_checks: tuple[RequiredCheck, ...] = ()
    responders: tuple[str, ...] | None = None
    follow_up_hours: int | None = None
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return self != LandingPolicyLayer(layer=self.layer, source=self.source)


@dataclass(frozen=True, slots=True)
class LandingPolicy:
    """The effective landing policy for one Snapshot."""

    auto_approve: AutoApprovePolicy = field(default_factory=AutoApprovePolicy)
    merge_result_required: bool = False
    merge_result_checks: tuple[RequiredCheck, ...] = ()
    break_glass: BreakGlassPolicy = field(default_factory=BreakGlassPolicy)
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def auto_approve_enabled(self) -> bool:
        """Enabled by an operator layer and no layer is invalid (an invalid layer never approves)."""

        return self.auto_approve.enabled and not self.errors

    @property
    def break_glass_enabled(self) -> bool:
        return bool(self.break_glass.responders) and not self.errors

    def to_wire(self) -> dict[str, Any]:
        merge: dict[str, Any] = {"required": self.merge_result_required}
        if self.merge_result_checks:
            merge["required_checks"] = [item.to_wire() for item in self.merge_result_checks]
        return {
            "auto_approve": {**self.auto_approve.to_wire(), "effective": self.auto_approve_enabled},
            "merge_result": merge,
            "break_glass": {**self.break_glass.to_wire(), "enabled": self.break_glass_enabled},
            "errors": list(self.errors),
            "notes": list(self.notes),
            "sources": list(self.sources),
        }


def _mapping(raw: object, *, where: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _PolicyError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in keys)
    if unknown:
        raise _PolicyError(f"{where} has unknown setting {unknown[0]!r}")
    return raw


def _bool(raw: object, *, where: str) -> bool:
    if not isinstance(raw, bool):
        raise _PolicyError(f"{where} must be true or false")
    return raw


def _strings(raw: object, *, where: str, limit: int = MAX_PATTERNS) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise _PolicyError(f"{where} must be a list of strings")
    if len(raw) > limit:
        # Never truncated: a dropped exclusion would silently widen auto-approval.
        raise _PolicyError(f"{where} lists {len(raw)} entries; at most {limit} are allowed")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_PATTERN_CHARS:
            raise _PolicyError(f"{where} entries must be non-empty strings of at most {MAX_PATTERN_CHARS} characters")
        value = item.strip()
        if value not in out:
            out.append(value)
    return tuple(out)


def _globs(raw: object, *, where: str) -> tuple[str, ...]:
    values = _strings(raw, where=where)
    for value in values:
        try:
            path_matches("probe", (value,))
        except ValueError as exc:
            raise _PolicyError(f"{where}: {exc}") from exc
    return values


def _required_checks(raw: object) -> tuple[RequiredCheck, ...]:
    where = "landing.merge_result.required_checks"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _PolicyError(f"{where} must be a list of {{check, class}} mappings")
    if len(raw) > MAX_PATTERNS:
        raise _PolicyError(f"{where} lists {len(raw)} entries; at most {MAX_PATTERNS} are allowed")
    out: list[RequiredCheck] = []
    for item in raw:
        entry = _mapping(item, where=f"{where}[]", keys=_REQUIRED_CHECK_KEYS)
        check, check_class = entry.get("check"), entry.get("class")
        if not isinstance(check, str) or not check.strip() or len(check) > MAX_PATTERN_CHARS:
            raise _PolicyError(f"{where}[].check must be a non-empty string of at most {MAX_PATTERN_CHARS} characters")
        if check_class not in MERGE_CHECK_CLASSES:
            raise _PolicyError(f"{where}[].class must be one of {', '.join(MERGE_CHECK_CLASSES)}")
        if check_class not in PRODUCED_MERGE_CHECK_CLASSES:
            raise _PolicyError(
                f"{where}[].class {check_class} is not supported yet: nothing reports a {check_class} result "
                f"on the merge result, so the check could never pass. Use "
                f"{' or '.join(PRODUCED_MERGE_CHECK_CLASSES)}"
            )
        required = RequiredCheck(check=check.strip(), check_class=str(check_class))
        clash = next(
            (item for item in out if item.check == required.check and item.check_class != required.check_class), None
        )
        if clash is not None:
            raise _PolicyError(
                f"{where} lists {required.check} under both {clash.check_class} and {required.check_class}; "
                "a check has one class"
            )
        if required not in out:
            out.append(required)
    return tuple(out)


def _parse(raw: Mapping[str, Any], *, layer: LandingLayer, source: str) -> LandingPolicyLayer:
    section = _mapping(raw, where="landing", keys=_SECTION_KEYS)
    auto = _mapping(section.get("auto_approve"), where="landing.auto_approve", keys=_AUTO_KEYS)
    include = _mapping(auto.get("include"), where="landing.auto_approve.include", keys=_INCLUDE_KEYS)
    exclude = _mapping(auto.get("exclude"), where="landing.auto_approve.exclude", keys=_EXCLUDE_KEYS)
    merge = _mapping(section.get("merge_result"), where="landing.merge_result", keys=_MERGE_KEYS)
    breaking = _mapping(section.get("break_glass"), where="landing.break_glass", keys=_BREAK_KEYS)
    notes: list[str] = []
    enabled: bool | None = None
    if "enabled" in auto:
        enabled = _bool(auto["enabled"], where="landing.auto_approve.enabled")
        if enabled and layer != "operator":
            # Repository text never enables auto-approval; it may only turn it off.
            notes.append(f"{source}: auto_approve.enabled is set by organization policy only; ignored here")
            enabled = None
    max_criticality: str | None = None
    if "max_criticality" in auto:
        value = auto["max_criticality"]
        if not isinstance(value, str) or value not in AUTO_APPROVE_CRITICALITY:
            raise _PolicyError(
                f"landing.auto_approve.max_criticality must be one of {', '.join(AUTO_APPROVE_CRITICALITY)}"
            )
        max_criticality = value
    require_clean: bool | None = None
    if "require_clean_review" in auto:
        require_clean = _bool(auto["require_clean_review"], where="landing.auto_approve.require_clean_review")
        if not require_clean and layer != "operator":
            notes.append(f"{source}: require_clean_review can only be relaxed by organization policy; ignored here")
            require_clean = None
    include_paths: tuple[str, ...] | None = None
    if "paths" in include:
        include_paths = _globs(include["paths"], where="landing.auto_approve.include.paths")
    merge_required: bool | None = None
    if "required" in merge:
        merge_required = _bool(merge["required"], where="landing.merge_result.required")
        if not merge_required and layer != "operator":
            merge_required = None
    responders: tuple[str, ...] | None = None
    if "responders" in breaking:
        if layer != "operator":
            notes.append(f"{source}: break_glass.responders are named by organization policy only; ignored here")
        else:
            responders = _strings(breaking["responders"], where="landing.break_glass.responders", limit=MAX_RESPONDERS)
    follow_up: int | None = None
    if "follow_up_hours" in breaking:
        value = breaking["follow_up_hours"]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not FOLLOW_UP_HOURS_MIN <= value <= FOLLOW_UP_HOURS_MAX
        ):
            raise _PolicyError(
                f"landing.break_glass.follow_up_hours must be an integer between {FOLLOW_UP_HOURS_MIN} and "
                f"{FOLLOW_UP_HOURS_MAX}"
            )
        follow_up = value
    return LandingPolicyLayer(
        layer=layer,
        source=source,
        enabled=enabled,
        max_criticality=max_criticality,
        require_clean_review=require_clean,
        include_paths=include_paths,
        exclude_paths=_globs(exclude.get("paths"), where="landing.auto_approve.exclude.paths"),
        exclude_labels=_strings(exclude.get("labels"), where="landing.auto_approve.exclude.labels"),
        exclude_branches=_globs(exclude.get("branches"), where="landing.auto_approve.exclude.branches"),
        sensitive_paths=_globs(auto.get("sensitive_paths"), where="landing.auto_approve.sensitive_paths"),
        merge_result_required=merge_required,
        merge_result_checks=_required_checks(merge.get("required_checks")),
        responders=responders,
        follow_up_hours=follow_up,
        notes=tuple(notes),
    )


def parse_landing_section(raw: object, *, layer: LandingLayer, source: str) -> LandingPolicyLayer:
    """One layer's ``landing`` section; an invalid section keeps its error (fails closed)."""

    if raw is None:
        return LandingPolicyLayer(layer=layer, source=source)
    if not isinstance(raw, Mapping):
        return LandingPolicyLayer(layer=layer, source=source, errors=(f"{source}: landing must be a mapping",))
    try:
        return _parse(raw, layer=layer, source=source)
    except _PolicyError as exc:
        return LandingPolicyLayer(layer=layer, source=source, errors=(f"{source}: {exc}",))


def parse_trusted_landing(content: str, *, source: str) -> LandingPolicyLayer:
    """The trusted base's ``landing`` section (never the Change's head bytes)."""

    if not content.strip():
        return LandingPolicyLayer(layer="trusted_base", source=source)
    try:
        decoded = yaml.safe_load(content)
    except yaml.YAMLError:
        if "landing" in content:
            # Unparsable config that may restrict landing: what applies is unknown.
            return LandingPolicyLayer(
                layer="trusted_base", source=source, errors=(f"{source}: review config is not valid YAML",)
            )
        return LandingPolicyLayer(layer="trusted_base", source=source)
    if not isinstance(decoded, Mapping):
        return LandingPolicyLayer(layer="trusted_base", source=source)
    return parse_landing_section(decoded.get("landing"), layer="trusted_base", source=source)


def _union(*groups: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for group in groups:
        out.extend(item for item in group if item not in out)
    return tuple(out)


def merge_landing_policies(layers: Sequence[LandingPolicyLayer]) -> LandingPolicy:
    """Combine layers, highest authority first; the safer value always wins.

    Enabled only when an operator layer enables it and no layer disables it;
    exclusions, sensitive paths and errors accumulate; the criticality ceiling
    is the lowest any layer sets; the include scope is the first operator one
    (a trusted-base include can only narrow it by intersection); merge-result
    checks are required when any layer requires them, and every layer's typed
    required checks apply (any implies required); responders come from the
    first operator layer that names them; the follow-up deadline is the shortest.
    """

    enabled: bool | None = None
    disabled = False
    ceiling = DEFAULT_MAX_CRITICALITY
    ceiling_set = False
    require_clean: bool | None = None
    include: tuple[str, ...] | None = None
    exclude_paths: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    exclude_branches: tuple[str, ...] = ()
    sensitive: tuple[str, ...] = ()
    merge_required = False
    merge_checks: list[RequiredCheck] = []
    responders: tuple[str, ...] | None = None
    follow_up: int | None = None
    errors: list[str] = []
    notes: list[str] = []
    sources: list[str] = []
    for layer in layers:
        errors.extend(item for item in layer.errors if item not in errors)
        notes.extend(item for item in layer.notes if item not in notes)
        if layer.present and layer.source not in sources:
            sources.append(layer.source)
        if layer.enabled is False:
            disabled = True
        elif layer.enabled is True and layer.layer == "operator" and enabled is None:
            enabled = True
        if layer.max_criticality is not None:
            if not ceiling_set or CRITICALITY_RANK[layer.max_criticality] < CRITICALITY_RANK[ceiling]:
                ceiling = layer.max_criticality
            ceiling_set = True
        if layer.require_clean_review is not None:
            require_clean = (
                layer.require_clean_review if require_clean is None else require_clean or (layer.require_clean_review)
            )
        if layer.include_paths is not None:
            if include is None:
                include = layer.include_paths
            elif layer.layer == "trusted_base":
                include = tuple(item for item in include if item in layer.include_paths)
        exclude_paths = _union(exclude_paths, layer.exclude_paths)
        exclude_labels = _union(exclude_labels, layer.exclude_labels)
        exclude_branches = _union(exclude_branches, layer.exclude_branches)
        sensitive = _union(sensitive, layer.sensitive_paths)
        merge_required = merge_required or bool(layer.merge_result_required) or bool(layer.merge_result_checks)
        merge_checks.extend(item for item in layer.merge_result_checks if item not in merge_checks)
        if layer.responders is not None and responders is None and layer.layer == "operator":
            responders = layer.responders
        if layer.follow_up_hours is not None:
            follow_up = layer.follow_up_hours if follow_up is None else min(follow_up, layer.follow_up_hours)
    return LandingPolicy(
        auto_approve=AutoApprovePolicy(
            enabled=bool(enabled) and not disabled,
            max_criticality=ceiling,
            require_clean_review=True if require_clean is None else require_clean,
            include_paths=include or (),
            exclude_paths=exclude_paths,
            exclude_labels=exclude_labels,
            exclude_branches=exclude_branches,
            sensitive_paths=sensitive,
        ),
        merge_result_required=merge_required,
        merge_result_checks=tuple(merge_checks),
        break_glass=BreakGlassPolicy(
            responders=responders or (),
            follow_up_hours=DEFAULT_FOLLOW_UP_HOURS if follow_up is None else follow_up,
        ),
        errors=tuple(errors),
        notes=tuple(notes),
        sources=tuple(sources),
    )


__all__ = [
    "AUTO_APPROVE_CRITICALITY",
    "CRITICALITY_RANK",
    "DEFAULT_FOLLOW_UP_HOURS",
    "DEFAULT_MAX_CRITICALITY",
    "FOLLOW_UP_HOURS_MAX",
    "FOLLOW_UP_HOURS_MIN",
    "MAX_PATTERNS",
    "MAX_RESPONDERS",
    "MERGE_CHECK_CLASSES",
    "PRODUCED_MERGE_CHECK_CLASSES",
    "AutoApprovePolicy",
    "BreakGlassPolicy",
    "LandingLayer",
    "LandingPolicy",
    "LandingPolicyLayer",
    "RequiredCheck",
    "merge_landing_policies",
    "parse_landing_section",
    "parse_trusted_landing",
]
