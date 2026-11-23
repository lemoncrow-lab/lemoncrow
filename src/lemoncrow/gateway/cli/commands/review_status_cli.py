"""``lc review status``: is this repository's current Review ready, and what is left?

The report and its exit codes live in :mod:`lemoncrow_client.review_terminal`, which the installed ``lc``
runs too, so a script or an agent reads the same document from either command line. Exit status: 0 ready,
1 not ready yet, 2 no Review found, 3 error (a usage error included).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

_EXIT_ERROR = 3  # lemoncrow_client.review_terminal.EXIT_ERROR, restated so `lc --help` imports nothing


class _StatusUsageError(click.UsageError):
    """A command line ``status`` cannot use: exit 3, because 2 means "no Review found"."""

    exit_code = _EXIT_ERROR


class _StatusCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            raise _StatusUsageError(exc.message, ctx=exc.ctx) from exc


@click.command(
    "status",
    cls=_StatusCommand,
    epilog="Exit status: 0 ready, 1 not ready yet (work remains), 2 no Review found for this repository, 3 error.",
)
@click.option("--json", "as_json", is_flag=True, help="One JSON document on standard output (the same exit codes).")
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="The repository. Default: the current one.",
)
@click.option(
    "--review",
    "review_ref",
    default="",
    metavar="REF",
    help="A Review by reference (r/...) instead of this checkout's current one.",
)
@click.pass_context
def status_cmd(ctx: click.Context, as_json: bool, repo_root: Path | None, review_ref: str) -> Any:
    """Report this repository's current Review without capturing.

    Revision, parts reviewed, comments still unresolved, feedback not yet sent, whether it is ready
    to land (the server's readiness verdict and what blocks it), and the Reader URL.
    """

    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError
    from lemoncrow_client.review_terminal import status_main

    try:
        config = load_config(cwd=repo_root or Path.cwd())
    except (ClientError, ValueError) as exc:
        click.echo(str(getattr(exc, "message", exc)), err=True)
        ctx.exit(_EXIT_ERROR)
    ctx.exit(status_main(config, review_ref=review_ref, as_json=as_json, write=click.echo))


__all__ = ["status_cmd"]
