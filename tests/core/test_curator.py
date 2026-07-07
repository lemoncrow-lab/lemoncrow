"""Tests for explicit Playbook lifecycle curation."""

from __future__ import annotations

from atelier.core.foundation.curator import apply_curation, curate
from atelier.core.foundation.models import BlockTier, Playbook


def _block(
    block_id: str,
    *,
    tier: BlockTier = "e2",
    usage: int = 0,
    success: int = 0,
    failure: int = 0,
) -> Playbook:
    return Playbook(
        id=block_id,
        title=block_id,
        domain="coding",
        situation="sit",
        procedure=["do the thing"],
        tier=tier,
        usage_count=usage,
        success_count=success,
        failure_count=failure,
    )


def _action_for(report: object, block_id: str) -> str:
    for decision in report.decisions:  # type: ignore[attr-defined]
        if decision.block_id == block_id:
            return decision.action
    raise AssertionError(f"no decision for {block_id}")


def test_insufficient_evidence_is_kept() -> None:
    report = curate([_block("b", usage=2, success=2, failure=0)])
    assert _action_for(report, "b") == "keep"


def test_consistent_winner_is_promoted() -> None:
    block = _block("b", tier="e2", usage=10, success=9, failure=1)
    report = curate([block])
    decision = report.decisions[0]
    assert decision.action == "promote"
    assert decision.tier_from == "e2"
    assert decision.tier_to == "e3"


def test_promote_caps_at_top_tier() -> None:
    report = curate([_block("b", tier="e3", usage=10, success=10, failure=0)])
    assert _action_for(report, "b") == "keep"


def test_persistent_failure_is_removed() -> None:
    report = curate([_block("b", usage=8, success=1, failure=7)])
    assert _action_for(report, "b") == "remove"


def test_mediocre_block_is_demoted() -> None:
    # rate 0.4 (< demote 0.5) but failures below remove threshold band
    block = _block("b", tier="e3", usage=10, success=4, failure=6)
    report = curate([block])
    decision = report.decisions[0]
    # 0.4 <= REMOVE_SUCCESS_RATE is False (0.4 > 0.25) so it demotes, not removes
    assert decision.action == "demote"
    assert decision.tier_to == "e2"


class _FakeStore:
    def __init__(self) -> None:
        self.upserts: list[Playbook] = []
        self.deletes: list[str] = []

    def upsert_block(self, block: Playbook, *, write_markdown: bool = True) -> None:
        self.upserts.append(block)

    def delete_block(self, block_id: str) -> bool:
        self.deletes.append(block_id)
        return True


def test_apply_curation_writes_and_deletes() -> None:
    promote = _block("win", tier="e2", usage=10, success=9, failure=1)
    remove = _block("lose", usage=8, success=1, failure=7)
    keep = _block("meh", usage=1, success=1, failure=0)
    store = _FakeStore()

    counts = apply_curation(store, curate([promote, remove, keep]))

    assert counts == {"promote": 1, "demote": 0, "remove": 1}
    assert store.deletes == ["lose"]
    assert len(store.upserts) == 1
    assert store.upserts[0].id == "win"
    assert store.upserts[0].tier == "e3"
