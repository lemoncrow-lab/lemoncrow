"""Provider-neutral Runtime Validation over existing Review execution/evidence.

This module deliberately owns no runner implementation, browser-verdict schema,
queue, or durable state table. It gives the existing pieces one semantic request
identity and one conservative normalization rule:

* browser scenarios remain browser_checks scenarios and verdicts;
* non-browser execution remains runtimes execution;
* results are persisted as ordinary ReviewEvidence rows;
* exact Snapshot materialization remains the runners'/snapshot module's job.

A runner reports observations. Review decides what those observations establish.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePath
from typing import Any, Literal

from lemoncrow.core.foundation.run_file_io import RunFileLock
from lemoncrow.pro.capabilities.review.execution import (
    ExecutionResult,
    ExecutorClass,
    VerificationEnvironment,
    canonical_digest,
    parse_executor_class,
)
from lemoncrow.pro.capabilities.review.run_evidence import protect_execution_result, protect_mapping, protect_text
from lemoncrow.pro.capabilities.review.runtimes import RunnerRegistry, SurfaceRunResult
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.store import new_evidence_id
from lemoncrow.pro.capabilities.review.surfaces import (
    ReviewSurface,
    ReviewSurfaceConfig,
    RuntimeConfigEntry,
    SurfaceContext,
)

VALIDATION_SCHEMA_VERSION = 1
VALIDATION_EXECUTION_VERSION = 1
RUNTIME_VALIDATION_MEDIA_TYPE = "application/vnd.lemoncrow.runtime-validation+json;version=1"

ValidationStatus = Literal["PASS", "FAIL", "UNKNOWN", "NOT_RUN"]
ValidationScenarioKind = Literal["browser", "surface", "recipe"]

_ARTIFACT_KINDS: dict[str, frozenset[str]] = {
    "screenshot": frozenset({"screenshot", "image"}),
    "image": frozenset({"screenshot", "image"}),
    "diff": frozenset({"screenshot", "image"}),
    "trace": frozenset({"playwright_trace"}),
    "video": frozenset({"video"}),
}


def _json_default(value: object) -> str:
    return str(value)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def snapshot_semantic_identity(revision: ReviewRevision) -> str:
    """Stable exact-Snapshot content identity, independent of materialization."""

    return _digest(
        {
            "tree_fingerprint": revision.tree_fingerprint,
            "source_fingerprint": revision.source_fingerprint,
        }
    )


def trusted_policy_digest(content: str | bytes | None, *, base_known: bool = True) -> str:
    """Digest trusted-base policy authority without using a filesystem path."""

    if isinstance(content, bytes):
        payload = content.decode("utf-8", errors="replace")
    else:
        payload = content or ""
    return _digest({"base_known": bool(base_known), "content": payload})


def browser_policy_digest(policy: Any) -> str:
    """Digest the existing BrowserPolicy without introducing another schema."""

    scenarios = []
    for scenario in getattr(policy, "scenarios", ()):
        scenarios.append(
            {
                "id": scenario.id,
                "title": scenario.title,
                "route": scenario.route,
                "paths": list(scenario.paths),
                "always": scenario.always,
                "required": scenario.required,
                "viewports": list(scenario.viewports),
                "visual_threshold": scenario.visual_threshold,
                "allow_console_errors": scenario.allow_console_errors,
            }
        )
    return _digest(
        {
            "version": int(getattr(policy, "version", 1)),
            "trusted_origins": list(getattr(policy, "trusted_origins", ())),
            "scenarios": scenarios,
        }
    )


_MACHINE_LOCAL_RUNTIME_KEYS = frozenset(
    {
        "cwd",
        "repo_root",
        "store_root",
        "scratch_root",
        "workspace",
        "workspace_path",
        "materialization_path",
        "materialized_path",
        "temp_dir",
        "tmpdir",
        "worker_id",
    }
)


def _semantic_config_value(value: Any, *, key: str = "") -> Any:
    """Remove execution-location noise while retaining semantic runner config."""

    normalized_key = key.strip().lower()
    if normalized_key in _MACHINE_LOCAL_RUNTIME_KEYS:
        return "<machine-local>"
    if isinstance(value, Mapping):
        return {
            str(name): _semantic_config_value(item, key=str(name))
            for name, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_semantic_config_value(item, key=key) for item in value]
    if isinstance(value, str):
        # Absolute executable/config paths are machine placement, not semantic
        # validation identity. Preserve only their basename. Routes such as
        # /health remain semantic unless they occur in a path-bearing key/list.
        path_bearing = (
            normalized_key in {"command", "args", "executable", "binary", "file"}
            or normalized_key.endswith("_path")
            or normalized_key.endswith("_file")
        )
        if path_bearing and value.startswith("/"):
            return f"<absolute:{PurePath(value).name}>"
        if value.startswith("/tmp/") or "/review/runtime-runs/" in value or "/surface-context/" in value:
            return f"<ephemeral:{PurePath(value).name}>"
    return value


def runtime_config_digest(runtime: RuntimeConfigEntry) -> str:
    """Stable runner configuration identity without machine-local execution paths."""

    return _digest(
        {
            "id": runtime.id,
            "runner": runtime.runner,
            "values": _semantic_config_value(dict(runtime.values)),
        }
    )


def _surface_scope(surface: ReviewSurface) -> dict[str, Any]:
    return {
        "provider": surface.provider,
        "surface_id": surface.id,
        "kind": surface.kind,
        "locator": surface.locator,
        "runtime": surface.runtime,
        "affected_paths": list(surface.affected_paths),
        "capabilities": list(surface.capabilities),
        "metadata": _semantic_config_value(dict(surface.metadata)),
    }


@dataclass(frozen=True, slots=True)
class ValidationScenario:
    """Minimal generic adapter for existing non-browser surface execution.

    Browser validation keeps using BrowserScenario directly; this type exists
    only to state what an existing runtime surface is expected to prove.
    """

    id: str
    title: str
    provider: str
    surface_id: str
    runtime_id: str
    target_ids: tuple[str, ...] = ()
    expected_exit_code: int | None = 0
    expected_http_statuses: tuple[int, ...] = ()
    accept_http_success_range: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "surface_id": self.surface_id,
            "runtime_id": self.runtime_id,
            "target_ids": list(self.target_ids),
            "expected_exit_code": self.expected_exit_code,
            "expected_http_statuses": list(self.expected_http_statuses),
            "accept_http_success_range": self.accept_http_success_range,
        }


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """Stable semantic identity for one exact Snapshot/scenario validation."""

    review_id: str
    revision_id: str
    snapshot_identity: str
    scenario_kind: ValidationScenarioKind
    scenario_id: str
    side: Literal["old", "new"]
    viewport_key: str
    runtime_id: str
    runtime_config_digest: str
    trusted_policy_digest: str
    scope_digest: str
    target_ids: tuple[str, ...] = ()
    schema_version: int = VALIDATION_SCHEMA_VERSION
    execution_version: int = VALIDATION_EXECUTION_VERSION

    def __post_init__(self) -> None:
        if not self.review_id or not self.revision_id or not self.scenario_id:
            raise ValueError("runtime validation requires review, revision, and scenario identity")
        if self.side not in {"old", "new"}:
            raise ValueError("runtime validation side must be old or new")
        if self.scenario_kind not in {"browser", "surface", "recipe"}:
            raise ValueError("runtime validation scenario kind must be browser, surface or recipe")
        for name in ("snapshot_identity", "runtime_config_digest", "trusted_policy_digest", "scope_digest"):
            value = str(getattr(self, name))
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a sha256 digest")

    def identity_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_version": self.execution_version,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "snapshot_identity": self.snapshot_identity,
            "scenario_kind": self.scenario_kind,
            "scenario_id": self.scenario_id,
            "side": self.side,
            "viewport_key": self.viewport_key,
            "runtime_id": self.runtime_id,
            "runtime_config_digest": self.runtime_config_digest,
            "trusted_policy_digest": self.trusted_policy_digest,
            "scope_digest": self.scope_digest,
            "target_ids": list(self.target_ids),
        }

    @property
    def identity(self) -> str:
        return _digest(self.identity_document())

    def to_wire(self) -> dict[str, Any]:
        return {**self.identity_document(), "identity": self.identity}


@dataclass(frozen=True, slots=True)
class LocalValidationOutcome:
    request: ValidationRequest
    scenario: ValidationScenario
    record: Mapping[str, Any]
    evidence: ReviewEvidence
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ReproductionProposal:
    """A finding may only point at an already trusted/configured scenario."""

    scenario_id: str
    target_ids: tuple[str, ...]
    reason: str
    expected_behavior: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "target_ids": list(self.target_ids),
            "reason": self.reason,
            "expected_behavior": self.expected_behavior,
        }


def _expected_statuses(surface: ReviewSurface) -> tuple[int, ...]:
    raw = surface.metadata.get("expected_status")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return (raw,) if 100 <= raw <= 599 else ()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        values = []
        for item in raw:
            if isinstance(item, int) and not isinstance(item, bool) and 100 <= item <= 599:
                values.append(item)
        return tuple(dict.fromkeys(values))
    return ()


def surface_validation_scenario(
    surface: ReviewSurface,
    runtime: RuntimeConfigEntry,
    *,
    target_ids: Sequence[str] = (),
) -> ValidationScenario:
    expected_exit_raw = runtime.values.get("expected_exit_code", 0)
    expected_exit = (
        int(expected_exit_raw)
        if isinstance(expected_exit_raw, (int, float)) and not isinstance(expected_exit_raw, bool)
        else 0
    )
    statuses = _expected_statuses(surface)
    return ValidationScenario(
        id=f"surface:{surface.provider}:{surface.id}",
        title=surface.title or f"{surface.provider}:{surface.id}",
        provider=surface.provider,
        surface_id=surface.id,
        runtime_id=runtime.id,
        target_ids=tuple(dict.fromkeys(str(value) for value in target_ids if str(value))),
        expected_exit_code=expected_exit,
        expected_http_statuses=statuses,
        accept_http_success_range=surface.kind == "api.request" and not statuses,
    )


def build_surface_validation_request(
    revision: ReviewRevision,
    surface: ReviewSurface,
    runtime: RuntimeConfigEntry,
    *,
    side: Literal["old", "new"] = "new",
    policy_digest: str | None = None,
    target_ids: Sequence[str] = (),
) -> ValidationRequest:
    scenario = surface_validation_scenario(surface, runtime, target_ids=target_ids)
    return ValidationRequest(
        review_id=revision.review_id,
        revision_id=revision.id,
        snapshot_identity=snapshot_semantic_identity(revision),
        scenario_kind="surface",
        scenario_id=scenario.id,
        side=side,
        viewport_key="",
        runtime_id=f"{runtime.runner}:{runtime.id}",
        runtime_config_digest=runtime_config_digest(runtime),
        trusted_policy_digest=policy_digest or trusted_policy_digest("", base_known=True),
        scope_digest=_digest(_surface_scope(surface)),
        target_ids=scenario.target_ids,
    )


def build_browser_validation_request(
    revision: ReviewRevision,
    scenario: Any,
    *,
    viewport_key: str = "",
    policy_digest: str,
    provider: str = "browser",
    runtime_config: Mapping[str, Any] | None = None,
    side: Literal["old", "new"] = "new",
    target_ids: Sequence[str] = (),
) -> ValidationRequest:
    """Build identity around the authoritative existing BrowserScenario."""

    scenario_id = str(getattr(scenario, "id", "") or "")
    if not scenario_id:
        raise ValueError("browser validation requires a configured BrowserScenario id")
    scope = {
        "scenario_id": scenario_id,
        "route": str(getattr(scenario, "route", "") or ""),
        "paths": list(getattr(scenario, "paths", ())),
        "required": bool(getattr(scenario, "required", False)),
    }
    return ValidationRequest(
        review_id=revision.review_id,
        revision_id=revision.id,
        snapshot_identity=snapshot_semantic_identity(revision),
        scenario_kind="browser",
        scenario_id=scenario_id,
        side=side,
        viewport_key=viewport_key,
        runtime_id=provider,
        runtime_config_digest=_digest({"provider": provider, "config": dict(runtime_config or {})}),
        trusted_policy_digest=policy_digest,
        scope_digest=_digest(scope),
        target_ids=tuple(dict.fromkeys(str(value) for value in target_ids if str(value))),
    )


def _check(name: str, passed: bool | None, *, expected: object, actual: object, detail: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed is True else "FAIL" if passed is False else "UNKNOWN",
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def _result_mapping(result: SurfaceRunResult | Mapping[str, Any]) -> dict[str, Any]:
    return result.to_payload() if isinstance(result, SurfaceRunResult) else dict(result)


def normalize_surface_run(
    request: ValidationRequest,
    scenario: ValidationScenario,
    result: SurfaceRunResult | Mapping[str, Any],
) -> dict[str, Any]:
    """Turn runner observations into a narrow deterministic Review verdict."""

    if request.scenario_kind != "surface":
        raise ValueError("surface normalization requires a surface validation request")
    if scenario.id != request.scenario_id or scenario.runtime_id not in request.runtime_id:
        raise ValueError("surface validation result does not match the requested scenario/runtime")
    raw = _result_mapping(result)
    if str(raw.get("surface_id") or "") != scenario.surface_id:
        raise ValueError("surface validation result belongs to another surface")
    if str(raw.get("provider") or "") != scenario.provider:
        raise ValueError("surface validation result belongs to another provider")
    if str(raw.get("side") or "") != request.side:
        raise ValueError("surface validation result belongs to another Snapshot side")
    if str(raw.get("runtime") or "") != scenario.runtime_id:
        raise ValueError("surface validation result belongs to another runtime")

    runner = str(raw.get("runner") or "")
    reported = str(raw.get("status") or "").lower().strip()
    checks: list[dict[str, Any]] = []
    unresolved: list[str] = []
    failures: list[str] = []

    exit_code = raw.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and scenario.expected_exit_code is not None:
        exit_passed = exit_code == scenario.expected_exit_code
        checks.append(
            _check(
                "process exit code",
                exit_passed,
                expected=scenario.expected_exit_code,
                actual=exit_code,
                detail="This proves only the configured runtime command's exit contract.",
            )
        )
        if not exit_passed:
            failures.append(f"process exited {exit_code}; expected {scenario.expected_exit_code}")

    data = raw.get("data")
    data = dict(data) if isinstance(data, Mapping) else {}
    status_code = data.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        http_passed: bool | None
        if scenario.expected_http_statuses:
            http_passed = status_code in scenario.expected_http_statuses
            expected: object = list(scenario.expected_http_statuses)
        elif scenario.accept_http_success_range:
            http_passed = 200 <= status_code < 400
            expected = "HTTP 200-399"
        else:
            http_passed = None
            expected = "an explicitly configured HTTP expectation"
        checks.append(
            _check(
                "HTTP response status",
                http_passed,
                expected=expected,
                actual=status_code,
                detail="The status check proves only this configured request.",
            )
        )
        if http_passed is False:
            failures.append(f"HTTP status {status_code} did not match the configured expectation")
        elif http_passed is None:
            unresolved.append("HTTP response had no configured status expectation")

    assertions = data.get("assertions")
    assertions_executed = bool(data.get("assertions_executed"))
    if assertions_executed:
        if not isinstance(assertions, Sequence) or isinstance(assertions, (str, bytes, bytearray)):
            unresolved.append("runner said assertions executed but supplied no inspectable assertion list")
        else:
            for index, raw_assertion in enumerate(assertions[:128]):
                if not isinstance(raw_assertion, Mapping):
                    unresolved.append("runner supplied a malformed assertion result")
                    continue
                name = str(raw_assertion.get("name") or f"assertion {index + 1}")[:200]
                assertion_status = str(raw_assertion.get("status") or "").upper()
                assertion_passed: bool | None = (
                    True if assertion_status == "PASS" else False if assertion_status == "FAIL" else None
                )
                checks.append(
                    _check(
                        name,
                        assertion_passed,
                        expected=raw_assertion.get("expected"),
                        actual=raw_assertion.get("actual"),
                        detail=str(raw_assertion.get("detail") or "")[:2000],
                    )
                )
                if assertion_passed is False:
                    failures.append(f"assertion failed: {name}")
                elif assertion_passed is None:
                    unresolved.append(f"assertion has no PASS/FAIL result: {name}")

    duration_raw = raw.get("duration_ms")
    duration_ms = (
        int(duration_raw) if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool) else 0
    )
    if reported == "unavailable":
        status: ValidationStatus = "UNKNOWN" if duration_ms > 0 else "NOT_RUN"
        reasons = [
            str(raw.get("summary") or "")
            or ("runtime execution became unavailable" if duration_ms > 0 else "runtime execution was not attempted")
        ]
    elif failures:
        status = "FAIL"
        reasons = failures
    elif not checks:
        status = "UNKNOWN"
        reasons = ["no inspectable deterministic check was reported; runner status alone is not verification"]
    elif unresolved or any(check["status"] == "UNKNOWN" for check in checks):
        status = "UNKNOWN"
        reasons = unresolved or ["one or more deterministic checks are unresolved"]
    elif reported not in {"passed", "failed"}:
        status = "UNKNOWN"
        reasons = [f"runner reported {reported or 'no status'}; deterministic observations do not establish completion"]
    elif reported == "failed":
        status = "UNKNOWN"
        reasons = ["runner reported failure but supplied no failing deterministic check"]
    else:
        status = "PASS"
        reasons = [f"{len(checks)} deterministic check(s) passed"]

    # Defense in depth: every runner's text is redacted before it is persisted,
    # including hosted/plugin runners that did not redact themselves. The typed
    # execution record was protected where it was produced and carries digests
    # that redaction must not rewrite.
    execution = data.pop("execution", None)
    data = protect_mapping(data)
    if isinstance(execution, Mapping):
        data["execution"] = dict(execution)
    return {
        "v": VALIDATION_SCHEMA_VERSION,
        "execution_version": VALIDATION_EXECUTION_VERSION,
        "identity": request.identity,
        "request": request.to_wire(),
        "scenario": scenario.to_wire(),
        "snapshot_id": request.revision_id,
        "side": request.side,
        "reported_status": reported,
        "status": status,
        "status_reasons": reasons,
        "deterministic_checks": checks,
        "runner": runner,
        "duration_ms": duration_ms,
        "summary": protect_text(str(raw.get("summary") or "")[:2000])[0],
        "output": protect_text(str(raw.get("output") or "")[-8000:])[0],
        "data": data,
    }


def validation_source_ref(request: ValidationRequest, *, execution_intent: str = "", fresh_ref: str = "") -> str:
    # Keep the semantic identity authoritative while exposing the trusted
    # scenario namespace so existing Review evidence UIs can associate a
    # persisted result with its surface after reload without reading the blob.
    # Operational intent is deliberately outside ValidationRequest.identity.
    # ``fresh_ref`` names one explicit fresh run, so its result is a new row
    # rather than a replay of the Snapshot's earlier (possibly reused) one.
    suffix = f":{execution_intent}" if execution_intent else ""
    fresh = f":fresh:{fresh_ref}" if fresh_ref else ""
    return f"runtime-validation:{request.scenario_id}:{request.identity}{suffix}{fresh}"


def validation_summary(record: Mapping[str, Any]) -> str:
    scenario = record.get("scenario")
    title = str(scenario.get("title") or "") if isinstance(scenario, Mapping) else ""
    reasons = [str(value) for value in record.get("status_reasons", ()) if str(value)]
    detail = reasons[0] if reasons else str(record.get("summary") or "")
    return " · ".join(value for value in (title, str(record.get("status") or "UNKNOWN"), detail) if value)[:1200]


def execution_verification(record: Mapping[str, Any]) -> str:
    """Canonical JSON of a typed record's verification environment, ``""`` when untyped.

    Stored on the evidence row so the Reader renders where the result was
    verified (``sandbox · image <digest>``) and the integrations it could not
    verify as structured fields rather than parsing detail text.
    """

    data = record.get("data")
    execution = data.get("execution") if isinstance(data, Mapping) else None
    verification = execution.get("verification") if isinstance(execution, Mapping) else None
    if not isinstance(verification, Mapping):
        return ""
    return json.dumps(VerificationEnvironment.from_wire(verification).to_wire(), sort_keys=True, separators=(",", ":"))


def _read_validation_record(store: Any, evidence: ReviewEvidence, request: ValidationRequest) -> dict[str, Any]:
    if evidence.review_id != request.review_id or evidence.revision_id != request.revision_id:
        raise ValueError("runtime validation evidence belongs to another Review/Snapshot")
    if (
        evidence.kind != "verification"
        or evidence.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE
        or not evidence.content_hash
    ):
        raise ValueError("runtime validation evidence has the wrong kind or media type")
    payload = store.read_evidence_artifact(evidence)
    if payload is None:
        raise ValueError("runtime validation evidence artifact is missing")
    if hashlib.sha256(payload).hexdigest() != evidence.content_hash:
        raise ValueError("runtime validation evidence artifact content hash does not match")
    try:
        record = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime validation evidence artifact is not valid JSON") from exc
    if not isinstance(record, dict) or record.get("identity") != request.identity:
        raise ValueError("runtime validation evidence artifact has a different semantic identity")
    return record


def persist_validation_record(
    store: Any,
    revision: ReviewRevision,
    request: ValidationRequest,
    scenario: ValidationScenario,
    record: Mapping[str, Any],
    *,
    path: str = "",
    source: str = "test",
    expected_current_revision_id: str = "",
    execution_intent: str = "",
    executor_class: ExecutorClass | str = ExecutorClass.AUTHOR_MACHINE,
    verdict: bool = True,
    provider_revision: str = "",
    attestation_scope: str | None = None,
    fresh_ref: str = "",
) -> ReviewEvidence:
    """Persist one normalized result using the existing ReviewEvidence authority.

    ``executor_class`` is the dispatcher's statement of who executed the code
    (an unknown value degrades to ``author_machine``). It is stamped on the
    evidence row and on the embedded execution record, overriding whatever a
    runner reported about itself.
    """

    if revision.review_id != request.review_id or revision.id != request.revision_id:
        raise ValueError("runtime validation request is not bound to this exact Snapshot")
    if str(record.get("identity") or "") != request.identity:
        raise ValueError("runtime validation record identity does not match its request")
    executor = parse_executor_class(executor_class)
    record = stamp_execution_record(record, executor)
    source_ref = validation_source_ref(request, execution_intent=execution_intent, fresh_ref=fresh_ref)
    existing: ReviewEvidence | None = store.get_evidence_by_source(request.review_id, source, source_ref)
    if existing is not None:
        _read_validation_record(store, existing, request)
        if expected_current_revision_id:
            current = store.latest_revision(request.review_id)
            if current is None or current.id != expected_current_revision_id:
                raise ValueError("runtime validation Snapshot was superseded before evidence publication")
        return existing

    payload = _canonical(dict(record))
    evidence_id = new_evidence_id()
    rel, digest, size = store.write_evidence_artifact(request.review_id, request.revision_id, evidence_id, payload)
    try:
        added: ReviewEvidence = store.add_evidence(
            ReviewEvidence(
                id=evidence_id,
                review_id=request.review_id,
                revision_id=request.revision_id,
                kind="verification",
                title=scenario.title,
                path=path,
                artifact_path=rel,
                content_hash=digest,
                mime_type=RUNTIME_VALIDATION_MEDIA_TYPE,
                bytes=size,
                source=source,  # type: ignore[arg-type]
                source_ref=source_ref,
                # ``verdict=False`` records a raw observation (one rerun attempt of
                # a check recipe) that is never itself a check result.
                verification_status=(
                    str(record.get("status") or "UNKNOWN") if request.side == "new" and verdict else ""
                ),
                detail=validation_summary(record),
                attestation_class="lemoncrow_verification",
                stable_key=request.scenario_id,
                # A check recipe result stamps its CI-equivalence key and exact head SHA instead.
                attestation_scope=request.scope_digest if attestation_scope is None else attestation_scope,
                provider_revision=provider_revision,
                attested_by="lemoncrow-runtime",
                remote_identity=request.identity,
                execution_intent=execution_intent,
                executor_class=executor.value,
                # The Reader shows where the result was verified and what the sandbox could not verify.
                execution_verification=execution_verification(record),
            ),
            expected_current_revision_id=expected_current_revision_id,
        )
        return added
    except ValueError as exc:
        store.discard_artifact(rel)
        if expected_current_revision_id:
            raise ValueError("runtime validation Snapshot was superseded before evidence publication") from exc
        raise
    except Exception:
        store.discard_artifact(rel)
        raise


def stamp_execution_record(record: Mapping[str, Any], executor: ExecutorClass) -> dict[str, Any]:
    """Re-stamp the embedded execution record with the dispatcher's executor class.

    A record that does not parse as a typed execution result is dropped and
    the drop is stated, so a forged or corrupted record never rides along.
    """

    out = dict(record)
    data = out.get("data")
    if not isinstance(data, Mapping) or "execution" not in data:
        return out
    data = dict(data)
    raw = data.pop("execution")
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("execution wire is not an object")
        digest = raw.get("result_digest")
        if not isinstance(digest, str) or digest != canonical_digest(
            {key: value for key, value in raw.items() if key != "result_digest"}
        ):
            raise ValueError("execution result digest mismatch")
        parsed = ExecutionResult.from_wire(raw)
        stage_rows = raw.get("stages")
        stages = parsed.stages
        # Hosted workers may report several bounded fetch/build operations.
        # Only the executable run stage must be unique; no dropped wire rows.
        if (
            not isinstance(stage_rows, list)
            or len(stage_rows) != len(stages)
            or len(stages) > 9
            or sum(stage.stage == "run" for stage in stages) > 1
            or raw.get("status") != parsed.status
        ):
            raise ValueError("execution stage record is ambiguous or contradictory")
        run_stage = parsed.stage("run")
        if out.get("status") == "PASS" and (
            run_stage is None
            or run_stage.status != "passed"
            or parsed.status != "passed"
            or any(stage.status == "passed" and stage.exit_code not in (None, 0) for stage in stages)
        ):
            raise ValueError("execution stages contradict the claimed PASS")
    except (TypeError, ValueError) as exc:
        data["execution_error"] = f"execution record rejected: {exc}"[:500]
        # Failure to authenticate process proof cannot preserve a typed PASS in
        # any Review/readiness consumer of the persisted verification_status.
        if out.get("status") == "PASS":
            out["status"] = "UNKNOWN"
    else:
        # Remote and in-process runners alike: the stored record is redacted here.
        data["execution"] = protect_execution_result(parsed.with_executor(executor)).to_wire()
    out["data"] = data
    return out


def validate_artifact_reference(
    evidence: ReviewEvidence | None,
    request: ValidationRequest,
    role: str,
    *,
    read_artifact: Any | None = None,
) -> ReviewEvidence:
    """Validate an artifact ref before attaching it to a runtime result."""

    allowed = _ARTIFACT_KINDS.get(role)
    if allowed is None:
        raise ValueError(f"unknown runtime validation artifact role {role!r}")
    if evidence is None:
        raise ValueError(f"runtime validation {role} artifact is missing")
    if evidence.review_id != request.review_id or evidence.revision_id != request.revision_id:
        raise ValueError(f"runtime validation {role} artifact must belong to the same exact Snapshot")
    if evidence.kind not in allowed or not evidence.content_hash:
        raise ValueError(f"runtime validation {role} artifact has wrong kind {evidence.kind!r}")
    if read_artifact is not None:
        payload = read_artifact(evidence)
        if payload is None:
            raise ValueError(f"runtime validation {role} artifact is missing")
        if hashlib.sha256(payload).hexdigest() != evidence.content_hash:
            raise ValueError(f"runtime validation {role} artifact content hash does not match")
    return evidence


def execute_local_surface_validation(
    *,
    store: Any,
    registry: RunnerRegistry,
    context: SurfaceContext,
    trusted_config: ReviewSurfaceConfig,
    surface: ReviewSurface,
    side: Literal["old", "new"] = "new",
    policy_digest: str | None = None,
    target_ids: Sequence[str] = (),
    expected_current_revision_id: str = "",
    execution_intent: str = "",
) -> LocalValidationOutcome:
    """Explicitly execute one trusted configured surface on its exact Snapshot.

    The surface and trusted_config must come from the trusted/base Snapshot.
    context names the reviewed revision whose requested side is materialized.
    This prevents a Change from authoring a new command in review.yaml and then
    asking Review to execute that command while the Change is still under review.
    """

    configured_runtime = trusted_config.runtime(surface.runtime) if surface.runtime else None
    runtime = configured_runtime or RuntimeConfigEntry(
        id=surface.runtime or "unconfigured",
        runner="unavailable",
        values={},
    )
    scenario = surface_validation_scenario(surface, runtime, target_ids=target_ids)
    request = build_surface_validation_request(
        context.revision,
        surface,
        runtime,
        side=side,
        policy_digest=policy_digest,
        target_ids=target_ids,
    )
    lock_path = (
        context.store_root
        / "review"
        / "runtime-validation"
        / "locks"
        / request.review_id
        / request.revision_id
        / request.identity
    )
    with RunFileLock(lock_path):
        existing = store.get_evidence_by_source(
            request.review_id,
            "test",
            validation_source_ref(request, execution_intent=execution_intent),
        )
        if existing is not None:
            if expected_current_revision_id:
                current = store.latest_revision(request.review_id)
                if current is None or current.id != expected_current_revision_id:
                    raise ValueError("runtime validation Snapshot was superseded before evidence publication")
            return LocalValidationOutcome(
                request=request,
                scenario=scenario,
                record=_read_validation_record(store, existing, request),
                evidence=existing,
                reused=True,
            )

        if configured_runtime is None:
            observation = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "trusted-base scenario has no configured runtime",
                runtime.runner,
                runtime.id,
            )
        elif "execute" not in surface.capabilities:
            observation = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "trusted-base surface is not configured for explicit execution",
                runtime.runner,
                runtime.id,
            )
        elif registry.get(runtime.runner) is None:
            observation = SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"configured runner {runtime.runner!r} is unavailable",
                runtime.runner,
                runtime.id,
            )
        else:
            execution_context = replace(context, config=trusted_config)
            observation = registry.run(
                execution_context,
                surface,
                side=side,
                cleanup_workspace=True,
            )

        record = normalize_surface_run(request, scenario, observation)
        path = surface.affected_paths[0] if surface.affected_paths else ""
        evidence = persist_validation_record(
            store,
            context.revision,
            request,
            scenario,
            record,
            path=path,
            expected_current_revision_id=expected_current_revision_id,
            execution_intent=execution_intent,
            # The dispatcher configured this registry; its class, not the runner's claim.
            executor_class=getattr(registry, "executor_class", ExecutorClass.AUTHOR_MACHINE),
        )
        return LocalValidationOutcome(request=request, scenario=scenario, record=record, evidence=evidence)


def validate_reproduction_proposal(
    value: Mapping[str, Any],
    *,
    configured_scenario_ids: Sequence[str],
    review_target_ids: Sequence[str],
) -> ReproductionProposal:
    """Accept only a bounded pointer to trusted validation configuration."""

    allowed_keys = {"scenario_id", "target_ids", "reason", "expected_behavior"}
    extra = set(value) - allowed_keys
    if extra:
        raise ValueError(f"reproduction proposal contains unsupported fields: {', '.join(sorted(extra))}")
    scenario_id = str(value.get("scenario_id") or "").strip()
    if scenario_id not in set(configured_scenario_ids):
        raise ValueError("reproduction proposal references an unknown or untrusted scenario")
    raw_targets = value.get("target_ids")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes, bytearray)):
        raise ValueError("reproduction proposal target_ids must be a list")
    targets = tuple(dict.fromkeys(str(item) for item in raw_targets if str(item)))
    known_targets = set(review_target_ids)
    if not targets or not set(targets) <= known_targets:
        raise ValueError("reproduction proposal targets must belong to this Review")
    reason = str(value.get("reason") or "").strip()[:600]
    expected = str(value.get("expected_behavior") or "").strip()[:600]
    if not reason or not expected:
        raise ValueError("reproduction proposal requires reason and expected_behavior")
    return ReproductionProposal(scenario_id, targets, reason, expected)


__all__ = [
    "RUNTIME_VALIDATION_MEDIA_TYPE",
    "VALIDATION_EXECUTION_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "LocalValidationOutcome",
    "ReproductionProposal",
    "ValidationRequest",
    "ValidationScenario",
    "browser_policy_digest",
    "build_browser_validation_request",
    "build_surface_validation_request",
    "execute_local_surface_validation",
    "normalize_surface_run",
    "persist_validation_record",
    "runtime_config_digest",
    "snapshot_semantic_identity",
    "surface_validation_scenario",
    "trusted_policy_digest",
    "validate_artifact_reference",
    "validate_reproduction_proposal",
    "validation_source_ref",
    "validation_summary",
]
