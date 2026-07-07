"""Minimal LSP client bridge (G14).

Exposes the two requests needed to resolve N14 residuals --
``textDocument/definition`` and ``textDocument/references`` -- over an injected
:class:`~atelier.infra.code_intel.lsp.transport.LspTransport`.

Graceful degradation is the headline contract: a missing transport, an
unavailable server, a malformed response, or any transport error all collapse
to an empty result so the caller silently falls back to tree-sitter. Nothing
here raises on the happy *or* sad path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from atelier.infra.code_intel.lsp.transport import LspTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Location:
    """A resolved source location (LSP ``Location``), normalised.

    ``uri`` is the raw ``textDocument`` URI; ``line``/``character`` are
    zero-based per the LSP spec.
    """

    uri: str
    line: int
    character: int


class LspClient:
    """Thin, fail-open bridge over an injectable LSP transport.

    The client owns no process and no framing -- the transport does. Construct
    with ``transport=None`` to get a permanently-degraded client (every query
    returns ``[]``); this is the default opt-out path.
    """

    def __init__(self, transport: LspTransport | None = None) -> None:
        self._transport = transport

    @property
    def available(self) -> bool:
        """True only when a transport is present AND reports itself reachable."""
        if self._transport is None:
            return False
        try:
            return bool(self._transport.is_available())
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return False

    def definition(self, uri: str, line: int, character: int) -> list[Location]:
        """Resolve ``textDocument/definition`` at a position. ``[]`` on degrade."""
        return self._locations("textDocument/definition", uri, line, character)

    def references(self, uri: str, line: int, character: int, *, include_declaration: bool = False) -> list[Location]:
        """Resolve ``textDocument/references`` at a position. ``[]`` on degrade."""
        extra = {"context": {"includeDeclaration": include_declaration}}
        return self._locations("textDocument/references", uri, line, character, extra)

    def _locations(
        self,
        method: str,
        uri: str,
        line: int,
        character: int,
        extra: dict[str, Any] | None = None,
    ) -> list[Location]:
        if not self.available:
            return []
        params: dict[str, Any] = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        if extra:
            params.update(extra)
        try:
            assert self._transport is not None  # available implies a transport
            result = self._transport.request(method, params)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return []
        return _parse_locations(result)


def _parse_locations(result: Any) -> list[Location]:
    """Normalise an LSP definition/references ``result`` into ``Location``s.

    Accepts a single ``Location``, a ``Location[]``, or a ``LocationLink[]``
    (``targetUri``/``targetRange``). Anything unrecognised yields ``[]`` -- the
    parser never raises.
    """
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    locations: list[Location] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        loc = _parse_one(item)
        if loc is not None:
            locations.append(loc)
    return locations


def _parse_one(item: dict[str, Any]) -> Location | None:
    # LocationLink uses targetUri/targetRange; Location uses uri/range.
    uri = item.get("uri") or item.get("targetUri")
    rng = item.get("range") or item.get("targetRange")
    if not isinstance(uri, str) or not isinstance(rng, dict):
        return None
    start = rng.get("start")
    if not isinstance(start, dict):
        return None
    line = start.get("line")
    character = start.get("character")
    if not isinstance(line, int) or not isinstance(character, int):
        return None
    return Location(uri=uri, line=line, character=character)
