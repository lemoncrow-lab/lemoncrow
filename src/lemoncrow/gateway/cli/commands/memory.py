from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands._shared import _emit, _redact_memory_input


@click.group("memory")
def memory_group_cli() -> None:
    """Inspect and query LemonCrow's archival memory system."""


def _make_memory_registry(cwd: Path | None = None) -> Any:
    from lemoncrow.gateway.cli.commands._shared import require_pro

    require_pro("cross_vendor_memory", "Unified cross-vendor memory")

    from lemoncrow.core.capabilities.cross_vendor_memory import MemoryRegistry
    from lemoncrow.core.capabilities.cross_vendor_memory.claude_adapter import ClaudeAdapter
    from lemoncrow.core.capabilities.cross_vendor_memory.codex_adapter import CodexAdapter
    from lemoncrow.core.capabilities.cross_vendor_memory.gemini_adapter import GeminiAdapter

    return MemoryRegistry(
        adapters=[  # type: ignore[list-item]
            ClaudeAdapter(),
            CodexAdapter(),
            GeminiAdapter(cwd=cwd or Path.cwd()),
        ]
    )


def _make_memory_service(root: Path) -> Any:
    from lemoncrow.core.capabilities.memory import MemoryService
    from lemoncrow.core.foundation.redaction import redact
    from lemoncrow.infra.embeddings.factory import make_embedder
    from lemoncrow.infra.storage.factory import make_memory_store

    return MemoryService(store=make_memory_store(root), embedder=make_embedder(), redactor=redact)


@memory_group_cli.command("remember")
@click.argument("fact")
@click.option("--subject", required=True, help="Short subject for the durable fact.")
@click.option("--scope", type=click.Choice(["repository", "user"]), default="repository", show_default=True)
@click.option("--agent-id", default=None, help="Memory namespace (defaults to shared).")
@click.option("--citations", default="", help="Source citations for the fact.")
@click.option("--reason", default="", help="Why this fact should be retained.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_remember_cmd(
    ctx: click.Context,
    fact: str,
    subject: str,
    scope: str,
    agent_id: str | None,
    citations: str,
    reason: str,
    as_json: bool,
) -> None:
    """Store FACT as durable LemonCrow memory."""
    result = _make_memory_service(ctx.obj["root"]).store_fact(
        agent_id=agent_id,
        subject=_redact_memory_input(subject, "subject"),
        fact=_redact_memory_input(fact, "fact"),
        citations=_redact_memory_input(citations, "citations"),
        reason=_redact_memory_input(reason, "reason"),
        scope=scope,
    )
    payload = result.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"remembered {payload['id']}: {payload['fact']}")


@memory_group_cli.command("vote")
@click.argument("fact")
@click.argument("direction", type=click.Choice(["upvote", "downvote"]))
@click.option("--reason", required=True, help="Why this vote is warranted.")
@click.option("--scope", type=click.Choice(["repository", "user"]), default=None)
@click.option("--agent-id", default=None, help="Memory namespace (defaults to shared).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_vote_cmd(
    ctx: click.Context,
    fact: str,
    direction: str,
    reason: str,
    scope: str | None,
    agent_id: str | None,
    as_json: bool,
) -> None:
    """Vote on an existing durable FACT."""
    result = _make_memory_service(ctx.obj["root"]).vote_fact(
        agent_id=agent_id,
        fact=_redact_memory_input(fact, "fact"),
        direction=direction,
        reason=_redact_memory_input(reason, "reason"),
        scope=scope,
    )
    payload = result.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{payload['direction']} recorded for {payload['id']}")


@memory_group_cli.command("list")
@click.option("--vendor", default=None, help="Filter to a single vendor: claude, codex, gemini.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_list_cmd(ctx: click.Context, vendor: str | None, as_json: bool) -> None:
    """List all detected memory facts, grouped by vendor."""
    import dataclasses

    registry = _make_memory_registry()

    facts = registry.by_vendor(vendor) if vendor else registry.all_facts()

    if as_json:
        click.echo(
            json.dumps(
                [dataclasses.asdict(f) for f in facts],
                default=str,
                indent=2,
            )
        )
        return

    if not facts:
        click.echo("No memory facts found.")
        return

    by_vendor: dict[str, list[Any]] = {}
    for f in facts:
        by_vendor.setdefault(f.vendor, []).append(f)

    total = len(facts)
    n_vendors = len(by_vendor)
    click.echo(f"Memory facts ({total} total, {n_vendors} vendor{'s' if n_vendors != 1 else ''})")
    click.echo("")

    vendor_labels = {
        "claude": "Anthropic - Claude Code",
        "codex": "OpenAI - Codex",
        "gemini": "Google - Gemini CLI",
    }
    for v, vfacts in sorted(by_vendor.items()):
        label = vendor_labels.get(v, v.capitalize())
        click.echo(f"{label} ({len(vfacts)} fact{'s' if len(vfacts) != 1 else ''})")

        by_path: dict[Path, list[Any]] = {}
        for f in vfacts:
            by_path.setdefault(f.source_path, []).append(f)

        for path, pfacts in sorted(by_path.items(), key=lambda x: str(x[0])):
            click.echo(f"  {path}  ({pfacts[0].source_kind})")
            preview = pfacts[:3]
            for fact in preview:
                short = fact.content[:72].replace("\n", " ")
                click.echo(f"    [{fact.fact_id}] {short}")
            if len(pfacts) > 3:
                click.echo(f"    ... {len(pfacts) - 3} more")
        click.echo("")


@memory_group_cli.command("show")
@click.argument("fact_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_show_cmd(fact_id: str, as_json: bool) -> None:
    """Show full content and provenance for FACT_ID."""
    import dataclasses

    registry = _make_memory_registry()
    fact = registry.show(fact_id)

    if fact is None:
        click.echo(f"Fact '{fact_id}' not found.", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(fact), default=str, indent=2))
        return

    click.echo(f"ID:        {fact.fact_id}")
    click.echo(f"Vendor:    {fact.vendor}")
    click.echo(f"Source:    {fact.source_path}:{fact.line_number or '?'}")
    click.echo(f"Kind:      {fact.source_kind}")
    click.echo(f"Read at:   {fact.captured_at.isoformat()}")
    if fact.raw_meta:
        click.echo(f"Meta:      {json.dumps(fact.raw_meta, default=str)}")
    click.echo("")
    click.echo(fact.content)


@memory_group_cli.command("share")
@click.option("--agent-id", required=True, help="Editable memory agent id, e.g. lemon:code.")
@click.option("--label", required=True, help="Editable memory block label.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_share_cmd(ctx: click.Context, agent_id: str, label: str, as_json: bool) -> None:
    """Promote one editable memory block into workspace-shared memory."""
    from lemoncrow.core.capabilities.team import (
        TeamAuditEvent,
        TeamWorkspaceManager,
        ensure_shared_memory_write,
    )
    from lemoncrow.infra.storage.factory import make_memory_store

    root = ctx.obj["root"]
    manager = TeamWorkspaceManager(root)
    workspace = manager.load_workspace()
    member = manager.require_member(None, workspace=workspace)
    ensure_shared_memory_write(member)

    store = make_memory_store(root)
    block = store.get_block(agent_id, label)
    if block is None:
        raise click.ClickException(f"memory block not found: {agent_id}:{label}")
    metadata = dict(block.metadata or {})
    metadata["scope"] = "shared"
    metadata.setdefault("workspace_id", workspace.id)
    metadata.setdefault("owner_user_id", member.user_id)
    metadata["shared_by_user_id"] = member.user_id
    updated = block.model_copy(update={"metadata": metadata})
    stored = store.upsert_block(updated, actor=f"team:{member.user_id}", reason="workspace share")
    manager.append_audit_event(
        TeamAuditEvent(
            action="memory.share",
            actor_user_id=member.user_id,
            details={"agent_id": agent_id, "label": label, "block_id": stored.id},
        )
    )
    payload = {
        "id": stored.id,
        "label": stored.label,
        "scope": stored.metadata.get("scope"),
        "workspace_id": workspace.id,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"shared {agent_id}:{label} into workspace {workspace.name}")


@memory_group_cli.command("find")
@click.argument("query")
@click.option("--limit", default=20, show_default=True, help="Max results.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_find_cmd(query: str, limit: int, as_json: bool) -> None:
    """Find facts matching QUERY using substring + fuzzy search."""
    import dataclasses

    registry = _make_memory_registry()
    facts = registry.find(query, limit=limit)

    if as_json:
        click.echo(json.dumps([dataclasses.asdict(f) for f in facts], default=str, indent=2))
        return

    if not facts:
        click.echo(f"No facts found matching '{query}'.")
        return

    click.echo(f"Found {len(facts)} match{'es' if len(facts) != 1 else ''}:")
    for f in facts:
        short = f.content[:72].replace("\n", " ")
        click.echo(f"  [{f.fact_id}] {short}")


@memory_group_cli.command("paths")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_paths_cmd(as_json: bool) -> None:
    """Show all file paths the memory adapters read from."""
    registry = _make_memory_registry()
    paths_by_vendor = registry.source_paths_by_vendor()

    if as_json:
        click.echo(json.dumps(paths_by_vendor, indent=2))
        return

    if not paths_by_vendor:
        click.echo("No memory source files found on this machine.")
        return

    for vendor, paths in sorted(paths_by_vendor.items()):
        click.echo(f"{vendor}:")
        for p in paths:
            click.echo(f"  {p}")


@memory_group_cli.command("recall")
@click.argument("query")
@click.option("--agent-id", default=None, help="Memory namespace (defaults to shared).")
@click.option("--top-k", default=5, show_default=True, type=int, help="Max passages to return.")
@click.option("--tags", multiple=True, default=None, help="Filter by tag (repeatable).")
@click.option("--since", default=None, help="ISO datetime filter (e.g. 2025-01-01T00:00:00).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_recall_cmd(
    ctx: click.Context,
    query: str,
    agent_id: str | None,
    top_k: int,
    tags: tuple[str, ...] | None,
    since: str | None,
    as_json: bool,
) -> None:
    """Recall relevant archival memory passages by semantic search.

    Searches LemonCrow's internal memory store (embedding + keyword ranking).

    QUERY is the natural-language search string.
    """
    from lemoncrow.core.capabilities.archival_recall import ArchivalRecallCapability
    from lemoncrow.core.foundation.redaction import redact
    from lemoncrow.infra.embeddings.factory import make_embedder
    from lemoncrow.infra.storage.factory import make_memory_store

    root = ctx.obj["root"]
    store = make_memory_store(root)
    embedder = make_embedder()
    capability = ArchivalRecallCapability(store, embedder, redactor=redact)

    since_dt: datetime | None = None
    if since:
        since_dt = datetime.fromisoformat(since)

    passages, recall = capability.recall(
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=list(tags) if tags else None,
        since=since_dt,
    )

    if as_json:
        click.echo(
            json.dumps(
                {
                    "query": recall.query,
                    "agent_id": recall.agent_id,
                    "passages": [
                        {
                            "id": p.id,
                            "text": p.text,
                            "source_ref": p.source_ref,
                            "tags": p.tags,
                            "source": str(p.source) if p.source else None,
                        }
                        for p in passages
                    ],
                },
                indent=2,
            )
        )
        return

    if not passages:
        click.echo("No matching passages found.")
        return

    click.echo(f"Query: {recall.query}")
    click.echo(f"Agent: {recall.agent_id}")
    click.echo(f"Results: {len(passages)} passage{'s' if len(passages) != 1 else ''}")
    click.echo("")
    for i, p in enumerate(passages, start=1):
        click.echo(f"[{i}] (id={p.id})")
        if p.tags:
            click.echo(f"    tags: {', '.join(p.tags)}")
        if p.source_ref:
            click.echo(f"    source: {p.source_ref}")
        click.echo(f"    {p.text[:200]}")
        click.echo("")


__all__ = ["_make_memory_registry", "memory_group_cli"]
