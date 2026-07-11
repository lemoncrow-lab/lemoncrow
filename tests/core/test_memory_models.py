from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from lemoncrow.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from lemoncrow.infra.storage.ids import make_uuid7


def test_make_uuid7_has_uuid_text_shape() -> None:
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        make_uuid7(),
    )


def test_memory_block_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryBlock(
            agent_id="lemon:code",
            label="persona",
            value="text",
            unexpected=True,  # type: ignore[call-arg]
        )


def test_archival_passage_dedup_hash_is_non_empty() -> None:
    with pytest.raises(ValidationError):
        ArchivalPassage(
            agent_id="lemon:code",
            text="text",
            source="trace",
            dedup_hash="",
        )


def test_memory_block_value_length_respects_limit_chars() -> None:
    with pytest.raises(ValidationError):
        MemoryBlock(agent_id="lemon:code", label="persona", value="abcdef", limit_chars=3)


def test_memory_model_defaults_use_uuid7_prefixes() -> None:
    block = MemoryBlock(agent_id="lemon:code", label="persona", value="text")
    history = MemoryBlockHistory(
        block_id=block.id,
        prev_value="old",
        new_value="new",
        actor="agent:lemon:code",
    )
    passage = ArchivalPassage(
        agent_id="lemon:code",
        text="text",
        source="trace",
        dedup_hash="hash",
    )
    recall = MemoryRecall(agent_id="lemon:code", query="q", top_passages=[passage.id])

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
