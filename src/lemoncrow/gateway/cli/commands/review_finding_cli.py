"""``lc review finding`` and ``lc review report-findings``: a coding agent's own code review, answered in chat.

``finding`` is the installed client's command (:mod:`lemoncrow_client.review_findings`), run here unchanged
so both ``lc`` programs answer alike. ``report-findings`` hands a review no hook saw (Codex has no
``ReportFindings`` tool) to the worker the Claude hook uses, and prints what the chat should show.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import IO, Any

import click

_HOST = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _configured_root(ctx: click.Context) -> Path | None:
    obj = ctx.find_root().obj or {}
    return Path(obj["root"]) if isinstance(obj, dict) and obj.get("root") else None


@click.command(
    "finding",
    add_help_option=False,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def finding_cmd(ctx: click.Context, arguments: tuple[str, ...]) -> None:
    """The last code review's numbered findings, and the answer the user gave in chat."""

    from lemoncrow_client.review_findings import finding_main

    ctx.exit(finding_main(list(arguments), sink=sys.stdout, store_root=_configured_root(ctx)))


def _findings(source: IO[str]) -> list[dict[str, Any]]:
    try:
        raw = json.loads(source.read() or "null")
    except ValueError as exc:
        raise click.ClickException(f"the findings are not JSON: {exc}") from exc
    findings = raw.get("findings") if isinstance(raw, dict) else raw
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise click.ClickException(
            'give a JSON array of findings, each {"file", "line", "summary", ...} ([] when clean)'
        )
    return findings


@click.command("report-findings")
@click.argument("source", type=click.File("r"), default="-", metavar="FILE")
@click.option("--host", default="codex", show_default=True, help="The coding agent that did the review.")
@click.option(
    "--session-id",
    default="",
    metavar="ID",
    help="The agent session the review belongs to. Default: $CODEX_SESSION_ID, else a new one.",
)
@click.option(
    "--pr",
    "pr_url",
    default="",
    metavar="URL",
    help="The pull request the review is of (required unless this session just opened one): its findings are "
    "posted on it as one numbered review and saved on its Review.",
)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="The repository. Default: the current one.",
)
@click.pass_context
def report_findings_cmd(
    ctx: click.Context, source: IO[str], host: str, session_id: str, pr_url: str, repo_root: Path | None
) -> None:
    """Post a pull request's code review an agent did itself on that pull request and on lc review, numbered.

    FILE (or standard input) is a JSON array of findings, each {"file", "line", "summary", "short_summary",
    "failure_scenario", "severity"}; [] when the review found nothing. The command returns at once: the
    review is posted and saved in the background, and `lc review finding` records each fix and each skip the
    user agreed to, answering it on the pull request.
    """

    from lemoncrow.core.capabilities.session_comment_capture import reported_findings_rows
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.review import pr_trigger
    from lemoncrow.pro.capabilities.review import reported_findings as reported

    if not _HOST.fullmatch(host):
        raise click.ClickException("--host is a lowercase name such as codex")
    findings = _findings(source)
    cwd = (repo_root or Path.cwd()).expanduser().resolve()
    root = _configured_root(ctx) or default_store_root()
    sid = session_id.strip() or os.environ.get("CODEX_SESSION_ID", "").strip() or f"cli-{uuid.uuid4().hex[:16]}"
    digest = hashlib.sha256(json.dumps([sid, pr_url, findings], sort_keys=True, default=str).encode()).hexdigest()
    payload: dict[str, Any] = {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": "PostToolUse",
        "tool_name": "ReportFindings",
        # The same report sent twice is saved once.
        "tool_use_id": f"cli:{digest[:32]}",
        "tool_input": {"findings": findings},
    }
    pull = pr_trigger.claim_pull_request(root, host=host, session_id=sid, cwd=cwd)
    if pr_url and (pull is None or pull.url != pr_url.strip().rstrip("/")):
        opened = pr_trigger.opened_from_url(pr_url)
        if opened is None:
            raise click.ClickException("--pr takes the pull request's URL: https://github.com/OWNER/REPO/pull/N")
        pull = pr_trigger.pending_pull_request(opened, host=host, session_id=sid, cwd=cwd)
    if pull is None:
        raise click.ClickException(
            "lc review saves an agent's code review of a pull request only: pass --pr with its URL "
            "(https://github.com/OWNER/REPO/pull/N)"
        )
    payload["pull_request"] = pull.to_wire()
    url = reported.pull_request_review_url(payload, store_root=root)
    if url is None:
        raise click.ClickException(
            "lc review saves an agent's findings on the local Review server, from inside a Git checkout"
        )
    reported.note_report(payload, host=host, store_root=root, url=url)
    reported.spawn_reported_findings(payload, host=host, store_root=root)
    rows = reported_findings_rows(findings)
    click.echo(reported.report_message(rows, url, pr_number=pull.number, agent=reported.agent_name(host)))
    click.echo(reported.report_context(rows, url, pull_request=pull))


__all__ = ["finding_cmd", "report_findings_cmd"]
