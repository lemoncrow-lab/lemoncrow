"""Thin savings / external-report / optimize command surfaces (QBL-CLI-02).

This module hosts the ``savings``, ``savings-detail``, ``savings-reset``,
``external-status``, ``external-report`` commands and the ``optimize`` group
(with its ``shadow`` subgroup). Pure rendering lives in
``core.capabilities.reporting.dashboard``; these callbacks keep their original
Click wiring, option defaults, and output formatting verbatim. The data-fetch
helpers (``_advisor_result`` etc.) are command-layer glue that load the store
via ``ctx`` and stay module-private here.

Commands are defined as standalone Click objects (Pattern 1) so
``commands/__init__.py`` can ``add_command`` them onto the root ``cli`` group.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from atelier.gateway.cli.commands._shared import (
    _emit,
    _ledger_dir,
    _load_smart_state,
    _load_store,
    _save_smart_state,
)
from atelier.gateway.hosts.session_parsers.registry import SUPPORTED_SESSION_IMPORT_HOSTS
from atelier.gateway.integrations.external_analytics import REPORTABLE_TOOL_IDS

logger = logging.getLogger(__name__)

# `--tool` choices for the external-report CLI. Built once from the source-of-
# truth `SPECS` tuple plus the special-case `codeburn:optimize` sub-report and
# the `all` aggregator. Adding a new analyzer to external_analytics.SPECS now
# flows here automatically - no second hardcoded list to keep in sync.
_EXTERNAL_REPORT_TOOL_CHOICES = ("all", *REPORTABLE_TOOL_IDS, "codeburn:optimize")
# Order matters for the human-readable `all` iteration: keep it focused on the
# core report trio and leave newer analyzers available via explicit --tool.
_EXTERNAL_REPORT_ALL_TOOLS = (
    *(t for t in REPORTABLE_TOOL_IDS if t in {"tokscale", "codeburn"}),
    "codeburn:optimize",
)


@click.command("savings")
@click.option("--json", "as_json", is_flag=True)
@click.option("--line", is_flag=True, help="Pipe-delimited one-liner for statusline.sh.")
@click.pass_context
def savings_cmd(ctx: click.Context, as_json: bool, line: bool) -> None:
    """Aggregate savings: cache + reasoning-library + cost-delta vs. baseline."""
    if line:
        from atelier.core.capabilities.savings_summary import savings_line

        click.echo(
            savings_line(
                os.environ.get("ATELIER_STATUS_SESSION_ID", ""),
                workspace=os.environ.get("CLAUDE_WORKSPACE_ROOT", "") or None,
            )
        )
        return
    from atelier.core.capabilities.plugin_runtime import build_savings_report
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report
    runs = _ledger_dir(ctx.obj["root"])
    bad_plans_blocked = 0
    rescue_events = 0
    rubric_failures = 0
    if runs.is_dir():
        for p in runs.glob("*.json"):
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for ev in snap.get("events", []):
                kind = ev.get("kind")
                if kind == "watchdog_alert":
                    sev = (ev.get("payload") or {}).get("severity")
                    if sev == "high":
                        rescue_events += 1
                if kind == "rubric_run" and (ev.get("payload") or {}).get("status") == "blocked":
                    rubric_failures += 1
    payload = build_savings_report(ctx.obj["root"])
    store = _load_store(ctx.obj["root"])
    payload["optimization"] = build_trace_optimization_report(store.list_traces(limit=5000), days=7)
    payload["bad_plans_blocked"] = bad_plans_blocked
    payload["rescue_events"] = rescue_events
    payload["rubric_failures_caught"] = rubric_failures
    if as_json:
        _emit(payload, as_json=True)
    else:
        for k, v in payload.items():
            if isinstance(v, dict):
                click.echo(f"{k}:")
                for k2, v2 in v.items():
                    click.echo(f"  {k2}: {v2}")
            else:
                click.echo(f"{k}: {v}")


def _legacy_optimize_report(ctx: click.Context, host: str | None, days: int, limit: int) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report

    store = _load_store(ctx.obj["root"])
    return build_trace_optimization_report(store.list_traces(limit=5000), days=days, host=host, limit=limit)


def _run_external_optimize(ctx: click.Context, days: int) -> dict[str, Any] | None:
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_reports,
    )

    period = "week" if days <= 7 else "30days"
    try:
        external_batch = run_external_reports(
            tool="codeburn:optimize", period=period, cwd=Path.cwd(), include_optimize=True
        )
        store = _load_store(ctx.obj["root"])
        persist_external_reports(store, external_batch, source="cli_optimize")
        return external_batch["reports"][0] if external_batch["reports"] else None
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.debug("External optimization report failed: %s", exc)
        return None


def _advisor_result(ctx: click.Context, host: str | None, days: int) -> Any:
    from atelier.core.capabilities.optimization import load_current_policy, optimize_from_traces

    store = _load_store(ctx.obj["root"])
    current_policy = load_current_policy(ctx.obj["root"])
    return optimize_from_traces(store.list_traces(limit=5000), current_policy=current_policy, days=days, host=host)


@click.group("optimize", invoke_without_command=True)
@click.option(
    "--host",
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    default=None,
)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--limit", default=6, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_group(ctx: click.Context, host: str | None, days: int, limit: int, as_json: bool) -> None:
    """Show and apply Optimization Advisor recommendations."""
    if ctx.invoked_subcommand is not None:
        return

    from atelier.core.capabilities.optimization import append_history
    from atelier.core.capabilities.reporting.dashboard import _render_optimization_summary

    report = _legacy_optimize_report(ctx, host, days, limit)
    result = _advisor_result(ctx, host, days)
    append_history(ctx.obj["root"], result)
    report["advisor"] = result.to_dict()
    report["external"] = _run_external_optimize(ctx, days)
    if as_json:
        _emit(report, as_json=True)
        return
    _render_optimization_summary(result)
    click.echo("")
    click.echo(
        f"Legacy trace recommendations: {report['estimated_tokens_saved']} tokens, "
        f"${report['estimated_usd_saved']:.4f}"
    )
    if not report["recommendations"]:
        click.echo("No legacy trace recommendations found for this window.")
        return
    for index, recommendation in enumerate(report["recommendations"], start=1):
        click.echo("")
        click.echo(f"{index}. {recommendation['title']}  {recommendation['severity']}")
        click.echo(f"   Sessions: {recommendation['session_count']}")
        click.echo(
            f"   Savings: {recommendation['estimated_tokens_saved']} tokens, ${recommendation['estimated_usd_saved']:.4f}"
        )
        click.echo(f"   Action: {recommendation['action']}")


@optimize_group.command("details")
@click.option("--host", type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)), default=None)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_details(ctx: click.Context, host: str | None, days: int, as_json: bool) -> None:
    """Show Pareto frontier, compaction, and routing breakdowns."""
    from atelier.core.capabilities.reporting.dashboard import _render_optimization_details

    result = _advisor_result(ctx, host, days)
    if as_json:
        _emit(result.to_dict(), as_json=True)
        return
    _render_optimization_details(result)


@optimize_group.command("apply")
@click.option("--preset", type=click.Choice(["conservative", "balanced", "economy"]), default=None)
@click.option("--recommended", is_flag=True)
@click.option("--custom", type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_apply(
    ctx: click.Context,
    preset: str | None,
    recommended: bool,
    custom: Path | None,
    as_json: bool,
) -> None:
    """Apply a preset, the latest recommendation, or a custom policy YAML."""
    from atelier.core.capabilities.optimization.policy import (
        policy_from_config,
        preset_policy,
        save_policy,
    )

    selected = sum(1 for value in (preset, custom) if value is not None) + (1 if recommended else 0)
    if selected != 1:
        raise click.ClickException("choose exactly one of --preset, --recommended, or --custom")

    if preset is not None:
        policy = preset_policy(preset)
    elif custom is not None:
        import yaml as _yaml

        try:
            raw = _yaml.safe_load(custom.read_text(encoding="utf-8"))
        except _yaml.YAMLError as exc:
            raise click.ClickException(f"invalid custom policy YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise click.ClickException("custom policy YAML must be a mapping")
        policy = policy_from_config(raw)
    else:
        result = _advisor_result(ctx, None, 7)
        if not result.has_recommendation:
            raise click.ClickException(result.message)
        policy = result.recommended_policy

    path = save_policy(ctx.obj["root"], policy)
    payload = {"applied": policy.to_dict(), "path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo(f"Applied optimization policy: {policy.name} ({policy.preset})")
        click.echo(f"Saved: {path}")


@optimize_group.group("shadow", invoke_without_command=True)
@click.option("--policy", "policy_name", default="recommended", show_default=True)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--max-daily-spend-usd", type=float, default=None)
@click.option("--i-understand-this-costs-money", is_flag=True)
@click.option("--yes", is_flag=True, help="Accept the pre-run shadow cost estimate.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow(
    ctx: click.Context,
    policy_name: str,
    days: int,
    max_daily_spend_usd: float | None,
    i_understand_this_costs_money: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Shadow-run a policy in parallel without changing live behavior."""
    if ctx.invoked_subcommand is not None:
        return

    from atelier.core.capabilities.optimization.policy import (
        record_shadow_consent,
        shadow_consent_at,
    )
    from atelier.core.capabilities.optimization.shadow import build_shadow_state, save_shadow_state

    if shadow_consent_at(ctx.obj["root"]) is None:
        if not i_understand_this_costs_money:
            raise click.ClickException(
                "First shadow run requires --i-understand-this-costs-money because it may spend real money."
            )
        record_shadow_consent(ctx.obj["root"])

    result = _advisor_result(ctx, None, max(1, days))
    try:
        state = build_shadow_state(
            policy=policy_name,
            days=days,
            baseline_weekly_cost_usd=result.baseline_weekly_cost_usd,
            max_daily_spend_usd=max_daily_spend_usd,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json and not yes:
        _emit(
            {
                "status": "confirmation_required",
                "message": "Shadow run not started. Re-run with --yes to accept the pre-run cost estimate.",
                "estimate": state.to_dict(),
            },
            as_json=True,
        )
        return
    if not as_json and not yes:
        click.echo(
            f"Shadow will spend approximately ${state.estimated_weekly_spend_usd:.2f} this week "
            f"against your ${state.baseline_weekly_cost_usd:.2f} baseline."
        )
        if not click.confirm("Continue?", default=False):
            click.echo("Shadow run cancelled.")
            return

    save_shadow_state(ctx.obj["root"], state)
    if as_json:
        _emit(state.to_dict(), as_json=True)
    else:
        click.echo(f"Shadow run started for policy {policy_name}.")


@optimize_shadow.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_status(ctx: click.Context, as_json: bool) -> None:
    """Show live shadow spend versus cap."""
    from atelier.core.capabilities.optimization.shadow import load_shadow_state

    state = load_shadow_state(ctx.obj["root"]) or {"status": "not_running"}
    if as_json:
        _emit(state, as_json=True)
        return
    click.echo(f"Shadow status: {state.get('status', 'not_running')}")
    if state.get("status") != "not_running":
        click.echo(
            f"Shadow spend (this run only): ${float(state.get('spend_usd', 0.0)):.2f} / "
            f"${float(state.get('max_daily_spend_usd', 0.0)):.2f} daily cap"
        )


@optimize_shadow.command("stop")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_stop(ctx: click.Context, as_json: bool) -> None:
    """Halt the active shadow run immediately."""
    from atelier.core.capabilities.optimization.shadow import stop_shadow

    state = stop_shadow(ctx.obj["root"])
    if as_json:
        _emit(state, as_json=True)
    else:
        click.echo(f"Shadow status: {state.get('status')}")


@optimize_shadow.command("forget-consent")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_forget_consent(ctx: click.Context, as_json: bool) -> None:
    """Revoke persistent shadow-run cost consent."""
    from atelier.core.capabilities.optimization.policy import forget_shadow_consent

    revoked = forget_shadow_consent(ctx.obj["root"])
    payload = {"revoked": revoked}
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo("Shadow consent revoked." if revoked else "No shadow consent was recorded.")


@optimize_group.command("compare")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_compare(ctx: click.Context, as_json: bool) -> None:
    """Compare current policy with the active or latest shadow run."""
    from atelier.core.capabilities.optimization.shadow import load_shadow_state

    result = _advisor_result(ctx, None, 7)
    state = load_shadow_state(ctx.obj["root"]) or {"status": "not_running", "spend_usd": 0.0}
    payload = {"advisor": result.to_dict(), "shadow": state}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Current weekly cost: ${result.baseline_weekly_cost_usd:.2f}")
    if result.has_recommendation:
        click.echo(f"Recommended weekly savings: ${result.weekly_savings_usd:.2f}")
    click.echo(f"Shadow spend (this run only): ${float(state.get('spend_usd', 0.0)):.2f}")


@optimize_group.command("history")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_history(ctx: click.Context, limit: int, as_json: bool) -> None:
    """Show past optimization recommendations and outcomes."""
    from atelier.core.capabilities.optimization import load_history

    history = load_history(ctx.obj["root"], limit=limit)
    if as_json:
        _emit(history, as_json=True)
        return
    if not history:
        click.echo("No optimization history recorded yet.")
        return
    for item in reversed(history):
        recorded_at = item.get("recorded_at", "-")
        confidence = item.get("confidence", "-")
        savings = float(item.get("weekly_savings_usd", 0.0) or 0.0)
        click.echo(f"{recorded_at}  confidence={confidence}  weekly_savings=${savings:.2f}")


@click.command("external-status")
@click.option("--json", "as_json", is_flag=True)
def external_status_cmd(as_json: bool) -> None:
    """Show optional upstream analyzer availability and integration posture."""
    from atelier.gateway.integrations.external_analytics import external_status

    payload = {"tools": external_status(cwd=Path.cwd())}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("External analyzers")
    click.echo("")
    for item in payload["tools"]:
        state = "available" if item["available"] else "missing"
        click.echo(f"- {item['display_name']} [{state}]")
        click.echo(f"  license: {item['license']}")
        click.echo(f"  mode: {item['execution_mode']}")
        if item.get("path"):
            click.echo(f"  path: {item['path']}")
        click.echo(f"  update: {item['update_strategy']}")
        for note in item.get("notes", []):
            click.echo(f"  note: {note}")
        warning = item.get("warning")
        if warning:
            click.echo(f"  warning: {warning}")
        click.echo(f"  install: {item['install_hint']}")
        click.echo("")


@click.command("external-report")
@click.option(
    "--tool",
    type=click.Choice(_EXTERNAL_REPORT_TOOL_CHOICES),
    default="all",
    show_default=True,
)
@click.option(
    "--period",
    type=click.Choice(["today", "week", "month", "30days", "all"]),
    default="week",
    show_default=True,
)
@click.option(
    "--persist",
    is_flag=True,
    default=False,
    help="Store the collected report snapshots for the API/UI.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def external_report_cmd(ctx: click.Context, tool: str, period: str, persist: bool, as_json: bool) -> None:
    """Run upstream JSON reports from supported external analyzers."""
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_report,
        run_external_reports,
    )

    if as_json:
        try:
            payload = run_external_reports(tool=tool, period=period, cwd=Path.cwd(), include_optimize=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        if persist:
            store = _load_store(ctx.obj["root"])
            payload["persisted"] = persist_external_reports(store, payload, source="cli")
        _emit(payload, as_json=True)
        return

    selected_tools = list(_EXTERNAL_REPORT_ALL_TOOLS) if tool == "all" else [tool]
    store = _load_store(ctx.obj["root"]) if persist else None

    click.echo(f"External reports  period={period}")
    click.echo("")

    total_persisted = 0
    for selected_tool in selected_tools:
        click.echo(f"[external-report] running {selected_tool} period={period}...")
        sys.stdout.flush()
        try:
            report = run_external_report(selected_tool, period=period, cwd=Path.cwd())
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        persisted: list[dict[str, Any]] = []
        if store is not None:
            batch = {
                "generated_at": datetime.now(UTC).isoformat(),
                "tool": selected_tool,
                "period": period,
                "reports": [report],
            }
            persisted = persist_external_reports(store, batch, source="cli")
            total_persisted += len(persisted)

        status = "ok" if report.get("ok") else "failed"
        persisted_suffix = f" persisted={len(persisted)}" if persist else ""
        click.echo(f"[external-report] done {selected_tool} status={status}{persisted_suffix}")

        click.echo(f"- {report['tool']}")
        click.echo(f"  cmd: {report.get('command_display') or '-'}")
        if report["ok"]:
            click.echo("  status: ok")
        else:
            click.echo(f"  status: failed ({report.get('error') or report.get('returncode')})")
            message = report.get("message")
            if message:
                click.echo(f"  detail: {message}")
            stderr = report.get("stderr")
            if stderr:
                click.echo(f"  stderr: {stderr[:240]}")
            parse_error = report.get("parse_error")
            if parse_error:
                click.echo(f"  parse: {parse_error}")
            continue

        body = report.get("payload")
        if isinstance(body, dict):
            if report["tool"] == "codeburn":
                overview = body.get("overview") or {}
                click.echo(
                    "  summary: "
                    f"cost={overview.get('cost', '-')} calls={overview.get('calls', '-')} sessions={overview.get('sessions', '-')}"
                )
            elif report["tool"] == "codeburn:optimize":
                overview = body.get("overview") or {}
                click.echo(
                    "  summary: "
                    f"waste={overview.get('estimated_usd_saved', '-')} grade={overview.get('health_grade', '-')} score={overview.get('health_score', '-')}"
                )
            elif report["tool"] == "tokscale":
                click.echo(f"  summary: keys={', '.join(sorted(body.keys())[:6])}")
        click.echo("")

    if persist:
        click.echo(f"persisted {total_persisted} snapshots")


@click.command("savings-detail")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", default=20, show_default=True, help="Top N operations.")
@click.pass_context
def savings_detail(ctx: click.Context, as_json: bool, limit: int) -> None:
    """Per-operation cost-delta breakdown (last_cost - new_cost, baseline %)."""
    from atelier.infra.runtime.cost_tracker import CostTracker

    tracker = CostTracker(ctx.obj["root"])
    summary = tracker.total_savings()
    rows = summary["per_operation"][:limit]
    if as_json:
        _emit(
            {
                "summary": {k: v for k, v in summary.items() if k != "per_operation"},
                "operations": rows,
            },
            as_json=True,
        )
        return
    click.echo(
        f"Tracked operations: {summary['operations_tracked']}  "
        f"calls={summary['total_calls']}  "
        f"saved=${summary['saved_usd']:.4f} ({summary['saved_pct']}%)"
    )
    click.echo("-" * 92)
    click.echo(
        f"{'op_key':18} {'calls':>5} {'baseline$':>10} "
        f"{'last$':>10} {'now$':>10} {'d_last$':>10} {'d_base$':>10} {'%down':>6}  domain"
    )
    click.echo("-" * 92)
    for r in rows:
        click.echo(
            f"{r['op_key']:18} {r['calls_count']:>5} "
            f"{r['baseline_cost_usd']:>10.4f} {r['last_cost_usd']:>10.4f} "
            f"{r['current_cost_usd']:>10.4f} {r['delta_vs_last_usd']:>10.4f} "
            f"{r['delta_vs_base_usd']:>10.4f} {r['pct_vs_base']:>6.1f}  "
            f"{r.get('domain', '-')}"
        )


@click.command("savings-reset")
@click.pass_context
def savings_reset(ctx: click.Context) -> None:
    s = _load_smart_state(ctx.obj["root"])
    s["savings"] = {"calls_avoided": 0, "tokens_saved": 0}
    _save_smart_state(ctx.obj["root"], s)
    from atelier.infra.runtime.cost_tracker import save_cost_history

    save_cost_history(ctx.obj["root"], {"operations": {}})
    click.echo("savings reset (cache + cost history)")
