from __future__ import annotations

from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore


def test_lesson_scope_isolation(tmp_path) -> None:
    store = TypedLessonStore(tmp_path)
    user_lesson = TypedLesson(
        kind="route-preference",
        scope="user",
        match={"tool": "Read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
    )
    team_lesson = TypedLesson(
        kind="route-preference",
        scope="team",
        match={"tool": "Read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
    )
    store.upsert_lesson(user_lesson)
    store.upsert_lesson(team_lesson)

    active = store.list_active_lessons(scope="user")

    assert [lesson.id for lesson in active] == [user_lesson.id]
