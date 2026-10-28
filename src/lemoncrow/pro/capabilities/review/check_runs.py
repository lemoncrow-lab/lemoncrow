"""Planned, sandboxed execution of trusted-base check recipes (Phase 9R items 2, 6, 7, 27).

One call executes the recipes of one admitted run-plan entry on the exact
Snapshot through the R4 runner registry (local Bubblewrap, customer worker or
managed sandbox), and publishes:

* one raw attempt record per side, rerun and probe (runtime-validation
  records of scenario kind ``recipe``; never verification results);
* one differential record per recipe (``differential.py``), the input of the
  ``test_outcome`` behavior producer;
* typed ``lemoncrow_verification`` verdict rows per recipe
  (``checks.recipe:<id>``) and per kind entry (``checks.test`` ...), carrying
  the executor class and environment fingerprint of the run;
* formatter/pre-commit autofix output as change proposals (never applied).

Everything publishes against ``expected_current_revision_id``: a Snapshot
that advanced during the run refuses publication (:class:`CheckRunSuperseded`).
Nothing here decides *whether* to run; callers admit the plan entry first.

Execution economy (R6): the head runs first; a base side planned only on
demand runs when the head fails. A test recipe runs the tests its plan entry
selected (``{tests}``). Before executing, a base run is taken from a verified
shared base result (``base_results``) and a head run from an earlier Snapshot
with the same inputs (``incremental``); both are copied onto this Snapshot
with their provenance, and the compute they avoided is recorded.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.base_results import SharedBaseKey, find_shared_base, publish_shared_base
from lemoncrow.pro.capabilities.review.check_ingest import EquivalenceKey, tool_identity
from lemoncrow.pro.capabilities.review.differential import (
    CHECK_DIFFERENTIAL_MEDIA_TYPE,
    AgentClaim,
    AttemptOutcome,
    RecipeDifferential,
    TestCase,
    TestReport,
    assess_adequacy,
    classify_recipe,
    combine_verdicts,
    diff_test_file,
    is_test_path,
    parse_report,
    reconcile_claims,
)
from lemoncrow.pro.capabilities.review.execution import ExecutionResult, ExecutorClass
from lemoncrow.pro.capabilities.review.incremental import (
    CarryDecision,
    CarryKey,
    carry_enabled,
    explain_carry,
    record_carry,
    record_environment,
)
from lemoncrow.pro.capabilities.review.recipes import (
    KIND_ENTRY,
    RECIPE_ENTRY_PREFIX,
    TEST_DIFF_ENTRY,
    CheckRecipe,
    RecipeCatalog,
    entry_recipe_digest,
    head_definition_guard,
)
from lemoncrow.pro.capabilities.review.run_evidence import protect_text
from lemoncrow.pro.capabilities.review.runtime_validation import (
    RUNTIME_VALIDATION_MEDIA_TYPE,
    ValidationRequest,
    ValidationScenario,
    persist_validation_record,
    snapshot_semantic_identity,
    stamp_execution_record,
    validation_source_ref,
)
from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult, WorkspaceOverlay
from lemoncrow.pro.capabilities.review.session_models import ReviewChangeProposal, ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.store import DerivedResult, new_evidence_id
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.capabilities.review.test_impact import RecipeSelection
from lemoncrow.pro.capabilities.review.validity import carry_result
from lemoncrow.pro.capabilities.review.validity import result as validity_result

Side = Literal["old", "new"]
Variant = Literal["run", "probe", "fix"]
RunSide = Callable[[RuntimeConfigEntry, ReviewSurface, str], SurfaceRunResult]
"""Executes one synthetic recipe surface on one side through the runner registry."""
Meter = Callable[[Mapping[str, Any] | None, float], None]
"""Records one execution's compute against the admitted entry."""
Saved = Callable[[str, float], None]
"""Records compute a reused run avoided: ``(reason, seconds)`` (``economy.SavingsLedger``)."""

RECIPE_PROVIDER: Final[str] = "recipe"
CHECK_ENTRIES: Final[tuple[str, ...]] = (*KIND_ENTRY.values(), TEST_DIFF_ENTRY)
_SOURCE: Final = "test"
_MAX_TEST_ROWS: Final[int] = 20_000
_MAX_OVERLAY_FILES: Final[int] = 64
#: Derived-row kind naming a Snapshot's newest fresh run of one recipe (its "generation").
#: One upserted row per (Snapshot, recipe identity); it cascades with the Snapshot like carry rows.
FRESH_GENERATION_KIND: Final = "check_fresh_generation"
_FRESH_GENERATION_STAMP: Final = "v1"
#: The source_ref suffix of a plain head run that executed on a Snapshot which
#: already holds a carried copy that may no longer be replayed (carry turned
#: off, or the executor environment changed or is unknown): it sits beside the
#: copy and supersedes it on that Snapshot.
_UNCARRIED_REF: Final = "uncarried"


class CheckRunSuperseded(ValueError):
    """The Snapshot advanced during the run; nothing more is published as current."""


def is_check_entry(entry_id: str) -> bool:
    return entry_id in CHECK_ENTRIES or entry_id.startswith(RECIPE_ENTRY_PREFIX)


def _hex(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckRunInputs:
    store: Any
    revision: ReviewRevision
    catalog: RecipeCatalog
    entry_id: str
    sides: tuple[Side, ...]
    executor_class: ExecutorClass
    policy_digest: str
    """sha256 hex of the trusted-base review config that defined the recipes."""
    changed_paths: tuple[str, ...] = ()
    read_base: Callable[[str], str | None] = lambda _path: None
    read_head: Callable[[str], str | None] = lambda _path: None
    head_identity: str = ""
    claims: tuple[AgentClaim, ...] = ()
    rerun_budget_seconds: float | None = None
    expected_current_revision_id: str = ""
    execution_intent: str = ""
    created_by: str = "lemoncrow"
    #: R6 item 19: recipe id -> the plan entry's test selection (absent: the recipe runs in full).
    selection: Mapping[str, RecipeSelection] = field(default_factory=dict)
    #: R6 item 20: the plan placed the base side on demand (runs only when the head fails).
    base_on_demand: bool = False
    #: R6: records compute a shared or carried-forward run avoided.
    saved: Saved | None = None
    #: RC12-B: an explicit fresh run (its id). Nothing is replayed, carried forward
    #: or taken from a shared base: every attempt executes, and its head run
    #: becomes this Snapshot's carry source for later Snapshots.
    fresh_run_id: str = ""
    #: Trust T3 item 1: the executor environment a run of a recipe's command will
    #: record, probed before dispatch (the runner registry's ``environment_probe``,
    #: given the command). ``None`` or ``""`` means unknown, so no head run is
    #: carried from an earlier Snapshot.
    environment: Callable[[Sequence[str]], str] | None = None


@dataclass(slots=True)
class CheckRunReport:
    entry_id: str
    revision_id: str
    differentials: list[RecipeDifferential] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)
    verdict_evidence: dict[str, str] = field(default_factory=dict)
    differential_evidence: dict[str, str] = field(default_factory=dict)
    proposals: list[str] = field(default_factory=list)
    executions: int = 0
    reused: int = 0
    notes: list[str] = field(default_factory=list)
    shared_base: int = 0
    carried: int = 0
    saved_seconds: float = 0.0
    #: Trust T1: per plain head attempt (``check:<recipe>#<attempt>``), why it was
    #: carried from an earlier Snapshot or runs again (a ``ValidityResult`` wire).
    validity: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "revision_id": self.revision_id,
            "recipes": [
                {
                    **item.to_wire(),
                    "evidence_id": self.differential_evidence.get(item.recipe.id, ""),
                }
                for item in self.differentials
            ],
            "verdicts": dict(self.verdicts),
            "verdict_evidence": dict(self.verdict_evidence),
            "proposals": list(self.proposals),
            "executions": self.executions,
            "reused": self.reused,
            "notes": list(self.notes),
            "shared_base": self.shared_base,
            "carried": self.carried,
            "validity": [self.validity[subject] for subject in sorted(self.validity)],
            "saved_seconds": round(self.saved_seconds, 3),
        }


# --------------------------------------------------------------------------- #
# synthetic surface + runtime for one recipe variant
# --------------------------------------------------------------------------- #


def recipe_runtime(
    recipe: CheckRecipe,
    *,
    variant: Variant = "run",
    overlay: WorkspaceOverlay | None = None,
    tests: Sequence[str] | None = None,
) -> tuple[RuntimeConfigEntry, ReviewSurface]:
    """The runner-registry inputs for one recipe execution (never from head config).

    ``tests`` fills a test recipe's ``{tests}`` argument (impact selection);
    ``None`` runs the whole suite.
    """

    suffix = "" if variant == "run" else f":{variant}"
    runtime_id = f"recipe:{recipe.id}{suffix}"
    values: dict[str, Any] = {
        "command": list(recipe.fix_command if variant == "fix" else recipe.argv(tests)),
        "root": recipe.cwd,
        "timeout": recipe.timeout,
        "report": bool(recipe.report) and variant != "fix",
        "collect_changes": variant == "fix" or recipe.detect_changes,
    }
    if overlay is not None:
        values["overlay"] = overlay
    runtime = RuntimeConfigEntry(id=runtime_id, runner="command", values=values)
    surface = ReviewSurface(
        id=f"recipe:{recipe.id}",
        provider=RECIPE_PROVIDER,
        kind="check",
        title=recipe.title or recipe.id,
        runtime=runtime_id,
        capabilities=("execute",),
        metadata={"recipe_digest": recipe.digest, "variant": variant},
    )
    return runtime, surface


def _scenario(recipe: CheckRecipe, *, variant: Variant) -> ValidationScenario:
    suffix = "" if variant == "run" else f":{variant}"
    return ValidationScenario(
        id=recipe.entry_id,
        title=recipe.title or recipe.id,
        provider=RECIPE_PROVIDER,
        surface_id=f"recipe:{recipe.id}",
        runtime_id=f"recipe:{recipe.id}{suffix}",
    )


def attempt_request(
    inputs: CheckRunInputs,
    recipe: CheckRecipe,
    *,
    side: Side,
    variant: Variant,
    attempt: int,
    overlay: WorkspaceOverlay | None = None,
    tests: Sequence[str] | None = None,
) -> ValidationRequest:
    """Stable identity of one attempt: recipe digest, side, variant, attempt and executor class.

    Selected tests are part of the scope, only when a selection applies (every
    full-suite attempt keeps its identity).
    """

    digest = recipe.fix_digest if variant == "fix" else recipe.digest
    scope: dict[str, Any] = {
        "cwd": recipe.cwd,
        "overlay": list(overlay.digests()) if overlay is not None else [],
        "source": "",
    }
    if tests is not None:
        scope["tests"] = list(tests)
    return ValidationRequest(
        review_id=inputs.revision.review_id,
        revision_id=inputs.revision.id,
        snapshot_identity=snapshot_semantic_identity(inputs.revision),
        scenario_kind="recipe",
        scenario_id=recipe.entry_id,
        side=side,
        viewport_key=f"{variant}:{attempt}@{inputs.executor_class.value}",
        runtime_id=f"recipe:{recipe.id}" + ("" if variant == "run" else f":{variant}"),
        runtime_config_digest=digest.rpartition(":")[2],
        trusted_policy_digest=inputs.policy_digest,
        scope_digest=_hex(scope),
    )


def _report_from(data: Mapping[str, Any], recipe: CheckRecipe) -> TestReport | None:
    if not recipe.report:
        return None
    tests = data.get("tests")
    if isinstance(tests, list):
        # A persisted attempt: the parsed cases it stored.
        cases = tuple(
            TestCase(id=str(row[0]), status=row[1], file=str(row[2]) if len(row) > 2 else "")
            for row in tests[:_MAX_TEST_ROWS]
            if isinstance(row, list) and len(row) >= 2 and row[1] in {"passed", "failed", "skipped"}
        )
        meta = data.get("report") if isinstance(data.get("report"), Mapping) else {}
        assert isinstance(meta, Mapping)
        return TestReport(
            str(meta.get("format") or recipe.report),
            cases,
            error=str(meta.get("error") or ""),
            truncated=bool(meta.get("truncated")),
        )
    report = data.get("report")
    text = report if isinstance(report, str) else None
    if recipe.report in {"go-json", "cargo"} and not text:
        return TestReport(recipe.report, error="the runner wrote no report")
    return parse_report(recipe.report, text, truncated=bool(data.get("report_truncated")))


def _changed_paths(data: Mapping[str, Any]) -> tuple[str, ...]:
    changes = data.get("changes")
    if not isinstance(changes, Mapping):
        return ()
    rows = changes.get("files")
    paths = [str(row.get("path") or "") for row in rows or () if isinstance(row, Mapping)]
    omitted = [str(item) for item in changes.get("omitted") or ()]
    return tuple(path for path in (*paths, *omitted) if path)


def _attempt_record(
    request: ValidationRequest,
    scenario: ValidationScenario,
    result: SurfaceRunResult,
    *,
    recipe: CheckRecipe,
    variant: Variant,
    attempt: int,
    report: TestReport | None,
    input_digest: str = "",
) -> dict[str, Any]:
    """Normalized attempt record: a raw observation of one run, not a verdict."""

    status = "NOT_RUN" if result.status == "unavailable" else "PASS" if result.status == "passed" else "FAIL"
    data = dict(result.data)
    execution = data.pop("execution", None)
    data.pop("report", None)
    data.pop("report_truncated", None)
    changes = data.pop("changes", None)
    stored: dict[str, Any] = {"variant": variant, "attempt": attempt, "recipe_digest": recipe.digest}
    if input_digest:
        # R6 item 22: what this run depended on, so a later Snapshot may reuse it.
        stored["input_digest"] = input_digest
    if isinstance(execution, Mapping):
        stored["execution"] = dict(execution)
    if report is not None:
        stored["report"] = {
            "format": report.format,
            "error": report.error,
            "truncated": report.truncated,
            "tests": len(report.cases),
        }
        stored["tests"] = [[case.id, case.status, case.file] for case in report.cases[:_MAX_TEST_ROWS]]
    if isinstance(changes, Mapping):
        stored["changes"] = {
            "paths": list(_changed_paths({"changes": changes}))[:256],
            "count": int(changes.get("count") or 0),
        }
    reasons = [result.summary or ("runner was unavailable" if status == "NOT_RUN" else f"exit code {result.exit_code}")]
    return {
        "v": 1,
        "execution_version": 1,
        "identity": request.identity,
        "request": request.to_wire(),
        "scenario": scenario.to_wire(),
        "snapshot_id": request.revision_id,
        "side": request.side,
        "reported_status": result.status,
        "status": status,
        "status_reasons": reasons,
        "deterministic_checks": (
            []
            if result.exit_code is None
            else [
                {
                    "name": "process exit code",
                    "status": "PASS" if result.exit_code == 0 else "FAIL",
                    "expected": 0,
                    "actual": result.exit_code,
                    "detail": "Raw observation of one sandboxed attempt; the recipe verdict is separate.",
                }
            ]
        ),
        "runner": result.runner,
        "duration_ms": int(result.duration_ms or 0),
        "summary": protect_text(str(result.summary or "")[:2000])[0],
        "output": protect_text(str(result.output or "")[-8000:])[0],
        "data": stored,
    }


def _check_ref(item: ReviewEvidence) -> str:
    return f"check:{item.source}:{item.source_ref}" if item.source_ref else f"check:{item.id}"


def _outcome(
    record: Mapping[str, Any],
    item: ReviewEvidence,
    *,
    recipe: CheckRecipe,
    side: Side,
    variant: Variant,
    attempt: int,
    reused: bool,
) -> AttemptOutcome:
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    assert isinstance(data, Mapping)
    environment = ""
    raw = data.get("execution")
    if isinstance(raw, Mapping):
        try:
            environment = ExecutionResult.from_wire(raw).environment_fingerprint
        except (TypeError, ValueError):
            environment = ""
    status = str(record.get("status") or "")
    changes = data.get("changes") if isinstance(data.get("changes"), Mapping) else {}
    assert isinstance(changes, Mapping)
    exit_codes = [row.get("actual") for row in record.get("deterministic_checks") or () if isinstance(row, Mapping)]
    origin = data.get("shared_from") or data.get("reused_from")
    return AttemptOutcome(
        side=side,
        attempt=attempt,
        variant=variant,
        status="passed" if status == "PASS" else "failed" if status == "FAIL" else "not_run",
        reason=str((record.get("status_reasons") or [""])[0])[:500] if status not in {"PASS", "FAIL"} else "",
        exit_code=exit_codes[0] if exit_codes and isinstance(exit_codes[0], int) else None,
        duration_ms=int(record.get("duration_ms") or 0),
        environment=environment,
        executor_class=item.executor_class,
        evidence_id=item.id,
        evidence_ref=_check_ref(item),
        report=_report_from(data, recipe),
        changed_paths=tuple(str(path) for path in changes.get("paths") or ()),
        reused=reused or isinstance(origin, Mapping),
        reused_from=dict(origin) if isinstance(origin, Mapping) else None,
    )


def _carried_origin(record: Mapping[str, Any]) -> str:
    """The Snapshot (revision id) that actually ran a copy carried onto this one, or ``""``."""

    data = record.get("data")
    origin = data.get("reused_from") if isinstance(data, Mapping) else None
    if not isinstance(origin, Mapping) or origin.get("kind") != "snapshot":
        return ""
    return str(origin.get("revision_id") or "") or "unknown"


def _read_record(store: Any, item: ReviewEvidence) -> dict[str, Any] | None:
    payload = store.read_evidence_artifact(item)
    if payload is None or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


class _Run:
    def __init__(self, inputs: CheckRunInputs, run_side: RunSide, meter: Meter) -> None:
        self.inputs = inputs
        self.run_side = run_side
        self.meter = meter
        self.report = CheckRunReport(entry_id=inputs.entry_id, revision_id=inputs.revision.id)
        self.budget = inputs.rerun_budget_seconds
        self.fix_changes: dict[str, Mapping[str, Any]] = {}
        #: evidence id -> typed execution wire of that attempt (fresh or reused).
        self.executions: dict[str, Mapping[str, Any]] = {}
        #: recipe id -> environment fingerprint of its first head run (the shared-base lookup key).
        self.head_environment: dict[str, str] = {}
        #: scope key -> fresh run id of the Snapshot's newest fresh run (read once per run).
        self._generations: dict[str, str] | None = None
        #: recipe id -> its probed executor environment (read once per run; ``""`` unknown).
        self._environments: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # R6 execution economy
    # ------------------------------------------------------------------ #

    def selection(self, recipe: CheckRecipe) -> RecipeSelection | None:
        return self.inputs.selection.get(recipe.id)

    def tests_for(self, recipe: CheckRecipe) -> tuple[str, ...] | None:
        """The ``{tests}`` argument: the selected test files, or ``None`` for the whole suite."""

        chosen = self.selection(recipe)
        return chosen.cwd_relative(recipe.cwd) if chosen is not None and chosen.mode == "impact" else None

    def current_environment(self, recipe: CheckRecipe) -> str:
        """The executor environment a run of this recipe's command will record, or ``""`` when not known first."""

        known = self._environments.get(recipe.id)
        if known is None:
            probe = self.inputs.environment
            try:
                known = str(probe(recipe.argv(self.tests_for(recipe))) or "") if probe is not None else ""
            except Exception:  # a failed probe is an unknown environment, never a match
                known = ""
            self._environments[recipe.id] = known
        return known

    def _carry_key(
        self, recipe: CheckRecipe, attempt: int, request: ValidationRequest, *, environment: str = ""
    ) -> CarryKey:
        chosen = self.selection(recipe)
        return CarryKey(
            recipe_digest=recipe.digest,
            policy_digest=self.inputs.policy_digest,
            executor_class=self.inputs.executor_class.value,
            scope_digest=request.scope_digest,
            attempt=attempt,
            input_digest=chosen.input_digest if chosen is not None else "",
            check=recipe.id,
            environment=environment,
        )

    def _explain_carry(
        self,
        recipe: CheckRecipe,
        attempt: int,
        decision: CarryDecision | str,
        *,
        source_revision: str = "",
        evidence_id: str = "",
    ) -> None:
        """Record why a plain head attempt was reused or runs again.

        ``decision`` is the carry lookup's outcome, or the reason code of a path
        that never looks up a carry (a fresh run, a same-Snapshot replay). A
        replay names the Snapshot that actually ran the result it replays.
        """

        subject = f"check:{recipe.id}#{attempt}"
        current = str(getattr(self.inputs.revision, "id", "") or "")
        explained = (
            validity_result(
                decision,
                subject,
                current_revision=current,
                source_revision=source_revision,
                evidence_refs=(evidence_id,) if evidence_id else (),
            )
            if isinstance(decision, str)
            else carry_result(decision, subject_ref=subject, current_revision=current)
        )
        self.report.validity[subject] = explained.to_wire()

    def _replay_refusal(self, recipe: CheckRecipe, record: Mapping[str, Any]) -> str:
        """Why a copy carried onto this Snapshot may not be replayed now, or ``""``.

        A carried copy is replayed only under the conditions that let it carry:
        carry is on and this run's executor environment is known and equal to
        the one the origin run recorded.
        """

        if not carry_enabled():
            return "carry_disabled"
        current = self.current_environment(recipe)
        origin = record_environment(record)
        if not current or not origin:
            return "carry_environment_unknown"
        return "" if origin == current else "carry_environment_changed"

    def _base_key(
        self, recipe: CheckRecipe, attempt: int, request: ValidationRequest, environment: str
    ) -> SharedBaseKey:
        return SharedBaseKey(
            base_sha=str(getattr(self.inputs.revision, "base_sha", "") or ""),
            recipe_digest=recipe.digest,
            environment=environment,
            executor_class=self.inputs.executor_class.value,
            policy_digest=self.inputs.policy_digest,
            scope_digest=request.scope_digest,
            attempt=attempt,
        )

    def _save(self, reason: str, duration_ms: int) -> None:
        seconds = max(0, int(duration_ms)) / 1000.0
        self.report.saved_seconds += seconds
        if self.inputs.saved is not None and seconds > 0:
            self.inputs.saved(reason, seconds)

    def _adopt(
        self,
        request: ValidationRequest,
        scenario: ValidationScenario,
        source: Mapping[str, Any],
        provenance: tuple[str, dict[str, Any]],
    ) -> tuple[ReviewEvidence, dict[str, Any]]:
        """Copy a verified run onto this Snapshot under this attempt's identity, with its provenance."""

        record = dict(source)
        data = dict(record.get("data") or {})
        data.pop("shared_from", None)
        data.pop("reused_from", None)
        data[provenance[0]] = provenance[1]
        record.update(
            identity=request.identity,
            request=request.to_wire(),
            scenario=scenario.to_wire(),
            snapshot_id=request.revision_id,
            side=request.side,
            data=data,
        )
        return self._persist(request, scenario, record), record

    def _reuse(
        self, recipe: CheckRecipe, side: Side, attempt: int, request: ValidationRequest, scenario: ValidationScenario
    ) -> AttemptOutcome | None:
        """A shared base run (base side) or an unchanged earlier Snapshot's run (head side)."""

        inputs = self.inputs
        if side == "new":
            key = self._carry_key(recipe, attempt, request, environment=self.current_environment(recipe))
            decision = explain_carry(inputs.store, inputs.revision, key)
            self._explain_carry(recipe, attempt, decision)
            carried = decision.carried
            if carried is None:
                return None
            evidence, record = self._adopt(
                request,
                scenario,
                carried.record,
                (
                    "reused_from",
                    {
                        "kind": "snapshot",
                        "snapshot": carried.revision_number,
                        "revision_id": carried.revision_id,
                        "evidence_id": carried.evidence_id,
                        "duration_ms": carried.duration_ms,
                    },
                ),
            )
            record_carry(
                inputs.store,
                revision=inputs.revision,
                key=key,
                evidence=evidence,
                record=record,
                side=side,
                variant="run",
                overlay=False,
            )
            self.report.carried += 1
            self._save("carried_forward", carried.duration_ms)
        else:
            environment = self.head_environment.get(recipe.id, "")
            if not environment:
                return None  # no head fingerprint to match: the base executes
            shared = find_shared_base(inputs.store, self._base_key(recipe, attempt, request, environment))
            if shared is None:
                return None
            evidence, record = self._adopt(
                request,
                scenario,
                shared.record,
                (
                    "shared_from",
                    {
                        "kind": "shared_base",
                        "review_id": shared.review_id,
                        "revision_id": shared.revision_id,
                        "evidence_id": shared.evidence_id,
                        "duration_ms": shared.duration_ms,
                    },
                ),
            )
            self.report.shared_base += 1
            self._save("base_shared", shared.duration_ms)
        self._remember(evidence.id, record)
        return _outcome(record, evidence, recipe=recipe, side=side, variant="run", attempt=attempt, reused=True)

    def _index(
        self,
        recipe: CheckRecipe,
        side: Side,
        attempt: int,
        request: ValidationRequest,
        evidence: ReviewEvidence,
        record: Mapping[str, Any],
        *,
        newest: bool = False,
    ) -> None:
        """Offer a fresh plain run for reuse: a later Snapshot (head) or another Review (base).

        ``newest``: the run belongs to a fresh generation, so it replaces this
        Snapshot's carry row rather than deferring to an earlier one.
        """

        inputs = self.inputs
        stamped = stamp_execution_record(record, inputs.executor_class)
        if side == "new":
            record_carry(
                inputs.store,
                revision=inputs.revision,
                key=self._carry_key(recipe, attempt, request),
                evidence=evidence,
                record=stamped,
                side=side,
                variant="run",
                overlay=False,
                replace=newest,
            )
            return
        data = stamped.get("data")
        execution = data.get("execution") if isinstance(data, Mapping) else None
        try:
            environment = ExecutionResult.from_wire(execution).environment_fingerprint if execution else ""
        except (TypeError, ValueError):
            environment = ""
        refusal = publish_shared_base(
            inputs.store,
            review_id=inputs.revision.review_id,
            revision_id=inputs.revision.id,
            revision_base_sha=str(getattr(inputs.revision, "base_sha", "") or ""),
            key=self._base_key(recipe, attempt, request, environment),
            evidence=evidence,
            record=stamped,
            side=side,
            variant="run",
            overlay=False,
            replace=newest,
        )
        if refusal and attempt == 1:
            self.report.notes.append(f"{recipe.id}: base result not shared ({refusal})")

    def _generation_scope(self, recipe: CheckRecipe) -> str:
        """One recipe's identity on this Snapshot: its plain head attempt 1, whatever the attempt run now."""

        head = attempt_request(self.inputs, recipe, side="new", variant="run", attempt=1, tests=self.tests_for(recipe))
        return _hex([head.identity, self.inputs.execution_intent])

    def generation(self, recipe: CheckRecipe) -> str:
        """The fresh run whose results this run resolves to (``""``: the Snapshot's plain results).

        An explicit fresh run is its own generation. Any later run of the same
        recipe on this Snapshot follows the newest fresh run, so a fresh result
        supersedes the earlier plain (possibly carried) one for every attempt
        and variant, and a fresh-only Snapshot replays instead of executing again.
        """

        if self.inputs.fresh_run_id:
            return self.inputs.fresh_run_id
        if self._generations is None:
            rows = self.inputs.store.derived_results(FRESH_GENERATION_KIND, [self.inputs.revision.id])
            self._generations = {
                scope: str(payload.get("fresh_run_id") or "")
                for (_revision, scope), (stamp, payload) in rows.items()
                if stamp == _FRESH_GENERATION_STAMP
            }
        return self._generations.get(self._generation_scope(recipe), "")

    def _mark_generation(self, recipe: CheckRecipe) -> None:
        """Name this fresh run the recipe's newest result on the Snapshot (upsert: the newest wins)."""

        self.inputs.store.put_derived_results(
            [
                DerivedResult(
                    review_id=self.inputs.revision.review_id,
                    revision_id=self.inputs.revision.id,
                    kind=FRESH_GENERATION_KIND,
                    scope_key=self._generation_scope(recipe),
                    stamp=_FRESH_GENERATION_STAMP,
                    payload={"fresh_run_id": self.inputs.fresh_run_id},
                )
            ]
        )

    def _remember(self, evidence_id: str, record: Mapping[str, Any]) -> None:
        data = record.get("data")
        execution = data.get("execution") if isinstance(data, Mapping) else None
        if isinstance(execution, Mapping):
            self.executions[evidence_id] = execution

    def _existing(self, request: ValidationRequest, fresh_ref: str) -> tuple[ReviewEvidence, dict[str, Any]] | None:
        """This Snapshot's verified result of exactly this attempt under ``fresh_ref``, or ``None``."""

        source_ref = validation_source_ref(request, execution_intent=self.inputs.execution_intent, fresh_ref=fresh_ref)
        # Within one fresh run an attempt is still replayed idempotently; an earlier
        # (possibly carried) result of this Snapshot has another source_ref.
        existing: ReviewEvidence | None = self.inputs.store.get_evidence_by_source(
            request.review_id, _SOURCE, source_ref
        )
        if existing is None or existing.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE:
            return None
        record = _read_record(self.inputs.store, existing)
        if record is None or record.get("identity") != request.identity:
            return None
        return existing, record

    def _persist(
        self,
        request: ValidationRequest,
        scenario: ValidationScenario,
        record: Mapping[str, Any],
        *,
        path: str = "",
        fresh_ref: str = "",
    ) -> ReviewEvidence:
        try:
            evidence: ReviewEvidence = persist_validation_record(
                self.inputs.store,
                self.inputs.revision,
                request,
                scenario,
                record,
                path=path,
                source=_SOURCE,
                expected_current_revision_id=self.inputs.expected_current_revision_id,
                execution_intent=self.inputs.execution_intent,
                executor_class=self.inputs.executor_class,
                verdict=False,
                fresh_ref=fresh_ref,
            )
        except ValueError as exc:
            if "superseded" in str(exc).lower():
                raise CheckRunSuperseded(str(exc)) from exc
            raise
        return evidence

    def attempt(
        self,
        recipe: CheckRecipe,
        side: Side,
        *,
        variant: Variant = "run",
        attempt: int = 1,
        overlay: WorkspaceOverlay | None = None,
    ) -> AttemptOutcome:
        outcome = self._attempt(recipe, side, variant=variant, attempt=attempt, overlay=overlay)
        if side == "new" and variant == "run" and outcome.environment:
            self.head_environment.setdefault(recipe.id, outcome.environment)
        return outcome

    def _attempt(
        self,
        recipe: CheckRecipe,
        side: Side,
        *,
        variant: Variant,
        attempt: int,
        overlay: WorkspaceOverlay | None,
    ) -> AttemptOutcome:
        tests = self.tests_for(recipe) if variant != "fix" else None
        request = attempt_request(
            self.inputs, recipe, side=side, variant=variant, attempt=attempt, overlay=overlay, tests=tests
        )
        scenario = _scenario(recipe, variant=variant)
        # The newest fresh run of this recipe supersedes the Snapshot's plain results:
        # a later run replays (or continues) that generation, never the older one.
        fresh = self.generation(recipe)
        plain = variant == "run" and overlay is None
        replay = self._existing(request, fresh)
        if replay is not None and plain and side == "new" and not fresh and _carried_origin(replay[1]):
            # A copy carried onto this Snapshot: this Snapshot's own later run wins;
            # otherwise the copy replays only while it still could carry.
            own = self._existing(request, _UNCARRIED_REF)
            if own is not None:
                replay, fresh = own, _UNCARRIED_REF
            else:
                refusal = self._replay_refusal(recipe, replay[1])
                if refusal:
                    self._explain_carry(recipe, attempt, refusal)
                    replay, fresh = None, _UNCARRIED_REF
        if replay is not None:
            existing, record = replay
            self.report.reused += 1
            if side == "new" and plain:
                origin = _carried_origin(record)
                self._explain_carry(
                    recipe,
                    attempt,
                    "replayed_on_snapshot",
                    source_revision=origin or self.inputs.revision.id,
                    evidence_id=existing.id,
                )
            self._remember(existing.id, record)
            return _outcome(record, existing, recipe=recipe, side=side, variant=variant, attempt=attempt, reused=True)
        if plain and not fresh:
            reused = self._reuse(recipe, side, attempt, request, scenario)
            if reused is not None:
                return reused
        elif plain and side == "new" and fresh != _UNCARRIED_REF:
            self._explain_carry(recipe, attempt, "fresh_run_requested")
        runtime, surface = recipe_runtime(recipe, variant=variant, overlay=overlay, tests=tests)
        started = time.monotonic()
        try:
            result = self.run_side(runtime, surface, side)
        except (OSError, RuntimeError, ValueError) as exc:
            result = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"recipe could not execute: {exc}"[:1000],
                "command",
                runtime.id,
            )
        wall = time.monotonic() - started
        self.report.executions += 1
        execution = result.data.get("execution") if isinstance(result.data, Mapping) else None
        self.meter(execution if isinstance(execution, Mapping) else None, wall)
        if self.budget is not None:
            self.budget -= wall
        report = _report_from(result.data, recipe) if result.status != "unavailable" else None
        if variant == "fix" or (recipe.detect_changes and side == "new"):
            changes = result.data.get("changes")
            if isinstance(changes, Mapping):
                self.fix_changes[recipe.id] = changes
        chosen = self.selection(recipe)
        record = _attempt_record(
            request,
            scenario,
            result,
            recipe=recipe,
            variant=variant,
            attempt=attempt,
            report=report,
            input_digest=chosen.input_digest if chosen is not None and plain and side == "new" else "",
        )
        evidence = self._persist(request, scenario, record, fresh_ref=fresh)
        self._remember(evidence.id, record)
        if self.inputs.fresh_run_id and plain and side == "new" and attempt == 1:
            self._mark_generation(recipe)
        if plain:
            self._index(recipe, side, attempt, request, evidence, record, newest=bool(fresh))
        return _outcome(record, evidence, recipe=recipe, side=side, variant=variant, attempt=attempt, reused=False)

    def side_attempts(
        self, recipe: CheckRecipe, side: Side, *, reruns: int, notes: dict[str, Any]
    ) -> list[AttemptOutcome]:
        """Attempt 1 and, when it fails, reruns until a pass, the policy count or the budget."""

        first = self.attempt(recipe, side)
        outcomes = [first]

        def failed(item: AttemptOutcome) -> bool:
            return item.status == "failed" or (recipe.detect_changes and bool(item.changed_paths))

        executed = 0
        while failed(outcomes[-1]) and executed < reruns:
            estimate = max(outcomes[-1].duration_ms / 1000.0, 0.001)
            if self.budget is not None and estimate > self.budget:
                notes["limited_by_budget"] = True
                break
            outcomes.append(self.attempt(recipe, side, attempt=len(outcomes) + 1))
            executed += 1
        notes.setdefault("executed", {})[side] = executed
        return outcomes

    def overlay(self, recipe: CheckRecipe) -> WorkspaceOverlay | None:
        """The head's added/edited test files, to run on base source (adequacy probe)."""

        prefix = "" if recipe.cwd in {"", "."} else recipe.cwd.rstrip("/") + "/"
        files: list[tuple[str, str | None]] = []
        for path in sorted(self.inputs.changed_paths):
            if not path.startswith(prefix) or not is_test_path(path):
                continue
            text = self.inputs.read_head(path)
            if text is None:
                continue  # deleted on head: nothing to demonstrate
            files.append((path, text))
            if len(files) >= _MAX_OVERLAY_FILES:
                self.report.notes.append(f"{recipe.id}: adequacy probe limited to {_MAX_OVERLAY_FILES} test files")
                break
        if not files or not self.inputs.head_identity:
            return None
        return WorkspaceOverlay(source=self.inputs.head_identity, files=tuple(files))

    def recipe(self, recipe: CheckRecipe) -> RecipeDifferential:
        inputs = self.inputs
        policy = inputs.catalog.policy
        guard = head_definition_guard(recipe, inputs.changed_paths, inputs.read_base, inputs.read_head)
        notes: dict[str, Any] = {"policy": policy.reruns}
        economy: dict[str, Any] = {}
        chosen = self.selection(recipe)
        if chosen is not None:
            wire = chosen.to_wire()
            wire["tests"] = list(chosen.tests[:200])
            economy["selection"] = {**wire, "selected": len(chosen.tests)}
        # Head first (R6 item 20): the head decides whether an on-demand base runs,
        # and its environment fingerprint is the shared-base lookup key.
        head: list[AttemptOutcome]
        if guard:
            head = [AttemptOutcome(side="new", attempt=1, status="not_run", reason=guard)]
        else:
            head = self.side_attempts(recipe, "new", reruns=policy.reruns, notes=notes)
        run_base = "old" in inputs.sides
        if not run_base and inputs.base_on_demand:
            # The head failed when its last attempt failed (reruns stop at the first pass);
            # a head that passed on a rerun is flaky, which no base run can reclassify.
            last = head[-1]
            failed = last.status == "failed" or (recipe.detect_changes and bool(last.changed_paths))
            estimate = max(head[0].duration_ms / 1000.0, 0.001)
            if guard:
                why = "the head did not run, so there is no failure for the base to classify"
            elif not failed:
                why = (
                    "head passed: the base runs only to classify a head failure"
                    if len(head) == 1
                    else "head passed on a rerun (flaky): a base run cannot classify a flaky head"
                )
            elif self.budget is not None and estimate > self.budget:
                notes["limited_by_budget"] = True
                why = "head failed; the base was not run: Limited by budget"
            else:
                run_base = True
                why = "head failed: the base ran to tell pre-existing from introduced failures"
            economy["base"] = {"mode": "on_demand", "ran": run_base, "reason": why}
        base = self.side_attempts(recipe, "old", reruns=policy.reruns, notes=notes) if run_base else []
        attempts: list[AttemptOutcome] = [*base, *head]
        sides: tuple[Side, ...] = ("old", "new") if run_base else ("new",)
        differences = self._environment_differences(attempts)
        differential = classify_recipe(
            recipe,
            attempts,
            policy=policy,
            sides=sides,
            environment_differences=differences,
            head_guard=guard,
            reruns=notes,
        )
        adequacy: tuple[Any, ...] = ()
        if recipe.kind == "test" and policy.adequacy and run_base and differential.granularity == "test" and not guard:
            overlay = self.overlay(recipe)
            probes: list[AttemptOutcome] = []
            if overlay is not None:
                probes.append(self.attempt(recipe, "old", variant="probe", overlay=overlay))
                attempts.extend(probes)
            changed_tests = tuple(path for path, _text in overlay.files) if overlay is not None else ()
            changes = {
                path: diff_test_file(path, inputs.read_base(path), text or "")
                for path, text in (overlay.files if overlay is not None else ())
            }
            adequacy = assess_adequacy(recipe, differential, changed_tests, probes, changes)
        autofix, fixes = self._autofix(recipe, differential, guard)
        # The fix run is part of the record: its execution reproduces like any other attempt.
        attempts.extend(fixes)
        claims = reconcile_claims(inputs.claims, (recipe,), {recipe.id: differential.head_verdict})
        matched = tuple(row for row in claims if row.recipe_id == recipe.id)
        reasons = list(differential.reasons)
        if chosen is not None and chosen.mode == "impact":
            reasons.append(f"ran {len(chosen.tests)} of {chosen.total} test files selected by test impact")
        if "base" in economy and not run_base:
            reasons.append(str(economy["base"]["reason"]))
        # Trust T3 item 2: why each plain head attempt was reused or ran, kept with the
        # result so the run plan's re-verification summary can show it later.
        prefix = f"check:{recipe.id}#"
        validity = {subject: row for subject, row in self.report.validity.items() if subject.startswith(prefix)}
        if validity:
            economy["validity"] = validity
        return RecipeDifferential(
            recipe=differential.recipe,
            sides=differential.sides,
            granularity=differential.granularity,
            comparable=differential.comparable,
            environment=differential.environment,
            executor_class=differential.executor_class,
            head_verdict=differential.head_verdict,
            base_verdict=differential.base_verdict,
            reasons=tuple(reasons),
            recipe_unit=differential.recipe_unit,
            tests=differential.tests,
            counts=differential.counts,
            attempts=tuple(attempts),
            reruns=differential.reruns,
            adequacy=adequacy,
            claims=matched,
            autofix=autofix,
            head_guard=guard,
            economy=economy,
        )

    def _environment_differences(self, attempts: Sequence[AttemptOutcome]) -> tuple[str, ...]:
        """What differs between the base and head environment fingerprints, in words."""

        def fingerprint(side: str) -> Any:
            for attempt in attempts:
                raw = self.executions.get(attempt.evidence_id) if attempt.side == side else None
                if raw is None:
                    continue
                try:
                    return ExecutionResult.from_wire(raw).fingerprint
                except (TypeError, ValueError):
                    return None
            return None

        base, head = fingerprint("old"), fingerprint("new")
        if base is None or head is None or base.digest == head.digest:
            return ()
        return tuple(base.differences(head)) or ("environment fingerprint",)

    def _autofix(
        self, recipe: CheckRecipe, differential: RecipeDifferential, guard: str
    ) -> tuple[dict[str, Any], tuple[AttemptOutcome, ...]]:
        policy = self.inputs.catalog.policy
        if guard or differential.head_verdict != "FAIL":
            return {}, ()
        if not recipe.fix_command and not recipe.detect_changes:
            return {}, ()
        if not policy.autofix:
            return {"status": "off", "reason": "check_policy.autofix is off in the trusted base"}, ()
        fixes: tuple[AttemptOutcome, ...] = ()
        if not recipe.detect_changes:
            fix = self.attempt(recipe, "new", variant="fix")
            fixes = (fix,)
            if fix.status == "not_run":
                return {"status": "not_run", "reason": fix.reason or "the autofix command did not run"}, fixes
            if fix.reused and recipe.id not in self.fix_changes:
                return {"status": "reused", "reason": "autofix already proposed for this Snapshot"}, fixes
        changes = self.fix_changes.get(recipe.id)
        if changes is None:
            return {
                "status": "unavailable",
                "reason": "the executor returned no autofix change set, so nothing can be proposed",
            }, fixes
        proposals, skipped = self._proposals(recipe, changes)
        return {
            "status": "proposed" if proposals else "no_changes" if not skipped else "not_proposable",
            "proposal_ids": proposals,
            "skipped": skipped,
            "reason": (
                f"{len(proposals)} change proposal(s); source is never modified by LemonCrow"
                if proposals
                else "the autofix changed nothing that can be proposed"
            ),
        }, fixes

    def _proposals(self, recipe: CheckRecipe, changes: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        from lemoncrow.pro.capabilities.review.change_proposals import text_sha256, unified_proposal_patch

        store = self.inputs.store
        revision = self.inputs.revision
        intent = f"autofix:{recipe.id}"
        existing = {
            (item.path, item.replacement_text): item.id
            for item in store.list_change_proposals(revision.review_id)
            if item.base_revision_id == revision.id and item.intent == intent
        }
        prefix = "" if recipe.cwd in {"", "."} else recipe.cwd.rstrip("/") + "/"
        created: list[str] = []
        skipped: list[str] = [str(path) for path in changes.get("omitted") or ()]
        for row in changes.get("files") or ():
            if not isinstance(row, Mapping):
                continue
            path = str(row.get("path") or "")
            text = row.get("text")
            original = self.inputs.read_head(path)
            if row.get("status") != "modified" or not isinstance(text, str) or original is None or not original:
                # Added/deleted files have no line range to propose against.
                skipped.append(path)
                continue
            if not path.startswith(prefix):
                skipped.append(path)
                continue
            lines = original.splitlines(keepends=True)
            key = (path, text)
            if key in existing:
                created.append(existing[key])
                continue
            try:
                proposed, patch = unified_proposal_patch(path, original, 1, len(lines), text)
            except ValueError:
                skipped.append(path)
                continue
            if not patch or proposed == original:
                continue
            try:
                proposal = store.add_change_proposal(
                    ReviewChangeProposal(
                        id="",
                        review_id=revision.review_id,
                        base_revision_id=revision.id,
                        path=path,
                        start_line=1,
                        end_line=len(lines),
                        original_text=original,
                        replacement_text=text,
                        patch_text=patch,
                        base_file_sha256=text_sha256(original),
                        intent=intent,
                        created_by=f"lemoncrow:recipe:{recipe.id}",
                    ),
                    expected_current_revision_id=self.inputs.expected_current_revision_id,
                )
            except ValueError as exc:
                if "advanced" in str(exc).lower():
                    raise CheckRunSuperseded(str(exc)) from exc
                raise
            created.append(proposal.id)
            self.report.proposals.append(proposal.id)
        return created, skipped

    # ------------------------------------------------------------------ #
    # publication
    # ------------------------------------------------------------------ #

    def publish_differential(self, differential: RecipeDifferential) -> ReviewEvidence:
        inputs = self.inputs
        record = differential.to_wire()
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        source_ref = f"check-differential:{differential.recipe.id}:{digest}"
        existing: ReviewEvidence | None = inputs.store.get_evidence_by_source(
            inputs.revision.review_id, _SOURCE, source_ref
        )
        if existing is not None and existing.revision_id == inputs.revision.id:
            return existing
        evidence_id = new_evidence_id()
        rel, content_hash, size = inputs.store.write_evidence_artifact(
            inputs.revision.review_id, inputs.revision.id, evidence_id, payload
        )
        try:
            added: ReviewEvidence = inputs.store.add_evidence(
                ReviewEvidence(
                    id=evidence_id,
                    review_id=inputs.revision.review_id,
                    revision_id=inputs.revision.id,
                    kind="verification",
                    title=f"{differential.recipe.title or differential.recipe.id} · base vs head",
                    artifact_path=rel,
                    content_hash=content_hash,
                    mime_type=CHECK_DIFFERENTIAL_MEDIA_TYPE,
                    bytes=size,
                    source=_SOURCE,
                    source_ref=source_ref,
                    detail="; ".join(differential.reasons)[:1200],
                    execution_intent=inputs.execution_intent,
                    executor_class=differential.executor_class,
                ),
                expected_current_revision_id=inputs.expected_current_revision_id,
            )
        except ValueError as exc:
            inputs.store.discard_artifact(rel)
            raise CheckRunSuperseded("check differential Snapshot was superseded before publication") from exc
        except Exception:
            inputs.store.discard_artifact(rel)
            raise
        return added

    def publish_verdict(
        self,
        stable_key: str,
        title: str,
        status: str,
        reasons: Sequence[str],
        *,
        recipes: Sequence[CheckRecipe],
        differential_ids: Sequence[str],
        execution: Mapping[str, Any] | None,
        path: str = "",
        recipe_digest: str | None = None,
    ) -> ReviewEvidence:
        """One typed ``lemoncrow_verification`` result for a recipe or a kind entry."""

        inputs = self.inputs
        digest = _hex({"status": status, "reasons": list(reasons), "differentials": list(differential_ids)})
        request = ValidationRequest(
            review_id=inputs.revision.review_id,
            revision_id=inputs.revision.id,
            snapshot_identity=snapshot_semantic_identity(inputs.revision),
            scenario_kind="recipe",
            scenario_id=stable_key,
            side="new",
            viewport_key=f"verdict@{inputs.executor_class.value}:{digest[:16]}",
            runtime_id="recipes",
            runtime_config_digest=_hex(sorted(recipe.digest for recipe in recipes)),
            trusted_policy_digest=inputs.policy_digest,
            scope_digest=_hex({"entry": stable_key}),
        )
        scenario = ValidationScenario(
            id=stable_key, title=title, provider=RECIPE_PROVIDER, surface_id=stable_key, runtime_id="recipes"
        )
        data: dict[str, Any] = {
            "verdict": True,
            "recipes": [{"id": recipe.id, "digest": recipe.digest} for recipe in recipes],
            "differentials": list(differential_ids),
        }
        if execution is not None:
            # A trusted-policy quarantine may qualify a logical PASS even when
            # the underlying process failed. The individual attempts retain
            # their genuine failing execution and differential evidence.
            try:
                observed = ExecutionResult.from_wire(execution)
            except (TypeError, ValueError):
                observed = None
            run_stage = observed.stage("run") if observed is not None else None
            if status != "PASS" or (
                observed is not None
                and observed.status == "passed"
                and run_stage is not None
                and run_stage.status == "passed"
                and run_stage.exit_code in (None, 0)
            ):
                data["execution"] = dict(execution)
            else:
                data["process_proof"] = "not_claimed_policy_qualified_verdict"
        record = {
            "v": 1,
            "execution_version": 1,
            "identity": request.identity,
            "request": request.to_wire(),
            "scenario": scenario.to_wire(),
            "snapshot_id": inputs.revision.id,
            "side": "new",
            "reported_status": status.lower(),
            "status": status,
            "status_reasons": list(reasons) or [status],
            "deterministic_checks": [],
            "runner": "recipes",
            "duration_ms": 0,
            "summary": "; ".join(reasons)[:2000],
            "output": "",
            "data": data,
        }
        # CI equivalence (``check_ingest.EquivalenceKey``, the one canonical form):
        # the exact head commit, only for a clean Snapshot (a dirty tree is not
        # that commit), and the recipe digest it ran. The tool identity is the
        # one the federation matcher derives from the row written below
        # (``tool_identity``: no provider or integration, the entry id as the
        # stable key), so a trusted-base ``ci_equivalents`` row with
        # ``attestation_class: lemoncrow_verification`` and ``check: <entry id>``
        # matches it.
        revision = inputs.revision
        head_sha = str(getattr(revision, "head_sha", "") or "") if not getattr(revision, "dirty", False) else ""
        tool = tool_identity(
            ReviewEvidence(
                id="",
                review_id=revision.review_id,
                revision_id=revision.id,
                kind="verification",
                title=title,
                stable_key=stable_key,
            )
        )
        scope = EquivalenceKey(
            tool=tool,
            recipe=stable_key,
            head_sha=head_sha,
            recipe_digest=entry_recipe_digest(recipes) if recipe_digest is None else recipe_digest,
        ).canonical()
        try:
            evidence: ReviewEvidence = persist_validation_record(
                inputs.store,
                inputs.revision,
                request,
                scenario,
                record,
                path=path,
                source=_SOURCE,
                expected_current_revision_id=inputs.expected_current_revision_id,
                execution_intent=inputs.execution_intent,
                executor_class=inputs.executor_class,
                provider_revision=head_sha,
                attestation_scope=scope,
            )
        except ValueError as exc:
            if "superseded" in str(exc).lower():
                raise CheckRunSuperseded(str(exc)) from exc
            raise
        return evidence

    def head_execution(self, differential: RecipeDifferential) -> Mapping[str, Any] | None:
        head = next(
            (a for a in reversed(differential.attempts) if a.side == "new" and a.variant == "run" and a.evidence_id),
            None,
        )
        return self.executions.get(head.evidence_id) if head is not None else None


def selection_recipe_digest(recipes: Sequence[CheckRecipe], selection: Mapping[str, RecipeSelection]) -> str:
    """The recipe-digest slot of a verdict: a narrowed run never carries the full suite's digest.

    When impact selection narrowed or skipped any recipe, the slot is
    ``selected:sha256:...`` over the full digest and the selection, so a CI
    equivalence row pinned to the full recipe digest is never met by a subset.
    """

    full = entry_recipe_digest(recipes)
    narrowed = {
        recipe.id: selection[recipe.id].execution_document()
        for recipe in recipes
        if recipe.id in selection and selection[recipe.id].mode != "all"
    }
    if not narrowed:
        return full
    return "selected:sha256:" + _hex({"recipes": full, "selection": narrowed})


def run_check_entry(inputs: CheckRunInputs, run_side: RunSide, meter: Meter) -> CheckRunReport:
    """Execute one admitted check entry's recipes and publish their results.

    Raises :class:`CheckRunSuperseded` when the Snapshot advanced; whatever
    was already published stays bound to its exact (now historical) Snapshot.
    """

    run = _Run(inputs, run_side, meter)
    recipes = inputs.catalog.for_entry(inputs.entry_id)
    report = run.report
    if not recipes:
        report.notes.append(f"{inputs.entry_id} has no trusted-base recipe to run")
    kind_verdicts: dict[str, list[tuple[CheckRecipe, str, str]]] = {}
    not_run: dict[str, list[str]] = {}
    for recipe in recipes:
        chosen = inputs.selection.get(recipe.id)
        if chosen is not None and chosen.mode == "none":
            # Impact selection: no test of this recipe reaches the change (item 19).
            report.notes.append(f"{recipe.id}: not run ({chosen.reason})")
            if recipe.kind_entry:
                not_run.setdefault(recipe.kind_entry, []).append(
                    f"{recipe.title or recipe.id}: not run ({chosen.reason})"
                )
            continue
        differential = run.recipe(recipe)
        evidence = run.publish_differential(differential)
        report.differentials.append(differential)
        report.differential_evidence[recipe.id] = evidence.id
        execution = run.head_execution(differential)
        verdict = run.publish_verdict(
            recipe.entry_id,
            recipe.title or recipe.id,
            differential.head_verdict,
            differential.reasons,
            recipes=(recipe,),
            differential_ids=(evidence.id,),
            execution=execution,
            recipe_digest=selection_recipe_digest((recipe,), inputs.selection),
        )
        report.verdicts[recipe.entry_id] = differential.head_verdict
        report.verdict_evidence[recipe.entry_id] = verdict.id
        if recipe.kind_entry and not inputs.entry_id.startswith(RECIPE_ENTRY_PREFIX):
            kind_verdicts.setdefault(recipe.kind_entry, []).append((recipe, differential.head_verdict, evidence.id))
    targets = (
        [inputs.entry_id]
        if inputs.entry_id in KIND_ENTRY.values()
        else ["checks.test"] if inputs.entry_id == TEST_DIFF_ENTRY else []
    )
    for kind_entry in targets:
        rows = kind_verdicts.get(kind_entry, [])
        status = combine_verdicts([row[1] for row in rows])  # type: ignore[misc]
        skipped = not_run.get(kind_entry, [])
        reasons = (
            [*(f"{row[0].title or row[0].id}: {row[1]}" for row in rows), *skipped]
            if rows or skipped
            else ["no trusted-base recipe implements this check; add check_recipes to .lemoncrow/review.yaml"]
        )
        verdict = run.publish_verdict(
            kind_entry,
            kind_entry.removeprefix("checks.").replace("precommit", "pre-commit").capitalize() + " checks",
            status,
            reasons,
            recipes=[row[0] for row in rows],
            differential_ids=[row[2] for row in rows],
            execution=None,
            recipe_digest=selection_recipe_digest(
                [recipe for recipe in recipes if recipe.kind_entry == kind_entry] or [row[0] for row in rows],
                inputs.selection,
            ),
        )
        report.verdicts[kind_entry] = status
        report.verdict_evidence[kind_entry] = verdict.id
    return report


__all__ = [
    "CHECK_ENTRIES",
    "CheckRunInputs",
    "CheckRunReport",
    "CheckRunSuperseded",
    "attempt_request",
    "is_check_entry",
    "recipe_runtime",
    "run_check_entry",
]
