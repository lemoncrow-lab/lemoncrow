"""``lc session recall`` — semantic recall over ALL past sessions."""

from __future__ import annotations

import click

from lemoncrow.gateway.cli.commands._shared import _emit, require_pro


class _RecallGroup(click.Group):
    """A bare query defaults to ``search`` — ``recall "<q>"`` == ``recall search "<q>"``.

    Falls back to normal group resolution whenever the leading token is an
    actual subcommand name (``index``/``search``/``config``) or an option
    (e.g. ``--help``), so explicit invocations are unaffected.
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and args[0] not in self.commands:
            args = ["search", *args]
        return super().resolve_command(ctx, args)


@click.group("recall", cls=_RecallGroup)
def recall_group() -> None:
    """Index past sessions and semantically recall across all of them.

    A bare query defaults to search: ``lc session recall "<q>"`` is
    shorthand for ``lc session recall search "<q>"``.
    """


@recall_group.command("index")
@click.option(
    "--window-days", type=int, default=30, show_default=True, help="Only index sessions modified within N days."
)
@click.option("--max-sessions", type=int, default=80, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def recall_index_cmd(ctx: click.Context, window_days: int, max_sessions: int, as_json: bool) -> None:
    """Incrementally index past session transcripts for recall."""
    require_pro("session_recall", "Session recall over all past sessions")

    from lemoncrow.core.capabilities.session_recall import index_sessions

    result = index_sessions(ctx.obj["root"], window_days=window_days, max_sessions=max_sessions)
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(
        f"Indexed {result['indexed']} snippet(s) from {result['sessions']} session(s) ({result['skipped']} unchanged)."
    )


@recall_group.command("search")
@click.argument("query")
@click.option("--top-k", type=int, default=10, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def recall_search_cmd(ctx: click.Context, query: str, top_k: int, as_json: bool) -> None:
    """Semantically search across all indexed sessions."""
    require_pro("session_recall", "Session recall over all past sessions")

    from lemoncrow.core.capabilities.session_recall import recall

    results = recall(ctx.obj["root"], query, top_k=top_k)
    if as_json:
        _emit(results, as_json=True)
        return
    if not results:
        click.echo("No matches yet — run `lc recall index` first, or try a different query.")
        return
    for item in results:
        host = next((t.split(":", 1)[1] for t in item.get("tags", []) if t.startswith("host:")), "unknown")
        click.echo(f"· [{item['session']}] ({host}) {item['text'][:200]}")


@recall_group.command("config")
@click.option("--auto-index/--no-auto-index", default=None, help="Enable the SessionStart background indexer.")
@click.option("--embedder", type=click.Choice(["openai", "ollama"]), default=None, help="Embedder for indexing.")
@click.option("--embed-model", default=None, help="Embedder model (e.g. an Ollama model name).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def recall_config_cmd(
    ctx: click.Context,
    auto_index: bool | None,
    embedder: str | None,
    embed_model: str | None,
    as_json: bool,
) -> None:
    """Persist Recall settings (auto-index + embedder) to plugin_settings.json."""
    from lemoncrow.core.capabilities.plugin_runtime import set_recall_settings

    updated = set_recall_settings(ctx.obj["root"], auto_index=auto_index, embedder=embedder, embed_model=embed_model)
    summary = {
        "recallAutoIndex": updated.get("recallAutoIndex", True),
        "recallEmbedder": updated.get("recallEmbedder", "null"),
        "recallEmbedModel": updated.get("recallEmbedModel", ""),
    }
    if as_json:
        _emit(summary, as_json=True)
        return
    click.echo(
        f"Recall: auto-index={summary['recallAutoIndex']} "
        f"embedder={summary['recallEmbedder']} model={summary['recallEmbedModel'] or '(default)'}"
    )
