"""Path-scoped review-first priority, criticality, finding severity and threshold (Phase 9).

Four separate concepts (plan §3.14-§3.16), never collapsed into one score:

* **Review-first priority** (``first | normal | last``) only changes the order a
  reviewer meets targets in. It can never make a path less strict.
* **Criticality** (``critical | high | normal | low``) says how consequential a
  path is. It raises R1's depth floor (``min_effort``) and moves targets earlier
  in the canonical queue; it is not a finding severity.
* **Finding severity** (``P0 Critical | P1 High | P2 Medium | Suggestion``) is a
  property of one finding, normalized by policy from the producer's validated
  category/evidence. A model's own wording never sets it.
* **Finding threshold** (``critical | high | medium | suggestions``) controls what
  is surfaced. It is never part of the run plan, so it cannot change compute.

Settings are read from ``review.focus`` / ``review.finding_threshold`` in the
**trusted-base** ``.lemoncrow/review.yaml`` (local Snapshot base, or the hosted
provider-proven trusted-base capture) and from operator organization/repository
policy (``effective_review["triage"]["layers"]``). Head-side edits are proposed
future policy only: nothing here is ever handed head bytes.

Precedence (plan §3.9, §3.21): a layer either inherits (sets nothing) or
overrides. Criticality is security-relevant, so the **strictest** matching rule
across every layer wins and no layer can relax another's. Review-first priority
and the finding threshold are plain overrides: the highest-precedence layer that
sets a value wins and, inside one layer, the last matching focus rule wins
(CODEOWNERS style). ``last`` never demotes a critical/high path.

Pure: no store, no I/O.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_RANK, PRESETS
from lemoncrow.pro.capabilities.review.review_priority import QueueReason

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.run_plan import FloorRule

ReviewFirst = Literal["first", "normal", "last"]
Criticality = Literal["critical", "high", "normal", "low"]
FindingSeverity = Literal["p0", "p1", "p2", "suggestion"]
FindingThreshold = Literal["critical", "high", "medium", "suggestions"]
PolicyState = Literal["final", "provisional", "invalid"]

REVIEW_FIRST_VALUES: Final[tuple[str, ...]] = ("first", "normal", "last")
CRITICALITY_VALUES: Final[tuple[str, ...]] = ("critical", "high", "normal", "low")
FINDING_SEVERITIES: Final[tuple[str, ...]] = ("p0", "p1", "p2", "suggestion")
FINDING_THRESHOLDS: Final[tuple[str, ...]] = ("critical", "high", "medium", "suggestions")
DEFAULT_FINDING_THRESHOLD: Final[FindingThreshold] = "medium"

CRITICALITY_RANK: Final[Mapping[str, int]] = {"low": 0, "normal": 1, "high": 2, "critical": 3}
SEVERITY_RANK: Final[Mapping[str, int]] = {"suggestion": 0, "p2": 1, "p1": 2, "p0": 3}
SEVERITY_LABELS: Final[Mapping[str, str]] = {
    "p0": "P0 Critical",
    "p1": "P1 High",
    "p2": "P2 Medium",
    "suggestion": "Suggestion",
}
THRESHOLD_LABELS: Final[Mapping[str, str]] = {
    "critical": "Critical only",
    "high": "High+",
    "medium": "Medium+",
    "suggestions": "Suggestions",
}
_THRESHOLD_MINIMUM: Final[Mapping[str, str]] = {
    "critical": "p0",
    "high": "p1",
    "medium": "p2",
    "suggestions": "suggestion",
}
#: Depth floor a criticality implies for its paths (R1 ``min_effort``). An
#: explicit ``min_effort`` on the same focus rule may raise it further; the run
#: planner always keeps the strictest floor.
CRITICALITY_MIN_EFFORT: Final[Mapping[str, str]] = {"critical": "thorough", "high": "auto"}

#: AI finding category -> normalized severity. Only a confirmed, evidence-cited
#: finding reaches this map, and only policy (a critical/blocker organization
#: rule) can make one P0; the model's text is never consulted.
_AI_CATEGORY_SEVERITY: Final[Mapping[str, FindingSeverity]] = {
    "critical_rule": "p0",
    "security": "p1",
    "correctness": "p1",
    "concurrency_state": "p1",
    "persistence_migration": "p1",
    "api_compatibility": "p1",
    "performance": "p2",
    "verification": "p2",
}
_BEHAVIOR_SEVERITY: Final[Mapping[str, FindingSeverity]] = {"high": "p1", "medium": "p2", "low": "suggestion"}
_VULNERABILITY_SEVERITY: Final[Mapping[str, FindingSeverity]] = {
    "critical": "p0",
    "high": "p1",
    "moderate": "p2",
    "medium": "p2",
    "low": "suggestion",
}
#: Imported static-analysis result level (SARIF ``level``, check-run
#: ``annotation_level``) when no security score is given. A failing result is
#: P2 at most from its level alone; only a scored advisory reaches P1/P0.
_STATIC_LEVEL_SEVERITY: Final[Mapping[str, FindingSeverity]] = {"error": "p2", "failure": "p2"}

#: ``focus.require`` vocabulary: the review categories a path requires. These are
#: exactly the AI finding categories (``ai_review.FindingCategory``) minus
#: ``critical_rule``, which only an organization rule can set.
REQUIRED_FOCUS_CATEGORIES: Final[tuple[str, ...]] = (
    "correctness",
    "security",
    "api_compatibility",
    "concurrency_state",
    "persistence_migration",
    "performance",
    "verification",
)
#: Spellings the plan's examples use (§3.14), normalized to their category.
_REQUIRE_ALIASES: Final[Mapping[str, str]] = {
    "persistence": "persistence_migration",
    "backwards_compatibility": "api_compatibility",
}

_LAYER_ORDER: Final[tuple[str, ...]] = (
    "built_in_default",
    "organization",
    "team",
    "repository",
    "trusted_base_repository",
    "directory",
)
_FOCUS_KEYS: Final[frozenset[str]] = frozenset({"paths", "priority", "criticality", "min_effort", "require"})
MAX_FOCUS_RULES: Final[int] = 128
_MAX_PATHS: Final[int] = 64


# --------------------------------------------------------------------------- #
# origins and rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PolicyOrigin:
    """Which layer, and which file/commit or actor, set a value.

    ``layer`` is a settings layer (``organization`` ... ``trusted_base_repository``)
    or, for Review-level decisions, ``evidence`` / ``override``. ``source`` names
    the document (``base Snapshot .lemoncrow/review.yaml``, the hosted trusted-base
    capture, ``operator_review_policy``) or the actor; ``commit`` is the exact
    trusted-base commit when the source does not already carry it.
    """

    layer: str
    source: str
    commit: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"layer": self.layer, "source": self.source, "commit": self.commit}

    @property
    def label(self) -> str:
        where = f"{self.source}@{self.commit[:12]}" if self.commit else self.source
        return f"{self.layer.replace('_', ' ')} · {where}"

    @classmethod
    def from_wire(cls, raw: object) -> PolicyOrigin:
        if not isinstance(raw, Mapping):
            return BUILT_IN_ORIGIN
        return cls(
            str(raw.get("layer") or "built_in_default"), str(raw.get("source") or ""), str(raw.get("commit") or "")
        )


BUILT_IN_ORIGIN: Final[PolicyOrigin] = PolicyOrigin("built_in_default", "built_in_default")


@dataclass(frozen=True, slots=True)
class FocusRule:
    """One path-scoped ``review.focus`` rule; ``None`` fields inherit."""

    paths: tuple[str, ...]
    origin: PolicyOrigin
    priority: ReviewFirst | None = None
    criticality: Criticality | None = None
    min_effort: str | None = None
    require: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.paths)

    @property
    def pattern(self) -> str:
        return ", ".join(self.paths)

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {"paths": list(self.paths)}
        if self.priority is not None:
            out["priority"] = self.priority
        if self.criticality is not None:
            out["criticality"] = self.criticality
        if self.min_effort is not None:
            out["min_effort"] = self.min_effort
        if self.require:
            out["require"] = list(self.require)
        return out

    def to_wire(self) -> dict[str, Any]:
        return {**self.to_document(), "origin": self.origin.to_wire()}


@dataclass(frozen=True, slots=True)
class TriageLayer:
    """One settings layer's ``focus`` rules and ``finding_threshold``; absent means inherit."""

    origin: PolicyOrigin
    focus: tuple[FocusRule, ...] = ()
    finding_threshold: FindingThreshold | None = None
    errors: tuple[str, ...] = ()

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.focus:
            out["focus"] = [rule.to_document() for rule in self.focus]
        if self.finding_threshold is not None:
            out["finding_threshold"] = self.finding_threshold
        return out

    def to_wire(self) -> dict[str, Any]:
        return {"origin": self.origin.to_wire(), "settings": self.to_document(), "errors": list(self.errors)}


def _strings(raw: object, where: str, errors: list[str], *, limit: int = _MAX_PATHS) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        errors.append(f"{where} must be a list of strings")
        return ()
    out: list[str] = []
    for item in raw[:limit]:
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            errors.append(f"{where} entries must be non-empty strings")
            continue
        out.append(item.strip())
    if len(raw) > limit:
        errors.append(f"{where} exceeds {limit} entries")
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True, slots=True)
class RequiredFocus:
    """One review category that ``focus.require`` demands on changed paths.

    A typed fact, not an enforcement: it names what a complete review of
    *paths* must cover (``security``, ``correctness``, ...), with the layers
    that required it. Readiness (P10-READY) and escalation (P11-ESC) consume it
    as a condition; triage surfaces it and never lowers such a Review to
    ``low``. Nothing here marks a category as covered.
    """

    category: str
    paths: tuple[str, ...]
    origins: tuple[PolicyOrigin, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "paths": list(self.paths),
            "origins": [origin.to_wire() for origin in self.origins],
        }

    @classmethod
    def from_wire(cls, raw: object) -> RequiredFocus | None:
        if not isinstance(raw, Mapping) or raw.get("category") not in REQUIRED_FOCUS_CATEGORIES:
            return None
        return cls(
            category=str(raw["category"]),
            paths=tuple(str(item) for item in raw.get("paths") or () if str(item)),
            origins=tuple(PolicyOrigin.from_wire(item) for item in raw.get("origins") or ()),
        )


def _required_categories(raw: object, where: str, errors: list[str]) -> tuple[str, ...]:
    """Validate ``focus[i].require``; an unsupported category is a visible error, never ignored."""

    out: list[str] = []
    for item in _strings(raw, where, errors, limit=16):
        category = _REQUIRE_ALIASES.get(item.lower(), item.lower())
        if category not in REQUIRED_FOCUS_CATEGORIES:
            errors.append(
                f"{where} has unsupported category {item!r}; supported: " + ", ".join(REQUIRED_FOCUS_CATEGORIES)
            )
            continue
        if category not in out:
            out.append(category)
    return tuple(out)


def parse_triage_settings(raw: Mapping[str, Any], *, origin: PolicyOrigin) -> TriageLayer:
    """Parse ``focus`` and ``finding_threshold`` from one ``review:``-shaped mapping.

    Other keys belong to other owners (R1 run settings, triggers) and are
    ignored here. Every malformed value is a visible error; an invalid field is
    dropped from its rule, never coerced into a weaker value.

    ``focus[i].require`` lists the review categories a complete review of the
    rule's paths must cover (plan §3.14: ``require: [security, correctness]``).
    Values are the AI finding categories in :data:`REQUIRED_FOCUS_CATEGORIES`
    (``persistence`` and ``backwards_compatibility`` are accepted aliases); any
    other value is a validation error that makes the policy ``invalid``. A
    valid entry becomes a typed :class:`RequiredFocus` fact for the changed
    paths it matches. It does not assign a reviewer, add a check, or change the
    run plan or depth; readiness conditions consume the fact.
    """

    errors: list[str] = []
    rules: list[FocusRule] = []
    raw_focus = raw.get("focus")
    if raw_focus is not None:
        if not isinstance(raw_focus, list):
            errors.append("focus must be a list of path rules")
        else:
            for index, item in enumerate(raw_focus[:MAX_FOCUS_RULES]):
                where = f"focus[{index}]"
                if not isinstance(item, Mapping):
                    errors.append(f"{where} must be a mapping with paths")
                    continue
                for key in sorted(set(item) - _FOCUS_KEYS):
                    errors.append(f"{where} has unknown field {key!r}")
                paths = _strings(item.get("paths"), f"{where}.paths", errors)
                if not paths:
                    errors.append(f"{where} needs paths")
                    continue
                priority = item.get("priority")
                if priority is not None and priority not in REVIEW_FIRST_VALUES:
                    errors.append(f"{where}.priority must be first, normal or last")
                    priority = None
                criticality = item.get("criticality")
                if criticality is not None and criticality not in CRITICALITY_VALUES:
                    errors.append(f"{where}.criticality must be critical, high, normal or low")
                    criticality = None
                min_effort = item.get("min_effort")
                if min_effort is not None and min_effort not in PRESETS:
                    errors.append(f"{where}.min_effort must be quick, auto, thorough or deep")
                    min_effort = None
                require = _required_categories(item.get("require"), f"{where}.require", errors)
                if priority is None and criticality is None and min_effort is None and not require:
                    errors.append(f"{where} sets no priority, criticality, min_effort or require")
                    continue
                rules.append(
                    FocusRule(
                        paths=paths,
                        origin=origin,
                        priority=cast("ReviewFirst | None", priority),
                        criticality=cast("Criticality | None", criticality),
                        min_effort=cast("str | None", min_effort),
                        require=require,
                    )
                )
            if len(raw_focus) > MAX_FOCUS_RULES:
                errors.append(f"focus exceeds {MAX_FOCUS_RULES} rules")
    threshold: FindingThreshold | None = None
    if "finding_threshold" in raw:
        value = raw.get("finding_threshold")
        if value in FINDING_THRESHOLDS:
            threshold = cast(FindingThreshold, value)
        else:
            errors.append("finding_threshold must be critical, high, medium or suggestions")
    return TriageLayer(origin=origin, focus=tuple(rules), finding_threshold=threshold, errors=tuple(errors))


def parse_trusted_base_triage(content: str, *, source: str, commit: str = "") -> TriageLayer | None:
    """The ``trusted_base_repository`` layer from trusted-base ``.lemoncrow/review.yaml`` bytes.

    ``None`` means the document sets no triage policy. Callers pass only
    trusted-base content (``TrustedBaseDocument.content``), never head bytes.
    """

    origin = PolicyOrigin("trusted_base_repository", source or "base Snapshot .lemoncrow/review.yaml", commit)
    try:
        decoded = yaml.safe_load(content) if content.strip() else None
    except yaml.YAMLError as exc:
        return TriageLayer(origin=origin, errors=(f"invalid review configuration: {exc}",))
    if decoded is None:
        return None
    if not isinstance(decoded, Mapping):
        return TriageLayer(origin=origin, errors=("review configuration must be a mapping",))
    section = decoded.get("review")
    if section is None:
        return None
    if not isinstance(section, Mapping):
        return TriageLayer(origin=origin, errors=("review must be a mapping",))
    if "focus" not in section and "finding_threshold" not in section:
        return None
    return parse_triage_settings(section, origin=origin)


def operator_triage_layers(effective: object) -> tuple[TriageLayer, ...]:
    """Operator organization/repository layers from an effective Review policy document.

    Reads ``effective["triage"]["layers"]`` (each ``{origin, settings, errors}``).
    Malformed input yields one error layer: visible, and it disables ``low``
    priority downstream (fail closed).
    """

    if not isinstance(effective, Mapping):
        return ()
    triage = effective.get("triage")
    if triage is None:
        return ()
    malformed = TriageLayer(
        origin=PolicyOrigin("organization", "operator_review_policy"),
        errors=("effective Review policy triage is malformed",),
    )
    if not isinstance(triage, Mapping) or not isinstance(triage.get("layers") or [], list):
        return (malformed,)
    out: list[TriageLayer] = []
    for raw in triage.get("layers") or ():
        if not isinstance(raw, Mapping):
            out.append(malformed)
            continue
        origin = PolicyOrigin.from_wire(raw.get("origin"))
        if origin.layer not in _LAYER_ORDER or origin.layer == "built_in_default":
            origin = PolicyOrigin("organization", origin.source or "operator_review_policy")
        settings = raw.get("settings")
        layer = parse_triage_settings(settings if isinstance(settings, Mapping) else {}, origin=origin)
        carried = tuple(str(item) for item in raw.get("errors") or () if str(item))
        out.append(
            TriageLayer(
                origin=layer.origin,
                focus=layer.focus,
                finding_threshold=layer.finding_threshold,
                errors=tuple(dict.fromkeys((*carried, *layer.errors))),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# effective policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """The effective policy of one path with the rule (and origin) behind each value."""

    path: str
    criticality: Criticality = "normal"
    review_first: ReviewFirst = "normal"
    criticality_rule: FocusRule | None = None
    review_first_rule: FocusRule | None = None
    last_ignored: bool = False
    """A ``last`` rule matched but criticality is critical/high, so it is ignored."""
    min_effort: str | None = None
    require: tuple[str, ...] = ()

    @property
    def criticality_origin(self) -> PolicyOrigin:
        return BUILT_IN_ORIGIN if self.criticality_rule is None else self.criticality_rule.origin

    @property
    def review_first_origin(self) -> PolicyOrigin:
        return BUILT_IN_ORIGIN if self.review_first_rule is None else self.review_first_rule.origin

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "criticality": self.criticality,
            "criticality_rule": None if self.criticality_rule is None else self.criticality_rule.to_wire(),
            "review_first": self.review_first,
            "review_first_rule": None if self.review_first_rule is None else self.review_first_rule.to_wire(),
            "last_ignored": self.last_ignored,
            "min_effort": self.min_effort,
            "require": list(self.require),
        }


@dataclass(frozen=True, slots=True)
class EffectiveTriagePolicy:
    """Resolved review-first/criticality/threshold policy for one Snapshot.

    ``policy_state`` is ``provisional`` when the trusted base could not be read
    (repository rules unknown) and ``invalid`` when any layer reported errors.
    Neither is ever presented as "no policy": downstream, ``low`` priority is
    unavailable unless the state is ``final``.
    """

    layers: tuple[TriageLayer, ...] = ()
    finding_threshold: FindingThreshold = DEFAULT_FINDING_THRESHOLD
    threshold_origin: PolicyOrigin = BUILT_IN_ORIGIN
    policy_state: PolicyState = "final"
    errors: tuple[str, ...] = ()

    def classify(self, path: str) -> PathPolicy:
        """Strictest criticality across layers; last review-first override wins."""

        criticality: Criticality = "normal"
        criticality_rule: FocusRule | None = None
        review_first: ReviewFirst = "normal"
        review_first_rule: FocusRule | None = None
        explicit_floor: str | None = None
        require: list[str] = []
        for layer in self.layers:
            for rule in layer.focus:
                if not rule.matches(path):
                    continue
                if rule.criticality is not None and (
                    criticality_rule is None or CRITICALITY_RANK[rule.criticality] > CRITICALITY_RANK[criticality]
                ):
                    criticality, criticality_rule = rule.criticality, rule
                if rule.priority is not None:
                    review_first, review_first_rule = rule.priority, rule
                if rule.min_effort is not None and (
                    explicit_floor is None or PRESET_RANK[rule.min_effort] > PRESET_RANK[explicit_floor]
                ):
                    explicit_floor = rule.min_effort
                require.extend(item for item in rule.require if item not in require)
        last_ignored = review_first == "last" and CRITICALITY_RANK[criticality] >= CRITICALITY_RANK["high"]
        implied = CRITICALITY_MIN_EFFORT.get(criticality)
        floors = [value for value in (explicit_floor, implied) if value is not None]
        return PathPolicy(
            path=path,
            criticality=criticality,
            review_first="normal" if last_ignored else review_first,
            criticality_rule=criticality_rule,
            review_first_rule=review_first_rule,
            last_ignored=last_ignored,
            min_effort=max(floors, key=lambda value: PRESET_RANK[value]) if floors else None,
            require=tuple(require),
        )

    def review_criticality(self, paths: Iterable[str]) -> tuple[Criticality, tuple[PathPolicy, ...]]:
        """The strictest criticality over *paths* and the path policies that set it.

        No changed path means no policy evidence: ``normal`` (never ``low``).
        """

        decisions = [self.classify(path) for path in dict.fromkeys(paths)]
        if not decisions:
            return "normal", ()
        strictest = max(decisions, key=lambda item: CRITICALITY_RANK[item.criticality]).criticality
        return strictest, tuple(item for item in decisions if item.criticality == strictest)

    def required_focus(self, paths: Iterable[str]) -> tuple[RequiredFocus, ...]:
        """Every ``focus.require`` category that matches a changed path, with its paths and origins.

        Every layer contributes (requirements only accumulate; no layer can
        remove another's). Ordered by :data:`REQUIRED_FOCUS_CATEGORIES`.
        """

        by_category: dict[str, tuple[list[str], list[PolicyOrigin]]] = {}
        for path in dict.fromkeys(paths):
            for layer in self.layers:
                for rule in layer.focus:
                    if not rule.require or not rule.matches(path):
                        continue
                    for category in rule.require:
                        matched, origins = by_category.setdefault(category, ([], []))
                        if path not in matched:
                            matched.append(path)
                        if rule.origin not in origins:
                            origins.append(rule.origin)
        return tuple(
            RequiredFocus(category, tuple(by_category[category][0]), tuple(by_category[category][1]))
            for category in REQUIRED_FOCUS_CATEGORIES
            if category in by_category
        )

    def effort_floors(self) -> tuple[FloorRule, ...]:
        """Path-scoped R1 ``min_effort`` floors: explicit focus floors plus those criticality implies.

        Additive by construction: R1 applies every matching floor and keeps the
        strictest depth, so no layer can lower another's floor and a per-Review
        request can only raise depth. A Review-first priority or a priority
        override never reaches this list.
        """

        from lemoncrow.pro.capabilities.review.run_plan import FloorRule, SettingOrigin

        floors: dict[tuple[str, tuple[str, ...], str], FloorRule] = {}
        for layer in self.layers:
            for rule in layer.focus:
                origin_layer = rule.origin.layer if rule.origin.layer in _LAYER_ORDER else "organization"
                where = rule.origin.label.split(" · ", 1)[-1]
                implied = CRITICALITY_MIN_EFFORT.get(rule.criticality or "")
                for value, why in (
                    (rule.min_effort, "min_effort"),
                    (implied, f"criticality {rule.criticality}"),
                ):
                    if value is None:
                        continue
                    source = f"{where} focus {rule.pattern} ({why})"
                    floors.setdefault(
                        (value, rule.paths, source),
                        FloorRule("min_effort", value, SettingOrigin(origin_layer, source), rule.paths),
                    )
        return tuple(floors.values())

    def to_wire(self) -> dict[str, Any]:
        return {
            "finding_threshold": {
                "effective_value": self.finding_threshold,
                "label": THRESHOLD_LABELS[self.finding_threshold],
                "mode": "inherit" if self.threshold_origin.layer == "built_in_default" else "override",
                "origin": self.threshold_origin.to_wire(),
            },
            "layers": [layer.to_wire() for layer in self.layers],
            "policy_state": self.policy_state,
            "errors": list(self.errors),
        }


def resolve_triage_policy(
    layers: Sequence[TriageLayer | None],
    *,
    trusted_base_state: str = "present",
) -> EffectiveTriagePolicy:
    """Apply layers low-to-high precedence (inherit or explicit override per key).

    ``trusted_base_state`` is the ``TrustedBaseDocument.state`` the repository
    layer came from; ``provisional`` means repository policy is unknown.
    """

    present = sorted(
        (layer for layer in layers if layer is not None),
        key=lambda layer: _LAYER_ORDER.index(layer.origin.layer) if layer.origin.layer in _LAYER_ORDER else 1,
    )
    threshold: FindingThreshold = DEFAULT_FINDING_THRESHOLD
    threshold_origin = BUILT_IN_ORIGIN
    errors: list[str] = []
    for layer in present:
        errors.extend(f"{layer.origin.label}: {error}" for error in layer.errors)
        if layer.finding_threshold is not None:
            threshold, threshold_origin = layer.finding_threshold, layer.origin
    state: PolicyState = "invalid" if errors else "provisional" if trusted_base_state == "provisional" else "final"
    return EffectiveTriagePolicy(
        layers=tuple(present),
        finding_threshold=threshold,
        threshold_origin=threshold_origin,
        policy_state=state,
        errors=tuple(dict.fromkeys(errors)),
    )


def triage_policy_for(effective: object, config: Any, *, commit: str = "") -> EffectiveTriagePolicy:
    """Operator layers from *effective* plus the trusted-base layer from *config*.

    *config* is the Snapshot's ``TrustedBaseDocument`` for ``.lemoncrow/review.yaml``
    (local base or hosted trusted-base capture). Shared by the Review API and
    the run-plan service so ordering and depth floors read one policy.
    """

    content = str(getattr(config, "content", "") or "")
    source = (
        str(getattr(config, "source", "") or "") or f"base Snapshot {getattr(config, 'path', '.lemoncrow/review.yaml')}"
    )
    trusted = parse_trusted_base_triage(content, source=source, commit=commit) if content else None
    return resolve_triage_policy(
        [*operator_triage_layers(effective), trusted],
        trusted_base_state=str(getattr(config, "state", "present") or "present"),
    )


# --------------------------------------------------------------------------- #
# finding severity and threshold
# --------------------------------------------------------------------------- #


def _score_band(score: object) -> str:
    """A CVSS-like ``security-severity`` score as the advisory vocabulary (``""`` when absent)."""

    try:
        value = float(str(score))
    except (TypeError, ValueError):
        return ""
    if value >= 9.0:
        return "critical"
    if value >= 7.0:
        return "high"
    if value >= 4.0:
        return "medium"
    return "low" if value > 0 else ""


def normalize_finding_severity(
    source: str, *, category: str = "", raw: str = "", critical: bool = False, score: object = None
) -> FindingSeverity | None:
    """Normalize one producer's severity vocabulary to ``p0 | p1 | p2 | suggestion``.

    * ``ai_review``: from the validated finding *category* only (``critical_rule``
      -- a critical/blocker organization rule -- is the only P0).
    * ``behavior``: R2 delta severity ``high | medium | low``; R2's ``critical``
      flag (a test regression, a status change, a visual-threshold excess)
      raises it to at least P1. It never makes P0: that stays a policy decision.
    * ``vulnerability``: advisory severity (``critical | high | moderate | low``).
    * ``static_analysis``: an imported SARIF/check-run result. A numeric
      ``security-severity`` *score* (GitHub code-scanning convention) is read as
      the advisory vocabulary (>=9 critical, >=7 high, >=4 medium, else low);
      otherwise its *raw* level: ``error``/``failure`` P2, anything else a
      suggestion. Always rated.

    ``None`` means unrated. Unrated is never treated as low: it is always
    surfaced and counted separately.
    """

    if source == "ai_review":
        return _AI_CATEGORY_SEVERITY.get(category)
    if source == "behavior":
        severity = _BEHAVIOR_SEVERITY.get(raw.strip().lower())
        if critical and (severity is None or SEVERITY_RANK[severity] < SEVERITY_RANK["p1"]):
            return "p1"
        return severity
    if source == "vulnerability":
        return _VULNERABILITY_SEVERITY.get(raw.strip().lower())
    if source == "static_analysis":
        band = _score_band(score)
        if band:
            return _VULNERABILITY_SEVERITY[band]
        return _STATIC_LEVEL_SEVERITY.get(raw.strip().lower(), "suggestion")
    return None


def surfaced(severity: FindingSeverity | None, threshold: FindingThreshold) -> bool:
    """Whether a finding of *severity* is shown under *threshold*.

    P0 and unrated findings are always shown. The threshold changes only what
    is surfaced; nothing it decides reaches the run plan or a budget.
    """

    if severity is None or severity == "p0":
        return True
    return SEVERITY_RANK[severity] >= SEVERITY_RANK[_THRESHOLD_MINIMUM[threshold]]


# --------------------------------------------------------------------------- #
# canonical queue reasons
# --------------------------------------------------------------------------- #


def _short(rule: FocusRule | None) -> str:
    return "policy" if rule is None else f"{rule.pattern} policy"


def path_queue_reasons(policy: PathPolicy) -> list[QueueReason]:
    """The visible queue reasons one path's policy contributes (plan §3.15 wording)."""

    reasons: list[QueueReason] = []
    if policy.review_first == "first":
        reasons.append(QueueReason("review_first_policy", f"Review first · {_short(policy.review_first_rule)}"))
    if policy.criticality == "critical":
        reasons.append(QueueReason("criticality_policy", f"Critical · required by {_short(policy.criticality_rule)}"))
    elif policy.criticality == "high":
        reasons.append(
            QueueReason("criticality_policy", f"High criticality · required by {_short(policy.criticality_rule)}")
        )
    if policy.review_first == "last":
        reasons.append(QueueReason("review_last_policy", f"Review last · {_short(policy.review_first_rule)}"))
    return reasons


def target_policy_reasons(targets: Iterable[Any], policy: EffectiveTriagePolicy) -> dict[str, list[QueueReason]]:
    """Policy queue reasons per target id (targets need ``target_id`` and ``path``).

    ``review_priority`` attaches them to outstanding targets only, so policy
    reorders work but never reopens a judgment.
    """

    by_path: dict[str, list[QueueReason]] = {}
    out: dict[str, list[QueueReason]] = {}
    for target in targets:
        path = str(getattr(target, "path", "") or "")
        if path not in by_path:
            by_path[path] = path_queue_reasons(policy.classify(path)) if path else []
        if by_path[path]:
            out[str(target.target_id)] = list(by_path[path])
    return out


def strictest_path_policy(policy: EffectiveTriagePolicy, paths: Iterable[str]) -> PathPolicy | None:
    """One combined policy for an item spanning several paths (a behavior target).

    Strictest criticality wins; ``first`` beats ``normal`` beats ``last`` so a
    multi-path item is never demoted by one of its paths.
    """

    decisions = [policy.classify(path) for path in dict.fromkeys(paths) if path]
    if not decisions:
        return None
    order = {"first": 0, "normal": 1, "last": 2}
    critical = max(decisions, key=lambda item: CRITICALITY_RANK[item.criticality])
    first = min(decisions, key=lambda item: order[item.review_first])
    review_first = first.review_first
    last_ignored = review_first == "last" and CRITICALITY_RANK[critical.criticality] >= CRITICALITY_RANK["high"]
    return PathPolicy(
        path=first.path,
        criticality=critical.criticality,
        review_first="normal" if last_ignored else review_first,
        criticality_rule=critical.criticality_rule,
        review_first_rule=first.review_first_rule,
        last_ignored=last_ignored or any(item.last_ignored for item in decisions),
        min_effort=critical.min_effort,
        require=critical.require,
    )


__all__ = [
    "BUILT_IN_ORIGIN",
    "CRITICALITY_MIN_EFFORT",
    "CRITICALITY_RANK",
    "CRITICALITY_VALUES",
    "DEFAULT_FINDING_THRESHOLD",
    "FINDING_SEVERITIES",
    "FINDING_THRESHOLDS",
    "MAX_FOCUS_RULES",
    "REQUIRED_FOCUS_CATEGORIES",
    "REVIEW_FIRST_VALUES",
    "SEVERITY_LABELS",
    "SEVERITY_RANK",
    "THRESHOLD_LABELS",
    "Criticality",
    "EffectiveTriagePolicy",
    "FindingSeverity",
    "FindingThreshold",
    "FocusRule",
    "PathPolicy",
    "PolicyOrigin",
    "PolicyState",
    "RequiredFocus",
    "ReviewFirst",
    "TriageLayer",
    "normalize_finding_severity",
    "operator_triage_layers",
    "parse_triage_settings",
    "parse_trusted_base_triage",
    "path_queue_reasons",
    "resolve_triage_policy",
    "strictest_path_policy",
    "surfaced",
    "target_policy_reasons",
    "triage_policy_for",
]
