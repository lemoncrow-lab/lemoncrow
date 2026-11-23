"""Refuse human review judgment that arrives from a coding-agent process.

On loopback the machine token is readable by every process of the same user,
including the coding agent whose work is under review, and the human's own
``lc review --mark`` presents that same token. A token cannot tell them apart;
the calling process can. For a judgment write over the machine token the
server resolves the process that owns the client end of the connection and
walks its ancestry for the same agent-session evidence the CLI guard uses,
plus the agent executables themselves (an agent's shell can clear its own
environment but stays under the agent binary).

One write from an agent passes: its own decision on an AI finding, recorded as
the agent's (:func:`is_attributed_agent_decision`). That claims no human
judgment, so it is not what this guard stops.

This raises the bar; it is not a security boundary. A same-user process that
detaches from the agent's tree, or writes the Review store directly, is not
seen. The paired browser Reader is not checked here: it has its own
same-origin capability.
"""

from __future__ import annotations

import json
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
    r"(?:marks|marks/bulk|finish|outcome|annotations|verification/waivers|landing/arm|github/publish"
    # Flow send schedules a verdict the server worker later records and finishes
    # without passing this guard; seen-coverage is signed into the record.
    r"|flow-send|flow-send/[^/]+/undo|seen-coverage)$"
)
_JUDGMENT_PATCH = re.compile(r"^/api/(?:annotations/[^/]+|review-settings)$")
# The reviewer's own settings (keys, reading view, theme): an agent must not change how the reviewer works.
_JUDGMENT_DELETE = re.compile(r"^/api/review-settings$")
# Where a Review's fixes go: an agent must not point the feedback at itself.
# The viewer's queue (snooze hides a Review from their own Your turn): a person's call, never an agent's.
# The machine's AI settings (provider, model, on/off, where jobs run) are the reviewer's to change too.
_JUDGMENT_PUT = re.compile(r"^/api/(?:reviews/[^/]+/(?:destination|queue-state)|review-intelligence/settings)$")

_MARKER_HOSTS: dict[str, str] = dict(AGENT_SESSION_ENVS)
#: Agent CLIs by executable name (Linux ``comm`` or argv[0] basename).
_AGENT_EXECUTABLES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "cursor-agent": "cursor",
    "copilot": "copilot",
    "hermes": "hermes",
    "antigravity": "antigravity",
    "agy": "antigravity",
}
_MAX_ANCESTRY = 64
#: The verdict for a caller that let go of its connection before it could be
#: identified: a closed client end holds no process, and a human's CLI waits for
#: its answer, so only a caller dodging the lookup ends up here.
UNIDENTIFIED_CALLER = "unidentified"
_CALLER_GONE = -1


# Deliberately broad (any spelling that could reach the decision route).
_APPROVAL_DECISION = re.compile(r"/agent-approvals/.*decision")


def is_approval_decision(method: str, path: str) -> bool:
    """Whether this request decides a risky agent action (P12-AGENT).

    Stricter than :func:`is_judgment_request`: an unresolvable caller is refused
    too (fail closed), because the agent whose action is waiting holds the same
    machine token.
    """

    return method.upper() != "GET" and bool(_APPROVAL_DECISION.search(path))


def is_judgment_request(method: str, path: str) -> bool:
    """Whether this Review API write records human judgment (mirrors the CLI guard)."""

    verb = method.upper()
    if verb == "POST":
        return bool(_JUDGMENT_POST.match(path))
    if verb == "PATCH":
        return bool(_JUDGMENT_PATCH.match(path))
    if verb == "PUT":
        return bool(_JUDGMENT_PUT.match(path))
    if verb == "DELETE":
        return bool(_JUDGMENT_DELETE.match(path))
    return False


_AGENT_DECISION_PATH = re.compile(r"^/api/annotations/[^/]+$")
#: Everything `lc review finding N fixed|dismissed|open` sends from an agent's session.
_AGENT_DECISION_FIELDS = frozenset(
    {
        "finding_disposition",
        "finding_disposition_reason",
        "finding_disposition_evidence",
        "expected_revision_id",
        "actor",
        "agent_host",
    }
)
#: Confirming a finding (``confirmed_by_reviewer``) stays the reviewer's call.
_AGENT_DISPOSITIONS = frozenset({"fixed", "dismissed", "open"})


def may_be_agent_decision(method: str, path: str) -> bool:
    """Whether this write could be an agent's own finding decision, so its body is worth reading."""

    return method.upper() == "PATCH" and bool(_AGENT_DECISION_PATH.match(path))


def is_attributed_agent_decision(method: str, path: str, body: bytes, host: str) -> bool:
    """Whether this write is the calling agent's own decision on one finding, recorded as that agent's.

    ``lc review finding`` run in an agent's session sends ``actor: agent`` and the agent's host; the Review
    keeps that version as the agent's (``changed_by_actor = agent``) and the Reader says "Fixed by Claude".
    Only the decision fields qualify, never confirming a finding, and only when the host the body names is
    the one found in the caller's ancestry.
    """

    if not host or not may_be_agent_decision(method, path):
        return False
    try:
        value = json.loads(body)
    except ValueError:
        return False
    return (
        isinstance(value, dict)
        and value.get("actor") == "agent"
        and value.get("agent_host") == host
        and value.get("finding_disposition") in _AGENT_DISPOSITIONS
        and set(value) <= _AGENT_DECISION_FIELDS
    )


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


def _linux_client_inode(client_port: int, server_port: int) -> str | None:
    """The client end's socket inode; ``""`` when no live socket holds it, None if unreadable."""

    readable = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as handle:
                readable = True
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) < 10:
                        continue
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    remote_port = int(fields[2].rsplit(":", 1)[1], 16)
                    if local_port == client_port and remote_port == server_port:
                        # A closed end lingers (FIN_WAIT/TIME_WAIT) with inode 0.
                        return "" if fields[9] == "0" else fields[9]
        except (OSError, ValueError):
            continue
    return "" if readable else None


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
    """The process holding the client end, ``_CALLER_GONE`` once none does, None if /proc can't say."""

    inode = _linux_client_inode(client_port, server_port)
    if inode is None:
        return None
    if not inode:
        return _CALLER_GONE
    needle = f"socket:[{inode}]"
    own = os.getpid()
    if _linux_owns_socket(own, needle):
        return own
    # lc-debt: scans every same-user process's fds per judgment write (rare);
    # upgrade to a netlink sock_diag query if judgment traffic grows.
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != own and _linux_owns_socket(entry, needle):
            return int(entry)
    return _CALLER_GONE


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
    ``UNIDENTIFIED_CALLER`` means the caller closed its end before it could be
    looked up; ``None`` means the caller could not be resolved on this platform.
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
        if pid is None:
            return None
        if pid == _CALLER_GONE:
            return UNIDENTIFIED_CALLER
        return caller_agent_host(pid, own=os.getpid(), lookup=_linux_record)
    if sys.platform == "darwin" or "bsd" in sys.platform:
        pid = _bsd_peer_pid(client_port)
        if pid is None:
            return None
        if pid == os.getpid():
            return ""
        table = _bsd_table()
        return caller_agent_host(pid, own=os.getpid(), lookup=table.get) if table else None
    return None


__all__ = [
    "UNIDENTIFIED_CALLER",
    "ProcessRecord",
    "agent_host_in_ancestry",
    "caller_agent_host",
    "is_attributed_agent_decision",
    "is_judgment_request",
    "may_be_agent_decision",
    "peer_agent_host",
]
