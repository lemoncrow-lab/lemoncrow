from __future__ import annotations

from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfig
from lemoncrow.pro.capabilities.cross_vendor_routing.router import CrossVendorRouter
from lemoncrow.pro.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.pro.capabilities.lesson_promotion.store import TypedLessonStore


def test_cost_cap_binding_downgrades_projected_breach(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    store = TypedLessonStore(tmp_path)
    lesson = TypedLesson(
        kind="cost-cap",
        limit_usd_per_session=0.001,
        on_breach="downgrade-one-tier",
        decay_half_life_days=None,
    )
    store.upsert_lesson(lesson)
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "google"]),
        lesson_store=store,
    )

    recommendation = router.recommend(
        tool_name="agent",
        task_text="run the task end to end",
        session_state={"turn_number": 6, "session_phase": "execute", "session_cost_usd": 0.0009},
    )

    assert recommendation.cost_cap_triggered is True
    assert recommendation.cost_cap_limit_usd_per_session == 0.001
    assert lesson.id in recommendation.applied_lessons
