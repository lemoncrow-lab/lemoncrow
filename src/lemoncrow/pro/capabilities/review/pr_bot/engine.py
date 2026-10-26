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
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.pr_bot import impact as impact_mod
from lemoncrow.pro.capabilities.review.pr_bot import large_pr, pr_intent
from lemoncrow.pro.capabilities.review.pr_bot import overview as overview_mod
from lemoncrow.pro.capabilities.review.pr_bot.findings import Located, locate_target
from lemoncrow.pro.capabilities.review.pr_bot.pr_intent import IntentNote
from lemoncrow.pro.capabilities.review.pr_bot.settings import (
    NoiseInputs,
    NoisePolicy,
    resolve_policy,
    with_ignored_paths,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, cap_units, unit_token

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.ai_review import ConfirmedFinding, IndependentReviewResult
    from lemoncrow.pro.capabilities.review.gitdiff import RevRange
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
    """A pull request, or a local range, as a Snapshot in a throwaway store. ``pull`` is ``None`` for a local range."""

    store: ReviewStore
    session: ReviewSession
    revision: ReviewRevision
    repo_root: Path
    pull: PullRequestSource | None = None


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
    #: Changed code with no risk signal that the review set out to look at (``ai_review.general_review_targets``), and
    #: how many of it a model call about it returned for. ``candidates`` and ``investigated`` count the risk signals only.
    general: int = 0
    general_investigated: int = 0
    rules: tuple[str, ...] = ()
    #: Settings imported from another tool's config that have no behavior here ("strictness 3: imported, not applied").
    imported: tuple[str, ...] = ()
    policy: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    depth: str = "auto"
    max_calls: int = 0
    complete: bool = True
    errors: tuple[str, ...] = ()
    #: What the repository context pack did (G4): status, estimated tokens, item counts, paths. Empty: no pack.
    repo_context: Mapping[str, Any] = field(default_factory=dict)
    #: ``{target id: ((side, first line, last line), ...)}`` for every target, so a caller can say which changed
    #: lines a finding is about (the review benchmark scores a finding by them).
    spans: Mapping[str, tuple[tuple[str, int, int], ...]] = field(default_factory=dict)
    #: Targets a model call about them returned: the ones the model was actually asked about.
    examined: tuple[str, ...] = ()
    #: ``{finding id: still_present | resolved | unclear}``: what the re-check said about each earlier finding it was
    #: shown. A finding the model was not asked about, or did not answer, has no entry, and an entry is never "fixed".
    answers: Mapping[str, str] = field(default_factory=dict)
    #: Ids of the earlier open findings whose cited lines are still in the file, word for word, wherever they now
    #: sit: the code those findings were about has not changed, whatever else did.
    intact: tuple[str, ...] = ()
    #: The deterministic impact of the whole change as :meth:`impact.Impact.to_state` writes it (``""``: not computed),
    #: and every changed target that carries a risk signal with what this run did about it (:mod:`impact`). Both come
    #: from the Snapshot's targets and nothing a model said.
    impact: str = ""
    signals: tuple[impact_mod.Signal, ...] = ()
    #: The noise controls in force for this review (``settings.NoisePolicy``): the trusted base's, else the workflow's.
    noise: NoisePolicy = field(default_factory=NoisePolicy)
    #: Files left out of the review because ``review.ignore`` names them (never read by the model, no findings), and
    #: the number of files in the rest of the change.
    ignored: tuple[str, ...] = ()
    total_files: int = 0
    #: Files this run left for the next because the change has more than ``max_files``, highest risk first; they are
    #: pending, not reviewed (``large_pr``).
    deferred_files: tuple[str, ...] = ()
    #: What the review was told the change is for, and what could not be read (``pr_intent.gather``); ``None``: not asked.
    intent: IntentNote | None = None
    #: What the pull request does (:mod:`overview`): made by one model call this run (its usage is in ``usage``), or reused
    #: from the last review of the same change (``overview_reused``); ``None`` when none was asked for, the change is
    #: trivial, or the call did not give one (``overview_note`` says why, for the job log).
    overview: overview_mod.Overview | None = None
    overview_reused: bool = False
    overview_note: str = ""
    #: The change whose overview this run decided (:attr:`overview.Request.identity`), kept in the state so the same change
    #: is not asked again; ``""``: undecided (not asked for, or the call failed).
    overview_for: str = ""

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

    rng = replace(resolve_rev_range(repo_root, base=pull.merge_base_sha, head=pull.head_sha), title=pull.review_title)
    return capture_range(repo_root, rng, store_root, with_impact=with_impact, pull=pull)


def capture_range(
    repo_root: Path,
    rng: RevRange,
    store_root: Path,
    *,
    with_impact: bool = True,
    pull: PullRequestSource | None = None,
) -> Captured:
    """Record any resolved range (a commit range, the index or the working tree) as one Snapshot in a store under
    *store_root*: the capture ``lc review --ai`` makes of a change that has no pull request (*pull* is ``None``)."""

    from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.working_tree_authority import WorkingTreeAuthority

    store = ReviewStore(store_root)
    if pull is None:
        # A change in the developer's own checkout: their configuration files, as they are on disk, apply.
        store.working_tree_authority = WorkingTreeAuthority(repo_root)
    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=with_impact,
        with_provenance=False,
        with_patch_text=True,
    )
    session = open_or_create_session(store, repo_root, rng, title=rng.title)
    if pull is not None:
        store.update_session(session.id, source_ref=pull.ref.source_ref)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    if revision.capture_kind == "draft":
        # The working tree and the index are captured as running drafts, which the Review API will not run an AI
        # review on until they are saved as a Snapshot; this store is throwaway, so saving it is the capture itself.
        store.publish_snapshot(session.id, revision.id)
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


def policy_digest(governance: Any, depth: str, rule_severity: Mapping[str, str] | None = None) -> str:
    """Everything that makes a reviewed unit's result reusable on the next push.

    A change to the rules, the content-access policy (``review.ignore`` is part of it), the severities the file gave
    its rules, the depth or the prompt makes last time's "reviewed" mean something else, so it re-reviews everything.
    What only changes how findings are shown (the threshold, ``inline_comments``, ``max_files``) is not in it.
    """

    from lemoncrow.pro.capabilities.review.ai_review import PR_FIELDS_VERSION, PROMPT_VERSION, SCHEMA_VERSION
    from lemoncrow.pro.capabilities.review.repo_context import CONTEXT_VERSION, repo_context_enabled

    document: dict[str, Any] = {
        "rules": sorted(rule.digest for rule in governance.rules),
        "content_access": sorted(json.dumps(row.to_wire(), sort_keys=True) for row in governance.content_access),
        "depth": depth,
        "prompt": PROMPT_VERSION,
        "schema": SCHEMA_VERSION,
        "pr_fields": PR_FIELDS_VERSION,
    }
    if rule_severity:
        # Left out when there is none, so a repository without overrides keeps the digest it had.
        document["rule_severity"] = sorted(rule_severity.items())
    if repo_context_enabled():
        # Context changes what a reviewed unit's result means (what the model saw): turning it on or off, or a new
        # selection rule, re-reviews once. Left out when off, so a repository that opts out keeps its digest.
        document["repo_context"] = CONTEXT_VERSION
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


def call_usage(model: str, used: ModelCallUsage) -> Usage:
    """One structured model call's usage as the bot records it: the provider's cost, else the local price table's.

    The same rule :func:`revision_usage` applies to a review's calls, for a call made outside a Review run (a chat
    answer). A cost that neither the provider nor the table knows stays ``None``, never a guess.
    """

    if used.provider_cost is not None and used.currency in {None, "USD"}:
        cost: float | None = used.provider_cost
    else:
        cost = _price(model, used)
    return Usage(max(used.calls, 1), used.input_tokens, used.output_tokens, cost)


def _price(model: str, used: ModelCallUsage) -> float | None:
    """USD for one call at the local price table's rate -- only for a model it knows exactly, on reported tokens."""

    from lemoncrow.pro.capabilities.review.intelligence_runtime import exact_table_cost_usd

    return exact_table_cost_usd(model, used)


def _mount(captured: Captured, submit: Callable[..., Mapping[str, Any]] | None = None) -> Any:
    """The Review API for *captured*'s store on an in-process app nothing outside this process can reach."""

    from fastapi import FastAPI

    from lemoncrow.pro.capabilities.review.api import register_review_api

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def open_access() -> None:
        # A one-shot, in-process app: nothing outside this process can reach it.
        return None

    register_review_api(
        app,
        captured.store,
        auth_dependency=open_access,
        repo_root=captured.repo_root,
        port=0,
        enforce_origin_guard=False,
        source_refresh_enabled=False,
        live_journal_enabled=False,
        ask_enabled=False,
        ai_review_submitter=submit,
    )
    return app


@dataclass(frozen=True)
class ReviewContext:
    """What the Review API resolved for a Snapshot: its targets, the rules and content-access policy in force, and
    the effective intelligence policy. Chat reads this to build the same context an investigation would see."""

    targets: tuple[ReviewTarget, ...]
    governance: Any
    policy: Mapping[str, Any]


def review_context(captured: Captured) -> ReviewContext:
    """The Snapshot's targets and governance, resolved by the Review API itself (never re-implemented here)."""

    app = _mount(captured)
    context = app.state.review_worker_ai_publication_context(captured.session.id, captured.revision.id)
    # An ignored path stays unreadable to chat as it is to the review.
    governance = with_ignored_paths(
        context["governance"], _noise_policy(context, context["governance"], None), context.get("review_config")
    )
    return ReviewContext(tuple(context["targets"]), governance, context["policy"])


def _noise_policy(context: Mapping[str, Any], governance: Any, inputs: NoiseInputs | None) -> NoisePolicy:
    """The noise controls for the Snapshot: its trusted-base ``.lemoncrow/review.yaml`` and triage policy, then the workflow's inputs."""

    return resolve_policy(
        config=context.get("review_config"),
        triage=context.get("triage"),
        rules=tuple(governance.rules),
        inputs=inputs,
    )


def run_ai_stage(
    captured: Captured,
    *,
    depth: str = "auto",
    previous: ReviewState | None = None,
    structured_executor: Callable[..., StructuredExecution] | None = None,
    only_targets: Collection[str] | None = None,
    inputs: NoiseInputs | None = None,
    intent_loader: Callable[[Captured], IntentNote] | None = None,
    overview: overview_mod.Request | None = None,
) -> AiStage:
    """Run the AI review on what changed since *previous*, through the Review API's own AI endpoint.

    *inputs* are the workflow's defaults for the noise controls (the trusted base's ``.lemoncrow/review.yaml`` wins);
    *intent_loader* reads the pull request's description and linked issues onto the Snapshot, called only when a model
    is about to be asked.

    *overview* asks for the overview of the change (:mod:`overview`) once the review is done: one more model call, counted
    in the review's call cap and in its usage, unless the change is trivial or the overview of this very change is
    already in the request. ``None`` (the default) asks for none, which is what a benchmark or a hook wants.

    *only_targets* narrows the review to those targets and nothing else, whatever else changed (chat's "recheck"
    names one finding's targets; an empty collection asks for no model call at all). The reviewed-unit list and the
    policy digest the stage returns then stay those of *previous* except for the targets it did re-review, so a
    narrow run never marks code it did not look at as reviewed.
    """

    if depth not in DEPTHS:
        raise AiStageError(f"depth must be one of {', '.join(DEPTHS)}")
    session, revision = captured.session, captured.revision
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
        outcome["stage"] = _execute(
            app,
            captured,
            depth,
            previous,
            policy,
            governance,
            structured_executor,
            only_targets=only_targets,
            inputs=inputs,
            intent_loader=intent_loader,
            overview=overview,
        )
        return {"status": "inline", "revision_id": request_revision.id}

    app = _mount(captured, submit)
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
        #: ``{target id: categories a returned call asked about it}``: what "examined" meant for each target.
        self.categories: dict[str, set[str]] = {}

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        execution = self.inner(**kwargs)
        category, ids = self._scope(kwargs.get("prompt"))
        also = self._also(kwargs.get("prompt"))
        self.targets.update(ids)
        for target_id in ids:
            if category:
                self.categories.setdefault(target_id, set()).update({category, *also})
        return execution

    @staticmethod
    def _also(prompt: object) -> set[str]:
        """The other categories one call covers (small files with different risk signals share a call)."""

        try:
            blocks = json.loads(str(prompt))
        except ValueError:
            return set()
        hypothesis = blocks.get("hypothesis") if isinstance(blocks, dict) else None
        value = hypothesis.get("value") if isinstance(hypothesis, dict) else None
        extra = value.get("also_categories") if isinstance(value, dict) else None
        return {item for item in extra if isinstance(item, str)} if isinstance(extra, list) else set()

    @staticmethod
    def _targets(prompt: object) -> set[str]:
        return _Examined._scope(prompt)[1]

    @staticmethod
    def _scope(prompt: object) -> tuple[str, set[str]]:
        """``(category, target ids)`` a prompt asks about; ``("", set())`` for one this cannot read."""

        try:
            blocks = json.loads(str(prompt))
        except ValueError:
            return "", set()
        if not isinstance(blocks, dict):
            return "", set()
        hypothesis = blocks.get("hypothesis")
        if isinstance(hypothesis, dict) and isinstance(hypothesis.get("value"), dict):
            ids = hypothesis["value"].get("target_ids")
            category = hypothesis["value"].get("category")
            return (
                category if isinstance(category, str) else "",
                {item for item in ids if isinstance(item, str)} if isinstance(ids, list) else set(),
            )
        if "question" in blocks:  # a per-rule question: the targets under that rule's paths
            source = blocks.get("snapshot_source")
            value = source.get("value") if isinstance(source, dict) else None
            return "critical_rule", (
                {key for key in value if isinstance(key, str)} if isinstance(value, dict) else set()
            )
        return "", set()


def _prior_rows(previous: ReviewState | None, changed: Collection[str]) -> tuple[dict[str, Any], ...]:
    """The earlier open findings about targets that changed, as the re-check prompt shows them.

    The model is asked, per finding id, whether the problem is still there; without them a changed target is
    re-asked blind, and a model that simply does not repeat itself would read as a fix.
    """

    if previous is None:
        return ()
    return tuple(
        {
            "id": old.id,
            "category": old.category,
            "target_ids": list(old.target_ids),
            "title": old.title,
            "explanation": old.explanation[:300],
            "consequence": old.consequence[:200],
        }
        for old in previous.open_findings()
        if set(old.target_ids) & set(changed)
    )


def _intact_findings(captured: Captured, previous: ReviewState | None) -> tuple[str, ...]:
    """Ids of the earlier open findings whose cited lines are still, verbatim, in the file at the new head.

    A finding is only ever marked fixed when the lines it cited changed. Line numbers cannot say so (an edit above
    shifts them), so the text is looked for: present anywhere means that code is untouched. A finding with no
    digest (it cited no checked lines) is never intact here, and is never auto-fixed either.
    """

    if previous is None:
        return ()
    from lemoncrow.pro.capabilities.review.ask import exact_revision_sources
    from lemoncrow.pro.capabilities.review.finding_replacement import lines_intact

    wanted = [old for old in previous.open_findings() if old.anchor_digest and old.side == "new"]
    if not wanted:
        return ()
    paths = tuple(dict.fromkeys(old.path for old in wanted))
    sources = exact_revision_sources(captured.store, captured.revision, paths=paths, side="new")
    return tuple(old.id for old in wanted if old.path in sources and lines_intact(sources[old.path], old.anchor_digest))


def _tokens(
    targets: tuple[ReviewTarget, ...], units: Mapping[str, tuple[str, str]], *, pending: Collection[str]
) -> dict[str, str]:
    """``{unit key token: fingerprint token}`` of every target whose review is done."""

    return {
        units[t.target_id][0]: units[t.target_id][1]
        for t in targets
        if t.target_id in units and t.target_id not in pending
    }


def _overview(
    request: overview_mod.Request | None,
    captured: Captured,
    governance: Any,
    policy: Mapping[str, Any],
    structured_executor: Callable[..., StructuredExecution] | None,
    *,
    calls_used: int,
    calls_cap: int,
) -> tuple[dict[str, Any], Usage]:
    """``(AiStage fields, usage)`` of the overview step: the overview of this change, reused or made, and what its call cost.

    Never raises and never fails a review: a route that cannot be resolved, a call that fails and an answer with nothing
    usable are each a note and no overview. The call goes through the route the review used, under the review's own
    policy, and may be shown only what the repository's content-access policy lets this route see. An agent-CLI route
    is run in an empty directory, so it has no repository to read, only the prompt.
    """

    if request is None:
        return {}, Usage()
    from lemoncrow_client.kit.redaction import redact

    from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
    from lemoncrow.pro.capabilities.review.intelligence_policy import review_intelligence_policy
    from lemoncrow.pro.capabilities.review.intelligence_runtime import (
        AGENT_CLI_DRIVERS,
        bound_intelligence_policy,
        execute_structured,
        resolve_driver_model,
    )
    from lemoncrow.pro.capabilities.review.rules import content_access_for_path
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    if request.reusable:
        return {"overview": request.previous, "overview_reused": True, "overview_for": request.identity}, Usage()
    try:
        with bound_intelligence_policy(review_intelligence_policy(policy)):
            driver = resolve_driver_model(None, None).driver
            trust = process_route_trust(driver)
            rules = tuple(governance.content_access)
            packet = read_packet_json(captured.store, captured.revision)
            with tempfile.TemporaryDirectory(prefix="lemoncrow-overview-") as empty:
                outcome = overview_mod.build(
                    packet,
                    request,
                    calls_left=calls_cap - calls_used,
                    allowed=lambda path: content_access_for_path(path, rules, route_trust=trust).allowed,
                    execute=structured_executor or execute_structured,
                    redact=redact,
                    project_root=Path(empty) if driver in AGENT_CLI_DRIVERS else None,
                )
    except Exception as exc:  # decoration: whatever it was, the review goes on without it
        return {"overview_note": f"overview skipped: it could not be asked ({type(exc).__name__})"}, Usage()
    spent = Usage()
    if outcome.execution is not None:
        spent = call_usage(outcome.execution.model, outcome.execution.usage)
    return {
        "overview": outcome.overview,
        "overview_reused": outcome.reused,
        "overview_note": outcome.note,
        "overview_for": request.identity if outcome.settled else "",
    }, spent


def _execute(
    app: Any,
    captured: Captured,
    depth: str,
    previous: ReviewState | None,
    policy: Mapping[str, Any],
    governance: Any,
    structured_executor: Callable[..., StructuredExecution] | None,
    *,
    only_targets: Collection[str] | None = None,
    inputs: NoiseInputs | None = None,
    intent_loader: Callable[[Captured], IntentNote] | None = None,
    overview: overview_mod.Request | None = None,
) -> AiStage:
    from lemoncrow.pro.capabilities.review.ai_review import (
        changed_file_facts,
        general_review_targets,
        pr_candidates,
        run_independent_review,
    )
    from lemoncrow.pro.capabilities.review.independence import configured_reviewer_routes
    from lemoncrow.pro.capabilities.review.intelligence_policy import review_intelligence_policy
    from lemoncrow.pro.capabilities.review.intelligence_runtime import (
        ModelCallBudget,
        bound_intelligence_policy,
        execute_structured,
    )
    from lemoncrow.pro.capabilities.review.planned_execution import persisted_plan
    from lemoncrow.pro.capabilities.review.repo_context import RepoContextOptions, repo_context_enabled
    from lemoncrow.pro.capabilities.review.run_plan import ai_stage_plan
    from lemoncrow.pro.capabilities.review.static_findings import static_security_lines

    store, session, revision = captured.store, captured.session, captured.revision
    context = app.state.review_worker_ai_publication_context(session.id, revision.id)
    # Lines the security scan reported an error-level result on (recorded before this stage): they make a target a
    # ``security`` candidate, which ranks a large pull request, names a risk signal and is investigated.
    security_lines = static_security_lines(store, revision)
    # What the packet knows of each changed file (its kind, whether it is binary or deleted): which changed code is worth
    # a model call, and which keyword in a name is not a risk (a lockfile called ``package-lock.json``).
    facts = changed_file_facts(store, revision)
    # The noise controls, from the trusted base's .lemoncrow/review.yaml (never the pull request's copy). An ignored path
    # is out of the review: it is not a target, and an ai_denied content-access row keeps it from every prompt.
    noise = _noise_policy(context, governance, inputs)
    governance = with_ignored_paths(governance, noise, context.get("review_config"))
    reviewable: tuple[ReviewTarget, ...] = tuple(context["targets"])
    targets = tuple(target for target in reviewable if not noise.ignores(target.path))
    ignored = tuple(sorted({target.path for target in reviewable if noise.ignores(target.path)}))
    shadowed = sorted(
        {
            rule.stable_key
            for rule in governance.rules
            if rule.severity == "critical" and any(rule.applies_to(path) for path in ignored)
        }
    )
    if shadowed:
        # A rule the repository wrote is not checked on a file it also ignores: say so rather than leave it silent.
        shown = ", ".join(shadowed[:3]) + (f" and {len(shadowed) - 3} more" if len(shadowed) > 3 else "")
        noise = replace(
            noise,
            notes=(
                *noise.notes,
                f"review.ignore leaves out files in the scope of rule {shown}; it is not checked there",
            ),
        )
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
    digest = policy_digest(governance, stage_plan.effort, noise.rule_severity)
    reuse = previous is not None and previous.policy == digest and previous.depth == depth
    before = previous.reviewed_units if previous is not None and reuse else {}

    def unchanged(target: ReviewTarget) -> bool:
        token = units.get(target.target_id)
        return token is not None and before.get(token[0]) == token[1]

    narrow = only_targets is not None
    wanted = frozenset(only_targets or ())
    pending_review = tuple(target for target in targets if not unchanged(target))
    changed_all = tuple(target for target in targets if target.target_id in wanted) if narrow else pending_review
    # A change with more files than max_files: the highest-risk ones now, the rest pending (said in the summary). A
    # narrow run (chat's recheck) names its targets itself and is not cut, but it still says what is pending.
    cut: large_pr.LargePlan | None = None
    if noise.max_files and len({target.path for target in pending_review}) > noise.max_files:
        triage = context.get("triage")
        cut = large_pr.plan_large(
            pending_review,
            limit=noise.max_files,
            total_files=len({target.path for target in targets}),
            candidates=pr_candidates(
                pending_review, rules=tuple(governance.rules), security_lines=security_lines, facts=facts
            ),
            criticality=(lambda path: triage.classify(path).criticality) if triage is not None else None,
            order={target.target_id: index for index, target in enumerate(targets)},
        )
    changed = cut.kept if cut is not None and not narrow else changed_all
    deferred_ids = cut.deferred_ids if cut is not None and not narrow else frozenset()
    locations = {target.target_id: locate_target(target) for target in targets}
    intact = _intact_findings(captured, previous)
    rules = tuple(rule.stable_key for rule in governance.rules)
    # The change's impact and its risk signals, from the whole Snapshot and by fixed rules (no model, no model text).
    risk_candidates = pr_candidates(targets, rules=tuple(governance.rules), security_lines=security_lines, facts=facts)
    weighed = impact_mod.assess(targets, risk_candidates).to_state()
    rule_keys = {rule.ref: rule.stable_key for rule in governance.rules}

    def signals(
        asked: set[str], categories: Mapping[str, set[str]], kept: Mapping[str, str]
    ) -> tuple[impact_mod.Signal, ...]:
        reviewed = {
            target.target_id
            for target in targets
            if target.target_id in units and kept.get(units[target.target_id][0]) == units[target.target_id][1]
        }
        return impact_mod.signals_for(
            targets, risk_candidates, changed=asked, asked=categories, reviewed=reviewed, rule_keys=rule_keys
        )

    # What the trusted base's import.rules_files and AGENTS.md / CLAUDE.md contributed (G10).
    imports = governance.imports
    base = AiStage(
        status="nothing_changed",
        locations=locations,
        spans={t.target_id: tuple((span.side, span.start_line, span.end_line) for span in t.spans) for t in targets},
        target_ids=tuple(target.target_id for target in targets),
        unchanged=len(targets) - len(changed_all),
        files=tuple(sorted({target.path for target in targets})),
        rules=rules,
        imported=imports.receipt_lines() if imports is not None else (),
        policy=previous.policy if narrow and previous is not None else digest,
        depth=depth,
        max_calls=stage_plan.max_review_calls or ModelCallBudget.from_env(max_calls=1).max_review_calls,
        impact=weighed,
        noise=noise,
        ignored=ignored,
        total_files=len({target.path for target in targets}),
        deferred_files=cut.deferred_paths if cut is not None else (),
    )
    if not changed_all:
        kept, truncated = cap_units(_tokens(targets, units, pending=()))
        if narrow and previous is not None:
            kept, truncated = dict(previous.reviewed_units), previous.units_truncated
        # No investigator ran, but a push that changed only what no review looks at still gets an overview of itself.
        unchanged_fields, spent = _overview(
            overview, captured, governance, policy, structured_executor, calls_used=0, calls_cap=base.max_calls
        )
        return replace(
            base,
            reviewed_units=kept,
            units_truncated=truncated,
            model=previous.model if previous else "",
            intact=intact,
            signals=signals(set(), {}, kept),
            usage=spent,
            **unchanged_fields,
        )

    budget = ModelCallBudget.from_env(max_calls=1)
    examined = _Examined(structured_executor or execute_structured)
    candidate_ids = {
        target_id
        for candidate in pr_candidates(
            changed, rules=tuple(governance.rules), security_lines=security_lines, facts=facts
        )
        for target_id in candidate.target_ids
    }
    # A model is about to be asked when the change carries a risk signal or any other changed code worth a look.
    asking = bool(candidate_ids) or bool(general_review_targets(changed, facts, exclude=candidate_ids))
    # What the author says the change is for: read only when a model is about to be asked (each linked issue is a GET),
    # recorded on the Snapshot as bound intent, and shown to the investigators as small, cleaned, untrusted data.
    note = intent_loader(captured) if intent_loader is not None and asking else None
    intent_block = pr_intent.prompt_block(store, session.id) if pr_intent.enabled() and asking else None
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
            # A model-API route has no tools; this gives its investigator the callers, definitions, tests and
            # config around what changed. An agent-CLI route reads the repository itself and gets none.
            repo_context=RepoContextOptions(enabled=repo_context_enabled()) if repo_context_enabled() else None,
            pr_fields=True,
            prior_findings=_prior_rows(previous, {target.target_id for target in changed}),
            project_conventions=imports.conventions if imports is not None else (),
            pr_intent=intent_block,
            # A file the security scan reported an error-level result in is investigated as a security candidate.
            static_security=True,
        )
    usage, served = revision_usage(store, session.id, revision.id)
    context = dict(result.repo_context)
    context_tokens = context.get("estimated_tokens")
    if isinstance(context_tokens, int) and context_tokens > 0:
        usage = replace(usage, context_tokens=context_tokens)
    # The overview comes last and only after a review that met no model failure: a provider that is down is not asked
    # for one more thing. Its call is one of the review's, so it is counted in its usage and against its cap.
    described: dict[str, Any] = {}
    if not result.provider_errors and not result.skipped_by_budget:
        described, spent = _overview(
            overview,
            captured,
            governance,
            policy,
            structured_executor,
            calls_used=max(usage.calls, result.calls_used),
            calls_cap=result.calls_max or base.max_calls,
        )
        usage = usage.plus(spent)
    # Reviewed means a model call about the target returned (or there was nothing to look at in it: documentation, a
    # generated file, a formatting-only change). A target no call reached -- a failed call, a spent budget, the hard cap
    # on model calls -- is pending: it is retried on the next push and competes for the cap again, so a large pull
    # request is covered over its pushes, never silently skipped.
    complete = result.status in {"completed", "skipped_unnecessary"} and not result.provider_errors
    complete = complete and not result.skipped_by_budget
    # A deferred file was not looked at at all: pending like a risk signal no call reached.
    other_ids = set(result.general_target_ids) - candidate_ids
    pending = ((candidate_ids | other_ids) - examined.targets) | deferred_ids
    why = ""
    if result.skipped_by_budget:
        why = "the model-call budget ran out"
    kept, truncated = cap_units(_tokens(targets, units, pending=pending))
    if narrow and previous is not None:
        # Only the targets that were looked at again are newly reviewed; the rest stay as the last review left them.
        looked_at = {units[t.target_id][0] for t in changed if t.target_id in units and t.target_id not in pending}
        kept = {**previous.reviewed_units, **{key: value for key, value in kept.items() if key in looked_at}}
        truncated = previous.units_truncated
    changed_ids = tuple(target.target_id for target in changed)
    return replace(
        base,
        status="ran",
        reason=why,
        findings=tuple(result.machine_findings),
        # Deferred targets count as asked-about but not covered: a finding on one is carried, marked unverified.
        rereviewed=tuple(target.target_id for target in changed_all),
        covered=tuple(target_id for target_id in changed_ids if target_id not in pending),
        reviewed_units=kept,
        units_truncated=truncated,
        changed_lines=sum(target.additions + target.deletions for target in changed),
        changed_files=len({target.path for target in changed}),
        candidates=len(candidate_ids),
        investigated=len(candidate_ids & examined.targets),
        general=len(other_ids),
        general_investigated=len(other_ids & examined.targets),
        usage=usage,
        model=served or (f"{result.driver}/{result.model}" if result.driver else ""),
        max_calls=result.calls_max,
        complete=complete,
        errors=tuple(result.provider_errors),
        repo_context=context,
        examined=tuple(sorted(examined.targets)),
        answers=dict(result.prior_answers),
        intact=intact,
        signals=signals(set(changed_ids), examined.categories, kept),
        intent=note,
        **described,
    )
