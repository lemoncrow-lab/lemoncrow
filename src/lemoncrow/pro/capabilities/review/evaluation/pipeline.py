"""Replay one corpus case through the real Review pipeline, hermetically.

Per (case, selection) a throwaway workspace holds a real git repository, a
real SQLite Review store, and the same components the Review API composes.
Static analysis and AI run through their normal product paths. Checks,
artifact analyses and runtime observation also run through their normal
product entry functions; only their final ``RunSide`` process boundary is
replaced by an exact recorded :class:`SurfaceRunResult` fixture.

Replay never starts reviewed-code processes. A missing or stale execution
fixture fails closed as a fixture event and leaves the Review incomplete.
"""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from lemoncrow.pro.capabilities.review.capability_catalog import PRESETS, valid_entry_id
from lemoncrow.pro.capabilities.review.evaluation.corpus import REVIEW_CONFIG_PATH, EvalCase
from lemoncrow.pro.capabilities.review.evaluation.fixtures import FixtureEvent, ServedCall
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution

#: Entries always executed by the harness itself. Recorded execution fixtures add
#: checks/analyses/runtime observations through their product entry functions.
HERMETIC_ENTRIES: Final[frozenset[str]] = frozenset({"side_effects.static", "ai.investigators", "ai.hypotheses"})
#: The runtime's Review-wide model-call ceiling default (``ModelCallBudget``),
#: fixed here so a developer's environment cannot change a replay.
DEFAULT_MAX_REVIEW_CALLS: Final[int] = 20
_NOT_EXECUTED_NOTE: Final[str] = "no hermetic executor: no recorded execution fixture; estimated cost only"
_SIGNATURE_TIME: Final[int] = 1_700_000_000
_STORE_TEMPLATE_LOCK = threading.Lock()
_STORE_TEMPLATE: dict[str, tuple[Path, Path]] = {}


class EvalExecutor(Protocol):
    served: list[ServedCall]
    events: list[FixtureEvent]

    def __call__(self, **kwargs: Any) -> StructuredExecution: ...


class EvalExecutionRunner(Protocol):
    """Case-scoped recorded execution source; product code consumes only ``RunSide``."""

    events: list[FixtureEvent]

    def covers(self, entry_id: str) -> bool: ...

    def for_entry(self, entry_id: str) -> Callable[[Any, Any, str], Any]: ...


#: Run-settings policy keys a gate selection may sweep (P11-ESC escalation thresholds
#: and the rule-check ceiling), so Gate I measures catch rate against cost per value.
SWEPT_POLICY_KEYS: Final[tuple[str, ...]] = ("escalation", "rule_checks")


@dataclass(frozen=True, slots=True)
class Selection:
    """One depth preset or Custom selection the corpus is replayed under."""

    label: str
    effort: str
    custom_from: str = "auto"
    entries: tuple[str, ...] = ()
    #: Swept run-settings policy data (``SWEPT_POLICY_KEYS``) on top of the depth.
    policy: Mapping[str, Any] = field(default_factory=dict, hash=False)

    @classmethod
    def swept(cls, name: str, effort: str, policy: Mapping[str, Any]) -> Selection:
        """A depth preset with policy data, e.g. ``escalation: {to: deep}`` (label ``policy:<name>``)."""

        from lemoncrow.pro.capabilities.review.run_plan import SettingOrigin, parse_run_settings

        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"policy selection name {name!r} must be alphanumeric (with - or _)")
        if effort not in PRESETS:
            raise ValueError(f"policy selection {name}: effort must be one of {list(PRESETS)}")
        unknown = sorted(set(policy) - set(SWEPT_POLICY_KEYS))
        if unknown or not policy:
            raise ValueError(f"policy selection {name} may set only {list(SWEPT_POLICY_KEYS)}")
        layer = parse_run_settings(dict(policy), origin=SettingOrigin("organization", f"selection {name}"))
        if layer.errors:
            raise ValueError(f"policy selection {name}: {layer.errors[0]}")
        return cls(label=f"policy:{name}", effort=effort, policy=layer.to_document())

    @classmethod
    def preset(cls, name: str) -> Selection:
        if name not in PRESETS:
            raise ValueError(f"unknown depth {name!r}; expected one of {list(PRESETS)}")
        return cls(label=name, effort=name)

    @classmethod
    def custom(cls, name: str, custom_from: str, entries: Sequence[str]) -> Selection:
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"custom selection name {name!r} must be alphanumeric (with - or _)")
        if custom_from not in PRESETS:
            raise ValueError(f"custom selection {name}: custom_from must be one of {list(PRESETS)}")
        rows = tuple(entries)
        if not rows:
            raise ValueError(f"custom selection {name} selects nothing")
        bad = [entry for entry in rows if not valid_entry_id(entry)]
        if bad:
            raise ValueError(f"custom selection {name}: unknown catalog ids {bad}")
        return cls(label=f"custom:{name}", effort="custom", custom_from=custom_from, entries=rows)

    def settings_document(self) -> dict[str, Any]:
        policy = {key: self.policy[key] for key in SWEPT_POLICY_KEYS if key in self.policy}
        if self.effort != "custom":
            return {"effort": self.effort, **policy}
        return {"effort": "custom", "custom_from": self.custom_from, "custom": list(self.entries), **policy}

    def to_wire(self) -> dict[str, Any]:
        return {"label": self.label, **self.settings_document()}


@dataclass(frozen=True, slots=True)
class Reviewer:
    """The AI route the replay asks for; fixtures must have been recorded with it."""

    driver: str
    model: str

    def to_wire(self) -> dict[str, str]:
        return {"driver": self.driver, "model": self.model}


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: str
    family: str
    status: str
    executor: str
    estimated_minutes: float
    estimated_model_calls: int
    executed: bool
    note: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "status": self.status,
            "executor": self.executor,
            "estimated_minutes": round(self.estimated_minutes, 6),
            "estimated_model_calls": self.estimated_model_calls,
            "executed": self.executed,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ObservedFinding:
    """One finding the Review recorded; ``ref`` is its stable semantic identity."""

    ref: str
    source: str
    entry: str
    path: str
    start_line: int
    end_line: int
    category: str
    severity: str | None
    title: str
    surfaced: bool
    annotation_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "source": self.source,
            "entry": self.entry,
            "path": self.path,
            "lines": [self.start_line, self.end_line],
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "surfaced": self.surfaced,
            "annotation_id": self.annotation_id,
        }


@dataclass(frozen=True, slots=True)
class ObservedBehavior:
    ref: str
    kind: str
    subject: str
    change: str
    severity: str | None
    critical: bool
    paths: tuple[str, ...]
    entry: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "subject": self.subject,
            "change": self.change,
            "severity": self.severity,
            "critical": self.critical,
            "paths": list(self.paths),
            "entry": self.entry,
        }


@dataclass(frozen=True, slots=True)
class CaseRun:
    """The Review record of one case under one selection."""

    case_id: str
    selection: str
    review_id: str = ""
    revision_id: str = ""
    plan_digest: str = ""
    plan: tuple[PlanRow, ...] = ()
    findings: tuple[ObservedFinding, ...] = ()
    behavior: tuple[ObservedBehavior, ...] = ()
    static_status: str = ""
    ai: Mapping[str, Any] = field(default_factory=dict)
    model_calls: tuple[ServedCall, ...] = ()
    measured_compute_minutes: float = 0.0
    wall_seconds: float = 0.0
    fixture_events: tuple[FixtureEvent, ...] = ()
    errors: tuple[str, ...] = ()
    store_path: str = ""

    @property
    def complete(self) -> bool:
        return not self.fixture_events and not self.errors

    def to_wire(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "selection": self.selection,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "plan_digest": self.plan_digest,
            "plan": [row.to_wire() for row in self.plan],
            "findings": [row.to_wire() for row in self.findings],
            "behavior": [row.to_wire() for row in self.behavior],
            "static_status": self.static_status,
            "ai": dict(self.ai),
            "model_calls": [row.to_wire() for row in self.model_calls],
            "measured_compute_minutes": round(self.measured_compute_minutes, 6),
            "wall_seconds": round(self.wall_seconds, 3),
            "fixture_events": [row.to_wire() for row in self.fixture_events],
            "errors": list(self.errors),
            "store_path": self.store_path,
        }


# --------------------------------------------------------------------------- #
# workspace
# --------------------------------------------------------------------------- #


def _commit(repo: Any, root: Path, message: str, parents: list[Any], when: int) -> Any:
    import pygit2

    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("LemonCrow Review Evaluation", "review-eval@lemoncrow.invalid", when, 0)
    return repo.create_commit("HEAD", signature, signature, message, repo.index.write_tree(), parents)


def materialize(case: EvalCase, root: Path) -> tuple[Any, str, str]:
    """A real repository with the case's base and head commits; returns (repo, base sha, head sha)."""

    import pygit2

    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    base_files = dict(case.base)
    if case.review_config is not None:
        base_files[REVIEW_CONFIG_PATH] = case.review_config
    for rel, text in base_files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    base = _commit(repo, root, f"base: {case.id}", [], _SIGNATURE_TIME)
    for rel, head_text in case.head.items():
        target = root / rel
        if head_text is None:
            target.unlink(missing_ok=True)
            repo.index.remove(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(head_text, encoding="utf-8")
    head = _commit(repo, root, f"head: {case.id}", [base], _SIGNATURE_TIME + 60)
    return repo, str(base), str(head)


def _store_template() -> tuple[Path, Path]:
    """One freshly initialized Review store per process: ``(root, database path relative to it)``.

    Building the schema and running its migrations dominates a case's replay
    time, so every case starts from a backup of this database instead. The
    copy is then opened with ``ReviewStore.init()``, which is idempotent on an
    initialized database, so each case still sees exactly what ``init`` builds.
    The directory is removed when the process exits.
    """

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    with _STORE_TEMPLATE_LOCK:
        cached = _STORE_TEMPLATE.get("store")
        if cached is not None and (cached[0] / cached[1]).is_file():
            return cached
        root = Path(tempfile.mkdtemp(prefix="lc-review-eval-store-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        seed = ReviewStore(root)
        seed.init()
        # ReviewStore uses WAL mode. Consolidate the initialized schema into the
        # main database once so per-case stores can be plain file copies instead
        # of 55 SQLite backup operations during a built-in replay.
        with sqlite3.connect(seed.db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        cached = (root, Path(seed.db_path).resolve().relative_to(root.resolve()))
        _STORE_TEMPLATE["store"] = cached
        return cached


def _fresh_store(store_root: Path) -> Any:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    class _EvalReviewStore(ReviewStore):
        """Throwaway Review store tuned for hermetic evaluation replay only.

        Replay databases live under a temporary workdir and are discarded after
        each case, so crash durability has no product value here. Keep normal
        ReviewStore WAL/foreign-key/busy-timeout behavior, but skip fsyncs on
        idle connections. Never change the setting on an already-active nested
        transaction: SQLite rejects that and nested Review operations share the
        pinned connection.
        """

        def _connect(self) -> sqlite3.Connection:
            conn = super()._connect()
            if not conn.in_transaction:
                conn.execute("PRAGMA synchronous = OFF")
            return conn

    template_root, relative = _store_template()
    target = store_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_root / relative, target)
    store = _EvalReviewStore(store_root)
    store.init()
    return store


def _snapshot_text(store: Any, repo: Any, packet: Mapping[str, Any], revision: Any, path: str, side: str) -> str | None:
    """One exact Snapshot document, read the way the Review API reads it.

    Persisted source trees first, then the new-side blob artifact, then the
    repository at the revision's base/head commit (``""`` when absent there).
    """

    from lemoncrow.pro.capabilities.review.ask import exact_revision_sources
    from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _tree_for_sha

    exact_side: Literal["old", "new"] = "old" if side == "old" else "new"
    if store.read_source_tree_artifact(revision.review_id, revision.id, side=exact_side) is not None:
        return exact_revision_sources(store, revision, paths=(path,), side=exact_side).get(path, "")
    if exact_side == "new":
        blobs = store.read_blob_artifact(revision.review_id, revision.id) or {}
        if path in blobs:
            return str(blobs[path])
        for row in packet.get("files") or ():
            if isinstance(row, Mapping) and str(row.get("path") or "") == path:
                if str(row.get("status") or "") == "deleted":
                    return ""
                break
    sha = revision.base_sha if exact_side == "old" else revision.head_sha
    if not sha:
        return None
    try:
        git_tree = _tree_for_sha(repo, sha)
        git_tree / path
    except KeyError:
        return ""
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    data, _reason = _blob_from_tree(repo, git_tree, path)
    return None if data is None else str(_decode(data))


def _policy(selection: Selection) -> dict[str, Any]:
    """An effective Review policy whose organization run-settings layer is *selection*."""

    return {
        "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
        "run_plan": {
            "layers": [
                {
                    "origin": {"layer": "organization", "source": f"review evaluation selection {selection.label}"},
                    "settings": selection.settings_document(),
                }
            ]
        },
    }


# --------------------------------------------------------------------------- #
# one case
# --------------------------------------------------------------------------- #


def replay_case(
    case: EvalCase,
    selection: Selection,
    *,
    executor: EvalExecutor,
    reviewer: Reviewer,
    execution_runner: EvalExecutionRunner | None = None,
    workdir: Path | None = None,
) -> CaseRun:
    """Run *case* under *selection*; never raises for a pipeline failure (it is recorded)."""

    started = time.monotonic()
    if workdir is not None:
        return _replay(
            case,
            selection,
            executor=executor,
            reviewer=reviewer,
            execution_runner=execution_runner,
            workdir=workdir,
            started=started,
        )
    with tempfile.TemporaryDirectory(prefix="lc-review-eval-") as tmp:
        return _replay(
            case,
            selection,
            executor=executor,
            reviewer=reviewer,
            execution_runner=execution_runner,
            workdir=Path(tmp),
            started=started,
        )


def _replay(
    case: EvalCase,
    selection: Selection,
    *,
    executor: EvalExecutor,
    reviewer: Reviewer,
    execution_runner: EvalExecutionRunner | None,
    workdir: Path,
    started: float,
) -> CaseRun:
    try:
        run = _pipeline(
            case, selection, executor=executor, reviewer=reviewer, execution_runner=execution_runner, workdir=workdir
        )
    except Exception as exc:  # one broken case must not hide the others' measurements
        run = CaseRun(
            case_id=case.id,
            selection=selection.label,
            errors=(f"{type(exc).__name__}: {str(exc)[:500]}",),
            model_calls=tuple(executor.served),
        )
    from dataclasses import replace

    execution_events = () if execution_runner is None else tuple(execution_runner.events)
    return replace(
        run,
        wall_seconds=time.monotonic() - started,
        fixture_events=tuple(executor.events) + execution_events,
        model_calls=tuple(executor.served),
    )


def _pipeline(
    case: EvalCase,
    selection: Selection,
    *,
    executor: EvalExecutor,
    reviewer: Reviewer,
    execution_runner: EvalExecutionRunner | None,
    workdir: Path,
) -> CaseRun:
    from lemoncrow.pro.capabilities.review.ai_review import generate_candidates, run_independent_review
    from lemoncrow.pro.capabilities.review.artifact_analyses import (
        AnalysisRunInputs,
        ArtifactAnalysisProducer,
        run_analysis_entry,
    )
    from lemoncrow.pro.capabilities.review.behavior import BehaviorContext, behavior_key, collect_behavior
    from lemoncrow.pro.capabilities.review.check_runs import CheckRunInputs, run_check_entry
    from lemoncrow.pro.capabilities.review.criticality import (
        normalize_finding_severity,
        target_policy_reasons,
        triage_policy_for,
    )
    from lemoncrow.pro.capabilities.review.differential import CheckDifferentialProducer
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
    from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.review_priority import reprioritize
    from lemoncrow.pro.capabilities.review.review_triage import current_findings
    from lemoncrow.pro.capabilities.review.revisions import compute_frontier
    from lemoncrow.pro.capabilities.review.routine import routine_proofs
    from lemoncrow.pro.capabilities.review.rules import resolve_review_governance
    from lemoncrow.pro.capabilities.review.run_plan import ai_stage_plan
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService, git_tree_paths
    from lemoncrow.pro.capabilities.review.runner_host import revision_snapshot_identity
    from lemoncrow.pro.capabilities.review.runtime_observation import RuntimeObservationProducer
    from lemoncrow.pro.capabilities.review.runtime_observation_runs import ObservationRunInputs, run_observation_entry
    from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest
    from lemoncrow.pro.capabilities.review.side_effects import (
        STATIC_ENTRY_ID,
        ChangedPath,
        StaticSideEffectService,
        snapshot_base_tree,
    )
    from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
    from lemoncrow.pro.capabilities.review.targets import derive_review_targets, order_review_targets
    from lemoncrow.pro.capabilities.review.test_impact import selections_from_document
    from lemoncrow.pro.capabilities.review.trusted_base import resolve_trusted_base_document
    from lemoncrow.pro.environment.execution import ExecutorClass

    repo_root = workdir / "repo"
    store_root = workdir / "store"
    repo, base_sha, head_sha = materialize(case, repo_root)
    store = _fresh_store(store_root)

    # 1. capture
    rng = resolve_rev_range(repo_root, base=base_sha, head=head_sha)
    build = build_review_packet_with_blobs(
        repo_root, rng, store_root=store_root, with_impact=False, with_provenance=False, with_patch_text=True
    )
    session = open_or_create_session(store, repo_root, rng, title=case.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    packet = build.packet.to_dict()

    def read(rev: Any, path: str, side: str) -> str | None:
        return _snapshot_text(store, repo, packet, rev, path, side)

    config = resolve_trusted_base_document(REVIEW_CONFIG_PATH, case.review_config or "")
    policy = _policy(selection)
    triage = triage_policy_for(policy, config, commit=base_sha)
    # P11-ESC: trusted-base Review rules, as the API resolves them, so a case's rules are
    # asked as focused rule checks and the rule-check ceiling is measurable.
    governance = resolve_review_governance(
        config,
        read_base_document=lambda path: resolve_trusted_base_document(path, read(revision, path, "old") or ""),
    )

    # 2. targets, as the API's canonical queue derives them
    units = store.list_units(revision.id)
    frontier = compute_frontier(
        session.id,
        session.reviewer_id,
        revision,
        units,
        store.list_marks(session.id, reviewer_id=session.reviewer_id),
        annotations=(),
    )
    base_targets = derive_review_targets(units, frontier.entries, packet)
    proofs = routine_proofs(base_targets, packet, lambda path, side: read(revision, path, side))
    policy_reasons = target_policy_reasons(base_targets, triage)
    targets = (
        reprioritize(base_targets, routine_proofs=proofs, policy_reasons=policy_reasons)
        if proofs or policy_reasons
        else base_targets
    )
    ordered = order_review_targets(targets)
    changed = tuple(str(row.get("path") or "") for row in packet.get("files") or () if row.get("path"))

    # 3. effective run plan
    base_tree = repo[base_sha].tree
    service = RunPlanService(
        store=store,
        policy=lambda _session, _revision: {"effective_review": policy},
        review_config=lambda _revision: config,
        read_base=lambda rev, path: read(rev, path, "old"),
        read_head=lambda rev, path: read(rev, path, "new"),
        base_paths=lambda _revision: list(git_tree_paths(repo, base_tree)),
        changed_paths=lambda _revision: changed,
        browser_policy=lambda _revision: None,
        review_rules=lambda _session, _revision: governance.rules,
    )
    candidates = generate_candidates(ordered)
    plan = service.current(session, revision, ai_candidates=len(candidates))

    # 4. static analysis, only when planned
    static = StaticSideEffectService(
        store=store,
        plan=service.peek,
        changed=lambda _revision: [
            ChangedPath(
                path=str(row.get("path") or ""),
                old_path=str(row.get("old_path") or ""),
                status=str(row.get("status") or "modified"),
            )
            for row in packet.get("files") or ()
            if row.get("path")
        ],
        read_text=lambda rev, path, side: read(rev, path, side),
        base_tree=lambda rev: snapshot_base_tree(store, rev, (repo_root,)),
        recorder=service.compute.recorder,
    )
    report = static.report(session, revision, latest=True)

    # 5. AI review, as the persisted plan's AI stage says.
    stage = ai_stage_plan(plan)
    ai_wire: dict[str, Any] = {"status": "not_planned", "plan_status": stage.status, "reason": stage.reason}
    executed_ai: tuple[str, ...] = ()
    if stage.runs_ai:
        result = run_independent_review(
            store=store,
            revision=revision,
            targets=ordered,
            repo_root=repo_root,
            effort=stage.effort,
            driver=reviewer.driver,
            model=reviewer.model,
            rules=governance.rules,
            content_access=governance.content_access,
            budget=ModelCallBudget(max_calls=1, max_review_calls=DEFAULT_MAX_REVIEW_CALLS),
            structured_executor=executor,
            stage_plan=stage,
        )
        executed_ai = result.executed_entries
        ai_wire = {
            "status": result.status,
            "effort": result.effort,
            "candidates": result.candidates,
            "hypotheses": result.hypotheses,
            "confirmed": result.confirmed,
            "not_confirmed": result.not_confirmed,
            "unknown": result.unknown,
            "executed_entries": list(result.executed_entries),
            "provider_errors": list(result.provider_errors),
        }

    if execution_runner is not None:
        binder = getattr(execution_runner, "bind_context", None)
        if callable(binder):
            binder(
                repo_root=repo_root,
                store_root=store_root,
                revision=revision,
                changed_paths=changed,
                store=store,
            )

    # 6. Recorded execution fixtures. Product orchestration/persistence/diffing
    # remains real; only the final process boundary is replaced by RunSide.
    executed_runtime: set[str] = set()
    execution_behavior: list[ObservedBehavior] = []
    seen_behavior = {behavior_key(delta) for delta in (report.deltas if report.status == "ran" else ())}
    policy_digest = trusted_policy_digest(case.review_config or "", base_known=True)
    recipe_catalog = service.recipe_catalog(revision)
    analysis_catalog = service.analysis_catalog(revision)

    def collect_new(entry_id: str, producer: Any) -> None:
        collection = collect_behavior(
            BehaviorContext(
                review_id=session.id,
                revision=revision,
                evidence=tuple(store.list_evidence(session.id)),
                read_artifact=store.read_evidence_artifact,
                changed_paths=changed,
            ),
            producers=(producer,),
        )
        for delta in collection.deltas:
            ref = behavior_key(delta)
            if ref in seen_behavior:
                continue
            seen_behavior.add(ref)
            execution_behavior.append(
                ObservedBehavior(
                    ref=ref,
                    kind=delta.kind,
                    subject=delta.subject,
                    change=delta.change,
                    severity=normalize_finding_severity("behavior", raw=delta.severity, critical=delta.critical),
                    critical=delta.critical,
                    paths=tuple(delta.paths),
                    entry=entry_id,
                )
            )

    test_diff_running = any(row.id == "behavior.test_diff" and row.status == "run" for row in plan.entries)
    if execution_runner is not None:
        for entry in plan.entries:
            if entry.status != "run" or not execution_runner.covers(entry.id):
                continue
            sides: tuple[Literal["old", "new"], ...] = ("old", "new") if entry.sides == "base+head" else ("new",)
            run_side = execution_runner.for_entry(entry.id)

            def meter(_execution: Mapping[str, Any] | None, _wall: float) -> None:
                return None

            if entry.id == "side_effects.runtime":
                run_observation_entry(
                    ObservationRunInputs(
                        store=store,
                        revision=revision,
                        recipes=recipe_catalog.for_entry("checks.test"),
                        executor_class=ExecutorClass.AUTHOR_MACHINE,
                        policy_digest=policy_digest,
                        changed_paths=changed,
                        sides=("old", "new"),
                        expected_current_revision_id=revision.id,
                        execution_intent="current",
                    ),
                    run_side,
                    meter,
                )
                executed_runtime.add(entry.id)
                collect_new(entry.id, RuntimeObservationProducer())
                continue
            if recipe_catalog.for_entry(entry.id):
                check_report = run_check_entry(
                    CheckRunInputs(
                        store=store,
                        revision=revision,
                        catalog=recipe_catalog,
                        entry_id=entry.id,
                        sides=sides,
                        executor_class=ExecutorClass.AUTHOR_MACHINE,
                        policy_digest=policy_digest,
                        changed_paths=changed,
                        read_base=lambda path: read(revision, path, "old"),
                        read_head=lambda path: read(revision, path, "new"),
                        head_identity=revision_snapshot_identity(revision, "new"),
                        expected_current_revision_id=revision.id,
                        execution_intent="current",
                        selection=selections_from_document(entry.selection),
                        base_on_demand=entry.base_on_demand,
                    ),
                    run_side,
                    meter,
                )
                executed_runtime.add(entry.id)
                # A head-only check FAIL is already a product verdict even when no
                # base/head differential is planned. Project that persisted verdict
                # into Gate I so checks.test can receive credit at Quick/Auto.
                if entry.id == "checks.test" and check_report.verdicts.get("checks.test") == "FAIL":
                    execution_behavior.append(
                        ObservedBehavior(
                            ref=f"check-verdict:{entry.id}:FAIL",
                            kind="test_outcome",
                            subject=f"{entry.id}:FAIL",
                            change="changed",
                            severity="p1",
                            critical=True,
                            paths=(),
                            entry=entry.id,
                        )
                    )
                behavior_owner = "behavior.test_diff" if entry.id == "checks.test" and test_diff_running else entry.id
                collect_new(behavior_owner, CheckDifferentialProducer())
                continue
            if analysis_catalog.for_entry(entry.id):
                run_analysis_entry(
                    AnalysisRunInputs(
                        store=store,
                        revision=revision,
                        catalog=analysis_catalog,
                        entry_id=entry.id,
                        sides=sides,
                        executor_class=ExecutorClass.AUTHOR_MACHINE,
                        policy_digest=policy_digest,
                        changed_paths=changed,
                        read_base=lambda path: read(revision, path, "old"),
                        read_side=lambda path, side: read(revision, path, side),
                        base_paths=lambda: service.base_paths(revision),
                        depth=entry.depth,
                        base_identity=revision_snapshot_identity(revision, "old"),
                        expected_current_revision_id=revision.id,
                        execution_intent="current",
                    ),
                    run_side,
                    meter,
                )
                executed_runtime.add(entry.id)
                collect_new(entry.id, ArtifactAnalysisProducer())

    annotations = store.list_annotations(session.id)
    by_id = {annotation.id: annotation for annotation in annotations}
    current, _stale = current_findings(
        store, revision_id=revision.id, annotations=annotations, threshold=triage.finding_threshold
    )
    findings = tuple(
        sorted(
            (
                ObservedFinding(
                    ref=f"ai_review:{by_id[row.annotation_id].source_id or row.annotation_id}",
                    source="ai_review",
                    # A rule the plan's rule checks ask is never investigated again (P11-ESC).
                    entry=(
                        "ai.rule_checks"
                        if set(by_id[row.annotation_id].rule_refs) & set(stage.rule_checks)
                        else "ai.investigators"
                    ),
                    path=by_id[row.annotation_id].anchor.path,
                    start_line=by_id[row.annotation_id].anchor.start_line,
                    end_line=by_id[row.annotation_id].anchor.end_line,
                    category=row.category,
                    severity=row.severity,
                    title=row.title,
                    surfaced=row.surfaced,
                    annotation_id=row.annotation_id,
                )
                for row in current
                if row.annotation_id in by_id
            ),
            key=lambda item: item.ref,
        )
    )
    behavior = tuple(
        sorted(
            [
                *(
                    ObservedBehavior(
                        ref=behavior_key(delta),
                        kind=delta.kind,
                        subject=delta.subject,
                        change=delta.change,
                        severity=normalize_finding_severity("behavior", raw=delta.severity, critical=delta.critical),
                        critical=delta.critical,
                        paths=tuple(delta.paths),
                        entry=STATIC_ENTRY_ID,
                    )
                    for delta in (report.deltas if report.status == "ran" else ())
                ),
                *execution_behavior,
            ],
            key=lambda item: (item.kind, item.subject, item.ref),
        )
    )

    executed = {STATIC_ENTRY_ID} if report.status == "ran" else set()
    executed.update(executed_runtime)
    executed.update(executed_ai)

    def note_for(entry_id: str, status: str) -> str:
        if status != "run" or entry_id in executed:
            return ""
        if entry_id == STATIC_ENTRY_ID:
            return f"static analysis {report.status}: {report.reason}".rstrip(": ")
        if entry_id in HERMETIC_ENTRIES:
            return f"planned; the AI stage did not need it ({ai_wire.get('status')})"
        return _NOT_EXECUTED_NOTE

    plan_rows = tuple(
        PlanRow(
            id=row.id,
            family=row.family,
            status=row.status,
            executor=row.executor,
            estimated_minutes=row.estimated_minutes,
            estimated_model_calls=row.estimated_model_calls,
            executed=row.id in executed,
            note=note_for(row.id, row.status),
        )
        for row in sorted(plan.entries, key=lambda item: item.id)
    )
    return CaseRun(
        case_id=case.id,
        selection=selection.label,
        review_id=session.id,
        revision_id=revision.id,
        plan_digest=plan.digest,
        plan=plan_rows,
        findings=findings,
        behavior=behavior,
        static_status=report.status,
        ai=ai_wire,
        measured_compute_minutes=service.compute.minutes([revision.id]),
        store_path=str(store_root),
    )


def run_selection(
    cases: Sequence[EvalCase],
    selection: Selection,
    *,
    executor_for: Callable[[EvalCase], EvalExecutor],
    reviewer: Reviewer,
    execution_runner_for: Callable[[EvalCase], EvalExecutionRunner | None] | None = None,
    keep_dir: Path | None = None,
) -> list[CaseRun]:
    runs: list[CaseRun] = []
    for case in cases:
        workdir = None if keep_dir is None else keep_dir / selection.label.replace(":", "-") / case.id
        runs.append(
            replay_case(
                case,
                selection,
                executor=executor_for(case),
                reviewer=reviewer,
                execution_runner=execution_runner_for(case) if execution_runner_for is not None else None,
                workdir=workdir,
            )
        )
    return runs


__all__ = [
    "DEFAULT_MAX_REVIEW_CALLS",
    "HERMETIC_ENTRIES",
    "CaseRun",
    "EvalExecutionRunner",
    "EvalExecutor",
    "ObservedBehavior",
    "ObservedFinding",
    "PlanRow",
    "Reviewer",
    "Selection",
    "materialize",
    "replay_case",
    "run_selection",
]
