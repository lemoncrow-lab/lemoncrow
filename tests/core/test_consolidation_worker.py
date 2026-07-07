from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atelier.core.capabilities.consolidation import consolidate
from atelier.core.foundation.models import Playbook
from atelier.core.foundation.store import ContextStore
from atelier.infra.internal_llm import InternalLLMError


def _block(
    block_id: str,
    title: str,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    success_count: int = 0,
    failure_count: int = 0,
) -> Playbook:
    timestamp = created_at or datetime.now(UTC)
    return Playbook(
        id=block_id,
        title=title,
        domain="testing",
        situation="When checkout retries fail with timeout during webhook delivery",
        triggers=["checkout", "retry", "timeout"],
        procedure=["Inspect retry budget", "Verify idempotency key", "Run webhook tests"],
        failure_signals=["timeout", "duplicate delivery"],
        success_count=success_count,
        failure_count=failure_count,
        created_at=timestamp,
        updated_at=updated_at or timestamp,
    )


def test_consolidate_writes_duplicate_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    first = _block("rb-one", "Checkout retry timeout")
    second = _block("rb-two", "Checkout retry webhook timeout")
    store.upsert_block(first, write_markdown=False)
    store.upsert_block(second, write_markdown=False)
    seen_messages: list[dict[str, str]] = []

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        assert isinstance(messages, list)
        seen_messages.extend(messages)
        raise InternalLLMError("offline")

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

    report = consolidate(store)

    candidates = store.list_consolidation_candidates()
    assert report.duplicates == 1
    assert report.written == 1
    assert len(candidates) == 1
    assert candidates[0].kind == "duplicate_cluster"
    assert set(candidates[0].affected_block_ids) == {"rb-one", "rb-two"}
    content = seen_messages[1]["content"]
    payload = json.loads(content)
    assert content == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert {item["id"] for item in payload} == {"rb-one", "rb-two"}


def test_consolidate_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    store.upsert_block(_block("rb-one", "Checkout retry timeout"), write_markdown=False)
    store.upsert_block(_block("rb-two", "Checkout retry webhook timeout"), write_markdown=False)
    monkeypatch.setattr(
        "atelier.core.capabilities.consolidation.worker.chat",
        lambda messages, json_schema=None: (_ for _ in ()).throw(InternalLLMError("offline")),
    )

    report = consolidate(store, dry_run=True)

    assert report.duplicates == 1
    assert report.quarantined == 0
    assert report.written == 0
    assert store.list_consolidation_candidates() == []


def test_consolidate_writes_stale_active_block_candidate(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    stale_at = datetime.now(UTC) - timedelta(days=200)
    store.upsert_block(
        _block("rb-stale", "Ancient checkout retry timeout", created_at=stale_at, updated_at=stale_at),
        write_markdown=False,
    )

    report = consolidate(store)

    candidates = store.list_consolidation_candidates()
    assert report.stale == 1
    assert report.quarantined == 0
    assert report.written == 1
    assert len(candidates) == 1
    assert candidates[0].kind == "stale_candidate"
    assert candidates[0].affected_block_ids == ["rb-stale"]
    assert candidates[0].evidence["source"] == "playbook"


def test_consolidate_honors_since_for_stale_active_blocks(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    updated_at = datetime.now(UTC) - timedelta(days=30)
    store.upsert_block(
        _block(
            "rb-recent",
            "Recently used checkout retry",
            created_at=updated_at,
            updated_at=updated_at,
        ),
        write_markdown=False,
    )

    report = consolidate(store, since=timedelta(days=60), dry_run=True)

    assert report.stale == 0
    assert report.quarantined == 0
    assert report.written == 0


def test_consolidate_auto_quarantines_chronic_failures(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    stale_at = datetime.now(UTC) - timedelta(days=200)
    store.upsert_block(
        _block(
            "rb-bad",
            "Broken checkout retry playbook",
            created_at=stale_at,
            updated_at=stale_at,
            success_count=1,
            failure_count=3,
        ),
        write_markdown=False,
    )

    report = consolidate(store)

    updated = store.get_block("rb-bad")
    assert updated is not None
    assert updated.status == "quarantined"
    assert report.quarantined == 1
    assert report.written == 0
    assert store.list_consolidation_candidates() == []


def test_consolidate_dry_run_does_not_quarantine(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    store.upsert_block(
        _block(
            "rb-bad",
            "Broken checkout retry playbook",
            success_count=1,
            failure_count=3,
        ),
        write_markdown=False,
    )

    report = consolidate(store, dry_run=True)

    updated = store.get_block("rb-bad")
    assert updated is not None
    assert updated.status == "active"
    assert report.quarantined == 1
    assert report.written == 0


def test_quarantined_blocks_do_not_emit_stale_or_duplicate_candidates(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    stale_at = datetime.now(UTC) - timedelta(days=200)
    store.upsert_block(
        _block(
            "rb-bad",
            "Checkout retry timeout",
            created_at=stale_at,
            updated_at=stale_at,
            success_count=1,
            failure_count=3,
        ),
        write_markdown=False,
    )
    store.upsert_block(
        _block(
            "rb-good",
            "Checkout retry webhook timeout",
            created_at=stale_at,
            updated_at=datetime.now(UTC),
        ),
        write_markdown=False,
    )

    report = consolidate(store)

    assert report.quarantined == 1
    assert report.duplicates == 0
    assert report.stale == 0
    assert store.list_consolidation_candidates() == []
