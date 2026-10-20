"""Capture a pull request into a throwaway Review store and run the AI review on it, in process.

No browser and no server. The pull request becomes a Snapshot in a temporary
``ReviewStore`` exactly as ``lc review --pr`` captures it (merge base to head, GitHub's
*Files changed*), and the Review API is mounted on an in-process ASGI app so the
existing endpoint, run plan, intelligence policy and kill switch decide what runs.

The one difference from the Reader is the *submitter* seam. ``register_review_api``
already hands a fully resolved AI request (effort, effective policy, governance, the
persisted run plan) to an ``ai_review_submitter`` instead of running it, which is how
a hosted worker takes it. Here the submitter runs it inline -- on only the targets that
changed since the last review, and with the same hard call cap -- and keeps the result.

Depth ``auto`` is the Snapshot's own effective plan, escalation included (the route a
fresh re-run takes); ``thorough`` and ``deep`` are the same manual picks the Reader's
"Run deeper" makes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.pr_bot.findings import Located, locate_target
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, cap_units, unit_token

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.ai_review import ConfirmedFinding, IndependentReviewResult
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
    from lemoncrow.pro.capabilities.review.sources.github import PullRequestSource
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

#: ``quick`` is not offered: a manual request only adds to the Snapshot's plan, so it could not lower an
#: Auto default. A repository that wants Quick sets ``review.effort: quick``, which ``auto`` then follows.
DEPTHS = ("auto", "thorough", "deep")


class AiStageError(RuntimeError):
    """The AI review could not be started for a reason the user can act on."""


@dataclass(frozen=True)
class Captured:
    """A pull request as a Snapshot in a throwaway store."""

    store: ReviewStore
    session: ReviewSession
    revision: ReviewRevision
    repo_root: Path
    pull: PullRequestSource


@dataclass(frozen=True)
class AiStage:
    """What the AI stage did, with everything the reconcile and render steps need.

    ``status`` is ``ran``, ``nothing_changed`` (no target differs from the last
    review, so no model was called), ``disabled`` (the intelligence kill switch is
    off), ``unconfigured`` (no model route) or ``unplanned`` (the effective run plan
    runs no AI review).
    """

    status: str
    reason: str = ""
    findings: tuple[ConfirmedFinding, ...] = ()
    locations: Mapping[str, Located] = field(default_factory=dict)
    target_ids: tuple[str, ...] = ()
    rereviewed: tuple[str, ...] = ()
    covered: tuple[str, ...] = ()
    reviewed_units: Mapping[str, str] = field(default_factory=dict)
    units_truncated: bool = False
    changed_lines: int = 0
    changed_files: int = 0
    files: tuple[str, ...] = ()
    unchanged: int = 0
    candidates: int = 0
    investigated: int = 0
    rules: tuple[str, ...] = ()
    policy: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    depth: str = "auto"
    max_calls: int = 0
    complete: bool = True
    errors: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return self.status in {"ran", "nothing_changed"}


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #


def capture_pull_request(
    repo_root: Path, pull: PullRequestSource, store_root: Path, *, with_impact: bool = True
) -> Captured:
    """Record the PR (merge base to head) as one Snapshot of a Review in a store under *store_root*."""

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    rng = replace(resolve_rev_range(repo_root, base=pull.merge_base_sha, head=pull.head_sha), title=pull.review_title)
    store = ReviewStore(store_root)
    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=with_impact,
        with_provenance=False,
        with_patch_text=True,
    )
    session = open_or_create_session(store, repo_root, rng, title=pull.review_title)
    store.update_session(session.id, source_ref=pull.ref.source_ref)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    return Captured(store, session, revision, repo_root, pull)


# --------------------------------------------------------------------------- #
# the route, before anything is captured
# --------------------------------------------------------------------------- #


def route_problem() -> tuple[str, str]:
    """``("", "")`` when a model route can be used; otherwise ``(status, reason)``.

    ``disabled`` is the intelligence kill switch (or an unreadable policy, which fails
    closed); ``unconfigured`` is no provider. Both are checked through the same
    resolver every model call goes through, so the answer here is the answer there.
    """

    from lemoncrow.infra.internal_llm import InternalLLMError
    from lemoncrow.pro.capabilities.review.intelligence_runtime import IntelligenceDisabled, resolve_driver_model

    try:
        resolve_driver_model(None, None)
    except IntelligenceDisabled as exc:
        return "disabled", str(exc)
    except InternalLLMError as exc:
        return "unconfigured", str(exc)
    return "", ""


# --------------------------------------------------------------------------- #
# the in-process Review API
# --------------------------------------------------------------------------- #


def _asgi_post(app: Any, path: str, body: Mapping[str, Any]) -> tuple[int, Any]:
    """One JSON ``POST`` into an ASGI app without a socket or an HTTP client library."""

    payload = json.dumps(body).encode("utf-8")

    async def call() -> tuple[int, Any]:
        sent: list[dict[str, Any]] = []
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 0),
        }
        await app(scope, receive, send)
        status = next(int(item["status"]) for item in sent if item["type"] == "http.response.start")
        raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return status, json.loads(raw) if raw else None

    return asyncio.run(call())


def policy_digest(governance: Any, depth: str) -> str:
    """Everything that makes a reviewed unit's result reusable on the next push.

    A change to the rules, the content-access policy, the depth or the prompt makes
    last time's "reviewed" mean something else, so it re-reviews everything.
    """

    from lemoncrow.pro.capabilities.review.ai_review import PR_FIELDS_VERSION, PROMPT_VERSION, SCHEMA_VERSION

    document = {
        "rules": sorted(rule.digest for rule in governance.rules),
        "content_access": sorted(json.dumps(row.to_wire(), sort_keys=True) for row in governance.content_access),
        "depth": depth,
        "prompt": PROMPT_VERSION,
        "schema": SCHEMA_VERSION,
        "pr_fields": PR_FIELDS_VERSION,
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def revision_usage(store: ReviewStore, review_id: str, revision_id: str) -> tuple[Usage, str]:
    """Model calls, tokens and cost of this Snapshot's runs, and the model that served them.

    Cost is what the provider reported, else the local price table's rate for a model
    it knows exactly. A model with neither has no cost here, never a guessed one.
    """

    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage

    calls = 0
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    cost: float | None = 0.0
    models: list[str] = []
    for run in store.list_intelligence_runs(review_id, revision_id=revision_id):
        if run.model_calls <= 0:
            continue
        calls += run.model_calls
        label = f"{run.driver}/{run.model}" if run.model else run.driver
        if label not in models:
            models.append(label)
        try:
            raw = json.loads(run.usage_json or "{}").get("usage")
        except ValueError:
            raw = None
        used = ModelCallUsage.from_wire(raw if isinstance(raw, dict) else {})
        if used.input_tokens is None or used.output_tokens is None:
            tokens_in = tokens_out = None
        elif tokens_in is not None and tokens_out is not None:
            tokens_in += used.input_tokens
            tokens_out += used.output_tokens
        if cost is None:
            continue
        if used.provider_cost is not None and used.currency in {None, "USD"}:
            cost += used.provider_cost
            continue
        priced = _price(run.model, used)
        cost = None if priced is None else cost + priced
    return Usage(calls, tokens_in, tokens_out, cost), ", ".join(models)


def _price(model: str, used: ModelCallUsage) -> float | None:
    """USD for one call at the local price table's rate -- only for a model it knows exactly, on reported tokens."""

    from lemoncrow.core.capabilities.pricing import get_model_pricing

    if not model or used.input_tokens is None or used.output_tokens is None:
        return None
    pricing = get_model_pricing(model)
    if not pricing.known or pricing.approximate:
        return None
    return pricing.cost_usd(
        input_tokens=used.input_tokens,
        output_tokens=used.output_tokens,
        cache_read_tokens=used.cache_read_input_tokens or 0,
        cache_write_tokens=used.cache_write_input_tokens or 0,
    )


def run_ai_stage(
    captured: Captured,
    *,
    depth: str = "auto",
    previous: ReviewState | None = None,
    structured_executor: Callable[..., StructuredExecution] | None = None,
) -> AiStage:
    """Run the AI review on what changed since *previous*, through the Review API's own AI endpoint."""

    from fastapi import FastAPI

    from lemoncrow.pro.capabilities.review.api import register_review_api

    if depth not in DEPTHS:
        raise AiStageError(f"depth must be one of {', '.join(DEPTHS)}")
    store, session, revision = captured.store, captured.session, captured.revision
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    outcome: dict[str, AiStage] = {}

    def submit(
        request_session: ReviewSession,
        request_revision: ReviewRevision,
        effort: str,
        trigger: str,
        policy: Mapping[str, Any],
        governance: Any,
        action_id: str,
        *,
        run_plan: Mapping[str, Any],
        fresh: bool = False,
    ) -> Mapping[str, Any]:
        # The seam a hosted worker's queue sits behind, run inline: everything the endpoint resolved is passed in.
        outcome["stage"] = _execute(app, captured, depth, previous, policy, governance, structured_executor)
        return {"status": "inline", "revision_id": request_revision.id}

    async def open_access() -> None:
        # A one-shot, in-process app: nothing outside this process can reach it.
        return None

    register_review_api(
        app,
        store,
        auth_dependency=open_access,
        repo_root=captured.repo_root,
        port=0,
        enforce_origin_guard=False,
        source_refresh_enabled=False,
        live_journal_enabled=False,
        ask_enabled=False,
        ai_review_submitter=submit,
    )
    expected = {"expected_revision_id": revision.id}
    if depth == "auto":
        # The Snapshot's effective plan, escalation included: what an Auto trigger would run.
        status, payload = _asgi_post(
            app, f"/api/reviews/{session.id}/ai-review/fresh", {**expected, "action_id": f"ci-{revision.head_sha[:12]}"}
        )
    else:
        status, payload = _asgi_post(
            app, f"/api/reviews/{session.id}/ai-review", {**expected, "effort": depth, "trigger": "manual"}
        )
    if status != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise AiStageError(f"the AI review endpoint refused the request ({status}): {detail}")
    stage = outcome.get("stage")
    if stage is None:
        # The endpoint decided against running it: the plan has no AI, or its policy is invalid.
        wire = payload.get("ai_review") if isinstance(payload, dict) else None
        detail = (
            str(wire.get("detail") if isinstance(wire, dict) else "") or "the effective run plan does not run AI review"
        )
        return AiStage(status="unplanned", reason=detail, depth=depth)
    return stage


class _Examined:
    """Wraps the model executor to learn which targets a model call actually looked at.

    The engine reports which targets it *planned* (``checked_target_ids``), not which a hypothesis or rule
    question reached: Auto investigates at most two hypotheses however many candidates there are. A target counts
    as examined only when a call about it returned, so a failed call, a spent budget or the depth limit leave it
    pending. A prompt this cannot read counts for nothing: unknown means not examined, never examined.
    """

    def __init__(self, inner: Callable[..., StructuredExecution]) -> None:
        self.inner = inner
        self.targets: set[str] = set()

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        execution = self.inner(**kwargs)
        self.targets.update(self._targets(kwargs.get("prompt")))
        return execution

    @staticmethod
    def _targets(prompt: object) -> set[str]:
        try:
            blocks = json.loads(str(prompt))
        except ValueError:
            return set()
        if not isinstance(blocks, dict):
            return set()
        hypothesis = blocks.get("hypothesis")
        if isinstance(hypothesis, dict) and isinstance(hypothesis.get("value"), dict):
            ids = hypothesis["value"].get("target_ids")
            return {item for item in ids if isinstance(item, str)} if isinstance(ids, list) else set()
        if "question" in blocks:  # a per-rule question: the targets under that rule's paths
            source = blocks.get("snapshot_source")
            value = source.get("value") if isinstance(source, dict) else None
            return {key for key in value if isinstance(key, str)} if isinstance(value, dict) else set()
        return set()


def _candidates_phrase(candidates: int) -> str:
    return f"{candidates} risk candidate" + ("" if candidates == 1 else "s")


def _tokens(
    targets: tuple[ReviewTarget, ...], units: Mapping[str, tuple[str, str]], *, pending: Collection[str]
) -> dict[str, str]:
    """``{unit key token: fingerprint token}`` of every target whose review is done."""

    return {
        units[t.target_id][0]: units[t.target_id][1]
        for t in targets
        if t.target_id in units and t.target_id not in pending
    }


def _execute(
    app: Any,
    captured: Captured,
    depth: str,
    previous: ReviewState | None,
    policy: Mapping[str, Any],
    governance: Any,
    structured_executor: Callable[..., StructuredExecution] | None,
) -> AiStage:
    from lemoncrow.pro.capabilities.review.ai_review import generate_candidates, run_independent_review
    from lemoncrow.pro.capabilities.review.independence import configured_reviewer_routes
    from lemoncrow.pro.capabilities.review.intelligence_policy import review_intelligence_policy
    from lemoncrow.pro.capabilities.review.intelligence_runtime import (
        ModelCallBudget,
        bound_intelligence_policy,
        execute_structured,
    )
    from lemoncrow.pro.capabilities.review.planned_execution import persisted_plan
    from lemoncrow.pro.capabilities.review.run_plan import ai_stage_plan

    store, session, revision = captured.store, captured.session, captured.revision
    context = app.state.review_worker_ai_publication_context(session.id, revision.id)
    targets: tuple[ReviewTarget, ...] = tuple(context["targets"])
    plan = persisted_plan(store, revision.id)
    if plan is None:
        raise AiStageError("the Snapshot has no persisted effective run plan")
    if plan.policy_error:
        return AiStage(status="unplanned", reason=f"Review policy invalid: {plan.policy_error}", depth=depth)
    stage_plan = ai_stage_plan(plan)
    if not stage_plan.runs_ai:
        return AiStage(status="unplanned", reason=stage_plan.reason or "AI review is not in the run plan", depth=depth)

    units = {
        f"target:{unit.unit_key}": unit_token(unit.unit_key, unit.content_fingerprint)
        for unit in store.list_units(revision.id)
    }
    digest = policy_digest(governance, stage_plan.effort)
    reuse = previous is not None and previous.policy == digest and previous.depth == depth
    before = previous.reviewed_units if previous is not None and reuse else {}

    def unchanged(target: ReviewTarget) -> bool:
        token = units.get(target.target_id)
        return token is not None and before.get(token[0]) == token[1]

    changed = tuple(target for target in targets if not unchanged(target))
    locations = {target.target_id: locate_target(target) for target in targets}
    rules = tuple(rule.stable_key for rule in governance.rules)
    base = AiStage(
        status="nothing_changed",
        locations=locations,
        target_ids=tuple(target.target_id for target in targets),
        unchanged=len(targets) - len(changed),
        files=tuple(sorted({target.path for target in targets})),
        rules=rules,
        policy=digest,
        depth=depth,
        max_calls=stage_plan.max_review_calls or ModelCallBudget.from_env(max_calls=1).max_review_calls,
    )
    if not changed:
        kept, truncated = cap_units(_tokens(targets, units, pending=()))
        return replace(base, reviewed_units=kept, units_truncated=truncated, model=previous.model if previous else "")

    budget = ModelCallBudget.from_env(max_calls=1)
    examined = _Examined(structured_executor or execute_structured)
    with bound_intelligence_policy(review_intelligence_policy(policy)):
        result: IndependentReviewResult = run_independent_review(
            store=store,
            revision=revision,
            targets=changed,
            repo_root=captured.repo_root,
            effort=stage_plan.effort,
            rules=tuple(governance.rules),
            content_access=tuple(governance.content_access),
            policy_context=policy,
            budget=budget,
            persist_annotations=False,
            stage_plan=stage_plan,
            reviewer_candidates=configured_reviewer_routes()[0],
            structured_executor=examined,
            pr_fields=True,
        )
    usage, served = revision_usage(store, session.id, revision.id)
    candidate_ids = {
        target_id
        for candidate in generate_candidates(changed, rules=tuple(governance.rules))
        for target_id in candidate.target_ids
    }
    # Reviewed means a model call about the target returned (or it carried no risk signal). A candidate no call
    # reached -- a failed call, a spent budget, Auto's cap on investigations -- is pending: it is retried on the next
    # push and competes for the cap again, so a large pull request is covered over its pushes, never silently skipped.
    uncovered = result.status == "skipped_unnecessary" and result.candidates > 0
    complete = result.status in {"completed", "skipped_unnecessary"} and not result.provider_errors
    complete = complete and not result.skipped_by_budget and not uncovered
    pending = candidate_ids - examined.targets
    why = ""
    if result.skipped_by_budget:
        why = "the model-call budget ran out"
    elif uncovered:
        why = f"{stage_plan.effort} depth cannot cover {_candidates_phrase(result.candidates)}; ask for more depth"
    kept, truncated = cap_units(_tokens(targets, units, pending=pending))
    changed_ids = tuple(target.target_id for target in changed)
    return replace(
        base,
        status="ran",
        reason=why,
        findings=tuple(result.machine_findings),
        rereviewed=changed_ids,
        covered=tuple(target_id for target_id in changed_ids if target_id not in pending),
        reviewed_units=kept,
        units_truncated=truncated,
        changed_lines=sum(target.additions + target.deletions for target in changed),
        changed_files=len({target.path for target in changed}),
        candidates=len(candidate_ids),
        investigated=len(candidate_ids & examined.targets),
        usage=usage,
        model=served or (f"{result.driver}/{result.model}" if result.driver else ""),
        max_calls=result.calls_max,
        complete=complete,
        errors=tuple(result.provider_errors),
    )
