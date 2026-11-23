"""``lc rt telemetry push``: this machine's coding-agent run counters, sent to the hosted organization.

Only counters and identifiers leave the machine, never a prompt, path, repository name or tool argument. The push is
explicit, and safe to repeat: the server keys each run by its id and takes only newer sequences.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import click
from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.credentials import RefreshingHttpTransport, managed_credentials_path
from lemoncrow_client.errors import ClientError
from lemoncrow_client.transport import HttpTransport

from lemoncrow.infra.runtime.session_report import SessionReport, build_report, list_run_snapshots

ENDPOINT: Final[str] = "/v1/telemetry/agent-runs"
#: The server's own batch limit.
BATCH_RUNS: Final[int] = 200
_MAX_COUNTER: Final[int] = 10**12
_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LABEL: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+/-]{0,95}$")
_STATUS: Final[dict[str, str]] = {
    "running": "running",
    "complete": "success",
    "completed": "success",
    "success": "success",
    "failed": "failed",
    "error": "failed",
}


def _instant(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _counter(value: float) -> int:
    return min(max(int(value or 0), 0), _MAX_COUNTER)


def _micros(usd: float) -> int:
    return _counter(round(usd * 1_000_000))


def _label(value: object) -> str:
    text = str(value or "")
    return text if _LABEL.fullmatch(text) else ""


def run_payload(report: SessionReport, status: str) -> dict[str, Any]:
    """One run in the server's allowlist. Unknown cost and savings stay null: they are not zero."""

    session = report.session_id
    run_id = session if _RUN_ID.fullmatch(session) else "run_" + hashlib.sha256(session.encode()).hexdigest()[:40]
    latest = report.ended_at or report.started_at + timedelta(seconds=max(report.duration_seconds, 0.0))
    latest = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
    payload: dict[str, Any] = {
        "run_id": run_id,
        # Seconds of the run's last activity: a later push of the same run replaces an earlier one, never the reverse.
        "sequence": _counter(latest.timestamp()),
        "vendor": _label(report.vendor) or "unknown",
        "status": _STATUS.get(status, "partial"),
        "started_at": _instant(report.started_at),
        "duration_seconds": _counter(report.duration_seconds),
        "active_duration_seconds": _counter(report.active_duration_seconds),
        "turns": _counter(report.total_turns),
        "input_tokens": _counter(report.input_tokens),
        "output_tokens": _counter(report.output_tokens),
        "cached_input_tokens": _counter(report.cache_read_tokens),
        "cache_write_tokens": _counter(report.cache_write_tokens),
        "tool_calls": _counter(report.tool_call_count),
        "cost_micros": None if report.actual_cost_source == "unavailable" else _micros(report.total_cost_usd),
        "savings_micros": (
            _micros(report.total_lemoncrow_savings_usd) if report.total_lemoncrow_savings_usd > 0 else None
        ),
    }
    model = _label(report.started_model)
    if model:
        payload["model"] = model
    if report.ended_at is not None:
        payload["ended_at"] = _instant(report.ended_at)
    return payload


def collect_runs(root: Path, *, days: int) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    runs: list[dict[str, Any]] = []
    for _path, snapshot in list_run_snapshots(root, since=since):
        try:
            report = build_report(snapshot, root)
        except (KeyError, TypeError, ValueError):
            continue  # A damaged local record is not worth stopping the rest for.
        runs.append(run_payload(report, str(snapshot.get("status") or "running")))
    return runs


def batches(runs: Iterable[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    ordered = list(runs)
    for start in range(0, len(ordered), BATCH_RUNS):
        yield ordered[start : start + BATCH_RUNS]


def _transport(config: ClientConfig) -> HttpTransport:
    managed = {str(managed_credentials_path(config.account_state_dir)), str(managed_credentials_path(config.state_dir))}
    if config.refresh_token and config.token_source in managed:
        return RefreshingHttpTransport(
            config.url,
            timeout_s=config.request_timeout_s,
            state_dir=config.account_state_dir,
            legacy_state_dir=config.state_dir,
            access_token=config.token,
            refresh_token=config.refresh_token,
        )
    return HttpTransport(config.url, timeout_s=config.request_timeout_s)


@click.command("push")
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 90), help="How far back to read runs.")
@click.option("--dry-run", is_flag=True, help="Print exactly what would be sent, and send nothing.")
@click.pass_context
def telemetry_push(ctx: click.Context, days: int, dry_run: bool) -> None:
    """Send coding-agent run counters to your hosted organization (no source content)."""

    runs = collect_runs(Path(ctx.obj["root"]), days=days)
    if dry_run:
        click.echo(json.dumps({"runs": runs}, indent=2, sort_keys=True))
        return
    config = load_config(cwd=None)
    if not config.hosted:
        raise click.ClickException("this installation is not connected to a hosted LemonCrow server")
    if not config.authenticated:
        raise click.ClickException("not signed in; run `lc auth login` first")
    if not runs:
        click.echo("No runs to send.")
        return
    transport = _transport(config)
    totals: Counter[str] = Counter()
    try:
        for batch in batches(runs):
            answer = transport.request(
                "POST", ENDPOINT, body={"runs": batch}, headers={"Authorization": f"Bearer {config.token}"}
            ).require()
            totals.update({key: int(value) for key, value in answer.items() if isinstance(value, int)})
    except ClientError as exc:
        sent = f" after {totals['accepted'] + totals['updated']} runs were stored" if totals else ""
        raise click.ClickException(f"{exc.server_code}: {exc.message}{sent}") from exc
    finally:
        transport.close()
    click.echo(
        f"Sent {len(runs)} runs: {totals['accepted']} new, {totals['updated']} updated, "
        f"{totals['duplicate'] + totals['stale']} unchanged."
    )
    refused = totals["refused_other_reporter"] + totals["refused_capacity"]
    if refused:
        raise click.ClickException(
            f"{totals['refused_other_reporter']} runs belong to another reporter and "
            f"{totals['refused_capacity']} exceeded your organization's limit; they were not stored"
        )
