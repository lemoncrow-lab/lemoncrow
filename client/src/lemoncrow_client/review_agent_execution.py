"""Explicitly approved, signed Review author-agent subprocess gate.

This does not enable unattended agents, model credentials, or background work.
The live provider model path remains disabled until metering + scoped outbound
provider transport are qualified. A simulated/offline subprocess result is only
an unverified local observation and is NEVER posted as a Review PASS/finding.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .review_agent_adapters import (
    AgentInvocationRefused,
    AgentProvider,
    LocalAgentInvocation,
    build_agent_invocation,
)
from .review_agent_sandbox import AgentSandbox, AgentSandboxLimits, AgentSandboxOutcome
from .review_agent_snapshot import AgentSnapshotLimits, materialize_leased_agent_snapshot
from .review_companion_protocol import CompanionPolicy, CompanionTask
from .review_worker_client import ReviewWorkerClient, WorkerLease


def execute_approved_offline_agent(
    client: ReviewWorkerClient,
    lease: WorkerLease,
    task: CompanionTask,
    policy: CompanionPolicy,
    *,
    provider: AgentProvider,
    executable: Path,
    repository: Path,
    snapshot: Path,
    locally_enabled: frozenset[str],
    explicit_operator_approval: bool,
    limits: AgentSandboxLimits | None = None,
    sandbox: AgentSandbox | None = None,
    stopped: Callable[[], bool] = lambda: False,
    monotonic: Callable[[], float] = time.monotonic,
) -> AgentSandboxOutcome:
    """Run offline-only, after a local opt-in and current Ed25519 signed grant.

    `snapshot` must be a separately materialized, content-verified leased tree:
    this API refuses the mutable checkout, but source download/manifest
    verification belongs to the existing hosted Review Snapshot transport.
    It never publishes a result: provider usage and cost are NOT qualified.
    """
    invocation = _admit_offline_agent(
        client,
        lease,
        task,
        policy,
        provider=provider,
        executable=executable,
        repository=repository,
        locally_enabled=locally_enabled,
        explicit_operator_approval=explicit_operator_approval,
    )
    if stopped() or client.heartbeat(lease) != "running":
        raise AgentInvocationRefused("signed agent lease was cancelled before execution")
    sandbox = sandbox or AgentSandbox()
    qualified, reason = sandbox.qualify()
    if not qualified:
        raise AgentInvocationRefused(f"qualified read-only agent sandbox unavailable: {reason}")
    # A bounded heartbeat runs in the controller (never inside the sandbox).
    # A revoked, superseded, expired or disconnected lease must kill the agent.
    next_heartbeat = monotonic() + 1.0

    def should_cancel() -> bool:
        nonlocal next_heartbeat
        if stopped() or min(lease.expires_at, lease.deadline_at) <= client.clock():
            return True
        now = monotonic()
        if now < next_heartbeat:
            return False
        next_heartbeat = now + 1.0
        try:
            return client.heartbeat(lease) != "running"
        except Exception:
            return True

    return sandbox.run(
        invocation,
        snapshot=snapshot,
        limits=limits or AgentSandboxLimits(),
        should_cancel=should_cancel,
        cancellation_poll_s=0.2,
    )


def _admit_offline_agent(
    client: ReviewWorkerClient,
    lease: WorkerLease,
    task: CompanionTask,
    policy: CompanionPolicy,
    *,
    provider: AgentProvider,
    executable: Path,
    repository: Path,
    locally_enabled: frozenset[str],
    explicit_operator_approval: bool,
) -> LocalAgentInvocation:
    """Keep all operator and signed task gates *before* any source fetch."""
    if not explicit_operator_approval:
        raise AgentInvocationRefused("explicit per-run local operator approval required")
    client._require_lease(lease)  # Recheck pinned Ed25519 and exact *signed* fields.
    scope = lease.spec.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("max_model_calls") != 1
        or type(scope.get("max_runtime_ms")) is not int
        or scope["max_runtime_ms"] < task.max_runtime_ms
    ):
        raise AgentInvocationRefused("signed agent grant lacks bounded task model/runtime budget")
    now_ms = int(client.clock() * 1000)
    return build_agent_invocation(
        provider,
        executable=executable,
        repo=repository,
        task=task,
        policy=policy,
        lease=lease,
        now_unix_ms=now_ms,
        locally_enabled=locally_enabled,
    )


def execute_approved_leased_agent(
    client: ReviewWorkerClient,
    lease: WorkerLease,
    task: CompanionTask,
    policy: CompanionPolicy,
    *,
    provider: AgentProvider,
    executable: Path,
    repository: Path,
    locally_enabled: frozenset[str],
    explicit_operator_approval: bool,
    limits: AgentSandboxLimits | None = None,
    source_limits: AgentSnapshotLimits | None = None,
    sandbox: AgentSandbox | None = None,
    stopped: Callable[[], bool] = lambda: False,
    monotonic: Callable[[], float] = time.monotonic,
) -> AgentSandboxOutcome:
    """Full P3m path: exact leased HTTPS source -> qualified offline sandbox.

    Never accepts user-provided source directories. Partial, stale, truncated,
    secret-like or corrupt source trees refuse execution. The existing offline
    sandbox stays the final process boundary; a provider exit is not a Review
    finding, PASS, attestation, model usage record or approval.
    """
    _admit_offline_agent(
        client,
        lease,
        task,
        policy,
        provider=provider,
        executable=executable,
        repository=repository,
        locally_enabled=locally_enabled,
        explicit_operator_approval=explicit_operator_approval,
    )
    if stopped() or client.heartbeat(lease) != "running":
        raise AgentInvocationRefused("agent task cancelled before revision fetch")
    with materialize_leased_agent_snapshot(client, lease, limits=source_limits) as (snapshot, _manifest):
        return execute_approved_offline_agent(
            client,
            lease,
            task,
            policy,
            provider=provider,
            executable=executable,
            repository=repository,
            snapshot=snapshot,
            locally_enabled=locally_enabled,
            explicit_operator_approval=explicit_operator_approval,
            limits=limits,
            sandbox=sandbox,
            stopped=stopped,
            monotonic=monotonic,
        )


__all__ = ["execute_approved_leased_agent", "execute_approved_offline_agent"]
