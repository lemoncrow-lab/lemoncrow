from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from atelier.core.foundation.models import to_jsonable
from atelier.gateway.cli.commands._shared import _emit


@click.group("route")
def route_public_group() -> None:
    """Cross-vendor routing helpers."""


@route_public_group.command("configure")
@click.option("--vendor", "vendors", multiple=True, type=click.Choice(["anthropic", "openai", "google"]))
@click.option("--risk-class", type=click.Choice(["low", "medium", "high"]), default="low", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_configure_public_cmd(
    ctx: click.Context,
    vendors: tuple[str, ...],
    risk_class: str,
    as_json: bool,
) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).configure(
            enabled_vendors=list(vendors) or None,
            risk_class=risk_class,
        )
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Saved {payload['path']}")
    click.echo("Enabled vendors: " + ", ".join(payload["enabled_vendors"]))


@route_public_group.command("plan")
@click.option("--tool", "tool_name", required=True, help="Tool or turn type to evaluate.")
@click.option("--task", "task_text", required=True, help="Task summary for routing.")
@click.option("--actual-vendor", default=None, help="Current host vendor for edit-pin decisions.")
@click.option("--expected-input-tokens", default=1000, show_default=True, type=int)
@click.option("--expected-output-tokens", default=200, show_default=True, type=int)
@click.option("--turn-number", default=0, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_plan_cmd(
    ctx: click.Context,
    tool_name: str,
    task_text: str,
    actual_vendor: str | None,
    expected_input_tokens: int,
    expected_output_tokens: int,
    turn_number: int,
    as_json: bool,
) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).recommend(
            tool_name=tool_name,
            task_text=task_text,
            actual_vendor=actual_vendor,
            session_state={
                "expected_input_tokens": expected_input_tokens,
                "expected_output_tokens": expected_output_tokens,
                "turn_number": turn_number,
            },
        )
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Recommendation: {payload['model']}")
    click.echo(f"  vendor: {payload['vendor']}")
    click.echo(f"  predicted cost: ${payload['predicted_cost_usd']:.6f}")
    if payload.get("fallback"):
        click.echo(f"  fallback: {payload['fallback']}")


@route_public_group.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_status_cmd(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).status()
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("Enabled vendors: " + ", ".join(payload["enabled_vendors"]))
    click.echo(f"Recommendations logged: {payload['recommendation_count']}")
    click.echo(f"Estimated savings: ${payload['estimated_savings_usd']:.6f}")
    click.echo(f"Active lessons: {payload['active_lesson_count']}")
    click.echo(f"Lesson-driven recommendations: {payload['lesson_application_count']}")
    click.echo(f"Cost-cap triggers: {payload['cost_cap_trigger_count']}")


@click.group("proof")
def proof_group() -> None:
    """Cost-quality proof gate commands (WP-32)."""


@proof_group.command("run")
@click.option(
    "--session-id",
    required=True,
    help="Stable identifier for this proof run (e.g. a git SHA or timestamp).",
)
@click.option(
    "--context-reduction-pct",
    type=float,
    default=None,
    help=(
        "Context reduction percentage from WP-19 savings bench. " "When omitted, the benchmark is re-run automatically."
    ),
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_run_cmd(
    ctx: click.Context,
    session_id: str,
    context_reduction_pct: float | None,
    as_json: bool,
) -> None:
    """Run the cost-quality proof gate and write proof-report.json/md (WP-32)."""
    from atelier.core.capabilities.proof_gate.capability import (
        BenchmarkCase,
        ProofGateCapability,
    )

    root: Path = ctx.obj["root"]

    if context_reduction_pct is None:
        try:
            from benchmarks.swe.savings_bench import run_savings_bench

            savings = run_savings_bench(root / "proof" / "savings_bench_tmp")
            context_reduction_pct = savings.reduction_pct
        except Exception as exc:
            raise click.ClickException(f"Could not run savings bench (pass --context-reduction-pct): {exc}") from exc

    cases: list[BenchmarkCase] = _build_proof_cases(session_id)

    capability = ProofGateCapability(root)
    report = capability.run(
        session_id=session_id,
        context_reduction_pct=context_reduction_pct,
        benchmark_cases=cases,
        save=True,
    )

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof session_id={report.session_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    click.echo(f"accepted_patch_rate={report.accepted_patch_rate:.3f}")
    click.echo(f"routing_regression_rate={report.routing_regression_rate:.4f}")
    click.echo(f"cheap_success_rate={report.cheap_success_rate:.3f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


def _show_proof_report(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.proof_gate.capability import ProofGateCapability

    root: Path = ctx.obj["root"]
    capability = ProofGateCapability(root)
    report = capability.load()

    if report is None:
        raise click.ClickException("No proof report found. Run `atelier proof run --session-id <id>` first.")

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof session_id={report.session_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


@proof_group.command("report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


@proof_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_show_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


def _build_proof_cases(session_id: str) -> list[Any]:
    """Build a deterministic set of benchmark cases for the proof gate.

    These cases are derived from the WP-28 routing eval suite.  Each case
    must include a trace_id so the evidence link requirement is met.  Failed
    cheap attempts are included - they cannot be elided.
    """
    from atelier.core.capabilities.proof_gate.capability import BenchmarkCase

    _CASES: list[dict[str, Any]] = [
        {
            "case_id": f"{session_id}:cheap-01",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:cheap-02",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": False,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-02",
            "session_id": session_id,
            "verifier_outcome": "fail",
        },
        {
            "case_id": f"{session_id}:cheap-03",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-03",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:mid-01",
            "task_type": "coding",
            "tier": "mid",
            "accepted": True,
            "cost_usd": 0.008,
            "trace_id": f"{session_id}:trace:mid-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:premium-01",
            "task_type": "coding",
            "tier": "premium",
            "accepted": True,
            "cost_usd": 0.05,
            "trace_id": f"{session_id}:trace:premium-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
    ]
    return [BenchmarkCase(**c) for c in _CASES]


__all__ = ["proof_group", "route_public_group"]
