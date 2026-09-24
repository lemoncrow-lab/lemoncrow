"""``lc mcp`` — start the stdio MCP server, publish it remotely, or run diagnostics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import click

import lemoncrow
from lemoncrow.gateway.cli.commands._shared import _emit
from lemoncrow.gateway.cli.commands.mcp_serve import mcp_client_cmd, mcp_serve_cmd, mcp_service_group

_BENCHMARK_REQUIRED_TOOLS = frozenset({"read", "edit", "code_search", "bash"})


def probe_stdio_server(*, host: str = "claude", timeout: float = 30.0) -> dict[str, Any]:
    """Start the configured LemonCrow stdio command and verify its core MCP surface."""
    # Source/local installs expose the supported short command (`lc`) through
    # ~/.local/bin, while the underlying `lemoncrow` executable can live only
    # inside uv's private tool directory.  Accept either entry point so the
    # diagnostic probes the same CLI the user can actually invoke.
    executable = shutil.which("lemoncrow") or shutil.which("lc")
    if executable is None:
        return {"ok": False, "error": "LemonCrow CLI executable not found on PATH", "tools": []}
    requests = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "lemoncrow-benchmark-preflight", "version": lemoncrow.__version__},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            # ``tools/list`` is intentionally available while the thin client is
            # degraded, so it cannot prove the configured LemonCrow server is
            # reachable. Exercise one real server-side tool as part of the
            # preflight; a healthy empty result is fine, a degraded/refused
            # result is not.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "code_search",
                        "arguments": {"query": "LemonCrow MCP preflight", "limit": 1},
                    },
                }
            ),
        ]
    )
    # A health/preflight check is allowed to wait for a cold server-side view
    # bootstrap. The ordinary thin client deliberately fails open quickly, but
    # using that interactive startup budget here would turn a slow first sync
    # into a false "server_unreachable" diagnostic. Respect explicit operator
    # overrides while giving diagnostics a bounded cold-start budget by default.
    probe_env = dict(os.environ)
    probe_env.setdefault("LEMONCROW_STARTUP_BUDGET_S", str(timeout))
    probe_env.setdefault("LEMONCROW_REQUEST_TIMEOUT_S", str(timeout))
    try:
        completed = subprocess.run(
            [executable, "mcp", "--host", host],
            input=requests + "\n",
            capture_output=True,
            text=True,
            env=probe_env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"MCP process failed: {exc}", "tools": []}
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        return {"ok": False, "error": f"MCP process failed: {detail}", "tools": []}
    try:
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        by_id = {response.get("id"): response for response in responses if isinstance(response, dict)}
        initialize_result = by_id[1]["result"]
        server_name = initialize_result["serverInfo"]["name"]
        instructions = str(initialize_result.get("instructions") or "")
        tools = sorted(tool["name"] for tool in by_id[2]["result"]["tools"])
        probe_result = by_id[3].get("result", {})
        probe_error = by_id[3].get("error")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid MCP handshake response: {exc}", "tools": []}
    from lemoncrow_client.mcpserver import SERVER_NAME as expected_server_name

    missing = sorted(_BENCHMARK_REQUIRED_TOOLS - set(tools))
    if server_name != expected_server_name or missing:
        detail = (
            f"unexpected server {server_name!r}"
            if server_name != expected_server_name
            else f"missing tools: {', '.join(missing)}"
        )
        return {"ok": False, "error": detail, "tools": tools}
    if "server-side tools unavailable" in instructions:
        return {"ok": False, "error": instructions, "tools": tools}
    if probe_error:
        return {"ok": False, "error": f"server-side code_search probe failed: {probe_error}", "tools": tools}
    if not isinstance(probe_result, dict) or bool(probe_result.get("isError")):
        probe_detail: object = probe_result.get("content") if isinstance(probe_result, dict) else probe_result
        return {"ok": False, "error": f"server-side code_search probe failed: {probe_detail}", "tools": tools}
    return {"ok": True, "server": server_name, "tools": tools}


# ─── path helpers ────────────────────────────────────────────────────────────────────────────


def _debug_log_path(root: Path) -> Path:
    return root / "mcp_debug.jsonl"


def _savings_events_path(root: Path) -> Path:
    return root / "live_savings_events.jsonl"


def _debug_log_paths(root: Path) -> list[Path]:
    """All debug log files, in stable order: per-session files first, then the legacy global file.

    Per-session logs live under the nested date/host session layout
    (sessions/YYYY/MM/DD/<host>/<sid>/mcp_debug.jsonl), so glob recursively.
    """
    paths: list[Path] = []
    sessions_dir = root / "sessions"
    if sessions_dir.is_dir():
        paths.extend(sorted(sessions_dir.glob("**/mcp_debug.jsonl")))
    legacy = _debug_log_path(root)
    if legacy.exists():
        paths.append(legacy)
    return paths


# ─── data helpers ───────────────────────────────────────────────────────────────────────────


def _read_tool_call_events(root: Path, since_seconds: float, filter_tool: str | None = None) -> list[dict[str, Any]]:
    """Read tool_call events from live_savings_events.jsonl that have duration_ms."""
    path = _savings_events_path(root)
    if not path.exists():
        return []
    cutoff = time.time() - since_seconds
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("kind") == "tool_call" and "duration_ms" in e and float(e.get("ts", 0)) >= cutoff:
                    if filter_tool is None or e.get("tool") == filter_tool:
                        events.append(e)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return events


def _read_debug_entries(
    root: Path, since_seconds: float, filter_tool: str | None = None
) -> list[tuple[int, dict[str, Any]]]:
    """Read debug log entries as (1-indexed-line-number, entry) pairs.

    Reads from per-session files (sessions/**/mcp_debug.jsonl) written by the
    current server, with a fallback to the legacy global path for older installs.
    """
    cutoff = time.time() - since_seconds
    result: list[tuple[int, dict[str, Any]]] = []

    global_idx = 0
    for path in _debug_log_paths(root):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    global_idx += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if float(e.get("ts", 0)) >= cutoff:
                            if filter_tool is None or e.get("tool") == filter_tool:
                                result.append((global_idx, e))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
        except OSError:
            pass
    return result


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (p / 100.0) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _fmt_ms(ms: float) -> str:
    if ms < 1000:
        return f"{round(ms)}ms"
    return f"{ms / 1000:.1f}s"


def _fmt_age(ts: float) -> str:
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _pid_is_running(pid: int) -> bool:
    """Zombie- and PID-reuse-aware liveness — one impl, in ``session_state``."""
    from lemoncrow.gateway.adapters.mcp.session_state import mcp_pid_is_live

    return mcp_pid_is_live(pid)


def active_mcp_sessions(root: Path) -> list[dict[str, Any]]:
    """Live LemonCrow MCP server registrations (PID-checked), oldest first.

    Each MCP server process writes ``{root}/mcp_sessions/<id>.json`` at startup
    and removes it on clean shutdown; stale files (dead PIDs) are skipped.
    """
    sessions_dir = root / "mcp_sessions"
    if not sessions_dir.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for entry in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not _pid_is_running(pid):
            continue
        data["registration_file"] = str(entry)
        sessions.append(data)
    return sessions


# ─── mcp group ─────────────────────────────────────────────────────────────────────────────


@click.group(
    "mcp",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--root",
    envvar="LEMONCROW_ROOT",
    type=click.Path(file_okay=False, path_type=Path),
    help="LemonCrow data root (default: ~/.lemoncrow)",
)
@click.option("--host", envvar="LEMONCROW_AGENT", help="Agent host identifier (e.g. claude-code)")
@click.version_option(version=lemoncrow.__version__, prog_name="lc mcp", message="%(prog)s %(version)s")
@click.pass_context
def mcp_group(ctx: click.Context, root: Path | None, host: str | None) -> None:
    """Start the LemonCrow MCP server, or inspect MCP diagnostics.

    With no subcommand: runs the short-lived thin-client stdio MCP server.
    Use ``lc mcp serve`` to publish the same tools at a public https URL that
    any remote MCP client — ChatGPT, Claude, Cursor, VS Code — can connect to.
    Use ``lc mcp stats`` to view latency analytics.
    """
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand → start the MCP server.
    if root is not None:
        os.environ["LEMONCROW_ROOT"] = str(root)
    if host is not None:
        os.environ["LEMONCROW_AGENT"] = host
    # ``lc mcp`` is now only the compatibility entrypoint for the dependency-free
    # thin client.  There is deliberately no local-index/server fallback here:
    # local means the same client talking to a separately running loopback
    # LemonCrow server, and hosted means the URL points at the remote server.
    if root is not None:
        os.environ["LEMONCROW_HOME"] = str(root)
    try:
        from lemoncrow_client.mcpserver import serve as serve_thin_client
    except ImportError as exc:  # pragma: no cover - packaging contract catches this
        raise click.ClickException(
            "lemoncrow-client is required by lc mcp; reinstall LemonCrow so the thin-client dependency is present"
        ) from exc
    raise SystemExit(serve_thin_client())


@mcp_group.command("check")
@click.option("--json", "as_json", is_flag=True)
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.pass_context
def mcp_check(ctx: click.Context, as_json: bool, timeout: float) -> None:
    """Fail unless a fresh LemonCrow stdio server initializes with core tools."""
    parent = ctx.parent
    host = str(parent.params.get("host") or "claude") if parent is not None else "claude"
    result = probe_stdio_server(host=host, timeout=timeout)
    if as_json:
        _emit(result, as_json=True)
    elif result["ok"]:
        click.echo(f"MCP ready: {result['server']} ({len(result['tools'])} tools)")
    if not result["ok"]:
        raise click.ClickException(str(result["error"]))


def _cpu_ticks(pid: int) -> float | None:
    """utime+stime of ``pid`` in clock ticks, or ``None`` if it's gone (Linux)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[-1].split()
    except (OSError, IndexError):
        return None
    try:  # after the trailing ')' the 12th/13th fields are utime/stime
        return float(fields[11]) + float(fields[12])
    except (IndexError, ValueError):
        return None


def _pss_mb(pid: int) -> float | None:
    """Proportional set size of ``pid`` in MB, or ``None`` (non-Linux/raced).

    Preferred over RSS for a memory column: RSS charges every shared page
    (interpreter, libs, mmapped index/model files) in full to each process, so
    a workspace daemon that maps a multi-GB index reads far larger than the
    memory it actually costs the machine. PSS splits shared pages across the
    processes mapping them, so summing a column is meaningful.
    """
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8").splitlines():
            if line.startswith("Pss:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        return None
    return None


def _proc_stats(pids: list[int]) -> dict[int, tuple[float, float]]:
    """``{pid: (memory_mb, cpu_percent_of_one_core)}`` for the pids given.

    Memory is PSS where the kernel exposes it, else the RSS ``ps`` reports.
    CPU is sampled over a short live interval from ``/proc`` rather than taken
    from ``ps %cpu``: ``%cpu`` is the average over the *whole* process
    lifetime, so a daemon that has been up for hours reads ~0% no matter what
    it is doing right now. Non-Linux keeps the ``ps`` value — lifetime-averaged,
    but the only thing available without adding a dependency.
    """
    unique = sorted({pid for pid in pids if pid > 0})
    if not unique:
        return {}
    stats: dict[int, tuple[float, float]] = {}
    result = subprocess.run(
        ["ps", "-o", "pid=,rss=,%cpu=", "-p", ",".join(str(pid) for pid in unique)],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        try:
            stats[int(parts[0])] = (float(parts[1]) / 1024.0, float(parts[2]))
        except ValueError:
            continue

    if not Path("/proc").is_dir():
        return stats
    interval = 0.12
    hz = float(os.sysconf("SC_CLK_TCK") or 100)
    first = {pid: _cpu_ticks(pid) for pid in stats}
    time.sleep(interval)
    for pid, before in first.items():
        memory_mb, cpu_pct = stats[pid]
        pss = _pss_mb(pid)
        if pss is not None:
            memory_mb = pss
        after = _cpu_ticks(pid)
        if before is not None and after is not None:
            cpu_pct = max(0.0, (after - before) / hz / interval * 100.0)
        stats[pid] = (memory_mb, cpu_pct)
    return stats


# --- mcp list ---------------------------------------------------------------


@mcp_group.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def mcp_list(ctx: click.Context, as_json: bool) -> None:
    """List managed/public MCP servers still running on this machine.

    ``lc mcp`` itself is a short-lived stdio thin client and intentionally has
    no daemon registry to inspect. Persistent/public ``lc mcp serve`` services
    remain visible here because they are explicitly managed server processes.
    """
    root: Path = ctx.obj["root"]
    from dataclasses import asdict

    from lemoncrow.gateway.adapters.mcp.session_state import prune_stale_mcp_sessions
    from lemoncrow.gateway.cli.commands._mcp_service import describe_services

    pruned = prune_stale_mcp_sessions(root)
    registered = active_mcp_sessions(root)
    remote = describe_services()

    if as_json:
        _emit(
            {
                "count": len(registered) + len(remote),
                "remote_servers": [asdict(service) for service in remote],
                "servers": registered,
            },
            as_json=True,
        )
        return

    home = str(Path.home())

    def _short(path: object) -> str:
        text = str(path or "?")
        return "~" + text[len(home) :] if text.startswith(home) else text

    stats = _proc_stats(
        [service.pid for service in remote if service.pid]
        + [int(str(entry.get("pid"))) for entry in registered if str(entry.get("pid", "")).isdigit()]
    )

    def _usage(pid: object) -> str:
        text = str(pid or "")
        key = int(text) if text.isdigit() else None
        entry = stats.get(key) if key is not None else None
        if entry is None:
            return "gone"
        memory_mb, cpu_pct = entry
        if memory_mb <= 0:
            return "defunct"
        return f"{memory_mb:,.0f} MB  cpu {cpu_pct:.0f}%"

    total = len(remote) + len(registered)
    click.echo("")
    click.secho(f"  LemonCrow MCP servers · {total}", bold=True)
    if pruned:
        click.secho(f"  pruned {pruned} stale registration(s) whose process had exited", dim=True)
    if total == 0:
        click.echo("  No managed/public server processes are running.")
        click.echo("  `lc mcp` is a short-lived thin client and is not listed here.")
        click.echo("")
        return

    for service in remote:
        click.echo(
            f"  {service.state:<13} {_short(service.workspace):<40} "
            f"{_usage(service.pid) if service.pid else '':<20} https://{service.hostname}/mcp"
        )
    for session in registered:
        click.echo(
            f"  pid {session.get('pid')!s:<8} {_short(session.get('workspace')):<40} "
            f"{_usage(session.get('pid')):<20} explicit server process"
        )
    click.echo("")


# ─── mcp stats ────────────────────────────────────────────────────────────────────────────


@mcp_group.group("stats", invoke_without_command=True)
@click.option("--tool", "filter_tool", default=None, help="Filter to a specific tool name.")
@click.option(
    "--hours",
    default=24.0,
    show_default=True,
    type=float,
    help="Look-back window in hours.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def mcp_stats_group(
    ctx: click.Context,
    filter_tool: str | None,
    hours: float,
    as_json: bool,
) -> None:
    """Per-tool MCP latency stats: p50, p95, p99, p100 and top-5 slowest calls.

    \b
    Examples:
      lc mcp stats                 # 24-hour summary across all tools
      lc mcp stats --tool bash     # filter to bash only
      lc mcp stats --hours 1       # last-hour window
      lc mcp stats show 42         # drill into debug entry #42
    """
    if ctx.invoked_subcommand is not None:
        return

    root: Path = ctx.obj["root"]
    since = hours * 3600

    events = _read_tool_call_events(root, since, filter_tool)
    debug_entries = _read_debug_entries(root, since, filter_tool)
    debug_env_on = os.environ.get("LEMONCROW_MCP_DEBUG", "0") not in ("0", "", "false", "no")
    debug_marker_on = (root / ".dev_mode").exists()
    debug_on = debug_env_on or debug_marker_on

    # Build per-tool stats from live_savings_events.jsonl
    from collections import defaultdict

    latencies: dict[str, list[float]] = defaultdict(list)
    error_counts: dict[str, int] = defaultdict(int)
    for e in events:
        tool = str(e.get("tool") or "unknown")
        latencies[tool].append(float(e.get("duration_ms", 0)))
        if e.get("status") == "error":
            error_counts[tool] += 1

    tool_stats: list[dict[str, Any]] = []
    for tool in sorted(latencies):
        ms = latencies[tool]
        tool_stats.append(
            {
                "tool": tool,
                "calls": len(ms),
                "errors": error_counts.get(tool, 0),
                "p50_ms": round(_percentile(ms, 50)),
                "p95_ms": round(_percentile(ms, 95)),
                "p99_ms": round(_percentile(ms, 99)),
                "p100_ms": round(max(ms)),
            }
        )

    # Top-5 slowest — prefer debug log (has IDs + args); fall back to savings events
    top5: list[dict[str, Any]] = []
    if debug_entries:
        slowest = sorted(debug_entries, key=lambda x: x[1].get("duration_ms", 0), reverse=True)[:5]
        for line_id, e in slowest:
            top5.append(
                {
                    "id": line_id,
                    "tool": e.get("tool", ""),
                    "duration_ms": e.get("duration_ms", 0),
                    "ts": e.get("ts", 0),
                    "session_id": e.get("session_id", ""),
                    "status": e.get("status", ""),
                }
            )
    else:
        slowest_ev = sorted(events, key=lambda x: x.get("duration_ms", 0), reverse=True)[:5]
        for e in slowest_ev:
            top5.append(
                {
                    "id": None,
                    "tool": e.get("tool", ""),
                    "duration_ms": e.get("duration_ms", 0),
                    "ts": e.get("ts", 0),
                    "session_id": e.get("session_id", ""),
                    "status": e.get("status", ""),
                }
            )

    total_calls = sum(s["calls"] for s in tool_stats)
    total_errors = sum(s["errors"] for s in tool_stats)

    if as_json:
        _emit(
            {
                "stats": tool_stats,
                "top_slowest": top5,
                "total_calls": total_calls,
                "total_errors": total_errors,
                "debug_mode": debug_on,
                "window_hours": hours,
            },
            as_json=True,
        )
        return

    # ─── human-readable output (matches lc savings style) ───
    if debug_env_on:
        debug_label = "on (env)"
    elif debug_marker_on:
        debug_label = "on (dev mode)"
    else:
        debug_label = "off — run: make dev  or  LEMONCROW_MCP_DEBUG=1"

    window_label = f"{int(hours)}h" if hours == int(hours) else f"{hours:.1f}h"
    click.echo("")
    click.echo(
        f"  MCP Tool Latency · last {window_label} · {total_calls:,} calls"
        f" · {total_errors} errors · debug: {debug_label}"
    )
    click.echo("  " + "─" * 70)

    if not tool_stats:
        click.echo("  No tool_call events with duration data found.")
        click.echo("  The MCP server emits these on every call — start a session and retry.")
        click.echo("")
        return

    click.echo(f"  {'tool':<18} {'calls':>6}  {'p50':>7}  {'p95':>7}  {'p99':>7}  {'p100':>7}  {'err':>4}")
    click.echo("  " + "─" * 70)
    for s in tool_stats:
        click.echo(
            f"  {s['tool']:<18} {s['calls']:>6}  "
            f"{_fmt_ms(s['p50_ms']):>7}  {_fmt_ms(s['p95_ms']):>7}  "
            f"{_fmt_ms(s['p99_ms']):>7}  {_fmt_ms(s['p100_ms']):>7}  {s['errors']:>4}"
        )

    if top5:
        click.echo("")
        click.echo("  Top 5 slowest calls:")
        for entry in top5:
            age = _fmt_age(float(entry["ts"])) if entry["ts"] else ""
            sid = str(entry.get("session_id") or "")[:8]
            id_str = f"#{entry['id']}" if entry["id"] is not None else ""
            err_flag = " ✗" if entry.get("status") == "error" else ""
            click.echo(
                f"    {id_str:<7} {entry['tool']:<16} {_fmt_ms(entry['duration_ms']):>7}"
                f"   {age:<13}  session={sid}{err_flag}"
            )
        if debug_on and top5 and top5[0]["id"] is not None:
            first_id = top5[0]["id"]
            click.echo(f"\n  → lc mcp stats show {first_id}")
    click.echo("")


@mcp_stats_group.command("show")
@click.argument("entry_id", type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def mcp_stats_show(ctx: click.Context, entry_id: int, as_json: bool) -> None:
    """Drill into a specific debug log entry by ID.

    IDs are the line numbers shown in ``lc mcp stats``. Only available
    when debug logging is enabled (make dev, or LEMONCROW_MCP_DEBUG=1).
    """
    root: Path = ctx.obj["root"]
    debug_paths = _debug_log_paths(root)
    if not debug_paths:
        raise click.ClickException(
            "Debug log not found. Enable with: make dev  or  LEMONCROW_MCP_DEBUG=1, then run a few MCP tool calls."
        )

    # Resolve the ID over the same multi-file enumeration used by `lc mcp stats`
    # (global 1-indexed line number across all debug files, in stable order).
    entry: dict[str, Any] | None = None
    global_idx = 0
    for path in debug_paths:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    global_idx += 1
                    if global_idx == entry_id:
                        try:
                            entry = json.loads(line.strip())
                        except json.JSONDecodeError as exc:
                            raise click.ClickException(f"Entry #{entry_id} has malformed JSON: {exc}") from exc
                        break
        except OSError:
            pass
        if entry is not None:
            break

    if entry is None:
        raise click.ClickException(f"Entry #{entry_id} not found — the debug log may have fewer lines.")

    if as_json:
        _emit(entry, as_json=True)
        return

    ts = float(entry.get("ts", 0))
    age = _fmt_age(ts) if ts else "unknown"
    status = entry.get("status", "")
    status_flag = " ✓" if status == "ok" else " ✗"

    click.echo("")
    click.echo(f"  MCP debug entry #{entry_id}  ·  {age}")
    click.echo("  " + "─" * 60)
    click.echo(f"  tool:           {entry.get('tool', '')}")
    click.echo(f"  status:         {status}{status_flag}")
    click.echo(f"  duration:       {_fmt_ms(float(entry.get('duration_ms', 0)))}")
    click.echo(f"  response_size:  {entry.get('response_size_bytes', 0):,} bytes")
    click.echo(f"  session_id:     {entry.get('session_id', '')}")
    if entry.get("error"):
        click.echo(f"  error:          {entry['error']}")
    click.echo("")
    click.echo("  args:")
    args = entry.get("args") or {}
    if isinstance(args, dict):
        if args:
            for k, v in args.items():
                v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                click.echo(f"    {k}: {v_str}")
        else:
            click.echo("    (none)")
    else:
        click.echo(f"    {args}")
    click.echo("")


# ─── mcp debug ────────────────────────────────────────────────────────────────────────────


@mcp_group.group("debug")
def mcp_debug_group() -> None:
    """Manage MCP per-call debug logging."""


@mcp_debug_group.command("status")
@click.pass_context
def mcp_debug_status(ctx: click.Context) -> None:
    """Show whether MCP debug logging is active."""
    root: Path = ctx.obj["root"]
    marker = root / ".dev_mode"
    env_on = os.environ.get("LEMONCROW_MCP_DEBUG", "0") not in ("0", "", "false", "no")
    marker_on = marker.exists()
    log = _debug_log_path(root)
    active = env_on or marker_on
    click.echo(f"MCP debug logging: {'on' if active else 'off'}")
    if env_on:
        click.echo("  source: LEMONCROW_MCP_DEBUG env var")
    if marker_on:
        click.echo(f"  source: dev_mode marker ({marker})")
    if log.exists():
        size = log.stat().st_size
        click.echo(f"  log:    {log}  ({size:,} bytes)")
    else:
        click.echo(f"  log:    {log}  (not yet created)")
    if not active:
        click.echo("  → Enable: LEMONCROW_MCP_DEBUG=1  or  make dev  or  lc mcp debug on")


@mcp_debug_group.command("on")
@click.pass_context
def mcp_debug_on(ctx: click.Context) -> None:
    """Enable MCP debug logging (writes the dev_mode marker)."""
    root: Path = ctx.obj["root"]
    marker = root / ".dev_mode"
    root.mkdir(parents=True, exist_ok=True)
    marker.touch()
    click.echo("MCP debug logging: on")
    click.echo("  (takes effect on next MCP server start)")


@mcp_debug_group.command("off")
@click.pass_context
def mcp_debug_off(ctx: click.Context) -> None:
    """Disable MCP debug logging (removes the dev_mode marker)."""
    root: Path = ctx.obj["root"]
    marker = root / ".dev_mode"
    if marker.exists():
        marker.unlink()
        click.echo("MCP debug logging: off  (marker removed)")
    else:
        click.echo("MCP debug logging: already off")


# Remote (streamable-HTTP + OAuth) transport lives in its own module because it
# is a whole tunnel/OAuth stack; it hangs off this group so one `lc mcp` covers
# both transports: bare `lc mcp` = local stdio, `lc mcp serve` = public URL.
mcp_group.add_command(mcp_serve_cmd)
mcp_group.add_command(mcp_client_cmd)
mcp_group.add_command(mcp_service_group)

# backward-compat alias used by commands/__init__.py
mcp_cmd = mcp_group
