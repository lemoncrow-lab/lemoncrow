"""Planned execution of the artifact and infrastructure analyses (Phase 9R items 15, 31, 32, 33).

One call executes one admitted run-plan entry (``migrations.execute``,
``artifacts.regenerate``, ``artifacts.shipped``, ``infra.render``) on one exact
Snapshot through the R4 runner registry and publishes:

* one raw attempt record per spec and side (runtime-validation records of
  scenario kind ``recipe``, never verification results), reused on the same
  Snapshot only when the attempt identity matches and its heavy artifacts are
  still readable; the dispatcher -- never the driver -- states the side, so a
  head run can never stand in for a base result;
* one analysis record per spec (:data:`ANALYSIS_MEDIA_TYPE`) with the verdict,
  the findings (the input of the behavior producer below) and references to
  the redacted heavy artifacts (schema dumps, rendered manifests, plans,
  diffs) in the R4 run-artifact store, which expire unless a judgment or
  finding cites them;
* one typed ``lemoncrow_verification`` verdict per entry (stable key = entry
  id) carrying the executor class and environment fingerprint.

Nothing here decides whether to run: callers admit the plan entry first.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

from lemoncrow.pro.capabilities.review import (
    compat_mixed_version,
    flag_matrix,
    infra_render,
    migration_execution,
    regenerate_compare,
    shipped_artifacts,
)
from lemoncrow.pro.capabilities.review.analysis_common import (
    ANALYSIS_ENTRIES,
    ANALYSIS_MEDIA_TYPE,
    ANALYSIS_PRODUCER_ID,
    ANALYSIS_PROVIDER,
    COMPAT_ENTRY,
    CONFIG_KEYS,
    ENTRY_FACT,
    ENTRY_KIND,
    FLAG_MATRIX_ENTRY,
    INFRA_ENTRY,
    MAX_SPECS_PER_ENTRY,
    MIGRATIONS_ENTRY,
    REGENERATE_ENTRY,
    REPORT_LIMIT,
    SHIPPED_ENTRY,
    AnalysisCatalog,
    AnalysisSpec,
    Evaluation,
    Finding,
    Side,
    SideRun,
    decode_report,
    driver_command,
    payload_problem,
    sha256_hex,
    side_state,
)
from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    BehaviorProduction,
    ExecutedScope,
)
from lemoncrow.pro.capabilities.review.capability_catalog import STATIC_CATALOG, DetectedCapabilities
from lemoncrow.pro.capabilities.review.differential import combine_verdicts
from lemoncrow.pro.capabilities.review.hermetic import REPRODUCE_COMMAND
from lemoncrow.pro.capabilities.review.run_evidence import (
    DEFAULT_RAW_RETENTION_SECONDS,
    RedactionPolicy,
    protect_bytes,
    protect_mapping,
    protect_text,
)
from lemoncrow.pro.capabilities.review.runtime_validation import (
    RUNTIME_VALIDATION_MEDIA_TYPE,
    ValidationRequest,
    ValidationScenario,
    persist_validation_record,
    snapshot_semantic_identity,
    validation_source_ref,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.store import new_evidence_id
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.environment.execution import ExecutionResult, ExecutorClass
from lemoncrow.pro.environment.runners import SurfaceRunResult, WorkspaceOverlay

RunSide = Callable[[RuntimeConfigEntry, ReviewSurface, str], SurfaceRunResult]
"""Executes one synthetic analysis surface on one side through the runner registry."""
Meter = Callable[[Mapping[str, Any] | None, float], None]
"""Records one execution's compute against the admitted entry."""

_SOURCE: Final = "test"
_MAX_ATTEMPT_SLOTS: Final[int] = 6
_MAX_FINDINGS_STORED: Final[int] = 200


class AnalysisRunSuperseded(ValueError):
    """The Snapshot advanced during the run; nothing more is published as current."""


def is_analysis_entry(entry_id: str) -> bool:
    return entry_id in ANALYSIS_ENTRIES


def entry_title(entry_id: str) -> str:
    entry = STATIC_CATALOG.get(entry_id)
    return entry.title if entry is not None else entry_id


# --------------------------------------------------------------------------- #
# trusted-base catalog and detection
# --------------------------------------------------------------------------- #


def _analyses_section(content: str) -> tuple[Mapping[str, Any], list[str]]:
    from lemoncrow.pro.capabilities.review.recipes import _config_document

    decoded, error = _config_document(content)
    if decoded is None:
        return {}, [error] if error else []
    section = decoded.get("analyses")
    if section is None:
        return {}, []
    if not isinstance(section, Mapping):
        return {}, ["analyses must be a mapping of migrations/regenerate/shipped/infra lists"]
    unknown = sorted(str(key) for key in section if key not in set(CONFIG_KEYS.values()))
    return section, [f"analyses has unknown keys: {', '.join(unknown)}"] if unknown else []


_FIT_HINTS: Final[Mapping[str, str]] = {
    MIGRATIONS_ENTRY: (
        "The driver command carries one entry per base migration file: narrow analyses.migrations to one "
        "migration directory, or squash old migrations into a schema fixture."
    ),
    COMPAT_ENTRY: (
        "The driver command carries one entry per base migration file: narrow analyses.compatibility to one "
        "migration directory, or squash old migrations into a schema fixture."
    ),
    REGENERATE_ENTRY: "Shorten analyses.regenerate command and outputs (they travel inside the executor command).",
    SHIPPED_ENTRY: "Shorten analyses.shipped command and artifacts (they travel inside the executor command).",
    FLAG_MATRIX_ENTRY: (
        "Shorten the analyses.flags command, on/off values and env template (they travel inside the executor "
        "command next to up to 8 flag states, reserved at their largest)."
    ),
    INFRA_ENTRY: "Shorten the analyses.infra spec (it travels inside the executor command).",
}


def fit_command(spec: AnalysisSpec) -> AnalysisSpec:
    """*spec*, or a non-executable copy carrying the typed ``Command too large`` reason.

    The executor accepts a bounded command, and a driver's inputs travel inside it. The worst-case command
    is computed here, at plan time from the spec alone (no tree is read), so an analysis that can never run
    is shown ``not_available`` with how to shrink it, not discovered as a failed run.
    """

    if not spec.executable:
        return spec
    payload: Mapping[str, Any]
    if spec.entry_id == MIGRATIONS_ENTRY:
        payload, _problem = migration_execution.payload(spec, read_base=lambda _path: "")
    elif spec.entry_id == COMPAT_ENTRY:
        payload, _problem = compat_mixed_version.payload(spec, read_base=lambda _path: "")
    elif spec.entry_id == REGENERATE_ENTRY:
        payload = regenerate_compare.payload(spec)
    elif spec.entry_id == SHIPPED_ENTRY:
        payload = shipped_artifacts.payload(spec)
    elif spec.entry_id == FLAG_MATRIX_ENTRY:
        # The flag states are added at run time; their worst case is derived from the spec's own bounds.
        payload = flag_matrix.worst_case_payload(spec)
    else:
        payload = infra_render.payload(spec)
    problem = payload_problem(_driver(spec.entry_id, spec), spec, payload)
    if not problem:
        return spec
    return replace(spec, executable=False, reason=problem, setup_hint=_FIT_HINTS[spec.entry_id])


def build_analysis_catalog(
    content: str,
    paths: Sequence[str] | None,
    read_base: Callable[[str], str | None],
    *,
    base_state: str = "present",
) -> AnalysisCatalog:
    """Every analysis the trusted base configures or contains; head content never contributes.

    A configured ``analyses.<key>`` list replaces discovery for that entry
    (an empty list turns the entry's discovery off). ``paths=None`` (the base
    tree is unreadable) discovers nothing.
    """

    section, errors = _analyses_section(content)
    specs: list[AnalysisSpec] = []
    configured: set[str] = set()
    parsers: Mapping[str, Callable[[Sequence[Any], list[str]], list[AnalysisSpec]]] = {
        MIGRATIONS_ENTRY: migration_execution.parse_specs,
        REGENERATE_ENTRY: regenerate_compare.parse_specs,
        SHIPPED_ENTRY: shipped_artifacts.parse_specs,
        INFRA_ENTRY: infra_render.parse_specs,
        COMPAT_ENTRY: compat_mixed_version.parse_specs,
        FLAG_MATRIX_ENTRY: flag_matrix.parse_specs,
    }
    discoverers: Mapping[str, Callable[[Sequence[str], Callable[[str], str | None]], list[AnalysisSpec]]] = {
        MIGRATIONS_ENTRY: migration_execution.discover_specs,
        SHIPPED_ENTRY: shipped_artifacts.discover_specs,
        INFRA_ENTRY: infra_render.discover_specs,
        COMPAT_ENTRY: compat_mixed_version.discover_specs,
    }
    listing = list(paths) if paths is not None else []
    for entry_id in ANALYSIS_ENTRIES:
        key = CONFIG_KEYS[entry_id]
        rows: list[AnalysisSpec] = []
        if key in section:
            configured.add(entry_id)
            raw = section.get(key)
            if isinstance(raw, list):
                rows = parsers[entry_id](raw[: MAX_SPECS_PER_ENTRY * 2], errors)
            else:
                errors.append(f"analyses.{key} must be a list")
        elif paths is not None and entry_id in discoverers:
            rows = discoverers[entry_id](listing, read_base)
        ids: set[str] = set()
        for spec in rows[:MAX_SPECS_PER_ENTRY]:
            if spec.id in ids:
                errors.append(f"analyses.{key}: duplicate id {spec.id}")
                continue
            ids.add(spec.id)
            if entry_id == MIGRATIONS_ENTRY:
                spec = migration_execution.attach_base_files(
                    migration_execution.attach_environment(spec, read_base), listing
                )
            elif entry_id == COMPAT_ENTRY:
                spec = compat_mixed_version.attach_base_files(spec, listing)
            specs.append(fit_command(spec))
    state = base_state if base_state in {"present", "absent", "provisional"} else "provisional"
    return AnalysisCatalog(
        specs=tuple(specs),
        errors=tuple(errors),
        base_state=state,  # type: ignore[arg-type]
        configured_entries=frozenset(configured),
    )


def apply_analysis_facts(detected: DetectedCapabilities, catalog: AnalysisCatalog) -> DetectedCapabilities:
    """The analysis entries' facts come from what the trusted base gives them to act on.

    File-name detection alone (a ``migrations/`` folder of notes, a Terraform
    file inside a module) is replaced by the analysis catalog: an entry with
    no spec has nothing to act on, so it is neither run nor shown.
    """

    if detected.base_state == "provisional":
        return detected
    facts = set(detected.facts) - set(ENTRY_FACT.values())
    evidence = {key: value for key, value in detected.evidence.items() if key not in set(ENTRY_FACT.values())}
    for fact, why in catalog.facts().items():
        facts.add(fact)
        evidence[fact] = why
    # An entry configured with specs none of which can run plans not_available with the typed reason.
    unavailable = {key: value for key, value in detected.unavailable.items() if key not in set(ENTRY_FACT.values())}
    unavailable.update(catalog.unavailable())
    return replace(detected, facts=frozenset(facts), evidence=evidence, unavailable=unavailable)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AnalysisRunInputs:
    store: Any
    revision: ReviewRevision
    catalog: AnalysisCatalog
    entry_id: str
    sides: tuple[Side, ...]
    executor_class: ExecutorClass
    policy_digest: str
    """sha256 hex of the trusted-base review config that defined the analyses."""
    changed_paths: tuple[str, ...] = ()
    read_base: Callable[[str], str | None] = lambda _path: None
    #: The Snapshot's own text of a repo path on one side (``"old"``/``"new"``), for stage-B diffs.
    read_side: Callable[[str, str], str | None] = lambda _path, _side: None
    base_identity: str = ""
    redaction: RedactionPolicy | None = None
    retention_seconds: int = DEFAULT_RAW_RETENTION_SECONDS
    budget_seconds: float | None = None
    expected_current_revision_id: str = ""
    execution_intent: str = ""
    spec_ids: tuple[str, ...] = ()
    #: JSON characters one driver report may hold on this executor (copy-out capacity).
    report_limit: int = REPORT_LIMIT
    #: The trusted base's file listing (``None``: unreadable); the compatibility scan reads old code from it.
    base_paths: Callable[[], Iterable[str] | None] = lambda: None
    #: The admitted plan entry's depth; the flag matrix is bounded by it.
    depth: str = ""
    #: The other change-set members' heads (``compat_mixed_version.SetHeads``; ``None``: no set).
    set_heads: Callable[[], Any] = lambda: None


@dataclass(slots=True)
class AnalysisResult:
    spec: AnalysisSpec
    evaluation: Evaluation
    runs: list[SideRun] = field(default_factory=list)
    evidence_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "spec": self.spec.id,
            "title": self.spec.title,
            "tool": self.spec.tool,
            "status": self.evaluation.status,
            "reasons": list(self.evaluation.reasons),
            "findings": len(self.evaluation.findings),
            "setup_hint": self.evaluation.setup_hint,
            "attempts": [run.to_wire() for run in self.runs],
            "evidence_id": self.evidence_id,
        }


@dataclass(slots=True)
class AnalysisRunReport:
    entry_id: str
    revision_id: str
    results: list[AnalysisResult] = field(default_factory=list)
    verdict: str = ""
    verdict_evidence: str = ""
    executions: int = 0
    reused: int = 0
    notes: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "revision_id": self.revision_id,
            "results": [result.to_wire() for result in self.results],
            "verdict": self.verdict,
            "verdict_evidence": self.verdict_evidence,
            "executions": self.executions,
            "reused": self.reused,
            "notes": list(self.notes),
        }


def _driver(entry_id: str, spec: AnalysisSpec | None = None) -> str:
    if spec is not None and spec.entry_id == MIGRATIONS_ENTRY and spec.tool != "sql":
        return migration_execution.FRAMEWORK_DRIVER
    return {
        MIGRATIONS_ENTRY: migration_execution.DRIVER,
        REGENERATE_ENTRY: regenerate_compare.DRIVER,
        SHIPPED_ENTRY: shipped_artifacts.DRIVER,
        INFRA_ENTRY: infra_render.DRIVER,
        COMPAT_ENTRY: compat_mixed_version.DRIVER,
        FLAG_MATRIX_ENTRY: flag_matrix.DRIVER,
    }[entry_id]


def _check_ref(item: ReviewEvidence) -> str:
    return f"check:{item.source}:{item.source_ref}" if item.source_ref else f"check:{item.id}"


def read_analysis_record(store: Any, item: ReviewEvidence) -> dict[str, Any] | None:
    """A persisted analysis/attempt record, only when it matches its content hash."""

    return _read_record(store, item)


def _read_record(store: Any, item: ReviewEvidence) -> dict[str, Any] | None:
    payload = store.read_evidence_artifact(item)
    if payload is None or not item.content_hash or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _sanitize_heavy(spec: AnalysisSpec, heavy: Mapping[str, str]) -> dict[str, str]:
    """Structure-aware masking pattern redaction cannot do, before anything is stored.

    Rendered manifests are stored only with every Secret value masked; output
    that does not parse is not stored at all (it could carry them verbatim).
    """

    out = dict(heavy)
    if spec.entry_id == INFRA_ENTRY and "rendered.yaml" in out:
        _resources, masked, problem = infra_render.rendered_resources(out.pop("rendered.yaml"))
        if problem:
            out["rendered.invalid"] = problem  # the evaluation says UNKNOWN, never "0 resources"
        else:
            out["rendered.yaml"] = masked
    return out


def _environment(execution: object) -> str:
    if not isinstance(execution, Mapping):
        return ""
    try:
        return ExecutionResult.from_wire(execution).environment_fingerprint
    except (TypeError, ValueError):
        return ""


class _Run:
    def __init__(self, inputs: AnalysisRunInputs, run_side: RunSide, meter: Meter) -> None:
        self.inputs = inputs
        self.run_side = run_side
        self.meter = meter
        self.report = AnalysisRunReport(entry_id=inputs.entry_id, revision_id=inputs.revision.id)
        self.budget = inputs.budget_seconds
        self._artifacts: Any = None
        self._artifacts_loaded = False
        #: evidence id -> typed execution wire of that attempt (fresh or reused).
        self.executions: dict[str, Mapping[str, Any]] = {}
        #: spec id -> host-side preparation (compatibility pairings, flag states).
        self.prepared: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # heavy artifacts (R4 retention + redaction)
    # ------------------------------------------------------------------ #

    def artifacts(self) -> Any:
        if not self._artifacts_loaded:
            self._artifacts_loaded = True
            opener = getattr(self.inputs.store, "run_artifacts", None)
            self._artifacts = opener(retention_seconds=self.inputs.retention_seconds) if callable(opener) else None
        return self._artifacts

    def put(self, name: str, text: str, *, owner: str) -> Any:
        artifacts = self.artifacts()
        if artifacts is None or not text:
            return None
        protected, _count = protect_bytes(text.encode("utf-8"), self.inputs.redaction)
        return artifacts.put(
            self.inputs.revision.review_id,
            protected,
            role=f"analysis:{name}"[:64],
            media_type="text/plain; charset=utf-8",
            owner=owner[:256],
        )

    def read_heavy(self, digests: Mapping[str, Any]) -> dict[str, str] | None:
        artifacts = self.artifacts()
        out: dict[str, str] = {}
        for name, digest in digests.items():
            payload = artifacts.read(self.inputs.revision.review_id, str(digest)) if artifacts is not None else None
            if payload is None:
                return None
            out[str(name)] = payload.decode("utf-8", "replace")
        return out

    # ------------------------------------------------------------------ #
    # one spec, one side
    # ------------------------------------------------------------------ #

    def _payload(self, spec: AnalysisSpec, side: Side) -> tuple[dict[str, Any], str]:
        if spec.entry_id == MIGRATIONS_ENTRY:
            return migration_execution.payload(spec, read_base=self.inputs.read_base)
        if spec.entry_id == COMPAT_ENTRY:
            return compat_mixed_version.payload(spec, read_base=self.inputs.read_base)
        if spec.entry_id == FLAG_MATRIX_ENTRY:
            # The base side may spend half of what is left; the head side what remains after it.
            budget = None if self.budget is None else self.budget / (2 if side == "old" else 1)
            return flag_matrix.payload(spec, self.prepared[spec.id], budget_seconds=budget), ""
        if spec.entry_id == REGENERATE_ENTRY:
            return regenerate_compare.payload(spec), ""
        if spec.entry_id == SHIPPED_ENTRY:
            return shipped_artifacts.payload(spec), ""
        return infra_render.payload(spec), ""

    def _overlay(self, spec: AnalysisSpec, side: Side) -> tuple[WorkspaceOverlay | None, str]:
        """Trusted-base inputs written over the head workspace (generators, schema fixtures)."""

        files = [path for path in spec.trusted_inputs if not path.startswith("fixture:")]
        extra: tuple[tuple[str, str], ...] = ()
        if spec.entry_id == COMPAT_ENTRY and side == "new":
            # The dispatcher's host-extracted statements (old and new code) for the persistence pairing.
            extra = tuple(self.prepared[spec.id].overlay)
        if side == "old" or not (files or extra):
            return None, ""
        if spec.entry_id == REGENERATE_ENTRY:
            return regenerate_compare.overlay(
                spec, base_identity=self.inputs.base_identity, read_base=self.inputs.read_base
            )
        rows: list[tuple[str, str | None]] = []
        for path in files:
            text = self.inputs.read_base(path)
            if text is None:
                return None, f"trusted-base input {path} does not exist on the base"
            rows.append((path, text))
        rows.extend(extra)
        if not self.inputs.base_identity:
            return None, "the trusted base has no commit identity to take its inputs from"
        return WorkspaceOverlay(source=self.inputs.base_identity, files=tuple(rows)), ""

    def _request(
        self, spec: AnalysisSpec, side: Side, argv: Sequence[str], overlay: WorkspaceOverlay | None, slot: int
    ) -> ValidationRequest:
        inputs = self.inputs
        return ValidationRequest(
            review_id=inputs.revision.review_id,
            revision_id=inputs.revision.id,
            snapshot_identity=snapshot_semantic_identity(inputs.revision),
            scenario_kind="recipe",
            scenario_id=f"{spec.entry_id}:{spec.id}",
            side=side,
            viewport_key=f"run:{slot}@{inputs.executor_class.value}",
            runtime_id=f"analysis:{spec.entry_id}:{spec.id}",
            runtime_config_digest=sha256_hex({"argv": list(argv)}),
            trusted_policy_digest=inputs.policy_digest,
            scope_digest=sha256_hex(
                {
                    "root": spec.root,
                    "overlay": [list(item) for item in overlay.digests()] if overlay is not None else [],
                    "source": overlay.source if overlay is not None else "",
                }
            ),
        )

    def _scenario(self, spec: AnalysisSpec) -> ValidationScenario:
        return ValidationScenario(
            id=f"{spec.entry_id}:{spec.id}",
            title=f"{spec.title or spec.id} · {entry_title(spec.entry_id)}",
            provider=ANALYSIS_PROVIDER,
            surface_id=f"analysis:{spec.entry_id}:{spec.id}",
            runtime_id=f"analysis:{spec.entry_id}:{spec.id}",
        )

    def _side_from_record(self, record: Mapping[str, Any], item: ReviewEvidence, side: Side) -> SideRun | None:
        data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
        assert isinstance(data, Mapping)
        analysis = data.get("analysis") if isinstance(data.get("analysis"), Mapping) else None
        if analysis is None:
            return None
        state = str(analysis.get("state") or "")
        if state not in {"ok", "findings", "failed"}:
            return None  # an unavailable or broken attempt is retried, never reused
        digests = analysis.get("heavy") if isinstance(analysis.get("heavy"), Mapping) else {}
        assert isinstance(digests, Mapping)
        heavy = self.read_heavy(digests)
        if heavy is None:
            return None  # its heavy artifacts expired: re-execute rather than compare without them
        report = analysis.get("report") if isinstance(analysis.get("report"), Mapping) else {}
        assert isinstance(report, Mapping)
        execution = data.get("execution")
        if isinstance(execution, Mapping):
            self.executions[item.id] = execution
        checks = [row for row in record.get("deterministic_checks") or () if isinstance(row, Mapping)]
        exit_code = checks[0].get("actual") if checks else None
        return SideRun(
            side=side,
            state=cast(Any, state),
            reason=str(analysis.get("reason") or ""),
            report=dict(report),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            evidence_id=item.id,
            evidence_ref=_check_ref(item),
            duration_ms=int(record.get("duration_ms") or 0),
            reused=True,
            environment=_environment(execution),
            executor_class=item.executor_class,
            heavy=heavy,
        )

    def _stage_b(
        self, spec: AnalysisSpec, side: Side, decoded: dict[str, Any], exit_code: int | None
    ) -> tuple[dict[str, Any] | None, str]:
        if spec.entry_id == SHIPPED_ENTRY and spec.tool == "container":
            return shipped_artifacts.inspect_image_report(decoded)
        if spec.entry_id == SHIPPED_ENTRY:
            return shipped_artifacts.inspect_outputs(decoded, exit_code)
        if spec.entry_id == MIGRATIONS_ENTRY and spec.tool != "sql":
            return migration_execution.inspect_framework_report(decoded, exit_code)
        if spec.entry_id == REGENERATE_ENTRY:
            read_side = self.inputs.read_side
            return regenerate_compare.compare_outputs(spec, decoded, exit_code, lambda path: read_side(path, side))
        if spec.entry_id == FLAG_MATRIX_ENTRY:
            return flag_matrix.inspect(decoded, exit_code)
        return decoded, ""

    def attempt(self, spec: AnalysisSpec, side: Side) -> SideRun:
        inputs = self.inputs
        payload, problem = self._payload(spec, side)
        if problem:
            return SideRun(side, "not_run", problem)
        overlay, problem = self._overlay(spec, side)
        if problem:
            return SideRun(side, "not_run", problem)
        document = {
            **payload,
            "entry": spec.entry_id,
            "spec": spec.id,
            "root": spec.root,
            "timeout": spec.timeout,
            "limit": inputs.report_limit,
        }
        argv, problem = driver_command(_driver(spec.entry_id, spec), document)
        if problem:
            return SideRun(side, "not_run", problem)
        scenario = self._scenario(spec)
        slot = 1
        request = self._request(spec, side, argv, overlay, slot)
        while slot <= _MAX_ATTEMPT_SLOTS:
            request = self._request(spec, side, argv, overlay, slot)
            source_ref = validation_source_ref(request, execution_intent=inputs.execution_intent)
            existing: ReviewEvidence | None = inputs.store.get_evidence_by_source(
                request.review_id, _SOURCE, source_ref
            )
            if existing is None or existing.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE:
                break
            record = _read_record(inputs.store, existing)
            if record is not None and record.get("identity") == request.identity:
                reused = self._side_from_record(record, existing, side)
                if reused is not None:
                    self.report.reused += 1
                    return reused
            slot += 1
        if self.budget is not None and self.budget <= 0:
            return SideRun(side, "not_run", "Limited by budget: the plan's compute budget for this Review is used up")
        values: dict[str, Any] = {
            "command": list(argv),
            # The full Snapshot is materialized (reproducible from its commit); the driver enters spec.root.
            "root": ".",
            "timeout": spec.timeout + 30,
            "report": True,
            "collect_changes": False,
        }
        if overlay is not None:
            values["overlay"] = overlay
        runner = "command"
        if spec.entry_id == MIGRATIONS_ENTRY and spec.tool != "sql":
            values.update(migration_execution.runtime_values(spec))
        elif spec.entry_id == SHIPPED_ENTRY and spec.tool == "container":
            runner = "container_image"
            values = shipped_artifacts.runtime_values(spec)
        runtime = RuntimeConfigEntry(id=request.runtime_id, runner=runner, values=values)
        surface = ReviewSurface(
            id=f"analysis:{spec.entry_id}:{spec.id}",
            provider=ANALYSIS_PROVIDER,
            kind="check",
            title=scenario.title,
            runtime=runtime.id,
            capabilities=("execute",),
            metadata={"analysis_digest": spec.digest, "entry": spec.entry_id},
        )
        started = time.monotonic()
        try:
            result = self.run_side(runtime, surface, side)
        except (OSError, RuntimeError, ValueError) as exc:
            result = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"analysis could not execute: {exc}"[:1000],
                "command",
                runtime.id,
            )
        wall = time.monotonic() - started
        self.report.executions += 1
        data = result.data if isinstance(result.data, Mapping) else {}
        execution = data.get("execution") if isinstance(data.get("execution"), Mapping) else None
        self.meter(execution, wall)
        if self.budget is not None:
            self.budget -= wall
        report: dict[str, Any] | None = None
        if result.status == "unavailable":
            state, reason = "unavailable", result.summary or "the executor was unavailable"
        else:
            decoded, why = decode_report(str(data.get("report") or ""))
            if decoded is not None:
                # Stage B of a sealed analysis: the host inspects the copied-out bytes; they
                # never reach protect_mapping or storage, only what the inspection derives does.
                decoded, why = self._stage_b(spec, side, decoded, result.exit_code)
            if decoded is None:
                state, reason = "error", (
                    why if not data.get("report_truncated") else "the analysis report was truncated"
                )
            else:
                # Reviewed code wrote nothing the host trusts as structure beyond this report,
                # and every string in it is redacted before anything is stored.
                report = cast(dict[str, Any], protect_mapping(decoded, inputs.redaction))
                state, reason = side_state(result.exit_code, report), str(report.get("reason") or "")
                if state == "error" and not reason:
                    reason = f"the analysis driver exited {result.exit_code}"
        heavy_raw = report.pop("heavy", None) if report is not None else None
        heavy = _sanitize_heavy(
            spec,
            (
                {str(key): str(value) for key, value in heavy_raw.items() if isinstance(value, str) and value}
                if isinstance(heavy_raw, Mapping)
                else {}
            ),
        )
        if report is not None:
            # The dispatcher states entry, spec and side; a driver's own claims are ignored.
            report["entry"], report["spec"] = spec.entry_id, spec.id
            report.pop("side", None)
        digests: dict[str, str] = {}
        for name, text in heavy.items():
            ref = self.put(name, text, owner=f"artifact-analysis:{spec.entry_id}:{spec.id}:{side}")
            if ref is not None:
                digests[name] = str(ref.digest)
        if len(digests) != len(heavy):
            self.report.notes.append(f"{spec.id}: heavy artifacts could not be retained by this store")
        stored: dict[str, Any] = {
            "analysis": {
                "entry": spec.entry_id,
                "spec": spec.id,
                "spec_digest": spec.digest,
                "state": state,
                "reason": protect_text(reason[:2000], inputs.redaction)[0],
                "report": report or {},
                # Digests only: the analysis record holds the citable references.
                "heavy": digests,
            }
        }
        if execution is not None:
            stored["execution"] = dict(execution)
        status = "NOT_RUN" if state in {"unavailable", "not_run"} else "PASS" if result.exit_code == 0 else "FAIL"
        record = {
            "v": 1,
            "execution_version": 1,
            "identity": request.identity,
            "request": request.to_wire(),
            "scenario": scenario.to_wire(),
            "snapshot_id": request.revision_id,
            "side": side,
            "reported_status": result.status,
            "status": status,
            "status_reasons": [protect_text(reason or result.summary or state, inputs.redaction)[0][:1000]],
            "deterministic_checks": (
                []
                if result.exit_code is None
                else [
                    {
                        "name": "process exit code",
                        "status": "PASS" if result.exit_code == 0 else "FAIL",
                        "expected": 0,
                        "actual": result.exit_code,
                        "detail": "Raw observation of one sandboxed analysis attempt; the analysis verdict is separate.",
                    }
                ]
            ),
            "runner": result.runner,
            "duration_ms": int(result.duration_ms or 0),
            "summary": protect_text(str(result.summary or "")[:2000], inputs.redaction)[0],
            "output": protect_text(str(result.output or "")[-4000:], inputs.redaction)[0],
            "data": stored,
        }
        evidence = self._persist(request, scenario, record)
        if execution is not None:
            self.executions[evidence.id] = execution
        return SideRun(
            side=side,
            state=cast(Any, state),
            reason=reason,
            report=report or {},
            exit_code=result.exit_code,
            evidence_id=evidence.id,
            evidence_ref=_check_ref(evidence),
            duration_ms=int(result.duration_ms or 0),
            reused=False,
            environment=_environment(execution),
            executor_class=evidence.executor_class,
            heavy=heavy,
        )

    def _persist(
        self, request: ValidationRequest, scenario: ValidationScenario, record: Mapping[str, Any]
    ) -> ReviewEvidence:
        try:
            evidence: ReviewEvidence = persist_validation_record(
                self.inputs.store,
                self.inputs.revision,
                request,
                scenario,
                record,
                source=_SOURCE,
                expected_current_revision_id=self.inputs.expected_current_revision_id,
                execution_intent=self.inputs.execution_intent,
                executor_class=self.inputs.executor_class,
                verdict=False,
            )
        except ValueError as exc:
            if "superseded" in str(exc).lower():
                raise AnalysisRunSuperseded(str(exc)) from exc
            raise
        return evidence

    # ------------------------------------------------------------------ #
    # one spec
    # ------------------------------------------------------------------ #

    def _compat(self, spec: AnalysisSpec) -> AnalysisResult:
        """Item 41: pairings scoped to what the Change touches; only persistence executes (head side)."""

        inputs = self.inputs
        prepared = compat_mixed_version.prepare(
            spec,
            changed_paths=inputs.changed_paths,
            read_side=inputs.read_side,
            base_paths=inputs.base_paths,
            set_heads=inputs.set_heads,
        )
        self.prepared[spec.id] = prepared
        runs: list[SideRun] = []
        head: SideRun | None = None
        if prepared.execute:
            head = self.attempt(spec, "new")
            runs.append(head)
        return AnalysisResult(spec, compat_mixed_version.evaluate(spec, prepared, head), runs)

    def _flags(self, spec: AnalysisSpec) -> AnalysisResult:
        """Item 44: each touched flag on and off on base and head, bounded by depth and budget."""

        inputs = self.inputs
        prepared = flag_matrix.prepare(
            spec, changed_paths=inputs.changed_paths, read_side=inputs.read_side, depth=inputs.depth
        )
        self.prepared[spec.id] = prepared
        runs: list[SideRun] = []
        base: SideRun | None = None
        head: SideRun | None = None
        if any(state.id not in prepared.limited for state in prepared.states):
            base = self.attempt(spec, "old")
            head = self.attempt(spec, "new")
            runs.extend((base, head))
        return AnalysisResult(spec, flag_matrix.evaluate(spec, prepared, head, base), runs)

    def spec(self, spec: AnalysisSpec) -> AnalysisResult:
        inputs = self.inputs
        if spec.entry_id == COMPAT_ENTRY:
            return self._compat(spec)
        if spec.entry_id == FLAG_MATRIX_ENTRY:
            return self._flags(spec)
        if not spec.executable:
            return AnalysisResult(
                spec,
                Evaluation("NOT_RUN", [spec.reason or "not executable in this build"], setup_hint=spec.setup_hint),
            )
        runs: list[SideRun] = []
        base: SideRun | None = None
        # The migration head run replays the trusted base itself; a separate base run proves nothing more.
        if "old" in inputs.sides and spec.entry_id != MIGRATIONS_ENTRY:
            base = self.attempt(spec, "old")
            runs.append(base)
        head = self.attempt(spec, "new")
        runs.append(head)
        changed = [path for path in spec.trusted_inputs if path in set(inputs.changed_paths)]
        if spec.entry_id == MIGRATIONS_ENTRY:
            evaluation = migration_execution.evaluate(spec, head, base)
        elif spec.entry_id == REGENERATE_ENTRY:
            evaluation = regenerate_compare.evaluate(
                spec, head, base, changed_paths=inputs.changed_paths, generator_edited=changed
            )
        elif spec.entry_id == SHIPPED_ENTRY:
            evaluation = shipped_artifacts.evaluate(spec, head, base)
        else:
            evaluation = infra_render.evaluate(spec, head, base)
        if any(run.reason.startswith("Limited by budget") for run in runs) and evaluation.status in {"NOT_RUN", "PASS"}:
            evaluation.status = "NOT_RUN"
            evaluation.reasons.insert(0, "Limited by budget: not every side of this analysis ran")
        return AnalysisResult(spec, evaluation, runs)

    def publish(self, result: AnalysisResult) -> ReviewEvidence:
        inputs = self.inputs
        spec, evaluation = result.spec, result.evaluation
        owner = f"artifact-analysis:{spec.entry_id}:{spec.id}"
        artifacts: list[dict[str, Any]] = []
        for name, text in sorted(evaluation.heavy.items()):
            ref = self.put(name, text, owner=owner)
            if ref is not None:
                artifacts.append({"name": name, **ref.to_wire()})
        head = next((run for run in result.runs if run.side == "new"), None)
        base = next((run for run in result.runs if run.side == "old"), None)
        record = {
            "v": 1,
            "entry_id": spec.entry_id,
            "entry_title": entry_title(spec.entry_id),
            "revision_id": inputs.revision.id,
            "spec": spec.to_wire(),
            "sides": list(inputs.sides),
            "status": evaluation.status,
            "reasons": [protect_text(reason, inputs.redaction)[0][:1000] for reason in evaluation.reasons][:20],
            "setup_hint": evaluation.setup_hint,
            "findings": protect_mapping(
                [item.to_wire() for item in evaluation.findings[:_MAX_FINDINGS_STORED]], inputs.redaction
            ),
            "summary": protect_mapping(evaluation.summary, inputs.redaction),
            "attempts": [run.to_wire() for run in result.runs],
            "artifacts": artifacts,
            "executor_class": inputs.executor_class.value,
            "environment": {"base": base.environment if base else "", "head": head.environment if head else ""},
            "reproduce": {
                "command": REPRODUCE_COMMAND,
                "evidence_ids": [run.evidence_id for run in result.runs if run.evidence_id],
            },
        }
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        digest = hashlib.sha256(payload).hexdigest()
        source_ref = f"artifact-analysis:{spec.entry_id}:{spec.id}:{digest}"
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
                    title=f"{spec.title or spec.id} · {entry_title(spec.entry_id)}"[:300],
                    artifact_path=rel,
                    content_hash=content_hash,
                    mime_type=ANALYSIS_MEDIA_TYPE,
                    bytes=size,
                    source=_SOURCE,
                    source_ref=source_ref,
                    detail="; ".join(record["reasons"])[:1200],
                    execution_intent=inputs.execution_intent,
                    executor_class=inputs.executor_class.value,
                ),
                expected_current_revision_id=inputs.expected_current_revision_id,
            )
        except ValueError as exc:
            inputs.store.discard_artifact(rel)
            raise AnalysisRunSuperseded("analysis revision was superseded before publication") from exc
        except Exception:
            inputs.store.discard_artifact(rel)
            raise
        return added

    def publish_verdict(self, results: Sequence[AnalysisResult]) -> tuple[str, ReviewEvidence]:
        """One typed ``lemoncrow_verification`` result for the entry."""

        inputs = self.inputs
        entry_id = inputs.entry_id
        status = combine_verdicts([cast(Any, result.evaluation.status) for result in results]) if results else "NOT_RUN"
        reasons = [
            f"{result.spec.title or result.spec.id}: {result.evaluation.status} — {'; '.join(result.evaluation.reasons)[:300]}"
            for result in results
        ] or [f"the trusted base defines nothing for {entry_id} to act on"]
        reasons = [protect_text(reason, inputs.redaction)[0] for reason in reasons]
        digest = sha256_hex(
            {"status": status, "reasons": reasons, "records": [result.evidence_id for result in results]}
        )
        request = ValidationRequest(
            review_id=inputs.revision.review_id,
            revision_id=inputs.revision.id,
            snapshot_identity=snapshot_semantic_identity(inputs.revision),
            scenario_kind="recipe",
            scenario_id=entry_id,
            side="new",
            viewport_key=f"verdict@{inputs.executor_class.value}:{digest[:16]}",
            runtime_id="analyses",
            runtime_config_digest=sha256_hex(sorted(result.spec.digest for result in results)),
            trusted_policy_digest=inputs.policy_digest,
            scope_digest=sha256_hex({"entry": entry_id}),
        )
        scenario = ValidationScenario(
            id=entry_id,
            title=entry_title(entry_id),
            provider=ANALYSIS_PROVIDER,
            surface_id=entry_id,
            runtime_id="analyses",
        )
        data: dict[str, Any] = {
            "verdict": True,
            "analyses": [
                {
                    "spec": result.spec.id,
                    "digest": result.spec.digest,
                    "status": result.evaluation.status,
                    "evidence_id": result.evidence_id,
                }
                for result in results
            ],
        }
        execution = next(
            (
                self.executions[run.evidence_id]
                for result in results
                for run in result.runs
                if run.side == "new" and run.evidence_id in self.executions
            ),
            None,
        )
        if execution is not None:
            data["execution"] = dict(execution)
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
            "status_reasons": reasons,
            "deterministic_checks": [],
            "runner": "analyses",
            "duration_ms": 0,
            "summary": "; ".join(reasons)[:2000],
            "output": "",
            "data": data,
        }
        revision = inputs.revision
        head_sha = str(getattr(revision, "head_sha", "") or "") if not getattr(revision, "dirty", False) else ""
        try:
            evidence: ReviewEvidence = persist_validation_record(
                inputs.store,
                revision,
                request,
                scenario,
                record,
                source=_SOURCE,
                expected_current_revision_id=inputs.expected_current_revision_id,
                execution_intent=inputs.execution_intent,
                executor_class=inputs.executor_class,
                provider_revision=head_sha,
            )
        except ValueError as exc:
            if "superseded" in str(exc).lower():
                raise AnalysisRunSuperseded(str(exc)) from exc
            raise
        return status, evidence


def run_analysis_entry(inputs: AnalysisRunInputs, run_side: RunSide, meter: Meter) -> AnalysisRunReport:
    """Execute one admitted analysis entry's specs and publish their results.

    Raises :class:`AnalysisRunSuperseded` when the Snapshot advanced; whatever
    was already published stays bound to its exact (now historical) Snapshot.
    """

    run = _Run(inputs, run_side, meter)
    specs = [
        spec for spec in inputs.catalog.for_entry(inputs.entry_id) if not inputs.spec_ids or spec.id in inputs.spec_ids
    ]
    report = run.report
    if not specs:
        report.notes.append(f"{inputs.entry_id} has nothing in the trusted base to act on")
    for spec in specs:
        result = run.spec(spec)
        result.evidence_id = run.publish(result).id
        report.results.append(result)
    status, verdict = run.publish_verdict(report.results)
    report.verdict, report.verdict_evidence = status, verdict.id
    return report


# --------------------------------------------------------------------------- #
# reading results back
# --------------------------------------------------------------------------- #


def latest_analyses(
    store: Any, revision: Any
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    """Newest analysis record per (entry, spec) and newest verdict per entry on the exact Snapshot."""

    records: dict[tuple[str, str], dict[str, Any]] = {}
    verdicts: dict[str, dict[str, Any]] = {}
    rows = sorted(
        (item for item in store.list_evidence(revision.review_id) if item.revision_id == revision.id),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    for item in rows:
        if item.execution_intent == "explicit_historical":
            continue
        if item.mime_type == ANALYSIS_MEDIA_TYPE:
            record = _read_record(store, item)
            spec = record.get("spec") if isinstance(record, Mapping) else None
            key = (
                (str(record.get("entry_id") or ""), str(spec.get("id") or ""))
                if isinstance(spec, Mapping) and record
                else ("", "")
            )
            if record is not None and all(key) and key not in records:
                records[key] = {**record, "evidence_id": item.id, "observed_at": item.created_at}
        elif (
            item.attestation_class == "lemoncrow_verification"
            and item.verification_status
            and item.stable_key in ANALYSIS_ENTRIES
            and item.stable_key not in verdicts
        ):
            verdicts[item.stable_key] = {
                "stable_key": item.stable_key,
                "status": item.verification_status,
                "detail": item.detail,
                "evidence_id": item.id,
                "executor_class": item.executor_class,
                "observed_at": item.created_at,
            }
    return records, verdicts


# --------------------------------------------------------------------------- #
# behavior producer (R2)
# --------------------------------------------------------------------------- #


def _observation(side: str, raw: object, ref: str) -> BehaviorObservation | None:
    if not isinstance(raw, Mapping):
        return None
    return BehaviorObservation(
        side=cast(Any, side),
        status=str(raw.get("status") or ""),
        digest=str(raw.get("digest") or ""),
        summary=str(raw.get("summary") or ""),
        evidence_ref=ref,
    )


class ArtifactAnalysisProducer:
    """``migration``/``artifact``/``infra``/``compatibility``/``test_outcome`` targets from persisted analysis records."""

    producer_id = ANALYSIS_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        newest: dict[tuple[str, str], tuple[ReviewEvidence, Mapping[str, Any]]] = {}
        degraded: list[str] = []
        rows = sorted(
            (
                item
                for item in context.evidence
                if item.mime_type == ANALYSIS_MEDIA_TYPE
                and item.revision_id == context.revision.id
                and item.execution_intent != "explicit_historical"
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        for item in rows:
            payload = context.read_artifact(item)
            if payload is None or hashlib.sha256(payload).hexdigest() != item.content_hash:
                degraded.append(f"analysis record {item.id} is missing or does not match its digest")
                continue
            try:
                record = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                degraded.append(f"analysis record {item.id} is not JSON")
                continue
            spec = record.get("spec") if isinstance(record, Mapping) else None
            entry_id = str(record.get("entry_id") or "") if isinstance(record, Mapping) else ""
            if entry_id in ANALYSIS_ENTRIES and isinstance(spec, Mapping) and spec.get("id"):
                newest.setdefault((entry_id, str(spec["id"])), (item, record))
        deltas: list[BehaviorDelta] = []
        executed: list[ExecutedScope] = []
        for key in sorted(newest):
            item, record = newest[key]
            deltas.extend(self._deltas(item, record))
            for attempt in record.get("attempts") or ():
                if not isinstance(attempt, Mapping) or attempt.get("state") not in {"ok", "findings", "failed"}:
                    continue
                ref = str(attempt.get("evidence_ref") or "")
                if ref:
                    executed.append(
                        ExecutedScope(
                            producer=self.producer_id,
                            run_ref=ref,
                            side="base" if attempt.get("side") == "old" else "head",
                            target_ids=(key[0],),
                        )
                    )
        return BehaviorProduction(deltas=tuple(deltas), executed=tuple(executed), degraded=tuple(degraded))

    def _deltas(self, item: ReviewEvidence, record: Mapping[str, Any]) -> list[BehaviorDelta]:
        entry_id = str(record.get("entry_id") or "")
        spec = record.get("spec") if isinstance(record.get("spec"), Mapping) else {}
        assert isinstance(spec, Mapping)
        spec_title = str(spec.get("title") or spec.get("id") or "")
        attempts = [row for row in record.get("attempts") or () if isinstance(row, Mapping)]

        def side_ref(side: str) -> str:
            return next(
                (
                    str(row.get("evidence_ref") or "")
                    for row in attempts
                    if row.get("side") == side and row.get("evidence_ref")
                ),
                "",
            )

        base_ref, head_ref = side_ref("old"), side_ref("new")
        refs = [
            BehaviorEvidenceRef(
                ref=f"artifact:{item.id}",
                kind="artifact",
                label=f"{spec_title} · analysis",
                digest=item.content_hash,
                role="record",
                evidence_id=item.id,
                media_type=item.mime_type,
            )
        ]
        for side, ref in (("base", base_ref), ("head", head_ref)):
            if ref:
                refs.append(BehaviorEvidenceRef(ref=ref, kind="check", label=f"{spec_title} · {side} run", side=side))
        environment = record.get("environment") if isinstance(record.get("environment"), Mapping) else {}
        assert isinstance(environment, Mapping)
        base_env, head_env = str(environment.get("base") or ""), str(environment.get("head") or "")
        fingerprint = (
            head_env
            if base_env in {"", head_env}
            else "envpair:sha256:" + sha256_hex({"base": base_env, "head": head_env})
        )
        executor = str(record.get("executor_class") or "")
        executor = executor if executor in {"author_machine", "customer_worker", "managed_sandbox"} else ""
        deltas: list[BehaviorDelta] = []
        for raw in record.get("findings") or ():
            if not isinstance(raw, Mapping):
                continue
            finding = Finding.from_wire(raw)
            if not finding.subject or not finding.title:
                continue
            base = _observation("base", raw.get("base"), base_ref)
            head = _observation("head", raw.get("head"), head_ref)
            if base is None and head is None:
                continue
            deltas.append(
                BehaviorDelta(
                    kind=cast(Any, ENTRY_KIND[entry_id]),
                    producer=self.producer_id,
                    subject=f"{entry_id}:{finding.subject}",
                    title=finding.title,
                    summary=finding.summary,
                    base=base,
                    head=head,
                    change=finding.change,
                    evidence=tuple(refs),
                    environment_fingerprint=fingerprint,
                    executor_class=cast(Any, executor),
                    severity=finding.severity,
                    critical=finding.critical,
                    paths=finding.paths,
                    target_ids=(entry_id,),
                    facts=(("Analysis", spec_title), *finding.facts)[:12],
                )
            )
        return deltas


def entry_specs_wire(catalog: AnalysisCatalog, entry_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    return {entry_id: [spec.to_wire() for spec in catalog.for_entry(entry_id)] for entry_id in entry_ids}


__all__ = [
    "ANALYSIS_MEDIA_TYPE",
    "AnalysisRunInputs",
    "AnalysisRunReport",
    "AnalysisRunSuperseded",
    "ArtifactAnalysisProducer",
    "apply_analysis_facts",
    "build_analysis_catalog",
    "entry_specs_wire",
    "entry_title",
    "is_analysis_entry",
    "latest_analyses",
    "read_analysis_record",
    "run_analysis_entry",
]
