from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.context_compression.request_boundary import (
    HeadroomRequestContextCompressor,
    NoopRequestContextCompressor,
    RequestCompressionState,
    request_context_compressor_from_env,
)


def _assistant_call(call_id: str, name: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def test_noop_is_zero_overhead_passthrough() -> None:
    messages = [{"role": "user", "content": "hello"}]
    result = NoopRequestContextCompressor().compress(messages, model="test")
    assert result.messages is messages
    assert result.backend == "none"
    assert result.estimated_tokens_saved == 0


def test_headroom_preserves_lc_semantic_outputs_and_spills_generic_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    protected = "path/to/file.py:L10-L20\nERROR buried exact evidence"
    generic = '{"rows":[' + ",".join(f'{{"id":{i},"status":"ok"}}' for i in range(200)) + "]}"

    messages = [
        {"role": "system", "content": "system must remain exact"},
        {"role": "user", "content": "find the failure"},
        _assistant_call("lc1", "mcp__LC_lc__code_search"),
        {"role": "tool", "tool_call_id": "lc1", "content": protected},
        _assistant_call("ext1", "external_inventory"),
        {"role": "tool", "tool_call_id": "ext1", "content": generic},
    ]

    def fake_compress(payload: list[dict[str, Any]], **_: Any) -> Any:
        out = [dict(message) for message in payload]
        out[0]["content"] = "BROKEN SYSTEM"
        out[3]["content"] = "BROKEN LC POINTER"
        out[5]["content"] = '{"rows_summary":"200 ok"}'
        return types.SimpleNamespace(
            messages=out,
            tokens_saved=999,
            transforms_applied=["smart_crusher"],
        )

    fake_headroom = types.ModuleType("headroom")
    fake_headroom.compress = fake_compress  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "headroom", fake_headroom)

    result = HeadroomRequestContextCompressor().compress(messages, model="gpt-test")
    assert result.messages[0] == messages[0]
    assert result.messages[3]["content"] == protected
    compressed_generic = result.messages[5]["content"]
    assert isinstance(compressed_generic, str)
    assert '"rows_summary":"200 ok"' in compressed_generic
    assert "compacted:headroom" in compressed_generic
    assert "full:" in compressed_generic
    assert result.changed_messages == 1
    assert result.estimated_tokens_saved > 0

    spill_files = list(tmp_path.glob("external_inventory-*.txt"))
    assert len(spill_files) == 1
    assert spill_files[0].read_text(encoding="utf-8") == generic


def test_headroom_fails_open_when_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [_assistant_call("x", "external_tool"), {"role": "tool", "tool_call_id": "x", "content": "x" * 5000}]

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    fake_headroom = types.ModuleType("headroom")
    fake_headroom.compress = explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "headroom", fake_headroom)

    result = HeadroomRequestContextCompressor().compress(messages, model="gpt-test")
    assert result.messages == messages
    assert result.reason == "failed open: RuntimeError"


def test_compression_state_reuses_exact_wire_prefix_until_model_or_history_changes() -> None:
    raw = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _assistant_call("x", "external_tool"),
        {"role": "tool", "tool_call_id": "x", "content": "raw"},
    ]
    wire = [dict(message) for message in raw]
    wire[3] = {**wire[3], "content": "compressed [full: /tmp/x]"}

    state = RequestCompressionState()
    prepared, frozen = state.prepare(raw, model="model-a")
    assert prepared == raw
    assert frozen == 0

    state.commit(raw, wire, model="model-a")
    extended = [*raw, {"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
    prepared, frozen = state.prepare(extended, model="model-a")
    assert frozen == len(raw)
    assert prepared[: len(raw)] == wire
    assert prepared[len(raw) :] == extended[len(raw) :]

    switched, frozen = state.prepare(extended, model="model-b")
    assert frozen == 0
    assert switched == extended

    changed = [*raw]
    changed[1] = {"role": "user", "content": "changed"}
    changed.append({"role": "user", "content": "next"})
    reset, frozen = state.prepare(changed, model="model-a")
    assert frozen == 0
    assert reset == changed


def test_factory_defaults_to_none_and_rejects_unknown() -> None:
    assert isinstance(request_context_compressor_from_env({}), NoopRequestContextCompressor)
    with pytest.raises(ValueError, match="unknown LEMONCROW_CONTEXT_COMPRESSOR"):
        request_context_compressor_from_env({"LEMONCROW_CONTEXT_COMPRESSOR": "wat"})


def test_runtime_session_lifecycle_clears_wire_compression_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_CONTEXT_COMPRESSOR", "none")

    from lemoncrow.gateway.cli.runtime import InteractiveRuntime

    runtime = InteractiveRuntime(root=tmp_path, mcp_enabled=False)
    session_id = asyncio.run(runtime.start_session(session_id="session-a"))
    assert session_id == "session-a"
    assert runtime.session_messages(session_id) == []

    runtime._request_compression_states[session_id] = RequestCompressionState()
    runtime.restore_session(session_id, [{"role": "user", "content": "restored"}])
    assert runtime.session_messages(session_id) == [{"role": "user", "content": "restored"}]
    assert session_id not in runtime._request_compression_states

    runtime._request_compression_states[session_id] = RequestCompressionState()
    runtime.drop_session(session_id)
    assert session_id not in runtime.session_ids
    assert session_id not in runtime._request_compression_states
    with pytest.raises(ValueError, match="unknown LEMONCROW_CONTEXT_COMPRESSOR"):
        request_context_compressor_from_env({"LEMONCROW_CONTEXT_COMPRESSOR": "wat"})
