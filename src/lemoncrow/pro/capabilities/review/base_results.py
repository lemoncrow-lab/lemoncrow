"""Shared base results and base-commit indexes (Phase 9R R6, item 21).

A check recipe's base (old) side is the trusted base commit. Two Reviews on
the same base commit, environment fingerprint and recipe digest run the same
command on the same bytes, so the base run executes once and is reused:

* key: base commit, recipe digest, environment fingerprint (the full
  ``envfp`` digest), executor class, trusted policy digest, scope (cwd and
  selected tests) and attempt number;
* rows live in ``review_derived`` (kind ``base_result``) on the Snapshot that
  produced the run, as a pointer to that run's evidence; the Snapshot's
  deletion cascade and a least-recently-used cap (:data:`MAX_SHARED_BASE_RESULTS`)
  bound them;
* only a trusted base run writes one (:func:`publish_refusal`): the old side,
  an unmodified ``run`` attempt, a typed execution whose subject is the old
  side of exactly that base commit, the ``base`` cache namespace and the
  key's fingerprint. A head run, a probe with a head overlay, a copy of a
  reused result, or an untyped record is refused. Every read re-verifies the
  same against the stored record and its content hash;
* hosted stores run every query inside the tenant repository's row-level
  security scope, so a hosted lookup never reads another tenant's row;
* kill switch (Trust Gate T-J item 2): ``LEMONCROW_REVIEW_SHARED_BASE=off``
  makes every lookup miss without reading a row, so each base side executes.
  Rows are still published, so turning it back on reuses them with no data
  migration.

The lookup fingerprint is the head attempt's (head runs first): equal
fingerprints mean the same image, toolchain, lockfile and execution recipe.
A head that changed the lockfile has a different fingerprint, so its base runs
again (fail safe; such a pair is not comparable anyway).

The trusted base's test-impact map (``test_impact.ImpactMap``) is the other
base-commit index kept here: built once per base commit from the trusted base
only, stored as kind ``test_impact_map`` and capped the same way.
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
from lemoncrow.pro.capabilities.review.test_impact import IMPACT_MAP_KIND, IMPACT_MAP_VERSION, ImpactMap
from lemoncrow.pro.environment.execution import ExecutionResult

BASE_RESULT_KIND: Final[str] = "base_result"
_STAMP: Final[str] = "v1"
_MAP_STAMP: Final[str] = f"v{IMPACT_MAP_VERSION}"
#: lc-debt: one LRU cap per store (hosted: per tenant repository), evicted on write
#: by a full scan of the kind. Upgrade: a size-aware cap and an indexed eviction.
MAX_SHARED_BASE_RESULTS: Final[int] = 512
MAX_IMPACT_MAPS: Final[int] = 16
SHARED_BASE_FLAG: Final[str] = "LEMONCROW_REVIEW_SHARED_BASE"


def shared_base_enabled() -> bool:
    """The shared-base flag and kill switch: on when unset or ``on``; any other value is off."""

    return os.environ.get(SHARED_BASE_FLAG, "").strip().lower() in ("", "on")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SharedBaseKey:
    """What makes two base runs interchangeable."""

    base_sha: str
    recipe_digest: str
    environment: str
    executor_class: str
    policy_digest: str
    scope_digest: str
    attempt: int

    @property
    def scope_key(self) -> str:
        return "base:" + _digest(
            {
                "base": self.base_sha,
                "recipe": self.recipe_digest,
                "environment": self.environment,
                "executor": self.executor_class,
                "policy": self.policy_digest,
                "scope": self.scope_digest,
                "attempt": self.attempt,
            }
        )

    @property
    def usable(self) -> bool:
        return bool(self.base_sha and self.recipe_digest and self.environment and self.executor_class)


@dataclass(frozen=True, slots=True)
class SharedBase:
    """A verified shared base run: its record and where it ran."""

    record: dict[str, Any]
    review_id: str
    revision_id: str
    evidence_id: str
    duration_ms: int


def _execution(record: Mapping[str, Any]) -> ExecutionResult | None:
    data = record.get("data")
    raw = data.get("execution") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        return ExecutionResult.from_wire(raw)
    except (TypeError, ValueError):
        return None


def publish_refusal(
    record: Mapping[str, Any],
    key: SharedBaseKey,
    *,
    revision_base_sha: str,
    side: str,
    variant: str,
    overlay: bool,
) -> str:
    """Why this attempt may not become a shared base result, or ``""`` when it may."""

    if side != "old" or str(record.get("side") or "") != "old":
        return "only a base-side run is shared"
    if variant != "run" or overlay:
        return "only an unmodified base run is shared (no probe, overlay or fix)"
    if not key.usable:
        return "the base commit, recipe, executor or environment fingerprint is unknown"
    if not revision_base_sha or revision_base_sha != key.base_sha:
        return "the run is not bound to this base commit"
    if str(record.get("status") or "") not in {"PASS", "FAIL"}:
        return "only an executed run is shared"
    data = record.get("data")
    if not isinstance(data, Mapping):
        return "the run has no data"
    if data.get("reused_from") or data.get("shared_from"):
        return "a reused result is never re-shared"
    if str(data.get("variant") or "") != "run" or int(data.get("attempt") or 0) != key.attempt:
        return "the run is not this attempt"
    if str(data.get("recipe_digest") or "") != key.recipe_digest:
        return "the run used another recipe digest"
    execution = _execution(record)
    if execution is None:
        return "only a typed execution result is shared"
    subject = execution.subject
    if subject.side != "old" or subject.overlay or subject.snapshot_identity != key.base_sha:
        return "the execution did not run the base commit unmodified"
    if execution.cache_namespace != "base":
        return f"the execution wrote the {execution.cache_namespace} cache namespace, not base"
    if execution.environment_fingerprint != key.environment:
        return "the execution's environment fingerprint differs"
    if execution.executor_class.value != key.executor_class:
        return "the execution ran on another executor class"
    return ""


def publish_shared_base(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    revision_base_sha: str,
    key: SharedBaseKey,
    evidence: Any,
    record: Mapping[str, Any],
    side: str,
    variant: str,
    overlay: bool,
    replace: bool = False,
) -> str:
    """Share one trusted base run; returns the refusal reason (``""`` when shared).

    The first run a Snapshot publishes for a key stays its shared row, unless
    ``replace`` (an explicit fresh run): the newest executed base result then
    supersedes this Snapshot's row and, as the most recently used, is preferred.
    """

    refusal = publish_refusal(
        record, key, revision_base_sha=revision_base_sha, side=side, variant=variant, overlay=overlay
    )
    if refusal:
        return refusal
    if evidence.review_id != review_id or evidence.revision_id != revision_id or not evidence.content_hash:
        return "the evidence is not this revision's"
    row = DerivedResult(
        review_id=review_id,
        revision_id=revision_id,
        kind=BASE_RESULT_KIND,
        scope_key=key.scope_key,
        stamp=_STAMP,
        payload={
            "evidence_id": evidence.id,
            "content_hash": evidence.content_hash,
            "base_sha": key.base_sha,
            "recipe_digest": key.recipe_digest,
            "environment": key.environment,
            "executor": key.executor_class,
            "attempt": key.attempt,
            "duration_ms": int(record.get("duration_ms") or 0),
        },
    )
    if replace:
        store.put_derived_results([row])
    else:
        store.insert_derived_result(row)
    store.evict_derived_results(BASE_RESULT_KIND, keep=MAX_SHARED_BASE_RESULTS)
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


def find_shared_base(store: Any, key: SharedBaseKey) -> SharedBase | None:
    """The verified shared base run for *key*, most recently used first, or ``None``.

    ``None`` without reading anything while :func:`shared_base_enabled` is off.
    """

    if not shared_base_enabled() or not key.usable:
        return None
    for row in store.derived_rows_by_scope(BASE_RESULT_KIND, key.scope_key):
        payload = row.payload
        if (
            row.stamp != _STAMP
            or payload.get("base_sha") != key.base_sha
            or payload.get("recipe_digest") != key.recipe_digest
            or payload.get("environment") != key.environment
            or payload.get("executor") != key.executor_class
            or payload.get("attempt") != key.attempt
        ):
            continue
        evidence = store.get_evidence(str(payload.get("evidence_id") or ""))
        if (
            evidence is None
            or evidence.review_id != row.review_id
            or evidence.revision_id != row.revision_id
            or evidence.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE
            or evidence.content_hash != payload.get("content_hash")
        ):
            continue
        record = _read(store, evidence)
        if record is None:
            continue
        producer = store.get_revision(row.revision_id)
        refusal = publish_refusal(
            record,
            key,
            revision_base_sha=str(getattr(producer, "base_sha", "") or ""),
            side="old",
            variant="run",
            overlay=False,
        )
        if refusal:
            continue
        store.touch_derived_result(row.revision_id, BASE_RESULT_KIND, key.scope_key)
        return SharedBase(
            record=record,
            review_id=row.review_id,
            revision_id=row.revision_id,
            evidence_id=evidence.id,
            duration_ms=int(record.get("duration_ms") or 0),
        )
    return None


# --------------------------------------------------------------------------- #
# the trusted base's test-impact map
# --------------------------------------------------------------------------- #


def impact_scope(base_sha: str) -> str:
    return f"impact:{IMPACT_MAP_VERSION}:{base_sha}"


def load_impact_map(store: Any, base_sha: str) -> ImpactMap | None:
    """The stored map for this base commit (any Review in the store), or ``None``."""

    if not base_sha:
        return None
    for row in store.derived_rows_by_scope(IMPACT_MAP_KIND, impact_scope(base_sha)):
        if row.stamp != _MAP_STAMP:
            continue
        try:
            impact = ImpactMap.from_wire(row.payload)
        except (TypeError, ValueError, KeyError):
            continue
        if impact.base_sha != base_sha or row.payload.get("digest") != impact.digest:
            continue
        store.touch_derived_result(row.revision_id, IMPACT_MAP_KIND, impact_scope(base_sha))
        return impact
    return None


def store_impact_map(store: Any, *, review_id: str, revision_id: str, impact: ImpactMap) -> None:
    """Keep a map built from the trusted base; the first writer for a base commit wins."""

    if not impact.base_sha:
        return
    store.insert_derived_result(
        DerivedResult(
            review_id=review_id,
            revision_id=revision_id,
            kind=IMPACT_MAP_KIND,
            scope_key=impact_scope(impact.base_sha),
            stamp=_MAP_STAMP,
            payload={**impact.to_wire(), "digest": impact.digest},
        )
    )
    store.evict_derived_results(IMPACT_MAP_KIND, keep=MAX_IMPACT_MAPS)


__all__ = [
    "BASE_RESULT_KIND",
    "MAX_IMPACT_MAPS",
    "MAX_SHARED_BASE_RESULTS",
    "SHARED_BASE_FLAG",
    "SharedBase",
    "SharedBaseKey",
    "find_shared_base",
    "impact_scope",
    "load_impact_map",
    "publish_refusal",
    "publish_shared_base",
    "shared_base_enabled",
    "store_impact_map",
]
