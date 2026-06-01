from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import yaml

from atelier.core.foundation.models import ReasonBlock, to_jsonable
from atelier.core.foundation.renderer import render_block_markdown
from atelier.core.foundation.store import ContextStore
from atelier.gateway.cli.commands._dev import dev_command as _dev_command
from atelier.gateway.cli.commands._dev import dev_group as _dev_group
from atelier.gateway.cli.commands._shared import _emit, _load_store, _parse_duration


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_domain_manager(root: Path) -> Any:
    from atelier.core.domains import DomainManager

    return DomainManager(root)


@_dev_command("reembed")
@click.option("--dry-run", is_flag=True, help="Count legacy rows without writing vectors.")
@click.option("--batch-size", default=100, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reembed(ctx: click.Context, dry_run: bool, batch_size: int, as_json: bool) -> None:
    """Back-fill legacy_stub embeddings for archival passages and lesson candidates."""
    from atelier.infra.embeddings.factory import make_embedder

    root: Path = ctx.obj["root"]
    store = ContextStore(root)
    store.init()
    embedder = make_embedder()
    counts = {"archival_passage": 0, "lesson_candidate": 0, "dry_run": dry_run}
    with store._connect() as conn:
        passages = conn.execute(
            """
            SELECT id, text FROM archival_passage
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        lessons = conn.execute(
            """
            SELECT id, cluster_fingerprint, evidence_trace_ids, body FROM lesson_candidate
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        counts["archival_passage"] = len(passages)
        counts["lesson_candidate"] = len(lessons)
        if not dry_run:
            for row in passages:
                vector = embedder.embed([str(row["text"])])[0]
                conn.execute(
                    """
                    UPDATE archival_passage
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector).encode("utf-8"), embedder.__class__.__name__, row["id"]),
                )
            for row in lessons:
                text = "\n".join(
                    [
                        str(row["cluster_fingerprint"]),
                        str(row["evidence_trace_ids"]),
                        str(row["body"]),
                    ]
                )
                vector = embedder.embed([text])[0]
                conn.execute(
                    """
                    UPDATE lesson_candidate
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector), embedder.__class__.__name__, row["id"]),
                )
    _emit(counts, as_json=as_json)


@_dev_command("add-block")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def add_block(ctx: click.Context, path: Path) -> None:
    """Add or update a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@click.group("domain")
def domain_group() -> None:
    """Manage Atelier internal domain bundles."""


@domain_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_list(ctx: click.Context, as_json: bool) -> None:
    """List available domain bundles (built-in + user)."""
    manager = _load_domain_manager(ctx.obj["root"])
    refs = manager.list_bundles()
    payload = [r.model_dump(mode="json") for r in refs]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not payload:
        click.echo("(no domain bundles)")
        return
    for item in payload:
        click.echo(f"{item['bundle_id']}\t{item['domain']}\t{item['description'][:60]}")


@domain_group.command("info")
@click.argument("bundle_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_info(ctx: click.Context, bundle_id: str, as_json: bool) -> None:
    """Show details for a domain bundle."""
    manager = _load_domain_manager(ctx.obj["root"])
    result = manager.info(bundle_id)
    if result is None:
        raise click.ClickException(f"domain bundle not found: {bundle_id}")
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@click.command("report")
@click.option("--since", default="7d", show_default=True, help="Lookback duration, e.g. 7d or 12h.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    show_default=True,
)
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def report_cmd(ctx: click.Context, since: str, output_format: str, output_path: Path | None) -> None:
    """Generate an engineering-leader governance report."""
    from atelier.core.capabilities.reporting.weekly_report import generate_report, render_markdown

    store = _load_store(ctx.obj["root"])
    report = generate_report(_parse_duration(since), store=store, repo_root=Path.cwd())
    if output_format == "json":
        rendered = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
    else:
        rendered = render_markdown(report)
    if output_path is not None:
        output_path.write_text(rendered, encoding="utf-8")
        return
    click.echo(rendered.rstrip())


@click.command("import-style-guide")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path, exists=True))
@click.option("--domain", default="coding", show_default=True)
@click.option("--dry-run", is_flag=True, help="Print proposed candidates without writing.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.pass_context
def import_style_guide_cmd(
    ctx: click.Context,
    paths: tuple[Path, ...],
    domain: str,
    dry_run: bool,
    limit: int,
) -> None:
    """Draft lesson candidates from Markdown style guides."""
    from atelier.core.capabilities.style_import import import_files
    from atelier.infra.internal_llm.ollama_client import OllamaUnavailable

    if not paths:
        raise click.ClickException("at least one Markdown file or directory is required")
    store = _load_store(ctx.obj["root"])
    try:
        candidates = import_files(paths, domain, store=store, write=not dry_run, limit=limit)
    except OllamaUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "dry_run": dry_run,
        "written": 0 if dry_run else len(candidates),
        "candidates": [candidate.model_dump(mode="json", exclude={"embedding"}) for candidate in candidates],
    }
    if dry_run:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    click.echo(f"imported {len(candidates)} lesson candidates into inbox")
    for candidate in candidates:
        click.echo(candidate.id)


@_dev_group("block")
def block_group() -> None:
    """ReasonBlock curation commands."""


@block_group.command("list")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_list(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:  # type: ignore
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no blocks)")
        return
    click.echo(f"{len(blocks)} blocks shown")
    for b in blocks:
        click.echo(f"{b.id}\t{b.domain}\t{b.title}")


@block_group.command("add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def block_add(ctx: click.Context, path: Path) -> None:  # type: ignore
    """Import a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@block_group.command("extract")
@click.argument("trace_id")
@click.option("--save", is_flag=True, help="Persist the candidate block.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_extract(ctx: click.Context, trace_id: str, save: bool, as_json: bool) -> None:  # type: ignore
    """Extract a candidate ReasonBlock from a trace."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    from atelier.core.foundation.extractor import extract_candidate

    candidate = extract_candidate(trace)
    if save:
        store.upsert_block(candidate.block)
    payload = {
        "block": to_jsonable(candidate.block),
        "confidence": candidate.confidence,
        "reasons": candidate.reasons,
        "saved": save,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"candidate: {candidate.block.id} (confidence={candidate.confidence:.2f})")
    for r in candidate.reasons:
        click.echo(f"  - {r}")
    click.echo(render_block_markdown(candidate.block))


@click.command("list-blocks")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_blocks_cmd(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    from atelier.core.foundation.metrics import summarize

    summary = summarize(store)
    click.echo(
        f"# {len(blocks)} blocks shown "
        f"(active={summary.blocks_active}, "
        f"deprecated={summary.blocks_deprecated}, "
        f"quarantined={summary.blocks_quarantined})"
    )
    for b in blocks:
        click.echo(f"{b.status[:1].upper()} {b.id}\t{b.domain}\t{b.title}")


__all__ = [
    "add_block",
    "block_group",
    "domain_group",
    "import_style_guide_cmd",
    "list_blocks_cmd",
    "reembed",
    "report_cmd",
]
