"""The AI half of ``lc review``: a local change, or a pull request, reviewed on the caller's own model route.

Click-only at import time (the heavy modules load inside the functions), and free of ``lemoncrow.gateway``, so the
installed Review engine ships it: ``lc review --ai`` in an installed client runs these functions through
:mod:`lemoncrow.pro.capabilities.review.local_cli.main`, and the full CLI imports them back.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        click.echo(data)


class _LocalUsageError(click.ClickException):
    """A request a local review cannot serve: exit code 3, so a hook can tell it from a finding (1)."""

    exit_code = 3


#: ``--fail-on`` for a local review when the flag is not given (mirrors ``local.DEFAULT_FAIL_ON``, which this module
#: does not import so that ``lc --help`` pays for nothing).
DEFAULT_LOCAL_FAIL_ON = "p0"


def _check_ai_review_flags(*, pr_spec: str | None, publish_pr: bool, publish_bot: bool, others: Sequence[str]) -> None:
    if pr_spec is None:
        if publish_bot:
            raise click.ClickException("--publish-bot posts to a pull request; add --pr NUMBER|URL")
        if publish_pr:
            raise _LocalUsageError("--publish posts your review to a pull request; a local --ai review posts nothing")
        if others:
            raise _LocalUsageError(f"--ai reviews a change on its own; drop {', '.join(others)}")
        return
    if publish_pr:
        raise click.ClickException(
            "--publish posts your own review as you; --publish-bot posts AI findings as a bot. Use one of them"
        )
    if others:
        raise click.ClickException(
            f"--ai and --publish-bot review a pull request on their own; drop {', '.join(others)}"
        )


#: Local agent CLIs a review can run on, as ``--agent`` names them (the drivers ``LEMONCROW_LLM_BACKEND`` also takes).
AGENT_CLIS = ("claude", "codex", "opencode")


def use_agent_cli(agent: str | None, *, remember: bool = False, store_root: Path | None = None) -> None:
    """``--agent NAME``: run this one review on that local agent CLI, over any API key in the environment.

    An agent CLI spends the person's own subscription, so it is only ever used when they name it: with ``--agent`` each
    time, or once with ``--remember``, which saves the name (``review.agent``) as the agent to use whenever no API key is
    set. The missing-key message shows the command when exactly one is on the PATH, and runs nothing.
    """

    if remember and not agent:
        raise _LocalUsageError("--remember saves the agent you name: add --agent claude (or codex, opencode)")
    if not agent:
        return
    if shutil.which(agent) is None:
        raise _LocalUsageError(
            f"--agent {agent}: `{agent}` is not on your PATH. Install it and sign in, or set a key instead: "
            "export ANTHROPIC_API_KEY=sk-ant-..."
        )
    os.environ["LEMONCROW_LLM_BACKEND"] = agent
    if remember:
        from lemoncrow.core.foundation.paths import default_store_root
        from lemoncrow.core.settings import settings_path, write_setting
        from lemoncrow.pro.capabilities.review.pr_bot.local import AGENT_SETTING

        root = store_root or default_store_root()
        write_setting(root, AGENT_SETTING, agent)
        click.echo(
            f"Saved: with no API key set, lc review --ai now uses {agent} ({settings_path(root)}). "
            f"Undo: lc settings reset {AGENT_SETTING}",
            err=True,
        )


_WORKFLOW_COMMAND = {"notice": "notice", "warning": "warning", "error": "error"}


def _review_pull_request_with_ai(
    ctx: click.Context,
    repo_root: Path | None,
    pr_spec: str,
    *,
    depth: str,
    fail_on: str,
    max_usd_per_pr: float | None,
    max_runs_per_pr: int | None,
    review_drafts: bool,
    on_push: bool,
    diagram: str,
    publish: bool,
    dry_run: bool,
    with_impact: bool,
    as_json: bool,
) -> None:
    """``--ai`` / ``--publish-bot``: review a pull request headlessly, in process, on your own model route."""

    import os

    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root
    from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError, RunOptions, run_pr_review
    from lemoncrow.pro.capabilities.review.pr_bot.state import strip_state

    try:
        root = detect_repo_root(repo_root)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    options = RunOptions(
        pr=pr_spec,
        repo_root=root,
        depth=depth,
        fail_on=fail_on,
        review_drafts=review_drafts,
        on_push=on_push,
        publish=publish,
        dry_run=dry_run,
        with_impact=with_impact,
        max_usd_per_pr=max_usd_per_pr,
        max_runs_per_pr=max_runs_per_pr,
        diagram=diagram,
    )
    try:
        report = run_pr_review(options, progress=lambda message: click.echo(f"  {message}…", err=True))
    except PrReviewError as exc:
        raise click.ClickException(str(exc)) from exc
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if report.skipped and report.reason:
        if in_actions:
            click.echo(f"::{_WORKFLOW_COMMAND.get(report.level, 'notice')} title=LemonCrow Review::{report.reason}")
        else:
            click.echo(report.reason, err=True)
    for note in report.notes:
        click.echo(f"  ! {note}", err=True)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if in_actions and step_summary and report.summary:
        # The run page shows the review even when the comment could not be posted (or on a dry run).
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(strip_state(report.summary) + "\n")
    if as_json:
        _emit(report.to_wire(), as_json=True)
    elif not report.skipped:
        if isinstance(report.published.get("chat"), dict):
            _emit_chat(report, dry_run=dry_run)
        elif report.published:
            _emit_bot_publication(report)
        else:
            click.echo(strip_state(report.summary))
        if dry_run and not isinstance(report.published.get("chat"), dict):
            click.echo(f"\nDry run: would post {len(report.inline_bodies)} inline comment(s) and the summary above.")
    if report.exit_code:
        ctx.exit(report.exit_code)


def _review_local_with_ai(
    ctx: click.Context,
    repo_root: Path | None,
    *,
    store_root: Path,
    rev: str | None,
    base_rev: str | None,
    head_rev: str | None,
    staged: bool,
    working_tree: bool,
    depth: str,
    fail_on: str,
    strict: bool,
    fresh: bool,
    max_usd: float | None,
    max_runs: int | None,
    with_impact: bool,
    as_json: bool,
    no_color: bool,
    show_all: bool,
) -> None:
    """``--ai`` with no ``--pr``: review the working tree, the index or a range, post nothing, exit for a hook."""

    import os

    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root
    from lemoncrow.pro.capabilities.review.pr_bot.local import (
        LocalOptions,
        LocalReviewError,
        render_text,
        run_local_review,
    )

    try:
        root = detect_repo_root(repo_root)
    except ValueError as exc:
        raise _LocalUsageError(str(exc)) from exc
    options = LocalOptions(
        repo_root=root,
        store_root=store_root,
        rev=rev,
        base=base_rev,
        head=head_rev,
        staged=staged,
        working_tree=working_tree,
        depth=depth,
        fail_on=fail_on,
        strict=strict,
        with_impact=with_impact,
        fresh=fresh,
        max_usd=max_usd,
        max_runs=max_runs,
        fail_setup=True,
    )
    try:
        report = run_local_review(options, progress=lambda message: click.echo(f"  {message}…", err=True))
    except LocalReviewError as exc:
        raise _LocalUsageError(str(exc)) from exc
    plain = no_color or bool(os.environ.get("NO_COLOR"))
    if as_json:
        _emit(report.to_wire(), as_json=True)
    else:
        # A review that did not run (no model, a refused key) is an error: said on stderr, in a few plain lines.
        did_not_run = bool(report.setup and report.setup_message and not report.open_findings())
        click.echo(
            render_text(report, style=None if plain else click.style, show_all=show_all),
            nl=False,
            err=did_not_run,
            color=False if plain else None,
        )
    if report.exit_code:
        ctx.exit(report.exit_code)


def _emit_chat(report: Any, *, dry_run: bool) -> None:
    """What a chat run (a comment that mentioned the bot) did."""

    chat = report.published["chat"]
    if dry_run:
        click.echo(f"Dry run: would reply ({chat.get('kind')}):\n")
        click.echo("\n".join(report.inline_bodies))
        return
    click.echo(f"Chat             {chat.get('kind')}: {chat.get('reply_url') or 'no reply posted'}")
    if isinstance(report.published.get("chat_total"), dict):
        click.echo(f"Chat answers     {report.published['chat_total']['answers']} on this pull request")


def _emit_bot_publication(report: Any) -> None:
    published = report.published
    verdict = report.verdict
    if verdict is not None:
        click.echo(verdict.headline)
    if published.get("summary_url"):
        click.echo(f"Summary comment  {published['summary_url']}")
    if "inline" in published:
        click.echo(f"Inline comments  {published['inline']}")
    if published.get("learned"):
        click.echo(f"Learned          {published['learned']} thread(s) told what was learned from feedback")
    if published.get("replied"):
        click.echo(f"Fixed threads    {published['replied']} replied, {published.get('resolved', 0)} resolved")
    status = published.get("status")
    if isinstance(status, dict):
        click.echo(f"Status           {status['state']}: {status['description']}")
    if report.noop:
        click.echo("Nothing changed since the last review; nothing posted.")
