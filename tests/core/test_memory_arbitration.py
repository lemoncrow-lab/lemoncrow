from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from lemoncrow.core.capabilities.memory_arbitration import arbitrate
from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder


class _MemoryStore:
    def __init__(self, blocks: list[MemoryBlock]) -> None:
        self.blocks = blocks

    def list_blocks(self, agent_id: str, *, include_tombstoned: bool = False, limit: int = 500) -> list[MemoryBlock]:
        _ = (include_tombstoned, limit)
        return [block for block in self.blocks if block.agent_id == agent_id]


def test_arbitration_adds_when_no_similar_blocks() -> None:
    decision = arbitrate(
        MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact patches"),
        _MemoryStore([]),
        NullEmbedder(),
    )
    assert decision.op == "ADD"


def test_arbitration_emits_per_op_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted_ops: list[str] = []

    class _Counter:
        def __init__(self, name: str, description: str, labels: list[str]) -> None:
            _ = (name, description, labels)

        def labels(self, *, op: str) -> _Counter:
            emitted_ops.append(op)
            return self

        def inc(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "prometheus_client", SimpleNamespace(Counter=_Counter))
    monkeypatch.delattr(
        "lemoncrow.core.capabilities.memory_arbitration.arbiter._emit_arbitration_metric.counter",
        raising=False,
    )

    arbitrate(
        MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact patches"),
        _MemoryStore([]),
        NullEmbedder(),
    )

    assert emitted_ops == ["ADD"]


def test_arbitration_uses_ollama_json_for_similar_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact scoped patches")
    new_fact = MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact scoped edits")
    seen_messages: list[dict[str, str]] = []

    def fake_chat(messages: list[dict[str, str]], json_schema: object | None = None) -> dict[str, str]:
        _ = json_schema
        seen_messages.extend(messages)
        return {
            "op": "UPDATE",
            "target_block_id": existing.id,
            "merged_value": "prefer compact scoped patches and edits",
            "reason": "same preference",
        }

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.memory_arbitration.arbiter.chat",
        fake_chat,
    )

    decision = arbitrate(new_fact, _MemoryStore([existing]), NullEmbedder())

    assert decision.op == "UPDATE"
    assert decision.target_block_id == existing.id
    assert decision.merged_value == "prefer compact scoped patches and edits"
    assert seen_messages[1]["content"] == json.dumps(
        {
            "new_fact": new_fact.model_dump(mode="json"),
            "existing": [existing.model_dump(mode="json")],
            "ops": ["ADD", "UPDATE", "DELETE", "NOOP"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_arbitration_degrades_when_base_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.infra.internal_llm.exceptions import InternalLLMError

    existing = MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact scoped patches")
    new_fact = MemoryBlock(agent_id="lemon:code", label="style", value="prefer compact scoped edits")

    def fake_chat(messages: list[dict[str, str]], json_schema: object | None = None) -> dict[str, str]:
        _ = (messages, json_schema)
        raise InternalLLMError("Internal LLM disabled")

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.memory_arbitration.arbiter.chat",
        fake_chat,
    )

    decision = arbitrate(new_fact, _MemoryStore([existing]), NullEmbedder())

    assert decision.op == "ADD"
    assert decision.reason == "arbitration unavailable"
