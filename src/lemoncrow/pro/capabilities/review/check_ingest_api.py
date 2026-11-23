"""Review API routes for check ingestion and static findings (Phase 11 P11-FED, Trust T4).

Registered once from ``register_review_api``; every route reuses that app's
session/revision resolvers, so repository scoping and the discarded-Review
read-only rule are the Reader's own. Every post goes through the evidence
adapter SPI (``evidence_adapters.admit``): a plain result, SARIF or check
annotations is caller-attested (``human_observation`` for a human, untyped
otherwise), and an ``in_toto`` attestation is typed (``signed_attestation``)
only when a key the trusted base names verifies it. Provider-CI authority only
ever comes from the provider path, never from this API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review.check_ingest import (
    MAX_INGEST_BYTES,
    IngestConflict,
    IngestRefused,
    record_check_ingest,
)
from lemoncrow.pro.capabilities.review.evidence_adapters import (
    EvidenceAdapter,
    InTotoAdapter,
    ReviewApiAdapter,
    admit,
    review_api_channel,
    trusted_keys_of,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewEvidence,
    ReviewRevision,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.static_findings import (
    record_disposition,
    rule_usefulness,
    static_findings_projection,
)

#: JSON envelope allowance on top of the payload bound.
_ENVELOPE_BYTES = 1024 * 1024


async def _bounded_json(request: Request) -> dict[str, Any]:
    import json

    limit = MAX_INGEST_BYTES + _ENVELOPE_BYTES
    raw_length = request.headers.get("content-length", "")
    if raw_length.isdigit() and int(raw_length) > limit:
        raise HTTPException(
            status_code=413, detail=f"check ingestion is limited to {MAX_INGEST_BYTES} bytes; nothing was stored"
        )
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > limit:
            raise HTTPException(
                status_code=413, detail=f"check ingestion is limited to {MAX_INGEST_BYTES} bytes; nothing was stored"
            )
        buffer.extend(chunk)
    try:
        body = json.loads(bytes(buffer) or b"null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body must be a JSON object")
    return body


def register_check_ingest_routes(
    router: APIRouter,
    *,
    store: Any,
    session: Callable[[str], ReviewSession],
    mutable_session: Callable[[str], ReviewSession],
    requested_revision: Callable[[ReviewSession, str], ReviewRevision],
    latest: Callable[[ReviewSession], ReviewRevision],
    evidence_payload: Callable[[ReviewEvidence, ReviewSession, ReviewRevision], dict[str, Any]],
    verification_projection: Callable[[ReviewSession, ReviewRevision], dict[str, Any]],
    enforce_armed_landing: Callable[[ReviewSession], Awaitable[Any]],
    trusted_review_config: Callable[[ReviewRevision], Any] | None = None,
) -> None:
    def snapshot_paths(revision: ReviewRevision) -> tuple[str, ...]:
        return tuple(unit.path for unit in store.list_units(revision.id) if unit.kind == "file")

    @router.post("/reviews/{review_id}/checks/ingest", status_code=status.HTTP_201_CREATED)
    async def _post_check_ingest(review_id: str, request: Request) -> dict[str, Any]:
        """Ingest one check result (SARIF 2.1.0, check-run annotations, a plain result or in-toto).

        The result binds to the Snapshot named by ``revision_id`` (default:
        latest) and must carry that Snapshot's head SHA. Caller-attested
        results support review and import findings, but never satisfy a typed
        required check or a run-plan entry. A signed in-toto attestation is
        typed only when a trusted-base key verifies it, and refused otherwise.
        Problems with the trusted base's ``attestation_keys`` are returned in
        ``errors`` (and named in a refusal's detail), never dropped.
        """

        current = mutable_session(review_id)
        body = await _bounded_json(request)
        revision = requested_revision(current, str(body.get("revision_id") or "").strip())
        source = str(body.get("source") or "ci").strip()
        adapter: EvidenceAdapter
        keys: dict[str, Any] = {}
        key_errors: tuple[str, ...] = ()
        if str(body.get("format") or "").strip() == "in_toto":
            adapter = InTotoAdapter()
            if trusted_review_config is not None:
                keys, key_errors = await asyncio.to_thread(lambda: trusted_keys_of(trusted_review_config(revision)))
        else:
            adapter = ReviewApiAdapter()
        paths = await asyncio.to_thread(snapshot_paths, revision)
        try:
            channel = review_api_channel(source, current.reviewer_id, trusted_keys=keys)
            ingest, authority = admit(adapter, body, channel, snapshot_paths=paths)
        except IngestRefused as exc:
            problems = f" (trusted-base key problems: {'; '.join(key_errors)})" if key_errors else ""
            raise HTTPException(status_code=422, detail=f"{exc}{problems}"[:2000]) from exc

        def _record() -> tuple[ReviewEvidence, bool]:
            try:
                return record_check_ingest(store, revision, ingest, authority)
            except IngestConflict as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        record, created = await asyncio.to_thread(_record)
        if created:
            await enforce_armed_landing(current)
        return {
            "created": created,
            "evidence": evidence_payload(record, current, revision),
            "findings": len(ingest.findings),
            "truncation": ingest.truncation.to_wire(),
            "authority": ("attested" if authority.typed else "human" if authority.source == "human" else "reported"),
            "errors": list(key_errors),
            "verification": await asyncio.to_thread(verification_projection, current, revision),
        }

    @router.get("/reviews/{review_id}/static-findings")
    def _get_static_findings(review_id: str, revision_id: str = "", path: str = "") -> dict[str, Any]:
        current = session(review_id)
        revision = requested_revision(current, revision_id)
        projection = static_findings_projection(store, revision, path=path)
        projection["latest"] = revision.id == latest(current).id
        return projection

    @router.post("/reviews/{review_id}/static-findings/{finding_id}/disposition")
    async def _post_static_disposition(review_id: str, finding_id: str, request: Request) -> dict[str, Any]:
        """Dismiss, confirm or reopen one current static finding on the latest Snapshot (human only)."""

        current = mutable_session(review_id)
        body = await _bounded_json(request)
        revision = requested_revision(current, str(body.get("revision_id") or "").strip())
        if revision.id != latest(current).id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a static finding is judged on the latest revision; reload it first",
            )

        def _record() -> dict[str, Any]:
            try:
                return record_disposition(
                    store,
                    revision,
                    finding_id,
                    disposition=str(body.get("disposition") or "").strip(),
                    reason=str(body.get("reason") or "").strip(),
                    actor_id=current.reviewer_id,
                )
            except LookupError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        recorded = await asyncio.to_thread(_record)
        return {
            "disposition": recorded,
            "static_findings": await asyncio.to_thread(static_findings_projection, store, revision),
        }

    @router.get("/reviews/{review_id}/checks/usefulness")
    def _get_review_check_usefulness(review_id: str) -> dict[str, Any]:
        current = session(review_id)
        return rule_usefulness(store, [current.id])

    @router.get("/checks/usefulness")
    def _get_check_usefulness(limit: int = 200) -> dict[str, Any]:
        reviews = store.list_sessions(status="", limit=max(1, min(limit, 200)))
        readable: list[str] = []
        for item in reviews:
            try:
                readable.append(session(item.id).id)
            except HTTPException:
                continue
        return rule_usefulness(store, readable)


__all__ = ["register_check_ingest_routes"]
