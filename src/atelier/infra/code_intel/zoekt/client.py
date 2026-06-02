"""Client helpers for the managed Zoekt runtime."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from .server import ZoektServer


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
    """JSON client wrapper around the managed Zoekt runtime."""

    def __init__(self, server: ZoektServer) -> None:
        self.server = server

    def health(self) -> dict[str, Any]:
        health = self.server.health()
        return {
            "ok": health.ok,
            "backend": health.backend,
            "binary_path": health.binary_path,
            "index_age_seconds": health.index_age_seconds,
        }

    def search(self, query: str, *, num_matches: int = 50, file_glob: str | None = None) -> list[ZoektFileResult]:
        payload = self.server.raw_search({"Q": query})
        result_payload = payload.get("Result") or {}
        if not isinstance(result_payload, dict):
            result_payload = {}
        files = result_payload.get("Files")
        if not isinstance(files, list):
            files = []
        results: list[ZoektFileResult] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("FileName") or "")
            if file_glob and not fnmatch(path, file_glob):
                continue
            raw_matches = item.get("LineMatches")
            if not isinstance(raw_matches, list):
                raw_matches = []
            matches: list[ZoektClientMatch] = []
            for raw in raw_matches:
                if not isinstance(raw, dict):
                    continue
                line_number = int(raw.get("LineNumber", 0))
                line_start = int(raw.get("LineStart", 0))
                encoded_line = str(raw.get("Line") or "")
                try:
                    line_text = base64.b64decode(encoded_line).decode("utf-8", errors="replace").rstrip("\n")
                except (ValueError, TypeError):
                    line_text = ""
                line_fragments = raw.get("LineFragments")
                if not isinstance(line_fragments, list):
                    line_fragments = []
                if not line_fragments:
                    line_end = int(raw.get("LineEnd", line_start))
                    matches.append(
                        ZoektClientMatch(
                            byte_start=line_start,
                            byte_end=line_end,
                            line_number=line_number,
                            line_text=line_text,
                        )
                    )
                    continue
                for fragment in line_fragments:
                    if not isinstance(fragment, dict):
                        continue
                    byte_start = _int_value(fragment.get("Offset", fragment.get("LineOffset", line_start)))
                    byte_end = byte_start + _int_value(fragment.get("MatchLength", 0))
                    matches.append(
                        ZoektClientMatch(
                            byte_start=byte_start,
                            byte_end=byte_end,
                            line_number=line_number,
                            line_text=line_text,
                        )
                    )
            if matches:
                results.append(ZoektFileResult(path=path, matches=matches[:num_matches]))
            if len(results) >= num_matches:
                break
        return results


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["ZoektClient", "ZoektClientMatch", "ZoektFileResult"]
