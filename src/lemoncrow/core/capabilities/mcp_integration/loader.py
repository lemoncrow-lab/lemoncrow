"""Load and spawn MCP servers from .mcp.json configuration files."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Server names under which a host config registers LemonCrow's own MCP server.
# discover_mcp_configs() excludes these unconditionally: it is the shared
# primitive behind both the TUI's spawn-everything-discovered loop and the
# gateway's mcp-proxy tool, and neither must ever be handed LemonCrow's own
# entry to spawn (infinite self-recursion for the TUI; the proxy applies its
# own additional token-based check on top -- see mcp_proxy._is_self -- but
# relies on this set as the shared source of truth for name-only matches).
SELF_SERVER_NAMES = {"lc", "lemoncrow", "plugin_lemoncrow_lc", "lemoncrow_lc"}

_MCP_CONFIG_PATHS = [
    Path(".mcp.json"),
    Path(".claude") / "mcp.json",
    Path.home() / ".lemoncrow" / "tui" / ".mcp.json",
]

# Host-authored MCP config locations outside the workspace/project scope above.
# Read unconditionally (not gated behind workspace_root) since these describe
# the operator's own machine-wide MCP setup, not anything project-specific.
# Kept as separate module attributes (rather than folded into
# _MCP_CONFIG_PATHS) since their shape differs (nested `projects.*.mcpServers`,
# a bounded glob over plugin install dirs) and so tests can isolate them
# independently of the flat `.mcp.json` sources above.
_CLAUDE_JSON_PATH = Path.home() / ".claude.json"
_CLAUDE_PLUGINS_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"
_CURSOR_MCP_JSON_PATH = Path.home() / ".cursor" / "mcp.json"

# Seconds to wait for a JSON-RPC response before treating the server as hung.
_RPC_TIMEOUT_SECONDS = 10.0
# Cap on the textual result returned from a tool call.
_MAX_TOOL_RESULT_CHARS = 64_000
_TRUST_OPT_IN_ENV = "LEMONCROW_MCP_ALLOW_UNTRUSTED"


def _trusted_roots() -> list[Path]:
    """Locations an MCP config may resolve under (or equal, for a single-file
    config) to be auto-spawned without opt-in: the LemonCrow home, the explicit
    workspace root, and the operator's own home-level host configs.
    """
    roots = [
        Path.home() / ".lemoncrow",
        _CLAUDE_JSON_PATH,
        _CLAUDE_PLUGINS_CACHE_DIR,
        _CURSOR_MCP_JSON_PATH,
    ]
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    if workspace:
        roots.append(Path(workspace))
    return roots


def _is_trusted_config_path(config_path: Path) -> bool:
    """True if the config may be auto-spawned without explicit opt-in.

    Auto-spawning servers declared by a `.mcp.json` in an untrusted working
    directory is arbitrary command execution: a repo the operator merely
    cloned or checked out could ship a `.mcp.json` that spawns anything the
    instant LemonCrow looks at it. That is the actual attack vector -- a
    repo-controlled config read from an untrusted cwd -- not the operator's
    own home-level host configs (``~/.claude.json``, installed Claude Code
    plugins under ``~/.claude/plugins``, Cursor's ``~/.cursor/mcp.json``),
    which the operator authored/approved themselves by installing those
    servers. Those are trusted unconditionally (see ``_trusted_roots``); only
    a workspace/repo-level config resolved outside a trusted root requires the
    operator to set ``LEMONCROW_MCP_ALLOW_UNTRUSTED``.
    """
    if os.environ.get(_TRUST_OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        resolved = config_path.resolve()
    except OSError:
        return False
    for root in _trusted_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MCPTool:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RPCError:
    """JSON-RPC error response, carried back to callers so the server's
    actual error message reaches the model instead of being swallowed."""

    message: str


def _add_mcp_servers(
    servers: Any,
    configs: list[MCPServerConfig],
    seen_names: set[str],
    source: Path,
) -> None:
    """Append stdio server entries from *servers* (a raw ``mcpServers`` dict) to
    *configs*, skipping names already in *seen_names* and LemonCrow's own entry
    (``SELF_SERVER_NAMES``). A url/http/sse entry has no ``command`` key and is
    silently skipped -- only stdio entries are ever spawned.
    """
    if not isinstance(servers, dict):
        return
    for name, cfg in servers.items():
        if name in seen_names:
            logger.debug("Skipping duplicate MCP server %s from %s", name, source)
            continue
        if name in SELF_SERVER_NAMES:
            logger.debug("Skipping LemonCrow's own MCP server entry %s from %s", name, source)
            continue
        if isinstance(cfg, dict) and cfg.get("command"):
            seen_names.add(name)
            configs.append(
                MCPServerConfig(
                    name=name,
                    command=str(cfg["command"]),
                    args=[str(a) for a in cfg.get("args", [])],
                    env={str(k): str(v) for k, v in cfg.get("env", {}).items()},
                )
            )


def _load_json_config(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - config load is best-effort
        logger.debug("Failed to load MCP config %s: %s", path, exc)
        return None


def discover_mcp_configs(workspace_root: Path | None = None) -> list[MCPServerConfig]:
    """Read MCP host configs and return the discovered stdio server configs.

    Sources, in precedence order (first-seen name wins, so earlier sources
    override later same-named ones):
      1. Workspace/project `.mcp.json` / `.claude/mcp.json` (``_MCP_CONFIG_PATHS``,
         plus the equivalents resolved under *workspace_root* when given --
         the caller's cwd, e.g. the gateway process, is not necessarily the
         workspace root).
      2. ``~/.claude.json``'s per-project ``projects.<workspace_root>.mcpServers``
         (only the project matching *workspace_root*, when given), then its
         top-level user-scope ``mcpServers``.
      3. Installed Claude Code plugin configs:
         ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/.mcp.json``.
      4. Cursor's global ``~/.cursor/mcp.json``.

    Only stdio entries (a dict with a ``command`` key) are returned. Default
    behavior (no argument, the TUI consumer) is unchanged aside from now also
    including the host-config sources above.
    """
    config_paths = list(_MCP_CONFIG_PATHS)
    if workspace_root is not None:
        for extra in (workspace_root / ".mcp.json", workspace_root / ".claude" / "mcp.json"):
            if extra not in config_paths:
                config_paths.append(extra)
    configs: list[MCPServerConfig] = []
    seen_names: set[str] = set()
    for config_path in config_paths:
        if not config_path.exists():
            continue
        if not _is_trusted_config_path(config_path):
            logger.info(
                "Skipping untrusted MCP config %s; set %s to auto-spawn its servers",
                config_path,
                _TRUST_OPT_IN_ENV,
            )
            continue
        data = _load_json_config(config_path)
        if isinstance(data, dict):
            _add_mcp_servers(data.get("mcpServers") or data.get("servers") or {}, configs, seen_names, config_path)

    # ~/.claude.json: Claude Code's own user-level config. Project scope is read
    # first so it can override the user-global entry read right after it.
    if _CLAUDE_JSON_PATH.exists() and _is_trusted_config_path(_CLAUDE_JSON_PATH):
        claude_json = _load_json_config(_CLAUDE_JSON_PATH)
        if isinstance(claude_json, dict):
            if workspace_root is not None:
                projects = claude_json.get("projects")
                if isinstance(projects, dict):
                    project_keys = {str(workspace_root)}
                    try:
                        project_keys.add(str(workspace_root.resolve()))
                    except OSError:
                        pass
                    for key in project_keys:
                        project_entry = projects.get(key)
                        if isinstance(project_entry, dict):
                            _add_mcp_servers(project_entry.get("mcpServers"), configs, seen_names, _CLAUDE_JSON_PATH)
            _add_mcp_servers(claude_json.get("mcpServers"), configs, seen_names, _CLAUDE_JSON_PATH)

    # Installed Claude Code plugin MCP configs. Bounded-depth glob (not a
    # recursive walk of home): cache/<marketplace>/<plugin>/<version>/.mcp.json.
    if _CLAUDE_PLUGINS_CACHE_DIR.is_dir():
        for plugin_config in sorted(_CLAUDE_PLUGINS_CACHE_DIR.glob("*/*/*/.mcp.json")):
            if not _is_trusted_config_path(plugin_config):
                continue
            data = _load_json_config(plugin_config)
            if isinstance(data, dict):
                _add_mcp_servers(
                    data.get("mcpServers") or data.get("servers") or {}, configs, seen_names, plugin_config
                )

    # Cursor IDE's global config.
    if _CURSOR_MCP_JSON_PATH.exists() and _is_trusted_config_path(_CURSOR_MCP_JSON_PATH):
        data = _load_json_config(_CURSOR_MCP_JSON_PATH)
        if isinstance(data, dict):
            _add_mcp_servers(data.get("mcpServers"), configs, seen_names, _CURSOR_MCP_JSON_PATH)

    return configs


class MCPServerProcess:
    """Manages a spawned MCP server process communicating over stdio JSON-RPC."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._tools: list[MCPTool] = []
        self._request_id = 0
        self._rpc_lock = threading.Lock()

    def start(self) -> bool:
        """Start the server subprocess. Returns True if successful."""
        try:
            env = os.environ.copy()
            env.update(self.config.env)
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            # Initialize with JSON-RPC handshake
            self._initialize()
            return True
        except Exception as exc:  # noqa: BLE001 - spawn is best-effort
            logger.debug("Failed to start MCP server %s: %s", self.config.name, exc)
            return False

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and return the matching response's result.

        Reads stdout lines until the response whose ``id`` matches this
        request's id, skipping notifications and server-to-client requests
        (which carry a ``method``) so an interleaved ``notifications/message``
        emitted before the response does not desynchronize the stream. An
        ``error`` response is returned as :class:`_RPCError` so the server's
        message can be propagated instead of silently dropped.
        """
        with self._rpc_lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                return None
            self._request_id += 1
            request_id = self._request_id
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            try:
                line = json.dumps(request) + "\n"
                self._proc.stdin.write(line.encode())
                self._proc.stdin.flush()
                while True:
                    response_line = self._read_response_line()
                    if not response_line:
                        return None
                    try:
                        resp = json.loads(response_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(resp, dict):
                        continue
                    # Skip notifications and server->client requests: both
                    # carry a "method"; only our response carries our id.
                    if "method" in resp or resp.get("id") != request_id:
                        continue
                    error = resp.get("error")
                    if error is not None:
                        message = error.get("message") if isinstance(error, dict) else str(error)
                        return _RPCError(str(message))
                    return resp.get("result")
            except Exception as exc:  # noqa: BLE001 - rpc is best-effort
                logger.debug("MCP RPC error for %s: %s", self.config.name, exc)
        return None

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification: no ``id``, no response expected."""
        with self._rpc_lock:
            if self._proc is None or self._proc.stdin is None:
                return
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params:
                message["params"] = params
            try:
                self._proc.stdin.write((json.dumps(message) + "\n").encode())
                self._proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001 - notify is best-effort
                logger.debug("MCP notify error for %s: %s", self.config.name, exc)

    def _read_response_line(self) -> bytes | None:
        """Read one response line with a timeout; terminate a hung server.

        A bare ``readline()`` blocks forever if the child never replies, which
        wedges session startup. Read on a background thread and bound the wait
        with ``queue.get``; on timeout the child is terminated and ``None`` is
        returned so the caller falls back to the no-result path.
        """
        stdout = self._proc.stdout if self._proc is not None else None
        if stdout is None:
            return None
        result: queue.Queue[bytes | None] = queue.Queue(maxsize=1)

        def _read() -> None:
            try:
                result.put(stdout.readline())
            except Exception:  # noqa: BLE001 - reader thread is best-effort
                result.put(None)

        threading.Thread(target=_read, daemon=True).start()
        try:
            return result.get(timeout=_RPC_TIMEOUT_SECONDS)
        except queue.Empty:
            logger.debug("MCP server %s timed out; terminating", self.config.name)
            self.stop()
            return None

    def _initialize(self) -> None:
        """Send initialize + notifications/initialized to complete handshake."""
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "lemoncrow", "version": "0.1.0"},
            },
        )
        if result and not isinstance(result, _RPCError):
            self._notify("notifications/initialized")

    def list_tools(self) -> list[MCPTool]:
        """Fetch tool definitions from the server."""
        result = self._rpc("tools/list")
        tools = []
        if result and isinstance(result, dict):
            for t in result.get("tools", []):
                tools.append(
                    MCPTool(
                        server_name=self.config.name,
                        name=str(t.get("name", "")),
                        description=str(t.get("description", "")),
                        input_schema=dict(t.get("inputSchema", {})),
                    )
                )
        self._tools = tools
        return tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        max_chars: int | None = _MAX_TOOL_RESULT_CHARS,
    ) -> str:
        """Call a tool and return the result as a string.

        ``max_chars`` caps the returned text (default: the loader's historical
        cap). Pass ``None`` for the full, uncapped text -- e.g. the gateway's
        MCP proxy tool does this so its own dispatch-layer spill/compaction
        bounds the result instead of the overflow being silently dropped here.
        """
        result = self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if isinstance(result, _RPCError):
            return f"Error: MCP tool call failed for {tool_name}: {result.message}"
        if result is None:
            return f"Error: MCP tool call failed for {tool_name}"
        # MCP tools return content blocks
        if isinstance(result, dict):
            content = result.get("content", [])
            if isinstance(content, list):
                rendered = self._render_content_blocks(content)
            else:
                rendered = str(content)
            if result.get("isError"):
                rendered = f"Error: MCP tool {tool_name} reported failure: {rendered}"
            return rendered if max_chars is None else rendered[:max_chars]
        text = str(result)
        return text if max_chars is None else text[:max_chars]

    @staticmethod
    def _render_content_blocks(content: list[Any]) -> str:
        """Flatten MCP content blocks, keeping non-text blocks instead of dropping them."""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            block_type = item.get("type")
            if block_type == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(f"[{block_type or 'non-text'} block]")
        return "\n".join(parts)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        # Terminate the subprocess first so the OS closes the pipe ends.
        # This prevents the BufferedWriter's C-level finalize from hitting
        # a broken pipe during garbage collection later.
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Now that the child is dead, the OS has already closed both ends
        # of the pipe. Closing the Python wrappers is a formality — any
        # remaining OS error is harmless and suppressed here.
        for pipe_name in ("stdin", "stdout"):
            pipe = getattr(proc, pipe_name, None)
            if pipe is not None:
                _close_pipe_or_detach(pipe)
                setattr(proc, pipe_name, None)

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools


def _close_pipe_or_detach(pipe: Any) -> None:
    """Close a pipe silently, best-effort.

    First tries ``close()``; if the pipe is broken it falls back to
    ``detach()`` so the C-level IOBase finalize becomes a no-op, then
    closes the raw stream.  All errors are silently swallowed.
    """
    if pipe is None:
        return
    # Pipe/fd teardown only ever raises OSError (broken pipe, bad fd) or
    # ValueError (I/O on a closed/detached stream); catch exactly those so an
    # unexpected error still surfaces instead of being silently swallowed.
    try:
        pipe.close()
    except (OSError, ValueError):
        _detach_raw(pipe)


def _detach_raw(pipe: Any) -> None:
    """Detach the raw stream from a BufferedWriter/Reader and close it.

    After this the BufferedWriter's ``close()`` is safe to call (no-op).
    """
    import os as _os

    try:
        raw = pipe.detach()
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            _os.close(pipe.fileno())
        return
    if raw is not None:
        try:
            raw.close()
        except (OSError, ValueError):
            with contextlib.suppress(OSError, ValueError):
                _os.close(raw.fileno())


__all__ = ["MCPServerConfig", "MCPServerProcess", "MCPTool", "discover_mcp_configs"]
