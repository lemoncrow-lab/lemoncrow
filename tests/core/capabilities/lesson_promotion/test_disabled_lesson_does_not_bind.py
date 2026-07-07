from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter
from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore


def test_disabled_lesson_does_not_bind(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    store = TypedLessonStore(tmp_path)
    store.upsert_lesson(
        TypedLesson(
            kind="route-preference",
            enabled=False,
            confidence=0.9,
            match={"tool": "read", "phase": "explore"},
            prefer={"vendor": "anthropic", "model": "claude-haiku-4-5"},
        )
    )
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "google"]),
        lesson_store=store,
    )

    recommendation = router.recommend(
        tool_name="read",
        task_text="inspect the failing test",
        session_state={"turn_number": 1, "session_phase": "explore"},
    )

    assert recommendation.vendor == "google"
    assert recommendation.applied_lessons == ()
