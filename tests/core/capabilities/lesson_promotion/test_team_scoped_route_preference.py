from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter
from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore


def test_team_scoped_route_preference_applies_only_for_matching_team(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    store = TypedLessonStore(root)
    lesson = TypedLesson(
        kind="route-preference",
        scope="team",
        match={"tool": "read", "phase": "explore"},
        prefer={"vendor": "anthropic", "model": "claude-haiku-4-5"},
        confidence=0.9,
        metadata={"team_id": "team-123"},
    )
    store.upsert_lesson(lesson)
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "google"]),
        env={"ANTHROPIC_API_KEY": "anthropic-key", "GOOGLE_API_KEY": "google-key"},
        lesson_store=store,
    )

    matched = router.recommend(
        tool_name="read",
        task_text="find the failing test",
        session_state={"phase": "explore", "team_id": "team-123"},
    )
    unmatched = router.recommend(
        tool_name="read",
        task_text="find the failing test",
        session_state={"phase": "explore", "team_id": "other-team"},
    )

    assert matched.vendor == "anthropic"
    assert lesson.id in matched.applied_lessons
    assert unmatched.vendor == "google"
