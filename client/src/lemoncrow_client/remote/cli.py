"""``lc runtime mcp serve|client|service|gateway``: publish this machine as a remote MCP server.

The gateway runs inside the client (Runtime Lite, or the hosted thin client);
cloudflared provides the public HTTPS URL. ``serve`` without ``--persistent``
runs a rotating quick tunnel in this terminal. ``--persistent`` routes a stable
hostname through the one shared named tunnel and installs two user units.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, NoReturn, TextIO

from . import service, tunnel
from .gateway import GatewayServer, runtime_mode
from .state import (
    PAIRING_CODE_TTL_TEXT,
    ConnectorBinding,
    OAuthStore,
    connector_slug,
    is_allowed_redirect_uri,
    load_all_connectors,
    load_connector_for_hostname,
    load_or_create_pairing_code,
    migrate_legacy_state,
    pairing_path,
    remove_connector,
    reset_file,
    save_connector,
    state_path,
    store_root_from,
)

COMMANDS: Final[frozenset[str]] = frozenset({"serve", "client", "service", "gateway"})

# Redirects of the hosted chat clients that ask for a user-defined client ID
# instead of registering dynamically. Each is still an exact-match allowlist.
CONNECTOR_REDIRECT_URIS: Final[tuple[str, ...]] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chat.openai.com/connector_platform_oauth_redirect",
)

_CLIENT_HINTS: Final[tuple[tuple[str, str], ...]] = (
    ("ChatGPT", "Settings -> Plugins -> Browse Plugins -> + -> Create"),
    ("Claude", "Settings -> Connectors -> Add custom connector"),
    ("Cursor / VS Code / Zed / ...", "add a remote (streamable-HTTP) MCP server"),
)
_RULE: Final[str] = "-" * 64
_MIN_PAIRING_CODE: Final[int] = 8


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(f"{self.prog}: {message}")


def _is_loopback(host: str) -> bool:
    return host == "localhost" or host.startswith("127.")


def _parser() -> _Parser:
    parser = _Parser(prog="lc runtime mcp", add_help=False)
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)

    serve = commands.add_parser("serve", add_help=False)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--pairing-code", default=None)
    serve.add_argument("--new-pairing-code", action="store_true")
    serve.add_argument("--reset", action="store_true")
    serve.add_argument("--no-tunnel", action="store_true", default=None)
    serve.add_argument("--tunnel", dest="no_tunnel", action="store_false")  # the default; kept for old scripts
    serve.add_argument("--no-auth", action="store_true")
    serve.add_argument("--persistent", action="store_true")
    serve.add_argument("--hostname", default=None)
    serve.add_argument("--reset-tunnel", action="store_true")
    serve.add_argument("--foreground", action="store_true")

    client = commands.add_parser("client", add_help=False)
    client.add_argument("--redirect-uri", action="append", default=[])
    client.add_argument("--hostname", default=None)

    gateway = commands.add_parser("gateway", add_help=False)
    gateway.add_argument("--port", type=int, default=None)
    gateway.add_argument("--host", default="127.0.0.1")

    manage = commands.add_parser("service", add_help=False)
    actions = manage.add_subparsers(dest="action", parser_class=_Parser)
    for name in ("list", "repair", "start", "stop", "uninstall"):
        actions.add_parser(name, add_help=False)
    restart = actions.add_parser("restart", add_help=False)
    restart.add_argument("hostname", nargs="?")
    restart.add_argument("--new-pairing-code", action="store_true")
    restart.add_argument("--reset", action="store_true")
    for name in ("code", "remove"):
        actions.add_parser(name, add_help=False).add_argument("hostname", nargs="?")
    logs = actions.add_parser("logs", add_help=False)
    logs.add_argument("-n", "--lines", type=int, default=50)
    logs.add_argument("--no-follow", action="store_true")
    return parser


USAGE: Final[str] = """usage:
  lc runtime mcp serve [--no-tunnel] [--no-auth] [--port N] [--pairing-code CODE]
                       [--new-pairing-code] [--reset]
  lc runtime mcp serve --persistent [--hostname HOST] [--foreground] [--reset-tunnel]
                       [--new-pairing-code] [--reset]
  lc runtime mcp client [--hostname HOST] [--redirect-uri URI ...]
  lc runtime mcp service list|repair|start|stop|logs|uninstall
  lc runtime mcp service restart|code|remove [HOST]

Publishes this workspace as a remote MCP server over OAuth 2.1 (pairing code).
The gateway runs inside this client: Workspaces Lite by default, or the hosted
thin client when Workspaces is installed in hosted mode. No LemonCrow server.
A public URL needs cloudflared on PATH; --no-tunnel serves loopback only.
--persistent keeps a stable https://HOST/mcp through a Cloudflare named tunnel
and installs two user services (systemd or launchd).
"""


def remote_main(
    arguments: Sequence[str],
    sink: TextIO,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    if not arguments or arguments[0] not in COMMANDS or any(item in {"-h", "--help"} for item in arguments):
        sink.write(USAGE)
        return 0 if arguments else 2
    env = dict(os.environ if environment is None else environment)
    workspace = (cwd or Path.cwd()).resolve()
    try:
        args = _parser().parse_args(list(arguments))
        if args.command == "serve":
            return _serve(args, sink, env, workspace)
        if args.command == "client":
            return _client(args, sink, env)
        if args.command == "gateway":
            return _gateway(args, sink, env)
        return _service(args, sink, env)
    except _UsageError as exc:
        sink.write(f"{exc}\n")
        return 2
    except (service.ServiceError, tunnel.TunnelSetupError) as exc:
        sink.write(f"error: {exc}\n")
        return 1


# -- serve ------------------------------------------------------------------------- #


def _narrator(sink: TextIO) -> Callable[[str], None]:
    def narrate(message: str) -> None:
        sink.write(f"  {message}\n")
        sink.flush()

    return narrate


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _block(server: GatewayServer, process: subprocess.Popen[str] | None, sink: TextIO) -> int:
    """Serve until Ctrl-C; with a tunnel, also stop when the tunnel dies.

    A dead tunnel leaves a gateway that answers locally while every public
    request fails, so exiting non-zero is what lets a supervisor restart both.
    """

    if process is None:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    server.start()
    interrupted = False
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        interrupted = True
        returncode = 0
    finally:
        _stop(process)
        server.close()
    if interrupted:
        return 0
    sink.write(f"  tunnel process exited unexpectedly (code {returncode}); stopping the gateway.\n")
    return 1


def _hints(sink: TextIO) -> None:
    for name, where in _CLIENT_HINTS:
        sink.write(f"       {name}:  {where}\n")


def _serve(args: argparse.Namespace, sink: TextIO, env: dict[str, str], workspace: Path) -> int:
    if args.no_auth and (args.pairing_code is not None or args.reset or args.new_pairing_code):
        raise _UsageError("--no-auth cannot be combined with --pairing-code, --new-pairing-code or --reset")
    if args.no_auth and args.no_tunnel is False:
        # A public tunnel URL with no authentication hands anyone who learns it host bash.
        raise _UsageError(
            "--no-auth is loopback only and cannot be combined with --tunnel; drop --no-auth to pair over OAuth"
        )
    if args.new_pairing_code and args.pairing_code is not None:
        raise _UsageError("--new-pairing-code cannot be combined with --pairing-code")
    if args.pairing_code is not None and len(args.pairing_code.strip()) < _MIN_PAIRING_CODE:
        # The pairing code is the only secret in front of shell-grade tools.
        raise _UsageError(f"--pairing-code must be at least {_MIN_PAIRING_CODE} characters")
    if args.reset_tunnel and not args.persistent:
        raise _UsageError("--reset-tunnel requires --persistent")
    if args.foreground and not args.persistent:
        raise _UsageError("--foreground requires --persistent")
    if not _is_loopback(args.host):
        raise _UsageError("serve binds loopback only; publish it through a tunnel, never a public address")
    store_root = store_root_from(env)
    if args.persistent:
        return _serve_persistent(args, sink, env, workspace, store_root)

    code: str | None = None
    if not args.no_auth:
        migrate_legacy_state(store_root)
        if args.reset:
            removed = reset_file(state_path(store_root))
            reset_file(pairing_path(store_root))
            sink.write(f"  Reset OAuth state ({'removed' if removed else 'nothing to remove'}).\n")
        code = args.pairing_code or load_or_create_pairing_code(pairing_path(store_root), rotate=args.new_pairing_code)
    try:
        server = GatewayServer(
            environment=env,
            host=args.host,
            port=args.port or 0,
            single=ConnectorBinding(hostname="local", workspace=str(workspace)),
            pairing_code=args.pairing_code,
            open_access=args.no_auth,
        )
    except OSError as exc:
        sink.write(f"could not bind {args.host}:{args.port or '(auto)'}: {exc}\n")
        return 1
    try:
        process: subprocess.Popen[str] | None = None
        url: str | None = None
        if not (args.no_tunnel or args.no_auth):
            binary = tunnel.resolve_cloudflared(store_root)
            if binary is None:
                sink.write(tunnel.missing_cloudflared_help(server.port))
                return 1
            sink.write("  Starting cloudflared quick tunnel...\n")
            sink.flush()
            process, url = tunnel.start_quick_tunnel(binary, server.port)
            if url is None:
                sink.write("  cloudflared printed no tunnel URL in time; follow the manual steps below.\n")
        _quick_banner(sink, args, server, code, url, runtime_mode(env), workspace)
        return _block(server, process, sink)
    finally:
        server.close()


def _quick_banner(
    sink: TextIO,
    args: argparse.Namespace,
    server: GatewayServer,
    code: str | None,
    url: str | None,
    mode: str,
    workspace: Path,
) -> None:
    auth = "None (no auth)" if args.no_auth else "OAuth"
    sink.write(
        f"\n  {_RULE}\n  LemonCrow remote MCP server ({'NO AUTH' if args.no_auth else 'OAuth 2.1'})\n  {_RULE}\n"
    )
    if code is not None:
        sink.write(f"  Pairing code:    {code}\n")
        if args.pairing_code is None:
            sink.write(f"                   {PAIRING_CODE_TTL_TEXT} (restart serve for a fresh one)\n")
        else:
            sink.write("                   single use for this server process\n")
    sink.write(f"  Local server:    {server.url}/mcp\n")
    sink.write(f"  Workspaces:      {'hosted thin client' if mode == 'hosted' else 'Workspaces Lite'}\n")
    sink.write(f"  Workspace:       {workspace}\n\n")
    if url is not None:
        sink.write(f"  MCP server URL:  {url}/mcp\n  Authentication:  {auth}\n\n")
        sink.write("  1. Add it as a remote MCP server in any client that takes a URL:\n")
        _hints(sink)
        if code is not None:
            sink.write("  2. Approve the browser OAuth page with the pairing code above.\n")
        sink.write("\n  This quick-tunnel URL rotates on every restart; --persistent keeps one stable URL.\n")
    elif args.no_auth:
        sink.write("  Loopback only: --no-auth never opens a tunnel. Point a local MCP client at the URL above.\n")
    else:
        sink.write("  1. Expose it through a tunnel (in another terminal):\n")
        sink.write(f"       cloudflared tunnel --url http://localhost:{server.port}\n")
        sink.write("  2. Add https://<tunnel-host>/mcp as a remote MCP server:\n")
        _hints(sink)
        sink.write(f"     Authentication:  {auth}\n")
        if code is not None:
            sink.write("  3. Approve the browser OAuth page with the pairing code above.\n")
    if args.no_auth:
        sink.write("\n  NO AUTHENTICATION: any local process or user can reach shell-grade access to this machine.\n")
    else:
        sink.write("\n  This exposes shell-grade tool access over the tunnel. Keep the pairing code private;\n")
        sink.write("  stop the server (Ctrl-C) when you are done.\n")
    sink.write(f"  {_RULE}\n\n")
    sink.flush()


def _serve_persistent(
    args: argparse.Namespace, sink: TextIO, env: dict[str, str], workspace: Path, store_root: Path
) -> int:
    if args.no_tunnel:
        raise _UsageError("--persistent cannot be combined with --no-tunnel (--persistent is a tunnel mode)")
    if args.no_auth:
        raise _UsageError("--persistent cannot be combined with --no-auth")
    if args.pairing_code is not None:
        raise _UsageError("--pairing-code is one-off; use --new-pairing-code to rotate the stored code")
    if args.port is not None or args.host != "127.0.0.1":
        sink.write("  note: --port/--host are ignored with --persistent (LEMONCROW_MCP_GATEWAY_PORT sets the port)\n")
    hostname = (args.hostname or "").strip().lower()
    if not hostname:
        configured = load_all_connectors(store_root)
        if not configured:
            raise _UsageError("the first --persistent run needs --hostname <a hostname you manage in Cloudflare>")
        if len(configured) > 1:
            names = ", ".join(binding.hostname for binding in configured)
            raise _UsageError(f"several connector hostnames are configured; pass --hostname: {names}")
        hostname = configured[0].hostname
    try:
        save_connector(ConnectorBinding(hostname=hostname, workspace=str(workspace)), store_root=store_root)
    except ValueError as exc:
        raise _UsageError(str(exc)) from exc
    scope = connector_slug(hostname)
    migrate_legacy_state(store_root, scope)
    if args.reset:
        removed = reset_file(state_path(store_root, scope))
        reset_file(pairing_path(store_root, scope))
        sink.write(f"  Reset OAuth state for {hostname} ({'removed' if removed else 'nothing to remove'}).\n")
    code = load_or_create_pairing_code(pairing_path(store_root, scope), rotate=args.new_pairing_code)
    port = service.origin_port(env)
    binary = tunnel.resolve_cloudflared(store_root)
    if binary is None:
        sink.write(tunnel.missing_cloudflared_help(port))
        return 1
    narrate = _narrator(sink)
    state = tunnel.provision_shared_tunnel(
        store_root=store_root, hostname=hostname, binary=binary, narrate=narrate, reset=args.reset_tunnel
    )
    supervised = not args.foreground and service.supervisor_kind() is not None
    if not args.foreground and not supervised:
        sink.write("  note: no systemd/launchd user session; serving in this terminal instead.\n")
    if supervised:
        unit = service.register_shared_service(
            binary=binary, state=state, port=port, store_root=store_root, environment=env, narrate=narrate
        )
        sink.write(f"\n  {_RULE}\n  LemonCrow remote MCP connector (OAuth 2.1)\n  {_RULE}\n")
        sink.write(f"  Pairing code:    {code}\n")
        sink.write(f"                   {PAIRING_CODE_TTL_TEXT} (`lc runtime mcp service code` prints a fresh one)\n")
        sink.write(f"  MCP server URL:  https://{hostname}/mcp\n")
        sink.write(f"  Gateway:         http://127.0.0.1:{port} ({runtime_mode(env)})\n")
        sink.write(f"  Services:        {unit}\n  Workspace:       {workspace}\n\n")
        sink.write("  1. Add it as a remote MCP server in any client that takes a URL:\n")
        _hints(sink)
        sink.write("  2. Approve the browser OAuth page with the pairing code above.\n\n  Manage:\n")
        for hint in service.management_hints(unit):
            sink.write(f"      {hint}\n")
        sink.write(f"  {_RULE}\n")
        return 0
    try:
        server = GatewayServer(environment=env, port=port)
    except OSError as exc:
        sink.write(f"could not bind 127.0.0.1:{port}: {exc} (is the service running? lc runtime mcp service stop)\n")
        return 1
    try:
        process = tunnel.start_named_tunnel(binary, state, port)
        sink.write(f"  Pairing code:    {code}\n  {hostname} -> shared tunnel -> gateway :{port}\n")
        sink.flush()
        return _block(server, process, sink)
    finally:
        server.close()


# -- client / gateway ------------------------------------------------------------ #


def _client(args: argparse.Namespace, sink: TextIO, env: dict[str, str]) -> int:
    uris = list(args.redirect_uri) or list(CONNECTOR_REDIRECT_URIS)
    for uri in uris:
        if not is_allowed_redirect_uri(uri):
            raise _UsageError(f"redirect_uri must be https (or http loopback): {uri}")
    store_root = store_root_from(env)
    scope = connector_slug(args.hostname) if args.hostname else None
    migrate_legacy_state(store_root, scope)
    record = OAuthStore(state_path(store_root, scope)).ensure_user_client(uris)
    sink.write(f"\n  Client ID:      {record['client_id']}\n")
    sink.write("  Client secret:  leave empty (public client, PKCE)\n\n")
    sink.write("  Paste the client ID into your connector's OAuth settings. Registered redirect URIs:\n")
    for uri in record["redirect_uris"]:
        sink.write(f"    - {uri}\n")
    return 0


def _gateway(args: argparse.Namespace, sink: TextIO, env: dict[str, str]) -> int:
    """What the persistent gateway unit runs: every connector hostname, one listener."""

    if not _is_loopback(args.host):
        raise _UsageError("the gateway binds loopback only; the tunnel is its public side")
    port = args.port if args.port is not None else service.origin_port(env)
    try:
        server = GatewayServer(environment=env, host=args.host, port=port)
    except OSError as exc:
        sink.write(f"could not bind {args.host}:{port}: {exc}\n")
        return 1
    sink.write(f"LemonCrow remote MCP gateway ({runtime_mode(env)}) on {server.url}\n")
    sink.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# -- service -------------------------------------------------------------------------- #


def _connector(store_root: Path, hostname: str | None) -> ConnectorBinding:
    if hostname:
        binding = load_connector_for_hostname(store_root, hostname)
        if binding is None:
            raise _UsageError(f"no persistent MCP connector for {hostname!r}")
        return binding
    connectors = load_all_connectors(store_root)
    if not connectors:
        raise _UsageError("no persistent MCP connectors are configured")
    if len(connectors) > 1:
        names = ", ".join(binding.hostname for binding in connectors)
        raise _UsageError(f"several connector hostnames are configured; name one: {names}")
    return connectors[0]


def _service(args: argparse.Namespace, sink: TextIO, env: dict[str, str]) -> int:
    store_root = store_root_from(env)
    action = args.action
    if action is None:
        raise _UsageError(
            "service needs an action: list, repair, start, stop, restart, code, logs, remove or uninstall"
        )
    if action == "list":
        connectors = load_all_connectors(store_root)
        sink.write(f"  Persistent MCP connectors: {len(connectors)}\n")
        if not connectors:
            sink.write("  None configured. Publish one with: lc runtime mcp serve --persistent --hostname <host>\n")
            return 0
        state, enabled, pid = service.shared_service_state()
        sink.write(f"  shared tunnel  {state} ({enabled}){f'  pid={pid}' if pid else ''}\n")
        for binding in connectors:
            sink.write(f"    https://{binding.hostname}/mcp  {binding.workspace}\n")
        return 0
    if action == "repair":
        unit = service.repair_shared_service(store_root=store_root, environment=env, narrate=_narrator(sink))
        if unit is None:
            sink.write("  No persistent MCP connectors are configured; nothing to repair.\n")
        else:
            sink.write(f"  Repaired the persistent MCP gateway and tunnel ({unit}).\n")
        return 0
    if action == "uninstall":
        # Units only: connector bindings and pairing state stay for `service repair`.
        if service.remove_shared_service():
            sink.write("  Removed the persistent MCP gateway and tunnel services; connector state is kept.\n")
        else:
            sink.write("  No persistent MCP services are installed.\n")
        return 0
    if action in {"start", "stop"}:
        service.control_shared_service(action)
        sink.write(f"  {'Started' if action == 'start' else 'Stopped'} the persistent MCP gateway and tunnel\n")
        return 0
    if action == "logs":
        command = service.log_command(lines=args.lines, follow=not args.no_follow, store_root=store_root)
        os.execvp(command[0], command)
    binding = _connector(store_root, args.hostname)
    scope = connector_slug(binding.hostname)
    migrate_legacy_state(store_root, scope)
    if action == "code":
        sink.write(load_or_create_pairing_code(pairing_path(store_root, scope)) + "\n")
        return 0
    if action == "remove":
        remove_connector(store_root, binding.hostname)
        reset_file(state_path(store_root, scope))
        reset_file(pairing_path(store_root, scope))
        sink.write(f"  Removed connector {binding.hostname}.\n")
        if not load_all_connectors(store_root) and service.remove_shared_service():
            sink.write("  Removed the shared gateway and tunnel services: no connectors remain.\n")
        return 0
    # restart
    code: str | None = None
    if args.reset:
        removed = reset_file(state_path(store_root, scope))
        reset_file(pairing_path(store_root, scope))
        sink.write(f"  Reset OAuth state for {binding.hostname} ({'removed' if removed else 'nothing to remove'}).\n")
    if args.reset or args.new_pairing_code:
        code = load_or_create_pairing_code(pairing_path(store_root, scope), rotate=args.new_pairing_code)
    service.control_shared_service("restart")
    sink.write("  Restarted the persistent MCP gateway and tunnel\n")
    if code is not None:
        sink.write(f"  Pairing code:  {code}\n")
    return 0


__all__ = ["COMMANDS", "CONNECTOR_REDIRECT_URIS", "USAGE", "remote_main"]
