"""Deterministic Review readiness core (Phase 10 items 7, 12-17).

One evaluator decides whether a Change is ready to land, for the local Reader
and every hosted composition alike. It is recomputable without a model: the
inputs are stored human decisions, typed check observations, evidence
freshness at an explicit instant, blocking feedback and the effective policy
generation, and the same inputs always produce the same result (and the same
``conditions_hash``).

Contract for downstream packages (P10-PREMERGE, P10-LAND, P10-SIGN, R5):

* :class:`ReadinessCondition` -- one typed condition: ``id``, ``label``,
  ``state`` (:data:`CONDITION_STATES`), ``detail``, ``owner``, ``category``,
  ``reason`` (machine code) and ``evidence_refs``. ``pass``, ``waived`` and
  ``not_applicable`` are satisfied; ``fail``, ``pending`` and ``unknown`` block.
* :class:`ReadinessResult` -- the ordered conditions plus ``ready``,
  ``armable``, ``conditions_hash``, the :class:`PolicyGeneration` it was
  evaluated against, ``evaluated_at`` and ``valid_until`` (earliest expiry of
  time-sensitive evidence a satisfied condition relies on).
* :class:`ConditionRegistry` / :class:`RegisteredCondition` -- registration
  API. A registered condition has an optional ``gather(sources) -> fact``
  (deterministic store reads, no model calls) and a pure
  ``evaluate(inputs) -> Iterable[ReadinessCondition]`` reading its own fact at
  ``inputs.facts[key]``. Compositions extend :data:`DEFAULT_CONDITIONS` with
  :meth:`ConditionRegistry.with_conditions`; ``register_review_api`` accepts
  them through ``readiness_conditions``. An evaluator that raises becomes an
  ``unknown`` blocker, never a silent pass. A composition that owns the fact
  of a core condition supplies only its gatherer
  (:meth:`ConditionRegistry.with_gatherers`, ``readiness_gatherers``): hosted
  operator policy is the core ``org_policy`` condition fed
  :class:`OrgPolicyFacts`, so local and hosted share one evaluation path.
* Transient vs definite: a condition whose ``reason`` is in
  :data:`TRANSIENT_REASONS` (a fact that could not be read, a rule that
  raised) blocks arming but never withdraws an armed landing on its own; only
  :func:`definite_blockers` do.
* Freshness (:mod:`.freshness`): conditions cite evidence ids; the core
  resolves each against ``inputs.evidence`` and downgrades any ``pass`` that
  relies on an expired/unknown external observation to ``unknown``. This is the
  single place item 13 is enforced.
* :class:`PolicyGeneration` -- the durable identity of the effective policy:
  a content digest over ordered :class:`PolicyLayer` digests (built-in rules
  version, trusted-base ``.lemoncrow/review.yaml``, hosted operator policy).
  Head-side policy is never a layer (trusted-base authority). ``conditions_hash``
  binds the generation, so a decision taken under one generation cannot be
  replayed under another; armed landing records it and is revalidated when it
  changes. Approvals record the generation they were given under
  (:class:`ApprovalRecord`); when standing decisions predate the current
  generation, the derived ``policy-generation`` condition names old and new
  generation (pending while the current policy is unsatisfied), and
  compositions receive a typed :class:`PolicyChangeNotice` hook.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import (
    EvidenceFreshness,
    FreshnessPolicy,
    parse_timestamp,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

ConditionState = Literal["pass", "fail", "pending", "unknown", "not_applicable", "waived"]
CONDITION_STATES: Final[frozenset[str]] = frozenset({"pass", "fail", "pending", "unknown", "not_applicable", "waived"})
#: Condition states that do not block landing. ``waived`` is a human override
#: shown as such, never as ``pass``.
SATISFIED_STATES: Final[frozenset[str]] = frozenset({"pass", "not_applicable", "waived"})
#: Condition reasons meaning "could not be evaluated right now" (a store read or
#: a rule that raised), never "not satisfied". Such a condition blocks arming,
#: but on its own never withdraws an armed landing: a transient read failure is
#: not a regression of the human decision the intent carries.
TRANSIENT_REASONS: Final[frozenset[str]] = frozenset({"evaluation_error"})
#: The built-in supply-chain policy as a waivable requirement: a human waiver
#: names it in ``ReviewEvidence.waived_requirement_key``.
SUPPLY_CHAIN_REQUIREMENT_KEY: Final[str] = "supply-chain-policy"
ConditionOwner = Literal["lemoncrow", "scm"]
#: Bumped whenever a built-in rule changes what readiness requires; it is a
#: policy layer, so a rules change yields a new policy generation.
READINESS_RULES_VERSION: Final[int] = 3


# --------------------------------------------------------------------------- #
# policy generation
# --------------------------------------------------------------------------- #


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PolicyLayerState = Literal["final", "provisional", "invalid"]
#: ``invalid``: the authority exists but cannot be applied (a malformed trusted
#: config); it blocks readiness wherever it appears.
POLICY_LAYER_STATES: Final[frozenset[str]] = frozenset({"final", "provisional", "invalid"})


@dataclass(frozen=True, slots=True)
class PolicyLayer:
    """One authority that contributes to the effective merge/review policy."""

    layer: str
    source: str
    digest: str
    state: PolicyLayerState = "final"

    def to_wire(self) -> dict[str, str]:
        return {"layer": self.layer, "source": self.source, "digest": self.digest, "state": self.state}


def policy_layer(
    layer: str,
    source: str,
    document: object,
    *,
    provisional: bool = False,
    state: PolicyLayerState | None = None,
) -> PolicyLayer:
    """A layer whose identity is the canonical digest of its effective document.

    ``state`` (when given) wins over ``provisional``. A composition that owns
    provider-proven trusted-base provenance (hosted) supplies the
    ``trusted_base`` layer this way; it replaces the one the core derives from
    the Snapshot's base tree.
    """

    return PolicyLayer(
        layer=layer,
        source=source,
        digest=_canonical_digest(document),
        state=state if state is not None else "provisional" if provisional else "final",
    )


def built_in_policy_layer() -> PolicyLayer:
    return policy_layer("built_in", "lemoncrow", {"readiness_rules_version": READINESS_RULES_VERSION})


def trusted_base_policy_layer(document: TrustedBaseDocument) -> PolicyLayer:
    """The trusted base's Review config; the Change's own head bytes never enter.

    Uses the same digest Runtime Validation binds requests to, so a policy
    generation and a runtime request agree on what "the trusted policy" was.
    """

    from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest

    return PolicyLayer(
        layer="trusted_base",
        source=document.source or f"trusted base {document.path} ({document.state})",
        digest=trusted_policy_digest(document.content, base_known=document.base_known),
        state="provisional" if document.provisional else "final",
    )


@dataclass(frozen=True, slots=True)
class PolicyGeneration:
    """Durable identity of one effective policy: ``policy:<sha256>`` over its layers.

    Content-addressed, so the same effective policy always has the same
    generation (tighten-then-revert returns to the earlier generation) and any
    change to any layer is a new one.
    """

    digest: str
    layers: tuple[PolicyLayer, ...] = ()

    @property
    def provisional(self) -> bool:
        return any(layer.state == "provisional" for layer in self.layers)

    @property
    def invalid(self) -> bool:
        return any(layer.state == "invalid" for layer in self.layers)

    @property
    def short(self) -> str:
        return self.digest.removeprefix("policy:")[:12]

    def to_wire(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "short": self.short,
            "authority": "invalid" if self.invalid else "provisional" if self.provisional else "final",
            "layers": [layer.to_wire() for layer in self.layers],
        }


def policy_generation(layers: Iterable[PolicyLayer]) -> PolicyGeneration:
    ordered = tuple(layers)
    digest = _canonical_digest([layer.to_wire() for layer in ordered])
    return PolicyGeneration(digest=f"policy:{digest}", layers=ordered)


def short_generation(digest: str) -> str:
    """The 12-character display form of a generation digest (``unrecorded`` when empty)."""

    return digest.removeprefix("policy:")[:12] or "unrecorded"


# --------------------------------------------------------------------------- #
# conditions and result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Evidence a condition relies on; freshness is filled in by the core."""

    evidence_id: str
    freshness_class: str = ""
    state: str = ""
    observed_at: str = ""
    source_revision: str = ""
    expires_at: str = ""
    reason: str = ""
    #: The :mod:`validity` reason code the freshness guard resolved (``""``: not
    #: resolved yet). Explanatory only and not on the wire, so it never moves a
    #: conditions digest.
    reason_code: str = ""

    def to_wire(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "freshness_class": self.freshness_class,
            "state": self.state,
            "observed_at": self.observed_at,
            "source_revision": self.source_revision,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReadinessCondition:
    """One typed readiness condition. ``extra`` carries composition-specific wire keys."""

    id: str
    label: str
    state: str
    detail: str
    owner: str = "lemoncrow"
    category: str = "review"
    url: str = ""
    reason: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Unknown vocabulary never passes: it is normalized to a blocker.
        if self.state not in CONDITION_STATES:
            object.__setattr__(self, "state", "pending")
        if self.owner not in {"lemoncrow", "scm"}:
            object.__setattr__(self, "owner", "lemoncrow")

    @property
    def satisfied(self) -> bool:
        return self.state in SATISFIED_STATES

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {key: value for key, value in self.extra.items() if key not in _CORE_KEYS}
        wire.update(
            {
                "id": self.id,
                "label": self.label,
                "state": self.state,
                "detail": self.detail,
                "owner": self.owner,
                "category": self.category,
                "url": self.url,
                "reason": self.reason,
                "evidence_refs": [ref.to_wire() for ref in self.evidence_refs],
            }
        )
        return wire


_CORE_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "label", "state", "detail", "owner", "category", "url", "reason", "evidence_refs"}
)


def condition_from_mapping(raw: Mapping[str, Any], *, fallback_id: str) -> ReadinessCondition:
    """Adopt a composition-supplied condition dict; unknown states block.

    ``evidence_ids`` (or ``evidence_refs`` rows with ``evidence_id``) become
    typed refs so the freshness guard applies to outer conditions too.
    """

    refs: list[EvidenceRef] = []
    for value in raw.get("evidence_ids", ()) or ():
        if str(value or ""):
            refs.append(EvidenceRef(evidence_id=str(value)))
    for row in raw.get("evidence_refs", ()) or ():
        if isinstance(row, Mapping) and str(row.get("evidence_id") or ""):
            refs.append(EvidenceRef(evidence_id=str(row["evidence_id"])))
    return ReadinessCondition(
        id=str(raw.get("id") or fallback_id),
        label=str(raw.get("label") or "Additional landing requirement"),
        state=str(raw.get("state") or "pending"),
        detail=str(raw.get("detail") or "Outer composition requirement."),
        owner=str(raw.get("owner") or "lemoncrow"),
        category=str(raw.get("category") or "policy"),
        url=str(raw.get("url") or ""),
        reason=str(raw.get("reason") or ""),
        evidence_refs=tuple(refs),
        extra={key: value for key, value in raw.items() if key not in _CORE_KEYS and key != "evidence_ids"},
    )


def conditions_digest(conditions: Sequence[Mapping[str, Any]], *, policy_generation: str = "") -> str:
    """Stable compare-and-swap identity for the exact conditions a human saw.

    With a policy generation the digest also binds it, so a view taken under
    one policy cannot arm landing under another even if every condition reads
    the same.
    """

    normalized = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "state": str(item.get("state") or ""),
            "detail": str(item.get("detail") or ""),
            "owner": str(item.get("owner") or ""),
            "category": str(item.get("category") or ""),
            "url": str(item.get("url") or ""),
        }
        for item in conditions
    ]
    document: object = (
        normalized if not policy_generation else {"conditions": normalized, "policy_generation": policy_generation}
    )
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    review_id: str
    snapshot_id: str
    evaluated_at: str
    policy: PolicyGeneration
    conditions: tuple[ReadinessCondition, ...]

    @property
    def blockers(self) -> tuple[ReadinessCondition, ...]:
        return tuple(item for item in self.conditions if not item.satisfied)

    @property
    def local_blockers(self) -> tuple[ReadinessCondition, ...]:
        return tuple(item for item in self.blockers if item.owner != "scm")

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def armable(self) -> bool:
        return not self.local_blockers

    @property
    def conditions_hash(self) -> str:
        return conditions_digest([item.to_wire() for item in self.conditions], policy_generation=self.policy.digest)

    @property
    def valid_until(self) -> str:
        """Earliest expiry among time-sensitive evidence satisfied conditions rely on."""

        instants = [
            parsed
            for item in self.conditions
            if item.satisfied
            for ref in item.evidence_refs
            if (parsed := parse_timestamp(ref.expires_at)) is not None
        ]
        if not instants:
            return ""
        from lemoncrow.pro.capabilities.review.freshness import format_timestamp

        return format_timestamp(min(instants))

    def to_wire(self) -> dict[str, Any]:
        return {
            "conditions": [item.to_wire() for item in self.conditions],
            "ready": self.ready,
            "armable": self.armable,
            "blocker_count": len(self.blockers),
            "local_blocker_count": len(self.local_blockers),
            "conditions_hash": self.conditions_hash,
            "policy_generation": self.policy.to_wire(),
            "evaluated_at": self.evaluated_at,
            "valid_until": self.valid_until,
        }


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReadinessSources:
    """What a registered condition's ``gather`` may read: durable state only."""

    store: Any
    session: Any
    revision: Any
    evaluated_at: str
    policy: PolicyGeneration


@dataclass(frozen=True, slots=True)
class ReadinessInputs:
    """Everything one evaluation reads. ``evaluated_at`` is the only clock."""

    review_id: str
    snapshot_id: str
    snapshot_number: int
    evaluated_at: str
    policy: PolicyGeneration
    freshness: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    evidence: Mapping[str, EvidenceFreshness] = field(default_factory=dict)
    facts: Mapping[str, object] = field(default_factory=dict)

    def fact(self, key: str) -> Any:
        return self.facts.get(key)


ConditionEvaluator = Callable[[ReadinessInputs], Iterable[ReadinessCondition]]
FactGatherer = Callable[[ReadinessSources], object]


@dataclass(frozen=True, slots=True)
class RegisteredCondition:
    """``key`` names the fact slot and the registration; ``order`` sorts output."""

    key: str
    order: int
    evaluate: ConditionEvaluator
    gather: FactGatherer | None = None


class ConditionRegistry:
    """Ordered, immutable-by-copy set of readiness conditions."""

    def __init__(self, entries: Iterable[RegisteredCondition] = ()) -> None:
        self._entries: dict[str, RegisteredCondition] = {}
        for entry in entries:
            self._add(entry)

    def _add(self, entry: RegisteredCondition) -> None:
        if not entry.key or entry.key in self._entries:
            raise ValueError(f"readiness condition {entry.key!r} is empty or already registered")
        self._entries[entry.key] = entry

    @property
    def entries(self) -> tuple[RegisteredCondition, ...]:
        return tuple(sorted(self._entries.values(), key=lambda item: (item.order, item.key)))

    def with_conditions(self, *entries: RegisteredCondition) -> ConditionRegistry:
        return ConditionRegistry((*self._entries.values(), *entries))

    def with_gatherers(self, gatherers: Mapping[str, FactGatherer]) -> ConditionRegistry:
        """Supply the fact of an already registered condition (e.g. hosted operator policy).

        The evaluator stays the core's, so every composition that supplies the
        fact shares one evaluation path; naming an unregistered key is an error.
        """

        unknown = sorted(set(gatherers) - set(self._entries))
        if unknown:
            raise ValueError(f"no readiness condition is registered for {', '.join(unknown)}")
        return ConditionRegistry(
            replace(entry, gather=gatherers[key]) if key in gatherers else entry for key, entry in self._entries.items()
        )

    def gather(self, sources: ReadinessSources, facts: Mapping[str, object]) -> dict[str, object]:
        """Fill every registered fact the composition did not already supply.

        A gatherer that raises leaves its fact unset; its evaluator then reports
        the condition as it would for missing input (never a pass by default).
        """

        gathered = dict(facts)
        for entry in self.entries:
            if entry.gather is None or entry.key in gathered:
                continue
            try:
                gathered[entry.key] = entry.gather(sources)
            except Exception as exc:  # isolation: one broken gatherer never hides the rest
                gathered[entry.key] = _GatherFailed(str(exc) or exc.__class__.__name__)
        return gathered

    def evaluate(self, inputs: ReadinessInputs) -> ReadinessResult:
        conditions: list[ReadinessCondition] = []
        for entry in self.entries:
            fact = inputs.facts.get(entry.key)
            if isinstance(fact, _GatherFailed):
                conditions.append(_evaluation_failure(entry.key, fact.error))
                continue
            try:
                conditions.extend(entry.evaluate(inputs))
            except Exception as exc:  # fail closed: a broken rule blocks, it never passes
                conditions.append(_evaluation_failure(entry.key, str(exc) or exc.__class__.__name__))
        evaluated = tuple(_apply_freshness(item, inputs.evidence) for item in conditions)
        return ReadinessResult(
            review_id=inputs.review_id,
            snapshot_id=inputs.snapshot_id,
            evaluated_at=inputs.evaluated_at,
            policy=inputs.policy,
            # Derived from every other condition, so it is evaluated last and
            # listed first: why the decisions on this Change are being re-checked.
            conditions=(*_policy_change(inputs, evaluated), *evaluated),
        )


@dataclass(frozen=True, slots=True)
class _GatherFailed:
    error: str


def fact_unavailable(error: str) -> object:
    """A fact that could not be read: its condition reports ``unknown`` (transient), never a pass."""

    return _GatherFailed(error or "unavailable")


def definite_blockers(conditions: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """LemonCrow-owned blockers that are a definite "not satisfied" (wire form).

    Only these may withdraw an armed landing; a blocker whose reason is in
    :data:`TRANSIENT_REASONS` blocks arming but defers, never cancels.
    """

    return [
        item
        for item in conditions
        if str(item.get("owner") or "lemoncrow") != "scm"
        and str(item.get("state") or "") not in SATISFIED_STATES
        and str(item.get("reason") or "") not in TRANSIENT_REASONS
    ]


def _evaluation_failure(key: str, error: str) -> ReadinessCondition:
    return ReadinessCondition(
        id=f"readiness-error:{key}",
        label="Readiness condition unavailable",
        state="unknown",
        detail=f"The {key} readiness condition could not be evaluated: {error[:300]}",
        category="policy",
        reason="evaluation_error",
    )


def _apply_freshness(item: ReadinessCondition, evidence: Mapping[str, EvidenceFreshness]) -> ReadinessCondition:
    """Resolve cited evidence freshness; an expired observation never keeps a pass."""

    if not item.evidence_refs:
        return item
    refs: list[EvidenceRef] = []
    stale: list[EvidenceRef] = []
    for ref in item.evidence_refs:
        known = evidence.get(ref.evidence_id)
        if known is None:
            resolved = replace(
                ref,
                state="unknown",
                reason="the cited evidence is not on this Snapshot",
                reason_code="evidence_not_on_snapshot",
            )
        else:
            resolved = replace(
                ref,
                freshness_class=known.freshness_class,
                state=known.state,
                observed_at=known.observed_at,
                source_revision=known.source_revision,
                expires_at=known.expires_at,
                reason=known.reason,
                reason_code=known.reason_code,
            )
        refs.append(resolved)
        if resolved.state != "current":
            stale.append(resolved)
    updated = replace(item, evidence_refs=tuple(refs))
    if item.state != "pass" or not stale:
        return updated
    reasons = sorted({ref.reason for ref in stale if ref.reason})
    return replace(
        updated,
        state="unknown",
        reason="evidence_expired",
        detail=(
            f"{item.detail} {len(stale)} cited observation(s) are no longer current"
            + (f" ({'; '.join(reasons)})" if reasons else "")
            + "; re-observe on this Snapshot. An expired observation is never counted as passing."
        ).strip(),
    )


# --------------------------------------------------------------------------- #
# verification gate (shared with the Finish summary)
# --------------------------------------------------------------------------- #

_SUPPLY_CHAIN_KINDS: Final[frozenset[str]] = frozenset({"sbom", "provenance", "vulnerability_scan"})


def _gate_h_advisory(row: Mapping[str, Any], flag: str) -> bool:
    # Gate H stamps ``qualification`` on LemonCrow-executed rows only.
    mark = row.get("qualification")
    return isinstance(mark, Mapping) and mark.get(flag) is True


def verification_gate(verification: Mapping[str, Any], *, as_enforced: bool = False) -> dict[str, list[Any]]:
    """The verification rows that gate landing, by how they gate it.

    One rule for readiness and the Finish summary, so the two never disagree
    on what blocks: a check observed on this Snapshot, a non-PASS carried
    from an earlier one until that check reports again, and a
    repository-required browser scenario. The authoring session's own report
    (its placeholders for checks it never ran included) and optional browser
    scenarios are context, never blockers; a human waiver unblocks a row
    without passing it. A human observation's PASS supports review but is
    never counted as a verification pass (``human_passes``).

    Gate H: a LemonCrow-executed row that is advisory (enforce mode, not
    qualified) is shown (``advisory``) but neither satisfies nor blocks: it is
    left out of every generic bucket, and a required browser scenario whose
    only result is advisory stays open (the requirement is unmet, the result
    neither passes nor fails it). ``as_enforced`` applies what enforcement
    would (``would_be_advisory``), for report mode's would-be effect.
    """

    flag = "would_be_advisory" if as_enforced else "advisory"
    checks = verification.get("checks", ())
    advisory = [row for row in checks if _gate_h_advisory(row, flag)]
    advisory_ids = {id(row) for row in advisory}
    generic = [
        row
        for row in checks
        if row.get("kind") not in _SUPPLY_CHAIN_KINDS and not row.get("browser") and id(row) not in advisory_ids
    ]
    current = [row for row in generic if row.get("state") == "current"]
    required_browser = [
        row for row in checks if row.get("browser") and row.get("state") == "current" and row.get("required")
    ]
    return {
        "current": current,
        "human_passes": [row for row in current if row.get("authority") == "human" and row.get("status") == "PASS"],
        "waived": [row for row in generic if row.get("waiver")],
        "carried": [
            row
            for row in generic
            if row.get("state") == "stale" and row.get("status") != "PASS" and not row.get("waiver")
        ],
        "stale_passes": [row for row in generic if row.get("state") == "stale" and row.get("status") == "PASS"],
        "failed": [row for row in current if row.get("status") == "FAIL"],
        "unresolved": [row for row in current if row.get("status") in {"NOT_RUN", "UNKNOWN"} and not row.get("waiver")],
        "required_browser": required_browser,
        "failed_browser": [
            row for row in required_browser if row.get("status") == "FAIL" and id(row) not in advisory_ids
        ],
        "open_browser": [
            row for row in required_browser if row.get("status") in {"NOT_RUN", "UNKNOWN"} or id(row) in advisory_ids
        ],
        "advisory": advisory,
    }


def _gate_h_mode(projection: Mapping[str, Any]) -> str:
    summary = projection.get("qualification")
    return str(summary.get("mode") or "") if isinstance(summary, Mapping) else ""


def _gate_h_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        mark = row.get("qualification")
        mark = mark if isinstance(mark, Mapping) else {}
        out.append(
            {
                "name": str(row.get("name") or "Verification"),
                "stable_key": str(row.get("stable_key") or ""),
                "evidence_id": str(row.get("evidence_id") or ""),
                "status": str(row.get("status") or ""),
                "qualification": str(mark.get("state") or ""),
                "reason": str(mark.get("reason") or ""),
            }
        )
    return out


def _gate_h_enforced(condition: ReadinessCondition, rows: Sequence[Mapping[str, Any]]) -> ReadinessCondition:
    """A condition decided without advisory rows, naming them (Gate H enforce mode)."""

    if not rows:
        return condition
    named = "; ".join(
        f"{row['name']} ({row['status'] or 'UNKNOWN'}, {row['reason']})" for row in _gate_h_rows(rows)[:4]
    ) + (f" and {len(rows) - 4} more" if len(rows) > 4 else "")
    note = (
        f"{len(rows)} LemonCrow-executed result(s) are advisory until Gate H qualifies them; "
        f"shown, not counted: {named}."
    )
    only = condition.state == "not_applicable"
    return replace(
        condition,
        detail=(
            "Only advisory LemonCrow-executed results are recorded for this Snapshot. " + note
            if only
            else f"{condition.detail} {note}".strip()
        ),
        reason="advisory_only" if only else condition.reason,
        extra={
            **condition.extra,
            "evidence_qualification": {"gate": "H", "mode": "enforce", "advisory": _gate_h_rows(rows)},
        },
    )


def _gate_h_reported(
    condition: ReadinessCondition, would_be: ReadinessCondition | None, rows: Sequence[Mapping[str, Any]]
) -> ReadinessCondition:
    """Report mode: the condition is unchanged; what enforcement would make of it rides in ``extra``.

    ``extra`` is outside :func:`conditions_digest`, so report mode never moves
    the digest a human acts on.
    """

    if not rows:
        return condition
    return replace(
        condition,
        extra={
            **condition.extra,
            "evidence_qualification": {
                "gate": "H",
                "mode": "report",
                "advisory": [],
                "would_be_advisory": _gate_h_rows(rows),
                "would_be": (
                    None
                    if would_be is None
                    else {"state": would_be.state, "reason": would_be.reason, "detail": would_be.detail}
                ),
            },
        },
    )


def _with_gate_h(
    projection: Mapping[str, Any],
    build: Callable[[dict[str, list[Any]]], ReadinessCondition | None],
    relevant: Callable[[Mapping[str, Any]], bool],
) -> ReadinessCondition | None:
    """Build a verification-derived condition under the Gate H mode the projection was annotated with."""

    gate = verification_gate(projection)
    condition = build(gate)
    mode = _gate_h_mode(projection)
    if condition is None or mode not in {"report", "enforce"}:
        return condition
    if mode == "enforce":
        return _gate_h_enforced(condition, [row for row in gate["advisory"] if relevant(row)])
    would = verification_gate(projection, as_enforced=True)
    rows = [row for row in would["advisory"] if relevant(row)]
    built = build(would) if rows else None
    return _gate_h_reported(condition, _gate_h_enforced(built, rows) if built is not None else None, rows)


@dataclass(frozen=True, slots=True)
class ReviewProgressFacts:
    unreviewed: int = 0
    unknown: int = 0
    changed_since_review: int = 0
    needs_changes: int = 0


@dataclass(frozen=True, slots=True)
class BehaviorFacts:
    """Present only when this Snapshot observed behavior deltas (R2 contract)."""

    open: int = 0
    blocked: int = 0
    #: A planned behavior producer (the static inventory) has not finished: its
    #: changes are unknown, so nothing may pass on their absence.
    analyzing: bool = False
    #: Refused older behavior contracts must not make missing targets look safe.
    incompatible_contracts: int = 0
    #: Incomplete/non-comparable recorded runtime observations must not look safe.
    incomplete_observations: int = 0
    #: Gate H: outstanding/blocked targets from advisory LemonCrow-executed
    #: results (enforce mode), left out of ``open`` / ``blocked``.
    advisory: int = 0
    #: Gate H mode (``""``: not evaluated) and, in report mode, what
    #: enforcement would count: ``open``, ``blocked``, ``advisory``.
    gate_h_mode: str = ""
    would_be: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class ManualVerificationFacts:
    """The trusted-base manual checklist (R2): one status per item, ``None`` = unrecorded."""

    policy_error: str = ""
    required: bool = False
    statuses: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class HumanFeedbackFacts:
    request_changes: int = 0
    request_changes_removed: int = 0
    comments: int = 0
    comments_removed: int = 0


@dataclass(frozen=True, slots=True)
class VerificationFacts:
    """The Snapshot's verification projection (checks, artifacts, browser summary)."""

    projection: Mapping[str, Any]
    snapshot_number: int = 0


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One current human LGTM and the policy generation it was given under (``""``: unrecorded)."""

    reviewer_id: str
    policy_generation: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalFacts:
    changes_requested: int = 0
    approvals: int = 0
    records: tuple[ApprovalRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class OrgScopeFact:
    kind: str
    values: tuple[str, ...]
    satisfied: bool


@dataclass(frozen=True, slots=True)
class OrgCheckFact:
    """One operator-required typed check, as the operator policy projected it on this Snapshot."""

    stable_key: str
    requirement_key: str
    satisfied: bool
    waived: bool = False
    expired: bool = False
    freshness_reason: str = ""
    allow_waiver: bool = False
    evidence_id: str = ""


@dataclass(frozen=True, slots=True)
class OrgPolicyFacts:
    """An operator (organization/repository) Review policy projected onto one Snapshot.

    Supplied by the composition that owns operator policy (hosted) through
    ``ConditionRegistry.with_gatherers({"org_policy": ...})``; evaluated by
    the core like every other condition. Absent (local), it adds nothing.
    """

    minimum_reviewers: int = 0
    required_reviewers: tuple[str, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    required_scopes: tuple[OrgScopeFact, ...] = ()
    required_checks: tuple[OrgCheckFact, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyDecisionFacts:
    """The policy generations a Change's standing human decisions were taken under."""

    landing_generation: str = ""
    approval_generations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyChangeNotice:
    """Typed hook payload: a Change's decisions were taken under an older policy generation.

    Emitted once per (previous generations, generation) with the
    ``policy.generation_changed`` activity event. Routing it to the people who
    must act (ReviewAttention) belongs to the composition that registers the
    hook (``register_review_api(policy_change_hook=...)``).
    """

    review_id: str
    snapshot_id: str
    previous_generations: tuple[str, ...]
    generation: str
    tightened: bool
    blocked_by: tuple[str, ...] = ()
    approved_under_previous: tuple[str, ...] = ()


def approval_records(store: Any, review_id: str, revision_id: str) -> tuple[ApprovalRecord, ...]:
    """Current human LGTMs on one Snapshot with the generation each was recorded under."""

    approvals = [
        item for item in store.latest_outcomes(review_id) if item.revision_id == revision_id and item.outcome == "lgtm"
    ]
    if not approvals:
        return ()
    wanted = {item.id for item in approvals}
    generations: dict[str, str] = {}
    for event in store.list_activity_events(review_id, kinds=("outcome.recorded",), limit=5000):
        if event.subject_id not in wanted:
            continue
        try:
            detail = json.loads(event.detail_json or "{}")
        except ValueError:
            detail = {}
        if isinstance(detail, Mapping):
            generations[event.subject_id] = str(detail.get("policy_generation") or "")
    return tuple(
        ApprovalRecord(reviewer_id=item.reviewer_id, policy_generation=generations.get(item.id, ""))
        for item in sorted(approvals, key=lambda row: row.reviewer_id)
    )


def _approved_under(records: Iterable[ApprovalRecord], generation: str) -> str:
    """`` alice approved under policy <short>.`` for approvals given under another generation."""

    older = [item for item in records if item.policy_generation and item.policy_generation != generation]
    if not older:
        return ""
    named = "; ".join(
        f"{item.reviewer_id} approved under policy {short_generation(item.policy_generation)}" for item in older[:4]
    )
    return f" {named}{f' and {len(older) - 4} more' if len(older) > 4 else ''}."


@dataclass(frozen=True, slots=True)
class ScmRequirementFact:
    id: str
    label: str
    status: str
    provider: str = ""
    url: str = ""


@dataclass(frozen=True, slots=True)
class RunPlanBlocker:
    """One required run-plan entry that will not run (R1): Limited by budget or no executor."""

    id: str
    title: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class RunPlanFacts:
    """Required work the Snapshot's effective run plan does not run.

    ``error`` is set when the plan could not be computed; ``policy_error`` when
    an invalid trusted-base config stopped every entry. Either keeps the
    condition pending, never PASS.
    """

    snapshot_number: int
    blockers: tuple[RunPlanBlocker, ...] = ()
    error: str = ""
    policy_error: str = ""


@dataclass(frozen=True, slots=True)
class StackDependencyFact:
    """One blocking relation. ``target_ref`` empty: the SCM owns its state."""

    id: str
    label: str
    url: str = ""
    target_ref: str = ""
    target_found: bool = False
    target_status: str = ""
    target_landing_status: str = ""
    raw_status: str = ""


# --------------------------------------------------------------------------- #
# built-in evaluators
# --------------------------------------------------------------------------- #

_PASS_STATES = frozenset({"pass", "passed", "success", "satisfied", "ready", "complete", "completed", "merged"})
_FAIL_STATES = frozenset({"fail", "failed", "blocked", "rejected", "error"})


def normalized_requirement_state(value: object) -> str:
    state = str(value or "").strip().lower()
    if state in _PASS_STATES:
        return "pass"
    if state in _FAIL_STATES:
        return "fail"
    return "pending"


def _policy_authority(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    invalid = [layer for layer in inputs.policy.layers if layer.state == "invalid"]
    if invalid:
        # An authority that exists but cannot be applied (a malformed trusted
        # config) is never read as defaults, whatever else governs: block.
        return (
            ReadinessCondition(
                id="policy-authority",
                label="Trusted Review policy",
                state="fail",
                detail=(
                    f"Policy authority is invalid ({', '.join(layer.source for layer in invalid)}): the trusted "
                    "Review policy cannot be applied, so what this Change must satisfy is not known. Fix the "
                    "trusted base's configuration; a head-side policy is never promoted."
                ),
                category="policy",
                reason="invalid_policy",
            ),
        )
    # Without any final authority beyond the built-in rules, an unreadable
    # trusted base leaves what this Change must satisfy unknown: block. A
    # composition's final operator policy (hosted org/repo policy) is the
    # governing authority there; the provisional repository layer stays
    # visible through the generation's ``authority`` instead of blocking every
    # Change whose provider event carries no base tree.
    if not inputs.policy.provisional or any(
        layer.state == "final" and layer.layer not in {"built_in", "trusted_base"} for layer in inputs.policy.layers
    ):
        return ()
    layers = ", ".join(layer.source for layer in inputs.policy.layers if layer.state == "provisional")
    return (
        ReadinessCondition(
            id="policy-authority",
            label="Trusted Review policy",
            state="unknown",
            detail=(
                f"Policy authority is provisional ({layers}): the trusted base could not be read, so what this "
                "Change must satisfy is not known. A head-side policy is never promoted; readiness stays blocked "
                "until the trusted base is available."
            ),
            category="policy",
            reason="provisional_policy",
        ),
    )


def _latest_snapshot_reviewed(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    progress = inputs.fact("review_progress")
    if not isinstance(progress, ReviewProgressFacts):
        raise ValueError("review progress is unavailable")
    pending = progress.unreviewed + progress.unknown + progress.changed_since_review + progress.needs_changes
    return (
        ReadinessCondition(
            id="latest-snapshot-reviewed",
            label="Latest revision reviewed",
            state="pass" if pending == 0 else "pending",
            detail=(
                "All review areas on the latest revision have current human judgment."
                if pending == 0
                else f"{pending} review area(s) still need human judgment or re-review."
            ),
            category="human_review",
            reason="satisfied" if pending == 0 else "awaiting_human",
        ),
    )


def _behavior_condition(
    open_count: int,
    blocked: int,
    analyzing: bool,
    advisory: int,
    *,
    incompatible_contracts: int = 0,
    incomplete_observations: int = 0,
) -> ReadinessCondition:
    state = (
        "unknown"
        if incompatible_contracts or incomplete_observations
        else "fail" if blocked else "pending" if open_count or analyzing else "pass"
    )
    detail = (
        f"{incompatible_contracts} behavior producer result(s) use an incompatible contract and must be regenerated before approval."
        if incompatible_contracts
        else (
            f"{incomplete_observations} runtime observation(s) are incomplete or non-comparable; re-run to verify behavior changes."
            if incomplete_observations
            else (
                f"{blocked} behavior change(s) were judged as needing changes."
                if blocked
                else (
                    f"{open_count} observed behavior change(s) still need human judgment."
                    if open_count
                    else (
                        "Static analysis of this Snapshot is still running; its behavior changes are not known yet."
                        if analyzing
                        else "Every observed behavior change has current human judgment."
                    )
                )
            )
        )
    )
    if advisory:
        detail += (
            f" {advisory} behavior change(s) come from LemonCrow-executed results that are advisory until Gate H "
            "qualifies them; they are shown but neither block nor satisfy this condition."
        )
    return ReadinessCondition(
        id="behavior-changes-judged",
        label="Behavior changes judged",
        state=state,
        detail=detail,
        category="human_review",
        reason=(
            "incompatible_behavior_contract"
            if incompatible_contracts
            else (
                "incomplete_runtime_observation"
                if incomplete_observations
                else {"fail": "judged_needs_changes", "pending": "awaiting_human"}.get(state, "satisfied")
            )
        ),
    )


def _behavior_changes_judged(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # Behavior targets are canonical review targets: present only when this
    # Snapshot has observed behavior deltas, and settled only by a human.
    behavior = inputs.fact("behavior")
    if not isinstance(behavior, BehaviorFacts):
        return ()
    condition = _behavior_condition(
        behavior.open,
        behavior.blocked,
        behavior.analyzing,
        behavior.advisory,
        incompatible_contracts=behavior.incompatible_contracts,
        incomplete_observations=behavior.incomplete_observations,
    )
    if behavior.gate_h_mode == "enforce" and behavior.advisory:
        condition = replace(
            condition,
            extra={"evidence_qualification": {"gate": "H", "mode": "enforce", "advisory_targets": behavior.advisory}},
        )
    elif behavior.gate_h_mode == "report" and behavior.would_be and behavior.would_be.get("advisory"):
        would = behavior.would_be
        enforced = _behavior_condition(
            int(would.get("open") or 0),
            int(would.get("blocked") or 0),
            behavior.analyzing,
            int(would["advisory"]),
            incompatible_contracts=behavior.incompatible_contracts,
            incomplete_observations=behavior.incomplete_observations,
        )
        condition = replace(
            condition,
            extra={
                "evidence_qualification": {
                    "gate": "H",
                    "mode": "report",
                    "advisory_targets": 0,
                    "would_be_advisory_targets": int(would["advisory"]),
                    "would_be": {"state": enforced.state, "reason": enforced.reason, "detail": enforced.detail},
                }
            },
        )
    return (condition,)


def _manual_verification(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # Manual verification gates only when the trusted base requires it, or when
    # its manual_verification section is invalid (never silently "not
    # required"). A wholly unparsable review.yaml is reported where it is parsed.
    manual = inputs.fact("manual_verification")
    if not isinstance(manual, ManualVerificationFacts):
        return ()
    error = manual.policy_error
    if not (error or (manual.required and manual.statuses)):
        return ()
    failed = sum(1 for status in manual.statuses if status == "FAIL")
    open_count = sum(1 for status in manual.statuses if status is None or status != "PASS") - failed
    state = "pending" if error else "fail" if failed else "pending" if open_count else "pass"
    return (
        ReadinessCondition(
            id="manual-verification",
            label="Manual verification recorded",
            state=state,
            detail=(
                f"Manual verification policy could not be read: {error}"
                if error
                else (
                    f"{failed} required manual check(s) were observed failing."
                    if failed
                    else (
                        f"{open_count} required manual check(s) have no passing human observation yet."
                        if open_count
                        else "Every required manual check has a passing human observation."
                    )
                )
            ),
            category="verification",
            reason=(
                "policy_unreadable"
                if error
                else "observed_failing" if failed else "awaiting_human" if open_count else "satisfied"
            ),
        ),
    )


def _human_feedback(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    feedback = inputs.fact("human_feedback")
    if not isinstance(feedback, HumanFeedbackFacts):
        raise ValueError("human feedback state is unavailable")

    def removed_note(count: int) -> str:
        return f" {count} refer(s) to code that was since removed; a human must still resolve them." if count else ""

    return (
        ReadinessCondition(
            id="human-change-requests",
            label="No unresolved request-change comments",
            state="pass" if not feedback.request_changes else "fail",
            detail=(
                "No unresolved human request-change comment remains."
                if not feedback.request_changes
                else f"{feedback.request_changes} unresolved human request-change comment(s) remain."
                + removed_note(feedback.request_changes_removed)
            ),
            category="human_review",
            reason="satisfied" if not feedback.request_changes else "changes_requested",
        ),
        ReadinessCondition(
            id="human-comments",
            label="No unresolved human comments",
            state="pass" if not feedback.comments else "pending",
            detail=(
                "No unresolved human review discussion remains."
                if not feedback.comments
                else f"{feedback.comments} unresolved human review discussion(s) remain."
                + removed_note(feedback.comments_removed)
            ),
            category="human_review",
            reason="satisfied" if not feedback.comments else "awaiting_human",
        ),
    )


def _names(rows: Sequence[Mapping[str, Any]]) -> str:
    names = [str(row.get("name") or "Verification") for row in rows]
    return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")


def _row_refs(rows: Iterable[Mapping[str, Any]]) -> tuple[EvidenceRef, ...]:
    seen: dict[str, EvidenceRef] = {}
    for row in rows:
        evidence_id = str(row.get("evidence_id") or "")
        if evidence_id and evidence_id not in seen:
            seen[evidence_id] = EvidenceRef(evidence_id=evidence_id)
    return tuple(seen.values())


def _verification(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    facts = inputs.fact("verification")
    if not isinstance(facts, VerificationFacts):
        raise ValueError("verification state is unavailable")
    condition = _with_gate_h(facts.projection, _verification_condition, lambda row: not row.get("browser"))
    assert condition is not None
    return (condition,)


def _verification_condition(gate: dict[str, list[Any]]) -> ReadinessCondition:
    # A check whose latest result on this Change was not PASS keeps blocking
    # as NOT_RUN on later Snapshots until that same check reports again: a
    # new Snapshot must never launder a failure into "no verification". A
    # stale PASS is named but never counted as passing. A human waiver on this
    # exact Snapshot unblocks such a row (and a current NOT_RUN/UNKNOWN)
    # without ever counting it as a pass. A human observation's PASS supports
    # review but never stands in for a CI/provider/LemonCrow result.
    human_pass_ids = {id(row) for row in gate["human_passes"]}
    counted = [row for row in gate["current"] if id(row) not in human_pass_ids]
    waived = gate["waived"]
    carried = gate["carried"]
    stale_passes = gate["stale_passes"]
    failed = len(gate["failed"])
    current_unresolved = len(gate["unresolved"])
    unresolved = current_unresolved + len(carried)
    passed_rows = [row for row in counted if row.get("status") == "PASS"]
    human_passes = len(gate["human_passes"])
    human_note = (
        f" {human_passes} human observation(s) recorded PASS; they support review but are not counted as verification."
        if human_passes
        else ""
    )
    state = (
        "fail"
        if failed
        else ("pending" if unresolved else "waived" if waived else "pass" if counted else "not_applicable")
    )
    waived_detail = (
        f"{len(waived)} check(s) waived by a human on this Snapshot, not passed: "
        + "; ".join(
            f"{row.get('name') or 'Verification'} ({str((row.get('waiver') or {}).get('reason') or '').strip()})"
            for row in waived[:4]
        )
        + (f" and {len(waived) - 4} more" if len(waived) > 4 else "")
        + "."
    )
    unresolved_detail = " ".join(
        part
        for part in (
            f"{current_unresolved} current check(s) are NOT_RUN or UNKNOWN." if current_unresolved else "",
            (
                f"{len(carried)} check(s) whose last result was not PASS have not reported on this "
                f"Snapshot: {_names(carried)}."
                if carried
                else ""
            ),
        )
        if part
    )
    passed = len(passed_rows)
    if failed:
        detail = f"{failed} current check(s) failed."
    elif unresolved:
        detail = unresolved_detail
    elif waived:
        detail = (f"{passed} current check(s) passed. " if passed else "") + waived_detail
    elif counted:
        detail = f"{passed} current check(s) passed with no current failure or unresolved result." + human_note
    elif stale_passes:
        detail = (
            "No verification result is recorded for this Snapshot; "
            f"{len(stale_passes)} earlier passing check(s) are stale and not counted: {_names(stale_passes)}."
            + human_note
        )
    else:
        detail = "No required verification result is configured or recorded for this Snapshot." + human_note
    return ReadinessCondition(
        id="verification",
        label=(
            "Verification waived"
            if state == "waived"
            else ("Verification passed" if counted or carried or waived else "Verification requirements")
        ),
        state=state,
        detail=detail,
        category="verification",
        reason={
            "fail": "failed",
            "pending": "not_run",
            "waived": "waived",
            "pass": "satisfied",
        }.get(state, "not_configured"),
        # Everything a pass relies on is cited, so the freshness guard can
        # expire a pass built on an external observation.
        evidence_refs=_row_refs([*passed_rows, *gate["failed"]]),
    )


def _browser_regression(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # Browser regression: only repository-required, applicable scenarios gate
    # landing. Optional scenarios stay informative in verification.
    facts = inputs.fact("verification")
    if not isinstance(facts, VerificationFacts):
        raise ValueError("verification state is unavailable")
    projection = facts.projection
    summary = projection.get("browser") or {}
    out: list[ReadinessCondition] = []
    if summary.get("policy_error"):
        out.append(
            ReadinessCondition(
                id="browser-regression-policy",
                label="Browser regression policy",
                state="pending",
                detail=(
                    f"{summary.get('policy_source') or 'Review configuration'} could not be read: "
                    f"{summary.get('policy_error')}. Required browser checks cannot be determined."
                ),
                category="verification",
                reason="policy_unreadable",
            )
        )
    number = facts.snapshot_number

    def build(gate: dict[str, list[Any]]) -> ReadinessCondition | None:
        required = gate["required_browser"]
        if not required:
            return None
        failed = gate["failed_browser"]
        open_rows = gate["open_browser"]

        def names(rows: list[Mapping[str, Any]]) -> str:
            return ", ".join(f"{row.get('name')} ({row.get('status')})" for row in rows[:4]) + (
                "…" if len(rows) > 4 else ""
            )

        state = "fail" if failed else "pending" if open_rows else "pass"
        return ReadinessCondition(
            id="browser-regression",
            label=(
                "Required browser regression passed" if not failed and not open_rows else "Required browser regression"
            ),
            state=state,
            detail=(
                f"Failed on Snapshot {number}: {names(failed)}."
                if failed
                else (
                    f"No current PASS for Snapshot {number}: {names(open_rows)}."
                    if open_rows
                    else f"{len(required)} required browser scenario(s) passed on this exact Snapshot."
                )
            ),
            category="verification",
            reason={"fail": "failed", "pending": "not_run"}.get(state, "satisfied"),
            evidence_refs=_row_refs(required if state == "pass" else ()),
        )

    condition = _with_gate_h(projection, build, lambda row: bool(row.get("browser") and row.get("required")))
    if condition is not None:
        out.append(condition)
    return out
    return out


def supply_chain_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Which supply-chain verdicts are the *same* policy observation over time.

    Typed attestations are keyed by their typed identity; untyped rows by kind
    and observation source (falling back to title). The authority lane is
    always part of the key, so a human upload never supersedes a CI/feed
    verdict by copying its title or source text.
    """

    typed = str(row.get("check_key") or "")
    if typed:
        return ("typed", typed)
    return (
        str(row.get("authority") or ""),
        str(row.get("kind") or ""),
        str(row.get("observation_source") or row.get("title") or ""),
    )


def _latest_by_identity(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    newest: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for row in rows:
        key = supply_chain_identity(row)
        best = newest.get(key)
        if best is None or _observation_rank(row) > _observation_rank(best):
            newest[key] = row
    return [newest[key] for key in sorted(newest)]


def _observation_rank(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    return (
        int(row.get("snapshot_number") or 0),
        str(row.get("source_observed_at") or ""),
        str(row.get("observed_at") or ""),
        str(row.get("id") or ""),
    )


def _feed_attested(row: Mapping[str, Any]) -> bool:
    return str(row.get("check_key") or "").startswith("trusted_security_feed|")


def _supply_chain(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    facts = inputs.fact("verification")
    if not isinstance(facts, VerificationFacts):
        raise ValueError("verification state is unavailable")
    artifacts = [row for row in facts.projection.get("artifacts", ()) if row.get("kind") in _SUPPLY_CHAIN_KINDS]
    rows = [row for row in artifacts if row.get("state") == "current"]

    # Supply-chain artifacts are evidence, not a policy verdict by themselves.
    # Only a typed ``trusted_security_feed`` attestation can satisfy this
    # condition (or an explicit human waiver). Any other verdict -- a human
    # upload, or an upload whose source label is self-declared -- is
    # unattested: its PASS never satisfies, supersedes or clears anything,
    # while its non-PASS still blocks (fail closed). Per observation identity
    # the newest counted observation is the verdict (a feed re-scan
    # supersedes an expired or failed scan); the freshness guard expires
    # external observations.
    def counted(row: Mapping[str, Any]) -> bool:
        status = row.get("verification_status")
        return bool(status) and (status != "PASS" or _feed_attested(row))

    policy_rows = _latest_by_identity(row for row in rows if counted(row))
    unattested = [row for row in rows if row.get("verification_status") == "PASS" and not _feed_attested(row)]
    failed = [row for row in policy_rows if row.get("verification_status") == "FAIL"]
    unresolved = [row for row in policy_rows if row.get("verification_status") in {"NOT_RUN", "UNKNOWN"}]
    # Same carry-forward rule as checks: the latest earlier verdict for a
    # policy that has not been re-evaluated on this Snapshot keeps blocking
    # unless it passed.
    current_keys = {supply_chain_identity(row) for row in policy_rows}
    prior = _latest_by_identity(row for row in artifacts if row.get("state") == "stale" and counted(row))
    carried = [
        row
        for row in prior
        if supply_chain_identity(row) not in current_keys and row.get("verification_status") != "PASS"
    ]
    blocking = bool(failed or unresolved or carried)
    # An explicit human waiver on this Snapshot unblocks anything but a
    # current attested FAIL; it never changes an observed verdict.
    waivable = blocking and not any(_feed_attested(row) for row in failed)
    raw_waiver = facts.projection.get("supply_chain_waiver")
    waiver = raw_waiver if waivable and isinstance(raw_waiver, Mapping) else None
    state = (
        "waived"
        if waiver is not None
        else (
            "fail" if failed else ("pending" if unresolved or carried else "pass" if policy_rows else "not_applicable")
        )
    )
    if failed:
        detail = f"{len(failed)} current supply-chain policy result(s) failed."
    elif unresolved or carried:
        detail = " ".join(
            part
            for part in (
                (f"{len(unresolved)} current supply-chain policy result(s) remain unresolved." if unresolved else ""),
                (
                    f"{len(carried)} earlier supply-chain verdict(s) that did not pass have not been "
                    "re-evaluated on this Snapshot: "
                    + ", ".join(str(row.get("title") or row.get("kind") or "") for row in carried[:4])
                    + "."
                    if carried
                    else ""
                ),
            )
            if part
        )
    elif policy_rows:
        detail = "Current dependency / supply-chain policy results passed."
    elif rows:
        detail = f"{len(rows)} current supply-chain artifact(s) are available, but no policy verdict is recorded."
    else:
        detail = "No dependency / supply-chain policy evidence is required or recorded for this Snapshot."
    if unattested:
        detail += (
            f" {len(unattested)} PASS verdict(s) without a trusted security feed attestation (a human upload or "
            "a self-declared source) support review only; they never satisfy this policy."
        )
    if waiver is not None:
        detail = (
            f"Explicitly waived on this Snapshot by {waiver.get('actor') or 'a reviewer'}: "
            f"{waiver.get('reason') or 'no reason recorded'}. The observed verdicts are unchanged: {detail}"
        )
    return (
        ReadinessCondition(
            id="supply-chain-policy",
            label="Dependency / supply-chain policy passed" if policy_rows else "Dependency / supply-chain policy",
            state=state,
            detail=detail,
            category="supply_chain",
            reason={"fail": "failed", "pending": "not_run", "pass": "satisfied", "waived": "waived"}.get(
                state, "not_configured"
            ),
            evidence_refs=tuple(
                EvidenceRef(evidence_id=str(row.get("id"))) for row in policy_rows if str(row.get("id") or "")
            ),
            extra={
                "requirement_key": SUPPLY_CHAIN_REQUIREMENT_KEY,
                "stable_key": SUPPLY_CHAIN_REQUIREMENT_KEY,
                "waivable": waivable and waiver is None,
            },
        ),
    )


def _human_approval(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    approval = inputs.fact("approval")
    if not isinstance(approval, ApprovalFacts):
        raise ValueError("reviewer outcomes are unavailable")
    if approval.changes_requested:
        return (
            ReadinessCondition(
                id="human-approval-state",
                label="Required human approval state",
                state="fail",
                detail=f"{approval.changes_requested} current human reviewer outcome(s) request changes.",
                category="approval",
                reason="changes_requested",
            ),
        )
    if approval.approvals:
        return (
            ReadinessCondition(
                id="human-approval-state",
                label="Human approval state",
                state="pass",
                detail=(
                    f"{approval.approvals} current human LGTM outcome(s); no current changes-requested outcome."
                    + _approved_under(approval.records, inputs.policy.digest)
                ),
                category="approval",
                reason="satisfied",
            ),
        )
    return (
        ReadinessCondition(
            id="human-approval-state",
            label="Additional LemonCrow approval",
            state="not_applicable",
            detail="No additional LemonCrow approval policy is configured for this Change.",
            category="approval",
            reason="not_configured",
        ),
    )


def _scm_requirements(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    rows = inputs.fact("scm_requirements") or ()
    return tuple(
        ReadinessCondition(
            id=f"scm-requirement:{row.id}",
            label=row.label,
            state=normalized_requirement_state(row.status),
            detail=(row.provider or "Connected SCM") + " remains authoritative for this landing requirement.",
            owner="scm",
            category="scm",
            url=row.url,
            reason="scm_owned",
        )
        for row in rows
        if isinstance(row, ScmRequirementFact)
    )


def _stack(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    out: list[ReadinessCondition] = []
    for row in inputs.fact("stack") or ():
        if not isinstance(row, StackDependencyFact):
            continue
        state = "pending"
        detail = "Dependent Change state is not yet known."
        owner = "scm" if not row.target_ref else "lemoncrow"
        if row.target_ref:
            if not row.target_found:
                state, detail = "fail", "The dependent Change cannot be resolved."
            elif row.target_status == "archived":
                state, detail = "fail", f"{row.target_ref} is archived."
            elif normalized_requirement_state(row.target_landing_status) == "pass":
                state, detail = "pass", f"{row.target_ref} has a confirmed landed state."
            elif row.target_status == "finished":
                detail = f"{row.target_ref} review is finished, but landing is not confirmed."
            else:
                detail = f"{row.target_ref} is still open or not yet confirmed landed."
        else:
            state = normalized_requirement_state(row.raw_status)
            detail = "Connected SCM remains authoritative for this related Change."
        out.append(
            ReadinessCondition(
                id=f"stack:{row.id}",
                label=f"Dependency {row.label}",
                state=state,
                detail=detail,
                owner=owner,
                category="stack",
                url=row.url,
                reason={"pass": "satisfied", "fail": "dependency_unavailable"}.get(state, "awaiting_dependency"),
            )
        )
    return out


def _org_policy(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    """Operator Review policy: required approvals, reviewers, scopes and typed checks."""

    policy = inputs.fact("org_policy")
    if not isinstance(policy, OrgPolicyFacts):
        return ()
    generation = inputs.policy.digest
    approvers = {item.reviewer_id for item in policy.approvals}
    out: list[ReadinessCondition] = []
    if policy.minimum_reviewers:
        met = len(approvers) >= policy.minimum_reviewers
        out.append(
            ReadinessCondition(
                id="org-policy:minimum-approvals",
                label="Required human approvals",
                state="pass" if met else "pending",
                detail=(
                    f"{len(approvers)}/{policy.minimum_reviewers} required human approval(s) are current for this "
                    "Snapshot." + _approved_under(policy.approvals, generation)
                ),
                category="approval",
                reason="satisfied" if met else "awaiting_human",
            )
        )
    for reviewer in policy.required_reviewers:
        record = next((item for item in policy.approvals if item.reviewer_id == reviewer), None)
        out.append(
            ReadinessCondition(
                id=f"org-policy:reviewer:{reviewer}",
                label=f"Approval from {reviewer}",
                state="pass" if record is not None else "pending",
                detail=(
                    f"{reviewer} has a current human LGTM for this Snapshot." + _approved_under((record,), generation)
                    if record is not None
                    else f"Required reviewer {reviewer} has not approved this Snapshot."
                ),
                category="approval",
                reason="satisfied" if record is not None else "awaiting_human",
            )
        )
    for index, scope in enumerate(policy.required_scopes):
        out.append(
            ReadinessCondition(
                id=f"org-policy:scope:{index}",
                label=f"Required {scope.kind or 'review'} scope completed",
                state="pass" if scope.satisfied else "pending",
                detail=", ".join(scope.values) or "Required review scope.",
                category="approval",
                reason="satisfied" if scope.satisfied else "awaiting_human",
            )
        )
    verification = inputs.fact("verification")
    marks: dict[str, Mapping[str, Any]] = {}
    if isinstance(verification, VerificationFacts):
        for row in verification.projection.get("checks", ()) or ():
            mark = row.get("qualification") if isinstance(row, Mapping) else None
            if isinstance(mark, Mapping) and str(row.get("evidence_id") or ""):
                marks[str(row["evidence_id"])] = mark
    for check in policy.required_checks:
        mark = marks.get(check.evidence_id, {}) if check.evidence_id else {}
        condition = _org_check_condition(check, advisory=mark.get("advisory") is True)
        if mark.get("mode") == "enforce" and mark.get("advisory") is True and check.satisfied:
            condition = replace(
                condition,
                extra={
                    **condition.extra,
                    "evidence_qualification": {"gate": "H", "mode": "enforce", "reason": mark.get("reason")},
                },
            )
        elif mark.get("mode") == "report" and mark.get("would_be_advisory") is True and check.satisfied:
            would = _org_check_condition(check, advisory=True)
            condition = replace(
                condition,
                extra={
                    **condition.extra,
                    "evidence_qualification": {
                        "gate": "H",
                        "mode": "report",
                        "reason": mark.get("reason"),
                        "would_be": {"state": would.state, "reason": would.reason, "detail": would.detail},
                    },
                },
            )
        out.append(condition)
    return out


def _org_check_condition(check: OrgCheckFact, *, advisory: bool) -> ReadinessCondition:
    # An expired observation is UNKNOWN, never a pass; a waiver never
    # changes the observed state. Gate H: a pass resting on an advisory
    # LemonCrow-executed result does not satisfy the requirement.
    satisfied = check.satisfied and not advisory
    state = (
        "waived"
        if check.waived
        else ("pass" if satisfied else "pending" if check.satisfied else "unknown" if check.expired else "pending")
    )
    held = check.satisfied and not satisfied and not check.waived
    return ReadinessCondition(
        id=f"org-policy:check:{check.requirement_key}",
        label=f"Required check · {check.stable_key}",
        state=state,
        detail=(
            "Organization-required typed attestation passed on a LemonCrow-executed result that is advisory until "
            "Gate H qualifies it; it does not satisfy this requirement."
            if held
            else {
                "waived": (
                    "Organization-required check was explicitly waived on this Snapshot; its observed state "
                    "was not changed."
                ),
                "pass": "Organization-required typed attestation passed on this Snapshot.",
                "unknown": (
                    "Organization-required typed attestation is an external observation that is no longer "
                    f"current ({check.freshness_reason or 'expired'}); re-observe it."
                ),
            }.get(state, "Organization-required typed attestation has not passed on this Snapshot.")
        ),
        category="verification",
        reason=(
            "advisory_evidence"
            if held
            else {"waived": "waived", "pass": "satisfied", "unknown": "evidence_expired"}.get(state, "not_run")
        ),
        # Cited so the freshness guard (which also applies the trusted
        # base's max-age) can expire it.
        evidence_refs=(EvidenceRef(evidence_id=check.evidence_id),) if check.evidence_id else (),
        extra={
            "requirement_key": check.requirement_key,
            "stable_key": check.stable_key,
            "waivable": check.allow_waiver and not satisfied,
        },
    )


def _policy_change(inputs: ReadinessInputs, conditions: Sequence[ReadinessCondition]) -> tuple[ReadinessCondition, ...]:
    """A visible condition when standing decisions were taken under another policy generation.

    Pending (a blocker naming old and new generation) while the current
    policy leaves a definite LemonCrow-owned condition unsatisfied; ``pass``
    when the decisions still satisfy it. It is derived from the other
    conditions, so it never blocks (or withdraws) on its own.
    """

    decisions = inputs.fact("policy_decisions")
    if not isinstance(decisions, PolicyDecisionFacts):
        return ()
    current = inputs.policy.digest
    previous = sorted(
        {
            value
            for value in (decisions.landing_generation, *decisions.approval_generations)
            if value and value != current
        }
    )
    if not previous:
        return ()
    blockers = [
        item
        for item in conditions
        if item.owner != "scm" and not item.satisfied and item.reason not in TRANSIENT_REASONS
    ]
    change = f"generation {', '.join(short_generation(value) for value in previous)} -> {short_generation(current)}"
    labels = "; ".join(item.label for item in blockers[:4]) + (
        f" and {len(blockers) - 4} more" if len(blockers) > 4 else ""
    )
    return (
        ReadinessCondition(
            id="policy-generation",
            label="Review policy changed",
            state="pending" if blockers else "pass",
            detail=(
                f"Review policy changed ({change}) after decisions on this Change were taken; "
                + (f"the current policy is not satisfied: {labels}." if blockers else "they still satisfy it.")
            ),
            category="policy",
            reason="policy_changed" if blockers else "revalidated",
            extra={"previous_generations": previous, "generation": current},
        ),
    )


def _run_plan_required(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    """R1: required work the effective run plan will not run keeps readiness pending.

    Never PASS: the condition is absent when every required entry runs, and
    pending otherwise. A plan that cannot be computed fails closed (pending);
    its reason is transient, so it blocks arming but alone never withdraws an
    armed landing.
    """

    facts = inputs.fact("run_plan")
    if facts is None:
        return ()  # a composition without a run planner
    if not isinstance(facts, RunPlanFacts):
        raise ValueError("run plan state is unavailable")
    if not facts.blockers and not facts.error and not facts.policy_error:
        return ()
    names = ", ".join(
        f"{row.title or row.id} ({'Limited by budget' if row.status == 'limited_by_budget' else 'no executor'})"
        for row in facts.blockers[:4]
    ) + ("…" if len(facts.blockers) > 4 else "")
    if facts.error:
        detail, reason = f"The effective run plan could not be computed: {facts.error}.", "evaluation_error"
    elif facts.policy_error:
        detail, reason = f"Review policy invalid, so nothing is scheduled: {facts.policy_error}.", "policy_invalid"
    else:
        detail, reason = f"Required on Snapshot {facts.snapshot_number} but not run: {names}.", "not_planned"
    return (
        ReadinessCondition(
            id="run-plan-required",
            label="Required checks planned",
            state="pending",
            detail=detail,
            category="verification",
            reason=reason,
            extra={"required_blockers": [row.id for row in facts.blockers]},
        ),
    )


def _extension(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    rows = inputs.fact("extension") or ()
    out: list[ReadinessCondition] = []
    for index, raw in enumerate(rows):
        if isinstance(raw, Mapping):
            out.append(condition_from_mapping(raw, fallback_id=f"extension:{index}"))
    return out


def _premerge(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # Lazy: pre-merge checks build on this module's types.
    from lemoncrow.pro.capabilities.review.premerge_checks import premerge_condition

    return premerge_condition(inputs)


def _independent_ai_review(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # P11 item 14: ``independent_ai_reviewer: require`` is visible, never silently satisfied.
    from lemoncrow.pro.capabilities.review.independence import independence_condition

    return independence_condition(inputs)


def _merge_result(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    # Phase 10 item 18: re-validate what lands (the provider's merge result).
    from lemoncrow.pro.capabilities.review.merge_result import evaluate

    return evaluate(inputs)


DEFAULT_CONDITIONS: Final[ConditionRegistry] = ConditionRegistry(
    (
        RegisteredCondition("policy_authority", 0, _policy_authority),
        RegisteredCondition("review_progress", 10, _latest_snapshot_reviewed),
        RegisteredCondition("behavior", 20, _behavior_changes_judged),
        RegisteredCondition("manual_verification", 30, _manual_verification),
        RegisteredCondition("human_feedback", 40, _human_feedback),
        RegisteredCondition("verification", 50, _verification),
        RegisteredCondition("browser_regression", 60, _browser_regression),
        RegisteredCondition("supply_chain", 70, _supply_chain),
        # Phase 10: error-mode pre-merge checks (``premerge_checks``).
        RegisteredCondition("premerge", 75, _premerge),
        RegisteredCondition("approval", 80, _human_approval),
        RegisteredCondition("scm_requirements", 90, _scm_requirements),
        RegisteredCondition("stack", 100, _stack),
        RegisteredCondition("org_policy", 105, _org_policy),
        RegisteredCondition("run_plan", 107, _run_plan_required),
        RegisteredCondition("independent_ai_review", 108, _independent_ai_review),
        RegisteredCondition("merge_result", 109, _merge_result),
        RegisteredCondition("extension", 110, _extension),
    )
)
"""Built-in conditions, in the order the Status sheet lists them.

Orders 0-110 are taken in steps of 10 (plus ``org_policy`` at 105, whose fact
an operator-policy composition supplies, the R1 ``run_plan`` at 107, the P11
``independent_ai_review`` at 108, the Phase 10 merge result at 109 and the
Phase 10 pre-merge checks at 75);
downstream packages register between them
or after 110. ``policy-generation`` is derived from the rest and listed first
when it applies.
"""


# --------------------------------------------------------------------------- #
# trust section (Trust T2)
# --------------------------------------------------------------------------- #

TRUST_FLAG: Final[str] = "LEMONCROW_REVIEW_TRUST"
TRUST_MODES: Final[tuple[str, ...]] = ("off", "shadow", "on")
TRUST_SECTION_VERSION: Final[int] = 1
TRUST_PROMOTION_TARGET: Final[str] = "merge"
"""Readiness evaluates merge readiness only; other targets wait for T6 promotion profiles."""
_TRUST_WORKFLOW_KEYS: Final[tuple[str, ...]] = ("state", "label", "rule", "explanation")
_TRUST_RECORD_RULES: Final[frozenset[str]] = frozenset({"readiness_stale", "readiness_unknown"})


@dataclass(frozen=True, slots=True)
class SnapshotProgress:
    """The target progress one Review Status read derived, and the Snapshot it is for (Trust T2).

    Handed to the composition's ``review_workflow`` so an Inbox row that counts
    targets reuses it rather than deriving every target again. A composition
    uses it only for this ``revision_id``: a Snapshot recorded between the two
    reads has its own targets.
    """

    revision_id: str
    counts: Mapping[str, int]


def trust_mode() -> str:
    """The T2 flag and kill switch: ``off`` (the default), ``shadow`` or ``on``.

    ``shadow`` builds the trust section on each Review Status read and logs
    its consistency checks without returning it; ``on`` also returns it as
    ``trust``. An unknown value is ``off``. Nothing is stored in any mode, so
    turning it off restores the earlier readiness answer exactly.
    """

    value = os.environ.get(TRUST_FLAG, "").strip().lower()
    return value if value in TRUST_MODES else "off"


def trust_items(result: ReadinessResult) -> list[dict[str, Any]]:
    """Each evaluated condition as one trust item that carries its T1 validity reasons.

    Normalization only. State and owner come from the condition, and the
    action from the one blocking-reason vocabulary. Nothing here decides
    precedence or who acts next. ``owner`` is the authority that owns the
    condition (``lemoncrow`` or ``scm``), not a person.
    """

    from lemoncrow.pro.capabilities.review.blocking_reasons import action_for
    from lemoncrow.pro.capabilities.review.validity import condition_results

    return [
        {
            "key": condition.id,
            "category": condition.category,
            "state": condition.state,
            "satisfied": condition.satisfied,
            "label": condition.label,
            "reason": condition.reason,
            "owner": condition.owner,
            "evidence_refs": [ref.evidence_id for ref in condition.evidence_refs],
            "action": "" if condition.satisfied else action_for(condition.to_wire()),
            "validity": [
                statement.to_wire() for statement in condition_results(condition, current_revision=result.snapshot_id)
            ],
        }
        for condition in result.conditions
    ]


def trust_section(
    result: ReadinessResult,
    *,
    workflow: Mapping[str, Any] | None,
    next_action: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The Review's trust section: the readiness items beside the Inbox's workflow state and next action.

    ``workflow`` and ``next_action`` are the Inbox row's own fields for this
    Review and viewer, copied as given. State and next action are never
    recomputed here, so Status and Inbox cannot disagree. Without them (a
    composition that supplies none, or a read that failed) the summary is
    ``unknown`` and ``next`` is ``None``: it never reads as ready.
    ``valid_until`` is the readiness answer's earliest expiry of relied-upon
    external evidence. ``digest`` covers everything else in the section.
    """

    from lemoncrow.pro.capabilities.review.workflow_state import WORKFLOW_STATES

    summary: dict[str, Any]
    upcoming: dict[str, Any] | None = None
    if (
        isinstance(workflow, Mapping)
        and isinstance(next_action, Mapping)
        and str(workflow.get("state") or "") in WORKFLOW_STATES
    ):
        summary = {key: str(workflow.get(key) or "") for key in _TRUST_WORKFLOW_KEYS}
        summary["source"] = "workflow"
        upcoming = dict(next_action)
    else:
        summary = {
            "state": "unknown",
            "label": "Unknown",
            "rule": "",
            "explanation": "Workflow state is unavailable for this Review",
            "source": "unavailable",
        }
    summary["ready"] = result.ready
    summary["blocker_count"] = len(result.blockers)
    section: dict[str, Any] = {
        "version": TRUST_SECTION_VERSION,
        "revision_id": result.snapshot_id,
        "promotion_target": TRUST_PROMOTION_TARGET,
        "policy_generation": result.policy.digest,
        "valid_until": result.valid_until,
        "items": trust_items(result),
        "summary": summary,
        "next": upcoming,
    }
    section["digest"] = _canonical_digest(section)
    return section


def trust_mismatches(section: Mapping[str, Any]) -> list[str]:
    """Shadow consistency checks between a trust section's workflow and the readiness it was built from.

    The section is built right after this read stored the viewer's readiness
    record, so the Inbox derivation must see that record as current. Any
    code returned here is a defect to investigate, never a state to show:

    * ``workflow_unavailable`` -- the composition supplied no workflow;
    * ``ready_without_readiness`` -- the workflow says ready to merge but the
      live readiness answer has blockers;
    * ``readiness_record_not_current`` -- the workflow read the viewer's record
      as stale or missing just after this read stored it.
    """

    summary = section.get("summary")
    if not isinstance(summary, Mapping) or summary.get("source") != "workflow":
        return ["workflow_unavailable"]
    out: list[str] = []
    if summary.get("state") == "ready_to_merge" and summary.get("ready") is not True:
        out.append("ready_without_readiness")
    if summary.get("rule") in _TRUST_RECORD_RULES:
        out.append("readiness_record_not_current")
    return out


__all__ = [
    "CONDITION_STATES",
    "DEFAULT_CONDITIONS",
    "POLICY_LAYER_STATES",
    "READINESS_RULES_VERSION",
    "SATISFIED_STATES",
    "SUPPLY_CHAIN_REQUIREMENT_KEY",
    "TRANSIENT_REASONS",
    "TRUST_FLAG",
    "TRUST_MODES",
    "TRUST_PROMOTION_TARGET",
    "TRUST_SECTION_VERSION",
    "ApprovalFacts",
    "ApprovalRecord",
    "BehaviorFacts",
    "ConditionEvaluator",
    "ConditionRegistry",
    "ConditionState",
    "EvidenceRef",
    "FactGatherer",
    "HumanFeedbackFacts",
    "ManualVerificationFacts",
    "OrgCheckFact",
    "OrgPolicyFacts",
    "OrgScopeFact",
    "PolicyChangeNotice",
    "PolicyDecisionFacts",
    "PolicyGeneration",
    "PolicyLayer",
    "PolicyLayerState",
    "ReadinessCondition",
    "ReadinessInputs",
    "ReadinessResult",
    "ReadinessSources",
    "RegisteredCondition",
    "ReviewProgressFacts",
    "RunPlanBlocker",
    "RunPlanFacts",
    "ScmRequirementFact",
    "SnapshotProgress",
    "StackDependencyFact",
    "VerificationFacts",
    "approval_records",
    "built_in_policy_layer",
    "condition_from_mapping",
    "conditions_digest",
    "definite_blockers",
    "fact_unavailable",
    "normalized_requirement_state",
    "policy_generation",
    "policy_layer",
    "short_generation",
    "supply_chain_identity",
    "trust_items",
    "trust_mismatches",
    "trust_mode",
    "trust_section",
    "trusted_base_policy_layer",
    "verification_gate",
]
