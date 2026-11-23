from __future__ import annotations

import os
import re
import signal
import sys
import time

import pytest
from pydantic import ValidationError

from lemoncrow.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from lemoncrow.infra.storage import ids
from lemoncrow.infra.storage.ids import make_uuid7


def test_make_uuid7_has_uuid_text_shape() -> None:
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        make_uuid7(),
    )


def test_memory_block_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryBlock(
            agent_id="lemoncrow:code",
            label="persona",
            value="text",
            unexpected=True,  # type: ignore[call-arg]
        )


def test_archival_passage_dedup_hash_is_non_empty() -> None:
    with pytest.raises(ValidationError):
        ArchivalPassage(
            agent_id="lemoncrow:code",
            text="text",
            source="trace",
            dedup_hash="",
        )


def test_memory_block_value_length_respects_limit_chars() -> None:
    with pytest.raises(ValidationError):
        MemoryBlock(agent_id="lemoncrow:code", label="persona", value="abcdef", limit_chars=3)


def test_memory_model_defaults_use_uuid7_prefixes() -> None:
    block = MemoryBlock(agent_id="lemoncrow:code", label="persona", value="text")
    history = MemoryBlockHistory(
        block_id=block.id,
        prev_value="old",
        new_value="new",
        actor="agent:lemoncrow:code",
    )
    passage = ArchivalPassage(
        agent_id="lemoncrow:code",
        text="text",
        source="trace",
        dedup_hash="hash",
    )
    recall = MemoryRecall(agent_id="lemoncrow:code", query="q", top_passages=[passage.id])

    assert block.id.startswith("mem-")
    assert history.id.startswith("memh-")
    assert passage.id.startswith("pas-")
    assert recall.id.startswith("rec-")


def test_run_memory_frame_instantiates() -> None:
    frame = RunMemoryFrame(
        session_id="run-1",
        pinned_blocks=["mem-1"],
        recalled_passages=["pas-1"],
        summarized_events=["evt-1"],
        tokens_pre_summary=100,
        tokens_post_summary=40,
        compaction_strategy="tfidf",
    )
    assert frame.tokens_pre_summary == 100


def test_make_uuid7_sorts_in_creation_order_even_inside_one_millisecond() -> None:
    ids = [make_uuid7() for _ in range(20000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


@pytest.mark.skipif(sys.platform == "win32", reason="os.fork is POSIX only")
def test_make_uuid7_in_a_forked_child_does_not_inherit_a_held_lock_or_the_parents_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another thread can hold ``ids._lock`` at the instant of a fork; the child's copy is then locked forever."""

    far_future_ms = int(time.time() * 1000) + 10 * 365 * 24 * 3600 * 1000
    monkeypatch.setattr(ids, "_last_ms", far_future_ms)
    assert ids._lock.acquire(timeout=1)  # stands in for a thread that is inside make_uuid7 when the fork happens
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: never return into pytest
        status = 1
        try:
            signal.alarm(10)  # a deadlock on the inherited lock ends here instead of hanging the run
            os.close(read_end)
            first, second = make_uuid7(), make_uuid7()
            os.write(write_end, f"{first} {second}".encode())
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    try:
        with os.fdopen(read_end) as pipe:
            payload = pipe.read()
        _, wait_status = os.waitpid(pid, 0)
    finally:
        ids._lock.release()
    assert os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) == 0, "the child deadlocked or failed"
    first, second = payload.split()
    assert first < second  # still monotonic inside the child
    child_ms = int(first.replace("-", "")[:12], 16)
    assert child_ms < far_future_ms - 24 * 3600 * 1000, "the child kept the parent's counter state"
    assert make_uuid7()  # and the parent is unaffected: its own lock is a different object from the child's
