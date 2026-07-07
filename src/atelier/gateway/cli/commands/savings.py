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
from pathlib import Path
from typing import Any

import click

from atelier.gateway.cli.commands._shared import (
    _emit,
    _ledger_dir,
    _load_smart_state,
    _load_store,
    _save_smart_state,
    require_pro,
)

logger = logging.getLogger(__name__)


@click.group("savings", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--segment",
    is_flag=True,
    hidden=True,  # internal: statusline.sh interface, env-driven (ATELIER_STATUS_SESSION_ID)
    help="Pre-formatted rotating segment for statusline.sh.",
)
@click.pass_context
def savings_cmd(ctx: click.Context, as_json: bool, segment: bool) -> None:
    """Aggregate savings: cache + reasoning-library + cost-delta vs. baseline."""
    if ctx.invoked_subcommand is not None:
        return
    if segment:
        from atelier.core.capabilities.savings_summary import savings_segment

        _session_id = os.environ.get("ATELIER_STATUS_SESSION_ID", "")
        _live_cost = float(os.environ.get("ATELIER_STATUSLINE_COST_USD") or 0)
        _live_in = int(os.environ.get("ATELIER_STATUSLINE_LIVE_IN_TOK") or 0)
        _live_cache = int(os.environ.get("ATELIER_STATUSLINE_LIVE_CACHE_TOK") or 0)
        _live_out = int(os.environ.get("ATELIER_STATUSLINE_LIVE_OUT_TOK") or 0)
        _no_color = bool(os.environ.get("ATELIER_STATUSLINE_NO_COLOR") or os.environ.get("ATELIER_NO_COLOR"))
        # Write directly — click.echo strips ANSI when stdout is not a TTY
        # (always the case when captured via $() in statusline.sh).
        import sys

        sys.stdout.write(
            savings_segment(
                _session_id,
                live_cost_usd=_live_cost,
                live_in_tok=_live_in,
                live_cache_tok=_live_cache,
                live_out_tok=_live_out,
                no_color=_no_color,
            )
        )
        sys.stdout.flush()
        return
    from atelier.core.capabilities.plugin_runtime import build_savings_report
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report

    runs = _ledger_dir(ctx.obj["root"])
    bad_plans_blocked = 0
    rescue_events = 0
    rubric_failures = 0
    if runs.is_dir():
        # Run ledgers live 5 levels deep: sessions/YYYY/MM/DD/<host>/<id>/run.json
        # (see _latest_ledger_path). A flat glob never matches anything here.
        for p in runs.glob("*/*/*/*/*/run.json"):
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
        from atelier.core.capabilities.savings_summary import render_savings_summary

        click.echo(render_savings_summary(payload))


def _legacy_optimize_report(ctx: click.Context, host: str | None, days: int, limit: int) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report

    store = _load_store(ctx.obj["root"])
    return build_trace_optimization_report(store.list_traces(limit=5000), days=days, host=host, limit=limit)


def _advisor_result(ctx: click.Context, host: str | None, days: int) -> Any:
    from atelier.core.capabilities.optimization import load_current_policy, optimize_from_traces

    store = _load_store(ctx.obj["root"])
    current_policy = load_current_policy(ctx.obj["root"])
    return optimize_from_traces(store.list_traces(limit=5000), current_policy=current_policy, days=days, host=host)


def _benchmark_evidence_from_options(
    *,
    runs_path: Path | None,
    baseline_cost_usd: float | None,
    candidate_cost_usd: float | None,
    margin: float,
    confidence: float,
) -> Any:
    from atelier.core.capabilities.optimization import BenchmarkEvidence

    provided = [runs_path is not None, baseline_cost_usd is not None, candidate_cost_usd is not None]
    if not any(provided):
        return None
    if not all(provided):
        raise click.ClickException("--runs, --baseline-cost-usd, and --candidate-cost-usd must be provided together")
    return BenchmarkEvidence(
        runs_path=str(runs_path),
        baseline_cost_usd=baseline_cost_usd,
        candidate_cost_usd=candidate_cost_usd,
        margin=margin,
        confidence=confidence,
    )


@click.group("optimize", invoke_without_command=True)
@click.option(
    # Filters by Trace.host, an open-ended field (SDK-recorded traces can carry
    # any provider/host string) -- not restricted to session-import hosts.
    "--host",
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
    if as_json:
        _emit(report, as_json=True)
        return
    _render_optimization_summary(result)
    click.echo("")
    click.echo(
        f"Legacy trace recommendations: {report['estimated_tokens_saved']} tokens, ${report['estimated_usd_saved']:.4f}"
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
@click.option("--host", default=None)
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


@optimize_group.command("run")
@click.option("--host", default=None)
@click.option("--days", default=7, show_default=True, type=int)
@click.option(
    "--runs",
    "runs_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="TerminalBench runs.jsonl file or a directory that contains it.",
)
@click.option("--baseline-cost-usd", type=float, default=None)
@click.option("--candidate-cost-usd", type=float, default=None)
@click.option("--margin", default=0.05, show_default=True, type=float)
@click.option("--confidence", default=0.95, show_default=True, type=float)
@click.option(
    "--proposal-tokens-threshold",
    type=int,
    default=None,
    help="Minimum projected token savings required before writing a proposal artifact.",
)
@click.option("--open-pr", is_flag=True, help="Open a draft PR after the proposal artifact is written.")
@click.option("--dry-run", is_flag=True, help="Preview PR preparation without git or GitHub side effects.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_run(
    ctx: click.Context,
    host: str | None,
    days: int,
    runs_path: Path | None,
    baseline_cost_usd: float | None,
    candidate_cost_usd: float | None,
    margin: float,
    confidence: float,
    proposal_tokens_threshold: int | None,
    open_pr: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Run the optimization advisor intentionally and evaluate proposal readiness."""
    from atelier.core.capabilities.optimization import run_optimization_cycle

    evidence = _benchmark_evidence_from_options(
        runs_path=runs_path,
        baseline_cost_usd=baseline_cost_usd,
        candidate_cost_usd=candidate_cost_usd,
        margin=margin,
        confidence=confidence,
    )
    try:
        payload = run_optimization_cycle(
            store_root=ctx.obj["root"],
            host=host,
            days=max(1, days),
            source="cli",
            open_pr=open_pr,
            dry_run=dry_run,
            proposal_tokens_threshold=proposal_tokens_threshold,
            benchmark_evidence=evidence,
            store=_load_store(ctx.obj["root"]),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    action = str(payload.get("proposal", {}).get("action", ""))
    if open_pr and action not in {"pr_opened", "pr_dry_run"}:
        raise click.ClickException(f"open-pr blocked: {action}")

    if as_json:
        _emit(payload, as_json=True)
        return

    advisor = payload["advisor"]
    current_policy = advisor.get("current_policy") or {}
    click.echo(f"Repo root: {payload['repo_root']}")
    click.echo(f"Current preset: {current_policy.get('preset', '-')}")
    click.echo(
        f"Estimated weekly savings: ${float(advisor.get('weekly_savings_usd', 0.0) or 0.0):.2f}  "
        f"confidence={advisor.get('confidence', '-')}"
    )
    click.echo(f"Proposal action: {action}")
    artifact_path = payload.get("proposal", {}).get("artifact_path")
    if artifact_path:
        click.echo(f"Proposal artifact: {artifact_path}")
    pr_info = payload.get("proposal", {}).get("open_pr")
    if isinstance(pr_info, dict) and pr_info:
        click.echo(f"PR branch: {pr_info.get('branch', '-')}")
        if pr_info.get("url"):
            click.echo(f"PR URL: {pr_info['url']}")


@optimize_group.group("auto")
def optimize_auto() -> None:
    """Inspect or persist autonomous optimization automation settings."""


@optimize_auto.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_auto_status(ctx: click.Context, as_json: bool) -> None:
    """Show the persisted optimize automation configuration."""
    from atelier.core.capabilities.optimization import load_automation_config
    from atelier.core.capabilities.optimization.policy import optimization_config_path

    automation = load_automation_config(ctx.obj["root"]).to_dict()
    payload = {
        "automation": automation,
        "path": str(optimization_config_path(ctx.obj["root"])),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Auto optimize: {'enabled' if automation['enabled'] else 'disabled'}")
    click.echo(f"Config: {payload['path']}")


@optimize_auto.command("enable")
@click.option(
    "--runs",
    "runs_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Optional TerminalBench runs.jsonl file or directory for NI gating.",
)
@click.option("--baseline-cost-usd", type=float, default=None)
@click.option("--candidate-cost-usd", type=float, default=None)
@click.option("--margin", default=0.05, show_default=True, type=float)
@click.option("--confidence", default=0.95, show_default=True, type=float)
@click.option("--proposal-tokens-threshold", type=int, default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_auto_enable(
    ctx: click.Context,
    runs_path: Path | None,
    baseline_cost_usd: float | None,
    candidate_cost_usd: float | None,
    margin: float,
    confidence: float,
    proposal_tokens_threshold: int | None,
    as_json: bool,
) -> None:
    """Enable periodic optimize jobs using the shared persisted config."""
    from atelier.core.capabilities.optimization import (
        AutomationConfig,
        load_automation_config,
        save_automation_config,
    )
    from atelier.core.capabilities.optimization.policy import optimization_config_path

    evidence = _benchmark_evidence_from_options(
        runs_path=runs_path,
        baseline_cost_usd=baseline_cost_usd,
        candidate_cost_usd=candidate_cost_usd,
        margin=margin,
        confidence=confidence,
    )
    current = load_automation_config(ctx.obj["root"])
    updated = AutomationConfig(
        enabled=True,
        minimum_projected_tokens_saved=(
            current.minimum_projected_tokens_saved
            if proposal_tokens_threshold is None
            else max(0, proposal_tokens_threshold)
        ),
        benchmark_evidence=evidence or current.benchmark_evidence,
        last_proposal_fingerprint=current.last_proposal_fingerprint,
        last_proposal_at=current.last_proposal_at,
    )
    path = save_automation_config(ctx.obj["root"], updated)
    payload = {"automation": updated.to_dict(), "path": str(path or optimization_config_path(ctx.obj["root"]))}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("Auto optimize enabled.")
    click.echo(f"Saved: {payload['path']}")


@optimize_auto.command("disable")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_auto_disable(ctx: click.Context, as_json: bool) -> None:
    """Disable periodic optimize jobs without discarding saved evidence."""
    from atelier.core.capabilities.optimization import (
        AutomationConfig,
        load_automation_config,
        save_automation_config,
    )

    current = load_automation_config(ctx.obj["root"])
    updated = AutomationConfig(
        enabled=False,
        minimum_projected_tokens_saved=current.minimum_projected_tokens_saved,
        benchmark_evidence=current.benchmark_evidence,
        last_proposal_fingerprint=current.last_proposal_fingerprint,
        last_proposal_at=current.last_proposal_at,
    )
    path = save_automation_config(ctx.obj["root"], updated)
    payload = {"automation": updated.to_dict(), "path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("Auto optimize disabled.")
    click.echo(f"Saved: {payload['path']}")


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


@optimize_group.command("gate")
@click.option(
    "--runs",
    "runs_path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="TerminalBench runs.jsonl file or a directory that contains it.",
)
@click.option("--baseline-cost-usd", required=True, type=float)
@click.option("--candidate-cost-usd", required=True, type=float)
@click.option("--margin", default=0.05, show_default=True, type=float)
@click.option("--confidence", default=0.95, show_default=True, type=float)
@click.option("--json", "as_json", is_flag=True)
def optimize_gate(
    runs_path: Path,
    baseline_cost_usd: float,
    candidate_cost_usd: float,
    margin: float,
    confidence: float,
    as_json: bool,
) -> None:
    """Evaluate the TerminalBench + cost non-inferiority gate."""
    from atelier.core.capabilities.optimization import evaluate_non_inferiority_from_runs

    try:
        verdict = evaluate_non_inferiority_from_runs(
            runs_path,
            baseline_cost_usd=baseline_cost_usd,
            candidate_cost_usd=candidate_cost_usd,
            margin=margin,
            confidence=confidence,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    payload = verdict.to_dict()
    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(f"Non-inferiority gate: {'PASS' if verdict.passed else 'FAIL'}")
    click.echo(
        f"Pass-rate delta (on-off): {verdict.pass_rate_delta:+.4f}  "
        f"CI lower bound: {verdict.delta_lower_bound:+.4f}  "
        f"margin: -{verdict.margin:.4f}"
    )
    click.echo(
        f"Estimated cost delta (candidate-baseline): ${verdict.estimated_cost_delta_usd:+.4f}  "
        f"savings: ${verdict.estimated_cost_savings_usd:.4f}"
    )
    if verdict.reasons:
        click.echo("Reasons:")
        for reason in verdict.reasons:
            click.echo(f"- {reason}")


_COMPRESS_CONTEXT_PROMPT = (
    "You are compacting an agent-instruction file (e.g. CLAUDE.md / AGENTS.md / .cursorrules). "
    "Rewrite it to be maximally token-dense while preserving EVERY directive, constraint, "
    "carve-out, and warning — dropping a rule is a failure. Keep all code blocks, commands, "
    "file paths, identifiers, environment variables, and URLs byte-exact. Compress only prose: "
    "drop filler, merge redundant sentences, convert prose lists to compact bullets. Keep the "
    "markdown heading structure. Return ONLY the rewritten file content, with no commentary "
    "and no surrounding code fence.\n\nFile content:\n"
)


@optimize_group.command("compress-context")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--write",
    "do_write",
    is_flag=True,
    help="Apply the rewrite; the original is saved to <file>.bak first. Default: dry-run.",
)
def optimize_compress_context(file: Path, do_write: bool) -> None:
    """Compress an authored persistent-context file (CLAUDE.md, AGENTS.md, ...).

    Produces a denser rewrite via the internal LLM that preserves every
    directive and keeps code blocks, commands, and paths byte-exact — only
    prose is compressed. Dry-run by default; --write applies it after saving
    the original to <file>.bak.
    """
    from atelier.core.capabilities.tool_supervision.compact_output import _count_tokens, gate_compact
    from atelier.infra.internal_llm import InternalLLMError, summarize

    original = file.read_text(encoding="utf-8")
    if not original.strip():
        raise click.ClickException(f"{file} is empty — nothing to compress")
    try:
        compressed = str(summarize(_COMPRESS_CONTEXT_PROMPT + original)).strip() + "\n"
    except InternalLLMError as exc:
        raise click.ClickException(
            f"internal LLM unavailable ({exc}). Refusing to fall back to lossy head/tail "
            "truncation for an authored instruction file — set ATELIER_LLM_BACKEND "
            "(ollama/openai) and retry."
        ) from exc

    gate = gate_compact(original, compressed)
    if not gate.used_compact:
        raise click.ClickException(
            f"compressed result is not smaller ({gate.compact_chars} vs {gate.original_chars} chars; "
            f"savings {gate.savings_ratio:.0%} < required {gate.threshold:.0%}) — leaving {file} unchanged"
        )

    before_tokens = _count_tokens(original)
    after_tokens = _count_tokens(compressed)
    click.echo(compressed)
    click.echo(
        f"tokens: {before_tokens} -> {after_tokens} "
        f"(saves {before_tokens - after_tokens} tokens per future read; {gate.savings_ratio:.0%} fewer chars)"
    )
    if not do_write:
        click.echo(f"dry-run: {file} unchanged. Pass --write to apply (original saved to {file.name}.bak).")
        return

    backup = file.with_name(file.name + ".bak")
    backup.write_text(original, encoding="utf-8")
    file.write_text(compressed, encoding="utf-8")
    click.echo(f"wrote {file} (original saved to {backup})")
    # One-off CLI compressions are not recorded in the savings ledger: the
    # per-session ledger (sessions/<id>/savings.jsonl) is keyed by a host
    # session id, which a standalone CLI run doesn't have. The token delta
    # printed above is the authoritative report until a dedicated CLI savings
    # event type exists.


@click.command("savings-detail")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", default=20, show_default=True, help="Top N operations.")
@click.pass_context
def savings_detail(ctx: click.Context, as_json: bool, limit: int) -> None:
    """Per-operation cost-delta breakdown (last_cost - new_cost, baseline %)."""
    require_pro("savings_dashboard", "Full savings breakdown")
    from atelier.core.capabilities.savings_summary import _fmt_pct, _fmt_usd
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
        f"saved={_fmt_usd(summary['saved_usd'])} ({_fmt_pct(summary['saved_pct'])})"
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
            f"{_fmt_usd(r['baseline_cost_usd']):>10} {_fmt_usd(r['last_cost_usd']):>10} "
            f"{_fmt_usd(r['current_cost_usd']):>10} {_fmt_usd(r['delta_vs_last_usd']):>10} "
            f"{_fmt_usd(r['delta_vs_base_usd']):>10} {_fmt_pct(r['pct_vs_base']):>6}  "
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


savings_cmd.add_command(savings_detail, name="detail")
savings_cmd.add_command(savings_reset, name="reset")
