"""WS9 -- N14 <-> G14 bridge: LSP client wired into the tiering hook.

Uses a mock transport (no server); proves the G14 client composes with the N14
residual tiering through ``build_lsp_resolver``, and degrades cleanly when the
client is unavailable.
"""

from __future__ import annotations

from typing import Any

from atelier.core.capabilities.code_context.edge_resolution import (
    CallSite,
    resolve_call_sites,
)
from atelier.core.capabilities.code_context.lsp_resolver import build_lsp_resolver
from atelier.infra.code_intel.lsp import LspClient
from atelier.infra.tree_sitter.tags import Tag


class _MockTransport:
    def __init__(self, results: dict[str, Any], *, available: bool = True) -> None:
        self._results = results
        self._available = available
        self.positions: list[tuple[int, int]] = []

    def is_available(self) -> bool:
        return self._available

    def request(self, method: str, params: dict[str, Any]) -> Any:
        pos = params["position"]
        self.positions.append((pos["line"], pos["character"]))
        return self._results.get(method)


def _loc(uri: str, line: int = 0, char: int = 0) -> dict[str, Any]:
    return {"uri": uri, "range": {"start": {"line": line, "character": char}}}


def _ref(name: str, line: int) -> Tag:
    return Tag(name=name, kind="reference", file="/repo/a.kt", line=line, byte_range=(0, 0))


def test_lsp_bridge_resolves_residual_via_definition() -> None:
    transport = _MockTransport({"textDocument/definition": _loc("file:///repo/dep.kt", 12, 0)})
    resolver = build_lsp_resolver(LspClient(transport))
    report = resolve_call_sites([_ref("mysteryCall", 4)], lsp_resolver=resolver)
    assert len(report.resolved) == 1
    site = report.resolved[0]
    assert site.provenance == "lsp_resolved"
    assert site.target == "file:///repo/dep.kt"
    assert report.residual == []
    # Default locator: one-based line 4 -> zero-based 3, column 0.
    assert transport.positions == [(3, 0)]


def test_lsp_bridge_multiple_defs_marked_dispatch() -> None:
    transport = _MockTransport({"textDocument/definition": [_loc("file:///a.kt"), _loc("file:///b.kt")]})
    resolver = build_lsp_resolver(LspClient(transport))
    report = resolve_call_sites([_ref("overloaded", 1)], lsp_resolver=resolver)
    assert report.resolved[0].provenance == "lsp_dispatch"


def test_lsp_bridge_custom_locator_used() -> None:
    transport = _MockTransport({"textDocument/definition": _loc("file:///d.kt")})
    resolver = build_lsp_resolver(LspClient(transport), locate=lambda site: (site.line, 9))
    resolve_call_sites([_ref("x", 7)], lsp_resolver=resolver)
    assert transport.positions == [(7, 9)]


def test_lsp_bridge_unavailable_client_keeps_residual() -> None:
    transport = _MockTransport({"textDocument/definition": _loc("file:///d.kt")}, available=False)
    resolver = build_lsp_resolver(LspClient(transport))
    report = resolve_call_sites([_ref("x", 1)], lsp_resolver=resolver)
    assert report.resolved == []
    assert [c.name for c in report.residual] == ["x"]
    # Unavailable -> the transport is never queried.
    assert transport.positions == []


def test_lsp_bridge_no_transport_degrades() -> None:
    resolver = build_lsp_resolver(LspClient(transport=None))
    assert resolver(CallSite(name="x", file="/repo/a.kt", line=1)) is None


def test_lsp_bridge_no_definition_keeps_residual() -> None:
    transport = _MockTransport({"textDocument/definition": None})
    resolver = build_lsp_resolver(LspClient(transport))
    report = resolve_call_sites([_ref("x", 1)], lsp_resolver=resolver)
    assert [c.name for c in report.residual] == ["x"]
