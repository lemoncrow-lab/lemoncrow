"""Runs-dashboard and optimization render helpers (QBL-CLI-03).

These formatters were moved verbatim from ``gateway/cli/app.py``; byte-identical
output is a hard requirement (T-25-08). They cover:

* the runs ``status`` dashboard (ported from the old ``bin/lemoncrow-status``),
  including the one-liner status-bar mode and the NO_COLOR fallback, and
* the Optimization Advisor summary / Pareto-frontier detail renderers.

Pure rendering only -- no Click command wiring lives here. The dashboard NO_COLOR
path swaps this module's ANSI globals for the duration of a single render call,
exactly as the original implementation did.
"""

from __future__ import annotations

import json
import logging
import os
import re
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click

from lemoncrow.core.capabilities.optimization.optimizer import potential_savings_breakdown
from lemoncrow.core.capabilities.pricing import fallback_cost_usd
from lemoncrow.core.capabilities.savings_summary import _fmt_tok, _fmt_usd

logger = logging.getLogger(__name__)

# ── Status dashboard helpers (ported from bin/lemoncrow-status) ───────────────

_STATUS_COLORS = {
    "success": "\033[38;2;80;200;120m",
    "complete": "\033[38;2;80;200;120m",
    "failed": "\033[38;2;255;80;80m",
    "error": "\033[38;2;255;80;80m",
    "running": "\033[38;2;255;200;60m",
    "partial": "\033[38;2;255;200;60m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[38;2;80;200;120m"
_RED = "\033[38;2;255;80;80m"
_YELLOW = "\033[38;2;255;200;60m"
_BRAND = "\033[1;38;2;155;117;217m"
_BADGE = "\033[1;48;2;155;117;217;38;2;255;255;255m lemon:code \033[0m"
_SEP = "\033[2;38;2;180;180;180m │\033[0m"
_W = 72


def _age(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        s = max(0, int((datetime.now(UTC) - dt).total_seconds()))
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return "?"


def _dur(t0: str, t1: str) -> str:
    if not t0 or not t1:
        return ""
    try:
        a = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        b = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        s = max(0, int((b - a).total_seconds()))
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m{s % 60:02d}s"
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return ""


def _status_color(s: str) -> str:
    c = _STATUS_COLORS.get(s)
    return f"{c}{s}{_RESET}" if c else f"{_DIM}{s}{_RESET}"


def _status_icon(s: str) -> str:
    if s in ("success", "complete"):
        return f"{_GREEN}✓{_RESET}"
    if s in ("failed", "error"):
        return f"{_RED}✖{_RESET}"
    if s in ("running", "partial"):
        return f"{_YELLOW}⋯{_RESET}"
    return f"{_DIM}?{_RESET}"


def _box_line(content: str = "") -> None:
    plain = re.sub(r"\033\[[^m]*m", "", content)
    pad = max(0, _W - 2 - len(plain))
    click.echo(f" {content}{' ' * pad} ")


def _rule(label: str = "") -> None:
    if label:
        line = f" {_BOLD}{label}{_RESET} "
        fill = _W - len(line) - 2
        click.echo(f"{_DIM}─{_RESET}{line}{_DIM}{'─' * fill}{_RESET}")
    else:
        click.echo(f"{_DIM}{'─' * _W}{_RESET}")


def render_overview(root: Path, *, days: int = 7, n_runs: int = 8) -> str:
    """Render the terminal dashboard: a windowed spend/savings rollup + recent runs.

    Composes the same ``build_insights`` aggregation the ``insights`` CLI and the
    web Savings page read, so every surface agrees on the numbers. Plain text by
    design — the legacy ``_render_dashboard`` ANSI plumbing is intentionally not
    reused (that was the surface asking for a beauty treatment).
    """
    from lemoncrow.infra.runtime.insights import _bar, build_insights
    from lemoncrow.infra.runtime.session_report import build_report, list_run_files

    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    window = build_insights(root, since=since, until=until)

    width = 60
    lines: list[str] = [f"LemonCrow · last {days} days", "─" * width]

    # At-a-glance
    if window.session_count:
        avg_min = int(window.total_duration_seconds / window.session_count / 60)
        total_h, rem = divmod(int(window.total_duration_seconds), 3600)
        lines.append(f"  Sessions   {window.session_count}   (avg {avg_min} min · total {total_h}h {rem // 60}m)")
    else:
        lines.append("  Sessions   0")
    lines.append(f"  AI spend   {_fmt_usd(window.total_cost_usd)}")
    if window.total_cost_usd > 0:
        frac = window.total_lemoncrow_savings_usd / window.total_cost_usd
        lines.append(
            f"  Saved      {_fmt_usd(window.total_lemoncrow_savings_usd)}   ({frac * 100:.0f}% of spend)   {_bar(frac)}"
        )
    else:
        lines.append(f"  Saved      {_fmt_usd(window.total_lemoncrow_savings_usd)}")

    # Cost by model
    if window.cost_by_model:
        lines.append("")
        lines.append("  Cost by model")
        for model, cost in list(window.cost_by_model.items())[:4]:
            frac = cost / window.total_cost_usd if window.total_cost_usd > 0 else 0.0
            lines.append(f"    {model:<20}{_fmt_usd(cost):>9}  {frac * 100:>3.0f}%  {_bar(frac, 14)}")

    # Top tools
    if window.cost_by_tool:
        lines.append("")
        lines.append("  Top tools")
        for tool, cost in list(window.cost_by_tool.items())[:4]:
            lines.append(f"    {tool:<20}{_fmt_usd(cost):>9}")

    # Recent runs -- ordered by the snapshot's own updated_at/created_at, the
    # same timestamp the age column displays. run.json mtime is not usable as
    # the sort key: the file gets rewritten (imports, bridge refreshes) without
    # refreshing updated_at, so mtime order made the age column non-monotonic.
    candidates: list[tuple[float, dict[str, Any]]] = []
    for run_file in list_run_files(root):
        try:
            snap: dict[str, Any] = json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - best-effort per-run row; skip unreadable files
            continue
        ts_raw = str(snap.get("updated_at") or snap.get("created_at") or "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = run_file.stat().st_mtime
        candidates.append((ts, snap))
    candidates.sort(key=lambda item: item[0], reverse=True)

    rows_added = False
    for _ts, snap in candidates[:n_runs]:
        try:
            # Small, bounded n_runs (default 8) -- cheap to include the
            # transcript-based carry component here, unlike bulk aggregation
            # paths (see build_report's docstring).
            report = build_report(snap, root, include_carry_credit=True)
        except Exception:  # noqa: BLE001 - best-effort per-run row; skip unreadable files
            continue
        if not rows_added:
            lines.append("")
            lines.append("  Recent runs")
            rows_added = True
        sid = (report.session_id or str(snap.get("run_id") or snap.get("id") or ""))[:6] or "?"
        agent = str(snap.get("agent") or "-")[:10]
        task_line = (str(snap.get("task") or "").strip().splitlines() or [""])[0]
        task = (task_line[:26] + "…") if len(task_line) > 26 else (task_line or "-")
        status = str(snap.get("status") or "?")
        mark = {"success": "✓", "complete": "✓", "failed": "✗", "error": "✗"}.get(status, "•")
        age = _age(str(snap.get("updated_at") or snap.get("created_at") or ""))
        lines.append(
            f"    {sid:<7}{agent:<11}{task:<28}{mark} {_fmt_usd(report.total_cost_usd):>8}  "
            f"saved {_fmt_usd(report.total_lemoncrow_savings_usd):>8}  {age}"
        )

    if window.session_count or rows_added:
        lines.append("")
        lines.append("  → drill into a run:  lemon session report <id>")
        lines.append("    open in browser:   lemon dashboard open")
    else:
        lines.append("")
        lines.append("  No sessions yet — run any AI command, then check back.")

    return "\n".join(lines)


def _render_dashboard(root: Path, *, line_mode: bool, n_runs: int, session_id: str | None) -> None:
    """Render the runs dashboard (same output as the old lemoncrow-status binary)."""

    # When NO_COLOR is set, suppress all ANSI by swapping module-level globals
    # for the duration of this call.
    if os.environ.get("NO_COLOR"):
        saved = {
            "_BRAND": _BRAND,
            "_BADGE": _BADGE,
            "_SEP": _SEP,
            "_DIM": _DIM,
            "_RESET": _RESET,
            "_GREEN": _GREEN,
            "_RED": _RED,
            "_YELLOW": _YELLOW,
            "_BOLD": _BOLD,
        }
        for k in saved:
            globals()[k] = ""
        try:
            return _render_dashboard_impl(root, line_mode, n_runs, session_id)
        finally:
            for k, v in saved.items():
                globals()[k] = v
    else:
        return _render_dashboard_impl(root, line_mode, n_runs, session_id)


def _render_dashboard_impl(root: Path, line_mode: bool, n_runs: int, session_id: str | None) -> None:
    sessions_dir = root / "sessions"

    # Resolve ledger path
    ledger_path: str | None = None
    if session_id:
        from lemoncrow.core.foundation.paths import find_session_dir

        existing = find_session_dir(root, session_id)
        candidate = (existing / "run.json") if existing is not None else None
        if candidate is not None and candidate.exists():
            ledger_path = str(candidate)
    elif sessions_dir.is_dir():
        files = sorted(sessions_dir.glob("**/run.json"), key=os.path.getmtime, reverse=True)
        if files:
            ledger_path = str(files[0])
    if not ledger_path:
        ledger_path = "NONE"

    # Load savings from the canonical per-session ledger (store A) — the same
    # source the statusline, `lemon savings` CLI, and web Savings page read,
    # so the dashboard's saved totals agree with every other surface.
    from lemoncrow.core.capabilities.savings_summary import _price_savings_row

    savings_map: dict[str, float] = {}
    routing_map: dict[str, float] = {}
    compaction_map: dict[str, float] = {}
    routing_total = 0.0
    compaction_total = 0.0
    sessions_root = root / "sessions"
    if sessions_root.is_dir():
        for sidecar in sessions_root.glob("**/savings.jsonl"):
            rid = sidecar.parent.name
            try:
                sidecar_lines = sidecar.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in sidecar_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict) or d.get("kind") == "session_end":
                    continue
                _pt, usd, _calls, calls_usd, _up = _price_savings_row(d)
                cost = usd + calls_usd
                if cost <= 0:
                    continue
                savings_map[rid] = savings_map.get(rid, 0.0) + cost
                kind = str(d.get("kind") or "")
                if kind == "routing":
                    routing_map[rid] = routing_map.get(rid, 0.0) + cost
                    routing_total += cost
                elif kind == "compaction":
                    compaction_map[rid] = compaction_map.get(rid, 0.0) + cost
                    compaction_total += cost

    # Load cost + token data from DB
    cost_map: dict[str, float] = {}
    tokens_map: dict[str, int] = {}
    db_runs: list[dict[str, Any]] = []
    total_runs_in_db = 0
    db_path = root / "lemoncrow.db"
    if db_path.exists():
        try:
            import sqlite3

            from lemoncrow.core.capabilities.pricing import usage_cost_usd
            from lemoncrow.core.capabilities.savings_summary import resolve_model_id
            from lemoncrow.core.foundation.store import ContextStore

            cstore = ContextStore(root)
            token_rows = cstore.token_rows()
            for trow in token_rows:
                sid = trow["session_id"]
                inp, out, cr, th = (
                    trow["input_tokens"],
                    trow["output_tokens"],
                    trow["cached_input_tokens"],
                    trow["thinking_tokens"],
                )
                cost_map[sid] = cost_map.get(sid, 0.0) + usage_cost_usd(
                    resolve_model_id(trow.get("model")),
                    input_tokens=inp or 0,
                    output_tokens=out or 0,
                    cache_read_tokens=cr or 0,
                    thinking_tokens=th or 0,
                )
                tokens_map[sid] = tokens_map.get(sid, 0) + (inp or 0) + (out or 0) + (cr or 0) + (th or 0)
            total_runs_in_db = len(token_rows)
            db_runs = cstore.list_trace_payloads(limit=1000)

            # context_budget remains in lemoncrow.db (separate table, not traces).
            # Group by model so each row is priced with its own rate, then
            # overwrite the trace-derived totals (one session can span models).
            budget_cost: dict[str, float] = {}
            budget_tokens: dict[str, int] = {}
            with sqlite3.connect(str(db_path)) as conn:
                for row in conn.execute(
                    "SELECT session_id, model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) "
                    "FROM context_budget GROUP BY session_id, model"
                ):
                    rid, model, inp, out, cr = row
                    budget_cost[rid] = budget_cost.get(rid, 0.0) + usage_cost_usd(
                        resolve_model_id(model),
                        input_tokens=inp or 0,
                        output_tokens=out or 0,
                        cache_read_tokens=cr or 0,
                    )
                    budget_tokens[rid] = budget_tokens.get(rid, 0) + (inp or 0) + (out or 0) + (cr or 0)
            cost_map.update(budget_cost)
            tokens_map.update(budget_tokens)
        except Exception:
            logging.exception("dashboard trace read failed")
            # Best-effort SQLite trace read; dashboard still renders without DB data.
            logger.debug("dashboard trace read failed", exc_info=True)

    # Load flat ledger if exists
    def _load_run(path: str) -> dict[str, Any] | None:
        try:
            return typing.cast(dict[str, Any], json.loads(Path(path).read_text()))
        except Exception:
            logger.exception("dashboard run load failed")
            return None

    snap: dict[str, Any] | None = None
    if ledger_path != "NONE":
        snap = _load_run(ledger_path)

    if not snap and session_id:
        snap = next(
            (r for r in db_runs if r.get("session_id") == session_id or r.get("id") == session_id),
            None,
        )

    if not snap and db_runs and not session_id:
        snap = db_runs[0]

    # ── ONE-LINER MODE ──
    if line_mode:
        if not snap:
            click.echo(f"lemon | run {Path(ledger_path).stem[:8] if ledger_path != 'NONE' else '?'} not found")
            return

        sid = snap.get("session_id") or snap.get("id") or "?"
        domain = snap.get("domain") or "-"
        task = (snap.get("task") or "").strip().splitlines()[0] if snap.get("task") else "-"
        if len(task) > 50:
            task = task[:47] + "..."
        status = snap.get("status") or "?"
        events = len(snap.get("events", []) or [])
        errors = len(snap.get("errors_seen", []) or [])
        blockers = len(snap.get("current_blockers", []) or [])
        files_n = len(snap.get("files_touched", []) or [])
        tools_n = int(snap.get("tool_call_count", 0) or snap.get("tool_count", 0) or len(snap.get("tools_called", [])))
        agent = snap.get("agent") or "?"
        age_str = _age(snap.get("updated_at") or snap.get("created_at") or "")
        dur_str = _dur(snap.get("created_at", ""), snap.get("updated_at", ""))

        cost_v = cost_map.get(sid, float(snap.get("cost", {}).get("total_cost_usd", 0.0)))
        if cost_v == 0 and "input_tokens" in snap:
            cost_v = fallback_cost_usd(
                input_tokens=int(snap.get("input_tokens", 0) or 0),
                output_tokens=int(snap.get("output_tokens", 0) or 0),
            )

        saved_v = savings_map.get(sid, 0.0)
        routing_v = routing_map.get(sid, 0.0)
        compaction_v = compaction_map.get(sid, 0.0)

        saved_seg = ""
        if saved_v > 0:
            breakdown = []
            if compaction_v > 0:
                breakdown.append(f"compact={_fmt_usd(compaction_v)}")
            if routing_v > 0:
                breakdown.append(f"routing={_fmt_usd(routing_v)}")
            suffix = f" ({', '.join(breakdown)})" if breakdown else ""
            saved_seg = f" {_SEP} {_GREEN}saved={_fmt_usd(saved_v)}{suffix}{_RESET}"

        line = (
            f"{_BADGE} {_BRAND}run {sid[:8]}{_RESET} {_SEP} {_DIM}{agent}{_RESET} {_SEP} "
            f"{domain} {_SEP} {task} {_SEP} {_status_color(status)} "
            f"{_SEP} ev={events} err={errors} blk={blockers}"
            f" {_SEP} files={files_n} tools={tools_n}"
            + (f" {_SEP} cost={_fmt_usd(cost_v)}" if cost_v > 0 else "")
            + saved_seg
            + (f" {_SEP} {dur_str}" if dur_str else "")
            + f" {_SEP} {_DIM}{age_str}{_RESET}"
        )
        click.echo(line)
        return

    # ── DASHBOARD MODE (default) ──
    all_run_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if sessions_dir.is_dir():
        for rf in sorted(sessions_dir.glob("**/run.json"), key=os.path.getmtime, reverse=True):
            try:
                d = json.loads(rf.read_text())
                rid = d.get("session_id") or rf.stem
                if isinstance(rid, str):
                    all_run_entries.append(d)
                    seen_ids.add(rid)
            except Exception:
                logging.exception("dashboard run-json parse failed")
                # Best-effort run-JSON parse; skip unreadable/malformed run files.
                logger.debug("dashboard run-json parse failed", exc_info=True)

    for dr in db_runs:
        rid_val = dr.get("session_id") or dr.get("id")
        if isinstance(rid_val, str) and rid_val not in seen_ids:
            all_run_entries.append(dr)
            seen_ids.add(rid_val)

    total_runs = max(total_runs_in_db, len(seen_ids))
    success_count = 0
    failed_count = 0
    total_tools = 0
    total_files = 0
    total_errors = 0

    for d in all_run_entries:
        s = d.get("status", "")
        if s in ("success", "complete"):
            success_count += 1
        elif s in ("failed", "error"):
            failed_count += 1
        total_tools += int(d.get("tool_call_count", 0) or d.get("tool_count", 0) or len(d.get("tools_called", [])))
        total_files += len(d.get("files_touched", []) or [])
        total_errors += len(d.get("errors_seen", []) or [])

    total_cost = sum(cost_map.values())
    saved_usd = sum(savings_map.values())
    total_tokens = sum(tokens_map.values())

    _rule("SYSTEM OVERVIEW")
    _box_line(f"{_BADGE}  {_DIM}{root}{_RESET}")

    sr = f"{_GREEN}{success_count} ok{_RESET}" if success_count else f"{_DIM}0 ok{_RESET}"
    fr = f"{_RED}{failed_count} failed{_RESET}" if failed_count else f"{_DIM}0 failed{_RESET}"
    _box_line(
        f"{_BOLD}{total_runs}{_RESET} runs  {sr}  {fr}  "
        f"{_DIM}tools={_fmt_tok(total_tools)}  files={total_files}  errs={total_errors}{_RESET}"
    )
    if total_cost > 0 or saved_usd > 0:
        parts = []
        if compaction_total > 0:
            parts.append(f"compact {_fmt_usd(compaction_total)}")
        if routing_total > 0:
            parts.append(f"routing {_fmt_usd(routing_total)}")
        breakdown_str = f"  {_DIM}({' · '.join(parts)}){_RESET}" if parts else ""
        _box_line(
            f"{_DIM}cost{_RESET} {_fmt_usd(total_cost)}  "
            + (f"{_GREEN}saved{_RESET} {_fmt_usd(saved_usd)}{breakdown_str}" if saved_usd > 0 else "")
            + (f"  {_DIM}tokens{_RESET} {_fmt_tok(total_tokens)}" if total_tokens else "")
        )

    shown = min(n_runs, len(all_run_entries))
    _rule(f"RECENT RUNS ({shown})")

    for d in all_run_entries[:n_runs]:
        sid = d.get("session_id") or d.get("id") or "?"
        agent = (d.get("agent") or "?")[:8]
        domain = (d.get("domain") or "-")[:12]
        task = (d.get("task") or "").strip().replace("\n", " ")
        if len(task) > 55:
            task = task[:52] + "..."
        if not task:
            task = f"{_DIM}(no task){_RESET}"
        status = d.get("status") or "?"
        files_n = len(d.get("files_touched", []) or [])
        tools_n = int(d.get("tool_call_count", 0) or d.get("tool_count", 0) or len(d.get("tools_called", [])))
        # errs_n intentionally unused; kept for debugging access
        age_str = _age(d.get("updated_at") or d.get("created_at") or "")
        dur_str = _dur(d.get("created_at", ""), d.get("updated_at", ""))

        cost_v = cost_map.get(sid, float(d.get("cost", {}).get("total_cost_usd", 0.0)))
        if cost_v == 0 and "input_tokens" in d:
            cost_v = fallback_cost_usd(
                input_tokens=int(d.get("input_tokens", 0) or 0),
                output_tokens=int(d.get("output_tokens", 0) or 0),
            )

        saved_v = savings_map.get(sid, 0.0)
        routing_v = routing_map.get(sid, 0.0)
        compaction_v = compaction_map.get(sid, 0.0)

        dots = "." * max(1, (_W - len(re.sub(r"\033\[[^m]*m", "", task)) - 16))
        _box_line(f" {_status_icon(status)}  {_BOLD}{task}{_RESET} {_DIM}{dots} {sid[:8]}{_RESET}")

        metrics = []
        if cost_v > 0:
            metrics.append(f"cost={_fmt_usd(cost_v)}")
        if saved_v > 0:
            breakdown_parts = []
            if compaction_v > 0:
                breakdown_parts.append(f"c={_fmt_usd(compaction_v)}")
            if routing_v > 0:
                breakdown_parts.append(f"r={_fmt_usd(routing_v)}")
            run_breakdown_str = f" {_DIM}({' '.join(breakdown_parts)}){_RESET}" if breakdown_parts else ""
            metrics.append(f"{_GREEN}saved={_fmt_usd(saved_v)}{run_breakdown_str}")
        if dur_str:
            metrics.append(dur_str)
        metrics_str = f" {_SEP} ".join(metrics)

        meta_line = f"    {_DIM}{age_str}{_RESET} {_SEP} {agent} {_SEP} {domain}"
        if metrics_str:
            meta_line += f" {_SEP} {metrics_str}"
        meta_line += f" {_SEP} {_DIM}f={files_n} t={tools_n}{_RESET}"
        _box_line(meta_line)

    _rule()
    _box_line(f"{_DIM}store: {root}   runs dir: {sessions_dir}{_RESET}")
    _rule()


# ── Optimization Advisor render helpers ─────────────────────────────────────


def _recommended_candidate(result: Any) -> Any:
    if not result.has_recommendation:
        return None
    target_cost = result.baseline_weekly_cost_usd - result.weekly_savings_usd
    candidates = [candidate for candidate in result.candidates if candidate.id != "current"]
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs(candidate.weekly_cost_usd - target_cost))


def _render_optimization_summary(result: Any) -> None:
    current = next(candidate for candidate in result.candidates if candidate.id == "current")
    recommended = _recommended_candidate(result)
    click.echo("Optimization Autopilot")
    click.echo("─────────────────────────────────────────────────")
    click.echo(
        f"Analysed your last 7 days: {result.sessions_analysed} sessions, {result.replayable_tasks} replayable tasks"
    )
    click.echo("")
    click.echo(f"Current setting: {result.current_policy.name}")
    click.echo(f"  Cost / week:      ${current.weekly_cost_usd:.2f}")
    click.echo(f"  Estimated quality: {current.estimated_quality:.1%}")
    click.echo(f"  Latency mult:      {current.latency_mult:.2f}x")
    click.echo(f"  Escalation rate:   {current.escalation_rate:.0%}")
    click.echo("")
    if recommended is None:
        click.echo(result.message)
    else:
        savings_pct = (
            result.weekly_savings_usd / result.baseline_weekly_cost_usd if result.baseline_weekly_cost_usd > 0 else 0.0
        )
        click.echo("Recommended: Custom (auto-tuned from your sessions)")
        click.echo(f"  Cost / week:      ${recommended.weekly_cost_usd:.2f}  (-{savings_pct:.0%})")
        click.echo(f"  Estimated quality: {recommended.estimated_quality:.1%}  ({result.quality_delta:+.1%})")
        click.echo(f"  Latency mult:      {recommended.latency_mult:.2f}x")
        click.echo(f"  Escalation rate:   {recommended.escalation_rate:.0%}")
        click.echo("")
        breakdown = potential_savings_breakdown(recommended, result.baseline_weekly_cost_usd, result.weekly_savings_usd)
        click.echo("  Savings breakdown (Read / Carry / Output / Routing / Total):")
        click.echo(f"    Read savings:     {_fmt_usd(breakdown['read_saved_usd'])}/wk")
        click.echo(f"    Carry credit:     {_fmt_usd(breakdown['carry_saved_usd'])}/wk")
        click.echo("    Output savings:   $0.00/wk (not yet modeled)")
        click.echo(f"    Routing savings:  {_fmt_usd(breakdown['routing_saved_usd'])}/wk")
        click.echo(f"    Total saved:      {_fmt_usd(breakdown['total_saved_usd'])}/wk")
    click.echo("")
    click.echo(f"Confidence: {result.confidence.title()}")
    click.echo(f"  {result.confidence_reason}")
    click.echo(
        f"Golden corpus: {result.golden.passed}/{result.golden.total} well-formed tasks ({result.golden.score:.0%})"
    )


def _render_optimization_details(result: Any) -> None:
    click.echo("Pareto frontier - cost vs estimated correctness on your tasks")
    click.echo("─────────────────────────────────────────────────")
    sorted_candidates = sorted(result.candidates, key=lambda item: item.weekly_cost_usd, reverse=True)
    recommended = _recommended_candidate(result)
    for candidate in sorted_candidates:
        marker = "★" if recommended is not None and candidate.id == recommended.id else " "
        label = candidate.policy.name
        click.echo(
            f"{marker} {label:<18} ${candidate.weekly_cost_usd:>7.2f}   "
            f"{candidate.estimated_quality:>6.1%}   latency {candidate.latency_mult:.2f}x   "
            f"escalation {candidate.escalation_rate:.0%}"
        )

    if recommended is None:
        return
    click.echo("")
    breakdown = potential_savings_breakdown(recommended, result.baseline_weekly_cost_usd, result.weekly_savings_usd)
    click.echo("Savings breakdown for [recommended] (Read / Carry / Output / Routing / Total):")
    click.echo(f"  Read savings:    {_fmt_usd(breakdown['read_saved_usd'])}/wk")
    click.echo(f"  Carry credit:    {_fmt_usd(breakdown['carry_saved_usd'])}/wk")
    click.echo("  Output savings:  $0.00/wk (not yet modeled)")
    click.echo(f"  Routing savings: {_fmt_usd(breakdown['routing_saved_usd'])}/wk")
    click.echo(f"  Total saved:     {_fmt_usd(breakdown['total_saved_usd'])}/wk")
    click.echo("")
    click.echo("Compaction breakdown for [recommended]:")
    for name, saved in recommended.compaction_breakdown.items():
        click.echo(f"  {name}: ${saved:.2f}/wk saved")
    click.echo("")
    click.echo("Routing breakdown for [recommended]:")
    for tier, share in recommended.routing_breakdown.items():
        click.echo(f"  {tier}-tier for {share:.0%} of turns")
    click.echo(f"  Escalation rate: {recommended.escalation_rate:.0%}")
