"""Conservative Snapshot evaluation timeline over existing immutable ReviewEvidence.

This is a read-only materialized *view*, not an alternate evaluation authority:
only persisted evidence and its exact Snapshot determine a row. No score is
invented, a runner attempt is not a required-check verdict, and results on
other Snapshots never attest to the selected Snapshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Any

from lemoncrow.pro.capabilities.review.execution import ExecutionResult, canonical_digest
from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

_TYPED = frozenset({"provider_ci", "lemoncrow_verification", "signed_attestation", "trusted_security_feed"})
_STATUSES = frozenset({"PASS", "FAIL", "NOT_RUN", "UNKNOWN"})
_MAX_ARTIFACT_BYTES = 256 * 1024
# A summary read may verify a handful of protected artifacts, but must never
# force arbitrarily large screenshot/video/blob reads into the Review process.
_MAX_VERIFIED_RUN_BYTES = 8 * 1024 * 1024
_MAX_VERIFIED_RUN_REFS = 32
_RUN_ARTIFACT_ROLES = frozenset(
    {"screenshot", "image", "video", "trace", "playwright_trace", "log", "stdout", "stderr", "report"}
)


def _available_run_artifact_roles(store: Any, evidence: Any, refs: Sequence[Any]) -> tuple[list[str], list[str]]:
    """Verify protected bytes and metadata before advertising an inspectable role.

    Historical execution proof can survive retention of large raw artifacts.
    Missing/corrupt/oversized artifacts must not advertise an inspection action;
    none of these checks upgrades the runner's process provenance.
    """
    declared = {ref.role for ref in refs if ref.role in _RUN_ARTIFACT_ROLES}
    if not declared:
        return [], []
    try:
        artifacts = store.run_artifacts()
    except (OSError, ValueError, AttributeError):
        artifacts = None
    if artifacts is None or len(refs) > _MAX_VERIFIED_RUN_REFS:
        return [], sorted(declared)
    verified: set[str] = set()
    invalid: set[str] = set()
    remaining_bytes = _MAX_VERIFIED_RUN_BYTES
    for ref in refs:
        if ref.role not in declared:
            continue
        if (
            ref.bytes <= 0
            or ref.bytes > remaining_bytes
            or len(ref.digest) != 64
            or any(char not in "0123456789abcdef" for char in ref.digest)
            or ref.ref != f"run-artifact:{evidence.review_id}:{ref.digest}"
        ):
            invalid.add(ref.role)
            continue
        try:
            meta = artifacts.meta(evidence.review_id, ref.digest)
            if (
                not isinstance(meta, dict)
                or meta.get("digest") != ref.digest
                or meta.get("role") != ref.role
                or meta.get("media_type") != ref.media_type
                or type(meta.get("bytes")) is not int
                or meta["bytes"] != ref.bytes
            ):
                invalid.add(ref.role)
                continue
            # Aggregate read budget for *this receipt*, not one budget per
            # reference. Charge before reading, including corrupt/missing data.
            remaining_bytes -= ref.bytes
            payload = artifacts.read(evidence.review_id, ref.digest)
        except (OSError, ValueError, KeyError, TypeError):
            invalid.add(ref.role)
            continue
        if (
            isinstance(payload, bytes)
            and len(payload) == ref.bytes
            and hashlib.sha256(payload).hexdigest() == ref.digest
        ):
            verified.add(ref.role)
        else:
            invalid.add(ref.role)
    # The protected viewer may select any of multiple references for one role;
    # do not advertise the role if any candidate is corrupt.
    verified.difference_update(invalid)
    return sorted(verified), sorted(declared - verified)


def _attempt_receipt_id(evidence: Any, record: dict[str, Any]) -> str:
    """Unique identity for *persisted evidence*, not a dispatcher/run lease.

    A semantic request identity can recur across fresh reruns. The evidence id
    and verified artifact digest distinguish those attempts without claiming a
    worker-signed execution id or an observed start/end timestamp.
    """
    document = {
        "kind": "runtime-attempt-receipt/v1",
        "review_id": evidence.review_id,
        "revision_id": evidence.revision_id,
        "evidence_id": evidence.id,
        "artifact_digest": evidence.content_hash,
        "request_identity": record["identity"],
    }
    return (
        "receipt:"
        + hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    )


def _verified_artifact(store: Any, evidence: Any) -> dict[str, Any] | None:
    if evidence.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE or not evidence.content_hash:
        return None
    blob = store.read_evidence_artifact(evidence)
    if not isinstance(blob, bytes) or len(blob) > _MAX_ARTIFACT_BYTES:
        return None
    if hashlib.sha256(blob).hexdigest() != evidence.content_hash:
        return None
    try:
        value = json.loads(blob)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get("snapshot_id") != evidence.revision_id:
        return None
    request = value.get("request")
    if not isinstance(request, dict) or request.get("revision_id") != evidence.revision_id:
        return None
    if request.get("review_id") != evidence.review_id:
        return None
    # Do not merely compare two caller-controlled copies of identity. Rebuild
    # the canonical digest of the actual persisted request, then require both
    # the record and embedded request to agree with it.
    request_document = dict(request)
    embedded_identity = request_document.pop("identity", None)
    if not isinstance(embedded_identity, str):
        return None
    actual_identity = hashlib.sha256(
        json.dumps(request_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    if value.get("identity") != actual_identity or embedded_identity != actual_identity:
        return None
    # A queue receipt is immutable source metadata, not independent process
    # proof. Corrupt/misbound queue associations cannot count in history.
    if "queue_completion_receipt" in value:
        try:
            from lemoncrow.pro.capabilities.review.runtime_validation import (
                ValidationRequest,
                validate_queue_completion_receipt,
                validation_source_ref,
            )

            validation_request = ValidationRequest(
                **{key: val for key, val in request.items() if key not in {"identity", "target_ids"}},
                target_ids=tuple(request["target_ids"]),
            )
            receipt = validate_queue_completion_receipt(value["queue_completion_receipt"], validation_request)
            if evidence.source != "external" or evidence.source_ref != validation_source_ref(
                validation_request,
                execution_intent=evidence.execution_intent,
                fresh_ref=receipt["job_id"],
            ):
                return None
        except (TypeError, ValueError, KeyError):
            return None
    # A stored verdict row and its immutable artifact must agree.
    if evidence.verification_status and value.get("status") != evidence.verification_status:
        return None
    return value


def _typed_execution_provenance(store: Any, record: dict[str, Any], evidence: Any) -> dict[str, Any]:
    """Validate the runner's existing protected execution wire, expose metadata only.

    The evidence artifact is authenticated by the Review store hash. The inner
    result additionally has its own digest, executor class and exact subject
    binding. No argv, environment values, output, artifact keys, or network
    destinations are returned through the timeline API.
    """
    none: dict[str, Any] = {
        "execution_provenance": "not_recorded",
        "execution_identity": "",
        "environment_fingerprint": "",
        "recipe_digest": "",
        "stage_outcomes": [],
        "artifact_roles": [],
        "unavailable_artifact_roles": [],
        "unverified_integrations": 0,
    }
    data = record.get("data")
    if not isinstance(data, dict):
        return none
    wire = data.get("execution")
    if wire is None:
        return {**none, "execution_provenance": "rejected" if data.get("execution_error") else "not_recorded"}
    if not isinstance(wire, dict):
        return {**none, "execution_provenance": "rejected"}
    try:
        # A forged result_digest must never be treated as the validated runner
        # record, even when the outer evidence artifact has a valid hash.
        result_digest = wire.get("result_digest")
        if not isinstance(result_digest, str) or result_digest != canonical_digest(
            {key: value for key, value in wire.items() if key != "result_digest"}
        ):
            raise ValueError("execution digest mismatch")
        # ExecutionResult.from_wire supports historical records by normalizing
        # a mismatched embedded verification executor to the outer executor.
        # A receipt intended as proof must be stricter than compatibility load.
        raw_verification = wire.get("verification")
        if not isinstance(raw_verification, dict) or raw_verification.get("executor_class") != evidence.executor_class:
            raise ValueError("untrusted execution verification class")
        execution = ExecutionResult.from_wire(wire)
        # Compatibility loading can silently discard malformed stage entries.
        # Hosted workers may emit multiple bounded fetch/build operations; the
        # actual run must be unique and no input row may disappear.
        raw_stages = wire.get("stages")
        stages = execution.stages
        if (
            not isinstance(raw_stages, list)
            or len(raw_stages) != len(stages)
            or len(stages) > 9
            or sum(stage.stage == "run" for stage in stages) > 1
            or wire.get("status") != execution.status
        ):
            raise ValueError("execution stage record is ambiguous or contradictory")
        request = record["request"]
        if execution.executor_class.value != evidence.executor_class:
            raise ValueError("executor class mismatch")
        subject = execution.subject
        if subject.review_id != evidence.review_id or subject.revision_id != evidence.revision_id:
            raise ValueError("execution belongs to another Snapshot")
        # ExecutionSubject identifies its materialized source/overlay, while
        # ValidationRequest identifies the semantic scenario Snapshot. Both
        # must bind to the same Review/revision; their two digests are not
        # interchangeable and must not be compared directly.
        if subject.side != request.get("side"):
            raise ValueError("execution side differs from request")
        if str(wire.get("environment_fingerprint") or "") != execution.environment_fingerprint:
            raise ValueError("environment fingerprint mismatch")
        if execution.verification.executor_class is not execution.executor_class:
            raise ValueError("verification's executor class disagrees")
        # The outer verdict is not independent of the embedded process stages.
        # A recorded PASS cannot be established by an execution that failed,
        # was refused/skipped, or claims exit 0 despite a nonzero process code.
        # A failing outer verdict with passing process stages remains possible
        # (e.g. a browser assertion failed after the process ran successfully).
        run_stage = execution.stage("run")
        if record.get("status") == "PASS" and (
            execution.status != "passed"
            or run_stage is None
            or run_stage.status != "passed"
            or any(stage.status == "passed" and stage.exit_code not in (None, 0) for stage in execution.stages)
        ):
            raise ValueError("outer PASS contradicts observed execution stages")
    except (TypeError, ValueError, KeyError):
        return {**none, "execution_provenance": "rejected"}
    # The role allowlist avoids leaking user-controlled file paths, opaque
    # artifact keys and external URLs through the public Review summary.
    available_roles, unavailable_roles = _available_run_artifact_roles(store, evidence, execution.artifacts)
    # Always show the run outcome, even if the worker reported many bounded
    # dependency stages. Select a failing dependency before a passing one.
    summaries = []
    for name in ("fetch", "build", "run"):
        options = [stage for stage in execution.stages if stage.stage == name]
        if not options:
            continue
        selected = next(
            (stage for stage in options if stage.status not in {"passed", "skipped"}),
            next((stage for stage in options if stage.status == "passed"), options[0]),
        )
        summaries.append({"stage": name, "status": selected.status, "exit_code": selected.exit_code})
    stage_summaries = summaries
    return {
        "execution_provenance": "validated",
        "execution_identity": execution.identity,
        "environment_fingerprint": execution.environment_fingerprint,
        "recipe_digest": execution.recipe.digest,
        "stage_outcomes": stage_summaries,
        "artifact_roles": available_roles,
        "unavailable_artifact_roles": unavailable_roles,
        "unverified_integrations": len(execution.verification.not_verified),
    }


def execution_receipt(store: Any, evidence: Any) -> dict[str, Any] | None:
    """Safe summary of a persisted attempt, not a claim that its check passed.

    No stdout, arbitrary command-line secrets, raw environment variables or
    URLs leave the evidence store in this endpoint. Full records remain behind
    the existing authenticated evidence viewer. A runner's claimed class is
    never used: the dispatcher-owned evidence field decides the trust label.
    """
    record = _verified_artifact(store, evidence)
    if record is None:
        return None
    raw = record.get("deterministic_checks")
    checks = raw if isinstance(raw, list) else []
    codes = [row.get("actual") for row in checks if isinstance(row, dict) and row.get("name") == "process exit code"]
    exit_code = codes[0] if codes and isinstance(codes[0], int) and not isinstance(codes[0], bool) else None
    provenance = _typed_execution_provenance(store, record, evidence)
    request = record["request"]
    recipe = str(request.get("scenario_id") or "")[:180]
    config_values = [
        request.get("runtime_config_digest"),
        request.get("trusted_policy_digest"),
        request.get("scope_digest"),
    ]
    config_digest = (
        hashlib.sha256(json.dumps(config_values, separators=(",", ":")).encode("utf-8")).hexdigest()
        if all(
            isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
            for value in config_values
        )
        else ""
    )
    trusted = (
        provenance["execution_provenance"] != "rejected"
        and evidence.attestation_class in _TYPED
        and evidence.executor_class
        in {
            "author_machine",
            "customer_worker",
            "managed_sandbox",
        }
    )
    return {
        "id": evidence.id,
        "attempt_receipt_id": _attempt_receipt_id(evidence, record),
        # Database evidence insertion time, NOT the worker's start/end time.
        "attempt_recorded_at": str(evidence.created_at or "")[:40],
        "attempt_lifecycle": (
            "observed_run"
            if provenance["execution_provenance"] == "validated"
            and any(stage["stage"] == "run" for stage in provenance["stage_outcomes"])
            else "rejected" if provenance["execution_provenance"] == "rejected" else "record_only"
        ),
        "revision_id": evidence.revision_id,
        "source": evidence.source,
        "title": evidence.title[:180],
        "scenario": recipe,
        "evaluator_config_digest": config_digest,
        "status": (
            str(record.get("status") or "UNKNOWN")
            if trusted
            else "UNVERIFIED" if provenance["execution_provenance"] == "rejected" else "REPORTED"
        ),
        "raw_attempt": not bool(evidence.verification_status),
        "exit_code": None if provenance["execution_provenance"] == "rejected" else exit_code,
        "duration_ms": (
            record.get("duration_ms")
            if provenance["execution_provenance"] != "rejected"
            and isinstance(record.get("duration_ms"), int)
            and not isinstance(record.get("duration_ms"), bool)
            and record["duration_ms"] >= 0
            else None
        ),
        "executor_class": evidence.executor_class if trusted else "unverified",
        # A valid typed runner digest (even with a fenced durable queue
        # completion) validates record consistency, not independently signed
        # process execution. The separately paired Ed25519 verifier is checked
        # at the hosted publication boundary, not by this generic history
        # reader. Never infer independent execution from executor class alone.
        "independent_execution": False,
        "environment_fingerprint": provenance["environment_fingerprint"],
        "execution_provenance": provenance["execution_provenance"],
        "execution_identity": provenance["execution_identity"],
        "recipe_digest": provenance["recipe_digest"],
        "stage_outcomes": provenance["stage_outcomes"],
        "artifact_roles": provenance["artifact_roles"],
        "unavailable_artifact_roles": provenance["unavailable_artifact_roles"],
        "unverified_integrations": provenance["unverified_integrations"],
        "artifact_digest": evidence.content_hash,
        "evidence_id": evidence.id,
        "scope": str(request.get("side") or "")[:16],
        "trust": "typed_observation" if trusted else "reported_only",
    }


def evaluation_timeline(
    store: Any,
    review_id: str,
    *,
    selected_revision_id: str = "",
    limit: int = 6,
    counted_evidence: Callable[[Any], Sequence[Any]] | None = None,
) -> dict[str, Any]:
    """Read up to 6 actual Snapshots (latest first); fail closed on absent data.

    Every evaluator is comparable only when its own stable identity and known
    config digest match. A blank config is *unknown*, never silently equal.
    """
    revisions = list(store.list_revisions(review_id))
    if not revisions:
        return {"schema_version": 1, "selected_revision_id": selected_revision_id, "snapshots": [], "current": None}
    index = next((i for i, rev in enumerate(revisions) if rev.id == selected_revision_id), len(revisions) - 1)
    selected = revisions[index]
    window = list(reversed(revisions[max(0, index - min(max(1, limit), 6) + 1) : index + 1]))
    # The caller's authority filter excludes revoked signed attestations using
    # this Snapshot's trusted base; a raw store listing cannot establish trust.
    snapshots: list[dict[str, Any]] = []
    for revision in window:
        evidence = counted_evidence(revision) if counted_evidence else store.list_evidence(review_id)
        # Newest first: repeated runs of one check on the same Snapshot have
        # different evidence IDs but only the last verdict is current.
        newest_first = sorted(evidence, key=lambda item: (item.created_at, item.id), reverse=True)
        rows: list[dict[str, Any]] = []
        for item in newest_first:
            if item.revision_id != revision.id or item.kind != "verification":
                continue
            receipt = execution_receipt(store, item)
            if receipt:
                rows.append(receipt)
                continue
            if item.mime_type == RUNTIME_VALIDATION_MEDIA_TYPE:
                # A corrupted, oversized or wrong-Snapshot runtime artifact
                # must never fall back to its metadata's historical PASS.
                rows.append(
                    {
                        "id": item.id,
                        "attempt_receipt_id": "",
                        "attempt_recorded_at": "",
                        "attempt_lifecycle": "rejected",
                        "revision_id": revision.id,
                        "title": item.title[:180],
                        "scenario": (item.stable_key or item.source_ref or item.id)[:180],
                        "evaluator_config_digest": "",
                        "status": "UNVERIFIED",
                        "raw_attempt": not bool(item.verification_status),
                        "exit_code": None,
                        "executor_class": "unverified",
                        "artifact_digest": item.content_hash,
                        "evidence_id": item.id,
                        "trust": "reported_only",
                        "environment_fingerprint": "",
                        "duration_ms": None,
                        "scope": "",
                    }
                )
                continue
            # Imported typed attestations may have no command execution. Keep
            # them separate from receipts and from self-reported observations.
            status = str(item.verification_status or "")
            if status not in _STATUSES:
                continue
            authority = "typed_observation" if item.attestation_class in _TYPED else "reported_only"
            key = item.stable_key or item.source_ref or item.id
            rows.append(
                {
                    "id": item.id,
                    "attempt_receipt_id": "",
                    "attempt_recorded_at": "",
                    "attempt_lifecycle": "record_only",
                    "revision_id": revision.id,
                    "title": item.title[:180],
                    "scenario": key[:180],
                    "evaluator_config_digest": "",
                    "status": status if authority == "typed_observation" else "REPORTED",
                    "raw_attempt": False,
                    "exit_code": None,
                    "executor_class": "unverified",
                    "artifact_digest": item.content_hash,
                    "evidence_id": item.id,
                    "trust": authority,
                    "environment_fingerprint": "",
                    "duration_ms": None,
                    "scope": "",
                }
            )
        effective: list[dict[str, Any]] = []
        seen_checks: set[tuple[str, str]] = set()
        for row in rows:
            if row["raw_attempt"] or (row["trust"] != "typed_observation" and row["status"] != "UNVERIFIED"):
                continue
            key = (row["scenario"], row.get("scope") or "")
            if key in seen_checks:
                row["superseded_by_newer_result"] = True
                continue
            seen_checks.add(key)
            effective.append(row)
        verdicts = [row["status"] for row in effective if row["trust"] == "typed_observation"]
        counts = {status.lower(): verdicts.count(status) for status in ("PASS", "FAIL", "NOT_RUN", "UNKNOWN")}
        config_ids = sorted(
            {row["scenario"] for row in effective if row["trust"] == "typed_observation" and row["scenario"]}
        )
        # Only evaluator identity is available for imported results; external
        # configuration is not exposed here, therefore comparability unknown.
        snapshots.append(
            {
                "revision_id": revision.id,
                "snapshot_number": max(0, revision.revision_number - 1),
                "head_sha": revision.head_sha,
                "source_identity": revision.source_fingerprint,
                "counts": counts,
                "receipts": rows[:60],
                "effective_outcomes": effective[:60],
                "receipts_omitted": max(0, len(rows) - 60),
                "state": "failed" if counts["fail"] else "observed" if counts["pass"] else "unverified",
                "evaluators": config_ids,
                "configuration_comparable": False,
                "comparable_results": [],
                "reason": (
                    "No independently verified evaluator configuration digest" if rows else "No recorded evaluations"
                ),
            }
        )
    # Same scenario and digest across adjacent revisions is a comparable
    # *observed result* (not an accuracy score, review approval or merge gate).
    for new, old in pairwise(snapshots):
        old_results = {
            (row["scenario"], row["evaluator_config_digest"]): row
            for row in old["effective_outcomes"]
            if row.get("evaluator_config_digest")
            and not row["raw_attempt"]
            and row["trust"] == "typed_observation"
            and row["status"] in {"PASS", "FAIL"}
        }
        comparable = []
        for row in new["effective_outcomes"]:
            if row["raw_attempt"] or row["trust"] != "typed_observation" or row["status"] not in {"PASS", "FAIL"}:
                continue
            key = (row["scenario"], row.get("evaluator_config_digest") or "")
            past = old_results.get(key) if key[1] else None
            if past is None:
                continue
            comparable.append(
                {
                    "scenario": row["scenario"],
                    "from": past["status"],
                    "to": row["status"],
                    "change": (
                        "regression"
                        if past["status"] == "PASS" and row["status"] == "FAIL"
                        else "recovered" if past["status"] == "FAIL" and row["status"] == "PASS" else "unchanged"
                    ),
                    "evidence_ids": [past["evidence_id"], row["evidence_id"]],
                }
            )
        if comparable:
            new["configuration_comparable"] = True
            new["comparable_results"] = comparable[:60]
            new["reason"] = f"{len(comparable)} evaluator(s) have matching recorded configurations"
    for snapshot in snapshots:
        # Internal aggregate only; the API already exposes bounded detailed receipts.
        snapshot.pop("effective_outcomes", None)
    return {
        "schema_version": 1,
        "review_id": review_id,
        "selected_revision_id": selected.id,
        "snapshots": snapshots,
        "current": snapshots[0],
        "limitations": [
            "This is a historical view of existing evidence, not a new execution or calibrated score.",
            "Reported observations and unrun checks never count as passing executed checks.",
            "Cross-Snapshot evaluator configurations are not yet independently verified; trend scores are withheld.",
        ],
    }
