"""urllib-based client for the local Zoekt-compatible search seam."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class ZoektClientMatch:
    byte_start: int
    byte_end: int
    line_number: int
    line_text: str


@dataclass(frozen=True)
class ZoektFileResult:
    path: str
    matches: list[ZoektClientMatch]


class ZoektClient:
    """Small JSON client for the local Zoekt search API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def health(self) -> dict[str, Any]:
        with urlopen(f"{self.base_url}/healthz", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, *, num_matches: int = 50, file_glob: str | None = None) -> list[ZoektFileResult]:
        effective_query = query if not file_glob else f"{query} file:{file_glob}"
        params = urlencode({"q": effective_query, "num": num_matches})
        with urlopen(f"{self.base_url}/api/search?{params}", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        files = payload.get("Result", {}).get("Files", [])
        results: list[ZoektFileResult] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            raw_matches = item.get("Matches", [])
            matches: list[ZoektClientMatch] = []
            for raw in raw_matches:
                if not isinstance(raw, dict):
                    continue
                matches.append(
                    ZoektClientMatch(
                        byte_start=int(raw.get("ByteStart", 0)),
                        byte_end=int(raw.get("ByteEnd", 0)),
                        line_number=int(raw.get("LineNumber", 0)),
                        line_text=str(raw.get("Line") or ""),
                    )
                )
            results.append(ZoektFileResult(path=str(item.get("FileName") or ""), matches=matches))
        return results


__all__ = ["ZoektClient", "ZoektClientMatch", "ZoektFileResult"]
