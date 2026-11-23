"""Organization automation rules (Phase 12, P12-AUTO items 8 and 9).

A state-changing or destructive Review operation needs an explicit human
action unless an organization automation rule deliberately enabled it. This
module is the one model of such a rule and the one evaluator of it; it never
performs an action itself.

Authority. Rules come only from operator / organization policy (the hosted
operator Review policy, or the local operator's user-level file). Nothing a
Change contains -- head-side repository text, the PR description, a bot
comment, an AI finding -- can define, enable or widen a rule.

Permissions. A rule names the identity it acts as (``acts_as``). An automated
run is checked against that identity's permissions with the same check a human
or agent doing it by hand gets (:data:`ACTION_PERMISSIONS` names the exact
permission of the human route). Who enabled the rule grants nothing: a rule an
administrator enabled cannot act beyond its acting identity, and no rule ever
acts with administrator authority.

Inspection. :func:`evaluate_rule` explains, for one Review, every guard and
condition it evaluated with the value it saw, including provider
branch-protection facts. Landing actions are guarded by branch protection and
required reviews; those guards are not configurable, an unknown provider fact
fails them, and a rule that tries to reference or bypass them does not parse.

Contract consumed by P10-LAND (auto-approval, native merge, break-glass) --
keep these signatures stable:

``AutomationRule`` / ``AutomationRuleSet``
    the parsed rule (``id``, ``trigger``, ``action``, ``conditions``,
    ``repositories``, ``enabled``, ``enabled_by``, ``enabled_at``,
    ``acts_as``, ``layer``, ``source``, ``params``, ``digest``).
``parse_automation_rules(raw, layer=..., source=...)``
    validated rules plus visible errors; an invalid rule never runs.
``AutomationFacts`` / ``BranchProtectionFacts``
    what the evaluator may read about one Review (closed fact vocabulary).
``evaluate_rule(rule, facts) -> RuleEvaluation``
    inspectable guards + conditions, ``would_fire``.
``decide_automation(action, rules=..., facts=..., permission_check=...)``
    ``AutomationDecision``: ``rule`` (run as ``acts_as`` under ``rule_id``),
    ``needs_human`` (no enabling rule fired) or ``refused`` (a rule fired but
    its acting identity lacks :data:`ACTION_PERMISSIONS[action]`).

P10-LAND executes ``landing.auto_approve`` / ``landing.native_merge``; this
package only parses, evaluates and inspects them (:data:`RESERVED_ACTIONS`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

AutomationTrigger = Literal["merge_observed", "readiness_ready", "production_problem"]
AUTOMATION_TRIGGERS: Final[tuple[AutomationTrigger, ...]] = ("merge_observed", "readiness_ready", "production_problem")

AutomationActionKind = Literal[
    "post_merge.notification",
    "post_merge.changelog_entry",
    "post_merge.ticket_update",
    "landing.auto_approve",
    "landing.native_merge",
    "post_merge.rollback",
]
AUTOMATION_ACTIONS: Final[tuple[AutomationActionKind, ...]] = (
    "post_merge.notification",
    "post_merge.changelog_entry",
    "post_merge.ticket_update",
    "landing.auto_approve",
    "landing.native_merge",
    "post_merge.rollback",
)
POST_MERGE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"post_merge.notification", "post_merge.changelog_entry", "post_merge.ticket_update"}
)
LANDING_ACTIONS: Final[frozenset[str]] = frozenset({"landing.auto_approve", "landing.native_merge"})
RESERVED_ACTIONS: Final[frozenset[str]] = LANDING_ACTIONS
"""Evaluated and inspectable here; executed only by P10-LAND."""

ACTION_TRIGGER: Final[Mapping[str, AutomationTrigger]] = {
    "post_merge.notification": "merge_observed",
    "post_merge.changelog_entry": "merge_observed",
    "post_merge.ticket_update": "merge_observed",
    "landing.auto_approve": "readiness_ready",
    "landing.native_merge": "readiness_ready",
    "post_merge.rollback": "production_problem",
}
STATE_CHANGING_ACTIONS: Final[frozenset[str]] = frozenset(
    {"post_merge.changelog_entry", "post_merge.ticket_update", "post_merge.rollback", *LANDING_ACTIONS}
)
ACTION_PERMISSIONS: Final[Mapping[str, str]] = {
    # The permission of the human route doing the same thing: running a
    # post-merge action and arming landing are Review management; an approval
    # is a judgment. Values are the hosted ``Permission`` values.
    "post_merge.notification": "review:manage",
    "post_merge.changelog_entry": "review:manage",
    "post_merge.ticket_update": "review:manage",
    "landing.auto_approve": "review:judge",
    "landing.native_merge": "review:manage",
    # Rolling production back through the CD system is what a Revert does by hand.
    "post_merge.rollback": "review:manage",
}
ACTION_LABELS: Final[Mapping[str, str]] = {
    "post_merge.notification": "Post-merge notification",
    "post_merge.changelog_entry": "Post-merge changelog entry",
    "post_merge.ticket_update": "Post-merge ticket update",
    "landing.auto_approve": "Auto-approval",
    "landing.native_merge": "Native merge",
    "post_merge.rollback": "Automatic rollback",
}
_ACTION_PARAMS: Final[Mapping[str, frozenset[str]]] = {
    "post_merge.notification": frozenset({"message"}),
    "post_merge.changelog_entry": frozenset({"section"}),
    "post_merge.ticket_update": frozenset({"ticket_ref", "transition"}),
    "landing.auto_approve": frozenset(),
    "landing.native_merge": frozenset(),
    "post_merge.rollback": frozenset({"environment"}),
}

RuleLayer = Literal["organization", "repository", "local_operator"]
RULE_LAYERS: Final[tuple[RuleLayer, ...]] = ("organization", "repository", "local_operator")

HOSTED_ACTING_ROLES: Final[frozenset[str]] = frozenset({"developer", "reviewer", "service"})
"""Roles a hosted rule may act as. ``admin`` is never one: automation has no administrator authority."""
LOCAL_ACTING_ROLES: Final[frozenset[str]] = frozenset({"owner"})
"""The local server has one principal, its owner; a local rule can only act as them."""

FactKind = Literal["bool", "text", "paths"]
FACTS: Final[Mapping[str, tuple[FactKind, str]]] = {
    "merge.observed": ("bool", "The connected provider reported this Change merged"),
    "merge.reviewed_head": ("bool", "The merged head is the reviewed revision's head"),
    "readiness.ready": ("bool", "LemonCrow readiness is ready for the current revision"),
    "review.status": ("text", "The Review's status (open, finished, archived)"),
    "change.provider": ("text", "The connected SCM provider (github, or empty)"),
    "change.repository": ("text", "The repository identity this Review belongs to"),
    "change.title": ("text", "The Change title"),
    "change.paths": ("paths", "Paths the current revision changes"),
    "outcome.production_problem": (
        "bool",
        "Production showed a problem with this merged Change (failed or rolled-back deployment, contradicted "
        "prediction); an incident correlation never counts",
    ),
}
_OPS: Final[Mapping[FactKind, frozenset[str]]] = {
    "bool": frozenset({"is_true", "is_false"}),
    "text": frozenset({"eq", "ne", "in", "not_in"}),
    "paths": frozenset({"any_match", "all_match", "none_match"}),
}
GUARD_FACT_PREFIX: Final[str] = "branch_protection."

MAX_RULES: Final[int] = 50
MAX_CONDITIONS: Final[int] = 20
_MAX_TEXT: Final[int] = 500
_RULE_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SUBJECT: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "description",
        "trigger",
        "action",
        "conditions",
        "repositories",
        "enabled",
        "enabled_by",
        "enabled_at",
        "acts_as",
        "params",
    }
)
_FORBIDDEN_WORDS: Final[tuple[str, ...]] = ("bypass", "override", "skip", "admin", "force", "break_glass")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ActingIdentity:
    """Who an automated run acts as; its permissions bound what the rule can do."""

    subject: str
    role: str

    def to_wire(self) -> dict[str, str]:
        return {"subject": self.subject, "role": self.role}


ConditionValue = str | bool | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleCondition:
    fact: str
    op: str
    value: ConditionValue = ""

    def to_wire(self) -> dict[str, Any]:
        value: Any = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"fact": self.fact, "op": self.op, "value": value}


@dataclass(frozen=True, slots=True)
class AutomationRule:
    """One organization automation rule as policy states it."""

    id: str
    trigger: AutomationTrigger
    action: AutomationActionKind
    acts_as: ActingIdentity
    layer: RuleLayer
    source: str
    enabled: bool = False
    enabled_by: str = ""
    enabled_at: str = ""
    description: str = ""
    conditions: tuple[RuleCondition, ...] = ()
    repositories: tuple[str, ...] = ()
    params: tuple[tuple[str, str], ...] = ()

    def param(self, name: str, default: str = "") -> str:
        return next((value for key, value in self.params if key == name), default)

    def applies_to(self, repository: str) -> bool:
        return not self.repositories or repository in self.repositories

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "trigger": self.trigger,
            "action": self.action,
            "action_label": ACTION_LABELS[self.action],
            "state_changing": self.action in STATE_CHANGING_ACTIONS,
            "reserved": self.action in RESERVED_ACTIONS,
            "permission": ACTION_PERMISSIONS[self.action],
            "conditions": [item.to_wire() for item in self.conditions],
            "repositories": list(self.repositories),
            "enabled": self.enabled,
            "enabled_by": self.enabled_by,
            "enabled_at": self.enabled_at,
            "acts_as": self.acts_as.to_wire(),
            "layer": self.layer,
            "source": self.source,
            "params": dict(self.params),
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_wire())


@dataclass(frozen=True, slots=True)
class AutomationRuleSet:
    """The effective rules for one scope, with any policy errors kept visible."""

    rules: tuple[AutomationRule, ...] = ()
    errors: tuple[str, ...] = ()
    source: str = ""
    edit_source: str = ""

    def for_repository(self, repository: str) -> tuple[AutomationRule, ...]:
        return tuple(rule for rule in self.rules if rule.applies_to(repository))

    @property
    def digest(self) -> str:
        return _digest([rule.digest for rule in self.rules])

    def to_wire(self, repository: str | None = None) -> dict[str, Any]:
        rules = self.rules if repository is None else self.for_repository(repository)
        return {
            "rules": [rule.to_wire() for rule in rules],
            "errors": list(self.errors),
            "source": self.source,
            "edit_source": self.edit_source,
            "digest": self.digest,
        }


ProtectionState = Literal["satisfied", "unsatisfied", "unknown"]


@dataclass(frozen=True, slots=True)
class BranchProtectionFacts:
    """Provider branch-protection / required-review truth as LemonCrow last observed it.

    ``requirements``: every SCM-owned landing requirement (branch protection,
    required status checks). ``required_reviews``: the provider review state on
    the current head. ``unknown`` is never treated as satisfied.
    """

    requirements: ProtectionState = "unknown"
    required_reviews: ProtectionState = "unknown"
    changes_requested: bool = False
    source: str = ""
    detail: str = ""
    rows: tuple[tuple[str, str, str], ...] = ()
    """``(id, label, state)`` of each provider requirement that was read."""

    def to_wire(self) -> dict[str, Any]:
        return {
            "requirements": self.requirements,
            "required_reviews": self.required_reviews,
            "changes_requested": self.changes_requested,
            "source": self.source,
            "detail": self.detail,
            "rows": [{"id": row[0], "label": row[1], "state": row[2]} for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class AutomationFacts:
    """Everything an evaluator may read about one Review; anything absent is unknown."""

    review_id: str
    repository: str
    values: Mapping[str, object] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    branch_protection: BranchProtectionFacts = field(default_factory=BranchProtectionFacts)

    def to_wire(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in FACTS:
            value = self.values.get(name)
            values[name] = list(value) if isinstance(value, tuple) else value
        return {
            "review_id": self.review_id,
            "repository": self.repository,
            "values": values,
            "sources": dict(self.sources),
            "branch_protection": self.branch_protection.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """One evaluated guard or condition, with the value the evaluator saw."""

    name: str
    kind: Literal["guard", "condition"]
    passed: bool
    detail: str
    fact: str = ""
    op: str = ""
    expected: Any = None
    actual: Any = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "fact": self.fact,
            "op": self.op,
            "expected": list(self.expected) if isinstance(self.expected, tuple) else self.expected,
            "actual": list(self.actual) if isinstance(self.actual, tuple) else self.actual,
        }


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: str
    action: str
    trigger: str
    would_fire: bool
    reason: str
    guards: tuple[ConditionResult, ...] = ()
    conditions: tuple[ConditionResult, ...] = ()
    branch_protection: BranchProtectionFacts = field(default_factory=BranchProtectionFacts)
    branch_protection_enforced: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "trigger": self.trigger,
            "would_fire": self.would_fire,
            "reason": self.reason,
            "guards": [item.to_wire() for item in self.guards],
            "conditions": [item.to_wire() for item in self.conditions],
            "branch_protection": self.branch_protection.to_wire(),
            "branch_protection_enforced": self.branch_protection_enforced,
        }


DecisionMode = Literal["rule", "needs_human", "refused"]
PermissionCheck = Callable[[ActingIdentity, str], bool]
"""``(acting identity, permission value) -> held``: the same check a human route applies."""


@dataclass(frozen=True, slots=True)
class AutomationDecision:
    action: str
    mode: DecisionMode
    reason: str
    rule_id: str = ""
    rule_digest: str = ""
    acts_as: ActingIdentity | None = None
    permission: str = ""
    params: tuple[tuple[str, str], ...] = ()
    evaluations: tuple[RuleEvaluation, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.mode == "rule"

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "mode": self.mode,
            "allowed": self.allowed,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "rule_digest": self.rule_digest,
            "acts_as": None if self.acts_as is None else self.acts_as.to_wire(),
            "permission": self.permission,
            "evaluations": [item.to_wire() for item in self.evaluations],
        }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class _RuleError(ValueError):
    pass


def _text(raw: Mapping[str, object], key: str, *, where: str, required: bool = False, limit: int = _MAX_TEXT) -> str:
    value = raw.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _RuleError(f"{where}.{key} must be a string")
    text = value.strip()
    if required and not text:
        raise _RuleError(f"{where}.{key} is required")
    if len(text) > limit:
        raise _RuleError(f"{where}.{key} is longer than {limit} characters")
    return text


def _forbidden(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in _FORBIDDEN_WORDS)


def _condition(raw: object, *, where: str) -> RuleCondition:
    if not isinstance(raw, Mapping):
        raise _RuleError(f"{where} must be an object")
    unknown = sorted(set(raw) - {"fact", "op", "value"})
    if unknown:
        raise _RuleError(f"{where} has unknown field {unknown[0]!r}")
    fact = str(raw.get("fact") or "").strip()
    if fact.startswith(GUARD_FACT_PREFIX) or _forbidden(fact):
        raise _RuleError(
            f"{where}.fact {fact!r} is not configurable: branch protection and required reviews are "
            "guards every landing rule must pass, never conditions a rule can relax or bypass"
        )
    if fact not in FACTS:
        raise _RuleError(f"{where}.fact {fact!r} is not a known automation fact")
    kind = FACTS[fact][0]
    op = str(raw.get("op") or "").strip()
    if op not in _OPS[kind]:
        raise _RuleError(
            f"{where}.op {op!r} does not apply to {fact} (expected one of {', '.join(sorted(_OPS[kind]))})"
        )
    value = raw.get("value")
    if kind == "bool":
        if value not in (None, "", True):
            raise _RuleError(f"{where}.value is not used by {op}")
        return RuleCondition(fact, op, True)
    if op in {"eq", "ne"}:
        if not isinstance(value, str) or len(value) > _MAX_TEXT:
            raise _RuleError(f"{where}.value must be a string")
        return RuleCondition(fact, op, value)
    if not isinstance(value, list) or not value or len(value) > 50:
        raise _RuleError(f"{where}.value must be a non-empty list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > _MAX_TEXT:
            raise _RuleError(f"{where}.value entries must be non-empty strings")
        items.append(item.strip())
    return RuleCondition(fact, op, tuple(items))


def _acting(raw: object, *, where: str, layer: RuleLayer) -> ActingIdentity:
    if not isinstance(raw, Mapping):
        raise _RuleError(f"{where}.acts_as must name the identity the rule acts as ({{subject, role}})")
    unknown = sorted(set(raw) - {"subject", "role"})
    if unknown:
        raise _RuleError(f"{where}.acts_as has unknown field {unknown[0]!r}")
    subject = str(raw.get("subject") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    if not _SUBJECT.fullmatch(subject):
        raise _RuleError(f"{where}.acts_as.subject must be a principal identifier")
    allowed = LOCAL_ACTING_ROLES if layer == "local_operator" else HOSTED_ACTING_ROLES
    if role == "admin":
        raise _RuleError(f"{where}.acts_as.role admin is refused: automation never acts with administrator authority")
    if role not in allowed:
        raise _RuleError(f"{where}.acts_as.role must be one of {', '.join(sorted(allowed))}")
    return ActingIdentity(subject, role)


def _rule(raw: object, *, where: str, layer: RuleLayer, source: str, repositories: Sequence[str]) -> AutomationRule:
    if not isinstance(raw, Mapping):
        raise _RuleError(f"{where} must be an object")
    keys = [str(key) for key in raw]
    forbidden = next((key for key in keys if _forbidden(key)), "")
    if forbidden:
        raise _RuleError(
            f"{where}.{forbidden} is refused: a rule can never bypass branch protection or required reviews"
        )
    unknown = sorted(set(keys) - _RULE_KEYS)
    if unknown:
        raise _RuleError(f"{where} has unknown field {unknown[0]!r}")
    rule_id = _text(raw, "id", where=where, required=True, limit=64)
    if not _RULE_ID.fullmatch(rule_id):
        raise _RuleError(f"{where}.id must be lowercase letters, digits, '.', '_' or '-'")
    action = _text(raw, "action", where=where, required=True, limit=64)
    if action not in AUTOMATION_ACTIONS:
        raise _RuleError(f"{where}.action {action!r} is not an automatable action")
    trigger = _text(raw, "trigger", where=where, required=True, limit=64)
    if trigger not in AUTOMATION_TRIGGERS:
        raise _RuleError(f"{where}.trigger {trigger!r} is not a known trigger")
    if ACTION_TRIGGER[action] != trigger:
        raise _RuleError(f"{where}: {action} only runs on the {ACTION_TRIGGER[action]} trigger")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _RuleError(f"{where}.enabled must be a boolean")
    enabled_by = _text(raw, "enabled_by", where=where, limit=128)
    if enabled and not enabled_by:
        raise _RuleError(f"{where}.enabled_by must name who deliberately enabled this rule")
    raw_conditions = raw.get("conditions", [])
    if not isinstance(raw_conditions, list) or len(raw_conditions) > MAX_CONDITIONS:
        raise _RuleError(f"{where}.conditions must be a list of at most {MAX_CONDITIONS} conditions")
    if trigger == "merge_observed" and any(
        isinstance(item, Mapping) and item.get("fact") == "readiness.ready" for item in raw_conditions
    ):
        raise _RuleError(
            f"{where}: readiness.ready is not a post-merge fact; a merged Change has no readiness to wait for"
        )
    raw_repositories = raw.get("repositories", [])
    if not isinstance(raw_repositories, list) or any(
        not isinstance(item, str) or not _SUBJECT.fullmatch(item) for item in raw_repositories
    ):
        raise _RuleError(f"{where}.repositories must be a list of repository identifiers")
    scope = tuple(dict.fromkeys(str(item) for item in raw_repositories))
    if repositories:
        # A repository-layer rule is confined to its repository; naming another is an error.
        stray = sorted(set(scope) - set(repositories))
        if stray:
            raise _RuleError(f"{where}.repositories names {stray[0]!r} outside this policy layer")
        scope = tuple(repositories) if not scope else scope
    raw_params = raw.get("params", {})
    if not isinstance(raw_params, Mapping):
        raise _RuleError(f"{where}.params must be an object")
    allowed_params = _ACTION_PARAMS[action]
    params: list[tuple[str, str]] = []
    for key in sorted(str(item) for item in raw_params):
        if key not in allowed_params:
            raise _RuleError(f"{where}.params.{key} is not a parameter of {action}")
        value = raw_params[key]
        if not isinstance(value, str) or len(value) > _MAX_TEXT:
            raise _RuleError(f"{where}.params.{key} must be a string of at most {_MAX_TEXT} characters")
        params.append((key, value))
    return AutomationRule(
        id=rule_id,
        trigger=trigger,
        action=action,
        acts_as=_acting(raw.get("acts_as"), where=where, layer=layer),
        layer=layer,
        source=source,
        enabled=enabled,
        enabled_by=enabled_by,
        enabled_at=_text(raw, "enabled_at", where=where, limit=64),
        description=_text(raw, "description", where=where),
        conditions=tuple(
            _condition(item, where=f"{where}.conditions[{index}]") for index, item in enumerate(raw_conditions)
        ),
        repositories=scope,
        params=tuple(params),
    )


def parse_automation_rules(
    raw: object,
    *,
    layer: RuleLayer,
    source: str,
    field_name: str = "automation",
    repositories: Sequence[str] = (),
) -> tuple[tuple[AutomationRule, ...], tuple[str, ...]]:
    """Validated rules plus every error; an invalid rule is dropped, never run.

    ``repositories`` confines a repository-layer document to its repository.
    """

    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), (f"{field_name} must be a list of rules",)
    if len(raw) > MAX_RULES:
        return (), (f"{field_name} has more than {MAX_RULES} rules",)
    rules: list[AutomationRule] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{field_name}[{index}]"
        try:
            rule = _rule(item, where=where, layer=layer, source=source, repositories=repositories)
        except _RuleError as exc:
            errors.append(str(exc))
            continue
        if rule.id in seen:
            errors.append(f"{where}.id {rule.id!r} is used by more than one rule")
            continue
        seen.add(rule.id)
        rules.append(rule)
    return tuple(rules), tuple(errors)


LOCAL_RULES_FILE: Final[str] = "review-automation.json"
LOCAL_EDIT_SOURCE: Final[str] = f"<LEMONCROW_ROOT>/{LOCAL_RULES_FILE} (operator file; never repository text)"


def load_local_automation_rules(root: Path | None = None) -> AutomationRuleSet:
    """The local operator's rules from the user-level LemonCrow root, never from a repository.

    Missing file: no rules (every automated action needs a human). An
    unreadable or invalid file: no rules, with the error kept visible.
    """

    if root is None:
        from lemoncrow.pro.capabilities.review.fix_agent import settings_root

        root = settings_root()
    path = Path(root) / LOCAL_RULES_FILE
    if not path.is_file():
        return AutomationRuleSet(source="local_operator_policy", edit_source=LOCAL_EDIT_SOURCE)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return AutomationRuleSet(
            errors=(f"{LOCAL_RULES_FILE} could not be read: {exc}",),
            source="local_operator_policy",
            edit_source=LOCAL_EDIT_SOURCE,
        )
    if not isinstance(document, Mapping) or document.get("version") != 1:
        return AutomationRuleSet(
            errors=(f"{LOCAL_RULES_FILE} must be an object declaring version 1",),
            source="local_operator_policy",
            edit_source=LOCAL_EDIT_SOURCE,
        )
    rules, errors = parse_automation_rules(
        document.get("rules"), layer="local_operator", source="local_operator_policy", field_name="rules"
    )
    return AutomationRuleSet(rules=rules, errors=errors, source="local_operator_policy", edit_source=LOCAL_EDIT_SOURCE)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _evaluate_condition(condition: RuleCondition, facts: AutomationFacts) -> ConditionResult:
    kind, label = FACTS[condition.fact]
    actual = facts.values.get(condition.fact)
    expected: Any = condition.value
    name = f"{condition.fact} {condition.op}"
    if actual is None:
        # An unknown fact never satisfies a condition, whichever way it is phrased.
        return ConditionResult(
            name, "condition", False, f"{label} is unknown for this Review", condition.fact, condition.op, expected
        )
    if kind == "bool":
        passed = bool(actual) is (condition.op == "is_true")
        return ConditionResult(name, "condition", passed, label, condition.fact, condition.op, None, bool(actual))
    if kind == "text":
        text = str(actual)
        if condition.op == "eq":
            passed = text == expected
        elif condition.op == "ne":
            passed = text != expected
        elif condition.op == "in":
            passed = text in expected
        else:
            passed = text not in expected
        return ConditionResult(name, "condition", passed, label, condition.fact, condition.op, expected, text)
    paths = tuple(str(item) for item in actual) if isinstance(actual, (tuple, list)) else ()
    patterns = tuple(expected) if isinstance(expected, tuple) else ()
    if condition.op == "any_match":
        passed = any(_matches(path, patterns) for path in paths)
    elif condition.op == "all_match":
        passed = bool(paths) and all(_matches(path, patterns) for path in paths)
    else:
        passed = not any(_matches(path, patterns) for path in paths)
    shown = paths[:20] + ((f"... {len(paths) - 20} more",) if len(paths) > 20 else ())
    return ConditionResult(name, "condition", passed, label, condition.fact, condition.op, expected, shown)


def _trigger_guard(rule: AutomationRule, facts: AutomationFacts) -> tuple[ConditionResult, ...]:
    if rule.trigger == "production_problem":
        problem = facts.values.get("outcome.production_problem") is True
        return (
            ConditionResult(
                "production showed a problem",
                "guard",
                problem,
                (
                    "Production showed a problem with this merged Change."
                    if problem
                    else "No observed production problem (or unknown): rollback automation waits for one."
                ),
                "outcome.production_problem",
                "is_true",
                None,
                facts.values.get("outcome.production_problem"),
            ),
        )
    if rule.trigger == "merge_observed":
        observed = facts.values.get("merge.observed") is True
        reviewed = facts.values.get("merge.reviewed_head") is True
        return (
            ConditionResult(
                "merge observed by the provider",
                "guard",
                observed,
                (
                    "The connected provider reported this Change merged."
                    if observed
                    else "No provider-observed merge: post-merge automation waits for provider truth."
                ),
                "merge.observed",
                "is_true",
                None,
                observed,
            ),
            ConditionResult(
                "the reviewed head was merged",
                "guard",
                reviewed,
                (
                    "The provider merged exactly the reviewed revision's head."
                    if reviewed
                    else "The merged head is not the reviewed head (or is unknown); automation never follows it."
                ),
                "merge.reviewed_head",
                "is_true",
                None,
                facts.values.get("merge.reviewed_head"),
            ),
        )
    ready = facts.values.get("readiness.ready") is True
    return (
        ConditionResult(
            "readiness is ready",
            "guard",
            ready,
            "LemonCrow readiness is ready." if ready else "LemonCrow readiness is not ready (or unknown).",
            "readiness.ready",
            "is_true",
            None,
            facts.values.get("readiness.ready"),
        ),
    )


def _protection_guards(protection: BranchProtectionFacts) -> tuple[ConditionResult, ...]:
    unknown = "the provider has not reported it, and unknown is never satisfied"
    return (
        ConditionResult(
            "provider branch protection satisfied",
            "guard",
            protection.requirements == "satisfied",
            {
                "satisfied": "Every provider landing requirement is satisfied.",
                "unsatisfied": "A provider landing requirement (branch protection / required check) is not satisfied.",
            }.get(protection.requirements, f"Provider branch protection is unknown: {unknown}."),
            f"{GUARD_FACT_PREFIX}requirements",
            "eq",
            "satisfied",
            protection.requirements,
        ),
        ConditionResult(
            "provider required reviews satisfied",
            "guard",
            protection.required_reviews == "satisfied" and not protection.changes_requested,
            (
                "A provider reviewer requested changes on the current head."
                if protection.changes_requested
                else {
                    "satisfied": "Provider required reviews are satisfied on the current head.",
                    "unsatisfied": "Provider required reviews are not satisfied.",
                }.get(protection.required_reviews, f"Provider required reviews are unknown: {unknown}.")
            ),
            f"{GUARD_FACT_PREFIX}required_reviews",
            "eq",
            "satisfied",
            protection.required_reviews,
        ),
    )


def evaluate_rule(rule: AutomationRule, facts: AutomationFacts) -> RuleEvaluation:
    """Why *rule* would or would not fire for this Review, guard by guard."""

    guards: list[ConditionResult] = [
        ConditionResult(
            "enabled",
            "guard",
            rule.enabled,
            (
                f"Enabled by {rule.enabled_by}" + (f" at {rule.enabled_at}" if rule.enabled_at else "")
                if rule.enabled
                else "Not enabled in policy."
            ),
        ),
        ConditionResult(
            "repository in scope",
            "guard",
            rule.applies_to(facts.repository),
            (
                "All repositories in this policy layer."
                if not rule.repositories
                else f"Scoped to {', '.join(rule.repositories)}."
            ),
            "change.repository",
            "in",
            rule.repositories or None,
            facts.repository,
        ),
        *_trigger_guard(rule, facts),
    ]
    enforced = rule.action in LANDING_ACTIONS
    if enforced:
        guards.extend(_protection_guards(facts.branch_protection))
    conditions = tuple(_evaluate_condition(item, facts) for item in rule.conditions)
    failed = [item for item in (*guards, *conditions) if not item.passed]
    would_fire = not failed
    if would_fire:
        reason = f"Fires: every guard and condition holds; it acts as {rule.acts_as.subject} ({rule.acts_as.role})."
    else:
        reason = "Does not fire: " + "; ".join(f"{item.name} ({item.detail})" for item in failed[:4])
    return RuleEvaluation(
        rule_id=rule.id,
        action=rule.action,
        trigger=rule.trigger,
        would_fire=would_fire,
        reason=reason,
        guards=tuple(guards),
        conditions=conditions,
        branch_protection=facts.branch_protection,
        branch_protection_enforced=enforced,
    )


def decide_automation(
    action: str,
    *,
    rules: Sequence[AutomationRule],
    facts: AutomationFacts,
    permission_check: PermissionCheck,
) -> AutomationDecision:
    """May *action* run now without a human, and as whom.

    ``needs_human`` unless an enabled rule for exactly this action fires; then
    the rule's acting identity must hold the action's human-route permission
    (``refused`` otherwise). Reserved landing actions are decided the same way
    but are executed only by P10-LAND.
    """

    if action not in AUTOMATION_ACTIONS:
        return AutomationDecision(action, "needs_human", f"{action} is not an automatable action")
    permission = ACTION_PERMISSIONS[action]
    candidates = [rule for rule in rules if rule.action == action]
    evaluations = tuple(evaluate_rule(rule, facts) for rule in candidates)
    firing = [
        (rule, evaluation) for rule, evaluation in zip(candidates, evaluations, strict=True) if evaluation.would_fire
    ]
    if not firing:
        why = (
            "no organization automation rule enables it"
            if not candidates
            else "no enabled organization automation rule for it fires on this Review"
        )
        return AutomationDecision(
            action,
            "needs_human",
            f"{ACTION_LABELS[action]} needs an explicit human action: {why}.",
            permission=permission,
            evaluations=evaluations,
        )
    denied: list[str] = []
    for rule, _evaluation in firing:
        if permission_check(rule.acts_as, permission):
            return AutomationDecision(
                action,
                "rule",
                f"Rule {rule.id} enables it; it runs as {rule.acts_as.subject} ({rule.acts_as.role}) "
                f"under the same {permission} check a human gets.",
                rule_id=rule.id,
                rule_digest=rule.digest,
                acts_as=rule.acts_as,
                permission=permission,
                params=rule.params,
                evaluations=evaluations,
            )
        denied.append(f"rule {rule.id} acts as {rule.acts_as.subject} ({rule.acts_as.role}), who lacks {permission}")
    first = firing[0][0]
    return AutomationDecision(
        action,
        "refused",
        "Refused: " + "; ".join(denied) + ". Automation never exceeds its acting identity's permissions.",
        rule_id=first.id,
        rule_digest=first.digest,
        acts_as=first.acts_as,
        permission=permission,
        evaluations=evaluations,
    )


def local_permission_check(identity: ActingIdentity, permission: str) -> bool:
    """Local Review has one principal, its owner, who holds every Review permission."""

    del permission
    return identity.role in LOCAL_ACTING_ROLES


_SCM_PASS: Final[frozenset[str]] = frozenset({"pass", "satisfied", "not_applicable"})


def branch_protection_from_conditions(
    conditions: Sequence[Mapping[str, Any]],
    *,
    reviews_changes_requested: bool | None = None,
    reviews_approved: bool | None = None,
    source: str = "readiness_scm_requirements",
) -> BranchProtectionFacts:
    """Provider-owned requirements from the readiness conditions the connected SCM owns.

    The SCM-owned (``owner == "scm"``) rows are the provider's own landing
    requirements; none recorded leaves branch protection unknown.
    """

    rows = tuple(
        (str(item.get("id") or ""), str(item.get("label") or ""), str(item.get("state") or "unknown"))
        for item in conditions
        if str(item.get("owner") or "") == "scm"
    )
    if not rows:
        requirements: ProtectionState = "unknown"
    elif all(state in _SCM_PASS for _id, _label, state in rows):
        requirements = "satisfied"
    elif any(state in {"fail", "pending", "blocked"} for _id, _label, state in rows):
        requirements = "unsatisfied"
    else:
        requirements = "unknown"
    if reviews_changes_requested:
        reviews: ProtectionState = "unsatisfied"
    elif reviews_approved:
        reviews = "satisfied"
    else:
        reviews = "unknown"
    return BranchProtectionFacts(
        requirements=requirements,
        required_reviews=reviews,
        changes_requested=bool(reviews_changes_requested),
        source=source if rows or reviews_approved is not None else "",
        detail=(
            f"{len(rows)} provider requirement(s) read"
            if rows
            else "No provider branch-protection requirement is recorded for this Change."
        ),
        rows=rows,
    )


__all__ = [
    "ACTION_LABELS",
    "ACTION_PERMISSIONS",
    "ACTION_TRIGGER",
    "AUTOMATION_ACTIONS",
    "AUTOMATION_TRIGGERS",
    "FACTS",
    "GUARD_FACT_PREFIX",
    "HOSTED_ACTING_ROLES",
    "LANDING_ACTIONS",
    "LOCAL_ACTING_ROLES",
    "LOCAL_RULES_FILE",
    "POST_MERGE_ACTIONS",
    "RESERVED_ACTIONS",
    "STATE_CHANGING_ACTIONS",
    "ActingIdentity",
    "AutomationActionKind",
    "AutomationDecision",
    "AutomationFacts",
    "AutomationRule",
    "AutomationRuleSet",
    "AutomationTrigger",
    "BranchProtectionFacts",
    "ConditionResult",
    "PermissionCheck",
    "RuleCondition",
    "RuleEvaluation",
    "branch_protection_from_conditions",
    "decide_automation",
    "evaluate_rule",
    "load_local_automation_rules",
    "local_permission_check",
    "parse_automation_rules",
]
