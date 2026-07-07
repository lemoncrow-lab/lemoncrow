"""``atelier audit`` command group — inspect configuration for context waste.

The ``audit context`` command scans MCP server configs, skill definitions
(AGENTS.md / SKILL.md), and recent session history to flag servers, skills,
and tools that consume context window space without being used.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click

from atelier.core.capabilities.savings_summary import _fmt_tok, _fmt_usd
from atelier.gateway.cli.commands.sessions import (
    _FLEET_SAVED_TOKENS_PER_CALL,
    _ROUTABLE_BUILTIN,
    _base_tool_name,
    _is_atelier_tool_name,
)

logger = logging.getLogger(__name__)

# ── heuristics ──────────────────────────────────────────────────────────────
_AVG_TOOL_SCHEMA_TOKENS = 400

_LOW_USE_SESSION_PCT = 0.10  # ≤10 % of sessions → CONSIDER
_NO_USE_SESSION_PCT = 0.0  # 0 % of sessions → DISABLE

# Estimated input/output token pricing (Sonnet 4.5 rates as baseline)
_INPUT_RATE_PER_TOKEN = 3.0 / 1_000_000
_OUTPUT_RATE_PER_TOKEN = 15.0 / 1_000_000

# Estimated turns per session for context-cost projection
_ESTIMATED_TURNS_PER_SESSION = 20

# ── data structures ─────────────────────────────────────────────────────────


class AuditItem:
    """One auditable entity (MCP server, skill, tool set)."""

    __slots__ = (
        "atelier_calls",
        "context_cost_usd",
        "detail",
        "est_context_tokens",
        "name",
        "net_benefit_tokens",
        "net_benefit_usd",
        "next_action",
        "potential_tokens_saved",
        "potential_usd_saved",
        "recommendation",
        "routable_calls",
        "session_count",
        "source_path",
        "source_type",
        "tool_count",
        "total_sessions",
        "use_count",
        "used",
    )

    def __init__(
        self,
        *,
        name: str,
        source_type: str,
        source_path: str,
        tool_count: int = 0,
        used: bool = False,
        use_count: int = 0,
        session_count: int = 0,
        total_sessions: int = 0,
        est_context_tokens: int = 0,
        recommendation: str = "",
        detail: str = "",
        atelier_calls: int = 0,
        routable_calls: int = 0,
        potential_tokens_saved: int = 0,
        potential_usd_saved: float = 0.0,
        context_cost_usd: float = 0.0,
        net_benefit_tokens: int = 0,
        net_benefit_usd: float = 0.0,
        next_action: str = "",
    ) -> None:
        self.name = name
        self.source_type = source_type
        self.source_path = source_path
        self.tool_count = tool_count
        self.used = used
        self.use_count = use_count
        self.session_count = session_count
        self.total_sessions = total_sessions
        self.est_context_tokens = est_context_tokens
        self.recommendation = recommendation
        self.detail = detail
        self.atelier_calls = atelier_calls
        self.routable_calls = routable_calls
        self.potential_tokens_saved = potential_tokens_saved
        self.potential_usd_saved = potential_usd_saved
        self.context_cost_usd = context_cost_usd
        self.net_benefit_tokens = net_benefit_tokens
        self.net_benefit_usd = net_benefit_usd
        self.next_action = next_action
        self.total_sessions = total_sessions

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.source_type,
            "source_path": self.source_path,
            "tool_count": self.tool_count,
            "used": self.used,
            "use_count": self.use_count,
            "session_count": self.session_count,
            "total_sessions": self.total_sessions,
            "est_context_tokens": self.est_context_tokens,
            "atelier_calls": self.atelier_calls,
            "routable_calls": self.routable_calls,
            "potential_tokens_saved": self.potential_tokens_saved,
            "potential_usd_saved": round(self.potential_usd_saved, 6),
            "context_cost_usd": round(self.context_cost_usd, 6),
            "net_benefit_tokens": self.net_benefit_tokens,
            "net_benefit_usd": round(self.net_benefit_usd, 6),
            "recommendation": self.recommendation,
            "detail": self.detail,
            "next_action": self.next_action,
        }


# ── data collection ─────────────────────────────────────────────────────────


def _scan_mcp_servers(root: Path) -> list[AuditItem]:
    """Collect all configured MCP servers via the canonical discovery path."""
    from atelier.core.capabilities.mcp_integration import discover_mcp_configs

    items: list[AuditItem] = []
    try:
        configs = discover_mcp_configs()
    except Exception:
        logger.exception("Failed to discover MCP configs")
        return items

    for cfg in configs:
        tool_count = len(getattr(cfg, "tools", []) or [])
        est_tokens = tool_count * _AVG_TOOL_SCHEMA_TOKENS or 500
        items.append(
            AuditItem(
                name=cfg.name,
                source_type="mcp_server",
                source_path=cfg.command,
                tool_count=tool_count,
                est_context_tokens=est_tokens,
                detail=f"args={cfg.args}" if cfg.args else "",
            )
        )
    return items


def _discover_skill_files(workspace: Path | None = None) -> list[tuple[str, Path]]:
    """Yield (display_name, path) for every SKILL.md and AGENTS.md found.

    Scans:
      - AGENTS.md at the repo root
      - .agents/skills/<name>/SKILL.md
      - integrations/<host>/skills/<name>/SKILL.md
      - integrations/<host>/plugin/skills/<name>/SKILL.md
    """
    found: list[tuple[str, Path]] = []

    roots: list[Path] = []
    if workspace and workspace.is_dir():
        roots.append(workspace)
    cwd = Path.cwd()
    if cwd != workspace and cwd.is_dir():
        roots.append(cwd)

    seen: set[Path] = set()
    for root_dir in roots:
        agents = root_dir / "AGENTS.md"
        if agents.is_file() and agents not in seen:
            seen.add(agents)
            found.append(("AGENTS.md", agents))

        for sk in sorted(root_dir.glob(".agents/skills/*/SKILL.md")):
            if sk not in seen:
                seen.add(sk)
                name = sk.parent.name
                found.append((name, sk))

        for sk in sorted(root_dir.glob("integrations/*/skills/*/SKILL.md")):
            if sk not in seen:
                seen.add(sk)
                name = f"integ/{sk.parent.parent.parent.name}/{sk.parent.name}"
                found.append((name, sk))

        for sk in sorted(root_dir.glob("integrations/*/plugin/skills/*/SKILL.md")):
            if sk not in seen:
                seen.add(sk)
                name = f"integ/{sk.parent.parent.parent.parent.name}/{sk.parent.name}"
                found.append((name, sk))

    return found


def _scan_skills(workspace: Path | None = None) -> list[AuditItem]:
    """Collect all skill definitions from AGENTS.md and SKILL.md files."""
    items: list[AuditItem] = []
    for display_name, path in _discover_skill_files(workspace):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        est_tokens = max(200, len(text) // 4)
        items.append(
            AuditItem(
                name=display_name,
                source_type="skill",
                source_path=str(path),
                est_context_tokens=est_tokens,
                detail=f"{len(text)} chars",
            )
        )
    return items


def _scan_sessions(
    root: Path,
    since: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Load session stats from the last *since* window.

    Returns (sessions_list, total_session_count_in_window).
    """
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return [], 0

    results: list[dict[str, Any]] = []
    for stats_path in sorted(sessions_dir.glob("*/stats.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            mtime = datetime.fromtimestamp(stats_path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < since:
            continue
        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            results.append(data)
    return results, len(results)


def _collect_tool_names(sessions: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate ``tools_used`` across sessions.

    Returns a map of normalized tool name → total call count.
    Handles both transcript-precise (``tools_used`` dict) and
    hook-accumulated (``hook_tools_used`` dict) naming.
    """
    combined: dict[str, int] = {}
    for sess in sessions:
        for key in ("tools_used", "hook_tools_used"):
            tools = sess.get(key)
            if not isinstance(tools, dict):
                continue
            for name, count in tools.items():
                try:
                    c = int(count or 0)
                except (ValueError, TypeError):
                    c = 0
                if c > 0:
                    combined[name] = combined.get(name, 0) + c
    return combined


# ── classification & savings analysis ───────────────────────────────────────


def _classify_item_calls(
    tool_names: dict[str, int],
    item: AuditItem,
) -> tuple[int, int, int]:
    """Classify tool calls touching *item*.

    Returns (total_matches, atelier_calls, routable_builtin_calls).
    """
    if item.source_type == "mcp_server":
        server_key = item.name.lower()
        total = 0
        atelier = 0
        routable = 0
        for name, count in tool_names.items():
            if server_key in name.lower():
                total += count
                if _is_atelier_tool_name(name):
                    atelier += count
                elif _base_tool_name(name) in _ROUTABLE_BUILTIN:
                    routable += count
        return total, atelier, routable

    # skill: keyword overlap
    skill_keywords = set(re.split(r"[^a-z0-9]+", item.name.lower()))
    total = 0
    atelier = 0
    routable = 0
    for name, count in tool_names.items():
        tool_parts = set(re.split(r"[^a-z0-9]+", name.lower()))
        if skill_keywords & tool_parts:
            total += count
            if _is_atelier_tool_name(name):
                atelier += count
            elif _base_tool_name(name) in _ROUTABLE_BUILTIN:
                routable += count
    return total, atelier, routable


def _compute_savings_estimate(item: AuditItem) -> None:
    """Fill in savings and net-benefit fields on *item*."""
    total_sessions = max(1, item.total_sessions)

    # Context cost: per-turn context tokens x estimated turns in window
    projected_turns = total_sessions * _ESTIMATED_TURNS_PER_SESSION
    item.context_cost_usd = item.est_context_tokens * projected_turns * _INPUT_RATE_PER_TOKEN

    # Potential savings from routing routable builtin calls
    item.potential_tokens_saved = item.routable_calls * _FLEET_SAVED_TOKENS_PER_CALL
    item.potential_usd_saved = item.potential_tokens_saved * _OUTPUT_RATE_PER_TOKEN

    # Net benefit: total potential savings - total context cost
    item.net_benefit_tokens = item.potential_tokens_saved - item.est_context_tokens
    item.net_benefit_usd = item.potential_usd_saved - item.context_cost_usd


def _compute_next_action(item: AuditItem) -> str:
    """Generate a specific next-action recommendation."""
    if item.recommendation == "DISABLE":
        if item.source_type == "mcp_server":
            return (
                f"Set `alwaysLoad: false` for `{item.name}` in .mcp.json. "
                f"Saves ~{_fmt_tok(item.est_context_tokens)} tok/turn "
                f"({_fmt_usd(item.context_cost_usd)} over window)."
            )
        if item.name == "AGENTS.md":
            return (
                f"Remove unused skill entries from AGENTS.md. "
                f"Saves ~{_fmt_tok(item.est_context_tokens)} tok/turn "
                f"({_fmt_usd(item.context_cost_usd)} over window)."
            )
        return (
            f"Remove `{item.name}` from skills/ directory. "
            f"Saves ~{_fmt_tok(item.est_context_tokens)} tok/turn "
            f"({_fmt_usd(item.context_cost_usd)} over window)."
        )

    if item.recommendation == "CONSIDER":
        if not item.used:
            reason = f"Not used in any of {item.total_sessions} session(s)"
        else:
            reason = f"Low use: {item.use_count} calls across {item.session_count}/{item.total_sessions} sessions"
        if item.source_type == "mcp_server":
            return f"{reason}. Set `alwaysLoad: false` for `{item.name}`; re-enable when actively used."
        return f"{reason}. Keep only if actively needed."

    if item.recommendation == "KEEP":
        if item.net_benefit_tokens > 0:
            return (
                f"Active — {item.use_count} calls in {item.session_count} session(s). "
                f"Net positive: {_fmt_usd(item.net_benefit_usd)}."
            )
        if item.use_count > 0:
            return f"Active — {item.use_count} calls in {item.session_count} session(s). Keep enabled."
        return "Widely applicable system context — keep enabled."

    return ""


# ── recommendation ──────────────────────────────────────────────────────────


def _compute_recommendation(
    item: AuditItem,
    used_in_any: bool,
    use_count: int,
    total_sessions: int,
) -> str:
    """Assign DISABLE / CONSIDER / KEEP using usage data.

    Net benefit from routable calls is shown as informational data in the
    table and next-action text, but the recommendation itself is driven by
    actual usage: an item that is actively used stays KEEP even if its
    potential savings don't fully offset its context cost, because those
    savings are an additional-opportunity figure, not a value assessment.
    """
    if not used_in_any:
        if item.est_context_tokens > 1000:
            return "DISABLE"
        return "CONSIDER"

    # Low usage rate across sessions
    if total_sessions > 0 and use_count / max(1, total_sessions) < _LOW_USE_SESSION_PCT:
        return "CONSIDER"

    return "KEEP"


# ── rendering (Rich) ────────────────────────────────────────────────────────


def _render_audit_rich(
    items: list[AuditItem],
    days: int,
    total_sessions: int,
    threshold: int,
) -> str:
    """Render the full audit report using Rich tables and panels."""
    from rich import box as rbox
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console(highlight=False, width=200)

    if not items:
        console.print("[dim]No MCP servers or skills found to audit.[/]")
        return ""

    # ── summary counts ──
    disable_count = sum(1 for i in items if i.recommendation == "DISABLE")
    consider_count = sum(1 for i in items if i.recommendation == "CONSIDER")
    keep_count = sum(1 for i in items if i.recommendation == "KEEP")
    recoverable_tokens = sum(i.est_context_tokens for i in items if i.recommendation in ("DISABLE", "CONSIDER"))
    total_net_benefit = sum(i.net_benefit_usd for i in items)
    total_potential_saved = sum(i.potential_usd_saved for i in items)
    total_context_cost = sum(i.context_cost_usd for i in items)

    def _chip(label: str, value: str, color: str) -> Panel:
        return Panel(
            f"[bold {color}]{value}[/]\n[dim]{label}[/]",
            border_style="dim",
            padding=(0, 2),
        )

    # ── Header ──
    console.print()
    console.rule(f"[bold bright_white]Context Audit[/]  [dim]·  last {days}d  ·  {total_sessions} sessions[/]")
    console.print()

    # ── Hero chips ──
    chip_grid = Table.grid(expand=True)
    for _ in range(5):
        chip_grid.add_column(justify="center")

    chip_grid.add_row(
        _chip("Items Scanned", f"{len(items)}", "bright_white"),
        _chip("to DISABLE", f"{disable_count}", "bright_red"),
        _chip("to CONSIDER", f"{consider_count}", "bright_yellow"),
        _chip("KEEP", f"{keep_count}", "bright_green"),
        _chip("Recoverable/Turn", f"{_fmt_tok(recoverable_tokens)} tok", "bright_cyan"),
    )
    console.print(chip_grid)
    console.print()

    # ── Savings summary panel ──
    savings_lines = [
        f"  [dim]Context waste      [/]  [bright_red]{_fmt_usd(total_context_cost)}[/]  [dim](projected over window)[/]",
    ]
    if total_potential_saved > 0:
        savings_lines.append(
            f"  [dim]Call savings        [/]  [bright_green]{_fmt_usd(total_potential_saved)}[/]  [dim](routable calls x fleet rate)[/]",
        )
    sign = "[bright_green]+" if total_net_benefit >= 0 else "[bright_red]-"
    savings_lines.append(f"  [dim]Net benefit        [/]  {sign}{_fmt_usd(abs(total_net_benefit))}[/]")
    console.print(
        Panel(
            "\n".join(savings_lines),
            title="[bold]Savings Estimate[/]",
            border_style="yellow dim",
            padding=(1, 2),
        )
    )
    console.print()

    # ── Main items table ──
    console.print("[bold bright_white]  Items[/]  [dim]sorted by net benefit (worst first)[/]")
    console.print()

    table = Table(box=rbox.SIMPLE, show_header=True, header_style="dim", padding=(0, 2))
    table.add_column("Name", style="bold", min_width=18, no_wrap=True)
    table.add_column("Type", min_width=10, no_wrap=True)
    table.add_column("Used", justify="center", min_width=4, no_wrap=True)
    table.add_column("Sessions", justify="right", min_width=5)
    table.add_column("Calls", justify="right", min_width=6)
    table.add_column("Atelier", justify="right", min_width=6)
    table.add_column("Routable", justify="right", min_width=6)
    table.add_column("Ctx Cost", justify="right", min_width=11)
    table.add_column("Saved", justify="right", min_width=11)
    table.add_column("Net Δ", justify="right", min_width=11)
    table.add_column("Rec", min_width=9, no_wrap=True)

    sorted_items = sorted(items, key=lambda i: i.net_benefit_usd)

    for item in sorted_items:
        used_mark = "[bright_green]✓[/]" if item.used else "[dim]—[/]"

        session_str = f"{item.session_count}/{item.total_sessions}" if item.total_sessions > 0 else "[dim]—[/]"
        call_str = str(item.use_count) if item.use_count > 0 else "[dim]—[/]"
        atelier_str = str(item.atelier_calls) if item.atelier_calls > 0 else "[dim]—[/]"
        routable_str = str(item.routable_calls) if item.routable_calls > 0 else "[dim]—[/]"

        ctx_cost_s = f"[bright_red]{_fmt_usd(item.context_cost_usd)}[/]" if item.context_cost_usd > 0 else "[dim]—[/]"
        saved_s = (
            f"[bright_green]{_fmt_usd(item.potential_usd_saved)}[/]" if item.potential_usd_saved > 0 else "[dim]—[/]"
        )

        if item.net_benefit_usd > 0:
            net_s = f"[bright_green]+{_fmt_usd(item.net_benefit_usd)}[/]"
        elif item.net_benefit_usd < 0:
            net_s = f"[bright_red]{_fmt_usd(item.net_benefit_usd)}[/]"
        else:
            net_s = "[dim]—[/]"

        if item.recommendation == "DISABLE":
            rec_s = "[bright_red]DISABLE[/]"
        elif item.recommendation == "CONSIDER":
            rec_s = "[bright_yellow]CONSIDER[/]"
        else:
            rec_s = "[bright_green]KEEP[/]"

        table.add_row(
            item.name[:28],
            item.source_type,
            used_mark,
            session_str,
            call_str,
            atelier_str,
            routable_str,
            ctx_cost_s,
            saved_s,
            net_s,
            rec_s,
        )

    console.print(table)
    console.print()

    # ── Next actions section ──
    actionable = [i for i in sorted_items if i.recommendation in ("DISABLE", "CONSIDER")]
    if actionable:
        console.print("[bold bright_yellow]  Next Actions[/]")
        console.print()
        for item in actionable:
            icon = "!" if item.recommendation == "DISABLE" else "?"
            label = (
                f"[bright_red]{item.recommendation}[/]"
                if item.recommendation == "DISABLE"
                else f"[bright_yellow]{item.recommendation}[/]"
            )
            console.print(f"  [{icon}] {label}  [bold]{item.name}[/]  [dim]({item.source_type})[/]")
            console.print(f"       {item.next_action}")
            console.print()
    else:
        console.print("[dim]No items need action — everything is earning its keep.[/]")
        console.print()

    # ── Bottom summary ──
    console.rule("[dim]Summary[/]")
    console.print()

    summary_grid = Table.grid(expand=True, padding=(0, 1))
    summary_grid.add_column(ratio=1)
    summary_grid.add_column(ratio=1)

    left_lines = [
        f"  [dim]Items            [/]  [bright_white]{len(items)} total[/]",
        f"  [dim]  DISABLE        [/]  [bright_red]{disable_count}[/]",
        f"  [dim]  CONSIDER       [/]  [bright_yellow]{consider_count}[/]",
        f"  [dim]  KEEP           [/]  [bright_green]{keep_count}[/]",
        "",
        f"  [dim]Recoverable/turn [/]  [bright_cyan]{_fmt_tok(recoverable_tokens)} tok[/]",
        f"  [dim]  per session    [/]  [dim]~{_fmt_tok(recoverable_tokens // max(1, total_sessions))} tok[/]",
    ]

    right_lines = [
        f"  [dim]Context waste    [/]  [bright_red]{_fmt_usd(total_context_cost)}[/]",
        f"  [dim]Call savings     [/]  [bright_green]{_fmt_usd(total_potential_saved)}[/]",
        f"  [dim]Net benefit      [/]  {'[bright_green]+' if total_net_benefit >= 0 else '[bright_red]'}{_fmt_usd(abs(total_net_benefit))}[/]",
        "",
        f"  [dim]Threshold        [/]  [white]{threshold} tok[/]  [dim](items > this → DISABLE)[/]",
    ]

    summary_grid.add_row(
        Panel("\n".join(left_lines), title="[bold]Items[/]", border_style="dim", padding=(1, 2)),
        Panel("\n".join(right_lines), title="[bold]Economics[/]", border_style="dim", padding=(1, 2)),
    )
    console.print(summary_grid)
    console.print()

    return ""


# ── text rendering (fallback) ───────────────────────────────────────────────


def _render_text(items: list[AuditItem]) -> str:
    """Build a human-readable table of audit results (fallback, no Rich)."""
    lines: list[str] = []
    header = f"{'Name':<30} {'Type':<14} {'Used?':<7} {'Calls':>6} {'A/R':>8} {'CtxTok':>8} {'NetΔ':>10}  Rec"
    lines.append(header)
    lines.append("-" * 94)

    for item in sorted(items, key=lambda i: i.net_benefit_usd):
        used_str = "YES" if item.used else "no"
        ar = f"{item.atelier_calls}/{item.routable_calls}" if item.atelier_calls or item.routable_calls else ""
        net_str = (
            f"+{_fmt_tok(item.net_benefit_tokens)}"
            if item.net_benefit_tokens > 0
            else str(_fmt_tok(item.net_benefit_tokens))
        )
        rec = item.recommendation
        rec_display = rec
        if rec == "DISABLE":
            rec_display = f"\x1b[31m{rec}\x1b[0m"
        elif rec == "CONSIDER":
            rec_display = f"\x1b[33m{rec}\x1b[0m"
        else:
            rec_display = f"\x1b[32m{rec}\x1b[0m"

        lines.append(
            f"{item.name:<30} {item.source_type:<14} {used_str:<7} {item.use_count:>6} {ar:>8} "
            f"{item.est_context_tokens:>8} {net_str:>10}  {rec_display}"
        )

    lines.append("")
    total_est = sum(i.est_context_tokens for i in items if i.recommendation in ("DISABLE", "CONSIDER"))
    disable_count = sum(1 for i in items if i.recommendation == "DISABLE")
    consider_count = sum(1 for i in items if i.recommendation == "CONSIDER")
    net_total = sum(i.net_benefit_tokens for i in items)
    lines.append(
        f"Summary: {disable_count} DISABLE, {consider_count} CONSIDER, "
        f"~{total_est} tokens recoverable per turn, net Δ={net_total}"
    )
    return "\n".join(lines)


# ── Click commands ──────────────────────────────────────────────────────────


@click.command("context")
@click.option(
    "--days",
    default=7,
    show_default=True,
    type=int,
    help="Look-back window in days for session history.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output JSON.",
)
@click.option(
    "--threshold",
    default=500,
    show_default=True,
    type=int,
    help="Context-token threshold above which unused items get DISABLE.",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Disable ANSI colour / Rich output.",
)
@click.pass_context
def audit_context_cmd(
    ctx: click.Context,
    days: int,
    as_json: bool,
    threshold: int,
    no_color: bool,
) -> None:
    """Audit MCP servers and skills against actual usage.

    Scans configured MCP servers and skill definitions, then compares
    them with tools actually used in recent sessions to identify context-
    wasting configuration. Includes token/USD savings estimates based
    on the fleet-measured average of 3,265 output tokens saved per
    routable call.
    """
    from atelier.gateway.cli.commands._shared import _emit

    root: Path = ctx.obj["root"]
    workspace: Path | None = ctx.obj.get("workspace") or Path.cwd()
    since = datetime.now(UTC) - timedelta(days=max(1, days))

    # ── 1. Collect all items ──
    items: list[AuditItem] = []
    items.extend(_scan_mcp_servers(root))
    items.extend(_scan_skills(workspace))

    if not items:
        msg = "No MCP servers or skills found to audit."
        if as_json:
            _emit(
                {
                    "items": [],
                    "summary": {
                        "total": 0,
                        "disable": 0,
                        "consider": 0,
                        "keep": 0,
                        "recoverable_tokens_per_turn": 0,
                        "total_context_cost_usd": 0.0,
                        "total_potential_savings_usd": 0.0,
                        "total_net_benefit_usd": 0.0,
                    },
                },
                as_json=True,
            )
        else:
            click.echo(msg)
        return

    # ── 2. Load session history ──
    sessions, total_sessions = _scan_sessions(root, since)
    tool_names = _collect_tool_names(sessions)

    # ── 3. Cross-reference, classify, and score ──
    for item in items:
        total_matches, atelier_calls, routable_calls = _classify_item_calls(tool_names, item)

        item.used = total_matches > 0
        item.use_count = total_matches
        item.atelier_calls = atelier_calls
        item.routable_calls = routable_calls
        item.session_count = 1 if total_matches > 0 else 0
        item.total_sessions = total_sessions

        # Compute savings and net benefit
        _compute_savings_estimate(item)

        # Compute recommendation
        item.recommendation = _compute_recommendation(item, item.used, item.use_count, total_sessions)

        # Next action
        item.next_action = _compute_next_action(item)

    # ── 4. Output ──
    disable_count = sum(1 for i in items if i.recommendation == "DISABLE")
    consider_count = sum(1 for i in items if i.recommendation == "CONSIDER")
    keep_count = sum(1 for i in items if i.recommendation == "KEEP")
    recoverable = sum(i.est_context_tokens for i in items if i.recommendation in ("DISABLE", "CONSIDER"))

    if as_json:
        _emit(
            {
                "days": days,
                "session_count": total_sessions,
                "items": [i.to_dict() for i in sorted(items, key=lambda x: (x.source_type, x.name))],
                "summary": {
                    "total": len(items),
                    "disable": disable_count,
                    "consider": consider_count,
                    "keep": keep_count,
                    "recoverable_tokens_per_turn": recoverable,
                    "total_context_cost_usd": round(sum(i.context_cost_usd for i in items), 6),
                    "total_potential_savings_usd": round(sum(i.potential_usd_saved for i in items), 6),
                    "total_net_benefit_usd": round(sum(i.net_benefit_usd for i in items), 6),
                },
            },
            as_json=True,
        )
    elif no_color:
        click.echo(f"Auditing context configuration against last {days} day(s) ({total_sessions} sessions)…\n")
        click.echo(_render_text(items))
    else:
        click.echo(
            f"Auditing context configuration against last {days} day(s) ({total_sessions} sessions)…",
            err=True,
        )
        _render_audit_rich(items, days, total_sessions, threshold)


@click.command("bash")
@click.option("--top", default=15, show_default=True, type=int, help="Max command rows to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colour / Rich output.")
@click.pass_context
def audit_bash_cmd(ctx: click.Context, top: int, as_json: bool, no_color: bool) -> None:
    """Rank bash command families by post-compaction context spend.

    Reads the per-command ledger the Atelier MCP server maintains in
    smart_state.json: for every normalized command family, how many tokens
    its output still shipped into context after all compaction, next to how
    many tokens compaction already saved. The top shipped rows are the
    compaction gaps worth new filters.
    """
    from atelier.gateway.cli.commands._shared import _emit

    root: Path = ctx.obj["root"]
    state_path = root / "smart_state.json"
    raw: dict[str, Any] = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bash_commands"), dict):
                raw = data["bash_commands"]
        except (OSError, ValueError):
            raw = {}

    rows: list[dict[str, Any]] = []
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        shipped_chars = int(val.get("shipped_chars", 0) or 0)
        omitted_chars = int(val.get("omitted_chars", 0) or 0)
        total = shipped_chars + omitted_chars
        rows.append(
            {
                "command": str(key),
                "calls": int(val.get("calls", 0) or 0),
                "shipped_tokens": shipped_chars // 4,
                "saved_tokens": omitted_chars // 4,
                "saved_pct": round(100.0 * omitted_chars / total, 1) if total else 0.0,
            }
        )
    rows.sort(key=lambda r: (-int(r["shipped_tokens"]), str(r["command"])))
    rows = rows[: max(1, top)]

    if as_json:
        _emit({"commands": rows, "source": str(state_path)}, as_json=True)
        return
    if not rows:
        click.echo("No bash command stats recorded yet — run some commands through the Atelier bash tool first.")
        return
    if not no_color:
        try:
            from rich.console import Console
            from rich.table import Table

            table = Table(title="Bash output spend after compaction (top shipped = filter-worthy)")
            table.add_column("command")
            table.add_column("calls", justify="right")
            table.add_column("shipped tok", justify="right")
            table.add_column("saved tok", justify="right")
            table.add_column("saved %", justify="right")
            for r in rows:
                table.add_row(
                    str(r["command"]),
                    str(r["calls"]),
                    f"{r['shipped_tokens']:,}",
                    f"{r['saved_tokens']:,}",
                    f"{r['saved_pct']}%",
                )
            Console().print(table)
            return
        except ImportError:
            pass
    width = max(7, *(len(str(r["command"])) for r in rows))
    click.echo(f"{'COMMAND':<{width}}  {'CALLS':>6}  {'SHIPPED TOK':>12}  {'SAVED TOK':>10}  {'SAVED %':>8}")
    for r in rows:
        click.echo(
            f"{r['command']!s:<{width}}  {r['calls']:>6}  {r['shipped_tokens']:>12,}  "
            f"{r['saved_tokens']:>10,}  {str(r['saved_pct']) + '%':>8}"
        )
