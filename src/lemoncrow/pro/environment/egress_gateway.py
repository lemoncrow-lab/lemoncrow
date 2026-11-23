"""A task network's only way out: an allowlisting HTTP proxy, plus TCP relays for previews.

This file runs by itself inside the gateway container (``python3 egress_gateway.py CONFIG``), standard library only
and Python 3.8 or newer (annotations are never evaluated), so it imports nothing from LemonCrow. The Workspaces server writes the configuration
(:func:`gateway_config`): the hosts the task was granted, the ports they may be reached on, and the relays.

* **Proxy.** ``CONNECT host:port`` (HTTPS) and absolute-form ``http://host/`` requests are served only for a host
  on the allowlist (exact name, no wildcard), only on an allowed port, and only when every address the name
  resolves to is public: a granted name that resolves to a loopback, private, link-local or metadata address is
  refused, so the proxy can never be steered at the host or the cloud's metadata service. Anything else is 403.
* **Relays.** Each relay forwards a port of this container to one port of a container on the task network (a
  preview in the sandbox). The server publishes only these ports, on the host's loopback.

The sandbox sits on an internal network with no route out; this container also joins an egress network. It logs
one line per decision (host and port, never a path, header or body).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import threading
from collections.abc import Sequence
from typing import Any

PROXY_PORT = 3128
DEFAULT_PORTS = (80, 443)
MAX_HEAD_BYTES = 64 * 1024
IDLE_SECONDS = 300.0
CONNECT_SECONDS = 15.0


def gateway_config(
    allow: Sequence[str],
    *,
    relays: Sequence[tuple[int, str, int]] = (),
    ports: Sequence[int] = DEFAULT_PORTS,
    proxy_port: int = PROXY_PORT,
) -> dict[str, Any]:
    """The configuration document the gateway reads (no secrets: names and ports only)."""

    return {
        "allow": sorted({normalize_host(host) for host in allow if normalize_host(host)}),
        "ports": sorted({int(port) for port in ports}),
        "proxy_port": int(proxy_port),
        "relays": [{"listen": int(listen), "host": host, "port": int(port)} for listen, host, port in relays],
    }


def normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def public_address(address: str) -> bool:
    """Whether *address* is a public unicast address (never loopback, private, link-local or reserved)."""

    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def decide(host: str, port: int, allow: Sequence[str], ports: Sequence[int]) -> str | None:
    """Why ``host:port`` is refused, or ``None`` when the allowlist admits it (before resolution)."""

    name = normalize_host(host)
    if not name:
        return "no host"
    try:
        ipaddress.ip_address(name.strip("[]"))
        return "an address, not a granted host name"
    except ValueError:
        pass
    if name not in allow:
        return "not granted to this task"
    if port not in ports:
        return f"port {port} is not allowed"
    return None


def resolve_public(host: str, port: int) -> list[tuple[Any, ...]]:
    """Addresses for ``host:port``; refused (``OSError``) when any of them is not public."""

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError("no address")
    for info in infos:
        if not public_address(str(info[4][0])):
            raise OSError("resolves to a non-public address")
    return infos


def parse_target(method: str, target: str) -> tuple[str, int, str]:
    """``(host, port, origin-form path)`` of a proxy request line; ``ValueError`` when it is not one."""

    if method == "CONNECT":
        host, sep, port = target.rpartition(":")
        if not sep or not port.isdigit():
            raise ValueError("CONNECT needs host:port")
        return host.strip("[]"), int(port), ""
    if not target.lower().startswith("http://"):
        raise ValueError("only absolute http:// targets and CONNECT are proxied")
    rest = target[len("http://") :]
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        raise ValueError("credentials in the target are refused")
    if authority.startswith("["):
        host, _, tail = authority[1:].partition("]")
        spelled = tail[1:] if tail.startswith(":") else ""
    else:
        host, _, spelled = authority.partition(":")
    if spelled and not spelled.isdigit():
        raise ValueError("bad port")
    return host, int(spelled) if spelled else 80, "/" + path if slash else "/"


def _log(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock, how in ((sink, socket.SHUT_WR), (source, socket.SHUT_RD)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def _splice(left: socket.socket, right: socket.socket) -> None:
    left.settimeout(IDLE_SECONDS)
    right.settimeout(IDLE_SECONDS)
    other = threading.Thread(target=_pipe, args=(right, left), daemon=True)
    other.start()
    _pipe(left, right)
    other.join(IDLE_SECONDS)
    for sock in (left, right):
        try:
            sock.close()
        except OSError:
            pass


def _read_head(client: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(8192)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEAD_BYTES:
            raise ValueError("request head too large")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _refuse(client: socket.socket, status: str, reason: str) -> None:
    body = ("lemoncrow: " + reason + "\n").encode("utf-8")
    try:
        client.sendall(
            ("HTTP/1.1 " + status + "\r\nContent-Type: text/plain\r\nConnection: close\r\n").encode("ascii")
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
    finally:
        client.close()


def _serve_proxy(client: socket.socket, config: dict[str, Any]) -> None:
    client.settimeout(CONNECT_SECONDS)
    try:
        head, rest = _read_head(client)
        lines = head.decode("iso-8859-1").split("\r\n")
        method, target, version = ([*lines[0].split(" "), "", "", ""])[:3]
        host, port, path = parse_target(method.upper(), target)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _refuse(client, "400 Bad Request", str(exc)[:200])
        return
    reason = decide(host, port, config["allow"], config["ports"])
    if reason is None:
        try:
            infos = resolve_public(host, port)
        except OSError as exc:
            reason = str(exc)
    if reason is not None:
        _log(f"deny {normalize_host(host)}:{port} ({reason})")
        _refuse(client, "403 Forbidden", f"egress to {normalize_host(host)}:{port} is refused: {reason}")
        return
    try:
        upstream = socket.create_connection(infos[0][4][:2], timeout=CONNECT_SECONDS)
    except OSError as exc:
        _refuse(client, "502 Bad Gateway", f"{host}:{port} did not answer: {exc}")
        return
    _log(f"allow {normalize_host(host)}:{port}")
    if method.upper() == "CONNECT":
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
    else:
        kept = [
            line for line in lines[1:] if line and not line.lower().startswith(("proxy-", "connection:", "keep-alive:"))
        ]
        request = f"{method} {path} {version or 'HTTP/1.1'}\r\n"
        upstream.sendall((request + "\r\n".join([*kept, "Connection: close"]) + "\r\n\r\n").encode("iso-8859-1"))
        if rest:
            upstream.sendall(rest)
    _splice(client, upstream)


def _serve_relay(client: socket.socket, host: str, port: int) -> None:
    try:
        upstream = socket.create_connection((host, port), timeout=CONNECT_SECONDS)
    except OSError:
        client.close()
        return
    _splice(client, upstream)


def _listen(port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(128)
    return server


def _accept_loop(server: socket.socket, handle: Any, *args: Any) -> None:
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client, *args), daemon=True).start()


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: egress_gateway.py CONFIG.json\n")
        return 2
    with open(argv[0], encoding="utf-8") as stream:
        config = json.load(stream)
    config["allow"] = [normalize_host(host) for host in config.get("allow", [])]
    config["ports"] = [int(port) for port in config.get("ports", DEFAULT_PORTS)]
    threads = [
        threading.Thread(
            target=_accept_loop, args=(_listen(int(config.get("proxy_port", PROXY_PORT))), _serve_proxy, config)
        )
    ]
    for relay in config.get("relays", []):
        listener = _listen(int(relay["listen"]))
        threads.append(
            threading.Thread(target=_accept_loop, args=(listener, _serve_relay, str(relay["host"]), int(relay["port"])))
        )
    for thread in threads:
        thread.daemon = True
        thread.start()
    _log(f"lemoncrow egress gateway: {len(config['allow'])} granted host(s), {len(threads) - 1} relay(s)")
    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
