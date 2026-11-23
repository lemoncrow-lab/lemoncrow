"""Standalone Review Companion customer-worker HTTPS lease transport.

Uses the *existing* hosted PostgreSQL Review job protocol and Ed25519 lease
envelopes; never connects to MCP, never executes shell/agent commands, and
never trusts a task notification as authority. WSS may eventually wake this
client, but only signed durable HTTPS claims can authorize actual work.

The worker binding is operator-provisioned until hosted pairing and revocation
are implemented. An ordinary interactive user token is not a machine grant.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .transport import HttpTransport

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_BASE64 = re.compile(r"[A-Za-z0-9_-]+\Z")
_ALLOWED_CLASSES = frozenset({"review_intelligence", "runtime_validation"})
_MAX_ENVELOPE = 128 * 1024


@dataclass(frozen=True)
class WorkerBinding:
    """Provisioned machine authorization and pinned signer, NOT OAuth login state."""

    origin: str
    org_id: str
    worker_id: str
    repo_id: str
    bearer: str
    trusted_key_id: str
    trusted_public_key_hex: str

    def __post_init__(self) -> None:
        parts = urlsplit(self.origin)
        if (
            (
                parts.scheme != "https"
                and not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1", "localhost"})
            )
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError("Review worker origin must be HTTPS or explicit loopback")
        if any(
            not isinstance(value, str) or not _REPO.fullmatch(value)
            for value in (self.org_id, self.worker_id, self.repo_id, self.trusted_key_id)
        ):
            raise ValueError(
                "Review worker identity must be scoped to one bounded organization, machine and repository"
            )
        if (
            not isinstance(self.bearer, str)
            or len(self.bearer) < 20
            or len(self.bearer) > 8192
            or any(char in self.bearer for char in "\r\n ")
        ):
            raise ValueError("Review worker requires a separate bounded machine bearer")
        if len(self.trusted_public_key_hex) != 64:
            raise ValueError("Review worker requires an explicitly pinned Ed25519 public key")
        try:
            bytes.fromhex(self.trusted_public_key_hex)
        except ValueError as exc:
            raise ValueError("Review worker signing key is not valid hex") from exc


@dataclass(frozen=True)
class WorkerLease:
    job_id: str
    review_id: str
    revision_id: str
    snapshot_id: str
    snapshot_digest: str
    policy_digest: str
    execution_class: str
    capability: str
    attempt: int
    deadline_at: float
    expires_at: float
    envelope: str
    spec: Mapping[str, Any]
    model_assignment: Mapping[str, Any] | None = None

    @property
    def is_current(self) -> bool:
        return self.expires_at > time.time() and self.deadline_at > time.time()


def _base64_decode(raw: str) -> bytes:
    if not isinstance(raw, str) or not _BASE64.fullmatch(raw):
        raise ValueError("invalid Review worker envelope encoding")
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid Review worker envelope encoding") from exc


def validate_lease(
    token: str,
    binding: WorkerBinding,
    *,
    now: float,
    expected_class: str,
) -> WorkerLease:
    """Fail-closed pinned Ed25519 signature, audience, lease and Snapshot check.

    Importing cryptography lazily keeps unauthenticated lcr watch dependency free;
    an unavailable verifier *refuses* leases instead of accepting claims.
    """
    if expected_class not in _ALLOWED_CLASSES:
        raise ValueError("unsupported Review worker execution class")
    if not isinstance(token, str) or len(token.encode("utf-8")) > _MAX_ENVELOPE:
        raise ValueError("invalid Review worker envelope size")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "lcw1":
        raise ValueError("invalid Review worker envelope format")
    try:
        document = json.loads(_base64_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Review worker envelope document") from exc
    if not isinstance(document, dict):
        raise ValueError("Review worker envelope must be an object")
    if document.get("kid") != binding.trusted_key_id or document.get("aud") != binding.worker_id:
        raise ValueError("Review worker envelope signer/audience mismatch")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ValueError("Review lease cannot run without the Ed25519 verifier") from exc
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(binding.trusted_public_key_hex))
    try:
        key.verify(_base64_decode(parts[2]), (parts[0] + "." + parts[1]).encode("ascii"))
    except InvalidSignature as exc:
        raise ValueError("Review worker envelope signature mismatch") from exc
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Review worker envelope has no signed job spec")
    if (
        spec.get("org_id") != binding.org_id
        or spec.get("repo_id") != binding.repo_id
        or spec.get("execution_locus") != "customer"
        or spec.get("execution_class") != expected_class
    ):
        raise ValueError("Review worker lease has wrong tenant/repository/locus/class")
    try:
        issued = float(document["iat"])
        expires = float(document["exp"])
        lease_expiry = float(document["lease_expires_at"])
        deadline = float(spec["deadline_at"])
        attempt = document["attempt"]
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Review worker lease has invalid time/attempt fields") from exc
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("Review worker attempt is invalid")
    if issued > now + 30 or min(expires, lease_expiry, deadline) <= now or expires <= issued:
        raise ValueError("Review worker lease is stale or expired")
    for field in ("input_digest", "policy_digest", "semantic_snapshot_digest", "contract_digest"):
        raw = document.get(field) if field == "contract_digest" else spec.get(field)
        if not isinstance(raw, str) or not _DIGEST.fullmatch(raw):
            raise ValueError("Review worker lease is missing signed digest identity")
    for field in ("review_id", "revision_id", "snapshot_id"):
        raw = spec.get(field)
        if not isinstance(raw, str) or not _REPO.fullmatch(raw):
            raise ValueError("Review worker lease is not bound to an immutable Review revision")
    if expected_class == "runtime_validation" and spec.get("requires_repository_execution") is not True:
        raise ValueError("independent execution must be explicitly identified by its signed job")
    if not isinstance(document.get("lease_token"), str) or not document["lease_token"]:
        raise ValueError("Review worker lease token missing")
    if not isinstance(document.get("job_id"), str) or not _REPO.fullmatch(document["job_id"]):
        raise ValueError("Review worker job identity missing")
    assignment = document.get("assignment")
    if assignment is not None and not isinstance(assignment, dict):
        raise ValueError("Review worker model route assignment is malformed")
    return WorkerLease(
        job_id=document["job_id"],
        review_id=spec["review_id"],
        revision_id=spec["revision_id"],
        snapshot_id=spec["snapshot_id"],
        snapshot_digest=spec["semantic_snapshot_digest"],
        policy_digest=spec["policy_digest"],
        execution_class=expected_class,
        capability=str(spec.get("capability") or ""),
        attempt=attempt,
        deadline_at=deadline,
        expires_at=min(expires, lease_expiry),
        envelope=token,
        spec=spec,
        model_assignment=assignment,
    )


class ReviewWorkerClient:
    """Explicit, operator-provisioned Review-only worker claim and lease lifecycle.

    No background daemon, web socket, agent adapter, shell or auto verification.
    A caller must additionally qualify the authorized executor before work.
    """

    def __init__(
        self,
        binding: WorkerBinding,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.binding = binding
        self.transport = transport or HttpTransport(binding.origin, timeout_s=15)
        self.clock = clock

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.binding.bearer}"}

    def claim(
        self,
        *,
        execution_class: str,
        model_route_id: str = "",
        exact_job_id: str = "",
    ) -> WorkerLease | None:
        """Durable HTTPS claim, never a push-notification or unilateral local shell grant."""
        if execution_class not in _ALLOWED_CLASSES:
            raise ValueError("only Review AI and independent validation are claimable")
        if execution_class == "review_intelligence" and not model_route_id:
            raise ValueError("AI work requires one operator-attested model route")
        if execution_class != "review_intelligence" and model_route_id:
            raise ValueError("verification workers cannot acquire AI routes")
        if exact_job_id and (execution_class != "review_intelligence" or not _REPO.fullmatch(exact_job_id)):
            raise ValueError("exact job claims require a bounded Review Intelligence identity")
        reply = self.transport.post(
            "/v1/review-workers/claim",
            body={
                "repository_ids": [self.binding.repo_id],
                "execution_class": execution_class,
                "model_route_ids": [model_route_id] if model_route_id else [],
                "max_concurrency": 1,
                **({"exact_job_id": exact_job_id} if exact_job_id else {}),
            },
            headers=self._headers(),
        ).require()
        job = reply.get("job")
        if job is None:
            return None
        if not isinstance(job, Mapping):
            raise ValueError("Review worker claim returned a malformed job")
        envelope = reply.get("envelope")
        if not isinstance(envelope, str):
            raise ValueError("Review worker claim has no signed envelope")
        lease = validate_lease(envelope, self.binding, now=self.clock(), expected_class=execution_class)
        if any(
            job.get(key) != getattr(lease, attr)
            for key, attr in (
                ("job_id", "job_id"),
                ("review_id", "review_id"),
                ("revision_id", "revision_id"),
                ("execution_class", "execution_class"),
            )
        ):
            raise ValueError("unsigned Review claim metadata disagrees with signed lease")
        if exact_job_id and (lease.job_id != exact_job_id or lease.capability != "review_intelligence:agent_task"):
            raise ValueError("signed lease is not the explicitly requested author-agent job")
        return lease

    def claim_unattended(self, *, model_route_id: str, task_kinds: tuple[str, ...]) -> tuple[WorkerLease | None, str]:
        """Claim the next queued author-agent task under the machine's admin-granted unattended budget.

        Returns ``(lease, "")``, ``(None, "")`` when nothing is queued, or ``(None, code)`` when the server refused
        (``organization_off``, ``device_off``, ``daily_cost_cap``, ``daily_task_cap``, ``stopped``). A lease must carry
        the server's signed per-task cap; the server reserved it before leasing.
        """
        kinds = sorted(set(task_kinds))
        if not model_route_id or not kinds or any(kind not in {"investigate", "propose_fix"} for kind in kinds):
            raise ValueError("unattended claims need one model route and known task kinds")
        reply = self.transport.post(
            "/v1/review-workers/claim",
            body={
                "repository_ids": [self.binding.repo_id],
                "execution_class": "review_intelligence",
                "model_route_ids": [model_route_id],
                "max_concurrency": 1,
                "unattended": {"task_kinds": kinds},
            },
            headers=self._headers(),
        ).require()
        job = reply.get("job")
        if job is None:
            refusal = reply.get("unattended_refusal", "")
            if not isinstance(refusal, str) or len(refusal) > 64:
                raise ValueError("Review unattended claim returned a malformed refusal")
            return None, refusal
        envelope = reply.get("envelope")
        if not isinstance(job, Mapping) or not isinstance(envelope, str):
            raise ValueError("Review worker claim returned a malformed job")
        lease = validate_lease(envelope, self.binding, now=self.clock(), expected_class="review_intelligence")
        signed = (lease.model_assignment or {}).get("unattended")
        if (
            job.get("job_id") != lease.job_id
            or lease.capability != "review_intelligence:agent_task"
            or not isinstance(signed, Mapping)
            or not isinstance(signed.get("run_id"), str)
            or type(signed.get("max_task_cost_micros")) is not int
            or not 1 <= signed["max_task_cost_micros"] <= 100_000_000
            or lease.spec.get("scope", {}).get("task_kind") not in kinds
        ):
            raise ValueError("signed lease is not a budgeted unattended author-agent task")
        return lease, ""

    def heartbeat(self, lease: WorkerLease) -> str:
        """Reconfirm durable lease, cancellation, currentness before every operation."""
        self._require_lease(lease)
        reply = self.transport.post(
            f"/v1/review-workers/{lease.job_id}/heartbeat",
            body={"envelope": lease.envelope},
            headers=self._headers(),
        ).require()
        if reply.get("job_id") != lease.job_id:
            raise ValueError("Review heartbeat returned a different job")
        state = reply.get("state")
        if state not in {"running", "cancel_requested", "cancelled"}:
            raise ValueError("Review heartbeat returned an invalid lease state")
        return str(state)

    def context(self, lease: WorkerLease) -> Mapping[str, Any]:
        self._require_lease(lease)
        if self.heartbeat(lease) != "running":
            raise ValueError("Review worker job was cancelled")
        reply = self.transport.post(
            f"/v1/review-workers/{lease.job_id}/context",
            body={"envelope": lease.envelope},
            headers=self._headers(),
        ).require()
        if any(
            reply.get(key) != value
            for key, value in (
                ("job_id", lease.job_id),
                ("review_id", lease.review_id),
                ("revision_id", lease.revision_id),
                ("snapshot_id", lease.snapshot_id),
            )
        ):
            raise ValueError("Review context is not for the exact claimed revision")
        return reply

    def content(self, lease: WorkerLease, digest: str, *, side: str = "new") -> bytes:
        """Fetch exact leased Snapshot bytes, never an arbitrary worker URL.

        The existing /content route checks tenant, lease, allowed source side and
        membership in this Snapshot. This method rechecks digest, Content-Type,
        transfer size and lease. Redirects and foreign-origin URLs cannot appear.
        """
        self._require_lease(lease)
        if not _DIGEST.fullmatch(digest) or side not in {"old", "new"}:
            raise ValueError("Review content requires an exact digest and source side")
        if self.heartbeat(lease) != "running":
            raise ValueError("Review job was cancelled before content fetch")
        status, metadata, data = self.transport.post_binary(
            f"/v1/review-workers/{lease.job_id}/content",
            body={"envelope": lease.envelope, "digest": digest, "side": side},
            headers=self._headers(),
        )
        if status != 200:
            raise ValueError("Review revision content is unavailable for the active lease")
        if metadata.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise ValueError("Review revision content type mismatch")
        if metadata.get("content-digest") != digest:
            raise ValueError("Review revision content digest header mismatch")
        if len(data) > 16 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Review revision source content is oversized or corrupt")
        return data

    def _require_lease(self, lease: WorkerLease) -> None:
        if not isinstance(lease, WorkerLease) or min(lease.expires_at, lease.deadline_at) <= self.clock():
            raise ValueError("Review worker requires a currently valid signed lease")
        # A valid signature alone is insufficient: the *local* WorkerLease must
        # equal the exact decoded signed contract. Otherwise callers could
        # mutate an in-memory Snapshot/scope while reusing a valid envelope.
        signed = validate_lease(lease.envelope, self.binding, now=self.clock(), expected_class=lease.execution_class)
        if lease != signed:
            raise ValueError("local Review worker lease differs from signed contract")
        spec = lease.spec
        if spec.get("repo_id") != self.binding.repo_id or spec.get("org_id") != self.binding.org_id:
            raise ValueError("Review worker lease belongs to another repository")

    def fail(self, lease: WorkerLease, *, reason: str, retryable: bool = False) -> Mapping[str, Any]:
        """Release an active worker lease with a bounded typed NOT_RUN reason.

        A claim is never reported as a successful verification by this method.
        Transient retries remain bounded by the existing hosted job attempts
        and expiry, not a local second queue.
        """
        allowed = {
            "executor_unavailable",
            "execution_failed",
            "lease_cancelled",
            "source_unavailable",
            "policy_not_approved",
            "evidence_unavailable",
            "worker_transport_failure",
        }
        if reason not in allowed or type(retryable) is not bool:
            raise ValueError("Review Companion failure reason must be a known typed code")
        self._require_lease(lease)
        response = self.transport.post(
            f"/v1/review-workers/{lease.job_id}/fail",
            body={"envelope": lease.envelope, "reason": reason, "retryable": retryable},
            headers=self._headers(),
        ).require()
        if response.get("job_id") != lease.job_id or response.get("state") not in {
            "queued",
            "failed",
            "cancelled",
        }:
            raise ValueError("Review Companion failure response is not authoritative")
        return response

    def report(
        self,
        lease: WorkerLease,
        *,
        result_refs: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        summary: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Submit separately qualified bounded evidence; never invent PASS here."""
        self._require_lease(lease)
        if self.heartbeat(lease) != "running":
            raise ValueError("Review job was cancelled before reporting")
        if len(result_refs) > 128 or len(evidence_refs) > 128:
            raise ValueError("Review result contains too many artifact references")
        if any(not isinstance(ref, str) or not _REPO.fullmatch(ref) for ref in (*result_refs, *evidence_refs)):
            raise ValueError("Review result has invalid artifact references")
        if any(str(key).lower() in {"pass", "verified", "human_judgment", "lgtm"} for key in summary):
            raise ValueError("Review worker cannot assert unaudited PASS or human authority")
        return self.transport.post(
            f"/v1/review-workers/{lease.job_id}/result",
            body={
                "envelope": lease.envelope,
                "result": {
                    "result_refs": list(result_refs),
                    "evidence_refs": list(evidence_refs),
                    "summary": dict(summary),
                },
            },
            headers=self._headers(),
        ).require()
