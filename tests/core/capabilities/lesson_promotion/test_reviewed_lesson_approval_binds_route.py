from __future__ import annotations

from atelier.core.capabilities.lesson_promotion.capability import LessonPromoterCapability
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.core.foundation.lesson_models import LessonCandidate
from atelier.core.foundation.store import ContextStore


def test_reviewed_lesson_approval_binds_route(tmp_path) -> None:
    store = ContextStore(tmp_path)
    store.init()
    candidate = LessonCandidate(
        domain="routing",
        cluster_fingerprint="route-preference:Read:explore:google:gemini-flash",
        kind="route-preference",
        evidence_trace_ids=[],
        confidence=0.9,
        evidence={
            "typed_lesson": {
                "kind": "route-preference",
                "scope": "user",
                "match": {"tool": "Read", "phase": "explore"},
                "prefer": {"vendor": "google", "model": "gemini-flash"},
                "confidence": 0.9,
                "source_session_id": "session-123",
                "decay_half_life_days": 30,
            }
        },
    )
    store.upsert_lesson_candidate(candidate)
    capability = LessonPromoterCapability(store)

    result = capability.decide(
        lesson_id=candidate.id,
        decision="approve",
        reviewer="dev",
        reason="Recurring read savings",
    )

    typed_store = TypedLessonStore(tmp_path)
    lesson = typed_store.get_lesson(candidate.id)
    assert lesson is not None
    assert lesson.kind == "route-preference"
    assert lesson.prefer["model"] == "gemini-flash"
    assert result["typed_lesson"]["id"] == candidate.id
