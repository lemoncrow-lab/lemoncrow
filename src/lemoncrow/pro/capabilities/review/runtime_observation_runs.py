"""Planned, observed base-vs-head runs of the trusted-base tests (Phase 9R item 10).

One call executes the admitted ``side_effects.runtime`` entry: every
trusted-base ``checks.test`` recipe runs on base and head through the R4
runner registry (local Bubblewrap, customer worker or managed sandbox) with
the runtime observer attached (:mod:`runtime_observation`), and publishes:

* one raw *attempt* record per side (kind ``verification``, never a verdict,
  no ``stable_key``: heavy run artifacts it references follow R4 raw
  retention unless a judgment cites them);
* one *diff* record per recipe -- only differences, and only under one
  environment fingerprint -- the input of the ``side_effect`` behavior
  producer and of the observed changed-execution-flow (call traces).

Nothing here decides whether to run: callers admit the plan entry first and
meter every execution. A Snapshot that advanced during the run refuses
publication (:class:`ObservationRunSuperseded`).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.recipes import CheckRecipe
from lemoncrow.pro.capabilities.review.run_evidence import protect_text
from lemoncrow.pro.capabilities.review.runtime_observation import (
    LIMITATIONS,
    OBSERVATION_VERSION,
    RUNTIME_OBSERVATION_ENTRY,
    RUNTIME_OBSERVATION_MEDIA_TYPE,
    ObservationSpec,
    RuntimeObservation,
    diff_observations,
    read_record,
)
from lemoncrow.pro.capabilities.review.runtime_validation import (
    execution_verification,
    snapshot_semantic_identity,
    stamp_execution_record,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.store import new_evidence_id
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.environment.execution import ExecutionResult, ExecutorClass
from lemoncrow.pro.environment.runners import SurfaceRunResult
from lemoncrow.pro.environment.services import ServiceSet

Side = Literal["old", "new"]
RunSide = Callable[[RuntimeConfigEntry, ReviewSurface, str], SurfaceRunResult]
Meter = Callable[[Mapping[str, Any] | None, float], None]

OBSERVE_PROVIDER: Final[str] = "runtime-observation"
_SOURCE: Final = "test"
_SIDE_NAME: Final[Mapping[str, str]] = {"old": "base", "new": "head"}


class ObservationRunSuperseded(ValueError):
    """The Snapshot advanced during the run; nothing more is published as current."""


@dataclass(frozen=True, slots=True)
class ObservationRunInputs:
    store: Any
    revision: ReviewRevision
    recipes: tuple[CheckRecipe, ...]
    executor_class: ExecutorClass
    policy_digest: str
    changed_paths: tuple[str, ...] = ()
    sides: tuple[Side, ...] = ("old", "new")
    budget_seconds: float | None = None
    """Seconds this entry may spend (plan estimate plus slack); ``None`` without a ceiling."""
    expected_current_revision_id: str = ""
    execution_intent: str = ""
    tier: Literal["auto", "hooks"] = "auto"


@dataclass(slots=True)
class ObservationRunReport:
    entry_id: str
    revision_id: str
    results: list[dict[str, Any]] = field(default_factory=list)
    executions: int = 0
    reused: int = 0
    limited_by_budget: bool = False
    notes: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "revision_id": self.revision_id,
            "results": list(self.results),
            "executions": self.executions,
            "reused": self.reused,
            "limited_by_budget": self.limited_by_budget,
            "notes": list(self.notes),
        }


def _hex(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def observation_spec(changed_paths: Sequence[str], *, tier: Literal["auto", "hooks"] = "auto") -> ObservationSpec:
    """Trace the changed Python files; everything else is observed for every file."""

    return ObservationSpec.from_wire(
        {"version": OBSERVATION_VERSION, "trace_paths": [p for p in changed_paths if p.endswith(".py")], "tier": tier}
    )


def observation_runtime(recipe: CheckRecipe, spec: ObservationSpec) -> tuple[RuntimeConfigEntry, ReviewSurface]:
    """The runner-registry inputs of one observed recipe run (never from head config)."""

    runtime_id = f"observe:{recipe.id}"
    values: dict[str, Any] = {
        "command": list(recipe.argv()),
        "root": recipe.cwd,
        "timeout": recipe.timeout,
        # The command may write its report to the fixed directory; keep it writable.
        "report": bool(recipe.report),
        "observe": spec,
    }
    definition = recipe.environment
    if definition is not None:
        # environment.toml: the check's services and pinned image, as an ordinary run of this recipe gets them.
        services = tuple(found for name in recipe.needs if (found := definition.service(name)) is not None)
        if services:
            values["services"] = ServiceSet(services)
        if definition.toolchain is not None and definition.toolchain.digest:
            values["toolchain"] = definition.toolchain
    runtime = RuntimeConfigEntry(id=runtime_id, runner="command", values=values)
    surface = ReviewSurface(
        id=f"observe:{recipe.id}",
        provider=OBSERVE_PROVIDER,
        kind="check",
        title=recipe.title or recipe.id,
        runtime=runtime_id,
        capabilities=("execute",),
        metadata={"recipe_digest": recipe.digest, "entry": RUNTIME_OBSERVATION_ENTRY},
    )
    return runtime, surface


def attempt_identity(inputs: ObservationRunInputs, recipe: CheckRecipe, side: Side, spec: ObservationSpec) -> str:
    """Stable identity of one observed attempt; a repeat request reuses the stored record."""

    return _hex(
        {
            "v": OBSERVATION_VERSION,
            "entry": RUNTIME_OBSERVATION_ENTRY,
            "review": inputs.revision.review_id,
            "revision": inputs.revision.id,
            "snapshot": snapshot_semantic_identity(inputs.revision),
            "side": side,
            "recipe": recipe.digest,
            "executor": inputs.executor_class.value,
            "policy": inputs.policy_digest,
            "observe": spec.to_wire(),
            "intent": inputs.execution_intent,
        }
    )


def _environment(record: Mapping[str, Any]) -> tuple[str, ExecutionResult | None]:
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    raw = data.get("execution") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        return "", None
    try:
        parsed = ExecutionResult.from_wire(raw)
    except (TypeError, ValueError):
        return "", None
    return parsed.environment_fingerprint, parsed


def _observation(record: Mapping[str, Any], side: str) -> RuntimeObservation | None:
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    raw = data.get("observation") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        observation = RuntimeObservation.from_wire(raw)
    except (TypeError, ValueError):
        return None
    return observation if observation.side == side else None


class _Run:
    def __init__(self, inputs: ObservationRunInputs, run_side: RunSide, meter: Meter) -> None:
        self.inputs = inputs
        self.run_side = run_side
        self.meter = meter
        self.report = ObservationRunReport(entry_id=RUNTIME_OBSERVATION_ENTRY, revision_id=inputs.revision.id)
        self.spent = 0.0

    def _publish(self, record: Mapping[str, Any], *, title: str, source_ref: str, detail: str) -> ReviewEvidence:
        inputs = self.inputs
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
                    title=title[:200],
                    artifact_path=rel,
                    content_hash=content_hash,
                    mime_type=RUNTIME_OBSERVATION_MEDIA_TYPE,
                    bytes=size,
                    source=_SOURCE,
                    source_ref=source_ref,
                    detail=detail[:1200],
                    # A raw observation: no verification status, no stable key (never a check result;
                    # run artifacts it references follow raw retention unless a judgment cites them).
                    execution_intent=inputs.execution_intent,
                    executor_class=inputs.executor_class.value,
                    execution_verification=execution_verification(record),
                ),
                expected_current_revision_id=inputs.expected_current_revision_id,
            )
        except ValueError as exc:
            inputs.store.discard_artifact(rel)
            raise ObservationRunSuperseded("runtime observation revision was superseded before publication") from exc
        except Exception:
            inputs.store.discard_artifact(rel)
            raise
        return added

    def attempt(self, recipe: CheckRecipe, side: Side, spec: ObservationSpec) -> tuple[ReviewEvidence, dict[str, Any]]:
        inputs = self.inputs
        identity = attempt_identity(inputs, recipe, side, spec)
        source_ref = f"runtime-observation:{identity}"
        existing: ReviewEvidence | None = inputs.store.get_evidence_by_source(
            inputs.revision.review_id, _SOURCE, source_ref
        )
        if (
            existing is not None
            and existing.revision_id == inputs.revision.id
            and existing.mime_type == RUNTIME_OBSERVATION_MEDIA_TYPE
        ):
            stored = read_record(inputs.store.read_evidence_artifact(existing), existing.content_hash)
            if stored is not None and stored.get("identity") == identity:
                self.report.reused += 1
                return existing, stored
        runtime, surface = observation_runtime(recipe, spec)
        started = time.monotonic()
        try:
            result = self.run_side(runtime, surface, side)
        except (OSError, RuntimeError, ValueError) as exc:
            result = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"observed run could not execute: {exc}"[:1000],
                "command",
                runtime.id,
            )
        wall = time.monotonic() - started
        self.spent += wall
        self.report.executions += 1
        data = result.data if isinstance(result.data, Mapping) else {}
        execution = data.get("execution")
        self.meter(execution if isinstance(execution, Mapping) else None, wall)
        status = "NOT_RUN" if result.status == "unavailable" else "PASS" if result.status == "passed" else "FAIL"
        observation = data.get("observation")
        stored_data: dict[str, Any] = {}
        if isinstance(execution, Mapping):
            stored_data["execution"] = dict(execution)
        if isinstance(observation, Mapping) and status != "NOT_RUN":
            stored_data["observation"] = dict(observation)
        record = stamp_execution_record(
            {
                "v": 1,
                "kind": "attempt",
                "identity": identity,
                "entry": RUNTIME_OBSERVATION_ENTRY,
                "recipe": {"id": recipe.id, "title": recipe.title or recipe.id, "digest": recipe.digest},
                "side": side,
                "status": status,
                "reason": protect_text(str(result.summary or "")[:1000])[0],
                "exit_code": result.exit_code,
                "duration_ms": int(result.duration_ms or 0),
                "output": protect_text(str(result.output or "")[-4000:])[0],
                "observe": spec.to_wire(),
                "data": stored_data,
            },
            inputs.executor_class,
        )
        evidence = self._publish(
            record,
            title=f"{recipe.title or recipe.id} · {_SIDE_NAME[side]} · observed run",
            source_ref=source_ref,
            detail=f"{status}: {record['reason']}",
        )
        return evidence, record

    def diff(
        self,
        recipe: CheckRecipe,
        attempts: Mapping[Side, tuple[ReviewEvidence, Mapping[str, Any]]],
    ) -> tuple[ReviewEvidence, dict[str, Any]]:
        sides: dict[str, Any] = {}
        environments: dict[str, str] = {}
        parsed: dict[str, ExecutionResult | None] = {}
        observations: dict[str, RuntimeObservation | None] = {}
        for side, (item, attempt_record) in attempts.items():
            name = _SIDE_NAME[side]
            environment, execution = _environment(attempt_record)
            observation = _observation(attempt_record, side)
            environments[name], parsed[name], observations[name] = environment, execution, observation
            sides[name] = {
                "status": str(attempt_record.get("status") or ""),
                "reason": str(attempt_record.get("reason") or "")[:500],
                "evidence_id": item.id,
                "evidence_ref": f"check:{item.source}:{item.source_ref}",
                "environment": environment,
                "observation": None if observation is None else observation.to_wire(),
            }
        base, head = observations.get("base"), observations.get("head")
        reasons: list[str] = []
        diff: dict[str, Any]
        if base is None or head is None:
            missing = [name for name in ("base", "head") if observations.get(name) is None]
            reasons.append(f"no observation for {', '.join(missing)}: not comparable")
            diff = {"comparable": False, "reasons": list(reasons), "differences": [], "changes": [], "unchanged": 0}
        else:
            base_fp, head_fp = parsed.get("base"), parsed.get("head")
            differences = (
                base_fp.fingerprint.differences(head_fp.fingerprint)
                if base_fp is not None and head_fp is not None
                else ("environment fingerprint",)
            )
            result = diff_observations(
                base,
                head,
                base_environment=environments.get("base", ""),
                head_environment=environments.get("head", ""),
                differences=differences,
            )
            diff = result.to_wire()
            reasons.extend(result.reasons)
        record: dict[str, Any] = {
            "v": 1,
            "kind": "diff",
            "entry": RUNTIME_OBSERVATION_ENTRY,
            "recipe": {"id": recipe.id, "title": recipe.title or recipe.id, "digest": recipe.digest},
            "executor_class": self.inputs.executor_class.value,
            "environment": environments,
            "sides": sides,
            "diff": diff,
            "limitations": list(LIMITATIONS),
        }
        digest = _hex(record)
        source_ref = f"runtime-observation-diff:{recipe.id}:{digest}"
        existing: ReviewEvidence | None = self.inputs.store.get_evidence_by_source(
            self.inputs.revision.review_id, _SOURCE, source_ref
        )
        if existing is not None and existing.revision_id == self.inputs.revision.id:
            return existing, record
        changes = len(diff.get("changes") or ())
        summary = (
            f"{changes} runtime side-effect difference(s)"
            if diff.get("comparable")
            else "not comparable: " + "; ".join(reasons)[:600]
        )
        evidence = self._publish(
            record,
            title=f"{recipe.title or recipe.id} · runtime side effects · base vs head",
            source_ref=source_ref,
            detail=summary,
        )
        return evidence, record


def run_observation_entry(inputs: ObservationRunInputs, run_side: RunSide, meter: Meter) -> ObservationRunReport:
    """Run the admitted ``side_effects.runtime`` entry and publish its records.

    Recipes run in catalog order; once the entry's budget is spent the rest
    are reported *Limited by budget*, never silently dropped.
    """

    run = _Run(inputs, run_side, meter)
    report = run.report
    spec = observation_spec(inputs.changed_paths, tier=inputs.tier)
    if not inputs.recipes:
        report.notes.append(
            "no trusted-base test recipe to observe; add check_recipes (kind: test) to .lemoncrow/review.yaml"
        )
        return report
    if inputs.sides != ("old", "new"):
        report.notes.append("runtime side effects compare base and head; the plan scheduled only one side")
    for index, recipe in enumerate(inputs.recipes):
        if index and inputs.budget_seconds is not None:
            estimate = run.spent / index
            if run.spent + estimate > inputs.budget_seconds:
                report.limited_by_budget = True
                skipped = [item.id for item in inputs.recipes[index:]]
                report.notes.append(f"Limited by budget: not observed: {', '.join(skipped)}")
                for item in inputs.recipes[index:]:
                    report.results.append(
                        {"recipe": {"id": item.id, "title": item.title or item.id}, "status": "limited_by_budget"}
                    )
                break
        attempts: dict[Side, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
        for side in inputs.sides:
            attempts[side] = run.attempt(recipe, side, spec)
        evidence, record = run.diff(recipe, attempts)
        report.results.append({**record, "status": "observed", "evidence_id": evidence.id})
    return report


def latest_observations(store: Any, revision: ReviewRevision) -> dict[str, dict[str, Any]]:
    """Newest verified diff record per recipe on the exact Snapshot."""

    out: dict[str, dict[str, Any]] = {}
    rows = sorted(
        (
            item
            for item in store.list_evidence(revision.review_id)
            if item.revision_id == revision.id
            and item.mime_type == RUNTIME_OBSERVATION_MEDIA_TYPE
            and item.execution_intent != "explicit_historical"
        ),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    for item in rows:
        record = read_record(store.read_evidence_artifact(item), item.content_hash)
        if record is None or record.get("kind") != "diff":
            continue
        recipe = record.get("recipe")
        recipe_id = str(recipe.get("id") or "") if isinstance(recipe, Mapping) else ""
        if recipe_id and recipe_id not in out:
            out[recipe_id] = {**record, "evidence_id": item.id, "observed_at": item.created_at}
    return out


__all__ = [
    "ObservationRunInputs",
    "ObservationRunReport",
    "ObservationRunSuperseded",
    "attempt_identity",
    "latest_observations",
    "observation_runtime",
    "observation_spec",
    "run_observation_entry",
]
