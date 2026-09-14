"""``lc resume-context`` — what a fresh session needs to pick up an old one.

The brief is bounded on purpose: it exists to be pasted into a new agent
session, so it competes for the same context window it is meant to save. Every
field is capped and the rendered form stays under 60 lines no matter how large
the session was.

Symbol lookup is opt-in (``--symbols``) because it is the one part of the build
that needs pygit2 and a code index; without the flag the command never imports
either. Heavy imports stay inside the callback so ``lc --help`` stays cheap.
"""

from __future__ import annotations

from pathlib import Path

import click

from lemoncrow.gateway.cli.commands._shared import _emit


@click.command("resume-context")
@click.argument("session_id")
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository for symbol lookup. Default: the resolved workspace root.",
)
@click.option("--symbols", is_flag=True, help="Include important symbols (requires a code index).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def resume_context_cmd(
    ctx: click.Context,
    session_id: str,
    repo_root: Path | None,
    symbols: bool,
    as_json: bool,
) -> None:
    """A compact, source-linked continuation brief for one session."""

    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.resume_context.builder import build_resume_context, render_resume_context

    obj = ctx.obj or {}
    store_root = Path(obj.get("root") or default_store_root())

    # `--repo-root` only ever feeds the symbol pass, so without `--symbols` the
    # builder is handed None and the whole index-dependent path is skipped.
    lookup_root = (repo_root or _workspace_repo_root()) if symbols else None

    try:
        context = build_resume_context(store_root, session_id, repo_root=lookup_root)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _emit(context.to_dict(), as_json=True)
        return

    _emit(render_resume_context(context), as_json=False)


def _workspace_repo_root() -> Path:
    """Resolve the default ``--repo-root``: the checkout cwd or the workspace names.

    Matches the documented default. Bare ``Path.cwd()`` would not: an explicit
    path is taken literally by ``detect_repo_root``, so running from a
    non-checkout directory would drop the symbol pass even when the workspace
    root is a perfectly good repository. Returning cwd when nothing resolves
    keeps the builder's own "not a repository -> no symbols" path in charge.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root

    try:
        return detect_repo_root()
    except ValueError:
        return Path.cwd()


__all__ = ["resume_context_cmd"]
