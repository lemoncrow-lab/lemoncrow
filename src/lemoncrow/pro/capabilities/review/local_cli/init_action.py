"""``lc review init-action``: write the GitHub Action workflow for this repository.

The workflow is the sample in ``docs/setup/github-action.md`` (see
:mod:`lemoncrow.pro.capabilities.review.pr_bot.init_action`). The command writes one file, never overwrites a
different one without ``--force``, and prints what the person still has to do: the secret and the pinned SHA.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click


@click.command("init-action")
@click.option(
    "--provider",
    type=click.Choice(["anthropic", "openai"]),
    default="anthropic",
    show_default=True,
    help="Which model key the workflow passes: ANTHROPIC_API_KEY or OPENAI_API_KEY.",
)
@click.option(
    "--fail-on",
    type=click.Choice(["none", "p0"]),
    default="none",
    show_default=True,
    help="p0 fails the `LemonCrow Review` check while a P0 finding is open; none keeps it advisory.",
)
@click.option(
    "--chat",
    is_flag=True,
    help="Also answer `@lemoncrow` in pull request comments and review threads (adds the comment triggers).",
)
@click.option("--force", is_flag=True, help="Replace the workflow file if it exists (without it, none is overwritten).")
@click.option(
    "--repo-root", type=click.Path(path_type=Path, file_okay=False), default=None, help="Repository. Default: here."
)
def init_action_cmd(provider: str, fail_on: str, chat: bool, force: bool, repo_root: Path | None) -> None:
    """Write .github/workflows/lemoncrow-review.yml, the GitHub Action workflow from the setup guide.

    Review-only by default; --chat adds the comment triggers. The file is the guide's sample with your choices, so the
    two cannot disagree. It leaves two things to you, and prints them: adding the model key as a secret and pinning
    the Action to a commit SHA.
    """

    from lemoncrow.pro.capabilities.review.pr_bot import init_action

    where = (repo_root or Path.cwd()).expanduser()
    top = subprocess.run(["git", "-C", str(where), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        raise click.ClickException(f"{where} is not inside a Git repository")
    target = Path(top.stdout.strip()) / init_action.WORKFLOW_PATH
    text = init_action.render_workflow(provider=provider, fail_on=fail_on, chat=chat)
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else None
    except (OSError, UnicodeDecodeError):
        existing = ""
    if existing is not None and existing != text and not force:
        raise click.ClickException(
            f"{init_action.WORKFLOW_PATH} already exists and differs from what this would write, so it was left "
            "alone. Use --force to replace it"
        )
    if existing == text:
        click.echo(f"{init_action.WORKFLOW_PATH} is already up to date; nothing changed.")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        click.echo(
            f"Wrote {init_action.WORKFLOW_PATH} ({'review and chat' if chat else 'review only'}, {provider}, fail-on {fail_on})."
        )
    click.echo("\nNext:")
    for index, (step, commands) in enumerate(init_action.next_steps(provider=provider, chat=chat), 1):
        click.echo(f"  {index}. {step}")
        for command in commands:
            click.echo(f"       {command}")
    click.echo("\nThe setup guide, docs/setup/github-action.md, covers permissions, depth, budgets and forks.")
