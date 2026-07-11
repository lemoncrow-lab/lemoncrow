"""Recording failed traces via the local SDK client feeds the lesson inbox.

Regression for finding #2: ``LessonPromoterCapability.ingest_trace`` had only
test callers, so the inbox was never populated in production. ``LocalClient.
record_trace`` now feeds it (store-backed clustering, best-effort).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Importing LocalClient cold trips a pre-existing circular import
# (gateway.sdk -> local -> adapters -> lemoncrow.sdk -> gateway.sdk). Importing the
# lemoncrow.sdk package first resolves it. Unrelated to finding #2.
import lemoncrow.sdk  # noqa: F401
from lemoncrow.gateway.sdk.local import LocalClient


def test_failed_traces_populate_lesson_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Threshold 0 makes any recurring failure cluster, decoupling the assertion
    # from the offline embedder's similarity scores.
    monkeypatch.setenv("LEMONCROW_LESSON_CLUSTER_THRESHOLD", "0.0")
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.lesson_promotion.capability.draft_lesson_body",
        lambda traces: "clustered failure lesson",
    )
    client = LocalClient(root=tmp_path / ".lemoncrow")

    for i in range(3):
        client.record_trace(
            agent="lemon:code",
            domain="coding",
            task=f"write fails on locked path {i}",
            status="failed",
            errors_seen=["PermissionError: permission denied"],
            output_summary="write failed: permission denied",
        )

    inbox = client.lesson_inbox(domain="coding")
    assert inbox.lessons, "3 similar failed traces should yield a clustered lesson candidate"


def test_recurring_failures_dedup_to_single_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the review's Blocker: recurring failures must refresh one
    # cluster candidate, not insert a near-duplicate per recurrence.
    monkeypatch.setenv("LEMONCROW_LESSON_CLUSTER_THRESHOLD", "0.0")
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.lesson_promotion.capability.draft_lesson_body",
        lambda traces: "clustered failure lesson",
    )
    client = LocalClient(root=tmp_path / ".lemoncrow")
    for i in range(5):
        client.record_trace(
            agent="lemon:code",
            domain="coding",
            task=f"write fails on locked path {i}",
            status="failed",
            errors_seen=["PermissionError: permission denied"],
            output_summary="write failed: permission denied",
        )
    inbox = client.lesson_inbox(domain="coding")
    assert len(inbox.lessons) == 1, f"recurring failures should dedup to one candidate, got {len(inbox.lessons)}"


def test_failed_traces_without_errors_do_not_populate_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LESSON_CLUSTER_THRESHOLD", "0.0")
    client = LocalClient(root=tmp_path / ".lemoncrow")
    for i in range(3):
        client.record_trace(
            agent="lemon:code",
            domain="coding",
            task=f"task {i}",
            status="failed",
            errors_seen=[],
        )
    inbox = client.lesson_inbox(domain="coding")
    assert not inbox.lessons
