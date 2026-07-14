"""Regression tests for the rescue evidence-floor retrieval path.

Guards the fix for rescue returning the canned fallback on every call: the
injection pipeline's keyword-trigger gate (match_score > 0.40) dropped even
the top BM25 match, so `rescue` had a brain it never used. Rescue now ranks
by raw bm25 via ``rescue_candidates`` and applies an evidence floor.
"""

from __future__ import annotations

import pytest

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.pro.capabilities.context_reuse.capability import ContextReuseCapability


def _block(block_id: str, title: str, *, situation: str, procedure: list[str], triggers: list[str]) -> Playbook:
    return Playbook(
        id=block_id,
        title=title,
        domain="debugging",
        situation=situation,
        procedure=procedure,
        triggers=triggers,
    )


@pytest.fixture
def capability(monkeypatch: pytest.MonkeyPatch) -> ContextReuseCapability:
    blocks = [
        _block(
            "repeated-agent-failure-loop",
            "Repeated Agent Failure Loop",
            situation=(
                "The same command or test failed twice with the same error signature "
                "and the agent keeps retrying the failing command in a loop."
            ),
            procedure=[
                "Stop. Do not run the failing command again.",
                "Summarize the invariant being fought in one sentence.",
                "Enumerate the approaches already tried and their failure modes.",
            ],
            triggers=["test failed", "same error", "retry", "loop", "stuck"],
        ),
        _block(
            "smallest-reviewable-change",
            "Smallest Reviewable Change",
            situation="A large edit is being planned without an obvious first reviewable slice.",
            procedure=[
                "Limit the first edit to the smallest slice that can test the current hypothesis.",
                "Keep unrelated cleanup and reformatting out of the first patch.",
            ],
            triggers=["large diff", "big change"],
        ),
        _block(
            "read-after-write-verification",
            "Read-After-Write Verification",
            situation="A state mutation was applied and needs verification before continuing.",
            procedure=[
                "Capture pre-change state when recovery is relevant.",
                "Apply the mutation, then read back the changed state and compare.",
            ],
            triggers=["state change", "migration"],
        ),
        _block(
            "fix-authoritative-source-first",
            "Fix Authoritative Source First",
            situation="A generated or derived artifact shows an incorrect value or behavior.",
            procedure=[
                "Locate the authoritative source for the incorrect value or behavior.",
                "Fix the issue at that source and regenerate the artifact.",
            ],
            triggers=["generated file", "derived artifact"],
        ),
    ]
    cap = ContextReuseCapability.__new__(ContextReuseCapability)
    monkeypatch.setattr(cap, "_all_active_blocks", lambda: blocks, raising=False)
    return cap


def test_rescue_candidates_ranks_failure_loop_block_first(capability: ContextReuseCapability) -> None:
    scored = capability.rescue_candidates(
        task="fix import error in test suite",
        error="pytest failed twice with the same ModuleNotFoundError after retry",
    )
    assert scored
    assert scored[0].block.id == "repeated-agent-failure-loop"
    # Strong lexical evidence: comfortably above the runtime floor (8.0).
    assert scored[0].score >= 8.0


def test_rescue_candidates_gives_weak_scores_for_unrelated_errors(capability: ContextReuseCapability) -> None:
    scored = capability.rescue_candidates(
        task="train model",
        error="CUDA out of memory",
    )
    # Something always ranks first, but nothing should clear the evidence
    # floor — the runtime falls back instead of returning junk.
    assert all(s.score < 8.0 for s in scored)


def test_rescue_candidates_empty_store_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = ContextReuseCapability.__new__(ContextReuseCapability)
    monkeypatch.setattr(cap, "_all_active_blocks", lambda: [], raising=False)
    assert cap.rescue_candidates(task="anything", error="anything") == []
