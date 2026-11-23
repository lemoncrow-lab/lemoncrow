"""The local capture warm-up starts the automatic per-part proof (P11-1, OWNER-15), after the pre-merge scheduler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from lemoncrow_server_core.local_reviews import LocalReviewReader


def _reader(calls: list[str], **state: Any) -> Any:
    async def cached_get(*_args: Any, **_kwargs: Any) -> None:
        return None

    app = SimpleNamespace(state=SimpleNamespace(**state))
    return SimpleNamespace(
        _WARM_FIRST=(),
        _WARM_NEXT=(),
        _read_stamp=lambda _store: None,
        _resolved_pair=lambda _path, _query: (app, object()),
        _cached_get=cached_get,
    )


def test_a_capture_warm_up_schedules_the_part_proof_after_the_premerge_checks() -> None:
    calls: list[str] = []
    reader = _reader(
        calls,
        review_premerge_scheduler=lambda review_id: calls.append(f"premerge:{review_id}"),
        review_part_proof_scheduler=lambda review_id: calls.append(f"part-proof:{review_id}"),
    )
    asyncio.run(LocalReviewReader.warm(reader, "rev1"))
    assert calls == ["premerge:rev1", "part-proof:rev1"]


def test_a_failing_part_proof_scheduler_never_fails_the_capture() -> None:
    calls: list[str] = []

    def broken(review_id: str) -> None:
        raise RuntimeError("scheduler down")

    reader = _reader(
        calls,
        review_premerge_scheduler=lambda review_id: calls.append(f"premerge:{review_id}"),
        review_part_proof_scheduler=broken,
    )
    asyncio.run(LocalReviewReader.warm(reader, "rev1"))
    assert calls == ["premerge:rev1"]


def test_a_reader_without_the_scheduler_still_warms() -> None:
    calls: list[str] = []
    reader = _reader(calls, review_premerge_scheduler=lambda review_id: calls.append(review_id))
    asyncio.run(LocalReviewReader.warm(reader, "rev1"))
    assert calls == ["rev1"]
