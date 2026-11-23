"""Part proof routes: one batched read per Snapshot, and the bounded automatic run.

``GET  /reviews/{id}/part-proof``       the Snapshot's outcomes (default plus exceptions), one read
``POST /reviews/{id}/part-proof/run``   run the tests that reach the changed parts, in view first, in the sandbox

The run executes the trusted-base test recipes through the same planned, metered, sandboxed
path as a reviewer's click (:func:`check_api.execute_check`) with the base run beside the head,
so a test that fails on the base and passes on the head can be called proven. It never calls a
model, never runs unsandboxed, never amends the run plan (an entry the plan does not schedule is
"couldn't run: not planned", not forced) and stops at the review's metered minutes, or at the
proposed per-review default when the review sets none.

It starts two ways. A local capture schedules it in the background (:func:`schedule_auto`, the same
hop the pre-merge checks use), and the Reader starts it when it opens a Snapshot whose record is
absent or incomplete, with the parts in view first. Both go through :func:`run_auto`, which holds
one run per Snapshot per process.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status

from lemoncrow.pro.capabilities.review import part_proof as pp
from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.check_api import (
    CheckRecipeDeps,
    execute_check,
    fresh_budget_refusal,
)
from lemoncrow.pro.capabilities.review.differential import resolve_test_file
from lemoncrow.pro.capabilities.review.run_plan import PROPOSED_BUDGET_DEFAULTS
from lemoncrow.pro.capabilities.review.test_impact import (
    ChangedReach,
    RecipeSelection,
    changed_reach,
    select_recipe_tests,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from lemoncrow.pro.capabilities.review.check_runs import CheckRunReport

_LOG = logging.getLogger(__name__)
_MAX_IN_VIEW = 200
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()
#: Capture-time runs in this process at once; the rest wait for a slot.
_BACKGROUND_SLOTS = threading.BoundedSemaphore(2)
#: Snapshots scheduled or running from a capture, so a repeat capture does not queue a second run.
_SCHEDULED: set[str] = set()
#: Capture-time runs queued or running at once (the two slots plus those waiting for one); past it a schedule is dropped.
_MAX_SCHEDULED = 8
#: A reach input bundle: ``(impact map, why unavailable, changes, changed-file list complete)``.
_ReachInputs = tuple[Any, str, Any, bool]


def _run_on(deps: CheckRecipeDeps) -> str:
    return str(getattr(deps.runners(), "run_on", "") or "local")


def _hosted(deps: CheckRecipeDeps) -> bool:
    """The one hosted predicate: the store is the authority and holds no source tree, so nothing here may run.

    The store, not the runner locus (:func:`_run_on` says "local" on a hosted server that has no worker), decides:
    every read fallback, run and record write below asks this, so a hosted review is only ever written by its CI
    ingest (:mod:`part_proof_ci`) and can never be started or overwritten from a request.
    """

    return not deps.store.hosted_attachment_supported()


def _fallback(deps: CheckRecipeDeps, revision: Any, *, current: bool) -> tuple[str, str]:
    """What a part shows before any record exists: ``(outcome, reason code)``."""

    if _hosted(deps):
        # A hosted store runs no tests and builds no import graph. A part with no CI-reported result is not known
        # to be untested, so it reads "couldn't run", never "not tested" (see part_proof_ci).
        return pp.COULDNT_RUN, "no_ci_result"
    _recipes, why = pp.eligibility(deps.run_plans.recipe_catalog(revision), run_on=_run_on(deps))
    if why:
        return pp.COULDNT_RUN, why
    if not current:
        return pp.COULDNT_RUN, "historical"
    return pp.NOT_RUN_YET, ""


def read_projection(deps: CheckRecipeDeps, revision: Any, *, current: bool) -> dict[str, Any]:
    record = pp.read_records(deps.store, [revision]).get(str(revision.id))
    with _RUNNING_LOCK:
        running = str(revision.id) in _RUNNING
    if record is not None:
        return pp.projection(revision, record, fallback=pp.NOT_TESTED, running=running)
    outcome, reason = _fallback(deps, revision, current=current)
    return pp.projection(revision, None, fallback=outcome, fallback_reason=reason, running=running)


def _declared(recipe: Any) -> set[str]:
    """The paths the recipe itself declares as inputs: a change to one reaches every test."""

    return set(recipe.inputs) | ({recipe.definition} if recipe.definition else set())


def _reach_of(inputs: _ReachInputs, revision: Any, recipe: Any) -> ChangedReach:
    impact_map, unavailable, changes, complete = inputs
    return changed_reach(
        impact_map,
        changes,
        cwd=recipe.cwd,
        base_sha=str(getattr(revision, "base_sha", "") or ""),
        unavailable=unavailable,
        complete=complete,
        declared=_declared(recipe),
    )


def _selected(differential: Any, reach: Any) -> list[str]:
    """The test files a recipe run executed.

    An impact run lists them, untruncated when the run still holds them
    (``RecipeDifferential.selected_files``). Any other run (a reviewer's mapped test, a whole-suite
    run) is credited only with the test files its own report names, never with every test of the
    repo: a command that ran one file says nothing about the others.
    """

    held = tuple(getattr(differential, "selected_files", ()) or ())
    if held:
        return list(held)
    selection = differential.economy.get("selection") if isinstance(differential.economy, Mapping) else None
    named = {
        resolve_test_file(str(unit.subject or ""), str(unit.file or ""), differential.recipe, reach.all_tests)
        for unit in differential.tests
    } - {""}
    if isinstance(selection, Mapping) and selection.get("mode") == "impact":
        listed = [str(item) for item in selection.get("tests") or ()]
        # A carried run keeps only the first 200 selected files; the rest are credited by name alone.
        return sorted({*listed, *named}) if int(selection.get("selected") or 0) > len(listed) else listed
    return sorted(named)


def _outline(reaches: Sequence[tuple[Any, ChangedReach]], ran: set[str]) -> tuple[dict[str, pp.PartOutcome], str, str]:
    """What is known before any test runs: ``(files, default, default reason)``.

    Every part some test reaches and that test has not run yet is ``not_run_yet`` (so it never
    reads "not tested" while tests are still to run), every part reach could not decide or that was
    deleted says so, and a recipe whose reach is wholly undecided sets the default. Waiting rows
    never replace a recorded fact.
    """

    files: dict[str, pp.PartOutcome] = {}
    default, default_reason = pp.NOT_TESTED, ""
    for _recipe, reach in reaches:
        why = pp.undecided_reason(reach)
        if why:
            default, default_reason = pp.COULDNT_RUN, why
        for path, row in pp.derive_batch(reach, pp.TestEvidence(), ()).items():
            if row.outcome == pp.NOT_RUN_YET and all(item in ran for item in reach.tests.get(path, ())):
                continue
            files[path] = pp.stronger(files.get(path), row) or row
    return files, default, default_reason


def _decided(
    differentials: Sequence[Any], reaches: Mapping[str, ChangedReach]
) -> tuple[dict[str, pp.PartOutcome], set[str]]:
    """``(files, ran tests)`` from the test recipes of one check run."""

    files: dict[str, pp.PartOutcome] = {}
    ran: set[str] = set()
    for differential in differentials:
        reach = reaches.get(differential.recipe.id)
        if reach is None or pp.undecided_reason(reach):
            continue
        selected = _selected(differential, reach)
        evidence = pp.evidence_from_differential(differential, selected, cwd=differential.recipe.cwd)
        ran |= evidence.ran
        for path, row in pp.derive_batch(reach, evidence, selected).items():
            if row.outcome != pp.NOT_RUN_YET:
                files[path] = pp.stronger(files.get(path), row) or row
    return files, ran


def record_check_run(
    deps: CheckRecipeDeps, session: Any, revision: Any, report: CheckRunReport, *, part_unit_key: str = ""
) -> dict[str, Any] | None:
    """Store what a check run says about its parts: the same record the automatic run writes.

    A reviewer's fresh "Run test" lands here (``part_unit_key`` set) and so does any other run of a
    test recipe, so the three surfaces read one record in one vocabulary. Reach is computed for every
    configured test recipe, not only the one that ran, so a part another recipe's tests reach waits as
    ``not_run_yet`` and the record stays incomplete until a run decides it. ``None`` when the run
    executed no test recipe, or when part proof does not apply to this review (hosted, switched off).
    """

    tests = [item for item in report.differentials if item.recipe.kind == "test"]
    if _hosted(deps) or not tests or str(report.revision_id) != str(revision.id):
        return None
    catalog = deps.run_plans.recipe_catalog(revision)
    # Hosted reviews (above: their record is the CI's, never a local write) and repos that switched part proof off keep
    # no record here. The other reasons (no declared recipe, no sandbox) still record: a reviewer ran a test, and
    # the record says what happened.
    if pp.eligibility(catalog, run_on=_run_on(deps), sandbox="reviewer-run")[1] in {"hosted", "disabled"}:
        return None
    inputs = deps.run_plans.reach_inputs(revision)
    recipes = [item for item in catalog.configured() if item.kind == "test"]
    for item in tests:
        if all(item.recipe.id != known.id for known in recipes):
            recipes.append(item.recipe)
    reaches = {recipe.id: _reach_of(inputs, revision, recipe) for recipe in recipes}
    files, ran = _decided(tests, reaches)
    seconds = sum(float(getattr(item, "duration_ms", 0) or 0) for diff in tests for item in diff.attempts)

    def apply(old: pp.ProofRecord | None) -> pp.ProofRecord:
        done = {*(old.ran_tests if old is not None else ()), *ran}
        outline, default, reason = _outline([(recipe, reaches[recipe.id]) for recipe in recipes], done)
        record = pp.merged(old, outline, revision=revision, default=default, default_reason=reason)
        record = pp.merged(
            record,
            files,
            revision=revision,
            ran=ran,
            source="run_test" if part_unit_key else "check_run",
            seconds=seconds / 1000.0,
            replace=True,
        )
        return replace(record, complete=not any(row.outcome == pp.NOT_RUN_YET for row in record.files.values()))

    pp.update_record(deps.store, revision, apply)
    return {"recorded": True, "parts": len(files)}


def run_auto(
    deps: CheckRecipeDeps, session: Any, revision: Any, in_view: list[str], *, current: bool
) -> dict[str, Any]:
    """Run the tests that reach the changed parts, in view first, within the review's minutes."""

    if _hosted(deps):
        return read_projection(deps, revision, current=current)
    recipes, why = pp.eligibility(deps.run_plans.recipe_catalog(revision), run_on=_run_on(deps))
    if why or not current or session.status == "archived":
        return read_projection(deps, revision, current=current)
    key = str(revision.id)
    with _RUNNING_LOCK:
        busy = key in _RUNNING
        if not busy:
            _RUNNING.add(key)
    if busy:
        # Read outside the lock: read_projection takes it too, and it is not reentrant.
        return {**read_projection(deps, revision, current=current), "running": True}
    try:
        # A complete record is the end of this Snapshot's plan: a second trigger (a second capture, a
        # reader that raced the capture run) neither rewrites it as incomplete nor spends the next
        # batch. Only an incomplete record, one that stopped between batches, is resumed.
        record = pp.read_records(deps.store, [revision]).get(key)
        if record is None or not record.complete:
            _run_batches(deps, session, revision, recipes, in_view)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(key)
    return read_projection(deps, revision, current=True)


def schedule_auto(deps: CheckRecipeDeps, session: Any, revision: Any) -> dict[str, Any]:
    """The local capture hop: start the automatic run in the background, never blocking the capture.

    Only where :func:`part_proof.eligibility` allows it (local, a sandbox, a declared test recipe, not
    switched off in ``check_policy.part_proof``) and only for a Snapshot that has no complete record.
    Never on a hosted review, never from a read.
    """

    if _hosted(deps):
        return {"status": "not_eligible", "reason": "hosted", "revision_id": str(revision.id)}
    recipes, why = pp.eligibility(deps.run_plans.recipe_catalog(revision), run_on=_run_on(deps))
    if why:
        return {"status": "not_eligible", "reason": why, "revision_id": str(revision.id)}
    if session.status != "open":
        return {"status": "not_eligible", "reason": "closed", "revision_id": str(revision.id)}
    record = pp.read_records(deps.store, [revision]).get(str(revision.id))
    if record is not None and record.complete:
        return {"status": "done", "revision_id": str(revision.id)}
    key = str(revision.id)
    with _RUNNING_LOCK:
        if key in _RUNNING or key in _SCHEDULED:
            return {"status": "running", "revision_id": key}
        if len(_SCHEDULED) >= _MAX_SCHEDULED:
            _LOG.warning(
                "part proof on capture dropped for Review %s: %d runs already queued", session.id, len(_SCHEDULED)
            )
            return {"status": "dropped", "reason": "queue_full", "revision_id": key}
        # Marked before the thread exists, so a second capture of this Snapshot cannot queue a second one.
        _SCHEDULED.add(key)

    def job() -> None:
        try:
            with _BACKGROUND_SLOTS:
                latest = deps.latest(session)
                if latest.id == revision.id:
                    run_auto(deps, session, revision, [], current=True)
        except Exception:
            _LOG.warning("part proof on capture failed for Review %s", session.id, exc_info=True)
        finally:
            with _RUNNING_LOCK:
                _SCHEDULED.discard(key)

    try:
        # lc-debt: a daemon thread dies with the process, and nothing in the sandbox runner registers
        # its in-flight child (no atexit or shutdown hook exists to terminate it), so a shutdown mid-batch can
        # leave that child to finish or hit its own timeout; the record is incomplete and resumes correctly.
        # Upgrade: a child registry in sandbox_limits.run_limited_bounded that an atexit hook kills.
        threading.Thread(target=job, name=f"part-proof-{key[:8]}", daemon=True).start()
    except Exception:
        with _RUNNING_LOCK:
            _SCHEDULED.discard(key)
        raise
    return {"status": "queued", "revision_id": key, "recipes": len(recipes)}


#: Reason codes of a batch whose tests never started.
_NOT_RUN = frozenset({"no_sandbox", "error"})
#: Automatic attempts at one test file that never really started: the plain run, then one fresh retry.
_MAX_TRIES = 2


def _folding(
    revision: Any,
    rows: Mapping[str, pp.PartOutcome],
    done: Sequence[str],
    seconds: float,
    tries: Mapping[str, int] | None = None,
) -> Callable[[pp.ProofRecord | None], pp.ProofRecord]:
    def fold(old: pp.ProofRecord | None) -> pp.ProofRecord:
        return pp.merged(old, rows, revision=revision, ran=done, seconds=seconds, tries=tries)

    return fold


def _run_batches(deps: CheckRecipeDeps, session: Any, revision: Any, recipes: list[Any], in_view: list[str]) -> None:
    inputs = deps.run_plans.reach_inputs(revision)
    reaches = [(recipe, _reach_of(inputs, revision, recipe)) for recipe in recipes]
    existing = pp.read_records(deps.store, [revision]).get(str(revision.id))
    ran = set(existing.ran_tests) if existing is not None else set()
    tries = dict(existing.tries) if existing is not None else {}
    # Tests whose automatic attempts never really started, twice over, are not tried again: the record
    # ends with their error rows and a reviewer's Run test is the way to try them once more.
    given_up = {item for item, count in tries.items() if count >= _MAX_TRIES}
    # Before any test runs, every part a test reaches waits as "not run yet" and the record is
    # incomplete: a part never reads "not tested" while a later batch or recipe still owes it a
    # result, and an interrupted run leaves a record the next visit resumes.
    outline, default, reason = _outline(reaches, ran)
    pp.update_record(
        deps.store,
        revision,
        lambda old: replace(
            pp.merged(old, outline, revision=revision, default=default, default_reason=reason), complete=False
        ),
    )
    plans = [
        (recipe, reach, pp.plan_batches(reach, in_view, ran | given_up))
        for recipe, reach in reaches
        if not pp.undecided_reason(reach)
    ]
    pending = pp.NOT_RUN_YET
    planned = {item for _recipe, _reach, batches in plans for slot_tests in batches for item in slot_tests}
    stopped = False  # the Review advanced to a newer Snapshot: nothing more is spent on this one
    retry = False  # a batch never started: the record stays incomplete and the next visit retries it
    # Every recipe's parts in view run before any recipe's remaining parts.
    for slot in (0, 1):
        for recipe, reach, batches in plans:
            batch = tuple(item for item in batches[slot] if item not in ran)
            if not batch or pending == pp.OVER_BUDGET or stopped:
                continue
            if deps.latest(session).id != revision.id:
                stopped = True
                continue
            # A batch that already failed to start once must not replay its stored error: it runs fresh.
            retrying = any(tries.get(item, 0) > 0 for item in batch)
            files, seconds, state = _run_one(deps, session, revision, recipe, reach, batch, inputs, fresh=retrying)
            if state == "superseded":
                stopped = True
                continue
            if state == "over_budget":
                pending = pp.OVER_BUDGET
            # Tests that never really ran (a refused or failed start) are not credited: a resume retries them.
            credited = (
                batch
                if state == "ok"
                and any(not (row.outcome == pp.COULDNT_RUN and row.reason in _NOT_RUN) for row in files.values())
                else ()
            )
            counted: dict[str, int] = {}
            if state in {"error", "ok"} and not credited:
                counted = {item: tries.get(item, 0) + 1 for item in batch}
                tries.update(counted)
                retry = retry or any(count < _MAX_TRIES for count in counted.values())
            pp.update_record(deps.store, revision, _folding(revision, files, credited, seconds, counted))
            ran |= set(credited)
    # Parts whose reaching tests did not run say why. Tests a batch planned but did not run wait ("not
    # run yet", or "over budget" when the minutes ran out); tests past both batch bounds are not retried
    # automatically, so on a finished record they say that plainly instead of promising a later run.
    finished = not stopped and not retry
    waiting: dict[str, pp.PartOutcome] = {}
    for _recipe, reach in reaches:
        for path, tests in reach.tests.items():
            unrun = [item for item in tests if item not in ran and tries.get(item, 0) < _MAX_TRIES]
            if not unrun:
                continue
            past_bound = finished and pending == pp.NOT_RUN_YET and all(item not in planned for item in unrun)
            waiting[path] = (
                pp.PartOutcome(pp.COULDNT_RUN, "past_bound", reaching=len(tests))
                if past_bound
                else pp.PartOutcome(pending, reaching=len(tests))
            )
    pp.update_record(
        deps.store,
        revision,
        lambda old: replace(pp.merged(old, waiting, revision=revision), complete=finished),
    )


def _over_bound(preview: Any, entry_id: str, live: Any) -> bool:
    """The Review's metered minutes are spent: its own ceilings, else the proposed per-review default.

    "Minutes" are all of the Review's metered minutes (``usage().review_minutes``: every check run on
    every Snapshot, not only part proof), and the next batch counts at its entry's estimate, the way
    :func:`fresh_budget_refusal` counts a fresh run, so a batch that starts just under the bound is refused.
    """

    if fresh_budget_refusal(preview, entry_id, live):
        return True
    ceilings = preview.ceilings if isinstance(preview.ceilings, Mapping) else {}
    if ceilings.get("per_review_minutes") is not None:
        return False
    spent = getattr(live, "review_minutes", None)
    entry = preview.entry(entry_id)
    estimate = float(getattr(entry, "estimated_minutes", 0.0) or 0.0)
    # lc-debt: the default bound is the proposed per_review_minutes, pending the owner's budget decision.
    return spent is not None and float(spent) + estimate >= float(PROPOSED_BUDGET_DEFAULTS["per_review_minutes"])


def _run_one(
    deps: CheckRecipeDeps,
    session: Any,
    revision: Any,
    recipe: Any,
    reach: Any,
    batch: tuple[str, ...],
    inputs: _ReachInputs,
    *,
    fresh: bool = False,
) -> tuple[dict[str, pp.PartOutcome], float, str]:
    """Run one batch of one recipe: ``(decided parts, wall seconds, "ok" | "over_budget" | "not_planned" | "superseded" | "error")``."""

    run_plans = deps.run_plans
    live = run_plans.usage(session, revision)
    preview = run_plans.current(
        session, revision, ai_candidates=lambda: deps.ai_candidates(session, revision), persist=True
    )
    if _over_bound(preview, recipe.entry_id, live):
        return {}, 0.0, "over_budget"
    impact_map, unavailable, changes, complete = inputs
    chosen = select_recipe_tests(
        impact_map,
        recipe,
        changes,
        base_sha=str(getattr(revision, "base_sha", "") or ""),
        unavailable=unavailable,
        complete=complete,
        executed=batch,
    )
    # With a decidable change the selection carries the input digest of this batch's closure, so an
    # unchanged closure carries the earlier run forward instead of running again.
    selection = (
        chosen
        if chosen.mode == "impact" and tuple(chosen.tests) == tuple(dict.fromkeys(batch))
        else RecipeSelection(
            recipe.id,
            "impact",
            f"part proof: {len(batch)} test files reach the parts in view first",
            tests=batch,
            total=len(reach.all_tests),
        )
    )
    started = time.monotonic()

    def failed(state: str, code: str) -> tuple[dict[str, pp.PartOutcome], float, str]:
        rows = {
            path: pp.PartOutcome(
                pp.OVER_BUDGET if state == "over_budget" else pp.COULDNT_RUN, code, reaching=len(tests)
            )
            for path, tests in reach.tests.items()
            if any(item in batch for item in tests)
        }
        return rows, time.monotonic() - started, state

    try:
        _wire, report = execute_check(
            deps,
            session,
            revision,
            current=True,
            entry_id=recipe.entry_id,
            # Not a reviewer's pick: the plan decides whether this entry runs, and it is never amended.
            pick=False,
            # A retry is its own generation (nothing replayed), but never ``fresh=True``: that would amend
            # the plan with a reviewer's pick. The bound above already counted this batch's estimate.
            fresh=False,
            fresh_run_id=uuid.uuid4().hex if fresh else "",
            from_revision_id="",
            selection_override={recipe.id: selection},
            both_sides=True,
        )
    except HTTPException as exc:
        text = str(exc.detail)
        if exc.status_code == status.HTTP_409_CONFLICT and "advanced" in text:
            # A newer Snapshot arrived mid-run: nothing was published for this one, and this is not a failure.
            return {}, time.monotonic() - started, "superseded"
        if "budget" in text.lower() or "does not fit" in text:
            return failed("over_budget", "")
        if "not in the effective run plan" in text:
            return failed("not_planned", "not_planned")
        return failed("error", "error")
    tests = [item for item in report.differentials if item.recipe.kind == "test" and item.recipe.id == recipe.id]
    files = _decided_for(tests, reach, batch)
    return files, time.monotonic() - started, "ok"


def _decided_for(differentials: list[Any], reach: Any, batch: tuple[str, ...]) -> dict[str, pp.PartOutcome]:
    files: dict[str, pp.PartOutcome] = {}
    for differential in differentials:
        evidence = pp.evidence_from_differential(differential, batch, cwd=differential.recipe.cwd)
        # Only the parts this batch decides; the others are decided by their own batches.
        for path, row in pp.derive_batch(reach, evidence, batch).items():
            if row.outcome != pp.NOT_RUN_YET:
                files[path] = row
    return files


def register_part_proof_routes(router: APIRouter, deps: CheckRecipeDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    @router.get("/reviews/{review_id}/part-proof")
    async def _get_part_proof(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """One Snapshot's per-part outcomes: a default plus the exceptions, one read (never one per file)."""

        session = deps.session(review_id)
        latest = deps.latest(session)
        revision = deps.requested_revision(session, revision_id) if revision_id else latest

        def read() -> dict[str, Any]:
            with request_reads.reads_once():
                return read_projection(deps, revision, current=revision.id == latest.id)

        return await run_in_threadpool(read)

    @router.post("/reviews/{review_id}/part-proof/run")
    async def _post_part_proof_run(review_id: str, request: Request) -> dict[str, Any]:
        """Run the tests that reach this Snapshot's changed parts (the parts in view first), sandboxed and metered."""

        session = deps.mutable_session(review_id)
        body = await deps.json_object(request)
        requested = str(body.get("revision_id") or "").strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="part proof runs on an exact revision_id"
            )
        revision = deps.requested_revision(session, requested)
        raw = body.get("in_view")
        in_view = (
            [str(item) for item in raw[:_MAX_IN_VIEW] if isinstance(item, str) and item]
            if isinstance(raw, list)
            else []
        )
        current = revision.id == deps.latest(session).id

        def run() -> dict[str, Any]:
            return run_auto(deps, session, revision, in_view, current=current)

        return await run_in_threadpool(run)
