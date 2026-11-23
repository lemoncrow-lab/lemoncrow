"""Interactive side-by-side preview sessions (Phase 9R item 16, Phase 14 item 37).

A reviewer opens base and head of one exact Snapshot running side by side and
explores them. :class:`InteractivePreviewManager` owns the lifecycle:

* **admission** happens before it is called: the API admits
  ``visual.interactive_preview`` through the effective run plan as a manual
  amendment on the executor's own locus (``planned_execution``);
* **budget**: the session lifetime is capped by the remaining per-Review compute
  budget (two sides consume two compute minutes per minute) and, on the
  LemonCrow-managed executor, by the monthly managed money cap; below one
  minute the start is refused as ``Limited by budget``, and a managed session
  is stopped when the money cap is reached while it runs. Compute is recorded
  per minute while running and finally at teardown, on the planner's ledger
  (and, for managed execution, on the usage meter);
* **time limit**: every request re-checks the TTL on the injected clock and the
  sweeper tears the sandboxes down at expiry, when the Review closes, or when a
  newer Snapshot supersedes the session's Snapshot;
* **authorization**: every proxied request re-checks that the viewer still
  holds the Review (the composition decides how fresh that answer is); losing
  it ends the session;
* **comparability**: base and head are labeled comparable only under the same
  environment fingerprint;
* **recording**: what the reviewer requested on each side (redacted) plus the
  sinks' captured outbound attempts become one evidence record on the exact
  Snapshot after teardown. It is a *human observation* with no verdict: it can
  never be read as a sandbox PASS, and as execution-derived evidence only its
  title and executor class reach a model route.

Drivers (:class:`PreviewDriver`) run the sides: the author machine's
bubblewrap driver (``preview_runtime``) or a hosted worker driver. Exposures
(:class:`PreviewExposure`) give each side its own origin.

**Several serving processes.** A hosted control plane runs replicas behind one
load balancer, so the process that started a session is often not the one a
later request reaches. Everything a request needs beyond the durable session
row is in the shared :class:`~.preview_state.PreviewState`: a preview host
resolves to its session side and worker there (:class:`SharedPreviewSessions`),
tickets are taken atomically, end markers refuse a stopped session everywhere,
and exactly one process (the teardown claim) stops the sides, meters the rest
and writes the recording -- including for a session whose starting replica is
gone (orphan reaping after :data:`ORPHAN_GRACE_SECONDS`).
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import math
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from lemoncrow.pro.capabilities.review.compute_ledger import ComputeRecorder
from lemoncrow.pro.capabilities.review.preview_runtime import (
    PreparedSide,
    PreviewRecipe,
    PreviewRecipeError,
    PreviewStartFailed,
    PreviewStartSpec,
    RunningPreview,
)
from lemoncrow.pro.capabilities.review.preview_sessions import (
    DEFAULT_TTL_SECONDS,
    INTERACTIVE_PREVIEW_ENTRY,
    MAX_TTL_SECONDS,
    MIN_SESSION_SECONDS,
    SIDES,
    PreviewSession,
    PreviewSessionStore,
    PreviewSide,
    SessionState,
    comparability,
    new_session_id,
    new_token,
    token_matches,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    RedactionPolicy,
    RunArtifactStore,
    access_allows,
    protect_mapping,
    protect_text,
)
from lemoncrow.pro.environment.execution import (
    EnvironmentFingerprint,
    ExecutorClass,
    SinkCapture,
    VerificationEnvironment,
)
from lemoncrow.pro.environment.preview_proxy import (
    GateDecision,
    LoopbackPreviewServer,
    PreviewProxyCore,
    PreviewUpstreamError,
    ProxyRequest,
    ProxyResponse,
    enter_target,
    mint_ticket,
)
from lemoncrow.pro.environment.preview_state import (
    STATE_RETENTION_SECONDS,
    InMemoryPreviewState,
    PreviewState,
    side_key,
)

_LOG = logging.getLogger(__name__)

RECORDING_SCHEMA: Final[str] = "lemoncrow.interactive-preview-recording/1"
RECORDING_MEDIA_TYPE: Final[str] = "application/json"
RECORDING_TITLE: Final[str] = "Interactive preview session"
RECORDING_SOURCE_PREFIX: Final[str] = "interactive-preview:"
MAX_EVENTS: Final[int] = 2000
METER_INTERVAL_SECONDS: Final[float] = 60.0
SWEEP_INTERVAL_SECONDS: Final[float] = 5.0
#: How long after its deadline a session no process tore down is reaped by another.
ORPHAN_GRACE_SECONDS: Final[float] = 60.0
#: Most interactive previews live at once in one serving scope (a hosted repository, or this machine).
#: Each is two sandboxed app processes; the hosted worker plane runs one per worker, and this bounds the
#: author machine and the control plane's own bookkeeping. At the bound a start is refused (HTTP 429),
#: never queued.
# lc-debt: a fixed per-repository ceiling of live previews, not configurable, and no fairness across one
# organization's repositories for the shared worker pool (each repository may hold 4). Upgrade: a
# per-organization setting and an organization-wide reservation once a deployment needs either.
MAX_LIVE_PREVIEWS: Final[int] = 4
#: How long a start's slot reservation lives past its last renewal (a process that dies mid-start
#: frees its slot after this); a start in flight renews it every :data:`RESERVATION_RENEW_SECONDS`.
RESERVATION_SECONDS: Final[float] = 60.0
RESERVATION_RENEW_SECONDS: Final[float] = 15.0
_STATE_CHECK_SECONDS: Final[float] = 2.0
_STATE_CHECK_CACHE_MAX: Final[int] = 1024
LIMITED_BY_BUDGET: Final[str] = "Limited by budget"
VIEWER_UNAUTHORIZED: Final[str] = "the viewer is no longer authorized for this Review"
MANAGED_CAP_REACHED: Final[str] = f"{LIMITED_BY_BUDGET}: monthly managed spend cap reached"
MANAGED_SPEND_UNMETERED: Final[str] = (
    f"{LIMITED_BY_BUDGET}: managed spend is not metered here, so the monthly managed spend cap cannot be verified"
)


class PreviewRefused(Exception):
    """A start/enter/stop that must not proceed; ``status`` maps to HTTP."""

    def __init__(self, status: int, reason: str, *, code: str = "refused") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.code = code


class PreviewDriver(Protocol):
    """Runs both sides of a session on one executor locus."""

    run_on: str
    executor_class: ExecutorClass

    def unavailable(self) -> str: ...

    def start(self, spec: PreviewStartSpec) -> RunningPreview: ...

    def forward(
        self, running: RunningPreview, side: str, request: ProxyRequest, *, max_bytes: int, timeout: float
    ) -> ProxyResponse: ...

    def stop(self, running: RunningPreview) -> Mapping[str, Sequence[Mapping[str, Any]]]: ...


class PreviewExposure(Protocol):
    """Gives one side's proxy its own browser origin."""

    def open(self, core: PreviewProxyCore) -> str: ...

    def close(self, core: PreviewProxyCore) -> None: ...


@dataclass(frozen=True)
class ManagedSpendCap:
    """The monthly managed money cap a LemonCrow-managed preview runs under.

    ``spent_usd`` reads the current monthly managed spend (``None`` when it is
    not metered here, which refuses: the cap cannot be verified);
    ``usd_per_compute_second`` prices this session's own compute.
    """

    cap_usd: float
    spent_usd: Callable[[], float | None]
    usd_per_compute_second: float = 0.0


class LoopbackExposure:
    """Author machine: one ``127.0.0.1`` listener (origin) per side."""

    def __init__(self) -> None:
        self._servers: dict[int, LoopbackPreviewServer] = {}
        self._lock = threading.Lock()

    def open(self, core: PreviewProxyCore) -> str:
        server = LoopbackPreviewServer(core)
        with self._lock:
            self._servers[id(core)] = server
        return server.origin

    def close(self, core: PreviewProxyCore) -> None:
        with self._lock:
            server = self._servers.pop(id(core), None)
        if server is not None:
            server.close()


class PreviewHostRegistry:
    """Hosted: which side proxy answers each preview host name.

    Every side gets a fresh random label under the deployment's dedicated
    preview domain, so each side is its own origin and no preview shares an
    origin (or a host-only cookie) with the Reader or another preview. Hosts of
    sessions this process started answer from their local core; any other host
    is resolved through ``resolver`` from the shared state (another replica
    started that session).
    """

    def __init__(self, domain: str, *, scheme: str = "https") -> None:
        cleaned = domain.strip().lower().strip(".")
        if not cleaned or any(ch in cleaned for ch in "/:@*\\ ") or "." not in cleaned:
            raise ValueError("preview domain must be a bare DNS name such as previews.example.net")
        self.domain = cleaned
        self.scheme = scheme
        self._cores: dict[str, PreviewProxyCore] = {}
        self._lock = threading.Lock()
        #: Builds the proxy for a host with no local core; ``None`` serves local cores only.
        self.resolver: Callable[[str], PreviewProxyCore | None] | None = None

    @staticmethod
    def _name(host: str) -> str:
        return host.strip().lower().rsplit(":", 1)[0].rstrip(".") if host.count(":") <= 1 else ""

    def owns(self, host: str) -> bool:
        """Every name at or under the preview domain is a preview origin, never the Reader."""

        name = self._name(host)
        return name == self.domain or name.endswith("." + self.domain)

    def register(self, core: PreviewProxyCore) -> str:
        host = f"{secrets.token_hex(16)}.{self.domain}"
        with self._lock:
            self._cores[host] = core
        return f"{self.scheme}://{host}"

    def unregister(self, core: PreviewProxyCore) -> None:
        with self._lock:
            for host in [name for name, value in self._cores.items() if value is core]:
                del self._cores[host]

    def dispatch(self, host: str, request: ProxyRequest) -> ProxyResponse:
        name = self._name(host)
        core = self._cores.get(name)
        resolver = self.resolver
        if core is None and resolver is not None and name.endswith("." + self.domain):
            try:
                core = resolver(name)
            except Exception:
                _LOG.exception("interactive preview host could not be resolved")
                core = None
        if core is None:
            body = b"No such LemonCrow preview; open it from the Review.\n"
            return ProxyResponse(
                404,
                (("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")),
                body,
            )
        return core.handle(request)


class HostRoutedExposure:
    """Hosted exposure: one random host name (origin) per side under the preview domain."""

    def __init__(self, registry: PreviewHostRegistry) -> None:
        self.registry = registry

    def open(self, core: PreviewProxyCore) -> str:
        return self.registry.register(core)

    def close(self, core: PreviewProxyCore) -> None:
        self.registry.unregister(core)


def _iso(instant: float) -> str:
    return datetime.fromtimestamp(instant, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redaction_wire(policy: RedactionPolicy | None) -> dict[str, Any] | None:
    """The text-redaction part of a policy, as the shared state carries it.

    Literals travel in full (they come from the trusted base's own config):
    the replica that serves a request must redact exactly as the one that
    started the session would. Only the internal coordination store holds it,
    for the session's lifetime.
    """

    if policy is None:
        return None
    return {"patterns": list(policy.patterns), "literals": list(policy.literals)}


def _redaction_from(raw: object) -> RedactionPolicy | None:
    if not isinstance(raw, Mapping):
        return None
    return RedactionPolicy(
        patterns=tuple(str(item) for item in raw.get("patterns") or ()),
        literals=tuple(str(item) for item in raw.get("literals") or ()),
    )


def _redacted_path(path: object, policy: RedactionPolicy | None) -> str:
    # Paths and queries are reviewed-app content: redacted before they are kept.
    return protect_text(str(path or ""), policy)[0][:512]


def _wire_handles(handles: Mapping[str, Any]) -> dict[str, Any]:
    """Driver handles another process can use (plain values only; local processes never travel)."""

    return {str(key): value for key, value in handles.items() if isinstance(value, (str, int, float, bool))}


def _ended_decision(marker: tuple[str, str]) -> GateDecision:
    if marker[1] == VIEWER_UNAUTHORIZED:
        return GateDecision(False, 403, "The viewer is no longer authorized for this Review.")
    return GateDecision(False, 410, f"This preview session has ended ({marker[1] or marker[0]}).")


def review_state_of(store: Any, review_id: str, revision_id: str) -> tuple[SessionState, str] | None:
    """Why a session of *revision_id* must end now (Review closed, Snapshot superseded), else ``None``."""

    try:
        session = store.get_session(review_id)
        latest = store.latest_revision(review_id)
    except Exception:
        _LOG.warning("interactive preview could not read Review state review_id=%s", review_id)
        return None
    if session is None or str(getattr(session, "status", "open")) != "open":
        return "closed", "the Review was closed"
    if latest is None or latest.id != revision_id:
        return "superseded", "a newer revision superseded this one"
    return None


class SharedPreviewSessions:
    """Serves a session side this process does not hold, from the shared state.

    The gate is the same as the starting process's: shared end marker, TTL on
    the injected clock, Review closed/superseded, viewer still authorized --
    and whatever ends the session here marks it ended for every process.
    Forwarding reaches only the session's own driver handle (the worker), and
    events are redacted with the session's own policy before they are kept.
    """

    def __init__(
        self,
        state: PreviewState,
        *,
        drivers: Callable[[str], PreviewDriver | None],
        authorized: Callable[[Mapping[str, Any]], bool],
        review_state: Callable[[Mapping[str, Any]], tuple[SessionState, str] | None] | None = None,
        clock: Callable[[], float] = time.time,
        max_events: int = MAX_EVENTS,
    ) -> None:
        self._state = state
        self._drivers = drivers
        self._authorized = authorized
        self._review_state = review_state
        self._clock = clock
        self._max_events = max(0, int(max_events))
        self._checked: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _due(self, session_id: str, now: float) -> bool:
        with self._lock:
            last = self._checked.get(session_id, -math.inf)
            if now - last < _STATE_CHECK_SECONDS:
                return False
            self._checked[session_id] = now
            self._checked.move_to_end(session_id)
            while len(self._checked) > _STATE_CHECK_CACHE_MAX:
                self._checked.popitem(last=False)
            return True

    def gate(self, session_id: str) -> GateDecision:
        record = self._state.session(session_id)
        if record is None:
            return GateDecision(False, 410, "This preview session has ended.")
        marker = self._state.ended(session_id)
        if marker is not None:
            return _ended_decision(marker)
        now = self._clock()
        if now >= float(record.get("expires_at") or 0.0):
            return _ended_decision(
                self._state.end(session_id, "expired", "time limit reached", ttl=STATE_RETENTION_SECONDS)
            )
        if self._review_state is not None and self._due(session_id, now):
            reason = self._review_state(record)
            if reason is not None:
                return _ended_decision(self._state.end(session_id, *reason, ttl=STATE_RETENTION_SECONDS))
        if not self._authorized(record):
            return _ended_decision(
                self._state.end(session_id, "stopped", VIEWER_UNAUTHORIZED, ttl=STATE_RETENTION_SECONDS)
            )
        return GateDecision(True)

    def running(self, session_id: str, record: Mapping[str, Any] | None = None) -> RunningPreview | None:
        shared = record if record is not None else self._state.session(session_id)
        if shared is None:
            return None
        fingerprints = shared.get("fingerprints")
        handles = shared.get("handles")
        return RunningPreview(
            session_id,
            {str(side): dict(value) for side, value in (fingerprints or {}).items() if isinstance(value, Mapping)},
            handles=dict(handles) if isinstance(handles, Mapping) else {},
        )

    def forward(
        self, session_id: str, side: str, request: ProxyRequest, *, max_bytes: int, timeout: float
    ) -> ProxyResponse:
        record = self._state.session(session_id)
        driver = self._drivers(str(record.get("run_on") or "")) if record is not None else None
        running = self.running(session_id, record)
        if driver is None or running is None or not running.handles:
            raise PreviewUpstreamError("this preview session is not served from here")
        return driver.forward(running, side, request, max_bytes=max_bytes, timeout=timeout)

    def record(self, session_id: str, event: Mapping[str, Any]) -> None:
        record = self._state.session(session_id)
        if record is None:
            return
        row = dict(event)
        row["path"] = _redacted_path(row.get("path"), _redaction_from(record.get("redaction")))
        self._state.append_event(session_id, row, limit=self._max_events)

    def core(self, binding: Mapping[str, Any] | None) -> PreviewProxyCore | None:
        """The proxy for one bound preview host, or ``None`` when nothing is bound there."""

        if binding is None:
            return None
        session_id, side = str(binding.get("session_id") or ""), str(binding.get("side") or "")
        record = self._state.session(session_id) if session_id and side in SIDES else None
        if record is None:
            return None
        return PreviewProxyCore(
            session_id=session_id,
            side=side,
            gate=functools.partial(self.gate, session_id),
            forward=functools.partial(self.forward, session_id, side),
            record=functools.partial(self.record, session_id),
            frame_ancestors=tuple(str(item) for item in record.get("frame_ancestors") or ()),
            secure_cookies=record.get("secure_cookies") is True,
            clock=self._clock,
            state=self._state,
        )

    def core_for_host(self, host: str) -> PreviewProxyCore | None:
        return self.core(self._state.binding(host))


@dataclass
class _Live:
    """What only the starting process holds: the driver's handle, the cores, the exposure."""

    record: PreviewSession
    running: RunningPreview
    driver: PreviewDriver
    redaction: RedactionPolicy | None
    content_access: str
    cores: dict[str, PreviewProxyCore] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)
    checked_at: float = -math.inf
    managed_cap: ManagedSpendCap | None = None
    done: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def scope_key(scope: Mapping[str, str] | None) -> str:
    """The shared-state name of one serving scope (a hosted repository, or this machine)."""

    return json.dumps(dict(scope or {}), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PreviewSlot:
    """One held live-preview slot: the id the session will open under and the sessions it replaces."""

    session_id: str
    replaces: tuple[str, ...] = ()


def _remove_owned(prepared: Sequence[PreparedSide]) -> None:
    for item in prepared:
        if item.owned:
            shutil.rmtree(item.tree, ignore_errors=True)


class InteractivePreviewManager:
    """Lifecycle, authorization, budget and recording of interactive preview sessions."""

    def __init__(
        self,
        *,
        store: Any,
        exposure: PreviewExposure,
        compute: Callable[[str, str], ComputeRecorder],
        clock: Callable[[], float] = time.time,
        frame_ancestors: Sequence[str] = (),
        secure_cookies: bool = False,
        viewer_authorized: Callable[[str, str], bool] | None = None,
        artifacts: Callable[[], RunArtifactStore | None] | None = None,
        max_events: int = MAX_EVENTS,
        state: PreviewState | None = None,
        scope: Mapping[str, str] | None = None,
        drivers: Callable[[str], PreviewDriver | None] | None = None,
        metering: Callable[[PreviewSession, str, float, int], None] | None = None,
        max_live: int = MAX_LIVE_PREVIEWS,
    ) -> None:
        self._store = store
        self._max_live = max(1, int(max_live))
        self.sessions = PreviewSessionStore(store)
        self._exposure = exposure
        self._compute = compute
        self._clock = clock
        self._frame_ancestors = tuple(frame_ancestors)
        self._secure = secure_cookies
        self._viewer_authorized = viewer_authorized or (lambda _review_id, _viewer_id: True)
        self._artifacts = artifacts or (lambda: None)
        self._max_events = max(0, int(max_events))
        self._state: PreviewState = state if state is not None else InMemoryPreviewState(clock=clock)
        self._scope = dict(scope or {})
        self._scope_key = scope_key(self._scope)
        self._drivers = drivers or (lambda _run_on: None)
        self._metering = metering
        self.shared = SharedPreviewSessions(
            self._state,
            drivers=self._drivers,
            authorized=lambda record: self._viewer_authorized(
                str(record.get("review_id") or ""), str(record.get("viewer_id") or "")
            ),
            review_state=lambda record: review_state_of(
                self._store, str(record.get("review_id") or ""), str(record.get("revision_id") or "")
            ),
            clock=clock,
            max_events=self._max_events,
        )
        self._live: dict[str, _Live] = {}
        self._lock = threading.Lock()
        self._sweeper: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ #
    # start / enter / stop
    # ------------------------------------------------------------------ #

    def start(
        self,
        *,
        review_id: str,
        revision_id: str,
        viewer_id: str,
        driver: PreviewDriver,
        recipe: PreviewRecipe,
        prepare: Callable[[], Sequence[PreparedSide]],
        plan_digest: str,
        ttl_seconds: int | None = None,
        remaining_minutes: float | None = None,
        redaction: RedactionPolicy | None = None,
        content_access: str = "ai_denied",
        managed_cap: ManagedSpendCap | None = None,
        slot: PreviewSlot | None = None,
        preview: str = "",
    ) -> tuple[PreviewSession, str]:
        """Start both sides; returns the running record and the one-time token.

        *preview* names the environment.toml preview *recipe* comes from (recorded on the session).

        *slot* is the reservation :meth:`reserve` took before the caller persisted anything (the API
        reserves before it amends the run plan); without one, the start reserves its own. Either way a
        start that does not open its session gives the slot back.
        """

        held = slot
        try:
            if not viewer_id:
                raise PreviewRefused(
                    403, "An authenticated viewer is required to start a preview.", code="unauthorized"
                )
            if not self._viewer_authorized(review_id, viewer_id):
                raise PreviewRefused(403, "You are not authorized for this Review.", code="unauthorized")
            problem = driver.unavailable()
            if problem:
                raise PreviewRefused(409, problem, code="not_available")
            requested = max(MIN_SESSION_SECONDS, min(int(ttl_seconds or DEFAULT_TTL_SECONDS), MAX_TTL_SECONDS))
            wanted = min(requested, recipe.ttl_cap_seconds)
            cap = wanted
            if remaining_minutes is not None:
                # Other reviewers' live sessions of this Review have not metered the
                # rest of their lifetime yet: that compute is already committed.
                remaining_minutes -= self._reserved_minutes(review_id, exclude_viewer=viewer_id)
                # Both sides run for the whole session: two compute minutes per minute.
                by_budget = int(max(0.0, remaining_minutes) * 60 / len(SIDES))
                if by_budget < MIN_SESSION_SECONDS:
                    raise PreviewRefused(
                        409,
                        f"{LIMITED_BY_BUDGET}: {max(0.0, remaining_minutes):.1f} compute minutes remain in this "
                        f"Review's budget; a preview needs at least {2 * MIN_SESSION_SECONDS // 60} for base and "
                        "head.",
                        code="limited_by_budget",
                    )
                cap = min(cap, by_budget)
            if managed_cap is not None:
                cap = min(cap, self._managed_lifetime(managed_cap))
            if held is None:
                held = self.reserve(review_id=review_id, viewer_id=viewer_id)
            # One live session per viewer per Review: a new start replaces the old one
            # (wherever it was started: the worker holds one preview at a time). Only
            # now, with this start's own slot held, so a racing start cannot take the
            # slot the old session frees and leave the viewer with no preview at all.
            for live in self._live_for(review_id):
                if live.record.viewer_id == viewer_id:
                    self._teardown(live, "stopped", "replaced by a new preview session")
            for record in self._shared_records(review_id):
                if record.viewer_id == viewer_id:
                    self._teardown_shared(record, "stopped", "replaced by a new preview session")
            with self._renewing(held):
                return self._launch(
                    held,
                    review_id=review_id,
                    revision_id=revision_id,
                    viewer_id=viewer_id,
                    driver=driver,
                    recipe=recipe,
                    prepare=prepare,
                    plan_digest=plan_digest,
                    cap=cap,
                    wanted=wanted,
                    redaction=redaction,
                    content_access=content_access,
                    managed_cap=managed_cap,
                    preview=preview,
                )
        except BaseException:
            if held is not None:
                self.release(held)
            raise

    def _launch(
        self,
        slot: PreviewSlot,
        *,
        review_id: str,
        revision_id: str,
        viewer_id: str,
        driver: PreviewDriver,
        recipe: PreviewRecipe,
        prepare: Callable[[], Sequence[PreparedSide]],
        plan_digest: str,
        cap: int,
        wanted: int,
        redaction: RedactionPolicy | None,
        content_access: str,
        managed_cap: ManagedSpendCap | None,
        preview: str = "",
    ) -> tuple[PreviewSession, str]:
        now = self._clock()
        token = new_token()
        from lemoncrow.pro.capabilities.review.preview_sessions import token_digest

        session_id = slot.session_id
        record = PreviewSession(
            id=session_id,
            review_id=review_id,
            revision_id=revision_id,
            viewer_id=viewer_id,
            token_digest=token_digest(session_id, token),
            created_at=now,
            expires_at=now + cap,
            ttl_seconds=cap,
            state="starting",
            run_on=driver.run_on,
            executor_class=driver.executor_class.value,
            plan_digest=plan_digest,
            mode=recipe.mode,
            preview=preview,
            limited_by_budget=cap < wanted,
        )
        self.sessions.put(record)
        began = time.monotonic()
        recorder = self._compute(review_id, revision_id)
        prepared: tuple[PreparedSide, ...] = ()
        try:
            prepared = tuple(prepare())
            if sorted(item.side for item in prepared) != sorted(SIDES):
                raise PreviewStartFailed("both base and head must be prepared")
            running = driver.start(PreviewStartSpec(session_id, review_id, recipe, prepared, deadline=now + cap))
        except (PreviewStartFailed, PreviewRecipeError, OSError, RuntimeError, ValueError) as exc:
            _remove_owned(prepared)
            reason = protect_text(str(exc) or type(exc).__name__, redaction)[0][:500]
            self.sessions.put(record.with_state("failed", reason=reason, at=self._clock()))
            self._record_setup(recorder, record, time.monotonic() - began)
            raise PreviewRefused(409, f"The preview could not start: {reason}", code="start_failed") from exc
        self._record_setup(recorder, record, time.monotonic() - began)
        cores: dict[str, PreviewProxyCore] = {}
        opened = False
        try:
            started = self._clock()
            comparable, why = comparability(running.fingerprints)
            sides = tuple(
                PreviewSide(
                    side=side,
                    fingerprint=dict(running.fingerprints.get(side) or {}),
                    fingerprint_digest=self._fingerprint_digest(running.fingerprints.get(side)),
                )
                for side in SIDES
            )
            record = replace(
                record,
                state="running",
                # The lifetime counts from when both sides answer.
                expires_at=started + cap,
                sides=sides,
                comparable=comparable,
                comparability=why,
            )
            live = _Live(
                record=record,
                running=running,
                driver=driver,
                redaction=redaction,
                content_access=content_access,
                checked_at=started,
                managed_cap=managed_cap,
                cores=cores,
            )
            hosts: dict[str, dict[str, Any]] = {}
            for side in SIDES:
                core = PreviewProxyCore(
                    session_id=session_id,
                    side=side,
                    gate=functools.partial(self._gate, session_id),
                    forward=functools.partial(self._forward, session_id, side),
                    record=functools.partial(self._record_event, session_id),
                    frame_ancestors=self._frame_ancestors,
                    secure_cookies=self._secure,
                    clock=self._clock,
                    state=self._state,
                )
                cores[side] = core
                live.origins[side] = self._exposure.open(core)
                hosts[urlsplit(live.origins[side]).netloc.lower()] = {"side": side, "scope": dict(self._scope)}
            # The slot is still this start's (renewed while it ran); if it lapsed and the
            # scope filled meanwhile, this refuses (429) and the started sides are stopped.
            self._hold(slot)
            self._state.open(
                session_id,
                {
                    **record.to_wire(),
                    "origins": dict(live.origins),
                    "fingerprints": {side: dict(value) for side, value in running.fingerprints.items()},
                    "handles": _wire_handles(running.handles),
                    "redaction": _redaction_wire(redaction),
                    "content_access": content_access,
                    "frame_ancestors": list(self._frame_ancestors),
                    "secure_cookies": self._secure,
                },
                scope=self._scope_key,
                hosts=hosts,
                started_at=started,
                ttl=cap + STATE_RETENTION_SECONDS,
            )
            opened = True
            with self._lock:
                self._live[session_id] = live
            self.sessions.put(record)
            return record, token
        except BaseException as exc:
            # The sides run but the session never became visible (or could not be recorded):
            # stop them here, or they would run outside every count until the worker deadline.
            self._abandon(record, running, driver, cores, prepared, opened=opened, error=exc, redaction=redaction)
            raise

    def _abandon(
        self,
        record: PreviewSession,
        running: RunningPreview,
        driver: PreviewDriver,
        cores: Mapping[str, PreviewProxyCore],
        prepared: Sequence[PreparedSide],
        *,
        opened: bool,
        error: BaseException,
        redaction: RedactionPolicy | None,
    ) -> None:
        """Stop the sides of a start that failed after the driver started them."""

        with self._lock:
            self._live.pop(record.id, None)
        for core in cores.values():
            core.closed = True
            try:
                self._exposure.close(core)
            except Exception:
                _LOG.exception("interactive preview exposure close failed session=%s", record.id)
        try:
            driver.stop(running)
        except Exception:
            _LOG.exception("interactive preview teardown failed session=%s", record.id)
        _remove_owned(prepared)
        reason = protect_text(str(error) or type(error).__name__, redaction)[0][:500]
        if opened:
            try:
                self._state.end(record.id, "failed", reason, ttl=STATE_RETENTION_SECONDS)
                self._state.close(record.id)
            except Exception:
                _LOG.exception("interactive preview shared state could not be closed session=%s", record.id)
        try:
            self.sessions.put(record.with_state("failed", reason=reason, at=self._clock()))
        except Exception:
            _LOG.exception("interactive preview failure could not be recorded session=%s", record.id)

    def reserve(self, *, review_id: str, viewer_id: str) -> PreviewSlot:
        """Hold one live-preview slot of this scope for a start, or refuse (429) before anything is persisted.

        The slot is held in the shared state, so every serving process counts it: live sessions and
        starts in flight on any replica. The viewer's own running sessions of this Review count as
        released -- the start replaces them, so it never needs a second slot -- and are torn down only
        once this slot is held (by :meth:`start`).
        """

        replaces = {
            live.record.id for live in self._live_for(review_id) if live.record.viewer_id == viewer_id and not live.done
        }
        replaces.update(record.id for record in self._shared_records(review_id) if record.viewer_id == viewer_id)
        slot = PreviewSlot(new_session_id(), tuple(sorted(replaces)))
        self._hold(slot)
        return slot

    def release(self, slot: PreviewSlot) -> None:
        """Give back a slot whose start opened no session (idempotent; never closes an opened session)."""

        try:
            self._state.unreserve(self._scope_key, slot.session_id, release=slot.replaces)
        except Exception:
            # The reservation still lapses at its TTL.
            _LOG.exception("interactive preview slot could not be released session=%s", slot.session_id)

    def _hold(self, slot: PreviewSlot) -> None:
        """Take (or renew) *slot* in the shared state, or refuse: one atomic count-and-add there."""

        ended = tuple(
            session_id
            for session_id in self._state.live(self._scope_key)
            if session_id != slot.session_id and self._state.ended(session_id) is not None
        )
        occupied = self._state.reserve(
            self._scope_key,
            slot.session_id,
            limit=self._max_live,
            ttl=RESERVATION_SECONDS,
            exclude=ended,
            release=slot.replaces,
        )
        if occupied >= 0:
            raise PreviewRefused(
                429,
                f"{occupied} interactive previews are already running here and the limit is {self._max_live}. "
                "Stop one (or wait for one to expire) and start again.",
                code="capacity",
            )

    @contextlib.contextmanager
    def _renewing(self, slot: PreviewSlot) -> Iterator[None]:
        """Keep *slot* held while a start runs (prepare and driver start may outlast its TTL)."""

        done = threading.Event()

        def loop() -> None:
            while not done.wait(RESERVATION_RENEW_SECONDS):
                try:
                    self._hold(slot)
                except PreviewRefused:
                    pass  # lapsed and the scope is full: the hold before opening refuses the start
                except Exception:
                    _LOG.exception("interactive preview slot could not be renewed session=%s", slot.session_id)

        renewer = threading.Thread(target=loop, name="lc-interactive-preview-slot", daemon=True)
        renewer.start()
        try:
            yield
        finally:
            done.set()
            # Never let a renewal land after the slot is released (it would re-take it).
            renewer.join(timeout=RESERVATION_RENEW_SECONDS)

    def _managed_lifetime(self, cap: ManagedSpendCap) -> int:
        """Seconds a managed preview may run under the monthly managed money cap (refuses below one minute)."""

        spent = cap.spent_usd()
        if spent is None:
            raise PreviewRefused(409, MANAGED_SPEND_UNMETERED, code="limited_by_budget")
        left = float(cap.cap_usd) - float(spent)
        if left <= 0:
            raise PreviewRefused(409, MANAGED_CAP_REACHED, code="limited_by_budget")
        if cap.usd_per_compute_second <= 0:
            return MAX_TTL_SECONDS
        seconds = int(left / (cap.usd_per_compute_second * len(SIDES)))
        if seconds < MIN_SESSION_SECONDS:
            raise PreviewRefused(
                409,
                f"{LIMITED_BY_BUDGET}: {left:.2f} USD remain under the monthly managed spend cap; a preview "
                f"needs at least {MIN_SESSION_SECONDS} seconds on base and head.",
                code="limited_by_budget",
            )
        return seconds

    def enter(
        self,
        *,
        review_id: str,
        session_id: str,
        viewer_id: str,
        token: str,
        side: str,
        next_path: str = "/",
    ) -> str:
        """A one-time entry URL into one side for the session's own viewer."""

        live, record = self._authorized(review_id, session_id, viewer_id)
        if not token_matches(record, token):
            raise PreviewRefused(403, "The preview session token is invalid.", code="unauthorized")
        if side not in SIDES:
            raise PreviewRefused(422, "side must be old or new", code="invalid")
        decision = self._gate(session_id)
        if not decision.allowed:
            raise PreviewRefused(decision.status, decision.reason, code="ended")
        if live is not None:
            origin = live.origins[side]
        else:
            shared = self._state.session(session_id) or {}
            origin = str((shared.get("origins") or {}).get(side) or "")
            if not origin:
                raise PreviewRefused(410, "This preview session has ended.", code="ended")
        ticket = mint_ticket(self._state, side_key(session_id, side), next_path, clock=self._clock)
        return origin + enter_target(ticket)

    def stop(self, *, review_id: str, session_id: str, viewer_id: str) -> PreviewSession:
        live, record = self._authorized(review_id, session_id, viewer_id)
        if live is not None:
            return self._teardown(live, "stopped", "stopped by the reviewer")
        return self._teardown_shared(record, "stopped", "stopped by the reviewer")

    def _authorized(self, review_id: str, session_id: str, viewer_id: str) -> tuple[_Live | None, PreviewSession]:
        live = self._live.get(session_id)
        if live is not None and live.record.review_id == review_id and not live.done:
            record = live.record
        else:
            live = None
            stored = self.sessions.get(review_id, session_id)
            if stored is None:
                raise PreviewRefused(404, "No such preview session in this Review.", code="unknown")
            record = stored
        if not viewer_id or record.viewer_id != viewer_id:
            raise PreviewRefused(403, "This preview belongs to another reviewer.", code="unauthorized")
        if not self._viewer_authorized(review_id, viewer_id):
            raise PreviewRefused(403, "You are not authorized for this Review.", code="unauthorized")
        if live is None:
            marker = self._state.ended(session_id)
            if record.terminal or marker is not None or self._state.session(session_id) is None:
                reason = record.stop_reason if record.terminal else (marker[1] if marker else "no longer running")
                raise PreviewRefused(410, f"This preview session has ended ({reason or record.state}).", code="ended")
        return live, record

    # ------------------------------------------------------------------ #
    # per-request gate, forwarding and recording
    # ------------------------------------------------------------------ #

    def _gate(self, session_id: str) -> GateDecision:
        live = self._live.get(session_id)
        if live is None or live.done:
            # Started by another process (or already over): the shared gate decides.
            return self.shared.gate(session_id)
        marker = self._state.ended(session_id)
        if marker is not None:
            self._teardown(live, *marker)  # type: ignore[arg-type]
            return _ended_decision(marker)
        ended = self._check(live)
        if ended is not None:
            return GateDecision(False, 410, f"This preview session has ended ({ended}).")
        if not self._viewer_authorized(live.record.review_id, live.record.viewer_id):
            self._teardown(live, "stopped", VIEWER_UNAUTHORIZED)
            return GateDecision(False, 403, "The viewer is no longer authorized for this Review.")
        return GateDecision(True)

    def _check(self, live: _Live, *, force: bool = False) -> str | None:
        """Tear down an expired, superseded or closed session; return why it ended."""

        now = self._clock()
        if now >= live.record.expires_at:
            self._teardown(live, "expired", "time limit reached")
            return "time limit reached"
        if not force and now - live.checked_at < _STATE_CHECK_SECONDS:
            return None
        live.checked_at = now
        state = review_state_of(self._store, live.record.review_id, live.record.revision_id)
        if state is not None:
            self._teardown(live, state[0], state[1])
            return state[1]
        return None

    def _forward(
        self, session_id: str, side: str, request: ProxyRequest, *, max_bytes: int, timeout: float
    ) -> ProxyResponse:
        live = self._live.get(session_id)
        if live is None or live.done:
            raise PreviewUpstreamError("preview session has ended")
        return live.driver.forward(live.running, side, request, max_bytes=max_bytes, timeout=timeout)

    def _record_event(self, session_id: str, event: Mapping[str, Any]) -> None:
        live = self._live.get(session_id)
        if live is None:
            return
        row = dict(event)
        row["path"] = _redacted_path(row.get("path"), live.redaction)
        self._state.append_event(session_id, row, limit=self._max_events)

    # ------------------------------------------------------------------ #
    # sweeping, supersession and metering
    # ------------------------------------------------------------------ #

    def sweep(self) -> list[str]:
        """Expire/supersede/close sessions and meter running ones; returns ended ids."""

        ended: list[str] = []
        for live in list(self._live.values()):
            marker = self._state.ended(live.record.id)
            if marker is not None:
                # Ended by another process (stop, revoked viewer, deadline seen there).
                self._teardown(live, *marker)  # type: ignore[arg-type]
                ended.append(live.record.id)
                continue
            if self._check(live, force=True) is not None:
                ended.append(live.record.id)
                continue
            now = self._clock()
            mark, _total = self._state.metered(live.record.id)
            if now - mark >= METER_INTERVAL_SECONDS:
                self._meter(live.record, now)
                if live.managed_cap is not None and self._managed_cap_reached(live):
                    self._teardown(live, "stopped", MANAGED_CAP_REACHED)
                    ended.append(live.record.id)
        ended.extend(self._reap_orphans())
        return ended

    def _reap_orphans(self) -> list[str]:
        """Tear down this scope's sessions whose starting process stopped serving them."""

        reaped: list[str] = []
        for session_id in self._state.live(self._scope_key):
            if session_id in self._live:
                continue
            shared = self._state.session(session_id)
            if shared is None:
                continue
            deadline = float(shared.get("expires_at") or 0.0)
            now = self._clock()
            if now < deadline + ORPHAN_GRACE_SECONDS:
                continue
            record = self.sessions.get(str(shared.get("review_id") or ""), session_id)
            if record is None or record.terminal:
                self._state.close(session_id)
                continue
            marker = self._state.ended(session_id) or ("expired", "time limit reached")
            self._teardown_shared(record, marker[0], marker[1], until=min(now, deadline))  # type: ignore[arg-type]
            reaped.append(session_id)
        return reaped

    def _managed_cap_reached(self, live: _Live) -> bool:
        cap = live.managed_cap
        if cap is None:
            return False
        try:
            spent = cap.spent_usd()
        except Exception:
            _LOG.exception("managed spend could not be read session=%s", live.record.id)
            spent = None
        if spent is None:
            return True  # the cap can no longer be verified: stop rather than overspend
        _mark, seconds = self._state.metered(live.record.id)
        return float(spent) + seconds * cap.usd_per_compute_second >= cap.cap_usd

    def supersede(self, review_id: str, current_revision_id: str) -> list[str]:
        out: list[str] = []
        for live in self._live_for(review_id):
            if live.record.revision_id != current_revision_id:
                self._teardown(live, "superseded", "a newer revision superseded this one")
                out.append(live.record.id)
        return out

    def close_review(self, review_id: str, reason: str = "the Review was closed") -> list[str]:
        out: list[str] = []
        for live in self._live_for(review_id):
            self._teardown(live, "closed", reason)
            out.append(live.record.id)
        return out

    def ensure_sweeper(self, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
        """Run :meth:`sweep` periodically so sandboxes stop even with no traffic."""

        with self._lock:
            if self._sweeper is not None and self._sweeper.is_alive():
                return

            def loop() -> None:
                while not self._stopping.wait(interval):
                    try:
                        self.sweep()
                    except Exception:
                        _LOG.exception("interactive preview sweep failed")

            self._stopping.clear()
            self._sweeper = threading.Thread(target=loop, name="lc-interactive-preview-sweeper", daemon=True)
            self._sweeper.start()

    def shutdown(self) -> None:
        self._stopping.set()
        for live in list(self._live.values()):
            self._teardown(live, "closed", "the Review server stopped")

    def _live_for(self, review_id: str) -> list[_Live]:
        return [live for live in list(self._live.values()) if live.record.review_id == review_id]

    def _shared_records(self, review_id: str) -> list[PreviewSession]:
        """Running sessions of *review_id* another process started (from the durable rows)."""

        out: list[PreviewSession] = []
        for session_id in self._state.live(self._scope_key):
            if session_id in self._live:
                continue
            shared = self._state.session(session_id)
            if shared is None or shared.get("review_id") != review_id:
                continue
            record = self.sessions.get(review_id, session_id)
            if record is not None and not record.terminal:
                out.append(record)
        return out

    def _reserved_minutes(self, review_id: str, *, exclude_viewer: str) -> float:
        """Unmetered remaining lifetime of other live sessions of *review_id*, in compute minutes."""

        now = self._clock()
        reserved = 0.0
        for session_id in self._state.live(self._scope_key):
            shared = self._state.session(session_id)
            if (
                shared is None
                or shared.get("review_id") != review_id
                or shared.get("viewer_id") == exclude_viewer
                or self._state.ended(session_id) is not None
            ):
                continue
            mark, _total = self._state.metered(session_id)
            reserved += max(0.0, float(shared.get("expires_at") or 0.0) - max(now, mark)) * len(SIDES) / 60.0
        return reserved

    def _meter(self, record: PreviewSession, until: float) -> None:
        running_sides = sum(1 for row in record.sides if row.status == "running")
        seconds, tick = self._state.meter(record.id, until, sides=running_sides)
        if seconds <= 0:
            return
        try:
            self._compute(record.review_id, record.revision_id).record_compute(
                record.entry_id, record.run_on, "run", seconds
            )
        except Exception:
            _LOG.exception("interactive preview compute could not be recorded session=%s", record.id)
        self._meter_managed(record, "run", seconds, tick)

    def _meter_managed(self, record: PreviewSession, kind: str, seconds: float, tick: int) -> None:
        if self._metering is None or seconds <= 0:
            return
        try:
            self._metering(record, kind, seconds, tick)
        except Exception:
            _LOG.exception("interactive preview usage could not be metered session=%s", record.id)

    def _record_setup(self, recorder: ComputeRecorder, record: PreviewSession, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            recorder.record_compute(INTERACTIVE_PREVIEW_ENTRY, record.run_on, "setup", seconds)
        except Exception:
            _LOG.exception("interactive preview setup compute could not be recorded")
        self._meter_managed(record, "setup", seconds, 0)

    @staticmethod
    def _fingerprint_digest(raw: Mapping[str, Any] | None) -> str:
        if not raw:
            return ""
        try:
            return EnvironmentFingerprint.from_wire(raw).digest
        except (TypeError, ValueError):
            return ""

    # ------------------------------------------------------------------ #
    # teardown and recording evidence
    # ------------------------------------------------------------------ #

    def _teardown(self, live: _Live, state: SessionState, reason: str) -> PreviewSession:
        with live.lock:
            if live.done:
                return live.record
            live.done = True
        with self._lock:
            self._live.pop(live.record.id, None)
        for core in live.cores.values():
            core.closed = True
            try:
                self._exposure.close(core)
            except Exception:
                _LOG.exception("interactive preview exposure close failed session=%s", live.record.id)
        winner = self._state.end(live.record.id, state, reason, ttl=STATE_RETENTION_SECONDS)
        if not self._state.claim(live.record.id, ttl=STATE_RETENTION_SECONDS):
            # Another process is stopping the sides and writing the recording.
            live.record = replace(live.record, state=winner[0], stop_reason=winner[1])  # type: ignore[arg-type]
            return live.record
        rows: Mapping[str, Sequence[Mapping[str, Any]]] = {}
        try:
            rows = live.driver.stop(live.running)
        except Exception:
            _LOG.exception("interactive preview teardown failed session=%s", live.record.id)
        live.record = self._finalize(
            live.record, winner, rows, live.redaction, live.content_access, until=self._clock()
        )
        return live.record

    def _teardown_shared(
        self, record: PreviewSession, state: SessionState, reason: str, *, until: float | None = None
    ) -> PreviewSession:
        """Stop a session another process started (its driver handle comes from the shared state)."""

        shared = self._state.session(record.id)
        winner = self._state.end(record.id, state, reason, ttl=STATE_RETENTION_SECONDS)
        if shared is None or not self._state.claim(record.id, ttl=STATE_RETENTION_SECONDS):
            return replace(record, state=winner[0], stop_reason=winner[1])  # type: ignore[arg-type]
        rows: Mapping[str, Sequence[Mapping[str, Any]]] = {}
        driver = self._drivers(record.run_on)
        running = self.shared.running(record.id, shared)
        if driver is not None and running is not None and running.handles:
            try:
                rows = driver.stop(running)
            except Exception:
                _LOG.exception("interactive preview teardown failed session=%s", record.id)
        return self._finalize(
            record,
            winner,
            rows,
            _redaction_from(shared.get("redaction")),
            str(shared.get("content_access") or "ai_denied"),
            until=self._clock() if until is None else until,
        )

    def _finalize(
        self,
        record: PreviewSession,
        ending: tuple[str, str],
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        redaction: RedactionPolicy | None,
        content_access: str,
        *,
        until: float,
    ) -> PreviewSession:
        """Meter the rest, write the recording and the final record, release the shared state."""

        self._meter(record, until)
        captures = self._captures(record, rows, redaction, content_access)
        events, dropped = self._state.events(record.id)
        _mark, metered = self._state.metered(record.id)
        now = self._clock()
        final = replace(
            record,
            state=ending[0],  # type: ignore[arg-type]
            stop_reason=ending[1],
            stopped_at=now,
            sides=tuple(replace(row, status="stopped") for row in record.sides),
            compute_seconds=metered,
            events=len(events) + dropped,
        )
        try:
            evidence_id = self._write_recording(final, events, dropped, captures, redaction, content_access)
        except Exception:
            _LOG.exception("interactive preview recording could not be stored session=%s", record.id)
            evidence_id = ""
        final = replace(final, recording_evidence_id=evidence_id)
        try:
            self.sessions.put(final)
        except Exception:
            _LOG.exception("interactive preview record could not be stored session=%s", record.id)
        try:
            self._state.close(record.id)
        except Exception:
            _LOG.exception("interactive preview shared state could not be released session=%s", record.id)
        return final

    def _captures(
        self,
        record: PreviewSession,
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        redaction: RedactionPolicy | None,
        content_access: str,
    ) -> dict[str, tuple[SinkCapture, ...]]:
        from lemoncrow.pro.capabilities.review.hermetic import captures_from_rows

        out: dict[str, tuple[SinkCapture, ...]] = {}
        artifacts = self._artifacts()
        for side in SIDES:
            side_rows = list(rows.get(side) or ())
            if not side_rows:
                out[side] = ()
                continue
            captures, _refs = captures_from_rows(
                side_rows,
                review_id=record.review_id,
                policy=redaction,
                content_access=content_access,  # type: ignore[arg-type]
                artifacts=artifacts,
            )
            out[side] = captures
        return out

    def _write_recording(
        self,
        record: PreviewSession,
        events: Sequence[Mapping[str, Any]],
        dropped: int,
        captures: Mapping[str, Sequence[SinkCapture]],
        redaction: RedactionPolicy | None,
        content_access: str,
    ) -> str:
        writer = getattr(self._store, "write_evidence_artifact", None)
        adder = getattr(self._store, "add_evidence", None)
        if not callable(writer) or not callable(adder):
            return ""
        from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence
        from lemoncrow.pro.capabilities.review.store import new_evidence_id

        executor = ExecutorClass(record.executor_class)
        verification = VerificationEnvironment(executor_class=executor, image_digest=self._image(record))
        per_side = {side: sum(1 for row in events if row.get("side") == side) for side in SIDES}
        blocked = sum(1 for row in events if row.get("kind") == "blocked_redirect")
        captured = sum(len(rows) for rows in captures.values())
        document = protect_mapping(
            {
                "schema": RECORDING_SCHEMA,
                "session_id": record.id,
                "review_id": record.review_id,
                "revision_id": record.revision_id,
                "viewer_id": record.viewer_id,
                "observation": "human_exploration",
                "verdict": None,
                "note": "A reviewer's exploration of base and head; a human observation, never a sandbox result.",
                "started_at": _iso(record.created_at),
                "ended_at": _iso(record.stopped_at or self._clock()),
                "end_state": record.state,
                "end_reason": record.stop_reason,
                "ttl_seconds": record.ttl_seconds,
                "limited_by_budget": record.limited_by_budget,
                "run_on": record.run_on,
                "executor_class": record.executor_class,
                "mode": record.mode,
                "comparable": record.comparable,
                "comparability": record.comparability,
                "fingerprints": {row.side: dict(row.fingerprint) for row in record.sides},
                "compute_seconds": round(record.compute_seconds, 3),
                "content_access": content_access,
                "verification": verification.to_wire(),
                "events": [dict(row) for row in events],
                "events_dropped": dropped,
                "sink_captures": {side: [row.to_wire() for row in rows] for side, rows in captures.items()},
            },
            redaction,
        )
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        evidence_id = new_evidence_id()
        rel, digest, size = writer(record.review_id, record.revision_id, evidence_id, payload)
        detail = (
            f"Reviewer exploration (human observation, not a sandbox result): {per_side['old']} requests on base, "
            f"{per_side['new']} on head; {captured} outbound attempt{'s' if captured != 1 else ''} captured, "
            f"none delivered; {blocked} off-preview redirect{'s' if blocked != 1 else ''} refused; "
            f"{'comparable' if record.comparable else 'not comparable'} environments; ended: {record.stop_reason}."
        )
        adder(
            ReviewEvidence(
                id=evidence_id,
                review_id=record.review_id,
                revision_id=record.revision_id,
                kind="document",
                title=RECORDING_TITLE,
                artifact_path=rel,
                content_hash=digest,
                mime_type=RECORDING_MEDIA_TYPE,
                bytes=size,
                source="human",
                source_ref=f"{RECORDING_SOURCE_PREFIX}{record.id}",
                verification_status="",
                detail=detail,
                attestation_class="human_observation",
                stable_key="",
                attested_by=record.viewer_id,
                observed_at=_iso(record.stopped_at or self._clock()),
                executor_class=record.executor_class,
                execution_verification=json.dumps(verification.to_wire(), sort_keys=True, separators=(",", ":")),
            )
        )
        return evidence_id

    @staticmethod
    def _image(record: PreviewSession) -> str:
        for row in record.sides:
            image = str(row.fingerprint.get("image_digest") or "")
            if image:
                return image
        return ""

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def describe(self, review_id: str, viewer_id: str) -> list[dict[str, Any]]:
        """This viewer's sessions of the Review, newest first, with live origins."""

        for candidate in self._live_for(review_id):
            self._check(candidate)
        now = self._clock()
        rows: list[dict[str, Any]] = []
        for record in reversed(self.sessions.for_review(review_id)):
            if record.viewer_id != viewer_id:
                continue
            live = self._live.get(record.id)
            wire = (live.record if live is not None else record).to_wire()
            if live is not None:
                wire["origins"] = dict(live.origins)
                wire["remaining_seconds"] = max(0, int(live.record.expires_at - now))
            elif not record.terminal:
                shared = self._state.session(record.id)
                marker = self._state.ended(record.id)
                if shared is not None and marker is None and now < record.expires_at:
                    # Running, started by another process.
                    wire["origins"] = dict(shared.get("origins") or {})
                    wire["remaining_seconds"] = max(0, int(record.expires_at - now))
                else:
                    # Not served anywhere any more; the final record follows its teardown.
                    ending = marker or (
                        ("expired", "time limit reached")
                        if now >= record.expires_at
                        else ("failed", "the preview is no longer running")
                    )
                    wire["state"], wire["stop_reason"] = ending
            rows.append(wire)
        return rows

    def live_session(self, session_id: str) -> PreviewSession | None:
        live = self._live.get(session_id)
        return live.record if live is not None else None


def recording_for_model(document: Mapping[str, Any], *, route_trust: str) -> dict[str, Any]:
    """The only projection of a preview recording a model route may receive.

    Structural facts (counts, executor, comparability) always; visited paths
    and captured payloads only when the recording's content-access mode allows
    *route_trust*, otherwise an explicit ``withheld`` marker.
    """

    access = str(document.get("content_access") or "ai_denied")
    allowed = access_allows(access, route_trust)
    withheld = f"withheld: AI content access {access} for {route_trust} route"
    raw_events = document.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    raw_captures = document.get("sink_captures")
    captures: Mapping[str, Any] = raw_captures if isinstance(raw_captures, Mapping) else {}
    return {
        "observation": "human_exploration",
        "verdict": None,
        "executor_class": str(document.get("executor_class") or ""),
        "comparable": document.get("comparable") is True,
        "requests": len(events),
        "events": events if allowed else withheld,
        "sink_captures": (
            {
                str(side): [
                    {
                        "kind": row.get("kind"),
                        "destination": row.get("destination") if allowed else withheld,
                        "payload": row.get("payload_preview") if allowed else withheld,
                        "delivered": False,
                    }
                    for row in rows
                    if isinstance(row, Mapping)
                ]
                for side, rows in captures.items()
                if isinstance(rows, list)
            }
        ),
    }


__all__ = [
    "LIMITED_BY_BUDGET",
    "MANAGED_CAP_REACHED",
    "MANAGED_SPEND_UNMETERED",
    "ORPHAN_GRACE_SECONDS",
    "RECORDING_SCHEMA",
    "RECORDING_SOURCE_PREFIX",
    "RECORDING_TITLE",
    "VIEWER_UNAUTHORIZED",
    "HostRoutedExposure",
    "InteractivePreviewManager",
    "LoopbackExposure",
    "ManagedSpendCap",
    "PreviewDriver",
    "PreviewExposure",
    "PreviewHostRegistry",
    "PreviewRefused",
    "PreviewSlot",
    "SharedPreviewSessions",
    "recording_for_model",
    "review_state_of",
    "scope_key",
]
