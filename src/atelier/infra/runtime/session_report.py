"""Per-session cost and savings report.

Pure-computation module: reads a serialised run snapshot (the dict produced
by ``RunLedger.snapshot()``) plus optional flat-file companions and returns a
``SessionReport`` dataclass ready for rendering.

Usage::

    # from a live ledger
    from atelier.infra.runtime.session_report import build_report_from_ledger, render_text
    report = build_report_from_ledger(ledger, root)
    print(render_text(report))

    # from a persisted run file
    from atelier.infra.runtime.session_report import load_report, render_text
    report = load_report(session_id, root)
    print(render_text(report))
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atelier.infra.runtime.run_ledger import RunLedger


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_VENDOR_PREFIXES: dict[str, str] = {
    "claude": "Anthropic",
    "gpt": "OpenAI",
    "o1": "OpenAI",
    "o3": "OpenAI",
    "o4": "OpenAI",
    "gemini": "Google",
    "mistral": "Mistral",
}


def _model_vendor(model: str) -> str:
    lower = model.lower()
    for prefix, vendor in _VENDOR_PREFIXES.items():
        if lower.startswith(prefix):
            return vendor
    return "Unknown"


def _derive_vendor(models: dict[str, int]) -> str:
    vendors = {_model_vendor(m) for m in models}
    vendors.discard("Unknown")
    if not vendors:
        return "Unknown"
    if len(vendors) == 1:
        return next(iter(vendors))
    return "mixed"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def _runs_dir(root: Path) -> Path:
    return root / "runs"


def _live_savings_path(root: Path) -> Path:
    return root / "live_savings_events.jsonl"


# --------------------------------------------------------------------------- #
# Per-token cost estimation                                                    #
# --------------------------------------------------------------------------- #


def _cost_breakdown_from_calls(
    calls: list[dict[str, Any]],
) -> tuple[int, float, int, float, int, float, int, float]:
    """Return (in_tok, in_cost, out_tok, out_cost, cr_tok, cr_cost, cw_tok, cw_cost)."""
    from atelier.core.capabilities.pricing import get_model_pricing

    total_in_tok = total_out_tok = total_cr_tok = total_cw_tok = 0
    total_in_cost = total_out_cost = total_cr_cost = total_cw_cost = 0.0

    for call in calls:
        model = str(call.get("model") or "claude-haiku-4-5")
        pricing = get_model_pricing(model)
        in_tok = int(call.get("input_tokens") or 0)
        out_tok = int(call.get("output_tokens") or 0)
        cr_tok = int(call.get("cache_read_tokens") or 0)
        cw_tok = int(call.get("cache_write_tokens") or 0)

        total_in_tok += in_tok
        total_out_tok += out_tok
        total_cr_tok += cr_tok
        total_cw_tok += cw_tok

        total_in_cost += pricing.cost_usd(input_tokens=in_tok)
        total_out_cost += pricing.cost_usd(output_tokens=out_tok)
        total_cr_cost += pricing.cost_usd(cache_read_tokens=cr_tok)
        if cw_tok:
            total_cw_cost += pricing.cost_usd(input_tokens=cw_tok)

    return (
        total_in_tok,
        round(total_in_cost, 6),
        total_out_tok,
        round(total_out_cost, 6),
        total_cr_tok,
        round(total_cr_cost, 6),
        total_cw_tok,
        round(total_cw_cost, 6),
    )


# --------------------------------------------------------------------------- #
# Compact savings                                                              #
# --------------------------------------------------------------------------- #


def _read_compact_savings(session_id: str, root: Path) -> tuple[int, float]:
    """Read compact savings from ``live_savings_events.jsonl`` for *session_id*.

    Returns ``(event_count, total_cost_saved_usd)``.
    """
    path = _live_savings_path(root)
    if not path.exists():
        return 0, 0.0

    count = 0
    total_saved = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id") == session_id and ev.get("lever") == "session_compaction":
                count += 1
                total_saved += float(ev.get("cost_saved_usd") or 0.0)
    except OSError:
        pass
    return count, round(total_saved, 6)


# --------------------------------------------------------------------------- #
# Routing savings                                                              #
# --------------------------------------------------------------------------- #


def _read_routing_savings(events: list[dict[str, Any]]) -> tuple[int, float]:
    """Extract routing savings from ledger events.

    Returns ``(downtiered_turns, total_saved_usd)``.
    A turn is "downtiered" when ``cost_saved_usd > 0``.
    """
    downtiered = 0
    total_saved = 0.0
    for ev in events:
        if ev.get("kind") != "model_recommendation":
            continue
        payload = ev.get("payload") or ev
        saved = float(payload.get("cost_saved_usd") or 0.0)
        if saved > 0:
            downtiered += 1
            total_saved += saved
    return downtiered, round(total_saved, 6)


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class SessionReport:
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    active_duration_seconds: float
    vendor: str
    agent_settings: dict[str, Any]
    skills: list[str]
    telemetry: dict[str, Any]
    models_used: dict[str, int]
    started_model: str | None
    total_turns: int
    tool_call_count: int

    # Costs
    input_token_cost_usd: float
    cache_write_cost_usd: float
    cache_read_cost_usd: float
    output_token_cost_usd: float
    total_cost_usd: float

    # Tokens
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int

    # Atelier savings
    routing_downtiered_turns: int
    routing_savings_usd: float
    compact_events: int
    compact_savings_estimate_usd: float
    total_atelier_savings_usd: float

    # Sources
    raw_artifact_ids: list[str]

    # Top tools by estimated cost
    top_tools_by_cost: list[tuple[str, int, float]]

    @property
    def is_running(self) -> bool:
        return self.ended_at is None


# --------------------------------------------------------------------------- #
# Build                                                                        #
# --------------------------------------------------------------------------- #


def build_report(snapshot: dict[str, Any], root: Path) -> SessionReport:
    """Build a ``SessionReport`` from a run-ledger *snapshot* dict."""
    from atelier.infra.runtime.cost_tracker import CostTracker

    session_id = str(snapshot.get("session_id") or "")
    status = str(snapshot.get("status") or "running")

    created_at = _parse_dt(snapshot.get("created_at")) or datetime.now(UTC)
    updated_at = _parse_dt(snapshot.get("updated_at")) or created_at
    ended_at: datetime | None = None if status == "running" else updated_at

    # --- events ---
    raw_events: list[dict[str, Any]] = snapshot.get("events") or []

    # Duration from first/last event timestamps
    event_times: list[datetime] = []
    for ev in raw_events:
        ts = _parse_dt(str(ev.get("at") or ""))
        if ts:
            event_times.append(ts)
    first_ts = event_times[0] if event_times else created_at
    last_ts = event_times[-1] if event_times else updated_at
    duration = max(0.0, (last_ts - first_ts).total_seconds())

    # --- active duration ---
    # Sum only the time chunks where the agent is working (from User -> Response)
    active_duration = 0.0
    current_start: datetime | None = None
    for ev in raw_events:
        ts = _parse_dt(str(ev.get("at") or ""))
        if not ts:
            continue
        kind = str(ev.get("kind") or "")
        if kind == "user_message":
            current_start = ts
        else:
            if current_start:
                chunk = (ts - current_start).total_seconds()
                if 0 < chunk < 3600:  # ignore massive gaps/clock drift
                    active_duration += chunk
                current_start = ts
            else:
                current_start = ts

    if active_duration <= 0:
        active_duration = duration

    # --- cost calls ---
    cost_data = snapshot.get("cost") or {}
    calls: list[dict[str, Any]] = cost_data.get("calls") or []
    total_cost = float(cost_data.get("total_cost_usd") or 0.0)

    # models_used: group calls by model
    models_used: dict[str, int] = {}
    for call in calls:
        m = str(call.get("model") or "unknown")
        models_used[m] = models_used.get(m, 0) + 1
    started_model = None
    if calls:
        started_model = str(calls[0].get("model") or "").strip() or None
    total_turns = len(calls)

    vendor = _derive_vendor(models_used)

    # tool_call_count from snapshot or event count
    tool_call_count = int(snapshot.get("tool_call_count") or 0)
    if not tool_call_count:
        tool_call_count = sum(1 for e in raw_events if e.get("kind") == "tool_call")

    # --- per-token cost breakdown ---
    (
        in_tok,
        in_cost,
        out_tok,
        out_cost,
        cr_tok,
        cr_cost,
        cw_tok,
        cw_cost,
    ) = _cost_breakdown_from_calls(calls)

    # Use total_cost from snapshot as ground truth; attribute remainder to input if needed
    cost_sum = in_cost + out_cost + cr_cost + cw_cost
    if cost_sum > 0 and total_cost > 0:
        ratio = total_cost / cost_sum
        in_cost = round(in_cost * ratio, 6)
        out_cost = round(out_cost * ratio, 6)
        cr_cost = round(cr_cost * ratio, 6)
        cw_cost = round(cw_cost * ratio, 6)

    # --- routing savings ---
    routing_downtiered, routing_saved = _read_routing_savings(raw_events)

    # --- compact savings ---
    compact_count, compact_saved = _read_compact_savings(session_id, root)

    total_saved = round(routing_saved + compact_saved, 6)

    # --- per-tool breakdown ---
    top_tools = CostTracker.per_tool_cost_breakdown(raw_events)

    agent_settings = dict(snapshot.get("agent_settings") or {})
    skills = list(snapshot.get("skills") or [])
    telemetry = dict(snapshot.get("telemetry") or {})
    raw_artifact_ids = list(snapshot.get("raw_artifact_ids") or [])

    return SessionReport(
        session_id=session_id,
        started_at=first_ts,
        ended_at=ended_at,
        duration_seconds=duration,
        active_duration_seconds=active_duration,
        vendor=vendor,
        agent_settings=agent_settings,
        skills=skills,
        telemetry=telemetry,
        raw_artifact_ids=raw_artifact_ids,
        models_used=models_used,
        started_model=started_model,
        total_turns=total_turns,
        tool_call_count=tool_call_count,
        input_token_cost_usd=in_cost,
        cache_write_cost_usd=cw_cost,
        cache_read_cost_usd=cr_cost,
        output_token_cost_usd=out_cost,
        total_cost_usd=total_cost,
        input_tokens=in_tok,
        cache_write_tokens=cw_tok,
        cache_read_tokens=cr_tok,
        output_tokens=out_tok,
        routing_downtiered_turns=routing_downtiered,
        routing_savings_usd=routing_saved,
        compact_events=compact_count,
        compact_savings_estimate_usd=compact_saved,
        total_atelier_savings_usd=total_saved,
        top_tools_by_cost=top_tools[:5],
    )


def build_report_from_ledger(ledger: RunLedger, root: Path) -> SessionReport:
    """Build a ``SessionReport`` directly from a live ``RunLedger``."""
    return build_report(ledger.snapshot(), root)


def load_report(session_id: str, root: Path) -> SessionReport | None:
    """Load and build a report from a persisted run file, or *None* if not found."""
    run_path = _runs_dir(root) / f"{session_id}.json"
    if not run_path.exists():
        return None
    try:
        snapshot = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return build_report(snapshot, root)


def list_run_files(root: Path, *, since: datetime | None = None) -> list[Path]:
    """Return run JSON files sorted newest-first, optionally filtered by *since*."""
    runs_dir = _runs_dir(root)
    if not runs_dir.exists():
        return []
    files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if since is None:
        return files
    cutoff = since.timestamp()
    return [f for f in files if f.stat().st_mtime >= cutoff]


# --------------------------------------------------------------------------- #
# Renderers                                                                    #
# --------------------------------------------------------------------------- #


def _fmt_duration(seconds: float, running: bool) -> str:
    if running:
        return "(ongoing)"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def _fmt_tok(n: int) -> str:
    return f"{n:,}"


def _fmt_cost(v: float) -> str:
    return f"${v:.2f}"


def render_text(report: SessionReport, *, no_color: bool = False) -> str:
    """Render a human-readable session report with Unicode box-drawing."""
    sid_short = report.session_id[:6] if report.session_id else "?"
    duration_str = _fmt_duration(report.duration_seconds, report.is_running)
    lines: list[str] = []

    # header
    lines.append(f"Session {sid_short} ({duration_str})")
    lines.append("─" * 49)

    # metadata
    def row(label: str, value: str) -> str:
        return f"  {label:<22}{value}"

    # models used
    models_str = (
        ", ".join(
            f"{m} ({c} turn{'s' if c != 1 else ''})"
            for m, c in sorted(report.models_used.items(), key=lambda x: -x[1])
        )
        or "none"
    )

    lines.append(row("Vendor:", report.vendor))
    lines.append(row("Models used:", models_str))
    lines.append(row("Total turns:", str(report.total_turns)))
    lines.append(row("Tool calls:", str(report.tool_call_count)))
    lines.append("")

    # cost breakdown
    lines.append("Cost breakdown")
    col_w = 10  # token column width
    cost_w = 8  # cost column width

    def cost_row(label: str, tok: int, cost: float, *, show: bool = True) -> str:
        if not show:
            return ""
        tok_str = _fmt_tok(tok).rjust(col_w)
        cost_str = _fmt_cost(cost).rjust(cost_w)
        return f"  {label:<20}{tok_str}  →  {cost_str}"

    lines.append(cost_row("Input tokens:", report.input_tokens, report.input_token_cost_usd))
    if report.cache_write_tokens or report.cache_write_cost_usd:
        lines.append(
            cost_row("Cache writes:", report.cache_write_tokens, report.cache_write_cost_usd)
        )
    lines.append(cost_row("Cache reads:", report.cache_read_tokens, report.cache_read_cost_usd))
    lines.append(cost_row("Output tokens:", report.output_tokens, report.output_token_cost_usd))
    lines.append("  " + "─" * 37)
    lines.append(f"  {'Total:':<32}{_fmt_cost(report.total_cost_usd).rjust(cost_w)}")
    lines.append("")

    # savings
    lines.append("Atelier savings")
    if report.routing_downtiered_turns:
        lines.append(
            f"  Routing recommendations: {report.routing_downtiered_turns} turn"
            f"{'s' if report.routing_downtiered_turns != 1 else ''} downtiered"
            f"  →  saved {_fmt_cost(report.routing_savings_usd)}"
        )
    else:
        lines.append("  Routing recommendations: none this session")

    if report.compact_events:
        lines.append(
            f"  Compaction ({report.compact_events} event"
            f"{'s' if report.compact_events != 1 else ''})"
            f"  →  avoided ~{_fmt_cost(report.compact_savings_estimate_usd)} in resend cost"
        )
    else:
        lines.append("  Compaction: none this session")

    lines.append("  " + "─" * 37)
    lines.append(
        f"  {'Total saved this session:':<32}"
        f"{_fmt_cost(report.total_atelier_savings_usd).rjust(cost_w)}"
    )
    lines.append("")

    # top tools
    if report.top_tools_by_cost:
        n = len(report.top_tools_by_cost)
        lines.append(f"Top {n} costliest tool{'s' if n != 1 else ''} this session")
        for tool_name, count, cost in report.top_tools_by_cost:
            lines.append(
                f"  {tool_name:<16}{count:>5} call{'s' if count != 1 else ''}"
                f"   {_fmt_cost(cost).rjust(cost_w)}"
            )

    return "\n".join(lines)


def render_json(report: SessionReport) -> str:
    """Render report as JSON (datetimes serialised as ISO-8601 strings)."""

    def _default(obj: object) -> str:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")

    return json.dumps(dataclasses.asdict(report), default=_default, indent=2)


__all__ = [
    "SessionReport",
    "build_report",
    "build_report_from_ledger",
    "list_run_files",
    "load_report",
    "render_json",
    "render_text",
]
