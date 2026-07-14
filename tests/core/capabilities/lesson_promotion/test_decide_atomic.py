"""decide() must be atomic (finding #10).

Previously decide() persisted status="approved" (and an empty promotion) before
_typed_lesson_from_candidate could raise, leaving a candidate marked approved
with no typed lesson. Now all fallible work runs before any persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.foundation.lesson_models import LessonCandidate
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.lesson_promotion import LessonPromoterCapability


def test_decide_atomic_on_typed_lesson_failure(tmp_path: Path) -> None:
    store = build_sqlite_store_bundle(tmp_path / ".lemoncrow")
    store.init()
    candidate = LessonCandidate(
        domain="coding",
        cluster_fingerprint="route-preference:Read:explore",
        kind="route-preference",
        evidence_trace_ids=[],
        confidence=0.9,
        evidence={},  # no "typed_lesson" -> _typed_lesson_from_candidate raises
    )
    store.lessons.upsert_lesson_candidate(candidate)
    promoter = LessonPromoterCapability(store)

    with pytest.raises(ValueError):
        promoter.decide(lesson_id=candidate.id, decision="approve", reviewer="t", reason="r")

    # Atomic: a failed typed-lesson build must not flip the candidate to approved.
    reloaded = store.lessons.get_lesson_candidate(candidate.id)
    assert reloaded is not None
    assert reloaded.status == "inbox"
