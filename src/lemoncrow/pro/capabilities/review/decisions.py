"""HumanDecisions: fingerprints, validity, state and queue (spec §5, §7).

Pure. Identity is ``(Change, decision_key)``. Two separate documents are
hashed: the semantic scope (selected option, assumption values, contract
fingerprints, relied check outcomes) and the implementation (content
fingerprints of the decision's targets). Display text, rationale,
producer/session/model and row identifiers never enter either, so rewording
or regeneration cannot invalidate an acceptance.

Validity is derived on read by comparing the documents stored on the
accepting event with the current ones, input by input, so the Reader can say
which input changed and so an input that is merely unavailable yields
*unverified* instead of reopening the decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow.pro.capabilities.review.contracts import UNAVAILABLE
from lemoncrow.pro.capabilities.review.guidance import (
    GuidanceAssumption,
    GuidanceDecision,
    GuidanceDefinition,
    GuidanceView,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_DONE,
    TIER_RE_REVIEW,
    TIER_REVIEW_FIRST,
    QueueReason,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    DecisionAction,
    DecisionDisposition,
    ReviewDecisionEvent,
    ReviewUnit,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

DECISION_FINGERPRINT_VERSION = 1
UNOBSERVED = "unobserved"
# Prefix of a changed-input line whose accepted value was never analyzed.
UNANALYZED_AT_ACCEPTANCE = "Could not be analyzed when it was accepted"
_OUTCOMES = frozenset({"PASS", "FAIL"})

Validity = Literal["current", "implementation_changed_since_acceptance", "needs_reconsideration", "unverified"]


@dataclass(frozen=True)
class DecisionScope:
    decision_key: str
    selected_option_key: str
    positions: tuple[str, ...]
    """Conflict only: each producer's canonical position (producer excluded)."""
    assumptions: tuple[tuple[str, str], ...]
    contracts: tuple[tuple[str, str], ...]
    checks: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DecisionImplementation:
    targets: tuple[tuple[str, str], ...]
    """``(unit_key, content fingerprint | unavailable)``, sorted."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def scope_document(scope: DecisionScope) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "v": DECISION_FINGERPRINT_VERSION,
        "decision_key": scope.decision_key,
        "selected_option_key": scope.selected_option_key,
        "assumptions": dict(scope.assumptions),
        "contracts": dict(scope.contracts),
        "checks": dict(scope.checks),
    }
    if scope.positions:
        doc["positions"] = list(scope.positions)
    return doc


def implementation_document(implementation: DecisionImplementation) -> dict[str, Any]:
    return {"v": DECISION_FINGERPRINT_VERSION, "targets": dict(implementation.targets)}


def semantic_fingerprint(scope: DecisionScope) -> str:
    return _sha(scope_document(scope))


def implementation_fingerprint(implementation: DecisionImplementation) -> str:
    return _sha(implementation_document(implementation))


def check_keys(row: Mapping[str, Any]) -> set[str]:
    keys = {str(row.get("source_ref") or ""), str(row.get("name") or "")}
    browser = row.get("browser")
    if str(row.get("kind") or "") == "browser" and isinstance(browser, Mapping) and browser.get("scenario"):
        # A recorded run's source_ref names its viewport and side
        # (browser:<scenario>:<viewport>:<side>); the logical check a
        # decision relies on is the scenario.
        keys.add(f"browser:{browser['scenario']}")
    return keys - {""}


def observed_outcome(row: Mapping[str, Any]) -> bool:
    # Agent-reported authoring-session rows are not observations of any
    # Snapshot (verification.py), in either direction.
    return str(row.get("state") or "") != "session" and str(row.get("status") or "").upper() in _OUTCOMES


def check_outcomes(relied: Sequence[str], checks: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Newest observed PASS/FAIL per relied logical check; ``unobserved`` otherwise.

    The newest Snapshot with an observation decides; at that Snapshot any
    FAIL (for example at one viewport) is FAIL. A NOT_RUN or unknown row never
    erases an observed outcome, and only an observed PASS<->FAIL change moves
    the semantic scope. Row order does not matter.
    """

    out: dict[str, str] = {key: UNOBSERVED for key in relied if key}
    newest: dict[str, tuple[int, str]] = {}
    for row in checks:
        if not observed_outcome(row):
            continue
        status = str(row.get("status") or "").upper()
        number = row.get("snapshot_number")
        when = (number if isinstance(number, int) and not isinstance(number, bool) else -1, "")
        for key in check_keys(row) & out.keys():
            seen = newest.get(key)
            if seen is None or when[0] > seen[0]:
                newest[key] = when
                out[key] = status
            elif when[0] == seen[0] and status == "FAIL":
                out[key] = status
    return tuple(sorted(out.items()))


def scope_from_decision(
    decision: GuidanceDecision,
    *,
    contracts: Mapping[str, str],
    checks: Sequence[Mapping[str, Any]],
) -> DecisionScope:
    return DecisionScope(
        decision_key=decision.key,
        selected_option_key=decision.selected_option,
        positions=(),
        assumptions=tuple(sorted((item.key, item.value) for item in decision.assumptions)),
        contracts=tuple(sorted((ref, contracts.get(ref, UNAVAILABLE)) for ref in decision.contract_scope)),
        checks=check_outcomes(decision.relied_checks, checks),
    )


def _position(decision: GuidanceDecision) -> str:
    return canonical_json(
        {
            "options": sorted(option.key for option in decision.options),
            "selected": decision.selected_option,
            "assumptions": {item.key: item.value for item in decision.assumptions},
        }
    )


def scope_from_positions(
    decisions: Sequence[GuidanceDecision],
    *,
    contracts: Mapping[str, str],
    checks: Sequence[Mapping[str, Any]],
) -> DecisionScope:
    """One scope for a conflict: every producer's position, producer excluded."""

    key = decisions[0].key
    refs = sorted({ref for decision in decisions for ref in decision.contract_scope})
    relied = sorted({ref for decision in decisions for ref in decision.relied_checks})
    assumptions: dict[str, str] = {}
    for decision in decisions:
        for item in decision.assumptions:
            assumptions.setdefault(item.key, item.value)
    return DecisionScope(
        decision_key=key,
        selected_option_key="|".join(sorted({decision.selected_option for decision in decisions})),
        positions=tuple(sorted({_position(decision) for decision in decisions})),
        assumptions=tuple(sorted(assumptions.items())),
        contracts=tuple((ref, contracts.get(ref, UNAVAILABLE)) for ref in refs),
        checks=check_outcomes(relied, checks),
    )


def _mapping(doc: Mapping[str, Any], field: str) -> dict[str, str]:
    value = doc.get(field)
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, Mapping) else {}


def rebind_lineage(
    accepted: Mapping[str, str],
    now: Mapping[str, str],
    snapshot_units: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Follow accepted targets whose key changed but whose content did not.

    Symbol fingerprints exclude the path (units.symbol_fingerprint), so a
    moved file keeps them. An accepted key that disappeared continues as a new
    key only when exactly one unclaimed new key carries the identical,
    analyzable fingerprint and no other disappeared key claims it too. Every
    other case stays ambiguous lineage (spec §12 Phase 5: only *valid*
    decisions are preserved).

    With *snapshot_units* (every unit's fingerprint on this Snapshot) the
    move must also be unique Snapshot-wide: the accepted key must be gone
    from the Snapshot (not merely from the decision's targets) and its
    fingerprint must occur exactly once, so a retarget onto a copy or a
    duplicated function is never read as the same code.
    """

    missing = [key for key in accepted if key not in now]
    fresh = [key for key in now if key not in accepted]
    if not missing or not fresh:
        return dict(accepted)
    candidates: dict[str, list[str]] = {}
    for key in missing:
        fingerprint = accepted[key]
        if fingerprint == UNAVAILABLE:
            continue
        if snapshot_units is not None and (
            key in snapshot_units or sum(1 for value in snapshot_units.values() if value == fingerprint) != 1
        ):
            continue
        candidates[key] = [new for new in fresh if now[new] == fingerprint]
    claimed: dict[str, int] = {}
    for matches in candidates.values():
        for new in matches:
            claimed[new] = claimed.get(new, 0) + 1
    out = dict(accepted)
    for key, matches in candidates.items():
        if len(matches) == 1 and claimed[matches[0]] == 1:
            out[matches[0]] = out.pop(key)
    return out


def compare(
    accepted_scope: Mapping[str, Any],
    accepted_implementation: Mapping[str, Any],
    scope: DecisionScope,
    implementation: DecisionImplementation,
    *,
    snapshot_units: Mapping[str, str] | None = None,
) -> tuple[Validity, tuple[str, ...]]:
    """Validity of an acceptance against the current inputs, and what changed.

    Precedence: a semantic change (or ambiguous target lineage) needs
    reconsideration; else a body change needs Reconfirm; else an input that
    is unavailable leaves the acceptance unverified; else it is current.
    """

    semantic: list[str] = []
    unavailable: list[str] = []
    body: list[str] = []
    now = scope_document(scope)
    if accepted_scope.get("v") != now["v"]:
        semantic.append("Fingerprint recipe version changed")
    if str(accepted_scope.get("selected_option_key") or "") != scope.selected_option_key:
        semantic.append(
            f"Author's selected option changed from '{accepted_scope.get('selected_option_key') or ''}' "
            f"to '{scope.selected_option_key}'"
        )
    if list(accepted_scope.get("positions") or ()) != list(scope.positions):
        semantic.append("Authors' positions changed")

    before, after = _mapping(accepted_scope, "assumptions"), dict(scope.assumptions)
    for key in sorted(before.keys() | after.keys()):
        if before.get(key) != after.get(key):
            semantic.append(f"Assumption '{key}' changed from '{before.get(key, '')}' to '{after.get(key, '')}'")

    before, after = _mapping(accepted_scope, "contracts"), dict(scope.contracts)
    for key in sorted(before.keys() | after.keys()):
        if key not in before or key not in after:
            semantic.append(f"Contract scope changed: '{key}' {'added' if key in after else 'removed'}")
        elif after[key] == UNAVAILABLE:
            # Never "current": nothing shows the contract is what was accepted.
            unavailable.append(f"Contract '{key}' could not be analyzed on this revision")
        elif before[key] == UNAVAILABLE:
            unavailable.append(f"{UNANALYZED_AT_ACCEPTANCE}: contract '{key}'; reconfirm to record it")
        elif before[key] != after[key]:
            semantic.append(f"Contract '{key}' changed")

    before, after = _mapping(accepted_scope, "checks"), dict(scope.checks)
    for key in sorted(before.keys() | after.keys()):
        if key not in before or key not in after:
            semantic.append(f"Relied checks changed: '{key}' {'added' if key in after else 'removed'}")
        elif before[key] in _OUTCOMES and after[key] in _OUTCOMES and before[key] != after[key]:
            semantic.append(f"Check '{key}' is now {after[key]} (was {before[key]} when accepted)")

    before, after = _mapping(accepted_implementation, "targets"), dict(implementation.targets)
    before = rebind_lineage(before, after, snapshot_units)
    removed_keys = before.keys() - after.keys()
    if not removed_keys and after.keys() - before.keys():
        # Every accepted target is still there (unit keys are position-
        # independent); new code joined the scope. That is an implementation
        # change, not ambiguous lineage.
        body.extend(f"{key} joined the decision's implementation" for key in sorted(after.keys() - before.keys()))
        after = {key: value for key, value in after.items() if key in before}
    if before.keys() != after.keys():
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        detail = "; ".join(
            part
            for part in (
                f"added {', '.join(added)}" if added else "",
                f"removed {', '.join(removed)}" if removed else "",
            )
            if part
        )
        semantic.append(f"The decision's implementation targets changed ({detail}); lineage is ambiguous")
    else:
        for key in sorted(after):
            if after[key] == UNAVAILABLE:
                unavailable.append(f"Content identity of {key} is unavailable on this revision")
            elif before[key] == UNAVAILABLE:
                unavailable.append(f"{UNANALYZED_AT_ACCEPTANCE}: {key}; reconfirm to record it")
            elif before[key] != after[key]:
                body.append(f"{key} changed since acceptance")

    if semantic:
        return "needs_reconsideration", tuple(semantic)
    if body:
        return "implementation_changed_since_acceptance", tuple(body)
    if unavailable:
        return "unverified", tuple(unavailable)
    return "current", ()


# --------------------------------------------------------------------------- #
# state: disposition from events, validity from fingerprints, queue reasons
# --------------------------------------------------------------------------- #

# A decision written for earlier content still governs the same path's
# targets: that is exactly the implementation whose change needs Reconfirm.
_OWNING = frozenset({"resolved", "renamed", "deleted_file", "stale_content"})
_CLOSED_FEEDBACK = frozenset({"resolved", "obsolete"})
_DISPOSITION: dict[DecisionAction, DecisionDisposition] = {
    "accept": "accepted",
    "reconfirm": "accepted",
    "request_change": "change_requested",
    "defer": "deferred",
    "reopen": "open",
}
TRIGGER_KINDS = ("manual", "new_snapshot", "check_available")


@dataclass(frozen=True)
class DecisionPosition:
    """One producer's current definition of a conflicted decision."""

    producer: str
    record_id: str
    selected_option: str
    option_keys: tuple[str, ...]
    assumptions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DecisionState:
    key: str
    question: str
    options: tuple[tuple[str, str], ...]
    positions: tuple[DecisionPosition, ...]
    author_selected: str
    reason_human_required: str
    target_ids: tuple[str, ...]
    relied_checks: tuple[str, ...]
    contract_scope: tuple[str, ...]
    assumptions: tuple[GuidanceAssumption, ...]
    producer: str
    disposition: DecisionDisposition
    validity: Validity | None
    changed_inputs: tuple[str, ...]
    trigger_kind: str
    trigger_check_key: str
    trigger_note: str
    trigger_fired: bool
    outstanding: bool
    navigable: bool
    withdrawn: bool
    conflict: bool
    stale: bool
    events: tuple[ReviewDecisionEvent, ...]
    scope: DecisionScope | None
    implementation: DecisionImplementation | None
    definition_ref: str
    reason_code: str
    reason_detail: str
    tier: int
    limitations: tuple[str, ...]
    available_actions: tuple[str, ...]

    @property
    def latest_event(self) -> ReviewDecisionEvent | None:
        return self.events[-1] if self.events else None

    @property
    def accepted_option(self) -> str:
        for event in reversed(self.events):
            if event.action in ("accept", "reconfirm"):
                return event.option_key
        return ""


def _owned(definition: GuidanceDefinition, known: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            target_id
            for resolution in definition.resolutions
            if resolution.outcome in _OWNING
            for target_id in resolution.target_ids
            if target_id in known
        )
    )


def _implementation(
    target_ids: Sequence[str], known: Mapping[str, ReviewTarget], units: Mapping[str, ReviewUnit]
) -> DecisionImplementation:
    out: dict[str, str] = {}
    for target_id in target_ids:
        unit_key = known[target_id].unit_key
        unit = units.get(unit_key)
        if unit is None or unit.fingerprint_method == "unknown" or not unit.content_fingerprint:
            out[unit_key] = UNAVAILABLE
        else:
            out[unit_key] = unit.content_fingerprint
    return DecisionImplementation(targets=tuple(sorted(out.items())))


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _trigger_fired(event: ReviewDecisionEvent, checks: Sequence[Mapping[str, Any]], current_revision_id: str) -> bool:
    if event.trigger_kind == "new_snapshot":
        return current_revision_id != event.revision_id
    if event.trigger_kind == "check_available":
        return any(
            event.trigger_check_key in check_keys(row)
            and str(row.get("state") or "") == "current"
            and str(row.get("status") or "").upper() in _OUTCOMES
            for row in checks
        )
    return False


def _actions(
    disposition: DecisionDisposition,
    validity: Validity | None,
    *,
    has_targets: bool,
    historical: bool,
    changed: Sequence[str] = (),
) -> tuple[str, ...]:
    if historical:
        return ("ask",)
    request = ("request_change",) if has_targets else ()
    if disposition == "open":
        return ("accept", *request, "ask", "defer")
    if disposition == "accepted":
        if validity == "implementation_changed_since_acceptance":
            return ("reconfirm", "accept", *request, "ask", "reopen")
        if validity == "unverified" and changed and all(line.startswith(UNANALYZED_AT_ACCEPTANCE) for line in changed):
            # Everything is analyzable now; Reconfirm records what was accepted blind.
            return ("reconfirm", "ask", "reopen")
        if validity == "needs_reconsideration":
            return ("accept", *request, "ask", "defer", "reopen")
        return ("ask", "reopen")
    if disposition == "change_requested":
        return ("accept", "ask", "reopen")
    return ("accept", *request, "ask", "reopen")


def derive_decision_states(
    *,
    guidance: GuidanceView,
    targets: Sequence[ReviewTarget],
    units: Sequence[ReviewUnit],
    events: Sequence[ReviewDecisionEvent],
    checks: Sequence[Mapping[str, Any]],
    contracts: Callable[[str], str],
    annotations: Sequence[Annotation],
    current_revision_id: str,
    historical: bool = False,
) -> tuple[DecisionState, ...]:
    """Every decision the author proposes, plus any with human history.

    Order: author decisions, then conflicted keys, then withdrawn keys, each
    in first-appearance order. A decision whose definition disappeared is
    kept (``withdrawn``) with its history; nothing is silently dropped.
    """

    known = {target.target_id: target for target in targets}
    by_unit = {unit.unit_key: unit for unit in units}
    # Every analyzable unit on this Snapshot: lineage must be unique here.
    analyzable = {
        unit.unit_key: unit.content_fingerprint
        for unit in units
        if unit.fingerprint_method != "unknown" and unit.content_fingerprint
    }
    feedback = {annotation.id: annotation for annotation in annotations}
    history: dict[str, list[ReviewDecisionEvent]] = {}
    for event in events:
        history.setdefault(event.decision_key, []).append(event)

    defined: list[tuple[str, tuple[GuidanceDefinition, ...], bool]] = [
        (definition.key, (definition,), False) for definition in guidance.decisions if definition.decision is not None
    ]
    defined.extend(
        (conflict.key, tuple(item for item in conflict.definitions if item.decision is not None), True)
        for conflict in guidance.conflicts
        if conflict.kind == "decision"
    )
    seen = {key for key, _, _ in defined}

    states: list[DecisionState] = []
    for key, definitions, conflicted in defined:
        decisions = [definition.decision for definition in definitions if definition.decision is not None]
        if not decisions:
            continue
        primary = decisions[-1] if not conflicted else decisions[0]
        target_ids = tuple(
            dict.fromkeys(target_id for definition in definitions for target_id in _owned(definition, known))
        )
        refs = sorted({ref for decision in decisions for ref in decision.contract_scope})
        contract_map = {ref: contracts(ref) for ref in refs}
        scope = (
            scope_from_positions(decisions, contracts=contract_map, checks=checks)
            if conflicted
            else scope_from_decision(primary, contracts=contract_map, checks=checks)
        )
        implementation = _implementation(target_ids, known, by_unit)
        options: dict[str, str] = {}
        for decision in decisions:
            for option in decision.options:
                options.setdefault(option.key, option.label)
        keyed = tuple(history.get(key, ()))
        latest = keyed[-1] if keyed else None
        disposition: DecisionDisposition = "open" if latest is None else _DISPOSITION[latest.action]
        validity: Validity | None = None
        changed: tuple[str, ...] = ()
        fired = False
        reason, detail, tier = "", "", TIER_DONE
        outstanding, navigable = True, True
        if disposition == "open":
            if conflicted:
                reason, detail = "author_guidance_conflict", "Authors disagree; choose an option"
            else:
                reason, detail = "decision_open", "Needs your decision"
            tier = TIER_REVIEW_FIRST
        elif disposition == "accepted" and latest is not None:
            validity, changed = compare(
                _loads(latest.scope_json),
                _loads(latest.implementation_json),
                scope,
                implementation,
                snapshot_units=analyzable,
            )
            if validity == "needs_reconsideration":
                reason, detail, tier = (
                    "decision_scope_changed",
                    "Decision scope changed since acceptance",
                    TIER_RE_REVIEW,
                )
            elif validity == "implementation_changed_since_acceptance":
                reason, detail, tier = (
                    "decision_implementation_changed",
                    "Implementation changed since acceptance; reconfirm",
                    TIER_RE_REVIEW,
                )
            else:
                outstanding, navigable = False, False
        elif disposition == "change_requested" and latest is not None:
            linked = feedback.get(latest.feedback_annotation_id)
            if linked is None or linked.state in _CLOSED_FEEDBACK:
                reason, detail, tier = "decision_open", "Requested change resolved; decide again", TIER_REVIEW_FIRST
            # else the linked request-change is the queue action on its target
        elif disposition == "deferred" and latest is not None:
            fired = _trigger_fired(latest, checks, current_revision_id)
            if fired:
                reason, detail, tier = (
                    "decision_deferred_trigger_fired",
                    "Deferred decision is due",
                    TIER_REVIEW_FIRST,
                )
            else:
                navigable = False
        limitations = tuple(
            f"Contract '{ref}' is not analyzed; only its reference is compared"
            for ref, value in contract_map.items()
            if value.startswith("unanalyzed:")
        )
        if validity == "unverified":
            limitations = (*limitations, *changed)
        states.append(
            DecisionState(
                key=key,
                question=primary.question,
                options=tuple(options.items()),
                positions=(
                    tuple(
                        DecisionPosition(
                            producer=definition.producer,
                            record_id=definition.record_id,
                            selected_option=definition.decision.selected_option,
                            option_keys=tuple(option.key for option in definition.decision.options),
                            assumptions=tuple((item.key, item.value) for item in definition.decision.assumptions),
                        )
                        for definition in definitions
                        if definition.decision is not None
                    )
                    if conflicted
                    else ()
                ),
                author_selected="" if conflicted else primary.selected_option,
                reason_human_required=primary.reason_human_required,
                target_ids=target_ids,
                relied_checks=tuple(dict.fromkeys(ref for decision in decisions for ref in decision.relied_checks)),
                contract_scope=tuple(refs),
                assumptions=primary.assumptions,
                producer=", ".join(dict.fromkeys(definition.producer for definition in definitions)),
                disposition=disposition,
                validity=validity,
                changed_inputs=changed,
                trigger_kind=latest.trigger_kind if latest is not None and disposition == "deferred" else "",
                trigger_check_key=latest.trigger_check_key if latest is not None and disposition == "deferred" else "",
                trigger_note=latest.trigger_note if latest is not None and disposition == "deferred" else "",
                trigger_fired=fired,
                outstanding=outstanding,
                navigable=navigable and bool(reason or disposition == "change_requested"),
                withdrawn=False,
                conflict=conflicted,
                stale=any(definition.stale for definition in definitions),
                events=keyed,
                scope=scope,
                implementation=implementation,
                definition_ref=",".join(sorted(definition.record_id for definition in definitions)),
                reason_code=reason,
                reason_detail=detail,
                tier=tier,
                limitations=limitations,
                available_actions=_actions(
                    disposition, validity, has_targets=bool(target_ids), historical=historical, changed=changed
                ),
            )
        )

    for key, keyed_list in history.items():
        if key in seen:
            continue
        keyed = tuple(keyed_list)
        latest = keyed[-1]
        states.append(
            DecisionState(
                key=key,
                question="",
                options=(),
                positions=(),
                author_selected="",
                reason_human_required="",
                target_ids=(),
                relied_checks=(),
                contract_scope=(),
                assumptions=(),
                producer="",
                disposition=_DISPOSITION[latest.action],
                validity=None,
                changed_inputs=(),
                trigger_kind="",
                trigger_check_key="",
                trigger_note="",
                trigger_fired=False,
                outstanding=False,
                navigable=False,
                withdrawn=True,
                conflict=False,
                stale=False,
                events=keyed,
                scope=None,
                implementation=None,
                definition_ref=latest.definition_ref,
                reason_code="",
                reason_detail="The author no longer proposes this decision",
                tier=TIER_DONE,
                limitations=(),
                available_actions=(),
            )
        )
    return tuple(states)


def decision_reasons(states: Sequence[DecisionState]) -> dict[str, list[QueueReason]]:
    """Queue reasons on the targets of decisions that need action now."""

    out: dict[str, list[QueueReason]] = {}
    for state in states:
        if not (state.reason_code and state.navigable):
            continue
        reason = QueueReason(state.reason_code, f"Decision: {state.question}")
        for target_id in state.target_ids:
            bucket = out.setdefault(target_id, [])
            if reason not in bucket:
                bucket.append(reason)
    return out


def decision_actions(states: Sequence[DecisionState]) -> list[dict[str, Any]]:
    """Review-level queue entries: a decision stays actionable even when all
    of its targets were already read."""

    return [
        {
            "id": f"decision:{state.key}",
            "reason": state.reason_code,
            "title": state.question or state.key,
            "detail": state.reason_detail,
            "item_ids": [],
            "target_ids": list(state.target_ids),
            "actions": list(state.available_actions),
            "tier": state.tier,
        }
        for state in states
        if state.reason_code and state.navigable
    ]


def parse_trigger(raw: str) -> tuple[str, str, str]:
    """``(kind, check_key, note)``; free text becomes ``manual`` with the text kept."""

    text = raw.strip()
    if text in ("", "manual"):
        return "manual", "", ""
    if text == "new_snapshot":
        return "new_snapshot", "", ""
    if text.startswith("check_available:") and text.partition(":")[2].strip():
        return "check_available", text.partition(":")[2].strip(), ""
    return "manual", "", text


def decision_event(
    state: DecisionState,
    *,
    action: DecisionAction,
    reviewer_id: str,
    revision_id: str,
    option_key: str = "",
    reason: str = "",
    trigger: str = "",
    feedback_annotation_id: str = "",
) -> ReviewDecisionEvent:
    """An unsaved event carrying the current scope/implementation documents."""

    scope = state.scope
    implementation = state.implementation
    kind, check_key, note = parse_trigger(trigger) if action == "defer" else ("", "", "")
    return ReviewDecisionEvent(
        id="",
        review_id="",
        decision_key=state.key,
        reviewer_id=reviewer_id,
        revision_id=revision_id,
        action=action,
        disposition=_DISPOSITION[action],
        option_key=option_key,
        definition_ref=state.definition_ref,
        semantic_fingerprint=semantic_fingerprint(scope) if scope is not None else "",
        implementation_fingerprint=implementation_fingerprint(implementation) if implementation is not None else "",
        scope_json=canonical_json(scope_document(scope)) if scope is not None else "{}",
        implementation_json=(
            canonical_json(implementation_document(implementation)) if implementation is not None else "{}"
        ),
        reason=reason,
        trigger_kind=kind,
        trigger_check_key=check_key,
        trigger_note=note,
        feedback_annotation_id=feedback_annotation_id,
    )


__all__ = [
    "DECISION_FINGERPRINT_VERSION",
    "TRIGGER_KINDS",
    "UNANALYZED_AT_ACCEPTANCE",
    "UNOBSERVED",
    "DecisionImplementation",
    "DecisionPosition",
    "DecisionScope",
    "DecisionState",
    "Validity",
    "canonical_json",
    "check_keys",
    "check_outcomes",
    "compare",
    "decision_actions",
    "decision_event",
    "decision_reasons",
    "derive_decision_states",
    "implementation_document",
    "implementation_fingerprint",
    "observed_outcome",
    "parse_trigger",
    "rebind_lineage",
    "scope_document",
    "scope_from_decision",
    "scope_from_positions",
    "semantic_fingerprint",
]
