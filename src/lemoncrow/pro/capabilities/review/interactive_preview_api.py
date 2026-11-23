"""HTTP routes for interactive side-by-side previews (Phase 9R item 16).

Registered by ``register_review_api`` through one call, so the hotspot only
hands its existing gates (session scope, mutability, expected revision, run
plan admission, budget usage, trusted-base reads, hermetic context) to this
module. Routes (all under the Review API prefix, authenticated by the router):

``GET  /reviews/{id}/interactive-preview[?preview=<name>]``      offer + this viewer's sessions
``POST /reviews/{id}/interactive-preview/sessions``              admit through the run plan, start
``POST /reviews/{id}/interactive-preview/sessions/{sid}/enter``  one-time entry URL into one side
``POST /reviews/{id}/interactive-preview/sessions/{sid}/stop``   stop now (tears down, records)

The session token travels only in the start response body and the
``X-LemonCrow-Preview-Token`` request header, never in a URL, so request
logs never carry it. The entry URL carries a single-use ticket that expires
in a minute.

Every ``[previews.<name>]`` the trusted base's ``environment.toml`` declares can run: the
offer lists them (``previews``, in declaration order) and ``preview`` names the one it
describes; a start body's ``preview`` picks one. Without a name, the first declared is the
default. A viewer runs one preview session at a time, so starting another replaces it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import posixpath
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.compute_ledger import ComputeRecorder
from lemoncrow.pro.capabilities.review.interactive_preview import (
    LIMITED_BY_BUDGET,
    MAX_LIVE_PREVIEWS,
    InteractivePreviewManager,
    LoopbackExposure,
    ManagedSpendCap,
    PreviewDriver,
    PreviewExposure,
    PreviewRefused,
)
from lemoncrow.pro.capabilities.review.preview_runtime import (
    PreparedSide,
    PreviewRecipe,
    PreviewRecipeError,
    PreviewStartFailed,
    SandboxedPreviewDriver,
    environment_preview_recipes,
    load_preview_recipe,
    lockfile_text_digest,
    static_recipe,
)
from lemoncrow.pro.capabilities.review.preview_sessions import (
    DEFAULT_TTL_SECONDS,
    INTERACTIVE_PREVIEW_ENTRY,
    MAX_TTL_SECONDS,
    MIN_SESSION_SECONDS,
    SIDES,
    PreviewSession,
)
from lemoncrow.pro.environment.preview_state import InMemoryPreviewState, PreviewState

TOKEN_HEADER: Final[str] = "X-LemonCrow-Preview-Token"
NOT_DETECTED: Final[str] = "No previewable app was detected in the trusted base."
#: Where the offer is surfaced; there is no top-level control (Phase 14 UX rule).
OPENS_FROM: Final[tuple[str, ...]] = ("behavior_changes", "checks_rail")
_START_KEYS: Final[frozenset[str]] = frozenset({"expected_revision_id", "ttl_seconds", "preview"})
_ENTER_KEYS: Final[frozenset[str]] = frozenset({"side", "path"})
_STOP_KEYS: Final[frozenset[str]] = frozenset({"expected_revision_id"})
_MAX_BODY: Final[int] = 4096
_LOCKFILES: Final[tuple[str, ...]] = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "requirements.txt",
    "uv.lock",
    "poetry.lock",
)
_STATIC_FRAMEWORKS: Final[tuple[str, ...]] = ("next", "astro", "vite")


#: Every local Review app's previews, so the live-preview cap counts this machine's.
_LOCAL_PREVIEW_STATE: Final[InMemoryPreviewState] = InMemoryPreviewState()


def _short_runtime_root() -> Path:
    """A short private directory for side sockets (Unix socket paths are ~108 bytes)."""

    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    parent = Path(base) if base and Path(base).is_dir() else Path(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix="lc-ips-", dir=parent))


@dataclass
class InteractivePreviewBackend:
    """Where previews run and how the browser reaches them; owned by the composition.

    ``drivers`` maps the run plan executor (``local``/``worker``/``managed``) to
    the driver that runs sides there, ``None`` when nothing is wired. ``unavailable``
    is why this server cannot expose previews at all (``""`` when it can).
    ``static_builds`` allows the inferred static Next.js/Astro/Vite build (it
    builds on this machine, so only the author-machine composition sets it).
    """

    drivers: Callable[[str], PreviewDriver | None]
    exposure: PreviewExposure
    tree_root: Path
    frame_ancestors: tuple[str, ...] = ()
    secure_cookies: bool = False
    viewer_authorized: Callable[[str, str], bool] | None = None
    static_builds: bool = False
    unavailable: str = ""
    clock: Callable[[], float] = time.time
    remote_provisioning: bool = False
    """The driver provisions dependencies on its own plane (hosted worker)."""
    closers: list[Callable[[], None]] = field(default_factory=list)
    state: PreviewState | None = None
    """Live session state every serving process shares (``None``: this process only)."""
    scope: Mapping[str, str] = field(default_factory=dict)
    """Where these sessions belong in the shared state (a hosted repository)."""
    metering: Callable[[PreviewSession, str, float, int], None] | None = None
    """Usage metering of ``(record, "setup"|"run", compute seconds, tick)`` (managed execution)."""
    usd_per_compute_second: Callable[[], float | None] | None = None
    """What one compute second costs on the managed executor (prices the managed money cap)."""
    max_live_previews: int = MAX_LIVE_PREVIEWS
    """Most previews live at once in this scope; a start past it is refused (429)."""

    @classmethod
    def local(cls, store_root: Path, *, port: int) -> InteractivePreviewBackend:
        """The author machine: bubblewrap sides, one loopback origin per side."""

        holder: dict[str, SandboxedPreviewDriver] = {}

        def driver(run_on: str) -> PreviewDriver | None:
            if run_on != "local":
                return None
            if "driver" not in holder:
                holder["driver"] = SandboxedPreviewDriver(_short_runtime_root())
            return holder["driver"]

        def close() -> None:
            created = holder.get("driver")
            if created is not None:
                shutil.rmtree(created.work_root, ignore_errors=True)

        backend = cls(
            drivers=driver,
            exposure=LoopbackExposure(),
            tree_root=store_root / "review" / "interactive-previews",
            frame_ancestors=(f"http://127.0.0.1:{port}", f"http://localhost:{port}"),
            static_builds=True,
            # The local server builds one Review app per repository and Review; the cap is per machine.
            state=_LOCAL_PREVIEW_STATE,
        )
        backend.closers.append(close)
        return backend


@dataclass(frozen=True)
class InteractivePreviewDeps:
    """The Review API gates and seams a preview reuses; nothing here is new authority."""

    store: Any
    backend: InteractivePreviewBackend
    compute: Callable[[str, str], ComputeRecorder]
    session: Callable[[str], Any]
    mutable_session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    require_expected_revision: Callable[[Mapping[str, Any], Any, str], None]
    viewer_id: Callable[[Any], str]
    detected: Callable[[Any], Any]
    """Trusted-base detection (``RunPlanService.detected``)."""
    plan: Callable[[Any, Any], Any]
    """The effective run plan the Review displays (``RunPlanService.peek``)."""
    planned_request: Callable[..., Mapping[str, Any]]
    """``_planned_request(session, revision, pick, run_on=, action=)``: amend + admit, 409 otherwise."""
    preview_pick: Callable[[Any, Any, Any], Mapping[str, Any]]
    """Unpersisted amendment preview (``RunPlanService.preview``) for one pick."""
    usage: Callable[[Any, Any], Any]
    """Budget usage of the one visible budget (``RunPlanService.usage``)."""
    review_config: Callable[[Any], str]
    """Trusted-base ``.lemoncrow/review.yaml`` text (never head bytes)."""
    read_side: Callable[[Any, str, str], str | None]
    """One file of one revision side: ``(revision, path, side)``."""
    hermetic: Callable[[Any, Any, str], Any]
    """Trusted hermetic context for one side (``_hermetic_execution``)."""
    materialize: Callable[[Any, str, Path], None]
    """Materialize one exact revision side into a fresh directory."""
    build_static: Callable[[Any, str, str, str], Path] | None = None
    """``(revision, side, framework, root)`` -> sandboxed static build output."""
    artifacts: Callable[[], Any] | None = None
    environment_config: Callable[[Any], str | None] = lambda _revision: None
    """Trusted-base ``.lemoncrow/environment.toml`` text (``None``: absent or unknown)."""


@dataclass(frozen=True)
class _Offer:
    state: str
    reason: str
    entry: Mapping[str, Any] | None = None
    recipe: PreviewRecipe | None = None
    driver: PreviewDriver | None = None
    remaining_minutes: float | None = None
    admission: Mapping[str, Any] | None = None
    previews: tuple[str, ...] = ()
    """The trusted base's environment.toml previews, in declaration order (the first is the default)."""
    preview: str = ""
    """The preview this offer describes (``""``: review.yaml or a static build)."""

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "previews": list(self.previews),
            "preview": self.preview or None,
            "entry": dict(self.entry) if self.entry is not None else None,
            "admission": dict(self.admission) if self.admission is not None else None,
            "recipe": (
                {"mode": self.recipe.mode, "source": self.recipe.source, "framework": self.recipe.framework}
                if self.recipe is not None
                else None
            ),
            "run_on": self.driver.run_on if self.driver is not None else None,
            "executor_class": self.driver.executor_class.value if self.driver is not None else None,
            "budget": {
                "remaining_minutes": (round(self.remaining_minutes, 2) if self.remaining_minutes is not None else None),
                "minutes_per_minute": len(SIDES),
            },
            "ttl": {
                "default_seconds": DEFAULT_TTL_SECONDS,
                "max_seconds": min(MAX_TTL_SECONDS, self.recipe.ttl_cap_seconds) if self.recipe else MAX_TTL_SECONDS,
                "min_seconds": MIN_SESSION_SECONDS,
            },
            "opens_from": list(OPENS_FROM),
            "observation": "human",
        }


def remaining_minutes(ceilings: Mapping[str, Any], usage: Any) -> float | None:
    """Compute minutes left under the per-Review/monthly ceilings, ``None`` when none is set."""

    left: float | None = None
    if "per_review_minutes" in ceilings:
        left = float(ceilings["per_review_minutes"]) - float(getattr(usage, "review_minutes", 0.0) or 0.0)
    monthly_used = getattr(usage, "monthly_minutes", None)
    if "monthly_minutes" in ceilings and monthly_used is not None:
        monthly = float(ceilings["monthly_minutes"]) - float(monthly_used)
        left = monthly if left is None else min(left, monthly)
    return left


def static_framework(package_json: str | None) -> str:
    """``next``/``astro``/``vite`` declared by a trusted-base package.json, else ``""``."""

    try:
        decoded = json.loads(package_json) if package_json else {}
    except ValueError:
        return ""
    if not isinstance(decoded, Mapping):
        return ""
    declared: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        value = decoded.get(key)
        if isinstance(value, Mapping):
            declared.update(str(name) for name in value)
    return next((name for name in _STATIC_FRAMEWORKS if name in declared), "")


_ENTRY_KEYS: Final[tuple[str, ...]] = (
    "id",
    "status",
    "reason",
    "executor",
    "when",
    "origin",
    "estimated_minutes",
    "setup_hint",
)


def _entry_wire(entry: Any) -> dict[str, Any]:
    raw = entry if isinstance(entry, Mapping) else entry.to_wire()
    return {key: raw.get(key) for key in _ENTRY_KEYS}


def _refused(exc: PreviewRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.reason)


async def _body(request: Request, allowed: frozenset[str]) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="request body is too large")
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be JSON") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be a JSON object")
    unknown = sorted(set(decoded) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported interactive preview field(s): {', '.join(unknown)}",
        )
    return decoded


def register_interactive_preview_routes(router: APIRouter, deps: InteractivePreviewDeps) -> InteractivePreviewManager:
    """Add the interactive preview routes to *router*; returns the session manager."""

    backend = deps.backend
    manager = InteractivePreviewManager(
        store=deps.store,
        exposure=backend.exposure,
        compute=deps.compute,
        clock=backend.clock,
        frame_ancestors=backend.frame_ancestors,
        secure_cookies=backend.secure_cookies,
        viewer_authorized=backend.viewer_authorized,
        artifacts=deps.artifacts,
        state=backend.state,
        scope=backend.scope,
        drivers=backend.drivers,
        metering=backend.metering,
        max_live=backend.max_live_previews,
    )

    def declared_previews(revision: Any) -> tuple[dict[str, PreviewRecipe], str]:
        """The environment.toml previews, or why the file cannot run any."""

        try:
            return environment_preview_recipes(deps.environment_config(revision)), ""
        except PreviewRecipeError as exc:
            return {}, f"The trusted base's environment.toml previews cannot run: {exc}"

    def resolve_recipe(
        revision: Any, declared: Mapping[str, PreviewRecipe], broken: str, preview: str
    ) -> tuple[PreviewRecipe | None, str]:
        # environment.toml [previews] first; review.yaml interactive_preview only without them.
        if broken:
            return None, broken
        if declared:
            return declared[preview], ""
        try:
            recipe = load_preview_recipe(deps.review_config(revision))
        except PreviewRecipeError as exc:
            return None, f"The trusted base's interactive_preview is invalid: {exc}"
        if recipe is not None:
            return recipe, ""
        evidence = str(deps.detected(revision).evidence.get("previewable_app") or "")
        suffix = " static web build"
        if evidence.endswith(suffix):
            manifest = evidence[: -len(suffix)]
            framework = static_framework(deps.read_side(revision, manifest, "old"))
            if framework:
                return static_recipe(framework, posixpath.dirname(manifest) or "."), ""
        return None, NOT_DETECTED

    def admission(session: Any, revision: Any, displayed: Any) -> dict[str, Any] | None:
        """What a manual pick would plan, without persisting (the displayed row when it already runs)."""

        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        if displayed is not None and displayed.status in {"run", "not_available", "limited_by_budget", "inapplicable"}:
            return _entry_wire(displayed)
        preview = deps.preview_pick(session, revision, ManualPick(INTERACTIVE_PREVIEW_ENTRY))
        plan_wire = preview.get("plan") if isinstance(preview, Mapping) else None
        rows = plan_wire.get("entries") if isinstance(plan_wire, Mapping) else None
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and row.get("id") == INTERACTIVE_PREVIEW_ENTRY:
                return _entry_wire(row)
        return None

    def offer(session: Any, revision: Any, preview: str | None = None) -> _Offer:
        declared, broken = declared_previews(revision)
        if preview is not None and not broken and preview not in declared:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"no preview named {preview!r}; the trusted base declares {', '.join(declared)}"
                    if declared
                    else f"no preview named {preview!r}; the trusted base's environment.toml declares no previews"
                ),
            )
        selected = (preview or next(iter(declared))) if declared else ""
        found = _offer(session, revision, declared, broken, selected)
        return replace(found, previews=tuple(declared), preview=selected)

    def _offer(
        session: Any, revision: Any, declared: Mapping[str, PreviewRecipe], broken: str, selected: str
    ) -> _Offer:
        detected = deps.detected(revision)
        plan = deps.plan(session, revision)
        displayed = plan.entry(INTERACTIVE_PREVIEW_ENTRY)
        wire = _entry_wire(displayed) if displayed is not None else None
        if not detected.has("previewable_app"):
            from lemoncrow.pro.capabilities.review.capability_catalog import STATIC_CATALOG, applicability

            coverage, why = applicability(STATIC_CATALOG[INTERACTIVE_PREVIEW_ENTRY], detected)
            if coverage == "unknown":
                # Detection stopped at its path bound: absence is not established.
                return _Offer("not_available", why, wire)
            return _Offer("not_offered", NOT_DETECTED, wire)
        admitted = admission(session, revision, displayed)
        if admitted is None:
            return _Offer("not_offered", NOT_DETECTED, wire)
        state, reason = str(admitted.get("status") or ""), str(admitted.get("reason") or "")
        if state == "inapplicable":
            return _Offer("not_offered", reason or NOT_DETECTED, wire, admission=admitted)
        if state == "not_available":
            return _Offer("not_available", reason, wire, admission=admitted)
        if state == "limited_by_budget":
            return _Offer("limited_by_budget", reason or LIMITED_BY_BUDGET, wire, admission=admitted)
        if state != "run":
            return _Offer(
                "not_available", reason or f"the run plan would not run it ({state})", wire, admission=admitted
            )
        recipe, why = resolve_recipe(revision, declared, broken, selected)
        if recipe is None:
            return _Offer("not_available", why, wire, admission=admitted)
        if backend.unavailable:
            return _Offer("not_available", backend.unavailable, wire, recipe, admission=admitted)
        executor = str(admitted.get("executor") or "")
        driver = backend.drivers(executor)
        if driver is None:
            return _Offer(
                "not_available",
                f"interactive previews are not wired on the {executor} executor here",
                wire,
                recipe,
                admission=admitted,
            )
        problem = driver.unavailable()
        if problem:
            return _Offer("not_available", problem, wire, recipe, driver, admission=admitted)
        if recipe.mode == "static" and (not backend.static_builds or deps.build_static is None):
            return _Offer(
                "not_available",
                "this server previews only a trusted-base interactive_preview command; "
                "add one to .lemoncrow/review.yaml",
                wire,
                recipe,
                driver,
                admission=admitted,
            )
        left = remaining_minutes(plan.ceilings, deps.usage(session, revision))
        if left is not None and left < len(SIDES) * MIN_SESSION_SECONDS / 60:
            return _Offer(
                "limited_by_budget",
                f"{LIMITED_BY_BUDGET}: {max(0.0, left):.1f} compute minutes remain in this Review's budget",
                wire,
                recipe,
                driver,
                left,
                admission=admitted,
            )
        return _Offer("available", "", wire, recipe, driver, left, admission=admitted)

    def prepare_sides(session: Any, revision: Any, recipe: PreviewRecipe) -> list[PreparedSide]:
        from lemoncrow.pro.capabilities.review.hermetic import (
            HermeticRefused,
            HermeticUnavailable,
            provision_dependencies,
        )

        prepared: list[PreparedSide] = []
        created: list[Path] = []
        try:
            for side in SIDES:
                if recipe.mode == "static":
                    assert deps.build_static is not None
                    texts = {
                        name: deps.read_side(revision, posixpath.normpath(posixpath.join(recipe.root, name)), side)
                        for name in _LOCKFILES
                    }
                    tree = deps.build_static(revision, side, recipe.framework, recipe.root)
                    prepared.append(PreparedSide(side, tree, lockfile_digest=lockfile_text_digest(texts)))
                    continue
                # One owned directory per side: the side's teardown removes it whole.
                backend.tree_root.mkdir(parents=True, exist_ok=True)
                target = Path(tempfile.mkdtemp(prefix=f"ips-{side}-", dir=backend.tree_root))
                created.append(target)
                deps.materialize(revision, side, target)
                target.chmod(0o700)
                project = (target / recipe.root).resolve()
                if project != target.resolve() and target.resolve() not in project.parents:
                    raise PreviewStartFailed("interactive_preview.root escapes the revision")
                if not project.is_dir():
                    raise PreviewStartFailed(f"interactive_preview.root is missing on {side}: {recipe.root}")
                hermetic = deps.hermetic(session, revision, side)
                if hermetic.policy_error:
                    raise PreviewStartFailed(f"trusted-base execution policy is invalid: {hermetic.policy_error}")
                if hermetic.policy.fixtures or hermetic.policy.sidecars:
                    # Never run the app without services/seed data the trusted base declares.
                    raise PreviewStartFailed(
                        "trusted-base fixtures/sidecars are not supported for interactive previews yet; "
                        "not run without them"
                    )
                if backend.remote_provisioning:
                    from lemoncrow.pro.capabilities.review.hermetic import lockfile_digest

                    prepared.append(
                        PreparedSide(
                            side,
                            target,
                            lockfile_digest=lockfile_digest(project),
                            owned=True,
                            hermetic={"policy": hermetic.policy.to_wire(), "namespace": hermetic.namespace},
                        )
                    )
                    continue
                sandbox_project = "/workspace" if recipe.root in {"", "."} else f"/workspace/{recipe.root}"
                try:
                    provisioned = provision_dependencies(
                        project, policy=hermetic.policy, cache=hermetic.cache, sandbox_project=sandbox_project
                    )
                except (HermeticRefused, HermeticUnavailable) as exc:
                    raise PreviewStartFailed(f"hermetic dependency provisioning failed on {side}: {exc}") from exc
                prepared.append(
                    PreparedSide(
                        side,
                        target,
                        lockfile_digest=provisioned.lockfile_digest,
                        dependencies=provisioned,
                        owned=True,
                    )
                )
        except BaseException:
            for path in created:
                shutil.rmtree(path, ignore_errors=True)
            raise
        return prepared

    def session_wire(review_id: str, viewer: str, session_id: str) -> dict[str, Any]:
        for row in manager.describe(review_id, viewer):
            if row.get("id") == session_id:
                return row
        return {}

    def sweep_shared() -> None:
        # With shared state, a replica that never started a session still reaps
        # this scope's sessions whose starting process stopped serving them.
        if backend.state is not None:
            manager.ensure_sweeper()

    @router.get("/reviews/{review_id}/interactive-preview")
    async def _describe(review_id: str, preview: str | None = None) -> dict[str, Any]:
        sweep_shared()
        session = deps.session(review_id)
        revision = deps.latest(session)
        viewer = deps.viewer_id(session)

        def scoped_offer() -> Any:
            with request_reads.reads_once():
                return offer(session, revision, preview)

        current = await asyncio.to_thread(scoped_offer)
        sessions = await asyncio.to_thread(manager.describe, session.id, viewer)
        return {**current.to_wire(), "revision_id": revision.id, "sessions": sessions}

    @router.post("/reviews/{review_id}/interactive-preview/sessions")
    async def _start(review_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        # From the first start attempt, not the first success: a fresh replica refused at
        # capacity still reaps the scope's sessions whose starting process stopped serving them.
        manager.ensure_sweeper()
        body = await _body(request, _START_KEYS)
        session = deps.mutable_session(review_id)
        if str(getattr(session, "status", "open")) != "open":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="the Review is finished; reopen it to preview"
            )
        revision = deps.latest(session)
        deps.require_expected_revision(body, revision, "interactive preview")
        viewer = deps.viewer_id(session)
        if not viewer:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="an authenticated viewer is required")
        ttl_raw = body.get("ttl_seconds")
        if ttl_raw is not None and (isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ttl_seconds must be an integer"
            )
        wanted = body.get("preview")
        if wanted is not None and not isinstance(wanted, str):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="preview must be a string")
        current = await asyncio.to_thread(offer, session, revision, wanted)
        if current.state != "available" or current.recipe is None or current.driver is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=current.reason or f"interactive preview is {current.state.replace('_', ' ')}",
            )
        recipe, driver = current.recipe, current.driver
        # Capacity first: a start refused at the live-preview bound must not leave a
        # plan amendment behind, so the slot is held before the run plan is amended.
        try:
            slot = await asyncio.to_thread(manager.reserve, review_id=session.id, viewer_id=viewer)
        except PreviewRefused as exc:
            raise _refused(exc) from exc
        try:
            # Executed == displayed: the preview runs only as an admitted plan entry
            # (a manual amendment), on exactly this executor.
            planned = await asyncio.to_thread(
                deps.planned_request,
                session,
                revision,
                ManualPick(INTERACTIVE_PREVIEW_ENTRY),
                run_on=driver.run_on,
                action="interactive preview",
            )
            plan = await asyncio.to_thread(deps.plan, session, revision)
            left = remaining_minutes(plan.ceilings, await asyncio.to_thread(deps.usage, session, revision))
            managed_cap: ManagedSpendCap | None = None
            if driver.run_on == "managed" and "monthly_managed_usd" in plan.ceilings:
                # The monthly managed money cap is hard: re-read at start and while running.
                price = backend.usd_per_compute_second() if backend.usd_per_compute_second is not None else None
                managed_cap = ManagedSpendCap(
                    cap_usd=float(plan.ceilings["monthly_managed_usd"]),
                    spent_usd=lambda: deps.usage(session, revision).monthly_managed_usd,
                    usd_per_compute_second=float(price or 0.0),
                )
            hermetic = await asyncio.to_thread(deps.hermetic, session, revision, "new")
            record, token = await asyncio.to_thread(
                manager.start,
                review_id=session.id,
                revision_id=revision.id,
                viewer_id=viewer,
                driver=driver,
                recipe=recipe,
                prepare=lambda: prepare_sides(session, revision, recipe),
                plan_digest=str(planned.get("digest") or ""),
                ttl_seconds=ttl_raw,
                remaining_minutes=left,
                redaction=hermetic.policy.redaction,
                content_access=str(hermetic.content_access),
                managed_cap=managed_cap,
                slot=slot,
                preview=current.preview,
            )
        except PreviewRefused as exc:
            raise _refused(exc) from exc
        except BaseException:
            # start() gives its slot back itself; this covers a refusal before it ran.
            manager.release(slot)
            raise
        return {
            "session": session_wire(session.id, viewer, record.id) or record.to_wire(),
            "token": token,
            "plan": dict(planned),
        }

    @router.post("/reviews/{review_id}/interactive-preview/sessions/{session_id}/enter")
    async def _enter(review_id: str, session_id: str, request: Request) -> dict[str, Any]:
        sweep_shared()
        body = await _body(request, _ENTER_KEYS)
        session = deps.session(review_id)
        side = str(body.get("side") or "")
        if side not in SIDES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")
        path = body.get("path") if isinstance(body.get("path"), str) else "/"
        try:
            url = await asyncio.to_thread(
                manager.enter,
                review_id=session.id,
                session_id=session_id,
                viewer_id=deps.viewer_id(session),
                token=request.headers.get(TOKEN_HEADER, ""),
                side=side,
                next_path=str(path or "/"),
            )
        except PreviewRefused as exc:
            raise _refused(exc) from exc
        return {"url": url}

    @router.post("/reviews/{review_id}/interactive-preview/sessions/{session_id}/stop")
    async def _stop(review_id: str, session_id: str, request: Request) -> dict[str, Any]:
        sweep_shared()
        await _body(request, _STOP_KEYS)
        session = deps.session(review_id)
        try:
            record = await asyncio.to_thread(
                manager.stop, review_id=session.id, session_id=session_id, viewer_id=deps.viewer_id(session)
            )
        except PreviewRefused as exc:
            raise _refused(exc) from exc
        return {"session": record.to_wire()}

    return manager


def close_backend(backend: InteractivePreviewBackend, manager: InteractivePreviewManager | None) -> None:
    """Stop every session and remove the backend's own directories (server shutdown)."""

    if manager is not None:
        with contextlib.suppress(Exception):
            manager.shutdown()
    for closer in backend.closers:
        with contextlib.suppress(Exception):
            closer()


__all__ = [
    "OPENS_FROM",
    "TOKEN_HEADER",
    "InteractivePreviewBackend",
    "InteractivePreviewDeps",
    "close_backend",
    "register_interactive_preview_routes",
    "remaining_minutes",
    "static_framework",
]
