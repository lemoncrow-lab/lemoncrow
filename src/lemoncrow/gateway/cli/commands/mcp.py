"""``lc mcp`` — start the stdio MCP server, or run MCP diagnostics."""

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

_BENCHMARK_REQUIRED_TOOLS = frozenset({"read", "edit", "code_search", "bash"})


def probe_stdio_server(*, host: str = "claude", timeout: float = 30.0) -> dict[str, Any]:
    """Start the configured LemonCrow stdio command and verify its core MCP surface."""
    executable = shutil.which("lemoncrow")
    if executable is None:
        return {"ok": False, "error": "lemoncrow executable not found on PATH", "tools": []}
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
        ]
    )
    try:
        completed = subprocess.run(
            [executable, "mcp", "--host", host],
            input=requests + "\n",
            capture_output=True,
            text=True,
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
        server_name = by_id[1]["result"]["serverInfo"]["name"]
        tools = sorted(tool["name"] for tool in by_id[2]["result"]["tools"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid MCP handshake response: {exc}", "tools": []}
    missing = sorted(_BENCHMARK_REQUIRED_TOOLS - set(tools))
    if server_name != "lemoncrow" or missing:
        detail = (
            f"unexpected server {server_name!r}"
            if server_name != "lemoncrow"
            else f"missing tools: {', '.join(missing)}"
        )
        return {"ok": False, "error": detail, "tools": tools}
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
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_live_sessions(reg: dict[str, Any]) -> int | None:
    """Best-effort live-session count for a daemon via its /healthz route."""
    try:
        import httpx

        resp = httpx.get(f"http://127.0.0.1:{reg['port']}/healthz", timeout=1.0)
        if resp.status_code == 200:
            return int(resp.json().get("live_sessions", 0))
    except Exception:
        return None
    return None


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

    With no subcommand: starts the stdio MCP server.
    Use ``lc mcp stats`` to view latency analytics.
    """
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand → start the MCP server.
    if root is not None:
        os.environ["LEMONCROW_ROOT"] = str(root)
    if host is not None:
        os.environ["LEMONCROW_AGENT"] = host
    from lemoncrow.gateway.adapters.mcp_bridge import run_bridge, singleton_enabled

    if singleton_enabled():
        # Singleton mode: this process becomes a thin stdio<->HTTP proxy to the
        # shared per-workspace daemon instead of a full heavy stdio server.
        run_bridge(os.environ.get("LEMONCROW_ROOT"))
        return
    from lemoncrow.gateway.adapters.mcp_server import main as _mcp_main

    _mcp_main()


@mcp_group.command("daemon", hidden=True)
@click.option("--workspace", required=True, help="Absolute workspace root this daemon serves.")
@click.option(
    "--idle-grace-seconds",
    type=float,
    default=600.0,
    show_default=True,
    help="Self-shutdown after this many seconds with no tool traffic (0 disables).",
)
@click.pass_context
def mcp_daemon(ctx: click.Context, workspace: str, idle_grace_seconds: float) -> None:
    """Run the per-workspace singleton MCP daemon (internal; spawned by the bridge)."""
    root: Path = ctx.obj["root"]
    from lemoncrow.gateway.adapters.mcp_daemon import run_daemon

    run_daemon(str(Path(workspace).resolve()), root, idle_grace_seconds=idle_grace_seconds)


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


# ─── mcp list ─────────────────────────────────────────────────────────────────────────────


@mcp_group.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def mcp_list(ctx: click.Context, as_json: bool) -> None:
    """List all active LemonCrow MCP server processes.

    Reads the live-session registry (``~/.lemoncrow/mcp_sessions/``); entries
    whose process has exited are ignored.
    """
    root: Path = ctx.obj["root"]
    sessions = active_mcp_sessions(root)
    if as_json:
        _emit({"count": len(sessions), "servers": sessions}, as_json=True)
        return

    click.echo("")
    click.echo(f"  Active LemonCrow MCP servers · {len(sessions)}")
    click.echo("  " + "─" * 70)
    if not sessions:
        click.echo("  None running. Servers register on startup (lc mcp) and unregister on exit.")
        click.echo("  In singleton mode (default) sessions share a daemon — see: lc mcp daemons")
        click.echo("")
        return
    home = str(Path.home())
    for s in sessions:
        ws = str(s.get("workspace") or "?")
        if ws.startswith(home):
            ws = "~" + ws[len(home) :]
        age = ""
        started = s.get("started_at") or ""
        if started:
            try:
                from datetime import UTC, datetime

                age = _fmt_age(datetime.fromisoformat(started).replace(tzinfo=UTC).timestamp())
            except ValueError:
                age = ""
        sid = str(s.get("claude_session_id") or "")[:8]
        model = str(s.get("model") or "")
        parts = [f"  pid {s.get('pid'):<8}", f"{ws:<40}", f"started {age:<10}" if age else ""]
        if sid:
            parts.append(f"session={sid}")
        if model:
            parts.append(model)
        click.echo(" ".join(p for p in parts if p))
    click.echo("  Singleton daemons (shared per workspace): lc mcp daemons")
    click.echo("")


# ─── mcp daemons ──────────────────────────────────────────────────────────


@mcp_group.command("daemons")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def mcp_daemons(ctx: click.Context, as_json: bool) -> None:
    """List active per-workspace singleton MCP daemons.

    In singleton mode (default) one shared daemon per workspace serves every
    host session; each ``lc mcp`` is a thin bridge to it. Reads the daemon
    registry (``~/.lemoncrow/mcp_daemons/``); crashed daemons are skipped.
    """
    root: Path = ctx.obj["root"]
    from lemoncrow.gateway.adapters.mcp_daemon import list_daemons

    daemons = list_daemons(root)
    for d in daemons:
        d["live_sessions"] = _probe_live_sessions(d)
        d.pop("token", None)  # never surface the bearer token
    if as_json:
        _emit({"count": len(daemons), "daemons": daemons}, as_json=True)
        return

    click.echo("")
    click.echo(f"  Active LemonCrow MCP daemons · {len(daemons)}")
    click.echo("  " + "─" * 70)
    if not daemons:
        click.echo("  None running. A daemon starts on the first `lc mcp` in a workspace.")
        click.echo("")
        return
    home = str(Path.home())
    for d in daemons:
        ws = str(d.get("workspace") or "?")
        if ws.startswith(home):
            ws = "~" + ws[len(home) :]
        age = ""
        started = d.get("started_at")
        if isinstance(started, (int, float)):
            age = _fmt_age(float(started))
        sessions = d.get("live_sessions")
        parts = [f"  pid {d.get('pid'):<8}", f"{ws:<40}", f"port {d.get('port')}"]
        if age:
            parts.append(f"age {age}")
        if sessions is not None:
            parts.append(f"sessions={sessions}")
        click.echo(" ".join(parts))
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


# backward-compat alias used by commands/__init__.py
mcp_cmd = mcp_group
