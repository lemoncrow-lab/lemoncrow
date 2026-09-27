"""WS9 G14 -- LSP client bridge over a MOCK transport (no real server).

Every test injects a fake transport implementing the LspTransport Protocol, so
nothing here spawns a process. The graceful-degrade contract is exercised on
every sad path.
"""

from __future__ import annotations

from typing import Any

from lemoncrow.infra.code_intel.lsp import (
    SERVER_COMMANDS,
    Location,
    LspClient,
    LspTransport,
    LspTransportError,
    server_command_for_language,
)


class _MockTransport:
    """In-memory LSP transport: canned ``result`` per method, no process."""

    def __init__(self, results: dict[str, Any], *, available: bool = True) -> None:
        self._results = results
        self._available = available
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def is_available(self) -> bool:
        return self._available

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method not in self._results:
            raise LspTransportError(f"unexpected method {method}")
        return self._results[method]


class _RaisingTransport:
    def is_available(self) -> bool:
        return True

    def request(self, method: str, params: dict[str, Any]) -> Any:
        raise LspTransportError("transport exploded")


def _loc(uri: str, line: int, char: int) -> dict[str, Any]:
    return {"uri": uri, "range": {"start": {"line": line, "character": char}}}


# --------------------------------------------------------------------------- #
# Protocol conformance.
# --------------------------------------------------------------------------- #
def test_mock_satisfies_transport_protocol() -> None:
    assert isinstance(_MockTransport({}), LspTransport)


# --------------------------------------------------------------------------- #
# Happy path: definition + references resolve via the mock.
# --------------------------------------------------------------------------- #
def test_definition_resolves_via_mock() -> None:
    transport = _MockTransport({"textDocument/definition": _loc("file:///foo.kt", 10, 4)})
    client = LspClient(transport)
    locs = client.definition("file:///bar.kt", 3, 7)
    assert locs == [Location(uri="file:///foo.kt", line=10, character=4)]
    method, params = transport.calls[0]
    assert method == "textDocument/definition"
    assert params["position"] == {"line": 3, "character": 7}


def test_definition_accepts_location_array() -> None:
    transport = _MockTransport({"textDocument/definition": [_loc("file:///a.kt", 1, 0), _loc("file:///b.kt", 2, 0)]})
    locs = LspClient(transport).definition("file:///x.kt", 0, 0)
    assert [loc.uri for loc in locs] == ["file:///a.kt", "file:///b.kt"]


def test_definition_accepts_location_link() -> None:
    # LocationLink form uses targetUri / targetRange.
    link = {"targetUri": "file:///t.kt", "targetRange": {"start": {"line": 5, "character": 2}}}
    locs = LspClient(_MockTransport({"textDocument/definition": link})).definition("file:///x.kt", 0, 0)
    assert locs == [Location(uri="file:///t.kt", line=5, character=2)]


def test_references_passes_include_declaration() -> None:
    transport = _MockTransport({"textDocument/references": [_loc("file:///r.kt", 9, 1)]})
    client = LspClient(transport)
    locs = client.references("file:///x.kt", 0, 0, include_declaration=True)
    assert locs == [Location(uri="file:///r.kt", line=9, character=1)]
    _, params = transport.calls[0]
    assert params["context"] == {"includeDeclaration": True}


# --------------------------------------------------------------------------- #
# Graceful degradation (every sad path -> []).
# --------------------------------------------------------------------------- #
def test_no_transport_degrades_to_empty() -> None:
    client = LspClient(transport=None)
    assert client.available is False
    assert client.definition("file:///x.kt", 0, 0) == []
    assert client.references("file:///x.kt", 0, 0) == []


def test_unavailable_server_degrades() -> None:
    transport = _MockTransport({"textDocument/definition": _loc("f", 1, 1)}, available=False)
    client = LspClient(transport)
    assert client.available is False
    assert client.definition("file:///x.kt", 0, 0) == []
    # An unavailable client never even issues the request.
    assert transport.calls == []


def test_transport_error_degrades() -> None:
    client = LspClient(_RaisingTransport())
    assert client.definition("file:///x.kt", 0, 0) == []


def test_malformed_result_degrades() -> None:
    # Missing range / wrong types must yield [] rather than raise.
    bad = _MockTransport({"textDocument/definition": [{"uri": "file:///x.kt"}, "garbage", 42]})
    assert LspClient(bad).definition("file:///x.kt", 0, 0) == []
    none_result = _MockTransport({"textDocument/definition": None})
    assert LspClient(none_result).definition("file:///x.kt", 0, 0) == []


# --------------------------------------------------------------------------- #
# Registry: one real adapter, extensible, None for the rest.
# --------------------------------------------------------------------------- #
def test_registry_has_one_documented_adapter() -> None:
    assert server_command_for_language("kotlin") == ("kotlin-language-server",)
    # An unwired language has no adapter -> graceful None.
    assert server_command_for_language("python") is None
    assert server_command_for_language("made-up") is None
    assert "kotlin" in SERVER_COMMANDS
