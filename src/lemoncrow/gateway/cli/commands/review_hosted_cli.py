"""``lc review hosted ...``: explicitly attach a local Review to a hosted one (G5-SYNC).

While attached, hosted owns comments, judgments, requests, outcomes and
policy. The local Review keeps a projection and a queue of explicitly queued
actions bound to the hosted Snapshot they were made on; ``sync`` replays them
and records, per action, ``applied`` or ``conflict`` with hosted's reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click


def _local_config() -> Any:
    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError

    try:
        config = load_config(cwd=Path.cwd())
    except ClientError as exc:
        raise click.ClickException(exc.message) from exc
    if config.hosted:
        raise click.ClickException("hosted attachment links a *local* Review; LEMONCROW_URL points at hosted")
    return config


def _hosted_config(local: Any, origin: str) -> Any:
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import hosted_attachment_config

    try:
        return hosted_attachment_config(local, origin)
    except ClientError as exc:
        raise click.ClickException(exc.message) from exc


def _review_id(reference: str) -> str:
    text = reference.strip()
    return text[2:] if text.startswith("r/") else text


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    from lemoncrow_client.errors import ClientError

    try:
        return fn(*args, **kwargs)
    except ClientError as exc:
        raise click.ClickException(exc.message) from exc


def _echo_status(payload: dict[str, Any]) -> None:
    if not payload.get("attached"):
        click.echo("Not attached to a hosted Review.")
        return
    attachment = payload.get("attachment") or {}
    counts = payload.get("counts") or {}
    projection = payload.get("projection") or {}
    policy = projection.get("policy") or {}
    click.echo(
        f"Attached to hosted {payload.get('reference')} ({attachment.get('url')}) as {attachment.get('principal_id')}"
    )
    click.echo(
        f"  Hosted owns comments, judgments, requests, outcomes and policy. Last synced {attachment.get('synced_at')}."
    )
    click.echo(
        f"  Hosted policy: {'ready' if policy.get('ready') else 'not ready'}"
        + (f" ({'; '.join(policy.get('blocking') or [])})" if policy.get("blocking") else "")
    )
    click.echo(
        f"  Queue: {counts.get('pending', 0)} pending, {counts.get('conflict', 0)} conflict, "
        f"{counts.get('applied', 0)} applied, {counts.get('discarded', 0)} discarded"
    )
    for action in payload.get("actions") or ():
        if action.get("state") in {"pending", "conflict"}:
            reason = f" ({action.get('reason')})" if action.get("reason") else ""
            click.echo(f"    {action.get('id')}  {action.get('kind')}  {action.get('state')}{reason}")


@click.group("hosted")
def hosted_group() -> None:
    """Attach a local Review to a hosted Review; queue offline actions and sync them."""


@hosted_group.command("attach")
@click.argument("review")
@click.argument("hosted_review")
@click.option("--origin", default="", help="Hosted LemonCrow origin (default: the one configured hosted server).")
@click.option("--repo-id", default="", help="Hosted repository id (default: found in your hosted inbox).")
def hosted_attach(review: str, hosted_review: str, origin: str, repo_id: str) -> None:
    """Explicitly attach local REVIEW to HOSTED_REVIEW (r/<id> or its URL)."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import attach_review

    local = _local_config()
    hosted = _hosted_config(local, origin)
    payload = _call(attach_review, local, hosted, _review_id(review), hosted_review, repo_id=repo_id)
    _echo_status(payload)


@hosted_group.command("status")
@click.argument("review")
@click.option("--json", "as_json", is_flag=True)
def hosted_status(review: str, as_json: bool) -> None:
    """Show the attachment, hosted policy and the action queue."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import ReaderLocalPort

    port = ReaderLocalPort(_local_config(), _review_id(review))
    try:
        payload = dict(_call(port.status))
    finally:
        port.close()
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _echo_status(payload)


@hosted_group.command("queue")
@click.argument("review")
@click.argument("kind", type=click.Choice(["mark", "comment", "reply", "outcome", "request_complete"]))
@click.option("--unit-key", default=None, help="mark: the part's key.")
@click.option(
    "--state",
    default=None,
    help="mark: reviewed, needs_changes or unreviewed ('later' is not available on hosted reviews yet).",
)
@click.option("--path", default=None, help="comment: the file.")
@click.option("--start-line", type=int, default=None, help="comment: first line (omit for a file comment).")
@click.option("--end-line", type=int, default=None, help="comment: last line.")
@click.option("--body", default=None, help="comment/reply: the text.")
@click.option("--parent", default=None, help="reply: the hosted thread id.")
@click.option("--outcome", default=None, help="outcome: comment, lgtm or changes_requested.")
@click.option("--summary", default=None, help="outcome: summary text.")
@click.option("--request", "request_id", default=None, help="request_complete: your hosted request id.")
def hosted_queue(review: str, kind: str, **fields: Any) -> None:
    """Queue one action for hosted, bound to the hosted Snapshot you are looking at."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import ReaderLocalPort

    names = {"parent": "parent_id"}
    body: dict[str, Any] = {"kind": kind}
    for name, value in fields.items():
        if value is not None:
            body[names.get(name, name)] = value
    if kind == "comment" and "start_line" not in body:
        body["file_level"] = True
    port = ReaderLocalPort(_local_config(), _review_id(review))
    try:
        payload = dict(_call(port.queue, body))
    finally:
        port.close()
    action = payload.get("action") or {}
    click.echo(f"Queued {action.get('kind')} {action.get('id')} for hosted {payload.get('reference')}.")
    for superseded in payload.get("superseded") or ():
        click.echo(f"  Superseded {superseded}.")


@hosted_group.command("sync")
@click.argument("review")
@click.option("--origin", default="", help="Hosted LemonCrow origin (default: the one configured hosted server).")
@click.option("--json", "as_json", is_flag=True)
def hosted_sync(review: str, origin: str, as_json: bool) -> None:
    """Reconnect: replay pending actions in order; each ends applied or conflict(reason)."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import sync_review

    local = _local_config()
    hosted = _hosted_config(local, origin)
    report = _call(sync_review, local, hosted, _review_id(review))
    if as_json:
        click.echo(json.dumps(report.to_wire(), indent=2, sort_keys=True))
        return
    if report.identity != "ok":
        raise click.ClickException(f"nothing replayed: {report.identity}")
    for row in report.results:
        reason = f" ({row['reason']})" if row["reason"] else ""
        click.echo(f"{row['action_id']}  {row['kind']}  {row['state']}{reason}")
    if report.stopped:
        click.echo(f"Stopped: {report.stopped}; {report.remaining} action(s) still pending.")


@hosted_group.command("resolve")
@click.argument("review")
@click.argument("action_id")
@click.argument("op", type=click.Choice(["discard", "requeue"]))
def hosted_resolve(review: str, action_id: str, op: str) -> None:
    """Resolve one action: discard it, or requeue a conflict onto the current hosted Snapshot."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import ReaderLocalPort

    port = ReaderLocalPort(_local_config(), _review_id(review))
    try:
        payload = dict(_call(port.resolve, action_id, op))
    finally:
        port.close()
    action = payload.get("action") or {}
    click.echo(f"{action.get('id')}  {action.get('kind')}  {action.get('state')}")


@hosted_group.command("detach")
@click.argument("review")
@click.option("--discard-unresolved", is_flag=True, help="Also drop pending and conflicted actions.")
def hosted_detach(review: str, discard_unresolved: bool) -> None:
    """Explicitly detach. Keeps the local Review and its own state; drops the projection and queue."""

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import ReaderLocalPort

    port = ReaderLocalPort(_local_config(), _review_id(review))
    try:
        payload = dict(_call(port.detach, discard_unresolved=discard_unresolved))
    finally:
        port.close()
    click.echo(
        f"Detached. Discarded {payload.get('discarded_unresolved', 0)} unresolved action(s); "
        f"kept {payload.get('kept')}."
    )


__all__ = ["hosted_group"]
