from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import memory


def test_memory_orchestration_has_no_mcp_adapter_dependency() -> None:
    source = inspect.getsource(memory)
    assert "gateway.adapters" not in source
    assert "mcp_server" not in source


def test_mcp_recall_wrapper_preserves_live_provider_seams() -> None:
    source = inspect.getsource(mcp_server._memory_recall)
    assert "_recall_memory_impl" in source
    assert "_memory_service()" in source
    assert "_session_recall_passages(q, k)" in source


def test_mcp_store_vote_wrappers_keep_field_redaction_boundary() -> None:
    store_source = inspect.getsource(mcp_server._memory_store_fact)
    vote_source = inspect.getsource(mcp_server._memory_vote_fact)
    assert "field_redactor=_redact_memory_input" in store_source
    assert "field_redactor=_redact_memory_input" in vote_source


def test_recall_memory_respects_combined_top_k() -> None:
    class _Result:
        def model_dump(self, mode: str = "json") -> dict[str, object]:
            return {"passages": [{"text": f"m{i}"} for i in range(5)]}

    class _Service:
        def recall(self, **kwargs):
            return _Result()

    result = memory.recall_memory(
        _Service(),
        agent_id=None,
        query="q",
        top_k=4,
        session_passages=lambda query, top_k: [{"text": "s0"}, {"text": "s1"}],
    )
    passages = result["passages"]
    assert isinstance(passages, list)
    assert len(passages) == 4
    assert any(item.get("text") == "s0" for item in passages)
