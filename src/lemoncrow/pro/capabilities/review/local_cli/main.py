"""``lc review --ai`` and the three commands beside it, as the installed Review engine runs them.

The installed ``lc`` is the stdlib-only Review client. It captures a change and opens the Reader, and it hands
everything that needs a model route or the repository's rules to the engine the installer put beside it:

    <engine python> -m lemoncrow.pro.capabilities.review.local_cli [review options | hook ... | init-action | config ...]

so a person who ran the installer gets the commands a source checkout has, from one ``lc``. The arguments are the
ones typed after ``lc review``; ``--ai`` is accepted and implied. Usage errors exit 3, because the exit codes 1 and 2
belong to a finding and to an incomplete review and a git hook reads them (see :mod:`...pr_bot.local`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from lemoncrow.pro.capabilities.review.local_cli.ai import (
    AGENT_CLIS,
    DEFAULT_LOCAL_FAIL_ON,
    _check_ai_review_flags,
    _LocalUsageError,
    _review_local_with_ai,
    _review_pull_request_with_ai,
    use_agent_cli,
)
from lemoncrow.pro.capabilities.review.local_cli.config import config_cmd
from lemoncrow.pro.capabilities.review.local_cli.hook import hook_group
from lemoncrow.pro.capabilities.review.local_cli.init_action import init_action_cmd

#: ``lc review <name> ...`` subcommands this module runs.
SUBCOMMANDS: dict[str, click.Command] = {
    "config": config_cmd,
    "hook": hook_group,
    "init-action": init_action_cmd,
}


class _UsageError(click.UsageError):
    """A command line the AI review cannot use: exit 3, so a hook does not read it as a finding (1) or as incomplete (2)."""

    exit_code = 3


class LocalReviewCommand(click.Command):
    """``lc review`` for the engine: a first argument naming a subcommand runs it, anything else is the AI review."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] in SUBCOMMANDS:
            ctx.meta["lemoncrow.local_subcommand"] = (args[0], list(args[1:]))
            return []
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            raise _UsageError(exc.message, ctx=exc.ctx) from exc

    def invoke(self, ctx: click.Context) -> Any:
        routed = ctx.meta.pop("lemoncrow.local_subcommand", None)
        if routed is None:
            return super().invoke(ctx)
        name, rest = routed
        command = SUBCOMMANDS[name]
        with command.make_context(name, rest, parent=ctx) as sub_ctx:
            return command.invoke(sub_ctx)


def _store_root() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return default_store_root()


@click.command("review", cls=LocalReviewCommand, context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("rev", required=False, default=None)
@click.option("--ai", is_flag=True, hidden=True, help="Accepted for `lc review --ai`; the AI review is what this runs.")
@click.option("--base", "base_rev", default=None, help="Explicit base revision (overrides REV).")
@click.option("--head", "head_rev", default=None, help="Explicit head revision. Default: HEAD.")
@click.option("--staged", is_flag=True, help="Review the staged index against HEAD.")
@click.option("--working-tree", is_flag=True, help="Review uncommitted working-tree changes (the default).")
@click.option(
    "--pr",
    "pr_spec",
    default=None,
    metavar="NUMBER|URL",
    help=(
        "Review a GitHub pull request from inside a clone of its repository (needs the `gh` CLI). Prints what the bot "
        "would post, the summary and each inline comment with its place; writes nothing to GitHub, so a closed or "
        "merged pull request works too."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="With --pr: the same as leaving it out. A pull request review here already posts nothing and prints what the bot would.",
)
@click.option(
    "--agent",
    type=click.Choice(AGENT_CLIS),
    default=None,
    help="Review with a local agent CLI you are signed in to, instead of an API key. It uses that tool's own plan.",
)
@click.option(
    "--remember",
    is_flag=True,
    help="With --agent: save that agent as the default for when no API key is set (lc settings reset review.agent undoes it).",
)
@click.option(
    "--depth",
    type=click.Choice(["auto", "thorough", "deep"]),
    default="auto",
    show_default=True,
    help="Review depth. auto follows review.effort in .lemoncrow/review.yaml; the others go deeper.",
)
@click.option(
    "--fail-on",
    type=click.Choice(["none", "p0", "p1"]),
    default=None,
    help=(
        "Exit 1 while a finding at or above this severity is open (none, p0 or p1; default p0). p1 fails on a P0 or a "
        "P1. With --pr N --publish-bot it also fails the LemonCrow Review commit status (default none)."
    ),
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit 2 when the review is incomplete (no model, a spent budget, risk signals left unexamined) instead of 0.",
)
@click.option("--fresh", is_flag=True, help="Ignore the result carried from the last review of this scope.")
@click.option(
    "--max-usd-per-pr",
    "max_usd_per_pr",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="USD",
    help="Stop reviewing this working tree or range once its reviews have cost this much (needs a known cost).",
)
@click.option(
    "--max-runs-per-pr",
    "max_runs_per_pr",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Stop reviewing this working tree or range after this many reviews of it.",
)
@click.option("--no-impact", is_flag=True, help="Skip change-impact analysis (diff-only).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option("--all", "-a", "show_all", is_flag=True, help="Uncapped text output: every finding and full reasons.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour.")
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository to review. Default: the one you are in.",
)
@click.pass_context
def review_cmd(
    ctx: click.Context,
    rev: str | None,
    ai: bool,
    base_rev: str | None,
    head_rev: str | None,
    staged: bool,
    working_tree: bool,
    pr_spec: str | None,
    dry_run: bool,
    agent: str | None,
    remember: bool,
    depth: str,
    fail_on: str | None,
    strict: bool,
    fresh: bool,
    max_usd_per_pr: float | None,
    max_runs_per_pr: int | None,
    no_impact: bool,
    as_json: bool,
    show_all: bool,
    no_color: bool,
    repo_root: Path | None,
) -> None:
    """Review a change with AI on your own model route, and print the findings.

    Reviews the working tree by default, or --staged, REV, or --base/--head. Needs ANTHROPIC_API_KEY or
    OPENAI_API_KEY in the environment, or --agent for a signed-in agent CLI. Posts nothing anywhere. With --pr N it
    reviews a pull request and prints what the bot would post: the summary and each inline comment, with its place.

    \b
    Exit status: 0 clean, 1 a finding at or above --fail-on is open, 2 incomplete with --strict, 3 it could not
    run (a bad option, or no AI model set up, or a refused key).
    Subcommands: hook install|run, init-action, config.
    """

    del ai
    use_agent_cli(agent, remember=remember, store_root=_store_root())
    if dry_run and pr_spec is None:
        raise _LocalUsageError(
            "--dry-run previews what a pull request review would post: a local --ai review already posts nothing; "
            "add --pr NUMBER|URL to preview a pull request"
        )
    if pr_spec is not None:
        if rev or base_rev or head_rev or staged or working_tree:
            raise click.ClickException(
                "--pr picks the range itself; drop REV, --base, --head, --staged and --working-tree"
            )
        _check_ai_review_flags(pr_spec=pr_spec, publish_pr=False, publish_bot=False, others=[])
        _review_pull_request_with_ai(
            ctx,
            repo_root,
            pr_spec,
            depth=depth,
            fail_on=fail_on or "none",
            max_usd_per_pr=max_usd_per_pr,
            max_runs_per_pr=max_runs_per_pr,
            review_drafts=False,
            on_push=True,
            diagram="auto",
            publish=False,
            dry_run=dry_run,
            with_impact=not no_impact,
            as_json=as_json,
        )
        return
    _check_ai_review_flags(pr_spec=None, publish_pr=False, publish_bot=False, others=[])
    _review_local_with_ai(
        ctx,
        repo_root,
        store_root=_store_root(),
        rev=rev,
        base_rev=base_rev,
        head_rev=head_rev,
        staged=staged,
        working_tree=working_tree,
        depth=depth,
        fail_on=fail_on or DEFAULT_LOCAL_FAIL_ON,
        strict=strict,
        fresh=fresh,
        max_usd=max_usd_per_pr,
        max_runs=max_runs_per_pr,
        with_impact=not no_impact,
        as_json=as_json,
        no_color=no_color,
        show_all=show_all,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command with *argv* (default ``sys.argv[1:]``); the exit status is click's, raised as ``SystemExit``."""

    review_cmd.main(args=list(sys.argv[1:] if argv is None else argv), prog_name="lc review")
    return 0


__all__ = ["SUBCOMMANDS", "LocalReviewCommand", "main", "review_cmd"]
