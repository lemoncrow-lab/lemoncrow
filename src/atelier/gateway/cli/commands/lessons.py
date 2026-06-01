from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from atelier.core.foundation.models import to_jsonable
from atelier.gateway.cli.commands._shared import _emit, _ledger_dir, _ledger_path, _load_store


def _failure_state_path(root: Path) -> Path:
    return Path(root) / "failure_clusters.json"


def _load_failure_state(root: Path) -> dict[str, dict[str, Any]]:
    path = _failure_state_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_failure_state(root: Path, state: dict[str, dict[str, Any]]) -> None:
    path = _failure_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _lesson_promoter(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability

    store = _load_store(root)
    return LessonPromoterCapability(store)


def _lesson_pr_bot(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPrBot

    store = _load_store(root)
    return LessonPrBot(store=store, root=root)


def _emit_lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    lessons = _lesson_promoter(ctx.obj["root"]).inbox(domain=domain, limit=limit)
    if as_json:
        _emit([item.model_dump(mode="json") for item in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no inbox lessons)")
        return
    for item in lessons:
        click.echo(f"{item.id}\t{item.domain}\t{item.kind}\t{item.confidence:.2f}\t{item.cluster_fingerprint[:48]}")


def _eval_dir(root: Path) -> Path:
    return Path(root) / "evals"


def _load_eval(root: Path, case_id: str) -> dict[str, Any] | None:
    p = _eval_dir(root) / f"{case_id}.json"
    if not p.exists():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_eval(root: Path, case: dict[str, Any]) -> Path:
    d = _eval_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case['id']}.json"
    p.write_text(json.dumps(case, indent=2), encoding="utf-8")
    return p


def _evaluate_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    expected_status = str(case.get("expected_status", "pass"))
    actual_status = str(case.get("actual_status", expected_status))
    return {
        "case_id": str(case.get("id", "unknown")),
        "domain": str(case.get("domain", "unknown")),
        "description": str(case.get("description", "")),
        "expected_status": expected_status,
        "actual_status": actual_status,
        "passed": actual_status == expected_status,
    }


@click.group()
def ledger() -> None:
    """Manage run ledgers."""


@ledger.command("show")
@click.option("--session-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ledger_show(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    if as_json:
        _emit(snap, as_json=True)
        return
    click.echo(f"session_id: {snap.get('session_id')}")
    click.echo(f"status: {snap.get('status')}")
    click.echo(f"task: {snap.get('task', '')}")
    click.echo(f"domain: {snap.get('domain', '')}")
    click.echo(f"events: {len(snap.get('events', []))}")
    click.echo(f"errors_seen: {len(snap.get('errors_seen', []))}")
    click.echo(f"current_blockers: {snap.get('current_blockers', [])}")


@ledger.command("reset")
@click.option("--session-id", default=None)
@click.confirmation_option(prompt="Delete this ledger snapshot?")
@click.pass_context
def ledger_reset(ctx: click.Context, session_id: str | None) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    path.unlink(missing_ok=True)
    click.echo(f"removed {path}")


@ledger.command("update")
@click.option("--session-id", default=None)
@click.option("--field", "field_name", required=True)
@click.option("--value", required=True, help="Value (use JSON literal for lists/dicts).")
@click.pass_context
def ledger_update(ctx: click.Context, session_id: str | None, field_name: str, value: str) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    snap[field_name] = parsed
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    click.echo(f"updated {field_name}")


@ledger.command("summarize")
@click.option("--session-id", default=None)
@click.pass_context
def ledger_summarize(ctx: click.Context, session_id: str | None) -> None:
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], session_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    click.echo(state.to_prompt_block())


@click.group()
def checkpoint() -> None:
    """Manage idempotent agent checkpoints for resumable execution."""


@checkpoint.command("create")
@click.option("--session-id", default=None, help="Session ID (defaults to latest ledger).")
@click.option("--tool", "tool_name", default="manual", show_default=True)
@click.option("--model-route", default="cheap_llm", show_default=True)
@click.option("--note", default="", help="Optional note stored as compact_state.")
@click.pass_context
def checkpoint_create(
    ctx: click.Context,
    session_id: str | None,
    tool_name: str,
    model_route: str,
    note: str,
) -> None:
    """Create a checkpoint at the current ledger step."""
    from atelier.infra.runtime.checkpoint import Checkpoint, CheckpointStore
    from atelier.infra.runtime.run_ledger import RunLedger

    root = ctx.obj["root"]
    path = _ledger_path(root, session_id)
    led = RunLedger.load(path)
    store = CheckpointStore(root)
    step_id = len(store.list_checkpoints(led.session_id))
    ckpt = Checkpoint.create(
        session_id=led.session_id,
        step_id=step_id,
        tool_name=tool_name,
        model_route=model_route,
        input_data=note,
        output_data=led.status,
        compact_state=note,
        cost_so_far_usd=led.cost_tracker.snapshot().get("total_cost_usd", 0.0) if led.cost_tracker else 0.0,
    )
    saved_path = store.save(ckpt)
    click.echo(f"checkpoint created: session={ckpt.session_id} step={ckpt.step_id} txn={ckpt.transaction_id}")
    click.echo(f"  saved to: {saved_path}")


@checkpoint.command("list")
@click.option("--session-id", default=None, help="Filter to a specific session.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_list(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """List available checkpoints."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    sessions = [session_id] if session_id else store.list_sessions()
    if not sessions:
        click.echo("no checkpoints found.")
        return
    rows = []
    for sid in sessions:
        for ckpt in store.list_checkpoints(sid):
            rows.append(ckpt.to_dict())
    if as_json:
        _emit(rows, as_json=True)
        return
    for row in rows:
        click.echo(
            f"  {row['session_id'][:12]}  step={row['step_id']:3d}"
            f"  tool={row['tool_name']:<18s}  route={row['model_route']:<14s}"
            f"  cost=${row['cost_so_far_usd']:.4f}  txn={row['transaction_id']}"
        )


@checkpoint.command("resume")
@click.argument("session_id")
@click.option(
    "--from-step",
    "from_step",
    type=int,
    default=None,
    help="Resume from this step (default: last).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_resume(
    ctx: click.Context,
    session_id: str,
    from_step: int | None,
    as_json: bool,
) -> None:
    """Resume execution context from a saved checkpoint.

    Prints the compact_state from the checkpoint so the agent can restore
    context and continue from step N instead of restarting the full loop.
    """
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)

    if from_step is not None:
        ckpt = store.load(session_id, from_step)
        if ckpt is None:
            raise click.ClickException(f"no checkpoint found for session={session_id} step={from_step}")
    else:
        ckpt = store.latest_checkpoint(session_id)
        if ckpt is None:
            raise click.ClickException(f"no checkpoints found for session={session_id}")

    if as_json:
        _emit(ckpt.to_dict(), as_json=True)
        return

    click.echo(f"resuming from: session={ckpt.session_id}  step={ckpt.step_id}  txn={ckpt.transaction_id}")
    click.echo(f"  tool_name:    {ckpt.tool_name}")
    click.echo(f"  model_route:  {ckpt.model_route}")
    click.echo(f"  cost_so_far:  ${ckpt.cost_so_far_usd:.4f}")
    click.echo(f"  input_hash:   {ckpt.input_hash}")
    click.echo(f"  output_hash:  {ckpt.output_hash}")
    if ckpt.compact_state:
        click.echo("\ncompact_state:")
        click.echo(ckpt.compact_state)


@checkpoint.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Delete all checkpoints for this session?")
@click.pass_context
def checkpoint_delete(ctx: click.Context, session_id: str) -> None:
    """Delete all checkpoints for a session."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    count = store.delete_session(session_id)
    click.echo(f"deleted {count} checkpoint(s) for session={session_id}")


@click.group()
def failure() -> None:
    """Failure cluster management."""


@failure.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def failure_list(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    runs = _ledger_dir(ctx.obj["root"])
    clusters = FailureAnalyzer(runs).analyze()
    state = _load_failure_state(ctx.obj["root"])
    if as_json:
        _emit(
            [{**to_jsonable(c), "status": state.get(c.id, {}).get("status", "open")} for c in clusters],
            as_json=True,
        )
        return
    if not clusters:
        click.echo("(no failure clusters)")
        return
    for c in clusters:
        st = state.get(c.id, {}).get("status", "open")
        click.echo(f"{c.id}\t{st}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


@failure.command("show")
@click.argument("cluster_id")
@click.pass_context
def failure_show(ctx: click.Context, cluster_id: str) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    state = _load_failure_state(ctx.obj["root"])
    payload = to_jsonable(clusters[cluster_id])
    payload["status"] = state.get(cluster_id, {}).get("status", "open")
    _emit(payload, as_json=True)


@failure.command("accept")
@click.argument("cluster_id")
@click.pass_context
def failure_accept(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "accepted"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"accepted {cluster_id}")


@failure.command("reject")
@click.argument("cluster_id")
@click.pass_context
def failure_reject(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "rejected"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"rejected {cluster_id}")


@click.group()
def lesson() -> None:
    """Lesson candidate review workflow."""


@lesson.command("list")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_list(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("inbox")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    """List lesson candidates currently waiting in the inbox."""
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("approve")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_approve(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="approve",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"approved {lesson_id}")


@lesson.command("reject")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_reject(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="reject",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"rejected {lesson_id}")


@lesson.command("decide")
@click.argument("lesson_id")
@click.argument("decision", type=click.Choice(["approve", "reject"]))
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_decide(
    ctx: click.Context,
    lesson_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    """Approve or reject a lesson candidate."""
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision=decision,
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    verb = "approved" if decision == "approve" else "rejected"
    click.echo(f"{verb} {lesson_id}")


@lesson.group("active")
def lesson_active_group() -> None:
    """Inspect and manage active typed lessons."""


@lesson_active_group.command("list")
@click.option("--include-inactive", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_list(ctx: click.Context, include_inactive: bool, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lessons = TypedLessonStore(ctx.obj["root"], create=False).list_lessons()
    if not include_inactive:
        lessons = [lesson for lesson in lessons if lesson.enabled]
    if as_json:
        _emit([lesson.model_dump(mode="json") for lesson in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no active lessons)")
        return
    for lesson in lessons:
        last_applied = lesson.last_applied_at.isoformat() if lesson.last_applied_at else "-"
        click.echo(
            f"{lesson.id}\t{lesson.kind}\t{lesson.scope}\t{lesson.effective_confidence_at():.2f}\t"
            f"{'enabled' if lesson.enabled else 'disabled'}\t{last_applied}"
        )


@lesson_active_group.command("show")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_show(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"], create=False).get_lesson(lesson_id)
    if lesson is None:
        raise click.ClickException(f"typed lesson not found: {lesson_id}")
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(json.dumps(lesson.model_dump(mode="json"), indent=2, ensure_ascii=False, default=str))


@lesson_active_group.command("disable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_disable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, False)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"disabled {lesson_id}")


@lesson_active_group.command("enable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_enable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, True)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"enabled {lesson_id}")


@lesson.command("sync-pr")
@click.argument("lesson_id")
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_sync_pr(ctx: click.Context, lesson_id: str, dry_run: bool, as_json: bool) -> None:
    payload = _lesson_pr_bot(ctx.obj["root"]).sync_pr(lesson_id=lesson_id, dry_run=dry_run)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload.get("skipped"):
        click.echo(f"skipped: {payload.get('reason', 'unknown')}")
        return
    if dry_run:
        click.echo(payload.get("diff", ""))
        return
    click.echo(f"created {payload.get('pr_url', '').strip()}")


@click.command("analyze-failures")
@click.option("--since", default=None, help="ISO timestamp or shorthand like '7d' (filter by mtime).")
@click.option("--trace", "trace_id", default=None, help="Single ledger run id to analyze.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def analyze_failures_cmd(ctx: click.Context, since: str | None, trace_id: str | None, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer, analyze_failures

    runs = _ledger_dir(ctx.obj["root"])
    fa = FailureAnalyzer(runs)
    snaps = fa.load_snapshots()

    if trace_id:
        snaps = [s for s in snaps if s.get("session_id") == trace_id]

    if since:
        from datetime import UTC, datetime, timedelta

        cutoff: datetime | None = None
        if since.endswith("d") and since[:-1].isdigit():
            cutoff = datetime.now(UTC) - timedelta(days=int(since[:-1]))
        else:
            try:
                cutoff = datetime.fromisoformat(since)
            except ValueError:
                cutoff = None
        if cutoff is not None:
            kept = []
            for s in snaps:
                ts = s.get("updated_at") or s.get("created_at")
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) >= cutoff:
                        kept.append(s)
                except ValueError:
                    continue
            snaps = kept

    clusters = analyze_failures(snaps)
    session_id = ctx.obj.get("_telemetry_session_id") if isinstance(ctx.obj, dict) else None
    if isinstance(session_id, str):
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        for cluster in clusters:
            emit_product(
                "failure_cluster_matched",
                cluster_id_hash=hash_identifier(cluster.id),
                domain=cluster.domain,
                session_id=session_id,
            )
    if as_json:
        _emit([to_jsonable(c) for c in clusters], as_json=True)
        return
    for c in clusters:
        click.echo(f"{c.id}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


@click.group(name="eval")
def eval_() -> None:
    """Evaluation case management."""


eval_.name = "eval"


@eval_.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_list(ctx: click.Context, as_json: bool) -> None:
    d = _eval_dir(ctx.obj["root"])
    cases = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if as_json:
        _emit(cases, as_json=True)
        return
    for c in cases:
        click.echo(f"{c.get('id')}\t{c.get('status', 'draft')}\t{c.get('domain', '')}\t{c.get('description', '')[:60]}")


@eval_.command("show")
@click.argument("case_id")
@click.pass_context
def eval_show(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    _emit(case, as_json=True)


@eval_.command("promote")
@click.argument("case_id")
@click.pass_context
def eval_promote(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "active"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"promoted {case_id}")


@eval_.command("deprecate")
@click.argument("case_id")
@click.pass_context
def eval_deprecate(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "deprecated"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"deprecated {case_id}")


@eval_.command("run")
@click.option("--domain", default=None)
@click.option("--case", "case_id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_run(ctx: click.Context, domain: str | None, case_id: str | None, as_json: bool) -> None:
    """Run deterministic eval cases."""
    d = _eval_dir(ctx.obj["root"])
    cases: list[dict[str, Any]] = []
    if case_id:
        c = _load_eval(ctx.obj["root"], case_id)
        if c is None:
            raise click.ClickException(f"eval case not found: {case_id}")
        cases = [c]
    elif d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if domain:
        cases = [c for c in cases if c.get("domain") == domain]
    results = [_evaluate_eval_case(case) for case in cases]

    if as_json:
        _emit(results, as_json=True)
    else:
        for result in results:
            click.echo(
                f"{result['case_id']}\t{result['domain']}\t{result['expected_status']}"
                f"\t{result['actual_status']}\t{'pass' if result['passed'] else 'fail'}"
            )


@click.command("eval-from-cluster")
@click.argument("cluster_id")
@click.pass_context
def eval_from_cluster(ctx: click.Context, cluster_id: str) -> None:
    """Generate a draft eval from an accepted FailureCluster."""
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    state = _load_failure_state(ctx.obj["root"])
    if state.get(cluster_id, {}).get("status") != "accepted":
        raise click.ClickException(f"cluster not accepted: {cluster_id}")
    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    c = clusters[cluster_id]
    case = {
        "id": f"eval_from_{cluster_id}",
        "domain": c.domain,
        "description": f"Replay of {c.fingerprint[:60]}",
        "task": f"Replay failure cluster {cluster_id}",
        "plan": [c.suggested_rubric_check or "no-op"],
        "expected_status": "blocked",
        "expected_warnings_contain": [],
        "expected_dead_ends": [],
        "status": "draft",
        "source_trace_ids": list(c.trace_ids),
    }
    p = _save_eval(ctx.obj["root"], case)
    click.echo(f"saved draft eval at {p}")


__all__ = [
    "_emit_lesson_inbox",
    "_eval_dir",
    "_evaluate_eval_case",
    "_ledger_dir",
    "_ledger_path",
    "_load_eval",
    "_load_failure_state",
    "_save_eval",
    "_save_failure_state",
    "analyze_failures_cmd",
    "checkpoint",
    "eval_",
    "eval_from_cluster",
    "failure",
    "ledger",
    "lesson",
]
