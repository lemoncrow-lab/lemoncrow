from __future__ import annotations

from atelier.core.foundation.store import ContextStore
from atelier.infra.runtime.outcome_capture import emit_typed_lesson_candidate


def test_outcome_capture_emits_candidate_for_recurring_pattern(tmp_path) -> None:
    store = ContextStore(tmp_path)
    store.init()

    candidate = emit_typed_lesson_candidate(
        store,
        kind="route-preference",
        domain="routing",
        source_session_id="session-42",
        route_outcomes=[
            {
                "tool": "Read",
                "recommended_vendor": "google",
                "recommended_model": "gemini-flash",
                "recommendation_followed": False,
                "scored_state": {"session_phase": "explore"},
            },
            {
                "tool": "Read",
                "recommended_vendor": "google",
                "recommended_model": "gemini-flash",
                "recommendation_followed": False,
                "scored_state": {"session_phase": "explore"},
            },
            {
                "tool": "Read",
                "recommended_vendor": "google",
                "recommended_model": "gemini-flash",
                "recommendation_followed": False,
                "scored_state": {"session_phase": "explore"},
            },
        ],
    )

    assert candidate is not None
    assert candidate.kind == "route-preference"
    assert candidate.evidence["typed_lesson"]["prefer"]["model"] == "gemini-flash"
    stored = store.get_lesson_candidate(candidate.id)
    assert stored is not None
