from __future__ import annotations

from pathlib import Path

from lemoncrow_client.kit.read_path import split_read_range_suffix

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import output


def test_mcp_server_reexports_canonical_output_policy() -> None:
    assert mcp_server._max_result_bytes is output.max_result_bytes
    assert mcp_server._truncate_result_text is output.truncate_result_text
    assert mcp_server._compact_result_chars is output.compact_result_chars
    assert mcp_server._spill_result_chars is output.spill_result_chars
    assert mcp_server._compact_result_text is output.compact_result_text
    assert mcp_server._effective_spill_tool is output.effective_spill_tool
    assert mcp_server._tool_output_spill_enabled is output.output_spill_enabled
    assert mcp_server._auto_compact_result_text is output.auto_compact_result_text
    assert mcp_server._spill_oversized_result_text is output.spill_oversized_result_text
    assert mcp_server._SPILL_TOOLS is output.SPILL_TOOLS
    assert mcp_server._SPILL_CHAR_CAP_TOOLS is output.SPILL_CHAR_CAP_TOOLS


def test_read_range_parser_has_one_canonical_implementation() -> None:
    assert mcp_server._split_read_range_suffix is split_read_range_suffix
    assert split_read_range_suffix("src/app.py:L10-L20") == ("src/app.py", "L10-L20")
    assert split_read_range_suffix("src/app.py:summary") == ("src/app.py:summary", None)


def test_mcp_transport_no_longer_owns_output_policy_functions() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text()
    for name in (
        "_max_result_bytes",
        "_truncate_result_text",
        "_compact_result_chars",
        "_spill_result_chars",
        "_compact_result_text",
        "_effective_spill_tool",
        "_auto_compact_result_text",
        "_spill_oversized_result_text",
    ):
        assert f"def {name}(" not in source
