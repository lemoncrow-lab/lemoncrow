from __future__ import annotations

from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfig
from lemoncrow.pro.capabilities.cross_vendor_routing.router import CrossVendorRouter
from lemoncrow.pro.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.pro.capabilities.lesson_promotion.store import TypedLessonStore


def test_route_preference_binding_reshapes_matching_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    store = TypedLessonStore(tmp_path)
    lesson = TypedLesson(
        kind="route-preference",
        confidence=0.9,
        match={"tool": "read", "phase": "explore"},
        prefer={"vendor": "anthropic", "model": "claude-haiku-4-5"},
    )
    store.upsert_lesson(lesson)
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "google"]),
        lesson_store=store,
    )

    explore = router.recommend(
        tool_name="read",
        task_text="inspect the failing test",
        session_state={"turn_number": 1, "session_phase": "explore"},
    )
    execute = router.recommend(
        tool_name="read",
        task_text="inspect the failing test",
        session_state={"turn_number": 8, "session_phase": "execute"},
    )

    assert explore.vendor == "anthropic"
    assert lesson.id in explore.applied_lessons
    assert execute.vendor == "google"
