"""``atelier session replay`` — counterfactual session replay (reconstruct, no re-run).

Replays a recorded coding session (claude / codex / opencode / copilot /
hermes / cursor / antigravity) as a full transcript timeline and marks the
grep→read loops a single Atelier ``code_search`` would have collapsed. Reads
recorded sessions off disk only — no model is re-run, no API is called.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.core import ParameterSource

from atelier.core.capabilities.session_replay import SUPPORTED_HOSTS, detect_transcript_host, load_replays
from atelier.core.capabilities.session_replay_live import enrich_replay
from atelier.core.capabilities.session_replay_render import render_html, render_text


@click.command("replay")
@click.option("--session-id", default=None, help="Session id to replay (looked up under the host's store).")
@click.option(
    "--host",
    type=click.Choice(list(SUPPORTED_HOSTS)),
    default="claude",
    show_default=True,
    help="Which agent's sessions to read.",
)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Replay a specific transcript JSONL directly (works for any host).",
)
@click.option("--last", type=int, default=1, show_default=True, help="Replay the N most recent sessions.")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repo root for real code_search/read output (default: current directory).",
)
@click.option("--no-live", is_flag=True, help="Skip calling real Atelier tools; show the structural view only.")
@click.option("--no-network", is_flag=True, help="Do not perform web_fetch calls during enrichment.")
@click.option(
    "--html",
    "html_out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path for the HTML replay (default: <root>/replay/<session>.html).",
)
@click.option("--no-open", is_flag=True, help="Do not open the HTML replay in a browser.")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw replay model as JSON (no HTML/terminal).")
@click.option("--no-color", is_flag=True, help="Disable ANSI color in terminal output.")
@click.pass_context
def replay_cmd(
    ctx: click.Context,
    session_id: str | None,
    host: str,
    file_path: Path | None,
    last: int,
    repo: Path | None,
    no_live: bool,
    no_network: bool,
    html_out: Path | None,
    no_open: bool,
    as_json: bool,
    no_color: bool,
) -> None:
    """Replay a past session and show what Atelier's one-shot search would collapse.

    Reconstructed from the recorded transcript — no model is re-run, no API call,
    deterministic. The full conversation is replayed (assistant text, thinking,
    tool calls and outputs); the grep-and-read loops the agent walked are marked
    and collapsed into the single ``code_search`` that would have replaced them.

    Works for every supported host: claude, codex, opencode (opencode.db),
    copilot, hermes (state.db), cursor and antigravity (imported sessions).
    With ``--file`` and no explicit ``--host``, the transcript format is
    auto-detected.

    \b
    Examples:
      atelier session replay --last 1
      atelier session replay --session-id <id> --host codex
      atelier session replay --host opencode --last 3
      atelier session replay --file ./session.jsonl --html replay.html
    """
    if file_path is not None and ctx.get_parameter_source("host") == ParameterSource.DEFAULT:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        detected, turn_count = detect_transcript_host(content)
        if detected is None or turn_count == 0:
            click.echo(
                f"Could not parse any turns from {file_path} — not a recognized transcript "
                "(tried claude, codex, opencode, copilot and normalized formats).",
                err=True,
            )
            ctx.exit(1)
        if detected != host:
            click.echo(f"Detected a {detected} transcript — parsing as {detected}.", err=True)
            host = detected

    replays = load_replays(
        host=host, session_id=session_id, file=file_path, last=max(1, last), store_root=ctx.obj["root"]
    )

    if replays and not no_live:
        repo_root = (repo or Path.cwd()).resolve()
        click.echo(f"Calling real Atelier tools against {repo_root} (read-only; edit/bash preview only)…", err=True)
        for replay in replays:
            enrich_replay(replay, repo_root, allow_network=not no_network)

    if not replays:
        where = f"session {session_id}" if session_id else f"recent {host} sessions"
        click.echo(
            f"No transcript found for {where}.\nPass an explicit file with --file <path.jsonl>, or check --host.",
            err=True,
        )
        ctx.exit(1)

    if as_json:
        click.echo(json.dumps({"replays": [r.to_dict() for r in replays]}, indent=2, default=str))
        return

    # Always produce both: the terminal timeline and a shareable HTML page.
    for replay in replays:
        click.echo(render_text(replay, color=not no_color))
        click.echo("")

    root: Path = ctx.obj["root"]
    if html_out is None:
        stem = replays[0].session_id or "session"
        html_out = root / "replay" / f"{stem}.html"
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(render_html(replays), encoding="utf-8")
    total = sum(r.summary.calls_saved for r in replays if r.summary)
    click.echo(f"HTML replay: {html_out}  ({len(replays)} session(s), {total} tool calls collapsed).")

    if not no_open:
        import webbrowser

        try:
            webbrowser.open(html_out.resolve().as_uri())
        except Exception:  # noqa: BLE001 - opening a browser must never fail the command
            click.echo("(could not open a browser automatically; open the file above manually.)")
