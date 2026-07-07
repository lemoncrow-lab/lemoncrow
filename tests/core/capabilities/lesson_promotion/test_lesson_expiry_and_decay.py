from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore


def test_lesson_expiry_and_decay(tmp_path) -> None:
    store = TypedLessonStore(tmp_path)
    now = datetime(2026, 5, 19, tzinfo=UTC)
    active = TypedLesson(
        kind="route-preference",
        match={"tool": "Read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
        captured_at=now - timedelta(days=5),
    )
    expired = TypedLesson(
        kind="route-preference",
        match={"tool": "Read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
        captured_at=now - timedelta(days=5),
        expires_at=now - timedelta(days=1),
    )
    decayed = TypedLesson(
        kind="route-preference",
        match={"tool": "Read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.8,
        captured_at=now - timedelta(days=60),
        decay_half_life_days=30,
    )
    store.upsert_lesson(active)
    store.upsert_lesson(expired)
    store.upsert_lesson(decayed)

    active_lessons = store.list_active_lessons(at=now)

    assert [lesson.id for lesson in active_lessons] == [active.id]
    assert decayed.effective_confidence_at(now) < 0.4
    assert active.applies_without_tiebreaker_at(now) is True
