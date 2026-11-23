"""Review-owned MCP server for immutable Snapshot investigation.

Internal: Review's intelligence drivers launch it as a stdio subprocess for one
investigation; it is not a user-facing MCP server, is never registered with a
host, and is not part of Runtime or Runtime Lite.

This is deliberately not the Runtime MCP server. Review intelligence gets only
five narrowly implemented tools over a materialized Snapshot; autonomous runs
receive the three read-only source tools. Paths are confined to the Snapshot,
and shell commands (human Ask only) run sandboxed (no network, no host home or
environment) in the disposable execution copy that the Review composition
provides.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import urljoin, urlsplit

from lemoncrow.core.environment import REVIEW_AUTONOMOUS_MCP_TOOLS, REVIEW_MCP_TOOLS

MCP_PROTOCOL_VERSION: Final[str] = "2025-06-18"
SERVER_NAME: Final[str] = "lemoncrow-review"
_MAX_LINE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_SOURCE_BYTES: Final[int] = 2 * 1024 * 1024
_MAX_OUTPUT_CHARS: Final[int] = 24_000
_MAX_FETCH_BYTES: Final[int] = 2_000_000
_MAX_REDIRECTS: Final[int] = 5
_SKIP_DIRS: Final[frozenset[str]] = frozenset({".git", ".lemoncrow", "node_modules", ".venv", "venv"})
# Interpreters the macOS shell sandbox may reach when they live in a Homebrew keg (node needs dependency kegs too).
_SHELL_INTERPRETERS: Final[tuple[str, ...]] = ("python3", "python")


def _profile() -> str:
    # Only the exact value the Review launcher writes grants shell/web. Unset, unknown or
    # non-canonical spellings (case, whitespace) are the read-only profile.
    raw = os.environ.get("LEMONCROW_MCP_TOOL_PROFILE", "review_autonomous")
    return raw if raw in {"review", "review_autonomous"} else "review_autonomous"


def _allowed_tools() -> frozenset[str]:
    return REVIEW_MCP_TOOLS if _profile() == "review" else REVIEW_AUTONOMOUS_MCP_TOOLS


def _snapshot_root() -> Path:
    raw = os.environ.get("LEMONCROW_REVIEW_SNAPSHOT_ROOT", "").strip()
    if not raw:
        raise ValueError("Review Snapshot root is not configured")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Review Snapshot root is unavailable")
    return root


def _execution_root() -> Path:
    raw = os.environ.get("LEMONCROW_BASH_WORKSPACE_ROOT", "").strip()
    if not raw:
        raise ValueError("Review execution workspace is not configured")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Review execution workspace is unavailable")
    return root


def _confined(root: Path, raw: str) -> Path:
    value = raw.strip() or "."
    candidate = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path leaves the Review Snapshot") from exc
    return candidate


def _iter_source_files(root: Path, scope: str = "") -> Iterator[tuple[Path, str]]:
    start = _confined(root, scope)
    candidates = [start] if start.is_file() else start.rglob("*")
    for path in candidates:
        if path.is_symlink() or not path.is_file() or any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        # A committed symlink (``docs/x -> ~/.aws/credentials``) must not pull a
        # host file into model context; ``read`` refuses the same path.
        if not path.resolve().is_relative_to(root):
            continue
        try:
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        yield path, text


def _tool_specs() -> list[dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {
        "code_search": {
            "name": "code_search",
            "description": "Search the exact immutable Review Snapshot. Returns ranked source excerpts with path and line ranges.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional Snapshot-relative file or directory"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        },
        "read": {
            "name": "read",
            "description": "Read a Snapshot-relative UTF-8 source file with numbered lines.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
        "grep": {
            "name": "grep",
            "description": "Regex search within the exact immutable Review Snapshot.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
            },
        },
        "bash": {
            "name": "bash",
            "description": "Run a shell command in a disposable writable copy of the Review Snapshot, offline and without host credentials or environment. Changes are discarded.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 300},
                },
                "required": ["command"],
            },
        },
        "web_fetch": {
            "name": "web_fetch",
            "description": "Fetch a public HTTP(S) page with LemonCrow's SSRF-safe fetcher.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
                },
                "required": ["url"],
            },
        },
    }
    return [specs[name] for name in sorted(_allowed_tools())]


def _read(args: Mapping[str, Any]) -> str:
    root = _snapshot_root()
    path = _confined(root, str(args.get("path") or ""))
    if not path.is_file():
        raise ValueError("path is not a readable Snapshot file")
    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError("Snapshot file exceeds the Review read limit")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 299)))
    if end < start:
        raise ValueError("end_line must be >= start_line")
    rendered = "\n".join(f"{index}\t{lines[index - 1]}" for index in range(start, end + 1))
    return rendered[:_MAX_OUTPUT_CHARS]


def _grep(args: Mapping[str, Any]) -> str:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise ValueError("pattern is required")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    limit = min(200, max(1, int(args.get("limit") or 100)))
    root = _snapshot_root()
    rows: list[str] = []
    for path, text in _iter_source_files(root, str(args.get("path") or "")):
        relative = path.relative_to(root).as_posix()
        for line_number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rows.append(f"{relative}:{line_number}: {line[:500]}")
                if len(rows) >= limit:
                    return "\n".join(rows)[:_MAX_OUTPUT_CHARS]
    return ("\n".join(rows) or "No matches.")[:_MAX_OUTPUT_CHARS]


def _code_search(args: Mapping[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    tokens = [token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]*", query) if len(token) > 1]
    if not tokens:
        tokens = [query.lower()]
    limit = min(50, max(1, int(args.get("limit") or 12)))
    root = _snapshot_root()
    hits: list[tuple[int, str, int, list[str]]] = []
    for path, text in _iter_source_files(root, str(args.get("path") or "")):
        lines = text.splitlines()
        relative = path.relative_to(root).as_posix()
        lower_path = relative.lower()
        for index, line in enumerate(lines):
            lower = line.lower()
            score = sum(4 for token in tokens if token in lower) + sum(2 for token in tokens if token in lower_path)
            if score <= 0:
                continue
            start = max(0, index - 2)
            end = min(len(lines), index + 3)
            hits.append((score, relative, index + 1, lines[start:end]))
    hits.sort(key=lambda item: (-item[0], item[1], item[2]))
    rendered: list[str] = []
    seen: set[tuple[str, int]] = set()
    for score, relative, line_number, excerpt in hits:
        key = (relative, line_number)
        if key in seen:
            continue
        seen.add(key)
        start_line = max(1, line_number - 2)
        end_line = start_line + len(excerpt) - 1
        rendered.append(f"{relative}:L{start_line}-L{end_line} [score {score}]\n" + "\n".join(excerpt))
        if len(rendered) >= limit:
            break
    return ("\n\n".join(rendered) or "No matches.")[:_MAX_OUTPUT_CHARS]


def _confined_shell(root: Path, command: str) -> list[str]:
    """Argv that runs ``command`` with only the execution copy and the runtime visible.

    The shell is steered by a model reading Change-controlled content, so it
    never sees the host home, credentials, signing keys or ambient environment
    and has no network. No sandbox on this host means no shell (fail closed).
    """

    shell = ["/bin/bash", "-c", command]
    if sys.platform.startswith("linux"):
        from lemoncrow.pro.capabilities.review.hermetic import HermeticUnavailable, offline_bwrap_prefix

        try:
            prefix = offline_bwrap_prefix()
        except HermeticUnavailable as exc:
            raise ValueError("Review bash requires bubblewrap; refusing to run unsandboxed") from exc
        return [*prefix, "--bind", str(root), str(root), "--chdir", str(root), "--", *shell]
    if sys.platform == "darwin":
        from lemoncrow.pro.environment.macos_sandbox import sandbox_command

        try:
            return sandbox_command(
                shell, workspace=root, cwd=root, writable_workspace=True, path_executables=_SHELL_INTERPRETERS
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    raise ValueError("Review bash has no sandbox on this platform; refusing to run unsandboxed")


def _bash(args: Mapping[str, Any]) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("command is required")
    timeout = min(300.0, max(1.0, float(args.get("timeout_s") or 120.0)))
    root = _execution_root()
    completed = subprocess.run(
        _confined_shell(root, command),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={},
    )
    output = completed.stdout
    if completed.stderr:
        output += ("\n" if output else "") + completed.stderr
    output = output[:_MAX_OUTPUT_CHARS]
    return f"exit_code={completed.returncode}\n{output}".rstrip()


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve public host: {hostname}") from exc
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0])
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("resolved host returned an invalid IP address") from exc
        if not address.is_global:
            raise ValueError(
                "web_fetch refuses private, loopback, link-local, reserved, or otherwise non-public targets"
            )
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise ValueError("host resolved to no usable public address")
    return addresses


def _request_public_url(url: str, *, timeout_s: float = 20.0) -> tuple[int, Mapping[str, str], bytes, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web_fetch requires an http(s) URL with a hostname")
    if parsed.username or parsed.password:
        raise ValueError("web_fetch does not accept URL credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _public_addresses(parsed.hostname, port)
    ip = addresses[0]
    if parsed.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname, port, timeout=timeout_s, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout_s)

    # Pin the TCP connection to the address validated above. HTTPSConnection
    # still uses ``self.host`` for SNI/certificate validation, preventing DNS
    # rebinding between our policy check and the network connection.
    def _connect(
        _address: tuple[str, int],
        timeout: float = timeout_s,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        return socket.create_connection((ip, port), timeout=timeout, source_address=source_address)

    connection._create_connection = _connect  # type: ignore[attr-defined]
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
                "User-Agent": "LemonCrow-Review/1.0",
            },
        )
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if length and int(length) > _MAX_FETCH_BYTES:
            raise ValueError("web_fetch response exceeds the Review size limit")
        body = response.read(_MAX_FETCH_BYTES + 1)
        if len(body) > _MAX_FETCH_BYTES:
            raise ValueError("web_fetch response exceeds the Review size limit")
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body, url
    finally:
        connection.close()


def _fetch_public_text(url: str) -> tuple[str, str]:
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        status, headers, body, _effective = _request_public_url(current)
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("location", "").strip()
            if not location:
                raise ValueError("web_fetch redirect is missing Location")
            current = urljoin(current, location)
            continue
        if status < 200 or status >= 400:
            raise ValueError(f"web_fetch returned HTTP {status}")
        content_type = headers.get("content-type", "")
        charset = "utf-8"
        match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
        if match:
            charset = match.group(1).strip("\"'")
        text = body.decode(charset, errors="replace")
        if "html" in content_type.lower():
            text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text)
        return current, text.strip()
    raise ValueError("web_fetch exceeded the redirect limit")


def _web_fetch(args: Mapping[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    max_chars = min(50_000, max(1_000, int(args.get("max_chars") or 12_000)))
    effective_url, text = _fetch_public_text(url)
    payload = {"url": effective_url, "content": text[:max_chars], "truncated": len(text) > max_chars}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:_MAX_OUTPUT_CHARS]


def _call(name: str, args: Mapping[str, Any]) -> str:
    if name not in _allowed_tools():
        raise ValueError(f"tool is not available in the {_profile()} Review profile: {name}")
    handlers = {"read": _read, "grep": _grep, "code_search": _code_search, "bash": _bash, "web_fetch": _web_fetch}
    return handlers[name](args)


def handle(request: Mapping[str, Any]) -> dict[str, Any] | None:
    method = str(request.get("method") or "")
    request_id = request.get("id")
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "title": "LemonCrow Review", "version": "1"},
                "capabilities": {"tools": {}},
                "instructions": "Investigate only the exact immutable Review Snapshot through the tools listed here.",
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": _tool_specs()})
    if method == "tools/call":
        params = request.get("params")
        params = params if isinstance(params, Mapping) else {}
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        args = raw_args if isinstance(raw_args, Mapping) else {}
        try:
            text = _call(name, args)
            return _ok(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return _ok(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if request_id is None:
        return None
    return _err(request_id, -32601, f"unknown method: {method}")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        if not line.strip():
            continue
        if len(line) > _MAX_LINE_BYTES:
            _write(sink, _err(None, -32600, "request exceeds Review MCP line limit"))
            continue
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            _write(sink, _err(None, -32700, "parse error"))
            continue
        if not isinstance(request, Mapping):
            _write(sink, _err(None, -32600, "request must be a JSON object"))
            continue
        response = handle(request)
        if response is not None:
            _write(sink, response)
    return 0


def _write(sink: TextIO, payload: Mapping[str, Any]) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()


def _ok(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    raise SystemExit(serve())


if __name__ == "__main__":
    main()
