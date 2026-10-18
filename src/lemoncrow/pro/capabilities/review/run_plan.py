"""Run planner: one cost ladder over the capability catalog (Phase 9R).

Users choose three things -- depth (``effort``), where it runs (``run_on``) and a
budget -- and may opt into exact control (``effort: custom``). This module
resolves those settings, policy floors, trusted-base applicability and the
budget into the **effective run plan**: every catalog entry with a status
(run, on request, skipped, inapplicable, not available, limited by budget), its
origin (preset, custom, profile, policy floor, manual, family override) and its
estimated cost.

The plan is persisted per Snapshot with a digest over its execution content,
so executors consume exactly the plan the Review displays. Presets are pure
selections: a Custom selection with the same resolved entries produces the same
execution document and digest (origins are display metadata, not identity).

No default ceiling is configured here. Budget ceilings gate work only when an
organization or trusted-base config sets them; the plan's proposed defaults are
documented suggestions (``PROPOSED_BUDGET_DEFAULTS``) pending product-owner
approval.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.capability_catalog import (
    COST_RANK,
    DEGRADE_ORDER,
    EXECUTOR_LOCI,
    FAMILIES,
    FAMILY_MODES,
    PRESET_RANK,
    PRESET_SELECTIONS,
    PRESETS,
    REVIEW_HOST,
    SIDES_VALUES,
    STATIC_CATALOG,
    WHEN_VALUES,
    CapabilityFamily,
    CatalogEntry,
    DetectedCapabilities,
    DynamicTarget,
    ExecutorLocus,
    FamilyMode,
    Preset,
    PresetEntry,
    Sides,
    When,
    applicability,
    expand_wildcard,
    paths_match,
    preset_entry,
    resolve_entry,
    valid_entry_id,
)
from lemoncrow.pro.capabilities.review.check_ingest import IngestedSatisfaction
from lemoncrow.pro.capabilities.review.depth_policy import PathEffortRule, parse_path_effort, path_default_depth
from lemoncrow.pro.capabilities.review.escalation import (
    EscalationFacts,
    EscalationPolicy,
    apply_escalation,
    decide_escalation,
    escalation_document,
    finalize_escalation,
    parse_escalation,
)
from lemoncrow.pro.capabilities.review.fast_lane import FastLaneSignals, apply_federation
from lemoncrow.pro.capabilities.review.independence import POLICY_KEY as INDEPENDENCE_POLICY_KEY
from lemoncrow.pro.capabilities.review.independence import (
    IndependenceRule,
    independence_document,
    parse_independence_policy,
)
from lemoncrow.pro.capabilities.review.rule_checks import (
    RuleQuestion,
    apply_rule_checks,
    parse_rule_checks,
    rule_check_settings_wire,
)
from lemoncrow.pro.capabilities.review.test_impact import EntryImpact, apply_impact, selection_execution_document

RUN_PLAN_KIND: Final[str] = "run_plan"
RUN_PLAN_SCOPE: Final[str] = "current"
RUN_PLAN_VERSION: Final[int] = 1
#: Reason prefix of an entry skipped because no changed path is in its scope (not affected).
PATH_SCOPE_SKIP: Final[str] = "no changed path matches"

# ``satisfied``: an equivalent current-Snapshot result was ingested, so the entry
# is not re-run (P11-FED); ``satisfied_by`` names that result.
EntryStatus = Literal["run", "on_request", "skipped", "inapplicable", "not_available", "limited_by_budget", "satisfied"]
# ``escalation``: raised inside the Auto rung by deterministic signals (P11-ESC).
EntryOrigin = Literal["preset", "custom", "profile", "policy_floor", "manual", "family_override", "escalation"]
Trigger = Literal["open", "push"]

BUDGET_FIELDS: Final[tuple[str, ...]] = (
    "per_review_minutes",
    "per_review_model_calls",
    "per_review_model_cost_usd",
    "monthly_minutes",
    "monthly_model_calls",
    "monthly_managed_usd",
)
_INTEGER_BUDGET_FIELDS: Final[frozenset[str]] = frozenset({"per_review_model_calls", "monthly_model_calls"})

#: The plan's YAML example values. Documented suggestions only: nothing in this
#: build applies them unless an organization or trusted-base config sets them.
PROPOSED_BUDGET_DEFAULTS: Final[Mapping[str, float | int]] = {
    "per_review_minutes": 15,
    "per_review_model_calls": 6,
    "monthly_managed_usd": 200,
}

_LAYER_ORDER: Final[tuple[str, ...]] = (
    "built_in_default",
    "organization",
    "team",
    "repository",
    "trusted_base_repository",
    "directory",
)
_MAX_SELECTION: Final[int] = 256


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SettingOrigin:
    layer: str
    source: str

    def to_wire(self) -> dict[str, str]:
        return {"layer": self.layer, "source": self.source}


BUILT_IN_ORIGIN: Final[SettingOrigin] = SettingOrigin("built_in_default", "built_in_default")


@dataclass(frozen=True, slots=True)
class EntrySelection:
    """One selected catalog ID; ``None`` fields inherit from the base preset."""

    id: str
    when: When | None = None
    sides: Sides | None = None
    paths: tuple[str, ...] | None = None
    executor: ExecutorLocus | None = None

    def to_document(self) -> str | dict[str, Any]:
        values: dict[str, Any] = {"id": self.id}
        if self.when is not None:
            values["when"] = self.when
        if self.sides is not None:
            values["sides"] = self.sides
        if self.paths is not None:
            values["paths"] = list(self.paths)
        if self.executor is not None:
            values["executor"] = self.executor
        return self.id if len(values) == 1 else values


#: No Review label source exists yet: ``PlanInputs.labels`` is always empty, so a
#: label rule can never select its profile. Settings and plans say so instead of
#: silently never matching.
# lc-debt: label-rule profiles are inactive (no label source); upgrade path: P9-INBOX/labels
# feeds PlanInputs.labels from provider labels and flips this flag.
LABEL_SOURCE_AVAILABLE: Final[bool] = False
LABEL_RULE_INACTIVE: Final[str] = "label rule inactive (no label source)"
#: A required LemonCrow-owned check (or capability floor) no catalog executor can run.
REQUIRED_NO_EXECUTOR: Final[str] = "required · no executor"


@dataclass(frozen=True, slots=True)
class FloorRule:
    """A policy floor, optionally path-scoped.

    ``capability`` names a catalog ID, ``min_effort`` a minimum depth, and
    ``check`` the stable key of a typed LemonCrow-owned required check
    (``lemoncrow_verification``), mapped onto the catalog by ``check_catalog_id``.
    """

    kind: Literal["capability", "min_effort", "check"]
    value: str
    origin: SettingOrigin
    paths: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "paths": list(self.paths), "origin": self.origin.to_wire()}


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    base: Preset
    entries: tuple[EntrySelection, ...]

    def to_document(self) -> dict[str, Any]:
        return {"from": self.base, "custom": [row.to_document() for row in self.entries]}


@dataclass(frozen=True, slots=True)
class ProfileRule:
    profile: str
    paths: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def matches(
        self,
        changed_paths: Sequence[str],
        labels: Sequence[str],
        *,
        label_source: bool = LABEL_SOURCE_AVAILABLE,
    ) -> bool:
        path_ok = bool(self.paths) and paths_match(self.paths, changed_paths)
        label_ok = label_source and bool(self.labels) and bool(set(self.labels) & set(labels))
        return path_ok or label_ok

    @property
    def label_rule_inactive(self) -> bool:
        return bool(self.labels) and not LABEL_SOURCE_AVAILABLE

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {"profile": self.profile}
        if self.paths:
            out["paths"] = list(self.paths)
        if self.labels:
            out["labels"] = list(self.labels)
        return out

    def to_wire(self) -> dict[str, Any]:
        """Display form: the document plus whether its label rule can match here."""

        out = self.to_document()
        if self.labels:
            out["label_rule"] = "inactive" if self.label_rule_inactive else "active"
            if self.label_rule_inactive:
                out["note"] = LABEL_RULE_INACTIVE
        return out


@dataclass(frozen=True, slots=True)
class RunSettingsLayer:
    """One settings layer. ``None``/absent means inherit (no hidden copies)."""

    origin: SettingOrigin
    effort: str | None = None
    custom_from: Preset | None = None
    custom: tuple[EntrySelection, ...] | None = None
    run_on: ExecutorLocus | None = None
    budget: Mapping[str, float | int] = field(default_factory=dict)
    capabilities: Mapping[str, FamilyMode] = field(default_factory=dict)
    ai_depth: Preset | None = None
    ai_max_parallel: int | None = None
    profiles: Mapping[str, Profile] = field(default_factory=dict)
    profile_rules: tuple[ProfileRule, ...] = ()
    floors: tuple[FloorRule, ...] = ()
    errors: tuple[str, ...] = ()
    # P11-ESC: explicit ``escalation``/``rule_checks`` keys and path depth defaults.
    escalation: Mapping[str, Any] = field(default_factory=dict)
    rule_checks: Mapping[str, Any] = field(default_factory=dict)
    path_effort: tuple[PathEffortRule, ...] = ()
    # P11 item 14: ``independent_ai_reviewer`` (a floor; independence.py).
    independence: tuple[IndependenceRule, ...] = ()

    def reset_to_preset(self) -> RunSettingsLayer:
        """Drop the explicit Custom override and restore preset inheritance."""

        effort = self.custom_from if self.effort == "custom" else self.effort
        return replace(self, effort=effort, custom=None, custom_from=None)

    def to_document(self) -> dict[str, Any]:
        """``review:``-shaped document; parsing it yields an equal layer."""

        out: dict[str, Any] = {}
        if self.effort is not None:
            out["effort"] = self.effort
        if self.custom_from is not None:
            out["custom_from"] = self.custom_from
        if self.custom is not None:
            out["custom"] = [row.to_document() for row in self.custom]
        if self.run_on is not None:
            out["run_on"] = self.run_on
        if self.budget:
            out["budget"] = dict(self.budget)
        if self.capabilities:
            out["capabilities"] = dict(self.capabilities)
        ai: dict[str, Any] = {}
        if self.ai_depth is not None:
            ai["effort"] = self.ai_depth
        if self.ai_max_parallel is not None:
            ai["max_parallel"] = self.ai_max_parallel
        if ai:
            out["ai"] = ai
        if self.profiles:
            out["profiles"] = {name: profile.to_document() for name, profile in sorted(self.profiles.items())}
        if self.profile_rules:
            out["profile_rules"] = [rule.to_document() for rule in self.profile_rules]
        required = [floor for floor in self.floors if floor.kind == "capability"]
        minimum = [floor for floor in self.floors if floor.kind == "min_effort"]
        if required:
            out["required"] = [
                floor.value if not floor.paths else {"id": floor.value, "paths": list(floor.paths)}
                for floor in required
            ]
        if minimum:
            out["min_effort"] = [{"effort": floor.value, "paths": list(floor.paths)} for floor in minimum]
        if self.escalation:
            out["escalation"] = escalation_document(self.escalation)
        if self.rule_checks:
            out["rule_checks"] = dict(self.rule_checks)
        if self.path_effort:
            out["path_effort"] = [rule.to_document() for rule in self.path_effort]
        if self.independence:
            out[INDEPENDENCE_POLICY_KEY] = independence_document(self.independence)
        return out

    def to_wire(self) -> dict[str, Any]:
        return {"origin": self.origin.to_wire(), "settings": self.to_document(), "errors": list(self.errors)}


def _strings(raw: object, field_name: str, errors: list[str], *, limit: int = 64) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list of strings")
        return ()
    out: list[str] = []
    for item in raw[:limit]:
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            errors.append(f"{field_name} entries must be non-empty strings")
            continue
        out.append(item.strip())
    if len(raw) > limit:
        errors.append(f"{field_name} exceeds {limit} entries")
    return tuple(dict.fromkeys(out))


def _selection(raw: object, field_name: str, errors: list[str]) -> tuple[EntrySelection, ...]:
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list of catalog IDs or objects")
        return ()
    rows: list[EntrySelection] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:_MAX_SELECTION]):
        where = f"{field_name}[{index}]"
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, Mapping):
            errors.append(f"{where} must be a catalog ID or an object with id")
            continue
        unknown = sorted(set(item) - {"id", "when", "sides", "paths", "executor"})
        if unknown:
            errors.append(f"{where} has unknown field {unknown[0]!r}")
        entry_id = str(item.get("id") or "").strip()
        if not valid_entry_id(entry_id):
            errors.append(f"{where}: unknown capability id {entry_id!r}")
            continue
        if entry_id in seen:
            errors.append(f"{where}: duplicate capability id {entry_id!r}")
            continue
        seen.add(entry_id)
        when = item.get("when")
        if when is not None and when not in WHEN_VALUES:
            errors.append(f"{where}.when must be open, push or manual")
            when = None
        sides = item.get("sides")
        if sides is not None and sides not in SIDES_VALUES:
            errors.append(f"{where}.sides must be head or base+head")
            sides = None
        executor = item.get("executor")
        if executor is not None and executor not in EXECUTOR_LOCI:
            errors.append(f"{where}.executor must be local, worker or managed")
            executor = None
        paths = _strings(item.get("paths"), f"{where}.paths", errors) if "paths" in item else None
        rows.append(
            EntrySelection(
                id=entry_id,
                when=cast("When | None", when),
                sides=cast("Sides | None", sides),
                paths=paths,
                executor=cast("ExecutorLocus | None", executor),
            )
        )
    if len(raw) > _MAX_SELECTION:
        errors.append(f"{field_name} exceeds {_MAX_SELECTION} entries")
    return tuple(rows)


def _number(raw: object, field_name: str, errors: list[str], *, integer: bool) -> float | int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(f"{field_name} must be a non-negative number")
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        errors.append(f"{field_name} must be a non-negative number")
        return None
    if integer:
        if int(value) != value:
            errors.append(f"{field_name} must be a whole number")
            return None
        return int(value)
    return value


_LAYER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "effort",
        "custom_from",
        "custom",
        "run_on",
        "budget",
        "capabilities",
        "ai",
        "profiles",
        "profile_rules",
        "required",
        "min_effort",
        "escalation",
        "rule_checks",
        "path_effort",
        "independent_ai_reviewer",
    }
)


def parse_run_settings(
    raw: Mapping[str, Any],
    *,
    origin: SettingOrigin,
    allow_profiles: bool = False,
    allow_effort: bool = True,
    ignore_keys: Iterable[str] = (),
) -> RunSettingsLayer:
    """Parse one ``review:``-shaped settings mapping, collecting visible errors.

    Unknown catalog IDs, unknown keys and malformed values are reported in
    ``errors``; they are never silently dropped into an effective setting.
    """

    errors: list[str] = []
    ignored = set(ignore_keys)
    unknown = sorted(set(raw) - _LAYER_KEYS - ignored)
    for key in unknown:
        errors.append(f"unknown review setting {key!r}")

    effort: str | None = None
    if "effort" in raw and allow_effort:
        value = raw.get("effort")
        if value in (*PRESETS, "custom"):
            effort = str(value)
        else:
            errors.append("effort must be quick, auto, thorough, deep or custom")
    custom_from: Preset | None = None
    if "custom_from" in raw:
        value = raw.get("custom_from")
        if value in PRESETS:
            custom_from = cast(Preset, value)
        else:
            errors.append("custom_from must be quick, auto, thorough or deep")
    custom = _selection(raw.get("custom"), "custom", errors) if "custom" in raw else None
    if effort == "custom" and custom is None:
        errors.append("effort custom requires a custom selection")
    run_on: ExecutorLocus | None = None
    if "run_on" in raw:
        value = raw.get("run_on")
        if value in EXECUTOR_LOCI:
            run_on = cast(ExecutorLocus, value)
        else:
            errors.append("run_on must be local, worker or managed")

    budget: dict[str, float | int] = {}
    raw_budget = raw.get("budget")
    if raw_budget is not None:
        if not isinstance(raw_budget, Mapping):
            errors.append("budget must be a mapping")
        else:
            for key, value in raw_budget.items():
                if key not in BUDGET_FIELDS:
                    errors.append(f"unknown budget field {key!r}")
                    continue
                parsed = _number(value, f"budget.{key}", errors, integer=key in _INTEGER_BUDGET_FIELDS)
                if parsed is not None:
                    budget[str(key)] = parsed

    capabilities: dict[str, FamilyMode] = {}
    raw_caps = raw.get("capabilities")
    if raw_caps is not None:
        if not isinstance(raw_caps, Mapping):
            errors.append("capabilities must map families to off, auto or always")
        else:
            for family, mode in raw_caps.items():
                # YAML 1.1 reads a bare ``off`` as False; accept the intent.
                normalized = "off" if mode is False else mode
                if family not in FAMILIES:
                    errors.append(f"unknown capability family {family!r}")
                elif normalized not in FAMILY_MODES:
                    errors.append(f"capabilities.{family} must be off, auto or always")
                else:
                    capabilities[str(family)] = cast(FamilyMode, normalized)

    ai_depth: Preset | None = None
    ai_max_parallel: int | None = None
    raw_ai = raw.get("ai")
    if raw_ai is not None:
        # Legacy review.ai.* keys (plan section 7.2) are accepted as ai-family
        # overrides on the same ladder rather than as a second settings model.
        if not isinstance(raw_ai, Mapping):
            errors.append("ai must be a mapping")
        else:
            for key in sorted(set(raw_ai) - {"effort", "max_calls", "max_parallel", "max_cost_usd", "mode", "enabled"}):
                errors.append(f"unknown ai setting {key!r}")
            if "effort" in raw_ai:
                if raw_ai["effort"] in PRESETS:
                    ai_depth = cast(Preset, raw_ai["effort"])
                else:
                    errors.append("ai.effort must be quick, auto, thorough or deep")
            if "max_calls" in raw_ai:
                parsed = _number(raw_ai["max_calls"], "ai.max_calls", errors, integer=True)
                if parsed is not None:
                    budget.setdefault("per_review_model_calls", parsed)
            if "max_cost_usd" in raw_ai:
                parsed = _number(raw_ai["max_cost_usd"], "ai.max_cost_usd", errors, integer=False)
                if parsed is not None:
                    budget.setdefault("per_review_model_cost_usd", parsed)
            if "max_parallel" in raw_ai:
                parsed = _number(raw_ai["max_parallel"], "ai.max_parallel", errors, integer=True)
                if parsed is not None:
                    ai_max_parallel = max(1, int(parsed))
            mode = raw_ai.get("mode")
            if raw_ai.get("enabled") is False:
                mode = "off"
            if mode is False:
                mode = "off"
            if mode is not None:
                if mode in FAMILY_MODES:
                    capabilities.setdefault("ai", cast(FamilyMode, mode))
                else:
                    errors.append("ai.mode must be off, auto or always")

    profiles: dict[str, Profile] = {}
    rules: list[ProfileRule] = []
    if "profiles" in raw or "profile_rules" in raw:
        if not allow_profiles:
            errors.append("named profiles may only be defined by organization policy")
        else:
            raw_profiles = raw.get("profiles") or {}
            if not isinstance(raw_profiles, Mapping):
                errors.append("profiles must map names to selections")
            else:
                for name, body in raw_profiles.items():
                    if not isinstance(name, str) or not name.strip() or len(name) > 64:
                        errors.append("profile names must be short non-empty strings")
                        continue
                    if not isinstance(body, Mapping):
                        errors.append(f"profile {name!r} must be a mapping with custom")
                        continue
                    base = body.get("from", "auto")
                    if base not in PRESETS:
                        errors.append(f"profile {name!r} from must be a preset")
                        base = "auto"
                    profiles[name] = Profile(
                        name=name,
                        base=cast(Preset, base),
                        entries=_selection(body.get("custom", []), f"profiles.{name}.custom", errors),
                    )
            raw_rules = raw.get("profile_rules") or []
            if not isinstance(raw_rules, list):
                errors.append("profile_rules must be a list")
            else:
                for index, item in enumerate(raw_rules[:64]):
                    if not isinstance(item, Mapping) or str(item.get("profile") or "") not in profiles:
                        errors.append(f"profile_rules[{index}] must name a defined profile")
                        continue
                    rule = ProfileRule(
                        profile=str(item["profile"]),
                        paths=_strings(item.get("paths"), f"profile_rules[{index}].paths", errors),
                        labels=_strings(item.get("labels"), f"profile_rules[{index}].labels", errors),
                    )
                    if not rule.paths and not rule.labels:
                        errors.append(f"profile_rules[{index}] needs paths or labels")
                        continue
                    rules.append(rule)

    floors: list[FloorRule] = []
    raw_required = raw.get("required")
    if raw_required is not None:
        for index, item in enumerate(raw_required if isinstance(raw_required, list) else [raw_required]):
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, Mapping):
                errors.append(f"required[{index}] must be a catalog ID or an object with id")
                continue
            entry_id = str(item.get("id") or "").strip()
            if not valid_entry_id(entry_id):
                errors.append(f"required[{index}]: unknown capability id {entry_id!r}")
                continue
            floors.append(
                FloorRule(
                    "capability",
                    entry_id,
                    origin,
                    _strings(item.get("paths"), f"required[{index}].paths", errors),
                )
            )
    raw_min = raw.get("min_effort")
    if raw_min is not None:
        for index, item in enumerate(raw_min if isinstance(raw_min, list) else [raw_min]):
            if isinstance(item, str):
                item = {"effort": item}
            if not isinstance(item, Mapping) or item.get("effort") not in PRESETS:
                errors.append(f"min_effort[{index}] must name quick, auto, thorough or deep")
                continue
            floors.append(
                FloorRule(
                    "min_effort",
                    str(item["effort"]),
                    origin,
                    _strings(item.get("paths"), f"min_effort[{index}].paths", errors),
                )
            )
    escalation = parse_escalation(raw["escalation"], errors) if "escalation" in raw else {}
    rule_checks = parse_rule_checks(raw["rule_checks"], errors) if "rule_checks" in raw else {}
    path_effort = parse_path_effort(raw["path_effort"], origin, errors) if "path_effort" in raw else ()
    independence = (
        parse_independence_policy(raw[INDEPENDENCE_POLICY_KEY], origin, errors)
        if INDEPENDENCE_POLICY_KEY in raw
        else ()
    )

    return RunSettingsLayer(
        origin=origin,
        effort=effort,
        custom_from=custom_from,
        custom=custom,
        run_on=run_on,
        budget=budget,
        capabilities=capabilities,
        ai_depth=ai_depth,
        ai_max_parallel=ai_max_parallel,
        profiles=profiles,
        profile_rules=tuple(rules),
        floors=tuple(floors),
        errors=tuple(errors),
        escalation=escalation,
        rule_checks=rule_checks,
        path_effort=path_effort,
        independence=independence,
    )


def parse_review_config_layer(text: str, *, source: str) -> RunSettingsLayer | None:
    """The ``review:`` section of a trusted-base ``.lemoncrow/review.yaml``.

    ``None`` means the document has no ``review:`` section. Head-side config is
    never passed here: it is proposed future policy for later Reviews only.
    """

    origin = SettingOrigin("trusted_base_repository", source or "base Snapshot .lemoncrow/review.yaml")
    try:
        decoded = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError as exc:
        return RunSettingsLayer(origin=origin, errors=(f"invalid review configuration: {exc}",))
    if decoded is None:
        return None
    if not isinstance(decoded, Mapping):
        return RunSettingsLayer(origin=origin, errors=("review configuration must be a mapping",))
    section = decoded.get("review")
    if section is None:
        return None
    if not isinstance(section, Mapping):
        return RunSettingsLayer(origin=origin, errors=("review must be a mapping",))
    # ``review.auto`` is the trigger model owned by the hosted behavior contract;
    # ``focus``/``finding_threshold`` are triage policy (``criticality.py``).
    return parse_run_settings(section, origin=origin, ignore_keys=("auto", "focus", "finding_threshold"))


def layer_from_wire(raw: Mapping[str, Any]) -> RunSettingsLayer:
    origin_raw = raw.get("origin")
    origin = (
        SettingOrigin(str(origin_raw.get("layer") or "organization"), str(origin_raw.get("source") or ""))
        if isinstance(origin_raw, Mapping)
        else SettingOrigin("organization", "")
    )
    settings = raw.get("settings")
    layer = parse_run_settings(
        settings if isinstance(settings, Mapping) else {},
        origin=origin,
        allow_profiles=origin.layer == "organization",
    )
    carried = tuple(str(item) for item in raw.get("errors") or () if str(item))
    return replace(layer, errors=tuple(dict.fromkeys((*carried, *layer.errors))))


@dataclass(frozen=True, slots=True)
class EffectiveRunSettings:
    effort: str = "auto"
    custom_from: Preset = "auto"
    custom: tuple[EntrySelection, ...] = ()
    run_on: ExecutorLocus = "local"
    budget: Mapping[str, float | int] = field(default_factory=dict)
    capabilities: Mapping[str, FamilyMode] = field(default_factory=lambda: {family: "auto" for family in FAMILIES})
    ai_depth: Preset | None = None
    ai_max_parallel: int | None = None
    profiles: Mapping[str, Profile] = field(default_factory=dict)
    profile_rules: tuple[ProfileRule, ...] = ()
    floors: tuple[FloorRule, ...] = ()
    origins: Mapping[str, SettingOrigin] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    # P11-ESC: explicit keys only (built-in defaults live in escalation/rule_checks).
    escalation: Mapping[str, Any] = field(default_factory=dict)
    rule_checks: Mapping[str, Any] = field(default_factory=dict)
    path_effort: tuple[PathEffortRule, ...] = ()
    #: P11 item 14: every layer's ``independent_ai_reviewer`` rule, low to high precedence.
    independence: tuple[IndependenceRule, ...] = ()

    def origin(self, key: str) -> SettingOrigin:
        return self.origins.get(key, BUILT_IN_ORIGIN)

    def digest_document(self) -> dict[str, Any]:
        # P11-ESC keys only when set, so every plan without them keeps its digest.
        extra: dict[str, Any] = {}
        if self.independence:
            extra[INDEPENDENCE_POLICY_KEY] = [rule.to_wire() for rule in self.independence]
        if self.escalation:
            extra["escalation"] = escalation_document(dict(sorted(self.escalation.items())))
        if self.rule_checks:
            extra["rule_checks"] = dict(sorted(self.rule_checks.items()))
        if self.path_effort:
            extra["path_effort"] = [rule.to_wire() for rule in self.path_effort]
        return {
            **extra,
            "effort": self.effort,
            "custom_from": self.custom_from,
            "custom": [row.to_document() for row in self.custom],
            "run_on": self.run_on,
            "budget": dict(sorted(self.budget.items())),
            "capabilities": dict(sorted(self.capabilities.items())),
            "ai_depth": self.ai_depth,
            "profiles": {name: profile.to_document() for name, profile in sorted(self.profiles.items())},
            "profile_rules": [rule.to_document() for rule in self.profile_rules],
            "floors": [floor.to_wire() for floor in self.floors],
        }

    def to_wire(self) -> dict[str, Any]:
        def setting(key: str, value: Any) -> dict[str, Any]:
            origin = self.origin(key)
            return {
                "effective_value": value,
                "mode": "inherit" if origin.layer == "built_in_default" else "override",
                "origin": origin.to_wire(),
            }

        return {
            "effort": setting("effort", self.effort),
            "custom_from": setting("custom_from", self.custom_from),
            "custom": setting("custom", [row.to_document() for row in self.custom]),
            "run_on": setting("run_on", self.run_on),
            "budget": {name: setting(f"budget.{name}", self.budget.get(name)) for name in BUDGET_FIELDS},
            "capabilities": {
                family: setting(f"capabilities.{family}", self.capabilities.get(family, "auto")) for family in FAMILIES
            },
            "ai_depth": setting("ai_depth", self.ai_depth),
            "profiles": {name: profile.to_document() for name, profile in sorted(self.profiles.items())},
            "profile_rules": [rule.to_wire() for rule in self.profile_rules],
            "floors": [floor.to_wire() for floor in self.floors],
            "proposed_budget_defaults": dict(PROPOSED_BUDGET_DEFAULTS),
            "escalation": EscalationPolicy.from_settings(self).to_wire(),
            "rule_checks": rule_check_settings_wire(self),
            "path_effort": [rule.to_wire() for rule in self.path_effort],
            **({INDEPENDENCE_POLICY_KEY: [rule.to_wire() for rule in self.independence]} if self.independence else {}),
            "errors": list(self.errors),
        }


def resolve_run_settings(
    layers: Sequence[RunSettingsLayer | None],
    *,
    default_run_on: ExecutorLocus = "local",
) -> EffectiveRunSettings:
    """Apply layers low-to-high precedence; each key is inherit or explicit override."""

    present = [layer for layer in layers if layer is not None]
    for layer in present:
        if layer.origin.layer not in _LAYER_ORDER:
            raise ValueError(f"unsupported run settings layer {layer.origin.layer!r}")
    effort = "auto"
    last_preset: Preset = "auto"
    custom_from: Preset | None = None
    custom: tuple[EntrySelection, ...] = ()
    run_on: ExecutorLocus = default_run_on
    budget: dict[str, float | int] = {}
    capabilities: dict[str, FamilyMode] = {family: "auto" for family in FAMILIES}
    ai_depth: Preset | None = None
    ai_max_parallel: int | None = None
    profiles: dict[str, Profile] = {}
    rules: list[ProfileRule] = []
    floors: list[FloorRule] = []
    origins: dict[str, SettingOrigin] = {}
    errors: list[str] = []
    escalation: dict[str, Any] = {}
    rule_checks: dict[str, Any] = {}
    path_effort: list[PathEffortRule] = []
    independence: list[IndependenceRule] = []
    for layer in present:
        errors.extend(f"{layer.origin.source or layer.origin.layer}: {error}" for error in layer.errors)
        if layer.effort is not None:
            if layer.effort != "custom":
                last_preset = cast(Preset, layer.effort)
            effort = layer.effort
            origins["effort"] = layer.origin
        if layer.custom_from is not None:
            custom_from = layer.custom_from
            origins["custom_from"] = layer.origin
        if layer.custom is not None:
            custom = layer.custom
            origins["custom"] = layer.origin
        if layer.run_on is not None:
            run_on = layer.run_on
            origins["run_on"] = layer.origin
        for key, value in layer.budget.items():
            budget[key] = value
            origins[f"budget.{key}"] = layer.origin
        for family, mode in layer.capabilities.items():
            capabilities[family] = mode
            origins[f"capabilities.{family}"] = layer.origin
        if layer.ai_depth is not None:
            ai_depth = layer.ai_depth
            origins["ai_depth"] = layer.origin
        if layer.ai_max_parallel is not None:
            ai_max_parallel = layer.ai_max_parallel
        if layer.profiles:
            profiles.update(layer.profiles)
        rules.extend(layer.profile_rules)
        floors.extend(layer.floors)
        for section, values, target in (
            ("escalation", layer.escalation, escalation),
            ("rule_checks", layer.rule_checks, rule_checks),
        ):
            for key, value in values.items():
                target[key] = value
                origins[f"{section}.{key}"] = layer.origin
        # Later (higher-precedence) layers' path rules win per path (last match).
        path_effort.extend(layer.path_effort)
        independence.extend(layer.independence)
    if effort == "custom" and not custom and "custom" not in origins:
        errors.append("effort custom has no custom selection; using the preset it started from")
        effort = custom_from or last_preset
    resolved_from = custom_from or last_preset
    if "custom_from" not in origins and effort == "custom":
        origins["custom_from"] = origins.get("effort", BUILT_IN_ORIGIN)
    return EffectiveRunSettings(
        effort=effort,
        custom_from=resolved_from,
        custom=custom,
        run_on=run_on,
        budget=budget,
        capabilities=capabilities,
        ai_depth=ai_depth,
        ai_max_parallel=ai_max_parallel,
        profiles=profiles,
        profile_rules=tuple(rules),
        floors=tuple(floors),
        origins=origins,
        errors=tuple(dict.fromkeys(errors)),
        escalation=escalation,
        rule_checks=rule_checks,
        path_effort=tuple(path_effort),
        independence=tuple(independence),
    )


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanEntry:
    id: str
    family: CapabilityFamily
    title: str
    status: EntryStatus
    reason: str
    origin: EntryOrigin
    when: When
    sides: Sides
    paths: tuple[str, ...]
    executor: ExecutorLocus
    depth: Preset
    required: bool
    escalate: bool
    cost_class: str
    degrade_tier: str
    estimated_minutes: float
    estimated_model_calls: int
    pulled_in_by: tuple[str, ...] = ()
    setup_hint: str = ""
    available: bool = True
    external: bool = False
    budget_reduced_from: str = ""
    # Display only: which floor made the entry required (capability, min_effort, check).
    floor_kind: str = ""
    # The ingested result a ``satisfied`` entry relies on (``IngestedSatisfaction.to_wire``).
    satisfied_by: Mapping[str, Any] | None = None
    # R6 item 19: per-recipe test selection (``test_impact.EntryImpact.selection_document``).
    selection: Mapping[str, Any] | None = None
    # R6 item 20: head first; the base runs only to classify a head failure.
    base_on_demand: bool = False
    # P11-ESC: the rule questions ``ai.rule_checks`` asks (execution identity).
    rule_refs: tuple[str, ...] = ()
    # Display only: escalation (state, from/to depth, triggers) and the rule-question breakdown.
    escalation: Mapping[str, Any] | None = None
    rule_checks: Mapping[str, Any] | None = None
    # Display only (Trust Gate T-J): why an equivalent ingested result did or did not
    # stand in for this entry, as a ``validity`` result wire (``equivalent_result``,
    # ``equivalence_disabled``, ``fresh_run_requested``). Outside the execution identity.
    equivalence: Mapping[str, Any] | None = None

    def execution_document(self) -> dict[str, Any]:
        # Present only when set, so every plan without them keeps its digest.
        economy: dict[str, Any] = {}
        if self.selection:
            economy["selection"] = selection_execution_document(self.selection)
        if self.base_on_demand:
            economy["base_on_demand"] = True
        if self.rule_refs:
            economy["rule_refs"] = list(self.rule_refs)
        return {
            **economy,
            "id": self.id,
            "status": self.status,
            "when": self.when,
            "sides": self.sides,
            "paths": list(self.paths),
            "executor": self.executor,
            "depth": self.depth,
            "required": self.required,
            "estimated_minutes": round(self.estimated_minutes, 3),
            "estimated_model_calls": self.estimated_model_calls,
        }

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.execution_document(),
            "family": self.family,
            "title": self.title,
            "reason": self.reason,
            "origin": self.origin,
            "escalate": self.escalate,
            "cost_class": self.cost_class,
            "degrade_tier": self.degrade_tier,
            "pulled_in_by": list(self.pulled_in_by),
            "setup_hint": self.setup_hint,
            "available": self.available,
            "external": self.external,
            "budget_reduced_from": self.budget_reduced_from,
            "floor_kind": self.floor_kind,
            "satisfied_by": dict(self.satisfied_by) if self.satisfied_by else None,
            "selection": dict(self.selection) if self.selection else None,
            "base_on_demand": self.base_on_demand,
            "escalation": dict(self.escalation) if self.escalation else None,
            "rule_checks": dict(self.rule_checks) if self.rule_checks else None,
            "equivalence": dict(self.equivalence) if self.equivalence else None,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PlanEntry:
        return cls(
            id=str(raw["id"]),
            family=cast(CapabilityFamily, str(raw["family"])),
            title=str(raw.get("title") or ""),
            status=cast(EntryStatus, str(raw["status"])),
            reason=str(raw.get("reason") or ""),
            origin=cast(EntryOrigin, str(raw.get("origin") or "preset")),
            when=cast(When, str(raw["when"])),
            sides=cast(Sides, str(raw["sides"])),
            paths=tuple(str(item) for item in raw.get("paths") or ()),
            executor=cast(ExecutorLocus, str(raw["executor"])),
            depth=cast(Preset, str(raw["depth"])),
            required=raw.get("required") is True,
            escalate=raw.get("escalate") is True,
            cost_class=str(raw.get("cost_class") or ""),
            degrade_tier=str(raw.get("degrade_tier") or "base"),
            estimated_minutes=float(raw.get("estimated_minutes") or 0.0),
            estimated_model_calls=int(raw.get("estimated_model_calls") or 0),
            pulled_in_by=tuple(str(item) for item in raw.get("pulled_in_by") or ()),
            setup_hint=str(raw.get("setup_hint") or ""),
            available=raw.get("available") is not False,
            external=raw.get("external") is True,
            budget_reduced_from=str(raw.get("budget_reduced_from") or ""),
            floor_kind=str(raw.get("floor_kind") or ""),
            satisfied_by=(dict(raw["satisfied_by"]) if isinstance(raw.get("satisfied_by"), Mapping) else None),
            selection=(dict(raw["selection"]) if isinstance(raw.get("selection"), Mapping) else None),
            base_on_demand=raw.get("base_on_demand") is True,
            rule_refs=tuple(str(item) for item in raw.get("rule_refs") or ()),
            escalation=(dict(raw["escalation"]) if isinstance(raw.get("escalation"), Mapping) else None),
            rule_checks=(dict(raw["rule_checks"]) if isinstance(raw.get("rule_checks"), Mapping) else None),
            equivalence=(dict(raw["equivalence"]) if isinstance(raw.get("equivalence"), Mapping) else None),
        )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Spend already observed; ``None`` monthly values mean not metered here."""

    review_minutes: float = 0.0
    review_model_calls: int = 0
    monthly_minutes: float | None = None
    monthly_model_calls: int | None = None
    monthly_managed_usd: float | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_minutes": round(self.review_minutes, 3),
            "review_model_calls": self.review_model_calls,
            "monthly_minutes": None if self.monthly_minutes is None else round(self.monthly_minutes, 3),
            "monthly_model_calls": self.monthly_model_calls,
            "monthly_managed_usd": self.monthly_managed_usd,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> BudgetUsage:
        def optional_float(key: str) -> float | None:
            value = raw.get(key)
            return None if value is None else float(value)

        monthly_calls = raw.get("monthly_model_calls")
        return cls(
            review_minutes=float(raw.get("review_minutes") or 0.0),
            review_model_calls=int(raw.get("review_model_calls") or 0),
            monthly_minutes=optional_float("monthly_minutes"),
            monthly_model_calls=None if monthly_calls is None else int(monthly_calls),
            monthly_managed_usd=optional_float("monthly_managed_usd"),
        )


@dataclass(frozen=True, slots=True)
class ManualPick:
    """A per-Review request (``Run specific…`` / ``Run deeper``); it can only add.

    ``target`` names a runtime surface discovered on the Snapshot at request
    time that the trusted base does not configure (``surface:<provider>:<id>``).
    Only the server sets it, from its own discovery; it never comes from a
    request body.

    ``fresh`` is an explicit fresh run: an equivalent ingested result (CI or an
    earlier LemonCrow verification) does not satisfy the entry on this Snapshot.
    """

    id: str
    depth: Preset | None = None
    target: DynamicTarget | None = None
    fresh: bool = False

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"id": self.id, "depth": self.depth}
        if self.target is not None:
            wire["target"] = self.target.to_wire()
        if self.fresh:
            wire["fresh"] = True
        return wire


@dataclass(frozen=True, slots=True)
class DroppedPick:
    """A manual pick a rebuilt plan could not keep, and exactly why (display only)."""

    id: str
    reason: str

    def to_wire(self) -> dict[str, str]:
        return {"id": self.id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """Everything a plan is computed from.

    ``ai_candidates``/``risk_signal`` are escalation signals observed when the
    plan is first built. They are recorded on the plan (``signals``) and are not
    part of the input identity: the plan for a Snapshot stays frozen while the
    reviewer's marks change the outstanding candidate count.
    """

    review_id: str
    revision_id: str
    trigger: Trigger
    settings: EffectiveRunSettings
    detected: DetectedCapabilities
    changed_paths: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    ai_candidates: int = 0
    risk_signal: bool = False
    allowed_run_on: tuple[ExecutorLocus, ...] = ("local",)
    extra_floors: tuple[FloorRule, ...] = ()
    # P11-FED: catalog entries an equivalent current-Snapshot result satisfies,
    # and what the service observed about the previous Snapshot (fast lane).
    ingested: tuple[IngestedSatisfaction, ...] = ()
    #: Trust Gate T-J kill switch (``check_ingest.ci_equivalence_enabled`` off):
    #: no ingested result stands in; each entry keeps its status and says why.
    equivalence_disabled: bool = False
    fast_lane: FastLaneSignals | None = None
    #: Non-empty when the trusted-base Review config is invalid: nothing runs.
    policy_error: str = ""
    #: Loci the deployment allows per execution class (``ai``, ``runtime``); a
    #: class not named here uses ``allowed_run_on``.
    allowed_run_on_by_class: Mapping[str, tuple[ExecutorLocus, ...]] = field(default_factory=dict)
    #: Paths policy floors are matched against when they differ from
    #: ``changed_paths``: a correction Snapshot narrows ``changed_paths`` to what
    #: the correction touched, never what is required (``None``: ``changed_paths``).
    floor_paths: tuple[str, ...] | None = None

    @property
    def required_scope(self) -> tuple[str, ...]:
        return self.changed_paths if self.floor_paths is None else self.floor_paths

    #: Measured average compute minutes per run, by entry, on this Review's earlier
    #: Snapshots. An estimate input, never identity: a frozen plan keeps its estimate.
    measured_minutes: Mapping[str, float] = field(default_factory=dict)
    #: Loci the deployment places a class on but has no executor wired for, per
    #: execution class, with the reason (``{"runtime": {"worker": "no customer
    #: worker connected"}}``). Stated by the deployment from what it actually
    #: wired, never the catalog: an entry placed there plans ``not_available``.
    unavailable_run_on_by_class: Mapping[str, Mapping[ExecutorLocus, str]] = field(default_factory=dict)
    #: R6 item 19: per check entry, the test selection for this Snapshot's changes
    #: (``test_impact.select_recipe_tests``). Empty: every recipe runs in full.
    impact: Mapping[str, EntryImpact] = field(default_factory=dict)
    #: P11-ESC: deterministic escalation signals, observed when the plan is first
    #: built (``None``: not observed, nothing escalates). Like ``ai_candidates``
    #: they are frozen on the plan (``signals``) and are not input identity.
    escalation: EscalationFacts | None = None
    #: P11-ESC: one question per authoritative Review rule applying to the Change.
    rule_questions: tuple[RuleQuestion, ...] = ()

    def unavailable_reason(self, entry_id: str, locus: ExecutorLocus) -> str:
        """Why *entry_id* cannot execute on *locus* in this deployment, or ``""``."""

        return str((self.unavailable_run_on_by_class.get(execution_class(entry_id)) or {}).get(locus) or "")

    def allowed_for(self, entry_id: str) -> tuple[ExecutorLocus, ...]:
        cls = execution_class(entry_id)
        if cls == "static":
            return tuple(self.allowed_run_on_by_class.get("static") or ()) or (REVIEW_HOST,)
        return tuple(self.allowed_run_on_by_class.get(cls) or ()) or self.allowed_run_on

    def digest(self) -> str:
        invalid = {"policy_error": self.policy_error} if self.policy_error else {}
        by_class = (
            {
                "allowed_run_on_by_class": {
                    name: list(loci) for name, loci in sorted(self.allowed_run_on_by_class.items())
                }
            }
            if self.allowed_run_on_by_class
            else {}
        )
        # Present only when it differs, so every other plan keeps its digest.
        floor_scope = {"floor_paths": sorted(self.floor_paths)} if self.floor_paths is not None else {}
        # Only when some placed locus has no executor: a fully wired deployment
        # keeps its digest, and wiring one later supersedes the frozen plan.
        unwired = (
            {
                "unavailable_run_on_by_class": {
                    name: dict(sorted(reasons.items()))
                    for name, reasons in sorted(self.unavailable_run_on_by_class.items())
                    if reasons
                }
            }
            if any(self.unavailable_run_on_by_class.values())
            else {}
        )
        # P11-FED: only when present, so plans without ingestion keep their identity.
        federation: dict[str, Any] = {}
        if self.ingested:
            federation["ingested"] = [row.digest_document() for row in self.ingested]
            if self.equivalence_disabled:
                # Only when it changes an entry, so turning it back on restores the identity.
                federation["equivalence"] = "disabled"
        if self.fast_lane is not None:
            federation["fast_lane"] = self.fast_lane.digest_document()
        # R6: only when present, so plans without test selection keep their identity.
        selection: dict[str, Any] = (
            {"impact": {entry_id: row.digest_document() for entry_id, row in sorted(self.impact.items())}}
            if self.impact
            else {}
        )
        # P11-ESC: only when present, so plans without rule questions keep their identity.
        if self.rule_questions:
            selection["rule_questions"] = [row.digest_document() for row in self.rule_questions]
        return _digest(
            {
                **invalid,
                **selection,
                **by_class,
                **floor_scope,
                **unwired,
                **federation,
                "version": RUN_PLAN_VERSION,
                "review_id": self.review_id,
                "revision_id": self.revision_id,
                "trigger": self.trigger,
                "settings": self.settings.digest_document(),
                "detected": self.detected.digest_document(),
                "changed_paths": sorted(self.changed_paths),
                "labels": sorted(self.labels),
                "allowed_run_on": list(self.allowed_run_on),
                "extra_floors": [floor.to_wire() for floor in self.extra_floors],
            }
        )


@dataclass(frozen=True, slots=True)
class RunPlan:
    review_id: str
    revision_id: str
    trigger: Trigger
    selection: Mapping[str, str]
    run_on: ExecutorLocus
    run_on_note: str
    ceilings: Mapping[str, float | int]
    usage: BudgetUsage
    entries: tuple[PlanEntry, ...]
    family_modes: Mapping[str, FamilyMode]
    errors: tuple[str, ...]
    budget_notes: tuple[str, ...]
    inputs_digest: str
    manual: tuple[ManualPick, ...] = ()
    parent_digest: str = ""
    signals: Mapping[str, Any] = field(default_factory=dict)
    # Display only (settings preview); the inputs digest already covers detection.
    detected: Mapping[str, Any] = field(default_factory=dict)
    # Display only: selection notes such as inactive label rules.
    notes: tuple[str, ...] = ()
    # Display only: the push fast-lane decision (``FastLaneDecision.to_wire``); empty on open.
    fast_lane: Mapping[str, Any] = field(default_factory=dict)
    # The trusted-base config error that stopped every entry (``policy invalid``).
    policy_error: str = ""
    # Display only: the escalation decision (``EscalationDecision.to_wire``); empty when not observed.
    escalation: Mapping[str, Any] = field(default_factory=dict)
    # Display only: manual picks a rebuild of this Snapshot's plan could not carry.
    manual_dropped: tuple[DroppedPick, ...] = ()

    def entry(self, entry_id: str) -> PlanEntry | None:
        return next((row for row in self.entries if row.id == entry_id), None)

    def required_blockers(self) -> tuple[PlanEntry, ...]:
        """Required work this plan does not run, so readiness can never pass on it.

        Any required entry Limited by budget, and any required capability or
        check floor with no catalog executor. Minimum-depth floors over entries
        this build cannot run stay informative.
        """

        return tuple(
            row
            for row in self.entries
            if row.required
            and (
                row.status == "limited_by_budget"
                or (row.status == "not_available" and row.floor_kind in {"capability", "check"})
            )
        )

    def runs(self, entry_id: str) -> bool:
        row = self.entry(entry_id)
        return row is not None and row.status == "run"

    @property
    def totals(self) -> dict[str, Any]:
        running = [row for row in self.entries if row.status == "run"]
        return {
            "minutes": round(sum(row.estimated_minutes for row in running), 3),
            "model_calls": sum(row.estimated_model_calls for row in running),
        }

    @property
    def limited_by_budget(self) -> bool:
        return any(
            row.status == "limited_by_budget"
            or row.budget_reduced_from
            or bool(row.rule_checks and row.rule_checks.get("limited"))
            for row in self.entries
        )

    def execution_document(self) -> dict[str, Any]:
        """What executors consume. Origins/reasons/titles are display only."""

        return {
            "version": RUN_PLAN_VERSION,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "trigger": self.trigger,
            "run_on": self.run_on,
            "ceilings": dict(sorted(self.ceilings.items())),
            "entries": [row.execution_document() for row in sorted(self.entries, key=lambda item: item.id)],
        }

    @property
    def digest(self) -> str:
        return _digest(self.execution_document())

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": RUN_PLAN_VERSION,
            "digest": self.digest,
            "parent_digest": self.parent_digest,
            "inputs_digest": self.inputs_digest,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "trigger": self.trigger,
            "selection": dict(self.selection),
            "run_on": self.run_on,
            "run_on_note": self.run_on_note,
            "budget": {
                "ceilings": dict(sorted(self.ceilings.items())),
                "usage": self.usage.to_wire(),
                "planned": self.totals,
                "limited": self.limited_by_budget,
                "notes": list(self.budget_notes),
            },
            "family_modes": dict(self.family_modes),
            "entries": [row.to_wire() for row in self.entries],
            "manual": [pick.to_wire() for pick in self.manual],
            "manual_dropped": [pick.to_wire() for pick in self.manual_dropped],
            "signals": dict(self.signals),
            "detected": dict(self.detected),
            "notes": list(self.notes),
            "fast_lane": dict(self.fast_lane) if self.fast_lane else None,
            "required_blockers": [row.id for row in self.required_blockers()],
            "policy_state": "invalid" if self.policy_error else "valid",
            "policy_error": self.policy_error,
            "escalation": dict(self.escalation) if self.escalation else None,
            "errors": list(self.errors),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any], *, verify: bool = True) -> RunPlan:
        """Rebuild a persisted plan; a digest mismatch fails closed."""

        budget = raw.get("budget") if isinstance(raw.get("budget"), Mapping) else {}
        assert isinstance(budget, Mapping)
        ceilings_raw = budget.get("ceilings") if isinstance(budget.get("ceilings"), Mapping) else {}
        assert isinstance(ceilings_raw, Mapping)
        usage_raw = budget.get("usage") if isinstance(budget.get("usage"), Mapping) else {}
        assert isinstance(usage_raw, Mapping)
        signals_raw = raw.get("signals")
        detected_raw = raw.get("detected")
        fast_lane_raw = raw.get("fast_lane")
        escalation_raw = raw.get("escalation")
        plan = cls(
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            trigger=cast(Trigger, str(raw.get("trigger") or "open")),
            selection={str(k): str(v) for k, v in dict(raw.get("selection") or {}).items()},
            run_on=cast(ExecutorLocus, str(raw.get("run_on") or "local")),
            run_on_note=str(raw.get("run_on_note") or ""),
            ceilings={
                str(key): (int(value) if key in _INTEGER_BUDGET_FIELDS else float(value))
                for key, value in ceilings_raw.items()
            },
            usage=BudgetUsage.from_wire(usage_raw),
            entries=tuple(PlanEntry.from_wire(row) for row in raw.get("entries") or () if isinstance(row, Mapping)),
            family_modes={str(k): cast(FamilyMode, str(v)) for k, v in dict(raw.get("family_modes") or {}).items()},
            errors=tuple(str(item) for item in raw.get("errors") or ()),
            budget_notes=tuple(str(item) for item in budget.get("notes") or ()),
            inputs_digest=str(raw.get("inputs_digest") or ""),
            manual=tuple(
                ManualPick(
                    id=str(row.get("id") or ""),
                    depth=cast("Preset | None", row.get("depth") if row.get("depth") in PRESETS else None),
                    target=_discovered_target(row.get("target")),
                    fresh=row.get("fresh") is True,
                )
                for row in raw.get("manual") or ()
                if isinstance(row, Mapping)
            ),
            parent_digest=str(raw.get("parent_digest") or ""),
            signals=dict(signals_raw) if isinstance(signals_raw, Mapping) else {},
            detected=dict(detected_raw) if isinstance(detected_raw, Mapping) else {},
            notes=tuple(str(item) for item in raw.get("notes") or ()),
            fast_lane=dict(fast_lane_raw) if isinstance(fast_lane_raw, Mapping) else {},
            policy_error=str(raw.get("policy_error") or ""),
            escalation=dict(escalation_raw) if isinstance(escalation_raw, Mapping) else {},
            manual_dropped=tuple(
                DroppedPick(id=str(row.get("id") or ""), reason=str(row.get("reason") or ""))
                for row in raw.get("manual_dropped") or ()
                if isinstance(row, Mapping)
            ),
        )
        if verify and str(raw.get("digest") or "") != plan.digest:
            raise ValueError("run plan digest does not match its execution content")
        return plan


@dataclass(slots=True)
class _Draft:
    catalog: CatalogEntry
    origin: EntryOrigin
    when: When
    sides: Sides
    paths: tuple[str, ...]
    executor: ExecutorLocus
    depth: Preset
    escalate: bool
    required: bool = False
    floor_kind: str = ""
    floor_reason: str = ""
    manual: bool = False
    # An explicit fresh-run pick: an ingested equivalent result does not satisfy it.
    fresh: bool = False
    # A surface discovered on the Snapshot and picked at request time; the
    # trusted base does not list it, so applicability comes from discovery.
    discovered: bool = False
    pulled_in_by: list[str] = field(default_factory=list)
    status: EntryStatus = "run"
    reason: str = ""
    budget_reduced_from: str = ""
    # Measured average compute per run of this entry on this Review (0: none yet).
    measured_minutes: float = 0.0
    satisfied_by: dict[str, Any] | None = None
    # R6: the entry's own sides were head until these dependents raised them.
    sides_raised_by: list[str] = field(default_factory=list)
    selection: dict[str, Any] | None = None
    base_on_demand: bool = False
    # P11-ESC (see PlanEntry).
    rule_refs: tuple[str, ...] = ()
    escalation: dict[str, Any] | None = None
    rule_checks: dict[str, Any] | None = None
    # Trust Gate T-J: why an equivalent result did or did not stand in (a validity wire).
    equivalence: dict[str, Any] | None = None


def _discovered_target(raw: Any) -> DynamicTarget | None:
    if not isinstance(raw, Mapping) or not str(raw.get("id") or ""):
        return None
    return DynamicTarget(
        id=str(raw["id"]),
        title=str(raw.get("title") or ""),
        reason=str(raw.get("reason") or ""),
        executable=raw.get("executable") is not False,
    )


def _discovered_surface_entry(pick: ManualPick) -> CatalogEntry | None:
    """Catalog entry for a request-time discovered surface pick, if well formed."""

    target = pick.target
    if target is None or not pick.id.startswith("surface:") or pick.id != f"surface:{target.id}":
        return None
    return CatalogEntry(
        id=pick.id,
        family="deep_runtime",
        title=target.title or f"Runtime surface {target.id}",
        cost_class="medium",
        degrade_tier="base",
        executor="runtime_validation" if target.executable else "",
    )


def _family_reason(family: str, settings: EffectiveRunSettings) -> str:
    origin = settings.origin(f"capabilities.{family}")
    return f"{family} family turned off ({origin.source or origin.layer})"


def _estimate(draft: _Draft) -> tuple[float, int]:
    sides = 2 if draft.sides == "base+head" else 1
    # The cost class per side, raised to the entry's documented minimum and to
    # what this Review measured for it.
    minutes = max(draft.catalog.minutes_per_side() * sides, draft.catalog.minimum_minutes, draft.measured_minutes)
    return minutes, draft.catalog.model_calls(draft.depth)


def check_catalog_id(stable_key: str, detected: DetectedCapabilities) -> str | None:
    """The catalog entry a LemonCrow-owned required check's stable key names, if any.

    Accepts a catalog ID (``checks.test``, ``browser:<id>``, ``surface:<id>``), a
    configured browser scenario ID (the browser attestation's stable key), or a
    runtime-validation scenario ID ``surface:<provider>:<discovered id>`` whose
    discovered ID is the configured surface ID or nested under it.
    """

    key = stable_key.strip()
    if not key:
        return None
    if not key.endswith(":*") and valid_entry_id(key) and resolve_entry(key, detected) is not None:
        return key
    if any(target.id == key for target in detected.browser_scenarios):
        return f"browser:{key}"
    parts = key.split(":", 2)
    if len(parts) == 3 and parts[0] == "surface":
        discovered = parts[2]
        for target in sorted(detected.surfaces, key=lambda item: (-len(item.id), item.id)):
            if discovered == target.id or discovered.startswith(f"{target.id}:"):
                return f"surface:{target.id}"
    return None


def _matching_profile(inputs: PlanInputs) -> tuple[ProfileRule, Profile] | None:
    for rule in inputs.settings.profile_rules:
        profile = inputs.settings.profiles.get(rule.profile)
        if profile is not None and rule.matches(inputs.changed_paths, inputs.labels):
            return rule, profile
    return None


ExecutionClass = Literal["ai", "runtime", "static", "default"]
#: Catalog IDs the runtime-validation executor runs; a hosted deployment may
#: place them on another locus than AI review.
_RUNTIME_PREFIXES: Final[tuple[str, ...]] = (
    "checks.",
    "behavior.",
    "visual.",
    "migrations.",
    "artifacts.",
    "infra.",
    "compat.",
    "browser:",
    "surface:",
)
_EXECUTION_CLASS_LABEL: Final[Mapping[str, str]] = {"ai": "AI", "runtime": "runtime", "static": "static analysis"}


def _locus_action(entry: CatalogEntry, allowed: Sequence[ExecutorLocus]) -> str:
    """What a reviewer can do about an entry placed on a locus that does not run it, in *this* deployment.

    Only a locus the deployment allows is worth naming as a setting (a hosted server allows the worker and
    managed executors, never the local one); otherwise the Review must be opened where the entry runs.
    """

    reachable = [locus for locus in entry.loci if locus in allowed]
    if reachable:
        named = " or ".join(f"the {locus} executor" for locus in reachable)
        return (
            f"Set this deployment's runtime executor to {named}, or open the Review locally (`lc review`), to run it."
        )
    only = "/".join(entry.loci)
    return (
        f"This deployment has no {only} executor, so it cannot run here: it is {only}-only. "
        "Open the Review locally (`lc review`) to run it."
    )


def execution_class(entry_id: str) -> ExecutionClass:
    """Which deployment executor locus an entry follows: AI review, runtime validation, static analysis, or the default.

    Static analysis only reads content the Review store already holds, so it
    runs where the Snapshot is held (``review_host``) unless the deployment
    names another locus for the ``static`` class; it never follows ``run_on``.
    """

    if entry_id.startswith("ai."):
        return "ai"
    if entry_id == "side_effects.runtime" or entry_id.startswith(_RUNTIME_PREFIXES):
        return "runtime"
    if entry_id == "side_effects.static":
        return "static"
    return "default"


def _resolve_run_on(inputs: PlanInputs) -> tuple[ExecutorLocus, str]:
    requested = inputs.settings.run_on
    if requested in inputs.allowed_run_on:
        return requested, ""
    fallback = inputs.allowed_run_on[0] if inputs.allowed_run_on else "local"
    return fallback, f"{requested} execution is not enabled here; {fallback} is used"


def build_run_plan(
    inputs: PlanInputs,
    *,
    usage: BudgetUsage | None = None,
    manual: Sequence[ManualPick] = (),
    pinned_run: Iterable[str] = (),
    parent_digest: str = "",
) -> RunPlan:
    """Resolve selection + floors + applicability + budget into the effective plan."""

    settings = inputs.settings
    detected = inputs.detected
    usage = usage or BudgetUsage()
    pinned = frozenset(pinned_run)
    errors: list[str] = list(settings.errors)
    run_on, run_on_note = _resolve_run_on(inputs)
    run_on_explicit = settings.origin("run_on").layer != "built_in_default"
    locus_notes: list[str] = [run_on_note] if run_on_note else []

    def locus_for(entry_id: str, explicit: ExecutorLocus | None) -> ExecutorLocus:
        """The entry's executor: its own locus class, the settings, then the deployment default."""

        cls = execution_class(entry_id)
        if cls == "static":
            # Never follows run_on: static analysis needs no execution locus.
            static_allowed = inputs.allowed_for(entry_id)
            if explicit is not None and explicit in static_allowed:
                return explicit
            if explicit is not None:
                locus_notes.append(
                    f"{explicit} execution is not used for static analysis; it runs on {static_allowed[0]}"
                )
            return static_allowed[0]
        if explicit is None and cls not in inputs.allowed_run_on_by_class:
            return run_on
        allowed = inputs.allowed_for(entry_id)
        requested = explicit or settings.run_on
        if requested in allowed:
            return requested
        fallback: ExecutorLocus = allowed[0] if allowed else "local"
        if explicit is not None or run_on_explicit:
            scope = f" for {_EXECUTION_CLASS_LABEL[cls]} work" if cls in _EXECUTION_CLASS_LABEL else ""
            locus_notes.append(f"{requested} execution is not enabled here{scope}; {fallback} is used")
        return fallback

    plan_notes = [
        f"profile {rule.profile}: {LABEL_RULE_INACTIVE}" + ("; its path rule still applies" if rule.paths else "")
        for rule in settings.profile_rules
        if rule.label_rule_inactive
    ]

    profile_match = _matching_profile(inputs)
    rows: Sequence[EntrySelection | PresetEntry]
    if profile_match is not None:
        _rule, profile = profile_match
        base: Preset = profile.base
        rows = profile.entries
        origin: EntryOrigin = "profile"
        selection = {"source": "profile", "name": profile.name, "base": base}
    elif settings.effort == "custom":
        base = settings.custom_from
        rows = settings.custom
        origin = "custom"
        selection = {"source": "custom", "name": "custom", "base": base}
    else:
        # P11-ESC item 10: a path default (``path_effort``) may set the preset for this Change.
        path_default = path_default_depth(
            settings.path_effort, settings.effort if settings.effort in PRESETS else "auto", inputs.required_scope
        )
        base = cast(Preset, path_default.depth)
        rows = PRESET_SELECTIONS[base]
        origin = "preset"
        selection = {"source": "preset", "name": base, "base": base, **path_default.selection_wire()}

    drafts: dict[str, _Draft] = {}

    def depth_for(entry: CatalogEntry, depth: Preset) -> Preset:
        if entry.family == "ai" and settings.ai_depth is not None and origin != "profile":
            return settings.ai_depth
        return depth

    def draft_for(
        entry_id: str,
        *,
        draft_origin: EntryOrigin,
        depth: Preset,
        when: When | None = None,
        sides: Sides | None = None,
        paths: tuple[str, ...] | None = None,
        executor: ExecutorLocus | None = None,
        escalate: bool | None = None,
        source: str,
    ) -> _Draft | None:
        entry = resolve_entry(entry_id, detected)
        if entry is None:
            errors.append(f"{source}: unknown capability id {entry_id!r} (not in the catalog or trusted base)")
            return None
        # Unset fields inherit from the base preset; an entry the base preset does
        # not select takes its structural shape (sides/when) from the fullest
        # preset, without escalation.
        own = preset_entry(depth, entry_id)
        defaults = own or preset_entry("deep", entry_id) or PresetEntry(entry_id)
        return _Draft(
            catalog=entry,
            origin=draft_origin,
            when=when or defaults.when,
            sides=sides or defaults.sides,
            paths=paths if paths is not None else (),
            executor=locus_for(entry_id, executor),
            depth=depth_for(entry, depth),
            escalate=(own.escalate if own is not None else False) if escalate is None else escalate,
            measured_minutes=float(inputs.measured_minutes.get(entry_id, 0.0)),
        )

    def omitted(entry_id: str) -> bool:
        """An entry a selection leaves out because the trusted base gives it nothing to act on."""

        entry = resolve_entry(entry_id, detected)
        return (
            entry is not None and entry.omit_when_inapplicable and applicability(entry, detected)[0] == "inapplicable"
        )

    for row in rows:
        expanded = expand_wildcard(row.id, detected)
        for entry_id in expanded:
            if entry_id in drafts:
                continue
            if omitted(entry_id):
                continue
            if isinstance(row, PresetEntry):
                draft = draft_for(
                    entry_id,
                    draft_origin=origin,
                    depth=base,
                    when=row.when,
                    sides=row.sides,
                    escalate=row.escalate,
                    source=f"preset {base}",
                )
            else:
                draft = draft_for(
                    entry_id,
                    draft_origin=origin,
                    depth=base,
                    when=row.when,
                    sides=row.sides,
                    paths=row.paths,
                    executor=row.executor,
                    source=f"{selection['source']} selection",
                )
            if draft is not None:
                drafts[entry_id] = draft

    # Family ``always`` forces the family's full catalog in, within budget.
    for family, mode in settings.capabilities.items():
        if mode != "always":
            continue
        for row in PRESET_SELECTIONS["deep"]:
            for entry_id in expand_wildcard(row.id, detected):
                entry = resolve_entry(entry_id, detected)
                if entry is None or entry.family != family or entry_id in drafts or omitted(entry_id):
                    continue
                draft = draft_for(
                    entry_id,
                    draft_origin="family_override",
                    depth=base,
                    when="push" if row.when == "manual" else row.when,
                    sides=row.sides,
                    escalate=False,
                    source=f"capabilities.{family}: always",
                )
                if draft is not None:
                    drafts[entry_id] = draft

    # Policy floors: never removable by Custom, profiles, family overrides,
    # per-Review picks or budget.
    for floor in (*settings.floors, *inputs.extra_floors):
        if not paths_match(floor.paths, inputs.required_scope):
            continue
        reason = f"required by {floor.origin.source or floor.origin.layer}"
        if floor.kind == "check":
            mapped = check_catalog_id(floor.value, detected)
            if mapped is None:
                # A required LemonCrow-owned check no catalog executor can run is
                # shown, never silently dropped: readiness cannot pass on it.
                check_id = f"check:{floor.value}"
                drafts.setdefault(
                    check_id,
                    _Draft(
                        catalog=CatalogEntry(
                            id=check_id,
                            family="checks",
                            title=floor.value,
                            cost_class="minimal",
                            degrade_tier="base",
                        ),
                        origin="policy_floor",
                        when="push",
                        sides="head",
                        paths=(),
                        executor=locus_for(check_id, None),
                        depth=base,
                        escalate=False,
                    ),
                )
                unmapped = drafts[check_id]
                unmapped.required, unmapped.floor_kind, unmapped.floor_reason = True, "check", reason
                continue
            targets: list[tuple[str, PresetEntry | None]] = [(mapped, None)]
        elif floor.kind == "capability":
            targets = [(entry_id, None) for entry_id in expand_wildcard(floor.value, detected)]
        else:
            floor_preset = cast(Preset, floor.value)
            targets = [
                (entry_id, row)
                for row in PRESET_SELECTIONS[floor_preset]
                if row.when != "manual"
                for entry_id in expand_wildcard(row.id, detected)
            ]
        for entry_id, preset_row in targets:
            floor_depth: Preset = (
                cast(Preset, floor.value)
                if floor.kind == "min_effort" and PRESET_RANK[floor.value] > PRESET_RANK[base]
                else base
            )
            existing = drafts.get(entry_id)
            if existing is None:
                existing = draft_for(
                    entry_id,
                    draft_origin="policy_floor",
                    depth=floor_depth,
                    when=preset_row.when if preset_row is not None else "push",
                    sides=preset_row.sides if preset_row is not None else None,
                    escalate=preset_row.escalate if preset_row is not None else False,
                    source=reason,
                )
                if existing is None:
                    continue
                drafts[entry_id] = existing
            existing.origin = "policy_floor"
            existing.required = True
            if floor.kind != "min_effort" or not existing.floor_kind:
                existing.floor_kind = floor.kind
            existing.floor_reason = reason
            if PRESET_RANK[floor_depth] > PRESET_RANK[existing.depth]:
                existing.depth = floor_depth
            if floor.kind != "min_effort":
                existing.escalate = False
                if existing.when == "manual":
                    existing.when = "push"

    manual_ids: list[str] = []
    # Discovery targets a pick relied on, persisted with it so a rebuilt plan keeps the entry.
    manual_targets: dict[str, DynamicTarget] = {}
    for pick in manual:
        if not valid_entry_id(pick.id) or pick.id.endswith(":*"):
            errors.append(f"manual pick: unknown capability id {pick.id!r}")
            continue
        depth = pick.depth or base
        existing = drafts.get(pick.id)
        discovered = _discovered_surface_entry(pick) if resolve_entry(pick.id, detected) is None else None
        if existing is None and discovered is not None:
            # lc-debt: a discovered pick is costed as one head-side run, so a base+head
            # comparison is under-estimated; upgrade: carry the requested sides on ManualPick.
            existing = _Draft(
                catalog=discovered,
                origin="manual",
                when="manual",
                sides="head",
                paths=(),
                executor=locus_for(pick.id, None),
                depth=depth_for(discovered, depth),
                escalate=False,
                discovered=True,
            )
            drafts[pick.id] = existing
        if discovered is not None and pick.target is not None:
            manual_targets[pick.id] = pick.target
        if existing is None:
            existing = draft_for(
                pick.id,
                draft_origin="manual",
                depth=depth,
                when="manual",
                escalate=False,
                source="manual pick",
            )
            if existing is None:
                continue
            drafts[pick.id] = existing
        elif existing.origin != "policy_floor":
            existing.origin = "manual"
        existing.manual = True
        existing.fresh = existing.fresh or pick.fresh
        existing.escalate = False
        if pick.target is not None and pick.target.id == pick.id and pick.id in STATIC_CATALOG:
            # The Snapshot's own discovery shows the entry applies (a web app the
            # trusted base does not show yet): applicability comes from discovery.
            existing.discovered = True
            manual_targets[pick.id] = pick.target
        if PRESET_RANK[depth] > PRESET_RANK[existing.depth]:
            existing.depth = depth
        manual_ids.append(pick.id)

    # Dependencies resolve automatically and visibly.
    def pull_dependencies() -> None:
        changed = True
        while changed:
            changed = False
            for draft in list(drafts.values()):
                for dep_id in draft.catalog.dependencies:
                    dep = drafts.get(dep_id)
                    if dep is None:
                        dep = draft_for(
                            dep_id,
                            draft_origin=draft.origin,
                            depth=draft.depth,
                            when=draft.when,
                            sides=draft.catalog.dependency_sides,
                            escalate=draft.escalate,
                            source=f"dependency of {draft.catalog.id}",
                        )
                        if dep is None:
                            continue
                        drafts[dep_id] = dep
                        changed = True
                    if draft.catalog.id not in dep.pulled_in_by:
                        dep.pulled_in_by.append(draft.catalog.id)
                        changed = True
                    if draft.catalog.dependency_sides == "base+head" and dep.sides != "base+head":
                        dep.sides = "base+head"
                        dep.sides_raised_by.append(draft.catalog.id)
                        changed = True
                    elif (
                        draft.catalog.dependency_sides == "base+head"
                        and dep.sides_raised_by
                        and draft.catalog.id not in dep.sides_raised_by
                    ):
                        dep.sides_raised_by.append(draft.catalog.id)
                        changed = True
                    if draft.required and not dep.required:
                        dep.required = True
                        dep.floor_kind = draft.floor_kind
                        dep.floor_reason = f"needed by required {draft.catalog.id}"
                        changed = True

    pull_dependencies()

    def initial_status(draft: _Draft) -> tuple[EntryStatus, str]:
        entry = draft.catalog
        state, why = (
            ("applicable", "discovered on this Snapshot") if draft.discovered else applicability(entry, detected)
        )
        if state == "inapplicable":
            return "inapplicable", why
        if state in {"unknown", "unavailable"}:
            # Coverage unknown is never a pass: a required entry blocks readiness on it
            # (``required_blockers``), an optional one is shown, never silently dropped.
            # Unavailable: configured, but no spec of it can run (``Command too large``).
            return "not_available", why
        if not entry.executor:
            if draft.required:
                return "not_available", f"{REQUIRED_NO_EXECUTOR} ({draft.floor_reason})"
            return "not_available", "no executor for this capability in this build yet"
        if entry.loci and draft.executor not in entry.loci:
            placed = (
                f"{entry.title} runs only where the Snapshot is held; this deployment places it on the "
                f"{draft.executor} executor, which does not run it"
                + (
                    f". {entry.locus_hint} {_locus_action(entry, inputs.allowed_for(entry.id))}"
                    if entry.locus_hint
                    else ""
                )
            )
            return "not_available", f"{REQUIRED_NO_EXECUTOR} ({placed})" if draft.required else placed
        # The displayed plan equals what will execute: a locus this deployment
        # wired no executor for (no worker, managed sandbox not enabled) runs nothing.
        unwired = inputs.unavailable_reason(entry.id, draft.executor)
        if unwired:
            return "not_available", f"{REQUIRED_NO_EXECUTOR} ({unwired})" if draft.required else unwired
        if draft.required and draft.floor_kind in {"capability", "check"}:
            return "run", draft.floor_reason
        mode = settings.capabilities.get(entry.family, "auto")
        if mode == "off" and not draft.required:
            return "skipped", _family_reason(entry.family, settings)
        forced = mode == "always"
        if draft.manual:
            return "run", "requested for this Review"
        if draft.when == "manual" and not forced:
            return "on_request", "runs only when requested"
        if draft.when == "open" and inputs.trigger == "push" and not forced:
            return "skipped", "runs when the Review opens; this Snapshot came from a push"
        if draft.escalate and not forced:
            signal = inputs.ai_candidates > 2 if entry.family == "ai" else inputs.risk_signal
            if not signal:
                return "skipped", "Auto escalates only on a risk signal; none was found"
            return "run", (
                f"escalated: {inputs.ai_candidates} risk candidates"
                if entry.family == "ai"
                else "escalated: risk signal"
            )
        if draft.paths and not paths_match(draft.paths, inputs.changed_paths):
            return "skipped", f"{PATH_SCOPE_SKIP} {', '.join(draft.paths)}"
        if draft.required:
            return "run", draft.floor_reason
        return "run", why if entry.external else ""

    for draft in drafts.values():
        draft.status, draft.reason = initial_status(draft)

    # A dependency of running work runs; a dependent of unavailable work cannot.
    changed = True
    while changed:
        changed = False
        for draft in drafts.values():
            if draft.status != "run":
                continue
            for dep_id in draft.catalog.dependencies:
                dep = drafts.get(dep_id)
                if dep is None:
                    continue
                if dep.status in {"on_request", "skipped"} and not (
                    dep.status == "skipped" and settings.capabilities.get(dep.catalog.family) == "off"
                ):
                    dep.status, dep.reason = "run", f"needed by {draft.catalog.id}"
                    changed = True
                elif dep.status != "run" and not draft.required:
                    draft.status = "skipped" if dep.status == "skipped" else cast(EntryStatus, dep.status)
                    draft.reason = f"depends on {dep_id} ({dep.status.replace('_', ' ')})"
                    changed = True

    # P11-ESC: rule questions within their own ceiling, then deep-review escalation
    # inside the Auto rung (one registration point each). Escalation needs observed
    # signals and a valid policy (fail closed).
    apply_rule_checks(drafts, inputs.rule_questions, settings)
    escalation = (
        apply_escalation(
            drafts,
            decide_escalation(
                inputs.escalation,
                EscalationPolicy.from_settings(settings),
                tuple(dict.fromkeys((*inputs.changed_paths, *inputs.required_scope))),
            ),
            base=base,
        )
        if inputs.escalation is not None and not inputs.policy_error
        else None
    )

    # P11-FED: push fast lane and ingestion over rerun (one registration point).
    # An invalid trusted-base config grants no equivalence mapping and no fast
    # lane (fail closed).
    fast_lane = apply_federation(
        drafts,
        trigger=inputs.trigger,
        selection_source=str(selection["source"]),
        base=base,
        ai_mode=settings.capabilities.get("ai", "auto"),
        ai_candidates=inputs.ai_candidates,
        risk_signal=inputs.risk_signal,
        changed_paths=tuple(dict.fromkeys((*inputs.changed_paths, *inputs.required_scope))),
        depth_floor=any(
            floor.kind == "min_effort" and paths_match(floor.paths, inputs.required_scope)
            for floor in (*settings.floors, *inputs.extra_floors)
        ),
        signals=None if inputs.policy_error else inputs.fast_lane,
        ingested=() if inputs.policy_error else inputs.ingested,
        equivalence_disabled=inputs.equivalence_disabled,
        revision_id=inputs.revision_id,
        escalated=escalation.labels if escalation is not None and escalation.state == "escalated" else (),
    )

    # R6 item 19: run only the tests a change can affect (one registration point).
    if inputs.impact and not inputs.policy_error:
        apply_impact(drafts, inputs.impact)

    def base_on_demand() -> None:
        """R6 item 20: head first. A check entry whose base side only dependents
        that do not run asked for runs the head; the base runs only when the head
        fails, to classify pre-existing vs introduced failures."""

        for draft in drafts.values():
            if (
                not draft.sides_raised_by
                or draft.sides != "base+head"
                or draft.status != "run"
                or draft.catalog.executor != "check_recipes"
                or any(drafts[dep].status == "run" for dep in draft.sides_raised_by if dep in drafts)
            ):
                continue
            draft.sides = "head"
            draft.base_on_demand = True
            note = "head first: the base runs only if the head fails, to tell pre-existing from introduced failures"
            draft.reason = f"{draft.reason}; {note}" if draft.reason else note

    base_on_demand()

    # An invalid trusted-base config is no policy at all: fail closed. Nothing
    # runs (floors and per-Review picks included) until the base config is fixed.
    if inputs.policy_error:
        for draft in drafts.values():
            if draft.status in {"run", "on_request", "limited_by_budget"}:
                draft.status, draft.reason = "skipped", f"Review policy invalid: {inputs.policy_error}"

    # Budget: degrade from the most expensive work down, with an explicit state.
    ceilings = dict(settings.budget)
    notes: list[str] = []
    for draft in sorted(drafts.values(), key=lambda item: item.catalog.id):
        if draft.required and draft.status == "run" and settings.capabilities.get(draft.catalog.family) == "off":
            notes.append(
                f"{draft.catalog.id} is policy-required and runs although the "
                f"{draft.catalog.family} family is off (floors ignore family off)"
            )

    def limit(draft: _Draft, reason: str, *, hard: bool = False) -> None:
        """Limit one entry; dependents follow (required ones too under a hard cap)."""

        draft.status = "limited_by_budget"
        draft.reason = reason
        for other in drafts.values():
            if (
                other.status == "run"
                and draft.catalog.id in other.catalog.dependencies
                and (hard or not other.required)
            ):
                limit(other, f"Limited by budget: depends on {draft.catalog.id}", hard=hard)

    # The monthly managed money cap is hard, even for policy floors: work on the
    # managed executor moves to an allowed non-managed executor, or is Limited by
    # budget (readiness then stays pending on required work). Spend is checked
    # when the plan is built (amendments reuse that usage), so a Snapshot's plan
    # is not re-capped as spend grows.
    # lc-debt: "would exceed" is checked as cap reached; no per-entry USD estimate
    # exists. Upgrade: price catalog cost classes per executor and check spent+estimate.
    if "monthly_managed_usd" in ceilings:
        managed = sorted(
            draft.catalog.id
            for draft in drafts.values()
            if draft.status == "run" and draft.executor == "managed" and not draft.catalog.external
        )
        spent = usage.monthly_managed_usd
        cap = float(ceilings["monthly_managed_usd"])
        cap_reason = ""
        if spent is None:
            cap_reason = "managed spend is not metered here, so the monthly managed spend cap cannot be verified"
        elif spent >= cap:
            cap_reason = "monthly managed spend cap reached"
        if not managed and run_on != "managed":
            notes.append("monthly_managed_usd applies only to LemonCrow-managed execution")
        elif managed and cap_reason:
            routed: set[str] = set()
            capped = False
            for entry_id in managed:
                draft = drafts[entry_id]
                if draft.status != "run":
                    continue
                # Each entry may move only to a locus its execution class allows.
                fallback = next((locus for locus in ("worker", "local") if locus in inputs.allowed_for(entry_id)), None)
                if fallback is not None:
                    draft.executor = fallback
                    draft.reason = f"routed to {fallback}: {cap_reason}" + (f"; {draft.reason}" if draft.reason else "")
                    routed.add(fallback)
                else:
                    limit(draft, f"Limited by budget: {cap_reason}", hard=True)
                    capped = True
            notes.extend(f"{cap_reason}: managed work routed to {locus}" for locus in sorted(routed))
            if capped:
                notes.append(f"{cap_reason}: managed work, policy-required included, is Limited by budget")

    remaining: dict[str, float] = {}
    if "per_review_minutes" in ceilings:
        remaining["minutes"] = float(ceilings["per_review_minutes"]) - usage.review_minutes
    if "per_review_model_calls" in ceilings:
        remaining["model_calls"] = float(ceilings["per_review_model_calls"]) - usage.review_model_calls
    for key, used, dimension in (
        ("monthly_minutes", usage.monthly_minutes, "minutes"),
        ("monthly_model_calls", usage.monthly_model_calls, "model_calls"),
    ):
        if key not in ceilings:
            continue
        if used is None:
            notes.append(f"{key} is configured but monthly usage is not metered here, so it is not enforced")
            continue
        left = float(ceilings[key]) - float(used)
        remaining[dimension] = min(remaining.get(dimension, left), left)

    def totals() -> dict[str, float]:
        out = {"minutes": 0.0, "model_calls": 0.0}
        for draft in drafts.values():
            if draft.status == "run":
                minutes, calls = _estimate(draft)
                out["minutes"] += minutes
                out["model_calls"] += calls
        return out

    def exceeded() -> list[str]:
        current = totals()
        return [dimension for dimension, left in remaining.items() if current[dimension] > left + 1e-9]

    over = exceeded()
    if over:
        degradable = sorted(
            (
                draft
                for draft in drafts.values()
                if draft.status == "run" and not draft.required and draft.catalog.id not in pinned
            ),
            key=lambda item: (
                DEGRADE_ORDER.index(cast(Any, item.catalog.degrade_tier)),
                -COST_RANK[item.catalog.cost_class],
                -_estimate(item)[0],
                -_estimate(item)[1],
                item.catalog.id,
            ),
        )
        for draft in degradable:
            over = exceeded()
            if not over:
                break
            if draft.status != "run":
                continue
            minutes, calls = _estimate(draft)
            contributes = ("minutes" in over and minutes > 0) or ("model_calls" in over and calls > 0)
            if not contributes:
                continue
            if draft.catalog.id == "ai.investigators":
                # Reduce investigator depth before dropping AI review entirely.
                original = draft.depth
                for lower in reversed(PRESETS[: PRESETS.index(draft.depth)]):
                    draft.depth = lower
                    if "model_calls" not in exceeded():
                        break
                if "model_calls" not in exceeded() and draft.depth != original:
                    draft.budget_reduced_from = original
                    draft.reason = f"Limited by budget: reduced from {original} to {draft.depth}"
                    continue
                draft.depth = original
            limit(draft, f"Limited by budget ({', '.join(over).replace('_', ' ')} ceiling)")
        still_over = exceeded()
        if still_over:
            floors_over = sorted(
                draft.catalog.id for draft in drafts.values() if draft.status == "run" and draft.required
            )
            if floors_over:
                notes.append(
                    f"policy floors ignore the {', '.join(still_over).replace('_', ' ')} ceilings: "
                    f"{', '.join(floors_over)} still run"
                )
            notes.append("policy-required or already-planned work alone exceeds the budget; it still runs")
    # A dependent the budget limited no longer needs its dependency's base side.
    base_on_demand()
    # An escalated entry the budget reduced or limited is shown so (never silently dropped).
    escalation = finalize_escalation(drafts, escalation)

    entries: list[PlanEntry] = []
    for entry_id in sorted(drafts, key=lambda key: (FAMILIES.index(drafts[key].catalog.family), key)):
        draft = drafts[entry_id]
        minutes, calls = _estimate(draft)
        entries.append(
            PlanEntry(
                id=entry_id,
                family=draft.catalog.family,
                title=draft.catalog.title,
                status=draft.status,
                reason=draft.reason,
                origin=draft.origin,
                when=draft.when,
                sides=draft.sides,
                paths=draft.paths,
                executor=draft.executor,
                depth=draft.depth,
                required=draft.required,
                escalate=draft.escalate,
                cost_class=draft.catalog.cost_class,
                degrade_tier=draft.catalog.degrade_tier,
                estimated_minutes=minutes,
                estimated_model_calls=calls,
                pulled_in_by=tuple(draft.pulled_in_by),
                setup_hint=draft.catalog.setup_hint if draft.status == "inapplicable" else "",
                available=bool(draft.catalog.executor),
                external=draft.catalog.external,
                budget_reduced_from=draft.budget_reduced_from,
                floor_kind=draft.floor_kind if draft.required else "",
                satisfied_by=draft.satisfied_by,
                selection=draft.selection,
                base_on_demand=draft.base_on_demand,
                rule_refs=draft.rule_refs if draft.status == "run" else (),
                escalation=draft.escalation,
                rule_checks=draft.rule_checks,
                equivalence=draft.equivalence,
            )
        )

    return RunPlan(
        review_id=inputs.review_id,
        revision_id=inputs.revision_id,
        trigger=inputs.trigger,
        selection=selection,
        run_on=run_on,
        run_on_note="; ".join(dict.fromkeys(locus_notes)),
        ceilings=ceilings,
        usage=usage,
        entries=tuple(entries),
        family_modes=dict(settings.capabilities),
        errors=tuple(dict.fromkeys(errors)),
        budget_notes=tuple(dict.fromkeys(notes)),
        inputs_digest=inputs.digest(),
        manual=tuple(
            ManualPick(pick_id, drafts[pick_id].depth, target=manual_targets.get(pick_id), fresh=drafts[pick_id].fresh)
            for pick_id in dict.fromkeys(manual_ids)
        ),
        parent_digest=parent_digest,
        signals={
            "ai_candidates": inputs.ai_candidates,
            "risk_signal": inputs.risk_signal,
            **({"escalation": inputs.escalation.to_wire()} if inputs.escalation is not None else {}),
        },
        detected=inputs.detected.to_wire(),
        notes=tuple(plan_notes),
        fast_lane=fast_lane,
        policy_error=inputs.policy_error,
        escalation=escalation.to_wire() if escalation is not None else {},
    )


def amend_run_plan(
    persisted: RunPlan | None,
    inputs: PlanInputs,
    *,
    usage: BudgetUsage,
    manual: Sequence[ManualPick] = (),
) -> RunPlan:
    """Add per-Review picks to the Snapshot's persisted plan without removing work.

    Entries already planned to run stay pinned (they may already have run) and
    the escalation signals the plan was built with are reused, so a pick can
    only add. A picked entry itself is never pinned: when it does not fit the
    budget it is shown ``limited_by_budget`` instead of silently exceeding it.
    When the plan's inputs changed (policy, trusted base, detection), a fresh
    plan supersedes the persisted one.
    """

    if persisted is None or persisted.inputs_digest != inputs.digest() or persisted.revision_id != inputs.revision_id:
        return rebuild_run_plan(persisted, inputs, usage=usage, manual=manual)
    picks: dict[str, ManualPick] = {pick.id: pick for pick in persisted.manual}
    added = False
    for pick in manual:
        current = persisted.entry(pick.id)
        deeper = (
            pick.depth is not None
            and current is not None
            and PRESET_RANK[pick.depth] > PRESET_RANK.get(current.depth, 0)
        )
        if current is not None and current.status == "run" and not deeper:
            continue
        previous = picks.get(pick.id)
        chosen = pick if previous is None or pick.depth is not None else previous
        target = chosen.target or pick.target or (previous.target if previous is not None else None)
        fresh = pick.fresh or (previous is not None and previous.fresh)
        picks[pick.id] = replace(chosen, target=target, fresh=fresh)
        added = True
    if not added:
        return persisted
    picked = frozenset(picks)
    pinned = tuple(row.id for row in persisted.entries if row.status == "run" and row.id not in picked)
    signals = persisted.signals
    frozen_inputs = replace(
        inputs,
        ai_candidates=int(signals.get("ai_candidates", inputs.ai_candidates) or 0),
        risk_signal=bool(signals.get("risk_signal", inputs.risk_signal)),
        # The escalation signals the plan was built with (none on a plan that predates them).
        escalation=EscalationFacts.from_wire(signals.get("escalation")),
    )
    return build_run_plan(
        frozen_inputs,
        usage=persisted.usage,
        manual=tuple(picks.values()),
        pinned_run=pinned,
        parent_digest=persisted.digest,
    )


def carry_manual_picks(
    persisted: RunPlan | None, inputs: PlanInputs
) -> tuple[tuple[ManualPick, ...], tuple[DroppedPick, ...]]:
    """The manual picks of a superseded plan that a rebuild of the same Snapshot keeps.

    A pick survives a plan-input change (policy, trusted base, detection,
    ingested results) while its capability still resolves: a catalog or
    trusted-base entry, or a surface the Snapshot's own discovery named (the
    Snapshot, so its discovery, is unchanged). Anything else is dropped with the
    exact reason. An earlier drop whose capability resolves again is cleared (its
    reason no longer holds); the pick itself is not restored, since a drop keeps
    no depth or target, so the reviewer picks it again. A plan of another
    Snapshot carries nothing.
    """

    if persisted is None or persisted.revision_id != inputs.revision_id:
        return (), ()
    kept: list[ManualPick] = []
    dropped: dict[str, DroppedPick] = {
        pick.id: pick for pick in persisted.manual_dropped if resolve_entry(pick.id, inputs.detected) is None
    }
    for pick in persisted.manual:
        if not valid_entry_id(pick.id) or pick.id.endswith(":*"):
            reason = f"{pick.id!r} is not a capability id"
        elif resolve_entry(pick.id, inputs.detected) is not None or _discovered_surface_entry(pick) is not None:
            kept.append(pick)
            dropped.pop(pick.id, None)
            continue
        else:
            reason = f"capability {pick.id!r} is no longer in the catalog or trusted base after the plan inputs changed"
        dropped[pick.id] = DroppedPick(pick.id, reason)
    return tuple(kept), tuple(dropped.values())


def rebuild_run_plan(
    persisted: RunPlan | None,
    inputs: PlanInputs,
    *,
    usage: BudgetUsage,
    manual: Sequence[ManualPick] = (),
) -> RunPlan:
    """A fresh plan for changed inputs that keeps every still-valid manual pick.

    ``manual`` are new picks; they win over a carried pick of the same id. Picks
    the rebuild cannot keep are listed on ``manual_dropped`` and in the notes.
    """

    kept, dropped = carry_manual_picks(persisted, inputs)
    requested = {pick.id for pick in manual}
    plan = build_run_plan(
        inputs,
        usage=usage,
        manual=(*(pick for pick in kept if pick.id not in requested), *manual),
    )
    dropped = tuple(pick for pick in dropped if pick.id not in requested)
    if not dropped:
        return plan
    return replace(
        plan,
        manual_dropped=dropped,
        notes=(*plan.notes, *(f"manual pick {pick.id} dropped: {pick.reason}" for pick in dropped)),
    )


def preview_picks(
    persisted: RunPlan | None,
    inputs: PlanInputs,
    *,
    usage: BudgetUsage,
    manual: Sequence[ManualPick],
) -> dict[str, Any]:
    """Cost preview for ``Run specific…`` before confirmation."""

    before = persisted if persisted is not None else build_run_plan(inputs, usage=usage)
    after = amend_run_plan(before, inputs, usage=usage, manual=manual)
    before_status = {row.id: (row.status, row.depth) for row in before.entries}
    added = [row.to_wire() for row in after.entries if before_status.get(row.id) != (row.status, row.depth)]
    delta = {
        "minutes": round(after.totals["minutes"] - before.totals["minutes"], 3),
        "model_calls": after.totals["model_calls"] - before.totals["model_calls"],
    }
    return {
        "changes": added,
        "incremental": delta,
        "limited_by_budget": [row["id"] for row in added if row["status"] == "limited_by_budget"],
        "rejected": [error for error in after.errors if error.startswith("manual pick")],
        "plan": after.to_wire(),
    }


def dependents_of(entry_id: str, selected: Iterable[str]) -> tuple[str, ...]:
    """Selected static entries that stop when ``entry_id`` is deselected."""

    chosen = set(selected)
    stopped: list[str] = []
    frontier = [entry_id]
    while frontier:
        current = frontier.pop()
        for candidate in sorted(chosen):
            entry = STATIC_CATALOG.get(candidate)
            if entry is not None and current in entry.dependencies and candidate not in stopped:
                stopped.append(candidate)
                frontier.append(candidate)
    return tuple(stopped)


# --------------------------------------------------------------------------- #
# executor contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AiStagePlan:
    """Exactly what the AI review executor may do under one persisted plan."""

    plan_digest: str
    effort: Preset
    investigators: bool
    hypotheses: bool
    status: EntryStatus
    reason: str
    max_review_calls: int | None = None
    max_cost_usd: float | None = None
    #: P11-ESC: the rule questions (rule refs) ``ai.rule_checks`` asks under this plan.
    rule_checks: tuple[str, ...] = ()

    @property
    def runs_ai(self) -> bool:
        """Whether the plan runs any AI review stage (investigators or rule checks)."""

        return self.investigators or bool(self.rule_checks)

    def to_wire(self) -> dict[str, Any]:
        return {
            **({"rule_checks": list(self.rule_checks)} if self.rule_checks else {}),
            "max_cost_usd": self.max_cost_usd,
            "plan_digest": self.plan_digest,
            "effort": self.effort,
            "investigators": self.investigators,
            "hypotheses": self.hypotheses,
            "status": self.status,
            "reason": self.reason,
            "max_review_calls": self.max_review_calls,
        }


def ai_stage_plan(plan: RunPlan) -> AiStagePlan:
    investigators = plan.entry("ai.investigators")
    hypotheses = plan.entry("ai.hypotheses")
    rule_checks = plan.entry("ai.rule_checks")
    effort: Preset = investigators.depth if investigators is not None else "auto"
    max_calls: int | None = None
    if "per_review_model_calls" in plan.ceilings:
        max_calls = max(0, int(plan.ceilings["per_review_model_calls"]))
    return AiStagePlan(
        plan_digest=plan.digest,
        effort=effort,
        investigators=investigators is not None and investigators.status == "run",
        hypotheses=hypotheses is not None and hypotheses.status == "run",
        status=investigators.status if investigators is not None else "skipped",
        reason=(
            investigators.reason
            if investigators is not None
            else "ai.investigators is not part of the effective run plan"
        ),
        max_review_calls=max_calls,
        max_cost_usd=(
            float(plan.ceilings["per_review_model_cost_usd"]) if "per_review_model_cost_usd" in plan.ceilings else None
        ),
        rule_checks=rule_checks.rule_refs if rule_checks is not None and rule_checks.status == "run" else (),
    )


# --------------------------------------------------------------------------- #
# persistence (review_derived rows; Snapshot deletion owns retention)
# --------------------------------------------------------------------------- #


def persist_run_plan(store: Any, plan: RunPlan) -> RunPlan:
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    store.put_derived_results(
        [
            DerivedResult(
                review_id=plan.review_id,
                revision_id=plan.revision_id,
                kind=RUN_PLAN_KIND,
                scope_key=RUN_PLAN_SCOPE,
                stamp=plan.digest,
                payload=plan.to_wire(),
            )
        ]
    )
    return plan


def load_run_plan(store: Any, revision_id: str) -> RunPlan | None:
    rows = store.derived_results(RUN_PLAN_KIND, [revision_id])
    row = rows.get((revision_id, RUN_PLAN_SCOPE))
    if row is None:
        return None
    stamp, payload = row
    plan = RunPlan.from_wire(payload)
    if plan.digest != stamp:
        raise ValueError("persisted run plan stamp does not match its digest")
    return plan


# --------------------------------------------------------------------------- #
# settings preview: "What this depth does for this repository"
# --------------------------------------------------------------------------- #


def depth_preview(
    settings: EffectiveRunSettings,
    detected: DetectedCapabilities,
    *,
    monthly_reviews: int | None = None,
) -> dict[str, Any]:
    """Detected capabilities only, per depth, with estimated per-Review/monthly cost.

    Inapplicable entries (nothing to act on) are omitted entirely. Costs are an
    upper bound (Auto escalation assumed to fire) and are estimates, not bills.
    """

    options: list[str] = [*PRESETS, *(["custom"] if settings.custom else [])]
    depths: dict[str, Any] = {}
    for option in options:
        variant = replace(settings, effort=option)
        plan = build_run_plan(
            PlanInputs(
                review_id="preview",
                revision_id="preview",
                trigger="open",
                settings=replace(variant, budget={}),
                detected=detected,
                ai_candidates=3,
                risk_signal=True,
                allowed_run_on=(settings.run_on,),
            )
        )
        rows = [
            {
                "id": row.id,
                "title": row.title,
                "family": row.family,
                "status": row.status,
                "sides": row.sides,
                "estimated_minutes": row.estimated_minutes,
                "estimated_model_calls": row.estimated_model_calls,
            }
            for row in plan.entries
            if row.status != "inapplicable"
        ]
        totals = plan.totals
        depths[option] = {
            "entries": rows,
            "per_review": totals,
            "monthly": (
                None
                if monthly_reviews is None
                else {
                    "minutes": round(totals["minutes"] * monthly_reviews, 3),
                    "model_calls": int(totals["model_calls"]) * monthly_reviews,
                }
            ),
        }
    return {
        "effective": settings.effort,
        "monthly_reviews": monthly_reviews,
        "detected": sorted(detected.facts),
        "base_state": detected.base_state,
        "depths": depths,
        "estimate_basis": "catalog defaults; upper bound with Auto escalation",
    }


def catalog_wire(detected: DetectedCapabilities) -> list[dict[str, Any]]:
    """Catalog entries with anything to act on in this trusted base."""

    rows: list[dict[str, Any]] = []
    for entry in STATIC_CATALOG.values():
        state, _why = applicability(entry, detected)
        # lc-debt: a coverage-unknown entry (detection hit its path bound) is not offered
        # for picking: a pick could only plan not_available, since the fact its executor
        # needs was never found; the plan row shows it with its coverage_unknown reason.
        # Upgrade: an unbounded (or targeted) re-detection for one picked capability.
        if state == "applicable":
            rows.append(entry.to_wire())
    for prefix_rows, prefix in (
        (detected.recipes, "checks.recipe:"),
        (detected.browser_scenarios, "browser:"),
        (detected.surfaces, "surface:"),
    ):
        for target in prefix_rows:
            resolved = resolve_entry(prefix + target.id, detected)
            if resolved is not None and target.applicability != "not_applicable":
                rows.append(resolved.to_wire())
    return rows


__all__ = [
    "BUDGET_FIELDS",
    "LABEL_RULE_INACTIVE",
    "LABEL_SOURCE_AVAILABLE",
    "PATH_SCOPE_SKIP",
    "PROPOSED_BUDGET_DEFAULTS",
    "REQUIRED_NO_EXECUTOR",
    "RUN_PLAN_KIND",
    "AiStagePlan",
    "BudgetUsage",
    "DroppedPick",
    "EffectiveRunSettings",
    "EntrySelection",
    "FloorRule",
    "ManualPick",
    "PlanEntry",
    "PlanInputs",
    "Profile",
    "ProfileRule",
    "RunPlan",
    "RunSettingsLayer",
    "SettingOrigin",
    "ai_stage_plan",
    "amend_run_plan",
    "build_run_plan",
    "carry_manual_picks",
    "catalog_wire",
    "check_catalog_id",
    "dependents_of",
    "depth_preview",
    "layer_from_wire",
    "load_run_plan",
    "parse_review_config_layer",
    "parse_run_settings",
    "persist_run_plan",
    "preview_picks",
    "rebuild_run_plan",
    "resolve_run_settings",
]
