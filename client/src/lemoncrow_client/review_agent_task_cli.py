"""Review model investigations on a paired machine: one approved job, or unattended within an admin's budget.

``lcr task run`` needs a specific Hosted job ID. ``lcr companion start`` claims queued tasks itself, only while an
organization admin keeps unattended execution on for this machine, inside the budget the server enforces. Both need a
locally selected model route/price/cost cap and explicit local permission to disclose approved source. Provider
credentials are read via the TTY, not environment/argv or Hosted, and never leave this machine except to the provider.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

from .config import load_config
from .errors import ClientError
from .review_companion_cli import _paired_binding, check_machine_keys
from .review_companion_protocol import CompanionPolicy, CompanionTask, parse_task
from .review_model_attempts import ModelAttemptAlreadySpent, ProviderAttemptLedger
from .review_model_broker import ModelBrokerRefused, OneShotReviewModelBroker, PinnedModelRoute, Provider
from .review_model_submission import submit_approved_model_investigation
from .review_worker_client import ReviewWorkerClient, WorkerLease
from .transport import HttpTransport

_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _signed_task(lease: WorkerLease) -> CompanionTask:
    """Never obtain execution scope from task prose or an unsigned HTTP body."""
    scope = lease.spec.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("trigger") != "manual"
        or scope.get("external_source_approved") is not True
    ):
        raise ValueError("signed Review job has no manual human source approval")
    if scope.get("max_model_calls") != 1:
        raise ValueError("signed Review job does not grant exactly one call")
    return parse_task(
        {
            "schema_version": 1,
            "type": "task_offer",
            "task_id": scope.get("task_id"),
            "review_id": lease.review_id,
            "repo_id": lease.spec.get("repo_id"),
            "snapshot_sha256": lease.snapshot_digest,
            "policy_sha256": lease.policy_digest,
            "task_kind": scope.get("task_kind"),
            "executor": "author_agent",
            "recipe_id": "",
            "deadline_unix_ms": int(min(lease.deadline_at, lease.expires_at) * 1000) - 1000,
            "max_runtime_ms": scope.get("max_runtime_ms"),
            "max_model_calls": 1,
        }
    )


def _route_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--route", required=True, help="Operator-attested model route ID")
    parser.add_argument("--provider", required=True, choices=("openai", "anthropic"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--generation", required=True, help="Operator route configuration generation")
    parser.add_argument("--input-price", required=True, type=int, help="USD micros per million input tokens")
    parser.add_argument("--output-price", required=True, type=int, help="USD micros per million output tokens")
    parser.add_argument("--max-cost", required=True, type=int, help="Local per-attempt USD micro cap")
    parser.add_argument(
        "--allow-external-source", action="store_true", help="Explicit local approval to send bounded revision excerpts"
    )
    parser.add_argument(
        "--allow-fix-proposals", action="store_true", help="Also allow unverified propose_fix model work"
    )


def _route(args: argparse.Namespace) -> PinnedModelRoute:
    return PinnedModelRoute(
        route_id=args.route,
        provider=cast(Provider, args.provider),
        model=args.model,
        configuration_generation=args.generation,
        input_micros_per_million=args.input_price,
        output_micros_per_million=args.output_price,
        max_cost_micros=args.max_cost,
    )


def _task_policy(binding_repo_id: str, lease: WorkerLease, *, allow_fix_proposals: bool) -> CompanionPolicy:
    return CompanionPolicy(
        repo_id=binding_repo_id,
        snapshot_sha256=lease.snapshot_digest,
        policy_sha256=lease.policy_digest,
        enabled_tasks=frozenset({"investigate", "propose_fix"} if allow_fix_proposals else {"investigate"}),
        max_runtime_ms=60_000,
        max_model_calls=1,
        allow_fix_proposals=allow_fix_proposals,
    )


_OFF = {
    "organization_off": "Unattended execution is off for this organization.",
    "device_off": "Unattended execution is off for this machine.",
    "stopped": "Unattended execution was stopped by an administrator.",
}
_CAPPED = {
    "daily_cost_cap": "Today's unattended spending limit for this machine is reached.",
    "daily_task_cap": "Today's unattended task limit for this machine is reached.",
}


def run_unattended_agent_tasks(
    arguments: list[str],
    *,
    sink: TextIO,
    read_secret: Callable[[str], str] = getpass.getpass,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """``lcr companion start``: run queued Review AI tasks without per-task approval, inside the server's budget.

    An organization admin must have turned on unattended execution for the organization and this machine, with a
    budget the server enforces before every lease. Each task still runs one signed, exact-Snapshot model call on this
    machine's own route and key, capped by the smaller of ``--max-cost`` and the signed per-task cap. Any admin can
    stop it; the next heartbeat or claim then refuses, and this command exits.
    """
    parser = argparse.ArgumentParser(
        prog="lcr companion start",
        description=(
            "Run queued Hosted Review AI tasks on this paired machine without per-task approval, "
            "within the budget an organization admin set for it."
        ),
    )
    _route_arguments(parser)
    parser.add_argument("--once", action="store_true", help="Stop when no task is queued instead of waiting")
    parser.add_argument("--poll", type=int, default=30, help="Seconds between claims while idle (5 to 300)")
    try:
        try:
            args = parser.parse_args(arguments)
        except SystemExit as exit_:
            return 0 if exit_.code in (0, None) else 2
        route = _route(args)
        if not args.allow_external_source:
            sink.write("External model source disclosure requires --allow-external-source. No task claimed.\n")
            return 2
        if type(args.poll) is not int or not 5 <= args.poll <= 300:
            sink.write("--poll must be between 5 and 300 seconds. No task claimed.\n")
            return 2
        config = load_config(product="review")
        if not config.hosted:
            sink.write("Unattended execution requires Hosted Review and a paired machine.\n")
            return 2
        binding = _paired_binding(config)
        if binding is None:
            sink.write("No paired Review machine. Run lcr connect.\n")
            return 2
        if read_secret is getpass.getpass and not sys.stdin.isatty():
            sink.write("Provider API key requires an interactive terminal. No task claimed.\n")
            return 2
        # Read once, held only in this process: never printed, persisted, or put in argv/environment.
        secret = read_secret("Provider API key (not saved): ")
        if not secret or any(ch.isspace() for ch in secret) or len(secret) < 20:
            sink.write("Provider API credential unavailable. No task claimed.\n")
            return 2
        kinds = ("investigate", "propose_fix") if args.allow_fix_proposals else ("investigate",)
        state = Path(config.review_state_dir)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        ledger = ProviderAttemptLedger(state)
    except (ClientError, ValueError, OSError) as exc:
        del exc
        sink.write("Review unattended execution refused. Check the model route, local approval and pairing.\n")
        return 2
    transport = HttpTransport(binding.origin, timeout_s=min(15.0, config.request_timeout_s))
    ran = 0
    try:
        check_machine_keys(binding, transport=transport)
        client = ReviewWorkerClient(binding, transport=transport)
        sink.write(
            f"Review Companion running unattended on {binding.worker_id} · {binding.repo_id}. "
            "Results stay unverified author-agent observations. Ctrl-C to stop.\n"
        )
        sink.flush()
        while True:
            lease, refusal = client.claim_unattended(model_route_id=route.route_id, task_kinds=kinds)
            if lease is None:
                if refusal in _OFF:
                    sink.write(_OFF[refusal] + f" Ran {ran} task(s).\n")
                    return 0 if ran else 1
                if refusal in _CAPPED:
                    sink.write(_CAPPED[refusal] + " Waiting for the next UTC day or a higher limit.\n")
                elif refusal:
                    sink.write("Review unattended claim refused. Stopping.\n")
                    return 1
                if args.once:
                    sink.write(f"No queued Review AI task to run. Ran {ran} task(s).\n")
                    return 0
                sink.flush()
                sleep(float(args.poll))
                continue
            signed = lease.model_assignment["unattended"] if lease.model_assignment else {}
            capped = dataclasses.replace(
                route, max_cost_micros=min(route.max_cost_micros, int(signed["max_task_cost_micros"]))
            )
            try:
                receipt = submit_approved_model_investigation(
                    client,
                    lease,
                    _signed_task(lease),
                    _task_policy(binding.repo_id, lease, allow_fix_proposals=args.allow_fix_proposals),
                    OneShotReviewModelBroker(capped, ledger=ledger),
                    api_key=secret,
                    explicit_external_source_approval=True,
                )
                ran += 1
                sink.write(
                    f"Review task {receipt.job_id} completed as author_agent_unverified "
                    f"(estimated ${receipt.observation.estimated_cost_micros / 1_000_000:.4f}).\n"
                )
            except (ClientError, ModelAttemptAlreadySpent, ModelBrokerRefused, ValueError) as exc:
                charged = isinstance(exc, ModelBrokerRefused) and exc.may_have_charged
                sink.write(
                    f"Review task {lease.job_id} did not complete"
                    + ("; it may have been billed and is not retried" if charged else "")
                    + ".\n"
                )
                try:
                    client.fail(lease, reason="execution_failed", retryable=False)
                except (ClientError, ValueError, OSError, TimeoutError):
                    pass  # a stopped or expired lease is already closed on the server
            sink.flush()
    except KeyboardInterrupt:
        sink.write(f"Review unattended execution stopped. Ran {ran} task(s).\n")
        return 0
    except (ClientError, OSError, TimeoutError, ValueError):
        sink.write("Review unattended execution stopped: connection, pairing or signed lease refused.\n")
        return 1
    finally:
        transport.close()


def run_manual_agent_task(
    arguments: list[str],
    *,
    sink: TextIO,
    read_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        prog="lcr task run",
        description="Run one explicitly approved Hosted Review task on this paired machine.",
    )
    parser.add_argument("job_ids", nargs="+", help="Explicit job IDs from Hosted Review (at most 20)")
    parser.add_argument(
        "--max-total-cost",
        type=int,
        default=None,
        help="Required for multiple jobs: upper bound of operator-estimated total cost in USD micros",
    )
    _route_arguments(parser)
    try:
        args = parser.parse_args(arguments)
        route = _route(args)
        if not args.allow_external_source:
            sink.write("External model source disclosure requires --allow-external-source. No job claimed.\n")
            return 2
        jobs: list[str] = args.job_ids
        if len(jobs) > 20 or len(set(jobs)) != len(jobs) or any(_JOB_ID.fullmatch(job) is None for job in jobs):
            sink.write("Specify at most 20 distinct, valid exact Review job IDs. No job claimed.\n")
            return 2
        total_cost = route.max_cost_micros if args.max_total_cost is None else args.max_total_cost
        if (
            type(total_cost) is not int
            or total_cost < len(jobs) * route.max_cost_micros
            or (len(jobs) > 1 and args.max_total_cost is None)
        ):
            sink.write(
                "Explicit --max-total-cost must cover every bounded job; no claims or external model calls were made.\n"
            )
            return 2
        config = load_config(product="review")
        if not config.hosted:
            sink.write("Model task execution requires Hosted Review and a paired machine.\n")
            return 2
        binding = _paired_binding(config)
        if binding is None:
            sink.write("No paired Review machine. Run lcr connect.\n")
            return 2
        if read_secret is getpass.getpass and not sys.stdin.isatty():
            sink.write("Provider API key requires an interactive terminal. No job claimed.\n")
            return 2
        # Read the token *before* claiming a job, so a cancelled password entry
        # does not leave a claimed lease. Never print it, persist it or pass it
        # in command arguments/environment.
        secret = read_secret("Provider API key (not saved): ")
        if not secret or any(ch.isspace() for ch in secret) or len(secret) < 20:
            sink.write("Provider API credential unavailable. No job claimed.\n")
            return 2
        transport = HttpTransport(binding.origin, timeout_s=min(15.0, config.request_timeout_s))
        try:
            check_machine_keys(binding, transport=transport)
            client = ReviewWorkerClient(binding, transport=transport)
            for job_id in jobs:
                lease = client.claim(
                    execution_class="review_intelligence",
                    model_route_id=route.route_id,
                    exact_job_id=job_id,
                )
                if lease is None:
                    sink.write("The next exact Review task is not queued for this paired machine.\n")
                    return 1
                task = _signed_task(lease)
                policy = _task_policy(binding.repo_id, lease, allow_fix_proposals=args.allow_fix_proposals)
                state = Path(config.review_state_dir)
                # Only the Review product state is allowed to hold durable spend
                # markers; an owner-unsafe existing path is refused by the ledger.
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                broker = OneShotReviewModelBroker(route, ledger=ProviderAttemptLedger(state))
                receipt = submit_approved_model_investigation(
                    client,
                    lease,
                    task,
                    policy,
                    broker,
                    api_key=secret,
                    explicit_external_source_approval=True,
                )
                sink.write(
                    f"Review task {receipt.job_id} completed as author_agent_unverified. "
                    "No checks passed or findings auto-published.\n"
                )
            return 0
        finally:
            transport.close()
    except (ClientError, ModelAttemptAlreadySpent, ModelBrokerRefused, ValueError, OSError, TimeoutError) as exc:
        if isinstance(exc, ModelBrokerRefused) and exc.may_have_charged:
            sink.write(
                "Review model request failed or was revoked after dispatch. It may have been billed; do not retry this signed attempt.\n"
            )
        else:
            sink.write(
                "Review task refused. Check signed task, local approval, model route, budget and machine pairing.\n"
            )
        return 1


__all__ = ["run_manual_agent_task", "run_unattended_agent_tasks"]
