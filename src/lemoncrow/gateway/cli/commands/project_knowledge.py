"""Deterministic Project Knowledge commands; legacy overlay commands stay separate."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands._shared import _emit


def register_project_knowledge_commands(parent: click.Group) -> None:
    @parent.group("project")
    def project() -> None:
        """Canonical repository context, with zero model calls."""

    def options(fn: Any) -> Any:
        for option in (
            click.option("--repo", type=click.Path(path_type=Path, file_okay=False), required=True),
            click.option("--repository-key", required=True, help="Stable local repository identity."),
            click.option("--canonical-ref", required=True, help="Configured refs/heads/* canonical ref."),
        ):
            fn = option(fn)
        return click.pass_context(fn)

    def sources(ctx: click.Context) -> Any:
        from lemoncrow.pro.capabilities.review.project_knowledge_surface import LocalKnowledgeSources

        return LocalKnowledgeSources(Path(ctx.obj["root"]))

    def source_store(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str) -> tuple[Any, Any]:
        from lemoncrow.pro.capabilities.review.project_knowledge import ProjectKnowledgeStore, RepositoryIdentity
        from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge
        from lemoncrow.pro.capabilities.review.store import ReviewStore

        source = CanonicalGitKnowledge(repo, RepositoryIdentity("local", repository_key), canonical_ref)
        return source, ProjectKnowledgeStore(ReviewStore(Path(ctx.obj["root"])))

    @project.command("sync")
    @options
    @click.option("--previous-revision", default="", help="Immutable previous stored canonical commit.")
    @click.option("--rebuild", is_flag=True, help="Replace derived history with the current canonical baseline.")
    @click.option(
        "--narrate",
        is_flag=True,
        help="Also queue AI narration of the touched subjects (background budget; runs while LemonCrow runs).",
    )
    def sync(
        ctx: click.Context,
        repo: Path,
        repository_key: str,
        canonical_ref: str,
        previous_revision: str,
        rebuild: bool,
        narrate: bool,
    ) -> None:
        """Bootstrap or advance from committed canonical Git objects (zero model calls).

        ``--narrate`` only queues PK1 narration jobs; the worker runs them under
        the background budget, and sync itself still makes no model call.
        """
        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            result = source.sync(store, previous_revision=previous_revision, rebuild=rebuild)
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        # Where this repository syncs from: the Knowledge surface labels it and can regenerate.
        sources(ctx).record(source.repository, canonical_ref=canonical_ref, repo_root=repo)
        if narrate:
            result["narration"] = queue_narration(ctx, source, store, repo, canonical_ref)
        _emit(result, as_json=True)

    def queue_narration(ctx: click.Context, source: Any, store: Any, repo: Path, canonical_ref: str) -> Any:
        from lemoncrow.infra.storage.factory import create_store
        from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler
        from lemoncrow.pro.capabilities.review.project_knowledge_narration import (
            content_access_digest,
            originate_knowledge_narration,
        )

        try:
            access = content_access_digest(source.content_access())
        except (ValueError, OSError) as exc:
            # Without the canonical content policy nothing may reach a model: queue nothing.
            return {"queued": [], "refused": f"content access unavailable: {exc}"}
        jobs_store = create_store(ctx.obj["root"])
        jobs_store.init()
        scheduler = LocalBackgroundScheduler.for_root(jobs_store.jobs, Path(ctx.obj["root"]))
        originations = originate_knowledge_narration(
            scheduler,
            store,
            repository=source.repository,
            canonical_ref=canonical_ref,
            repo_root=repo,
            access_digest=access,
        )
        return {"queued": [row.to_wire() for row in originations]}

    @project.command("query")
    @options
    @click.option("--term", default="")
    @click.option("--offset", default=0, type=int)
    @click.option("--limit", default=50, type=int)
    def query(
        ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str, term: str, offset: int, limit: int
    ) -> None:
        """Read one bounded local page; historical context cannot prove Review checks."""
        from lemoncrow.pro.capabilities.review.project_knowledge import KnowledgeQuery

        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            result = store.query(
                source.repository,
                query=KnowledgeQuery(term=term, offset=offset, limit=limit),
                content_access=source.content_access(),
                route_trust="customer_local",
            )
            _emit(
                {
                    "entries": [row.to_wire() for row in result.entries],
                    "current_generation": result.current_generation,
                    "scanned": result.scanned,
                    "truncated": result.truncated,
                    "next_offset": result.next_offset,
                },
                as_json=True,
            )
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc

    @project.command("prune")
    @options
    @click.option("--before", required=True, help="ISO timestamp retention cutoff.")
    def prune(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str, before: str) -> None:
        """Expire derived historical entries/events; preserve current knowledge."""
        source, store = source_store(ctx, repo, repository_key, canonical_ref)
        _emit(store.prune_before(source.repository, cutoff=before), as_json=True)

    @project.command("purge")
    @options
    @click.option("--path", "paths", multiple=True, help="Hard source purge; omit to purge repository knowledge.")
    def purge(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str, paths: tuple[str, ...]) -> None:
        """Remove derived context only; authoritative Git/Review facts survive."""
        source, store = source_store(ctx, repo, repository_key, canonical_ref)
        removed = store.purge_paths(source.repository, paths) if paths else store.purge_repository(source.repository)
        if not paths:
            sources(ctx).remove(source.repository.id)
        _emit({"removed": removed}, as_json=True)

    @project.command("narratives")
    @options
    def narratives(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str) -> None:
        """List the live AI narratives with their sources (hide or regenerate one by its entry id)."""
        from lemoncrow.pro.capabilities.review.project_knowledge_surface import knowledge_overview

        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            view = knowledge_overview(store, sources(ctx), source.repository.id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit({"narratives": view["narratives"], "truncated": view["narratives_truncated"]}, as_json=True)

    @project.command("hide")
    @options
    @click.argument("entry_id")
    def hide(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str, entry_id: str) -> None:
        """Hide one AI narrative from every reader; deterministic history never changes."""
        from lemoncrow.pro.capabilities.review.project_knowledge_surface import hide_narrative

        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            _emit(hide_narrative(store, source.repository.id, entry_id), as_json=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    @project.command("regenerate")
    @options
    @click.argument("entry_id")
    def regenerate(ctx: click.Context, repo: Path, repository_key: str, canonical_ref: str, entry_id: str) -> None:
        """Queue a fresh narration of one AI narrative (background budget; runs while LemonCrow runs)."""
        from lemoncrow.infra.storage.factory import create_store
        from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler
        from lemoncrow.pro.capabilities.review.project_knowledge_surface import regenerate_narrative

        def scheduler() -> Any:
            jobs_store = create_store(ctx.obj["root"])
            jobs_store.init()
            return LocalBackgroundScheduler.for_root(jobs_store.jobs, Path(ctx.obj["root"]))

        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            registry = sources(ctx)
            registry.record(source.repository, canonical_ref=canonical_ref, repo_root=repo)
            _emit(regenerate_narrative(store, registry, scheduler, source.repository.id, entry_id), as_json=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    @project.command("correct")
    @options
    @click.option("--entry", "entry_id", default="", help="The entry (or AI narrative) the correction is about.")
    @click.option("--subject-path", default="", help="Or the repository path the correction is about.")
    @click.option("--text", default="", help="The correction; '-' reads it from stdin.")
    @click.option("--patch-only", is_flag=True, help="Print only the patch (pipe it to git apply).")
    def correct(
        ctx: click.Context,
        repo: Path,
        repository_key: str,
        canonical_ref: str,
        entry_id: str,
        subject_path: str,
        text: str,
        patch_only: bool,
    ) -> None:
        """Propose a durable correction as a new repository document (a patch; nothing is written).

        It changes Project Knowledge only after a reviewed Change lands it on the
        canonical branch; AI narration and deterministic history are untouched.
        """
        from lemoncrow.pro.capabilities.review.project_knowledge_surface import propose_correction

        if bool(entry_id) == bool(subject_path):
            raise click.UsageError("name exactly one of --entry or --subject-path")
        body = sys.stdin.read() if text == "-" else text
        try:
            source, store = source_store(ctx, repo, repository_key, canonical_ref)
            proposal = propose_correction(
                store,
                source.repository.id,
                text=body,
                entry_id=entry_id,
                subject=None if entry_id else {"kind": "path", "key": subject_path},
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if patch_only:
            click.echo(proposal["patch"], nl=False)
        else:
            _emit(proposal, as_json=True)
