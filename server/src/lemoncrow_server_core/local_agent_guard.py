"""Refuse human review judgment that arrives from a coding-agent process.

On loopback the machine token is readable by every process of the same user,
including the coding agent whose work is under review, and the human's own
``lc review --mark`` presents that same token. A token cannot tell them apart;
the calling process can. For a judgment write over the machine token the
server resolves the process that owns the client end of the connection and
walks its ancestry for the same agent-session evidence the CLI guard uses,
plus the agent executables themselves (an agent's shell can clear its own
environment but stays under the agent binary).

This raises the bar; it is not a security boundary. A same-user process that
detaches from the agent's tree, or writes the Review store directly, is not
seen. The paired browser Reader is not checked here: it has its own
same-origin capability.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.core.foundation.paths import AGENT_SESSION_ENVS

logger = logging.getLogger(__name__)

_JUDGMENT_POST = re.compile(
    r"^/api/reviews/[^/]+/"
    r"(?:marks|marks/bulk|finish|outcome|annotations|verification/waivers|landing/arm|github/publish)$"
)
_JUDGMENT_PATCH = re.compile(r"^/api/annotations/[^/]+$")

_MARKER_HOSTS: dict[str, str] = dict(AGENT_SESSION_ENVS)
#: Agent CLIs by executable name (Linux ``comm`` or argv[0] basename).
_AGENT_EXECUTABLES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "lemoncode": "lemoncode",
    "cursor-agent": "cursor",
    "copilot": "copilot",
    "hermes": "hermes",
    "antigravity": "antigravity",
    "agy": "antigravity",
}
_MAX_ANCESTRY = 64


def is_judgment_request(method: str, path: str) -> bool:
    """Whether this Review API write records human judgment (mirrors the CLI guard)."""

    verb = method.upper()
    if verb == "POST":
        return bool(_JUDGMENT_POST.match(path))
    if verb == "PATCH":
        return bool(_JUDGMENT_PATCH.match(path))
    return False


@dataclass(frozen=True)
class ProcessRecord:
    ppid: int
    names: tuple[str, ...]
    #: Agent-session environment variables present with a non-empty value.
    markers: tuple[str, ...]


def agent_host_in_ancestry(pid: int, lookup: Callable[[int], ProcessRecord | None]) -> str:
    """The agent host found on *pid* or any ancestor, or ``""`` without evidence."""

    seen: set[int] = set()
    current = pid
    for _ in range(_MAX_ANCESTRY):
        if current <= 1 or current in seen:
            return ""
        seen.add(current)
        record = lookup(current)
        if record is None:
            return ""
        for marker in record.markers:
            if marker in _MARKER_HOSTS:
                return _MARKER_HOSTS[marker]
        for name in record.names:
            host = _AGENT_EXECUTABLES.get(name)
            if host:
                return host
        current = record.ppid
    return ""


def caller_agent_host(pid: int, *, own: int, lookup: Callable[[int], ProcessRecord | None]) -> str:
    """Agent verdict for caller *pid* of the server running as *own*.

    The server's own process and every process that launched it are its
    operator, not the agent under review: an operator can already write the
    Review store directly, so trusting it opens nothing new.
    """

    current = own
    for _ in range(_MAX_ANCESTRY):
        if current == pid:
            return ""
        record = lookup(current) if current > 1 else None
        if record is None or record.ppid == current:
            break
        current = record.ppid
    return agent_host_in_ancestry(pid, lookup)


# -- Linux: /proc ---------------------------------------------------------


def _linux_client_inode(client_port: int, server_port: int) -> str:
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) < 10:
                        continue
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    remote_port = int(fields[2].rsplit(":", 1)[1], 16)
                    if local_port == client_port and remote_port == server_port:
                        return fields[9]
        except (OSError, ValueError):
            continue
    return ""


def _linux_owns_socket(pid: int | str, needle: str) -> bool:
    fd_dir = f"/proc/{pid}/fd"
    try:
        descriptors = os.listdir(fd_dir)
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(f"{fd_dir}/{descriptor}") == needle:
                return True
        except OSError:
            continue
    return False


def _linux_peer_pid(client_port: int, server_port: int) -> int | None:
    inode = _linux_client_inode(client_port, server_port)
    if not inode or inode == "0":
        return None
    needle = f"socket:[{inode}]"
    own = os.getpid()
    if _linux_owns_socket(own, needle):
        return own
    # lc-debt: scans every same-user process's fds per judgment write (rare);
    # upgrade to a netlink sock_diag query if judgment traffic grows.
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != own and _linux_owns_socket(entry, needle):
            return int(entry)
    return None


def _linux_record(pid: int) -> ProcessRecord | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return None
    open_paren, close_paren = stat.find(b"("), stat.rfind(b")")
    if open_paren < 0 or close_paren < 0:
        return None
    comm = stat[open_paren + 1 : close_paren].decode("utf-8", "replace")
    try:
        ppid = int(stat[close_paren + 2 :].split()[1])
    except (IndexError, ValueError):
        return None
    names = [comm]
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv0 = handle.read().split(b"\0", 1)[0].decode("utf-8", "replace")
        if argv0:
            names.append(os.path.basename(argv0))
    except OSError:
        pass
    markers: list[str] = []
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            for item in handle.read().split(b"\0"):
                key, _, value = item.partition(b"=")
                name = key.decode("utf-8", "replace")
                if name in _MARKER_HOSTS and value.strip():
                    markers.append(name)
    except OSError:
        pass
    return ProcessRecord(ppid=ppid, names=tuple(names), markers=tuple(markers))


# -- macOS / BSD: lsof + one ps sweep -------------------------------------


def _run(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _bsd_peer_pid(client_port: int) -> int | None:
    own = os.getpid()
    pids = {
        int(line)
        for line in _run(["lsof", "-nP", "-a", f"-iTCP:{client_port}", "-sTCP:ESTABLISHED", "-t"]).split()
        if line.isdigit()
    }
    if not pids:
        return None
    others = pids - {own}
    # Both ends match the client port; only the server's accepted end is ours.
    return min(others) if others else own


def _bsd_table() -> dict[int, ProcessRecord]:
    marker_pattern = re.compile(r"(?:^|\s)(" + "|".join(map(re.escape, _MARKER_HOSTS)) + r")=(\S)")
    table: dict[int, ProcessRecord] = {}
    for line in _run(["ps", "-E", "-ww", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        command = parts[2]
        argv0 = os.path.basename(command.split(None, 1)[0]) if command.strip() else ""
        markers = tuple(dict.fromkeys(match.group(1) for match in marker_pattern.finditer(command)))
        table[int(parts[0])] = ProcessRecord(ppid=int(parts[1]), names=(argv0,), markers=markers)
    return table


def peer_agent_host(transport: Any) -> str | None:
    """The agent host behind this loopback connection.

    ``""`` means the calling process and its ancestry show no agent evidence;
    ``None`` means the caller could not be resolved on this platform.
    """

    if transport is None:
        return None
    peer = transport.get_extra_info("peername")
    local = transport.get_extra_info("sockname")
    if not isinstance(peer, tuple) or not isinstance(local, tuple) or len(peer) < 2 or len(local) < 2:
        return None
    try:
        client_port, server_port = int(peer[1]), int(local[1])
    except (TypeError, ValueError):
        return None
    if sys.platform.startswith("linux"):
        pid = _linux_peer_pid(client_port, server_port)
        return None if pid is None else caller_agent_host(pid, own=os.getpid(), lookup=_linux_record)
    if sys.platform == "darwin" or "bsd" in sys.platform:
        pid = _bsd_peer_pid(client_port)
        if pid is None:
            return None
        if pid == os.getpid():
            return ""
        table = _bsd_table()
        return caller_agent_host(pid, own=os.getpid(), lookup=table.get) if table else None
    return None


__all__ = ["ProcessRecord", "agent_host_in_ancestry", "caller_agent_host", "is_judgment_request", "peer_agent_host"]
