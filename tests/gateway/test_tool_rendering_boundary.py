from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import rendering

_RENDERERS = (
    "_append_search_verdict_footer",
    "_compress_candidate_files",
    "_render_code_search_md",
    "_render_compact_md",
    "_render_context_tool_md",
    "_render_memory_md",
    "_render_read_md",
    "_render_read_outline_md",
    "_render_rescue_md",
    "_render_search_md",
    "_render_symbol_read_md",
    "_render_verify_md",
    "_sparse_gutter",
)


def test_mcp_server_reexports_canonical_renderers() -> None:
    for name in _RENDERERS:
        assert getattr(mcp_server, name) is getattr(rendering, name), name


def test_mcp_transport_no_longer_owns_renderer_implementations() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text()
    for name in _RENDERERS:
        assert f"def {name}(" not in source
