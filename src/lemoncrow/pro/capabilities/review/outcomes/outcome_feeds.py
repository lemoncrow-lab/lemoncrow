"""Escaped-defect feed from production outcomes (P15-OBS item 8) for Phase 13 analytics and Phase 11.

An *escaped defect* is a production problem of a merged Change that review
did not stop: a prediction production contradicted, or a production
deployment of the change that failed or was rolled back. Each carries the
depth (effort preset) and the checks the merged Snapshot's effective run plan
ran, so Phase 13 can report escapes by depth and by check. Incident links are
correlation, not cause, and are never counted.

Contract for P13-ANALYTICS (keep stable):

``escaped_defects(store, session) -> tuple[EscapedDefect, ...]``
    one merged Review's escapes.
``store_escaped_defects(store, *, limit) -> tuple[EscapedDefect, ...]``
    every escape recorded in one Review store (bounded scan).
``escapes_by_depth_and_check(defects) -> dict``
    ``{"total", "by_depth": {depth: n}, "by_check": {check id: n}, "by_kind": {kind: n}}``.

Phase 11 (item 16) reads the same facts through ``track_record.change_outcome``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import DEPLOYMENT_ROW_KIND, DeploymentLedger
from lemoncrow.pro.capabilities.review.outcomes.deployments import ATTENTION_STATES
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import (
    OBSERVATION_ROW_KIND,
    SCAN_BATCH,
    production_records,
    summary_source_ref,
)

REVERT_RELATION: Final[str] = "reverted_by"


@dataclass(frozen=True, slots=True)
class EscapedDefect:
    review_id: str
    revision_id: str
    merged_commit: str
    environment: str
    kind: str
    """``contradicted``, ``deployment_failed`` or ``deployment_rolled_back``."""
    subject: str
    target_kind: str = ""
    category: str = ""
    signals: tuple[str, ...] = ()
    depth: str = ""
    """The merged revision's effort preset (``""`` when no run plan was stored)."""
    checks: tuple[str, ...] = ()
    """Plan entries that ran on the merged revision."""
    observed_at: str = ""
    reverted: bool = False
    evidence_ids: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "merged_commit": self.merged_commit,
            "environment": self.environment,
            "kind": self.kind,
            "subject": self.subject,
            "target_kind": self.target_kind,
            "category": self.category,
            "signals": list(self.signals),
            "depth": self.depth,
            "checks": list(self.checks),
            "observed_at": self.observed_at,
            "reverted": self.reverted,
            "evidence_ids": list(self.evidence_ids),
        }


def _plan_facts(store: Any, revision_id: str) -> tuple[str, tuple[str, ...]]:
    from lemoncrow.pro.capabilities.review.run_plan import load_run_plan

    try:
        plan = load_run_plan(store, revision_id)
    except ValueError:
        return "", ()
    if plan is None:
        return "", ()
    depth = str(plan.selection.get("base") or plan.selection.get("name") or "")
    return depth, tuple(sorted(entry.id for entry in plan.entries if entry.status == "run"))


def escaped_defects(store: Any, session: Any) -> tuple[EscapedDefect, ...]:
    reverted = any(item.relation == REVERT_RELATION for item in store.list_relationships(session.id, kind="change"))
    plans: dict[str, tuple[str, tuple[str, ...]]] = {}

    def plan_for(revision_id: str) -> tuple[str, tuple[str, ...]]:
        if revision_id not in plans:
            plans[revision_id] = _plan_facts(store, revision_id)
        return plans[revision_id]

    out: list[EscapedDefect] = []
    for record in sorted(production_records(store, session.id), key=lambda item: item.scope):
        categories = {item.key: (item.target_kind, item.category) for item in record.predictions}
        summary = store.get_evidence_by_source(session.id, "external", summary_source_ref(record))
        depth, checks = plan_for(record.revision_id)
        for target in record.outcomes:
            if target.state != "contradicted":
                continue
            target_kind, category = categories.get(target.key, ("", ""))
            out.append(
                EscapedDefect(
                    review_id=session.id,
                    revision_id=record.revision_id,
                    merged_commit=record.merged_commit,
                    environment=record.environment,
                    kind="contradicted",
                    subject=target.subject,
                    target_kind=target_kind,
                    category=category,
                    signals=target.signals,
                    depth=depth,
                    checks=checks,
                    observed_at=record.contradicted_at.get(target.key, ""),
                    reverted=reverted,
                    evidence_ids=() if summary is None else (summary.id,),
                )
            )
    for _key, (_stamp, deployed) in sorted(DeploymentLedger(store).records(session.id).items()):
        outcome = deployed.outcome()
        if outcome is None or not outcome.production or outcome.state not in ATTENTION_STATES:
            continue
        depth, checks = plan_for(deployed.revision_id)
        out.append(
            EscapedDefect(
                review_id=session.id,
                revision_id=deployed.revision_id,
                merged_commit=deployed.merged.sha,
                environment=deployed.environment,
                kind="deployment_failed" if outcome.state == "failed" else "deployment_rolled_back",
                subject=outcome.deciding_id,
                depth=depth,
                checks=checks,
                observed_at=outcome.since,
                reverted=reverted or outcome.state == "rolled_back",
            )
        )
    return tuple(out)


def store_escaped_defects(store: Any, *, limit: int = SCAN_BATCH) -> tuple[EscapedDefect, ...]:
    """Every escape in one Review store; reads at most ``limit`` rows of each ledger (oldest first)."""

    review_ids: dict[str, None] = {}
    for kind in (OBSERVATION_ROW_KIND, DEPLOYMENT_ROW_KIND):
        for row in store.derived_results_by_kind(kind, limit=limit):
            review_ids.setdefault(row.review_id, None)
    out: list[EscapedDefect] = []
    for review_id in review_ids:
        session = store.get_session(review_id)
        if session is not None:
            out.extend(escaped_defects(store, session))
    return tuple(out)


def escapes_by_depth_and_check(defects: Iterable[EscapedDefect]) -> dict[str, Any]:
    items = list(defects)
    by_check: Counter[str] = Counter()
    for item in items:
        by_check.update(item.checks)
    return {
        "total": len(items),
        "by_depth": dict(sorted(Counter(item.depth or "unknown" for item in items).items())),
        "by_check": dict(sorted(by_check.items())),
        "by_kind": dict(sorted(Counter(item.kind for item in items).items())),
    }


__all__ = ["EscapedDefect", "escaped_defects", "escapes_by_depth_and_check", "store_escaped_defects"]
