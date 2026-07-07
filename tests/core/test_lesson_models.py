from __future__ import annotations

import pytest
from pydantic import ValidationError

from atelier.core.foundation.lesson_models import LessonCandidate, LessonPromotion


def test_lesson_candidate_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LessonCandidate(
            domain="coding",
            cluster_fingerprint="cluster",
            kind="new_rubric_check",
            proposed_rubric_check="check",
            evidence_trace_ids=["trace-1"],
            confidence=0.5,
            unexpected=True,  # type: ignore[call-arg]
        )


def test_edit_block_candidate_requires_target_id() -> None:
    with pytest.raises(ValidationError):
        LessonCandidate(
            domain="coding",
            cluster_fingerprint="cluster",
            kind="edit_block",
            evidence_trace_ids=["trace-1"],
            confidence=0.5,
        )


def test_lesson_candidate_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        LessonCandidate(
            domain="coding",
            cluster_fingerprint="cluster",
            kind="new_rubric_check",
            proposed_rubric_check="check",
            evidence_trace_ids=["trace-1"],
            confidence=1.5,
        )


def test_lesson_promotion_instantiates_with_default_uuid7_id() -> None:
    promotion = LessonPromotion(lesson_id="lc-1")
    assert promotion.id.startswith("lp-")
