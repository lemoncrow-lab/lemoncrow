"""Local-only Zoekt-compatible HTTP seam with session-scoped lifecycle reuse."""

from __future__ import annotations

import atexit
import json
import re
import threading
from dataclasses import dataclass
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from time import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


@dataclass(frozen=True)
class ZoektHealth:
    ok: bool
    backend: str
    binary_path: str | None
    index_age_seconds: int | None


@dataclass(frozen=True)
class _SearchMatch:
    byte_start: int
    byte_end: int
    line_number: int
    line_text: str


class ZoektServer:
    """Shared local HTTP runtime that emulates the search contract Zoekt needs."""

    def __init__(self, repo_root: Path, *, binary_path: Path | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.binary_path = binary_path.resolve() if binary_path is not None else None
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self.start_count = 0

    def ensure_started(self) -> str:
        with self._lock:
            if self._httpd is not None and self._thread is not None and self._thread.is_alive():
                return self.base_url
            self._httpd = ThreadingHTTPServer(("127.0.0.1", _find_free_port()), _handler_for(self))
            self._thread = threading.Thread(target=self._httpd.serve_forever, name="zoekt-local-server", daemon=True)
            self._thread.start()
            self._started_at = time()
            self.start_count += 1
        self.health()
        return self.base_url

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("Zoekt server has not been started")
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def health(self) -> ZoektHealth:
        self.ensure_started()
        with urlopen(f"{self.base_url}/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return ZoektHealth(
            ok=bool(payload.get("ok")),
            backend=str(payload.get("backend") or "zoekt"),
            binary_path=str(payload.get("binary_path")) if payload.get("binary_path") else None,
            index_age_seconds=int(payload["index_age_seconds"]) if payload.get("index_age_seconds") is not None else None,
        )

    def stop(self) -> None:
        with self._lock:
            if self._httpd is None:
                return
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None
            self._started_at = None

    def health_payload(self) -> dict[str, Any]:
        index_age_seconds = int(max(0, time() - self._started_at)) if self._started_at is not None else None
        return {
            "ok": True,
            "backend": "zoekt",
            "binary_path": str(self.binary_path) if self.binary_path is not None else None,
            "index_age_seconds": index_age_seconds,
        }

    def search_payload(self, *, query: str, num_matches: int, file_glob: str | None) -> dict[str, Any]:
        files = self._search_repo(query=query, num_matches=num_matches, file_glob=file_glob)
        payload = self.health_payload()
        payload["Result"] = {"Files": files}
        return payload

    def _search_repo(self, *, query: str, num_matches: int, file_glob: str | None) -> list[dict[str, Any]]:
        pattern = re.compile(query)
        results: list[dict[str, Any]] = []
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(self.repo_root).as_posix()
            if file_glob and not fnmatch(rel_path, file_glob):
                continue
            if any(part.startswith(".") and part != "." for part in path.relative_to(self.repo_root).parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            matches = _find_matches(source, pattern)
            if not matches:
                continue
            results.append(
                {
                    "FileName": rel_path,
                    "Matches": [
                        {
                            "ByteStart": match.byte_start,
                            "ByteEnd": match.byte_end,
                            "LineNumber": match.line_number,
                            "Line": match.line_text,
                        }
                        for match in matches[:num_matches]
                    ],
                }
            )
            if len(results) >= num_matches:
                break
        return results


def _find_free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_matches(source: str, pattern: re.Pattern[str]) -> list[_SearchMatch]:
    matches: list[_SearchMatch] = []
    encoded = source.encode("utf-8")
    line_starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            line_starts.append(index + 1)
    for match in pattern.finditer(source):
        char_start, char_end = match.span()
        byte_start = len(source[:char_start].encode("utf-8"))
        byte_end = byte_start + len(source[char_start:char_end].encode("utf-8"))
        line_number = source.count("\n", 0, char_start) + 1
        line_start = source.rfind("\n", 0, char_start)
        line_end = source.find("\n", char_end)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1
        if line_end == -1:
            line_end = len(source)
        line_text = source[line_start:line_end]
        matches.append(
            _SearchMatch(
                byte_start=byte_start,
                byte_end=byte_end,
                line_number=line_number,
                line_text=line_text,
            )
        )
    return matches


def _handler_for(server: ZoektServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(server.health_payload())
                return
            if parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                query = str(params.get("q", [""])[0])
                file_glob = None
                if " file:" in query:
                    query, file_glob = query.split(" file:", 1)
                    query = query.strip()
                    file_glob = file_glob.strip() or None
                payload = server.search_payload(
                    query=query,
                    num_matches=int(params.get("num", ["20"])[0]),
                    file_glob=file_glob,
                )
                self._send_json(payload)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


_SERVERS: dict[str, ZoektServer] = {}
_SERVERS_LOCK = threading.Lock()


def get_zoekt_server(repo_root: str | Path, *, binary_path: Path | None = None) -> ZoektServer:
    root = Path(repo_root).resolve()
    key = str(root)
    with _SERVERS_LOCK:
        server = _SERVERS.get(key)
        if server is None:
            server = ZoektServer(root, binary_path=binary_path)
            _SERVERS[key] = server
        elif binary_path is not None and server.binary_path is None:
            server.binary_path = binary_path.resolve()
    return server


def reset_zoekt_servers() -> None:
    with _SERVERS_LOCK:
        servers = list(_SERVERS.values())
        _SERVERS.clear()
    for server in servers:
        server.stop()


atexit.register(reset_zoekt_servers)


__all__ = ["ZoektHealth", "ZoektServer", "get_zoekt_server", "reset_zoekt_servers"]
