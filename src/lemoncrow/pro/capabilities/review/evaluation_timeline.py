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

from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

_TYPED = frozenset({"provider_ci", "lemoncrow_verification", "signed_attestation", "trusted_security_feed"})
_STATUSES = frozenset({"PASS", "FAIL", "NOT_RUN", "UNKNOWN"})
_MAX_ARTIFACT_BYTES = 256 * 1024


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
    if value.get("identity") != request.get("identity"):
        return None
    # A stored verdict row and its immutable artifact must agree. Unknown or
    # malformed records cannot resurrect an old PASS from row metadata.
    if evidence.verification_status and value.get("status") != evidence.verification_status:
        return None
    return value


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
    embedded = record.get("data")
    execution = embedded.get("execution") if isinstance(embedded, dict) else None
    environment = str(execution.get("environment_fingerprint") or "") if isinstance(execution, dict) else ""
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
    trusted = evidence.attestation_class in _TYPED and evidence.executor_class in {
        "author_machine",
        "customer_worker",
        "managed_sandbox",
    }
    return {
        "id": evidence.id,
        "revision_id": evidence.revision_id,
        "source": evidence.source,
        "title": evidence.title[:180],
        "scenario": recipe,
        "evaluator_config_digest": config_digest,
        "status": str(record.get("status") or "UNKNOWN") if trusted else "REPORTED",
        "raw_attempt": not bool(evidence.verification_status),
        "exit_code": exit_code,
        "duration_ms": record.get("duration_ms") if isinstance(record.get("duration_ms"), int) else None,
        "executor_class": evidence.executor_class if trusted else "unverified",
        "environment_fingerprint": environment[:120],
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
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in evidence:
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
                        "revision_id": revision.id,
                        "title": item.title[:180],
                        "scenario": (item.stable_key or item.source_ref or item.id)[:180],
                        "evaluator_config_digest": "",
                        "status": "UNVERIFIED",
                        "raw_attempt": True,
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
            # No duplicate synthetic successes on repeated projections.
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": item.id,
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
        verdicts = [row["status"] for row in rows if not row["raw_attempt"] and row["trust"] == "typed_observation"]
        counts = {status.lower(): verdicts.count(status) for status in ("PASS", "FAIL", "NOT_RUN", "UNKNOWN")}
        config_ids = sorted(
            {row["scenario"] for row in rows if row["trust"] == "typed_observation" and row["scenario"]}
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
            for row in old["receipts"]
            if row.get("evaluator_config_digest")
            and not row["raw_attempt"]
            and row["trust"] == "typed_observation"
            and row["status"] in {"PASS", "FAIL"}
        }
        comparable = []
        for row in new["receipts"]:
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
