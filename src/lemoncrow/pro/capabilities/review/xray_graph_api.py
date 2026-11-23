"""Review API routes for the X-Ray graph document (``docs/reference/xray-graph.md``).

``GET  /reviews/{id}/xray-graph?revision_id=``  the revision's graph; never runs a model
``POST /reviews/{id}/xray-graph/generate``      rebuild the index graph, or (``ai: true``) start one enrichment

The index graph is cached in ``review_derived`` (kind ``xray_graph``, scope ``index``) under the digest of every
input it was built from, so a new recorded run or observation rebuilds it on the next read; the Review deletion
cascade removes the rows. The AI graph is the same kind under scope ``ai``, stamped with the index digest, the
content-access binding and the route's model, so a changed graph, policy or route never serves an old one. AI
runs only on an explicit ``POST`` (like Ask and thread summaries): it would otherwise spend a Review's budget
unasked. It runs in a background thread and the ``POST`` answers ``202`` ``pending`` at once.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

# Module level: FastAPI resolves the (postponed) ``Request`` annotation from these globals.
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from lemoncrow.pro.capabilities.review.check_api import CheckRecipeDeps
from lemoncrow.pro.capabilities.review.xray_graph import (
    PRODUCER_VERSION,
    AnchorIndex,
    XRayInputs,
    build_index_graph,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

_LOG = logging.getLogger(__name__)

KIND: Final[str] = "xray_graph"
INDEX_SCOPE: Final[str] = "index"
AI_SCOPE: Final[str] = "ai"
#: A pending run row older than this is a crashed run, not a running one.
_PENDING_GRACE: Final[timedelta] = timedelta(seconds=60)

_running_lock = threading.Lock()
#: (review id, revision id) -> the index digest an enrichment is running for, in this process.
_running: dict[tuple[str, str], str] = {}


def _spawn(work: Callable[[], None]) -> None:
    """Run one enrichment off the request (tests replace this to run it inline)."""

    threading.Thread(target=work, name="xray-graph-ai", daemon=True).start()


@dataclass(frozen=True, slots=True)
class XRayHooks:
    """The Review API's own seams the X-Ray reads through (exact-Snapshot evidence, behavior, governance)."""

    evidence: Callable[[Any, Any], Sequence[Any]]
    behavior: Callable[[Any, Any], Any]
    route_policy: Callable[[Any, Any], Any]
    content_access: Callable[[Any, Any], Any]


@dataclass(slots=True)
class _Built:
    inputs: XRayInputs
    texts: AnchorIndex
    graph: dict[str, Any]


def _envelope(state: str, graph: Mapping[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"state": state}
    if graph is not None:
        out["graph"] = dict(graph)
    if reason:
        out["reason"] = reason
    return out


def _unavailable_reason(built: _Built) -> str:
    if not built.inputs.files:
        if any("packet" in note for note in built.inputs.degraded):
            return "This revision's change packet could not be read."
        return "This revision changes no text files the X-Ray can draw."
    if not built.graph.get("nodes"):
        return "Nothing in this revision's change resolves to a component."
    return ""


def _ai_stamp(index_digest: str, binding_digest: str, model: str) -> str:
    return f"{PRODUCER_VERSION}:{index_digest}:{binding_digest}:{model}"


def _parse_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def register_xray_graph_routes(router: APIRouter, deps: CheckRecipeDeps, hooks: XRayHooks) -> None:
    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review import xray_graph_ai as ai
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
    from lemoncrow.pro.capabilities.review.store import DerivedResult
    from lemoncrow.pro.capabilities.review.xray_graph_sources import collect_inputs, inputs_digest

    store = deps.store

    def read_text_for(revision: Any) -> Callable[[str, str], str | None]:
        def read(side: str, path: str) -> str | None:
            try:
                return deps.read_side(revision, path, "old" if side == "base" else "new")
            except (OSError, LookupError, ValueError, UnicodeDecodeError, HTTPException):
                return None

        return read

    def build(session: Any, revision: Any, *, fresh: bool = False) -> _Built:
        """The index graph of exactly these inputs, from the cache unless it is stale or ``fresh``."""

        # lc-debt: every read re-gathers the inputs (packet, evidence records, observations) to check the stamp;
        # upgrade to an evidence-set memo per revision if polling a large Review shows up in request latency.

        read_text = read_text_for(revision)
        texts = AnchorIndex(read_text)
        inputs = collect_inputs(
            store=store,
            revision=revision,
            evidence=hooks.evidence(session, revision),
            read_text=read_text,
            behavior=lambda: hooks.behavior(session, revision),
        )
        stamp = f"{PRODUCER_VERSION}:{inputs_digest(inputs)}"
        cached = None if fresh else store.derived_results(KIND, [revision.id]).get((revision.id, INDEX_SCOPE))
        if cached is not None and cached[0] == stamp and isinstance(cached[1].get("graph"), Mapping):
            return _Built(inputs, texts, dict(cached[1]["graph"]))
        graph = build_index_graph(inputs, read_text, texts=texts)
        store.put_derived_results(
            [DerivedResult(str(session.id), str(revision.id), KIND, INDEX_SCOPE, stamp, {"graph": graph})]
        )
        return _Built(inputs, texts, graph)

    def budget() -> ModelCallBudget:
        return ModelCallBudget.from_env(max_calls=1)

    def stored_ai(revision: Any, stamp: str) -> Mapping[str, Any] | None:
        row = store.derived_results(KIND, [revision.id]).get((revision.id, AI_SCOPE))
        return row[1] if row is not None and row[0] == stamp else None

    def run_pending(session: Any, revision: Any, index_digest: str) -> bool:
        with _running_lock:
            if _running.get((str(session.id), str(revision.id))) == index_digest:
                return True
        # Another process (a hosted replica) may hold the run: its claimed row says so until it settles.
        scope_digest = ai.ai_scope_digest(index_digest)
        horizon = datetime.now(UTC) - timedelta(seconds=budget().timeout_s) - _PENDING_GRACE
        for row in store.list_intelligence_runs(str(session.id), revision_id=str(revision.id), capability=KIND):
            at = _parse_at(str(row.created_at or ""))
            if row.status == "pending" and row.scope_digest == scope_digest and at is not None and at >= horizon:
                return True
        return False

    def read_graph(session: Any, revision: Any) -> dict[str, Any]:
        built = build(session, revision)
        reason = _unavailable_reason(built)
        if reason:
            return _envelope("unavailable", reason=reason)
        index_digest = str(built.graph["digest"])
        if run_pending(session, revision, index_digest):
            return _envelope("pending", built.graph)
        prepared = ai.prepare_xray_ai(
            store=store,
            revision=revision,
            graph=built.graph,
            inputs=built.inputs,
            content_access=hooks.content_access(session, revision),
            budget=budget(),
            policy=hooks.route_policy(session, revision),
            check_budget=False,
        )
        if isinstance(prepared, str):
            return _envelope("ready", built.graph)  # AI is off or not allowed now: its old answer is not shown
        stored = stored_ai(revision, _ai_stamp(index_digest, prepared.binding.digest, prepared.model))
        if stored is not None and isinstance(stored.get("graph"), Mapping):
            return _envelope("ready", stored["graph"])
        if stored is not None and stored.get("failed"):
            return _envelope("ready", built.graph, f"AI enrichment failed: {stored['failed']}"[:300])
        return _envelope("ready", built.graph)

    def enrich(session: Any, revision: Any, built: _Built, prepared: Any, stamp: str, policy: Any) -> None:
        key = (str(session.id), str(revision.id))
        try:
            result = ai.run_xray_ai(
                store=store,
                revision=revision,
                index_graph=built.graph,
                prepared=prepared,
                texts=built.texts,
                budget=budget(),
                policy=policy,
            )
            if result.status in {"ready", "reused"} and result.graph is not None:
                payload: dict[str, Any] = {"graph": result.graph}
            elif result.status == "failed":
                payload = {"failed": result.reason or "the model's answer could not be used"}
            else:
                return  # refused before anything was sent: nothing to remember
            store.put_derived_results([DerivedResult(key[0], key[1], KIND, AI_SCOPE, stamp, payload)])
        except Exception:  # a background enrichment never takes the server down
            _LOG.exception("x-ray graph enrichment crashed review=%s revision=%s", key[0], key[1])
        finally:
            with _running_lock:
                if _running.get(key) == built.graph["digest"]:
                    _running.pop(key, None)

    def generate(session: Any, revision: Any, want_ai: bool) -> tuple[int, dict[str, Any]]:
        built = build(session, revision, fresh=not want_ai)
        reason = _unavailable_reason(built)
        if reason:
            return status.HTTP_200_OK, _envelope("unavailable", reason=reason)
        if not want_ai:
            return status.HTTP_200_OK, _envelope("ready", built.graph)
        index_digest = str(built.graph["digest"])
        if run_pending(session, revision, index_digest):
            return status.HTTP_202_ACCEPTED, _envelope("pending", built.graph)
        policy = hooks.route_policy(session, revision)
        prepared = ai.prepare_xray_ai(
            store=store,
            revision=revision,
            graph=built.graph,
            inputs=built.inputs,
            content_access=hooks.content_access(session, revision),
            budget=budget(),
            policy=policy,
            check_budget=False,
        )
        if isinstance(prepared, str):
            return status.HTTP_409_CONFLICT, _envelope("ready", built.graph, prepared)
        stamp = _ai_stamp(index_digest, prepared.binding.digest, prepared.model)
        stored = stored_ai(revision, stamp)
        if stored is not None and isinstance(stored.get("graph"), Mapping):
            return status.HTTP_200_OK, _envelope("ready", stored["graph"])  # made already: no call
        refusal = ai.call_refusal(store=store, revision=revision, budget=budget(), policy=policy)
        if refusal:
            return status.HTTP_409_CONFLICT, _envelope("ready", built.graph, refusal)
        key = (str(session.id), str(revision.id))
        with _running_lock:
            if _running.get(key) == index_digest:
                return status.HTTP_202_ACCEPTED, _envelope("pending", built.graph)
            _running[key] = index_digest
        try:
            _spawn(lambda: enrich(session, revision, built, prepared, stamp, policy))
        except Exception:
            with _running_lock:
                _running.pop(key, None)
            raise
        return status.HTTP_202_ACCEPTED, _envelope("pending", built.graph)

    @router.get("/reviews/{review_id}/xray-graph")
    async def _get_xray_graph(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """The revision's X-Ray graph (index-only, or AI-enriched when that ran). Never runs a model."""

        def read() -> dict[str, Any]:
            session = deps.session(review_id)
            revision = deps.requested_revision(session, revision_id.strip())
            return read_graph(session, revision)

        return await run_in_threadpool(read)

    @router.post("/reviews/{review_id}/xray-graph/generate")
    async def _post_xray_graph_generate(review_id: str, request: Request) -> JSONResponse:
        """Rebuild the index graph (``ai: false``) or start one explicit, budgeted enrichment (``ai: true``)."""

        body = await deps.json_object(request)
        want_ai = body.get("ai", False)
        if not isinstance(want_ai, bool):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ai must be true or false")
        requested = body.get("revision_id", "")
        if not isinstance(requested, str):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="revision_id must be a string")

        def run() -> tuple[int, dict[str, Any]]:
            session = deps.session(review_id)
            revision = deps.requested_revision(session, requested.strip())
            return generate(session, revision, want_ai)

        code, payload = await run_in_threadpool(run)
        return JSONResponse(status_code=code, content=payload)


__all__ = ["AI_SCOPE", "INDEX_SCOPE", "KIND", "XRayHooks", "register_xray_graph_routes"]
