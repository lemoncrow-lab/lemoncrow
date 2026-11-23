"""Incremental check execution per Snapshot (Phase 9R R6, item 22).

A head run is carried forward to a later Snapshot of the same Review when
nothing it depends on changed: the same recipe digest, trusted policy,
executor class, scope (cwd and selected tests), attempt number and input
digest, run in the same executor environment. The input digest (``test_impact.RecipeSelection.input_digest``)
covers the base commit and the head content of every changed file inside the
closure of the tests that run, or the whole changed tree when no narrower
closure is known. Changed inputs produce another digest, so the run executes
again. No digest (an incomplete or unreadable change list) never carries.

Rows live in ``review_derived`` (kind ``check_carry``) on the producing
Snapshot as a pointer to its attempt evidence and go with that Snapshot. The
carried record is copied onto the current Snapshot with ``reused_from``
provenance ("reused from Snapshot N"); its typed execution still names the
Snapshot that actually ran it. Each row also stores its key components, so a
later miss names the component that changed (:func:`explain_carry`, in the
:mod:`validity` vocabulary).

Environment (Trust T3 item 1): the executor environment the current run would
record (``EnvironmentFingerprint.executor_digest``: the image, the kernel
release, each executable the recipe runs, resolved through the sandbox's PATH
and mounts with its path, version output and content digest, the toolchain and
the sandbox) must be known before the lookup and equal the one the earlier
run's own execution record states. Any unknown component makes the whole
environment unknown, and an unknown environment never carries: a hosted worker
reports its environment only with its result, so hosted head runs execute
again until workers report it ahead of dispatch.

Kill switch (Trust Gate T-J item 2): ``LEMONCROW_REVIEW_CARRY=off`` carries
nothing; every plain head run executes again (``carry_disabled``). Rows are
still recorded while it is off, so turning it back on carries again without
a data migration. A carried copy already on a Snapshot is not replayed while
carry is off or once the environment changed or is unknown: the check runs
fresh. Same-Snapshot replay of a run that executed on that Snapshot, and
shared base runs, are not carry.

lc-debt: only plain head ``run`` attempts carry; adequacy probes and autofix runs
always execute again. Upgrade: carry probes keyed by their overlay digest.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE
from lemoncrow.pro.capabilities.review.store import DerivedResult
from lemoncrow.pro.environment.execution import ExecutionResult

CARRY_KIND: Final[str] = "check_carry"
_STAMP: Final[str] = "v1"
CARRY_FLAG: Final[str] = "LEMONCROW_REVIEW_CARRY"


def carry_enabled() -> bool:
    """The carry flag and kill switch: on when unset or ``on``; any other value is off."""

    return os.environ.get(CARRY_FLAG, "").strip().lower() in ("", "on")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CarryKey:
    recipe_digest: str
    policy_digest: str
    executor_class: str
    scope_digest: str
    attempt: int
    input_digest: str
    #: The check this key belongs to (recipe id). Explanatory: it names which earlier
    #: run to compare against when no exact key matches; never part of ``scope_key``.
    check: str = ""
    #: The executor environment the current run would record, known before dispatch
    #: (``""``: unknown, so nothing carries). It is compared with the earlier run's
    #: own execution record, which is the authority for that run's environment, so
    #: it is never part of ``scope_key`` or the stored components.
    environment: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.recipe_digest and self.executor_class and self.input_digest)

    @property
    def scope_key(self) -> str:
        return "carry:" + _digest(
            {
                "recipe": self.recipe_digest,
                "policy": self.policy_digest,
                "executor": self.executor_class,
                "scope": self.scope_digest,
                "attempt": self.attempt,
                "inputs": self.input_digest,
            }
        )

    def components(self) -> dict[str, Any]:
        """The key as stored on a carry row, so a later miss can name what changed."""

        return {
            "check": self.check,
            "policy": self.policy_digest,
            "recipe": self.recipe_digest,
            "executor": self.executor_class,
            "scope": self.scope_digest,
            "attempt": self.attempt,
            "inputs": self.input_digest,
        }


#: Carry-key component -> :mod:`validity` reason code, in explanation order.
_COMPONENT_REASONS: Final[tuple[tuple[str, str], ...]] = (
    ("policy", "carry_policy_changed"),
    ("recipe", "carry_recipe_changed"),
    ("executor", "carry_executor_changed"),
    ("scope", "carry_scope_changed"),
    ("attempt", "carry_attempt_differs"),
    ("inputs", "carry_inputs_changed"),
)


@dataclass(frozen=True, slots=True)
class Carried:
    record: dict[str, Any]
    revision_id: str
    revision_number: int
    evidence_id: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class CarryDecision:
    """The carry lookup's outcome and its reason chain (:mod:`validity` codes).

    ``carried`` is set exactly when ``reasons == ("carry_exact_key",)``; otherwise
    the reasons say why the run executes again, most decisive first.
    """

    carried: Carried | None
    reasons: tuple[str, ...]
    origin_revision_id: str = ""
    evidence_id: str = ""


def _refusal(record: Mapping[str, Any], key: CarryKey, *, side: str, variant: str, overlay: bool) -> tuple[str, str]:
    """``(reason code, text)`` why this attempt may not be carried forward, or ``("", "")``."""

    if side != "new" or variant != "run" or overlay:
        return "carry_not_plain_run", "only a plain head run is carried forward"
    if not key.usable:
        return "carry_no_input_digest", "no input digest"
    if str(record.get("status") or "") not in {"PASS", "FAIL"}:
        return "carry_evidence_unverifiable", "only an executed run is carried forward"
    data = record.get("data")
    if not isinstance(data, Mapping) or data.get("shared_from"):
        return "carry_evidence_unverifiable", "the run has no data"
    if str(data.get("recipe_digest") or "") != key.recipe_digest or int(data.get("attempt") or 0) != key.attempt:
        return "carry_evidence_unverifiable", "the run is not this recipe attempt"
    if str(data.get("input_digest") or "") != key.input_digest:
        return "carry_inputs_changed", "the run's input digest differs"
    return "", ""


def carry_refusal(record: Mapping[str, Any], key: CarryKey, *, side: str, variant: str, overlay: bool) -> str:
    """Why this attempt may not be carried forward, or ``""``."""

    return _refusal(record, key, side=side, variant=variant, overlay=overlay)[1]


def record_carry(
    store: Any,
    *,
    revision: Any,
    key: CarryKey,
    evidence: Any,
    record: Mapping[str, Any],
    side: str,
    variant: str,
    overlay: bool,
    replace: bool = False,
) -> bool:
    """Index one head run for later Snapshots of the same Review.

    The first run indexed for a Snapshot and key stays, unless ``replace``: an
    explicit fresh run supersedes a carried or earlier one.
    """

    if carry_refusal(record, key, side=side, variant=variant, overlay=overlay):
        return False
    data = record.get("data")
    origin = data.get("reused_from") if isinstance(data, Mapping) else None
    # A carried copy points at the Snapshot that actually ran it.
    source = origin if isinstance(origin, Mapping) else {}
    row = DerivedResult(
        review_id=revision.review_id,
        revision_id=revision.id,
        kind=CARRY_KIND,
        scope_key=key.scope_key,
        stamp=_STAMP,
        payload={
            "evidence_id": evidence.id,
            "content_hash": evidence.content_hash,
            "input_digest": key.input_digest,
            "revision_number": int(source.get("snapshot") or revision.revision_number),
            "origin_revision_id": str(source.get("revision_id") or revision.id),
            "duration_ms": int(source.get("duration_ms") or record.get("duration_ms") or 0),
            "key": key.components(),
        },
    )
    if replace:
        store.put_derived_results([row])
    else:
        store.insert_derived_result(row)
    return True


def record_environment(record: Mapping[str, Any]) -> str:
    """The executor environment a run record's typed execution states, or ``""``."""

    data = record.get("data")
    execution = data.get("execution") if isinstance(data, Mapping) else None
    if not isinstance(execution, Mapping):
        return ""
    try:
        return ExecutionResult.from_wire(execution).fingerprint.executor_digest
    except (TypeError, ValueError):
        return ""


def _read(store: Any, evidence: Any) -> dict[str, Any] | None:
    payload = store.read_evidence_artifact(evidence)
    if payload is None or hashlib.sha256(payload).hexdigest() != evidence.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _differing(stored: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(code for name, code in _COMPONENT_REASONS if stored.get(name) != current[name])


def _miss_reasons(
    key: CarryKey,
    earlier: list[Any],
    rows: Mapping[tuple[str, str], tuple[str, dict[str, Any]]],
    exact_refusals: list[str],
) -> tuple[str, ...]:
    """Why no earlier run carried: compare against this check's newest earlier carry row."""

    if exact_refusals:
        # A row with exactly this key exists but its run did not verify.
        return tuple(dict.fromkeys(exact_refusals))
    by_revision: dict[str, list[dict[str, Any]]] = {}
    for (revision_id, _scope), (stamp, payload) in rows.items():
        if stamp == _STAMP:
            by_revision.setdefault(revision_id, []).append(payload)
    unrecorded = False
    current = key.components()
    for item in earlier:
        candidates: list[dict[str, Any]] = []
        for payload in sorted(by_revision.get(item.id, ()), key=lambda row: json.dumps(row, sort_keys=True)):
            stored = payload.get("key")
            if not isinstance(stored, Mapping):
                unrecorded = True
                continue
            if key.check and stored.get("check") == key.check:
                candidates.append(dict(stored))
        if not candidates:
            continue
        # Prefer the same attempt, then the fewest differing components.
        best = min(
            candidates,
            key=lambda stored: (stored.get("attempt") != key.attempt, len(_differing(stored, current))),
        )
        return _differing(best, current) or ("carry_evidence_unverifiable",)
    return ("carry_prior_key_unrecorded",) if unrecorded else ("carry_no_prior_run",)


def explain_carry(store: Any, revision: Any, key: CarryKey) -> CarryDecision:
    """The newest earlier Snapshot's verified run with this exact key, with the reason chain.

    The run must also have recorded the executor environment ``key.environment``
    names; an unknown current environment carries nothing and reads no rows.
    One read of the earlier Snapshots' carry rows serves both the lookup and,
    on a miss, the explanation: the newest earlier row of the same check (same
    attempt preferred) names each key component that changed. With carry
    turned off (:func:`carry_enabled`) nothing is read and nothing carries.
    """

    if not carry_enabled():
        return CarryDecision(None, ("carry_disabled",))
    if not key.usable:
        return CarryDecision(None, ("carry_no_input_digest",))
    earlier = sorted(
        (
            item
            for item in store.list_revisions(revision.review_id)
            if item.revision_number < revision.revision_number and item.id != revision.id
        ),
        key=lambda item: (item.revision_number, item.created_at, item.id),
        reverse=True,
    )
    if not earlier:
        return CarryDecision(None, ("carry_no_earlier_snapshot",))
    if not key.environment:
        return CarryDecision(None, ("carry_environment_unknown",))
    rows = store.derived_results(CARRY_KIND, [item.id for item in earlier])
    refusals: list[str] = []
    for item in earlier:
        found = rows.get((item.id, key.scope_key))
        if found is None or found[0] != _STAMP:
            continue
        payload = found[1]
        if payload.get("input_digest") != key.input_digest:
            refusals.append("carry_inputs_changed")
            continue
        evidence = store.get_evidence(str(payload.get("evidence_id") or ""))
        if (
            evidence is None
            or evidence.review_id != revision.review_id
            or evidence.revision_id != item.id
            or evidence.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE
            or evidence.content_hash != payload.get("content_hash")
        ):
            refusals.append("carry_evidence_unverifiable")
            continue
        record = _read(store, evidence)
        if record is None:
            refusals.append("carry_evidence_unverifiable")
            continue
        code, _text = _refusal(record, key, side="new", variant="run", overlay=False)
        if code:
            refusals.append(code)
            continue
        environment = record_environment(record)
        if environment != key.environment:
            refusals.append("carry_environment_changed" if environment else "carry_environment_unknown")
            continue
        origin = str(payload.get("origin_revision_id") or item.id)
        return CarryDecision(
            Carried(
                record=record,
                revision_id=origin,
                revision_number=int(payload.get("revision_number") or item.revision_number),
                evidence_id=evidence.id,
                duration_ms=int(payload.get("duration_ms") or 0),
            ),
            ("carry_exact_key",),
            origin_revision_id=origin,
            evidence_id=evidence.id,
        )
    return CarryDecision(None, _miss_reasons(key, earlier, rows, refusals))


def find_carry(store: Any, revision: Any, key: CarryKey) -> Carried | None:
    """The newest earlier Snapshot's verified run with this exact key, or ``None``."""

    return explain_carry(store, revision, key).carried


__all__ = [
    "CARRY_FLAG",
    "CARRY_KIND",
    "Carried",
    "CarryDecision",
    "CarryKey",
    "carry_enabled",
    "carry_refusal",
    "explain_carry",
    "find_carry",
    "record_carry",
    "record_environment",
]
