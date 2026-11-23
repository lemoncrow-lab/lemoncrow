"""Read-only Review Companion connection diagnostics.

No daemon, listener, Runtime/MCP, checkout writes, job claims or execution.
Only a paired machine's scoped bearer is used on the fixed hosted worker
key endpoint. Connectivity is NOT evidence that execution is qualified.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from typing import Any, TextIO, cast

from .config import ClientConfig, load_config
from .errors import ClientError
from .review_device_connect import connect_main, load_pairing
from .review_worker_client import WorkerBinding
from .transport import HttpTransport


def check_machine_keys(binding: WorkerBinding, *, transport: HttpTransport) -> None:
    """A valid bearer plus the pinned signer still active at this origin.

    Do not silently adopt a new Ed25519 key from the server response. A changed
    signer is a re-pair event, never an implicit trust-on-first-use update.
    """
    response = transport.get(
        "/v1/review-workers/keys",
        headers={"Authorization": f"Bearer {binding.bearer}"},
    ).require()
    keys = response.get("keys")
    if response.get("algorithm") != "Ed25519" or not isinstance(keys, list) or len(keys) > 32:
        raise ValueError("Review worker key registry is invalid")
    found = False
    for candidate in keys:
        if not isinstance(candidate, Mapping):
            raise ValueError("Review worker registry contains a malformed signer")
        if candidate.get("key_id") != binding.trusted_key_id:
            continue
        if (
            candidate.get("algorithm") != "Ed25519"
            or candidate.get("public_key_hex") != binding.trusted_public_key_hex
            or candidate.get("status") != "active"
        ):
            raise ValueError("Pinned Review worker key is rotated or revoked; re-pair this device")
        found = True
    if not found:
        raise ValueError("Pinned Review worker signing key is not available; re-pair this device")


def presence_heartbeat(binding: WorkerBinding, *, transport: HttpTransport) -> float:
    """No command/task payload: only the server-authenticated machine identity."""
    data = transport.post(
        "/v1/review-workers/presence",
        body={},
        headers={"Authorization": f"Bearer {binding.bearer}"},
    ).require()
    seen = data.get("last_seen_at")
    if (
        data.get("worker_id") != binding.worker_id
        or not isinstance(seen, (float, int))
        or isinstance(seen, bool)
        or seen <= 0
    ):
        raise ValueError("Review presence receipt does not match the paired machine")
    return float(seen)


def pending_work_hint(
    binding: WorkerBinding, *, transport: HttpTransport, execution_class: str
) -> tuple[int, int, int]:
    """A non-authoritative cue, *never* a signed lease or execution grant."""
    if execution_class not in {"runtime_validation", "review_intelligence"}:
        raise ValueError("unsupported Review pending-work class")
    data = transport.post(
        "/v1/review-workers/pending",
        body={"execution_class": execution_class},
        headers={"Authorization": f"Bearer {binding.bearer}"},
    ).require()
    return validate_pending_hint(data, execution_class=execution_class)


def work_hint_stream(
    binding: WorkerBinding, transport: HttpTransport, *, execution_class: str
) -> Iterator[Mapping[str, Any]]:
    """The sole Review-level outbound WSS entrypoint (never a lease/command)."""
    return transport.websocket_hints(
        f"/v1/review-workers/wakeup?execution_class={execution_class}",
        bearer=binding.bearer,
        timeout_s=15,
        max_messages=10,
    )


def validate_pending_hint(data: Mapping[str, object], *, execution_class: str) -> tuple[int, int, int]:
    """All cues (WSS and HTTPS) share the same strict, non-executable schema."""
    if data.get("execution_class") != execution_class:
        raise ValueError("Review pending-work class differs from request")
    auto, manual, poll_after = data.get("automatic"), data.get("manual"), data.get("poll_after_s")
    if any(type(value) is not int for value in (auto, manual, poll_after)):
        raise ValueError("Review pending-work reply must have bounded integer counters")
    automatic, manual_count, interval = cast(int, auto), cast(int, manual), cast(int, poll_after)
    if not (0 <= automatic <= 100 and 0 <= manual_count <= 100 and 5 <= interval <= 300):
        raise ValueError("Review pending-work reply must have bounded integer counters")
    if set(data) != {"execution_class", "automatic", "manual", "poll_after_s"}:
        raise ValueError("Review pending-work hint must contain only the bounded counters")
    return automatic, manual_count, interval


def _paired_binding(config: ClientConfig) -> WorkerBinding | None:
    device = load_pairing(
        config.repo_root,
        config.review_state_dir,
        expected_origin=config.url,
    )
    if device is None:
        return None
    return WorkerBinding(
        origin=str(device["origin"]),
        org_id=str(device["org_id"]),
        repo_id=str(device["repo_id"]),
        worker_id=str(device["worker_id"]),
        bearer=str(device["machine_token"]),
        trusted_key_id=str(device["trusted_key_id"]),
        trusted_public_key_hex=str(device["trusted_public_key_hex"]),
    )


class PairedWatchPresence:
    """Optional hosted-only check-in; never authorizes capture or remote execution."""

    def __init__(self, binding: WorkerBinding, *, timeout_s: float) -> None:
        self.binding = binding
        self.last_seen = float("-inf")
        self.transport = HttpTransport(binding.origin, timeout_s=timeout_s)
        try:
            check_machine_keys(binding, transport=self.transport)
        except (ClientError, OSError, TimeoutError, ValueError):
            self.transport.close()
            raise

    def check_in(self) -> None:
        now = time.monotonic()
        if now - self.last_seen < 60:
            return
        presence_heartbeat(self.binding, transport=self.transport)
        self.last_seen = time.monotonic()

    def close(self) -> None:
        self.transport.close()


def optional_hosted_watch_presence(config: ClientConfig) -> PairedWatchPresence | None:
    """Local OSS and unpaired Hosted Review capture need no machine identity."""
    if not config.hosted:
        return None
    binding = _paired_binding(config)
    if binding is None:
        return None
    return PairedWatchPresence(binding, timeout_s=min(15.0, config.request_timeout_s))


def companion_watch_main(arguments: list[str], *, sink: TextIO) -> int:
    """One foreground Review-only watcher + authenticated machine check-ins.

    No process, shell, agent, hosted job claim or verification executor starts.
    Existing watch owns the publisher lock, Git delta cursor and Authward capture;
    the separate repository-scoped machine bearer is only for presence.
    """
    from .review_watch import watch_main

    if arguments and arguments[0] in {"help", "--help", "-h"}:
        return watch_main(arguments, sink=sink)
    try:
        config = load_config(product="review")
        if config.hosted and not config.authenticated:
            sink.write("Hosted Review requires sign-in; run lc auth login first.\n")
            return 1
        binding = _paired_binding(config)
        if binding is None:
            sink.write("No Review machine is paired. Run lcr connect.\n")
            return 1
        presence = PairedWatchPresence(binding, timeout_s=min(15.0, config.request_timeout_s))
        try:
            sink.write(
                f"Review Companion watching {binding.repo_id} on {binding.worker_id}. "
                "Incremental capture + machine check-in only; no jobs or agents.\n"
            )
            sink.flush()
            return watch_main(arguments, sink=sink, on_poll=presence.check_in, config_override=config)
        finally:
            presence.close()
    except (ClientError, OSError, TimeoutError, ValueError):
        sink.write("Review Companion connection refused. Check pairing, access and worker signing key.\n")
        return 1


def companion_main(arguments: list[str], *, sink: TextIO) -> int:
    """CLI diagnostics only; executing work needs a qualified executor."""
    if arguments in (["help"], ["--help"], ["-h"]):
        sink.write(
            "Usage: lcr pending [--watch] [--class runtime_validation|review_intelligence] | lcr device status|check|online | lcr task run JOB_ID [options]\n"
            "status: read local Review machine identity without showing secrets.\n"
            "check: authenticate to hosted Review and verify the pinned signing key.\n"
            "connect: redeem a single-use code through stdin.\n"
            "online: foreground machine presence (every 60 seconds, Ctrl-C to stop); no job execution.\n"
            "pending: read scoped counts; --watch uses outbound WSS with HTTPS fallback. No claims.\n"
            "run: execute one explicitly approved Hosted Review job with a local model route and spend cap.\n"
            "Use lcr task run --help for pricing, model and consent options.\n"
            "Use lcr watch for local or hosted capture and optional paired check-ins.\n"
            "No machine worker service is installed or started by these commands.\n"
        )
        return 0
    if arguments and arguments[0] == "run":
        from .review_agent_task_cli import run_manual_agent_task

        return run_manual_agent_task(arguments[1:], sink=sink)
    if arguments and arguments[0] == "watch":
        return companion_watch_main(arguments[1:], sink=sink)
    if arguments and arguments[0] == "pending":
        options = arguments[1:]
        if options in (["--help"], ["-h"], ["help"]):
            sink.write(
                "Usage: lcr pending [--watch] [--class runtime_validation|review_intelligence]\n"
                "Read-only pending work for a paired Hosted Review machine.\n"
                "--watch uses outbound WSS with authenticated HTTPS polling fallback.\n"
                "No job is claimed, no provider is billed, and no daemon is started.\n"
            )
            return 0
        if options in ([], ["--watch"]):
            execution_class = "runtime_validation"
            watch = bool(options)
        elif len(options) == 2 and options[0] == "--class":
            execution_class, watch = options[1], False
        elif len(options) == 3 and options[0] == "--watch" and options[1] == "--class":
            execution_class, watch = options[2], True
        elif len(options) == 3 and options[0] == "--class" and options[2] == "--watch":
            execution_class, watch = options[1], True
        else:
            sink.write("Usage: lcr pending [--watch] [--class runtime_validation|review_intelligence]\n")
            return 2
        if execution_class not in {"runtime_validation", "review_intelligence"}:
            sink.write("Review pending class must be runtime_validation or review_intelligence.\n")
            return 2
        try:
            config = load_config(product="review")
            machine = _paired_binding(config)
            if machine is None:
                sink.write("No Review machine is paired. Run lcr connect.\n")
                return 1
            transport = HttpTransport(machine.origin, timeout_s=min(15.0, config.request_timeout_s))
            try:
                check_machine_keys(machine, transport=transport)
                previous: tuple[int, int] | None = None

                def render(auto: int, manual: int) -> None:
                    nonlocal previous
                    if previous == (auto, manual):
                        return
                    sink.write(
                        f"Review {execution_class}: {auto} automatic queued, "
                        f"{manual} manually approved task(s). Hints only; no work claimed.\n"
                    )
                    sink.flush()
                    previous = (auto, manual)

                if not watch:
                    auto, manual, _interval = pending_work_hint(
                        machine,
                        transport=transport,
                        execution_class=execution_class,
                    )
                    render(auto, manual)
                else:
                    use_https_fallback = not hasattr(transport, "websocket_hints")
                    while True:
                        if not use_https_fallback:
                            try:
                                stream = transport.websocket_hints(
                                    f"/v1/review-workers/wakeup?execution_class={execution_class}",
                                    bearer=machine.bearer,
                                    timeout_s=15,
                                    max_messages=10,
                                )
                                for data in stream:
                                    auto, manual, _interval = validate_pending_hint(
                                        data,
                                        execution_class=execution_class,
                                    )
                                    render(auto, manual)
                                # Server closes each stream after bounded lifetime;
                                # reconnect against current machine authority.
                                continue
                            except (OSError, TimeoutError, ValueError, ClientError):
                                # Upgrade is optional during rolling Hosted rollout;
                                # fall back to the same authenticated HTTPS hints.
                                sink.write("Review wakeup stream unavailable; using HTTPS polling.\n")
                                sink.flush()
                                use_https_fallback = True
                        auto, manual, interval = pending_work_hint(
                            machine,
                            transport=transport,
                            execution_class=execution_class,
                        )
                        render(auto, manual)
                        time.sleep(interval)
                return 0
            finally:
                transport.close()
        except KeyboardInterrupt:
            sink.write("Review pending-work watch stopped.\n")
            return 0
        except (ClientError, OSError, TimeoutError, ValueError):
            sink.write("Review pending-work hint refused or unavailable; no work was claimed.\n")
            return 1
    if arguments == ["connect"]:
        return connect_main([], sink=sink)
    if arguments == ["status"]:
        return connect_main(["--status"], sink=sink)
    if arguments[:1] == ["start"]:
        # Lazy: the task runner imports this module's pairing helpers.
        from .review_agent_task_cli import run_unattended_agent_tasks

        return run_unattended_agent_tasks(arguments[1:], sink=sink)
    if arguments not in (["check"], ["online"], ["online", "--once"]):
        sink.write("Unknown Review Companion command. Use check, online, start, connect, status or watch.\n")
        return 2
    try:
        config = load_config(product="review")
        binding = _paired_binding(config)
        if binding is None:
            sink.write("No Review machine is paired. Run lcr connect.\n")
            return 1
        transport = HttpTransport(binding.origin, timeout_s=min(15.0, config.request_timeout_s))
        try:
            check_machine_keys(binding, transport=transport)
            if arguments[0] == "online":
                sink.write(
                    f"Review Companion foreground presence: {binding.worker_id} · "
                    f"{binding.repo_id}. No jobs will be executed. Ctrl-C to stop.\n"
                )
                sink.flush()
                while True:
                    receipt = presence_heartbeat(binding, transport=transport)
                    if arguments == ["online", "--once"]:
                        sink.write(f"Machine check-in recorded at {int(receipt)} (Unix seconds).\n")
                        break
                    time.sleep(60.0)
            else:
                sink.write(
                    f"Review Companion connection authenticated: {binding.worker_id} "
                    f"· {binding.repo_id}. Work execution is not enabled.\n"
                )
        except KeyboardInterrupt:
            sink.write("Review Companion presence stopped.\n")
        finally:
            transport.close()
        return 0
    except (ClientError, OSError, ValueError):
        # Refusals are deliberately opaque: never print bearer, raw response,
        # provider details, local source bytes or the original pairing code.
        sink.write("Review Companion connection refused. Check pairing, access and worker signing key.\n")
        return 1
