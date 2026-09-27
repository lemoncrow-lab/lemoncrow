"""Run the client under a Python audit hook and report what it actually did.

This is the instrument behind ``test_packaging_audit.py``. It runs in its own
process for one reason: ``sys.addaudithook`` cannot be removed and sees every
event in the interpreter, so anything sharing the process -- pytest, the stub
HTTP server, another test -- would show up in the transcript as though the
client had done it. Alone in a subprocess, every recorded event is the client's.

Usage::

    python _audit_probe.py <scenario> <repo root>

with ``LEMONCROW_URL`` and ``LEMONCROW_TOKEN`` in the environment. It writes one
JSON document to stdout: the events it recorded, plus what was still alive when
the scenario finished.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any

#: The events worth recording. Everything else is noise for this question.
WATCHED = frozenset(
    {
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "subprocess.Popen",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.exec",
        "os.spawn",
        "open",
        "os.mkdir",
        "os.rename",
        "os.remove",
        "os.symlink",
        "os.chmod",
        "urllib.Request",
        "ftplib.connect",
        "http.client.connect",
        "webbrowser.open",
        "ctypes.dlopen",
        "importlib.import_module",
    }
)

_EVENTS: list[dict[str, Any]] = []
_RECORDING = [False]


def _describe(value: object) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return [_describe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): "<value>" for key in value}
    return f"<{type(value).__name__}>"


def _hook(event: str, args: tuple[object, ...]) -> None:
    if not _RECORDING[0] or event not in WATCHED:
        return
    # No allocation-heavy work and nothing that can raise another audit event:
    # a hook that recurses is a hook that hangs the interpreter.
    _EVENTS.append({"event": event, "args": [_describe(item) for item in args]})


def _mcp(repo_root: str) -> tuple[Any, Mapping[str, Any]]:
    """Start the same in-process MCP lifecycle the host gets."""
    from pathlib import Path

    from lemoncrow_client.config import load_config
    from lemoncrow_client.mcpserver import McpServer

    config = load_config(dict(os.environ), cwd=Path(repo_root))
    server = McpServer(config)
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    if initialized is None:
        raise AssertionError("initialize produced no response")
    return server, initialized


def _scenario_session(repo_root: str) -> dict[str, Any]:
    """Initialize once, use tools through that session, then close it."""
    server, initialized = _mcp(repo_root)
    dispatcher = server.dispatcher
    outcomes = [
        dispatcher.call("read", {"files": ["pkg/alpha.py"]}),
        dispatcher.call("code_search", {"query": "alpha"}),
        dispatcher.call("edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"}]}),
        dispatcher.call("read", {"files": ["pkg/alpha.py"]}),
        dispatcher.call("index", {}),
    ]
    result = {
        "bootstrapped": dispatcher.session.bootstrapped,
        "reason": dispatcher.session.state.reason,
        "instructions": str(initialized["result"].get("instructions", "")),
        "errors": [outcome.is_error for outcome in outcomes],
    }
    server.close()
    return result


def _scenario_bash(repo_root: str) -> dict[str, Any]:
    """The one scenario that is *supposed* to start a non-LemonCrow program."""
    server, _initialized = _mcp(repo_root)
    outcome = server.dispatcher.call("bash", {"command": "printf audited"})
    server.close()
    return {"text": outcome.content[0].get("text", "")}


def _scenario_offline(repo_root: str) -> dict[str, Any]:
    """No server at all: initialize still succeeds with reduced local tools."""
    server, initialized = _mcp(repo_root)
    dispatcher = server.dispatcher
    outcomes = [
        dispatcher.call("read", {"files": ["pkg/alpha.py"]}),
        dispatcher.call("grep", {"regex": "def alpha"}),
        dispatcher.call("code_search", {"query": "alpha"}),
    ]
    result = {
        "bootstrapped": dispatcher.session.bootstrapped,
        "line": str(initialized["result"].get("instructions", "")),
        "degraded": [outcome.degraded for outcome in outcomes],
        "errors": [outcome.is_error for outcome in outcomes],
    }
    server.close()
    return result


SCENARIOS = {
    "session": _scenario_session,
    "bash": _scenario_bash,
    "offline": _scenario_offline,
}


def main() -> int:
    scenario, repo_root = sys.argv[1], sys.argv[2]
    # Import before recording starts: import machinery reads hundreds of files
    # and none of it is the client doing anything.
    import lemoncrow_client.dispatcher
    import lemoncrow_client.mcpserver
    import lemoncrow_client.session  # noqa: F401

    threads_before = threading.active_count()
    sys.addaudithook(_hook)
    _RECORDING[0] = True
    try:
        result = SCENARIOS[scenario](repo_root)
        failure = ""
    except BaseException as exc:  # reported, never swallowed
        result = {}
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        _RECORDING[0] = False

    # "Did anything survive?" without starting a program to ask -- running
    # ``ps`` here would put a child in the answer it is meant to produce.
    # ``waitpid(-1, WNOHANG)`` raises when the process has no children at all,
    # which is exactly the condition being asserted.
    try:
        pid, _status = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        survivors = "none"
    except OSError as exc:
        survivors = f"unknown: {exc}"
    else:
        survivors = "running child" if pid == 0 else f"unreaped child {pid}"

    json.dump(
        {
            "scenario": scenario,
            "result": result,
            "failure": failure,
            "events": _EVENTS,
            "threads_before": threads_before,
            "threads_after": threading.active_count(),
            "thread_names": [thread.name for thread in threading.enumerate()],
            "surviving_children": survivors,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
