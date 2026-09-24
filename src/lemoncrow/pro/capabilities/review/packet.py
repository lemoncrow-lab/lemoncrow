"""The `lc review` packet orchestrator — the only module that composes the rest.

Why this module exists: the diff, the semantic impact, the reading order and
the agent provenance are four independent analyses with four independent
failure modes. Composing them here — behind three small, fixed-signature hooks
— means each analysis can be built, degraded or disabled on its own without any
of the others learning about it, and the packet always comes back whole.

The four ``_collect_*``/``_link_*`` hooks below are the composition seams. Each
one is a total function: it returns a value on every path, including "the thing
it needs is not installed". Failing an analysis costs a name in
``ReviewPacket.degraded`` — never an exception.

The analysis modules are imported *inside* the hooks on purpose. ``impact.py``
pulls in the edit-impact detectors and the code-context engine, ``provenance.py``
pulls in the history store; importing either at module scope would make a
diff-only ``lc review --no-impact`` pay for both, and would turn a broken
optional dependency into an import error for the whole command instead of one
name in ``degraded``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .gitdiff import (
    MAX_HUNK_PATCH_BYTES,
    BlobPair,
    RevRange,
    collect_diff,
    commit_datetime,
    head_symbol_filter,
    load_blobs,
)
from .models import (
    SCHEMA_VERSION,
    ChangedFile,
    ChangedSymbol,
    EvidenceRecord,
    ImpactSite,
    IndexStatus,
    ProvenanceRecord,
    ReviewOrderEntry,
    ReviewPacket,
    unknown_provenance,
)


def _collect_impact(
    repo_root: Path,
    files: Sequence[ChangedFile],
    *,
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
    limit_sites: int = 40,
    exists_in_head: Callable[[str, str, str], int | None] | None = None,
    reuse: object | None = None,
) -> tuple[tuple[ChangedSymbol, ...], tuple[ImpactSite, ...], IndexStatus, tuple[str, ...]]:
    """Return (symbols, impact sites, index status, degraded signals).

    Delegates to ``review.impact.collect_impact``. That function already owns
    every index/ast-grep degradation, so the only failure this layer has to
    absorb is the analysis being unimportable at all.

    *exists_in_head* is passed straight through: the impact pass reads a code
    index keyed to the workspace on disk, and only this layer knows which
    revision is actually under review. It takes ``(path, symbol, caller)`` and
    returns the line to cite -- file existence alone let a file that outlived the
    reviewed revision stand in for one that actually named the symbol there, and
    a yes/no answer left the packet printing the workspace's line numbers beside
    paths in a revision that never had them.
    """

    try:
        from .impact import collect_impact
    except Exception:
        return (), (), "absent", ("impact_unavailable",)
    try:
        result = collect_impact(
            repo_root,
            files,
            old_blobs=old_blobs,
            new_blobs=new_blobs,
            limit_sites=limit_sites,
            exists_in_head=exists_in_head,
            reuse=reuse,  # type: ignore[arg-type]
        )
    except Exception:
        return (), (), "absent", ("impact_failed",)
    return result.symbols, result.sites, result.index_status, result.degraded


def _collect_provenance(
    store_root: Path,
    repo_root: Path,
    packet_paths: Sequence[str],
    *,
    head_sha: str,
    head_commit_time: datetime | None,
    session_id: str | None = None,
) -> tuple[ProvenanceRecord, tuple[EvidenceRecord, ...], tuple[str, ...]]:
    """Return (provenance, evidence records, degraded signals).

    Delegates to ``review.provenance.collect_provenance``, which shares one
    resolved ``Trace`` between the correlator and the evidence reader so the
    store is walked once.
    """

    try:
        from .provenance import collect_provenance
    except Exception:
        return unknown_provenance(), (), ("provenance_unavailable",)
    try:
        return collect_provenance(
            store_root,
            repo_root,
            packet_paths,
            head_sha=head_sha,
            head_commit_time=head_commit_time,
            session_id=session_id,
        )
    except Exception:
        return unknown_provenance(), (), ("provenance_failed",)


def _link_impact_to_provenance(
    provenance: ProvenanceRecord,
    impact: tuple[ImpactSite, ...],
) -> tuple[ProvenanceRecord, tuple[ImpactSite, ...]]:
    """Cross-fill the two fields that need *both* analyses.

    ``ProvenanceRecord.uninspected_impacted`` and ``ImpactSite.inspected_by_agent``
    are the only fields no single analysis can compute, which is why they get
    their own pass here instead of leaking impact into the correlator or a
    session store into the impact adapter.

    The recorded paths are host-absolute while impact site paths are
    repo-relative ``"rel/p.py:L12"``, so ``provenance.link_impact_to_provenance``
    matches on the relative tail rather than on equality, and claims nothing at
    all when provenance is ``unknown``.
    """

    try:
        from .provenance import link_impact_to_provenance
    except Exception:
        return provenance, impact
    try:
        return link_impact_to_provenance(provenance, impact)
    except Exception:
        return provenance, impact


def _collect_order(
    files: Sequence[ChangedFile],
    symbols: Sequence[ChangedSymbol],
    impact: Sequence[ImpactSite],
    *,
    inspected: frozenset[str] = frozenset(),
    limit: int = 40,
) -> tuple[ReviewOrderEntry, ...]:
    """Return the reading order, most-important first.

    Truncation happens *after* ranking so the surviving entries keep ranks
    ``1..limit``. The fallback — a stable path-sorted order, every score 0.0 and
    no reasons — is useful and never misleading, so an unimportable ranker costs
    nothing but the explanations.
    """

    try:
        from .ordering import rank_files

        return rank_files(files, symbols, impact, inspected=inspected)[: max(0, limit)]
    except Exception:
        return tuple(
            ReviewOrderEntry(path=item.path, rank=index, score=0.0, reasons=(), group=item.category)
            for index, item in enumerate(sorted(files, key=lambda f: f.path)[: max(0, limit)], start=1)
        )


@dataclass(frozen=True)
class PacketBuild:
    """A packet and the exact blob texts that produced it.

    The pair exists because they must not be read twice. A working-tree packet
    is a photograph of a moment; re-reading the tree afterwards to fingerprint
    it photographs a *different* moment, and any file saved in between is then
    stored as content A under fingerprints describing content B. Handing both
    back from one build is the only way a persisted revision can promise that
    what the reviewer was shown is what the marks describe.

    ``blobs`` is empty when the build was told not to keep them (see
    :func:`build_review_packet`) or when the change has no readable text side.
    """

    packet: ReviewPacket
    blobs: BlobPair


def build_review_packet(
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    with_impact: bool = True,
    with_provenance: bool = True,
    session_id: str | None = None,
    context_lines: int = 3,
    limit: int = 40,
    with_patch_text: bool = False,
) -> ReviewPacket:
    """Build the complete review packet for *rng*.

    Never raises for a missing index, a missing session store or an unreadable
    blob: each of those adds a name to ``ReviewPacket.degraded`` instead.

    *with_patch_text* is passed straight to :func:`collect_diff`. It defaults to
    ``False`` so ``lc review --json`` is byte-identical to what it was apart from
    the new, empty ``patch`` key; only a packet that will be *persisted* as the
    record of a revision needs the hunk bodies.

    The blobs read along the way are dropped. A caller that will *persist* this
    packet must use :func:`build_review_packet_with_blobs` instead, so the
    fingerprints describe the same bytes the packet does.
    """

    return _build(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=with_impact,
        with_provenance=with_provenance,
        session_id=session_id,
        context_lines=context_lines,
        limit=limit,
        with_patch_text=with_patch_text,
        keep_blobs=False,
    ).packet


def build_review_packet_with_blobs(
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    with_impact: bool = True,
    with_provenance: bool = True,
    session_id: str | None = None,
    context_lines: int = 3,
    limit: int = 40,
    with_patch_text: bool = False,
    unbounded_patch_text: bool = False,
    progress: Callable[[str], None] | None = None,
) -> PacketBuild:
    """:func:`build_review_packet`, plus the blob texts the build actually read.

    Identical work and identical degradation behaviour; the only difference is
    that the new-side text is loaded even when the impact pass is switched off,
    and that both halves come back together. Use this whenever the packet will
    be fingerprinted, stored or marked against.
    """

    return _build(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=with_impact,
        with_provenance=with_provenance,
        session_id=session_id,
        context_lines=context_lines,
        limit=limit,
        with_patch_text=with_patch_text,
        unbounded_patch_text=unbounded_patch_text,
        keep_blobs=True,
        progress=progress,
    )


def attach_changed_symbols(
    repo_root: Path,
    build: PacketBuild,
    *,
    limit: int = 40,
) -> PacketBuild:
    """Attach stable changed-symbol identity without running impact fan-out."""

    try:
        from .impact import collect_changed_symbols

        result = collect_changed_symbols(
            repo_root,
            build.packet.files,
            old_blobs=build.blobs.old,
            new_blobs=build.blobs.new,
        )
        symbols = result.symbols
        index_status = result.index_status
        symbol_degraded = result.degraded
    except Exception:
        symbols = ()
        index_status = "absent"
        symbol_degraded = ("impact_failed",)
    degraded = set(build.packet.degraded)
    degraded.update(symbol_degraded)
    order = _collect_order(
        build.packet.files,
        symbols,
        (),
        inspected=frozenset(build.packet.provenance.files_inspected),
        limit=len(build.packet.files),
    )
    stats = dict(build.packet.stats)
    stats["symbols"] = len(symbols)
    return PacketBuild(
        packet=replace(
            build.packet,
            symbols=tuple(symbols),
            order=order,
            index_status=index_status,
            degraded=tuple(sorted(degraded)),
            stats=stats,
        ),
        blobs=build.blobs,
    )


def attach_impact(
    repo_root: Path,
    rng: RevRange,
    build: PacketBuild,
    *,
    limit: int = 40,
    reuse: object | None = None,
) -> PacketBuild:
    """Attach semantic impact to an already captured diff/blob snapshot.

    Refresh uses this two-phase seam so it can compare the new file bytes with
    the previous ReviewRevision before deciding which deterministic detector
    work is reusable. The diff and blobs are never re-read, preserving the same
    snapshot invariant as :func:`build_review_packet_with_blobs`.
    """

    files = build.packet.files
    blobs = build.blobs
    degraded = set(build.packet.degraded)
    degraded.discard("impact_disabled")

    in_head: Callable[[str, str, str], int | None] | None
    try:
        in_head = head_symbol_filter(repo_root, rng)
    except Exception:
        in_head = None

    symbols, impact, index_status, impact_degraded = _collect_impact(
        repo_root,
        files,
        old_blobs=blobs.old,
        new_blobs=blobs.new,
        limit_sites=limit,
        exists_in_head=in_head,
        reuse=reuse,
    )
    degraded.update(impact_degraded)

    provenance, impact = _link_impact_to_provenance(build.packet.provenance, impact)
    order = _collect_order(
        files,
        symbols,
        impact,
        inspected=frozenset(provenance.files_inspected),
        limit=limit,
    )
    stats = dict(build.packet.stats)
    stats["symbols"] = len(symbols)
    stats["impact_sites"] = len(impact)

    return PacketBuild(
        packet=replace(
            build.packet,
            symbols=symbols,
            impact=impact,
            order=order,
            provenance=provenance,
            index_status=index_status,
            degraded=tuple(sorted(degraded)),
            stats=stats,
        ),
        blobs=blobs,
    )


def _build(
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    with_impact: bool,
    with_provenance: bool,
    session_id: str | None,
    context_lines: int,
    limit: int,
    with_patch_text: bool,
    keep_blobs: bool,
    unbounded_patch_text: bool = False,
    progress: Callable[[str], None] | None = None,
) -> PacketBuild:
    """The single composition pass behind both public entry points."""

    if progress is not None:
        progress("diff")
    diff = collect_diff(
        repo_root,
        rng,
        context_lines=context_lines,
        with_patch_text=with_patch_text,
        patch_text_limit=None if unbounded_patch_text else MAX_HUNK_PATCH_BYTES,
    )
    files = diff.files
    degraded: set[str] = set(diff.degraded)

    # One read, whether it feeds the impact pass, the fingerprints, or both.
    blobs = BlobPair(old={}, new={}, degraded=())
    if files and (with_impact or keep_blobs):
        if progress is not None:
            progress("source")
        blobs = load_blobs(repo_root, rng, files)
        degraded.update(blobs.degraded)

    symbols: tuple[ChangedSymbol, ...] = ()
    impact: tuple[ImpactSite, ...] = ()
    index_status: IndexStatus = "absent"
    if with_impact and files:
        if progress is not None:
            progress("impact")
        in_head: Callable[[str, str, str], int | None] | None
        try:
            in_head = head_symbol_filter(repo_root, rng)
        except Exception:
            # Never a precondition: without it the impact pass just keeps the
            # pre-existing, over-broad behaviour.
            in_head = None
        symbols, impact, index_status, impact_degraded = _collect_impact(
            repo_root,
            files,
            old_blobs=blobs.old,
            new_blobs=blobs.new,
            limit_sites=limit,
            exists_in_head=in_head,
        )
        degraded.update(impact_degraded)
    elif not with_impact:
        degraded.add("impact_disabled")

    provenance = unknown_provenance()
    evidence: tuple[EvidenceRecord, ...] = ()
    if with_provenance:
        if progress is not None:
            progress("provenance")
        provenance, evidence, provenance_degraded = _collect_provenance(
            store_root,
            repo_root,
            [item.path for item in files],
            head_sha=rng.head_sha,
            head_commit_time=commit_datetime(repo_root, rng.head_sha),
            session_id=session_id,
        )
        degraded.update(provenance_degraded)
    else:
        degraded.add("provenance_disabled")

    provenance, impact = _link_impact_to_provenance(provenance, impact)

    if progress is not None:
        progress("ranking")
    order = _collect_order(
        files,
        symbols,
        impact,
        inspected=frozenset(provenance.files_inspected),
        # Persisted packets feed safety decisions as well as presentation. A
        # terminal display cap must never erase ranking reasons from the units
        # beyond it, otherwise bulk completion can treat risky files as safe.
        limit=len(files) if keep_blobs else limit,
    )
    stats = {
        "files": len(files),
        "additions": sum(item.additions for item in files),
        "deletions": sum(item.deletions for item in files),
        "hunks": sum(len(item.hunks) for item in files),
        "symbols": len(symbols),
        "impact_sites": len(impact),
    }

    packet = ReviewPacket(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now(tz=UTC).isoformat(),
        repo_root=str(repo_root),
        range_mode=rng.mode,
        base_rev=rng.base_rev,
        head_rev=rng.head_rev,
        base_sha=rng.base_sha,
        head_sha=rng.head_sha,
        merge_base_sha=rng.merge_base_sha,
        dirty=rng.dirty,
        title=rng.title,
        files=files,
        symbols=symbols,
        impact=impact,
        order=order,
        provenance=provenance,
        evidence=evidence,
        index_status=index_status,
        degraded=tuple(sorted(degraded)),
        stats=stats,
    )
    return PacketBuild(packet=packet, blobs=blobs)


__all__ = [
    "PacketBuild",
    "attach_changed_symbols",
    "attach_impact",
    "build_review_packet",
    "build_review_packet_with_blobs",
]
