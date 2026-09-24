from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import output
from lemoncrow.gateway.tools.presentation import assemble_response_text, bound_tool_output


def test_mcp_server_reexports_canonical_trim_accounting() -> None:
    assert mcp_server._trimmed_tokens_saved is output.trimmed_tokens_saved
    assert mcp_server._HOST_INLINE_RESULT_CHARS == output.HOST_INLINE_RESULT_CHARS


def test_assemble_response_text_preserves_rendered_and_loop_note_order() -> None:
    assert assemble_response_text({"x": 1}, "rendered", "note") == "rendered\nnote"
    assert assemble_response_text({"x": 1}, "rendered\nnote", "note") == "rendered\nnote"
    assert assemble_response_text({"b": 2, "a": 1}, None) == '{"a":1,"b":2}'


def test_bound_tool_output_passes_small_text_through(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "0")
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    text, saved = bound_tool_output("bash", {}, "small")
    assert text == "small"
    assert saved == 0


def test_trimmed_tokens_saved_caps_at_host_inline_baseline() -> None:
    cap = output.HOST_INLINE_RESULT_CHARS
    assert output.trimmed_tokens_saved(cap * 4, cap - 400) == 100
    assert output.trimmed_tokens_saved(100, 100) == 0
