"""The Review service: every Review read, title, projection and Reader route.

It reads and writes the file-backed Review stores only; it never opens the code
index. Code intelligence and content reach it through injected providers --
the core's own objects when the core hosts it (tests, debugging), the core's
``/internal/v1`` back-channel when it runs as its own worker process.

Capture (``POST /v1/repos/{repo_id}/reviews``) and ``base-view`` stay in the
core: they turn an indexed View into a Snapshot and so must read the index.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import logging
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from lemoncrow_client.review_words import part_count, revision_name

from lemoncrow.pro.capabilities.review import fix_lines, titles
from lemoncrow.pro.capabilities.review.archive_safety import owed_cancellation
from lemoncrow.pro.capabilities.review.development import DevelopmentStore, DevelopmentUnavailable
from lemoncrow.pro.capabilities.review.hosted_attachment import attached_identity
from lemoncrow.pro.capabilities.review.inbox_progress import stored_progress
from lemoncrow.pro.capabilities.review.readiness import SnapshotProgress
from lemoncrow.pro.capabilities.review.relationships import change_context_payload
from lemoncrow.pro.capabilities.review.revisions import compute_frontier
from lemoncrow.pro.capabilities.review.saved_filters import wants_view_mode
from lemoncrow.pro.capabilities.review.sources.local import read_packet_json, review_subject
from lemoncrow.pro.capabilities.review.store import (
    DB_NAME,
    DerivedResult,
    ReviewListInputs,
    ReviewStore,
    ShortReferenceIndex,
)
from lemoncrow.pro.capabilities.review.targets import (
    DerivedRefresh,
)

from .context import Principal, validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .http.wire import json_response, require_object, require_str
from .local_inbox import SEARCH_KEYS, LocalInbox, local_review_wait, local_review_workflow
from .local_project_knowledge import LocalProjectKnowledge
from .local_review_analytics import LocalReviewAnalytics
from .local_reviews import LocalReviewReader, LocalReviewRepository
from .protocol import BRANCH_REVIEW_PATH, branch_review_query, branch_review_wire, require_packet_schema
from .review_preparation import ReviewPreparationJobs

__all__ = ["LocalReviewService"]

_LOG = logging.getLogger("lemoncrow.server.local_review_service")
_USER_SETTINGS_OWNER = "local"


def _review_handoff(
    store: ReviewStore,
    items: Sequence[tuple[Any, ReviewListInputs]],
    progress: Mapping[tuple[str, str], Mapping[str, int]],
    repo_id: str,
) -> dict[str, dict[str, Any]]:
    """Per Review: whether its feedback is back with the reviewer or waiting on its fixer."""

    from lemoncrow.pro.capabilities.review.handoff import (
        approved_on,
        handoff_row_fields,
        review_handoff,
        settled_by_approval,
    )
    from lemoncrow.pro.capabilities.review.sources.github import GITHUB_SOURCE_PREFIX

    ids = [str(session.id) for session, _item in items]
    versions, deliveries = store.feedback_rows_for_reviews(ids)
    side = store.inbox_side_facts(ids)
    out: dict[str, dict[str, Any]] = {}
    for session, item in items:
        session_id = str(session.id)
        if item.revision is None:
            out[session_id] = {}
            continue
        pull_request = str(session.source_ref).startswith(GITHUB_SOURCE_PREFIX) or (
            store.get_destination(session_id).kind == "github_pr"
        )
        counts = progress[(repo_id, session_id)]
        out[session_id] = handoff_row_fields(
            settled_by_approval(
                review_handoff(item.revision.id, item.annotations, versions, deliveries, pull_request=pull_request),
                approved_on(side.get(session_id, {}), str(item.revision.id)),
            ),
            status=str(session.status),
            actionable_targets=(
                counts["unreviewed"] + counts["changed_since_review"] + counts["unknown"] + counts.get("later", 0)
            ),
        )
    return out


def _git_stdout(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@contextlib.contextmanager
def _fix_line_errors() -> Iterator[None]:
    try:
        yield
    except fix_lines.FixLineError as exc:
        raise ServerError(
            ErrorCode.REVIEW_STATE_CONFLICT,
            str(exc),
            details={"files": list(exc.files)},
            action=AgentAction.FIX_REQUEST,
        ) from exc


def _automatic_review_title(
    session: Any, revision: Any | None, packet: Mapping[str, Any] | None, *, author_title: str = ""
) -> str:
    if author_title:
        return author_title
    if revision is None:
        return str(session.title or "Working tree")
    return titles.packet_title(packet, fallback=str(session.title or "Changes"))


@functools.cache
def _title_code_stamp() -> str:
    """Identity of the title rule, so a stored title made by other code is re-derived."""

    try:
        source = inspect.getsource(_automatic_review_title) + inspect.getsource(titles)
    except (OSError, TypeError):
        source = ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _stamp(*inputs: Any) -> str:
    return hashlib.sha256(repr(inputs).encode("utf-8", "surrogatepass")).hexdigest()


def _store_derived(store: ReviewStore, results: Sequence[DerivedResult]) -> None:
    """Best effort: what was served is exact either way; a failed write only re-derives next time."""

    if not results:
        return
    try:
        store.put_derived_results(results)
    except store.DRIVER_ERRORS:
        _LOG.warning("could not store derived Review list results", exc_info=True)


def _author_task_title(
    store: Any, session: Any, revision: Any | None, *, attributions: Sequence[Any] | None = None
) -> str:
    if revision is None:
        return ""
    authors = {
        (row.host, row.session_id)
        for row in (store.list_revision_attributions(revision.id) if attributions is None else attributions)
        if row.scope == "revision" and row.role == "primary" and row.certainty == "exact"
    }
    if len(authors) != 1:
        return ""
    host, session_id = next(iter(authors))
    try:
        author = DevelopmentStore(store).get_session(session.id, host, session_id)
    except DevelopmentUnavailable:
        return ""
    if author is None or author.role != "author":
        return ""
    title = author.task.splitlines()[0].strip(" #") if author.task else ""
    return title if 3 <= len(title) <= 120 and title.lower() not in {"working tree", "staged changes"} else ""


def _public_snapshot_number(revision: Any | None) -> int:
    """Translate one-based persistence ordinals to the public Snapshot number."""

    if revision is None:
        return 0
    return max(0, int(getattr(revision, "revision_number", 0) or 0) - 1)


def _review_location_hint(root: str) -> dict[str, Any]:
    """Project a packet workspace path without treating it as readable source.

    The path is client-supplied display metadata.  Do not resolve it, stat it, or
    run Git against it here: only a negotiated local-fs grant can authorize
    filesystem reads.
    """

    path = Path(root).expanduser()
    if not path.is_absolute():
        return {}
    name = path.name or str(path)
    return {
        "directory": str(path.parent),
        "project": name,
        "project_root": str(path),
        "worktree": name,
        "worktree_root": str(path),
        "branch": "",
        "revision": "",
        "detached": False,
    }


def _review_location_metadata(root: Path) -> dict[str, Any]:
    """Describe the exact proven checkout/worktree behind a local review."""

    resolved = root.expanduser().resolve(strict=False)
    branch = ""
    revision = ""
    project_root = resolved
    if resolved.is_dir():
        branch = _git_stdout(resolved, "branch", "--show-current")
        revision = _git_stdout(resolved, "rev-parse", "--short", "HEAD")
        common_dir_raw = _git_stdout(resolved, "rev-parse", "--git-common-dir")
        if common_dir_raw:
            common_dir = Path(common_dir_raw)
            if not common_dir.is_absolute():
                common_dir = (resolved / common_dir).resolve(strict=False)
            if common_dir.name == ".git":
                project_root = common_dir.parent

    return {
        "directory": str(resolved.parent),
        "project": project_root.name,
        "project_root": str(project_root),
        "worktree": resolved.name,
        "worktree_root": str(resolved),
        "branch": branch or (f"detached@{revision}" if revision else "unknown"),
        "revision": revision,
        "detached": not bool(branch),
    }


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def _git_head_stamp(root: Path) -> tuple[Any, ...] | None:
    """What the three Git reads behind :func:`_review_location_metadata` depend on, read from files.

    The checkout's HEAD (its text and file), the ref it names (loose, in the worktree or the common dir) and
    ``packed-refs``. Git replaces each of them by renaming a lock file over it, so any branch switch, commit or
    ref pack changes the stamp. ``None`` -- ask Git every time -- whenever that cannot be said: no ``.git`` at the
    root, an unreadable ``gitdir`` pointer, or a reftable repository (no single file moves with a ref).
    """

    dot = root / ".git"
    try:
        if dot.is_dir():
            git_dir = dot
        elif dot.is_file():
            pointer = dot.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer.removeprefix("gitdir:").strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve(strict=False)
        else:
            return None
        common = git_dir
        common_pointer = git_dir / "commondir"
        if common_pointer.is_file():
            named = Path(common_pointer.read_text(encoding="utf-8").strip())
            common = named if named.is_absolute() else (git_dir / named).resolve(strict=False)
        if (common / "reftable").exists() or (git_dir / "reftable").exists():
            return None
        head = git_dir / "HEAD"
        head_text = head.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    stamp: list[Any] = [str(git_dir), head_text, _stat_key(head), _stat_key(common / "packed-refs")]
    if head_text.startswith("ref:"):
        ref = head_text.removeprefix("ref:").strip()
        stamp += [_stat_key(git_dir / ref), _stat_key(common / ref)]
    return tuple(stamp)


class _CheckoutLocations:
    """Checkout metadata kept across requests while the checkout's HEAD and refs are unchanged (bounded, LRU).

    Home lists every local Review on each load, and each checkout costs three Git subprocesses (about 40 ms):
    with dozens of checkouts that was most of the read. A checkout whose stamp (:func:`_git_head_stamp`) cannot be
    taken is never kept, so it is described by Git on every request as before. The short revision Git prints may
    lengthen as a repository grows; a kept one stays a valid prefix until HEAD next moves.
    """

    LIMIT = 1024

    def __init__(self) -> None:
        self._kept: OrderedDict[Path, tuple[tuple[Any, ...], dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def describe(self, root: Path) -> dict[str, Any]:
        stamp = _git_head_stamp(root)
        if stamp is not None:
            with self._lock:
                kept = self._kept.get(root)
                if kept is not None and kept[0] == stamp:
                    self._kept.move_to_end(root)
                    return dict(kept[1])
        described = _review_location_metadata(root)
        if stamp is not None:
            with self._lock:
                self._kept[root] = (stamp, dict(described))
                self._kept.move_to_end(root)
                while len(self._kept) > self.LIMIT:
                    self._kept.popitem(last=False)
        return described

    def warm(self, roots: Sequence[Path]) -> None:
        """Describe the checkouts not yet kept, a few at a time: each is Git subprocesses that wait, not compute."""

        cold = [root for root in dict.fromkeys(roots) if not self._fresh(root)]
        if len(cold) < 2:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(cold)), thread_name_prefix="lc-checkout") as pool:
            list(pool.map(self.describe, cold))

    def _fresh(self, root: Path) -> bool:
        stamp = _git_head_stamp(root)
        with self._lock:
            kept = self._kept.get(root)
        return stamp is not None and kept is not None and kept[0] == stamp


async def _to_thread[T](fn: Callable[..., T], *args: Any) -> T:
    return await asyncio.to_thread(fn, *args)


class LocalReviewService:
    """Review read side over one principal's Review stores."""

    def __init__(
        self,
        repository: LocalReviewRepository,
        reader: LocalReviewReader,
        *,
        principal: Callable[[web.Request], Principal],
        body: Callable[[web.Request], Awaitable[dict[str, Any]]],
        run_blocking: Callable[..., Awaitable[Any]] = _to_thread,
    ) -> None:
        self.review_repository = repository
        self.review_reader = reader
        self._principal = principal
        self._body = body
        self._in_worker = run_blocking
        # Checkout metadata kept while the checkout's HEAD and refs stay put (Home reads every Review each load).
        self._locations = _CheckoutLocations()
        # Snapshots already warmed (or being warmed), LRU-capped, and the live tasks.
        self._warmed: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._warm_tasks: set[asyncio.Task[None]] = set()
        self._preparation_jobs = ReviewPreparationJobs()
        # P9-INBOX views and the local user's saved filters.
        self._inbox = LocalInbox(self)
        # P13-ANALYTICS: advisory Review analytics (read-only, never a merge gate).
        self._analytics = LocalReviewAnalytics(self)
        # UX0b: the Project Knowledge surface (derived context, never Review policy).
        self._knowledge = LocalProjectKnowledge(self)
        # Trust T2: Review Status repeats this Inbox's workflow and next action.
        reader.set_review_workflow(self._review_workflow)

    def _review_workflow(
        self, repo_id: str, store: ReviewStore, session: Any, as_of: datetime, progress: SnapshotProgress | None
    ) -> Mapping[str, Any] | None:
        return local_review_workflow(
            store, repo_id, session, progress_of=self._review_progress, as_of=as_of, progress=progress
        )

    def review_wait(self, review_id: str) -> tuple[str, float] | None:
        """The friction metric and seconds this Review has waited, for the Reader's one count per open."""

        repo_id, store, session, _root = self._fix_scope(review_id, need_checkout=False)
        return local_review_wait(store, repo_id, session, progress_of=self._review_progress, as_of=datetime.now(UTC))

    def _schedule_warm(self, repo_id: str, review_id: str, phase: str) -> None:
        """Warm the Reader's first-load caches for the latest Snapshot, once per phase.

        Called after the Snapshot's projection is enriched and when preparation
        reports ready (never earlier: caches are keyed by Snapshot), so the reviewer's first request is a cache hit.
        """

        try:
            store, session = self.review_repository.require_review(repo_id, review_id)
            revision = store.latest_revision(session.id)
        except ServerError:
            return
        if revision is None:
            return
        self._start_warm(repo_id, session, revision.id, phase)

    def schedule_capture_warm(self, repo_id: str, session: Any, revision_id: str) -> None:
        """Start the background derivation of a Snapshot the moment its capture is recorded.

        A capture is the durable event: everything the Reader's first screens need (overview,
        targets, every card's frozen code, readiness, the Brief) is computed now, not when the
        URL is first opened. Call on the event loop; it does no store read itself.
        """

        self._start_warm(repo_id, session, revision_id, "captured")

    def _start_warm(self, repo_id: str, session: Any, revision_id: str, phase: str) -> None:
        key = (str(session.id), revision_id, phase)
        if key in self._warmed:
            return
        self._warmed[key] = None
        while len(self._warmed) > 256:
            self._warmed.popitem(last=False)
        task = asyncio.get_running_loop().create_task(self._warm(repo_id, session))
        self._warm_tasks.add(task)
        task.add_done_callback(self._warm_tasks.discard)

    async def _warm(self, repo_id: str, session: Any) -> None:
        # The list row (title, progress) is stored per Snapshot too, so the
        # inbox's first load after a capture reads it instead of deriving it.
        # Stored first: it is a store write, and the Reader warm-up below caches
        # responses against the store's write generation.
        try:
            await self._in_worker(self._warm_list_row, repo_id, session)
        except Exception:
            _LOG.exception("Review list row warm-up failed review=%s", session.id)
        await self.review_reader.warm(str(session.id))

    def _warm_list_row(self, repo_id: str, session: Any) -> None:
        store = self.review_repository.store(repo_id)
        for _session, inputs, _row in self._review_wire_rows(repo_id, [session]):
            self._review_progress(store, [(session, inputs)])

    def route_table(self) -> tuple[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]], ...]:
        """Every route this service owns, besides the Reader's ``/api`` subtree.

        The core registers the same table -- forwarding each entry when the
        service runs elsewhere -- so the two can never disagree on a route.
        """
        return (
            ("GET", "/v1/reviews", self.review_inbox),
            ("GET", "/v1/repos/{repo_id}/reviews", self.review_list),
            ("GET", "/v1/repos/{repo_id}/reviews/{review_id}", self.review_get),
            (
                "POST",
                "/v1/repos/{repo_id}/reviews/{review_id}/revisions/{revision_id}/projection",
                self.review_enrich_projection,
            ),
            ("POST", "/v1/repos/{repo_id}/reviews/{review_id}/preparation", self.review_update_preparation),
            ("POST", "/api/review-preparations", self.preparation_create),
            ("GET", "/api/review-preparations/{job_id}", self.preparation_get),
            ("POST", "/api/review-preparations/{job_id}", self.preparation_update),
            ("GET", "/api/reviews", self.review_api_directory),
            ("GET", BRANCH_REVIEW_PATH, self.review_branch_review),
            ("GET", "/api/review-intelligence", self.review_intelligence),
            ("PUT", "/api/review-intelligence/settings", self.review_intelligence_settings_update),
            ("GET", "/api/review-settings", self.user_settings_get),
            ("PATCH", "/api/review-settings", self.user_settings_patch),
            ("DELETE", "/api/review-settings", self.user_settings_reset),
            ("GET", "/api/reviews/{review_id}/fix-lines", self.review_fix_lines),
            ("POST", "/api/reviews/{review_id}/snapshots/{revision_id}/fix-line", self.review_start_fix_line),
            ("POST", "/api/reviews/{review_id}/fix-lines/{fix_id}/apply-plan", self.review_fix_apply_plan),
            ("POST", "/api/reviews/{review_id}/fix-lines/{fix_id}/apply", self.review_fix_apply),
            ("POST", "/api/reviews/{review_id}/fix-lines/{fix_id}/resolved", self.review_fix_resolved),
            ("POST", "/api/reviews/{review_id}/fix-lines/{fix_id}/discard", self.review_fix_discard),
            ("POST", "/api/reviews/{review_id}/fix-lines/{fix_id}/recreate", self.review_fix_recreate),
            ("PUT", "/api/repos/{repo_id}/reviews/{review_id}/title", self.review_set_title),
            *self._inbox.routes(),
            *self._analytics.routes(),
            *self._knowledge.routes(),
        )

    async def preparation_create(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        return json_response(self._preparation_jobs.create())

    async def preparation_get(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        row = self._preparation_jobs.get(request.match_info["job_id"])
        if row is None:
            raise web.HTTPNotFound(text="Preparation expired. Run lc review again to retry.")
        response = json_response(row)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def preparation_update(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        body = await self._body(request)
        stage = require_str(body, "stage", max_len=128) if body.get("stage") != "" else ""
        detail = require_str(body, "detail", max_len=2048) if body.get("detail") != "" else ""
        review_url = require_str(body, "review_url", max_len=2048) if body.get("review_url") != "" else ""
        if review_url and (not review_url.startswith("/r/") or "\\" in review_url):
            raise web.HTTPBadRequest(text="Preparation must open a local Review URL.")
        row = self._preparation_jobs.update(
            request.match_info["job_id"], stage=stage, detail=detail, review_url=review_url
        )
        if row is None:
            raise web.HTTPNotFound(text="Preparation expired.")
        return json_response(row)

    async def reader_api(self, request: web.Request) -> web.StreamResponse:
        return await self.review_reader.handle(request)

    # -- wire ----------------------------------------------------------- #

    def _review_location_wire(
        self,
        repo_id: str,
        session: Any,
        *,
        metadata_cache: dict[Path, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Human identity for a Change, independent of source-read authority.

        A directory page may contain dozens of Reviews from one checkout.
        Avoid repeating three Git subprocesses for each row while keeping the
        cache request-scoped (a later request must see branch/HEAD changes).
        """

        kept: _CheckoutLocations | None = getattr(self, "_locations", None)
        describe = _review_location_metadata if kept is None else kept.describe

        def metadata(root: Path) -> dict[str, Any]:
            if metadata_cache is None:
                return describe(root)
            cached = metadata_cache.get(root)
            if cached is None:
                cached = describe(root)
                metadata_cache[root] = cached
            # Branch hints describe each Review's captured identity; never
            # mutate the shared checkout projection when applying a hint.
            return dict(cached)

        hint = self.review_repository.workspace_root_hint(repo_id, str(session.id))
        branch_hint = self.review_repository.workspace_branch_hint(repo_id, str(session.id))
        trusted = self.review_repository.checkout_root(repo_id)
        if hint:
            if trusted is not None:
                try:
                    if trusted == Path(hint).expanduser().resolve(strict=False):
                        location = metadata(trusted)
                        if branch_hint and location["branch"] != branch_hint:
                            # The checkout moved on; this Change belongs to the
                            # branch it was captured on, not the current one.
                            location.update(branch=branch_hint, revision="", detached=False)
                        return location
                except (OSError, ValueError):
                    pass
            hinted = _review_location_hint(hint)
            if hinted:
                if branch_hint:
                    hinted["branch"] = branch_hint
                return hinted
        if trusted is not None:
            location = metadata(trusted)
            if branch_hint and location["branch"] != branch_hint:
                # A capture with no readable checkout (``lcr`` uploads Views) still
                # names its branch; the Change belongs to that branch.
                location.update(branch=branch_hint, revision="", detached=False)
            return location
        return {
            "directory": "",
            "project": "",
            "project_root": "",
            "worktree": "",
            "worktree_root": "",
            "branch": branch_hint,
            "revision": "",
            "detached": False,
        }

    def warm_checkout_locations(self, repo_ids: Iterable[str]) -> None:
        """Describe these repositories' proven checkouts ahead of a page's rows, concurrently (see ``_CheckoutLocations``)."""

        roots = [root for repo_id in repo_ids if (root := self.review_repository.checkout_root(repo_id)) is not None]
        self._locations.warm(roots)

    def review_session_wire(
        self,
        repo_id: str,
        session: Any,
        *,
        location: Mapping[str, Any] | None = None,
        references: ShortReferenceIndex | None = None,
    ) -> dict[str, Any]:
        """One Review's list row."""

        locations = None if location is None else {str(session.id): location}
        return self._review_wire_rows(repo_id, [session], references=references, locations=locations)[0][2]

    def _review_wire_rows(
        self,
        repo_id: str,
        sessions: Sequence[Any],
        *,
        references: ShortReferenceIndex | None = None,
        locations: Mapping[str, Mapping[str, Any]] | None = None,
        location_metadata_cache: dict[Path, dict[str, Any]] | None = None,
    ) -> list[tuple[Any, ReviewListInputs, dict[str, Any]]]:
        """List rows for several Reviews of one repository, from one bundled read.

        Every input a row derives from -- current Snapshot, attributions, the
        reviewer's marks, annotations, active bindings -- is read once for all
        of them; titles come from stored per-Snapshot results. Rows keep the
        order of *sessions*.
        """

        if not sessions:
            return []
        repository = self.review_repository
        store = repository.store(repo_id)
        inputs = store.review_list_inputs(sessions)
        bindings = store.active_bindings()
        # One identity (G5-SYNC): an attached Review is named by its hosted reference.
        attachments = store.hosted_attachments() if store.hosted_attachment_supported() else {}
        titles = self._review_titles(repo_id, store, sessions, inputs)
        if references is None:
            references = repository.review_references()
        rows: list[tuple[Any, ReviewListInputs, dict[str, Any]]] = []
        metadata_cache = location_metadata_cache if location_metadata_cache is not None else {}
        for session in sessions:
            item = inputs[str(session.id)]
            revision = item.revision
            auto_title = titles[str(session.id)]
            manual_title = repository.manual_review_title(repo_id, session.id)
            attachment = attachments.get(str(session.id))
            short_id = (
                attachment.hosted_review_id
                if attachment is not None
                else repository.short_review_reference(session.id, references=references)
            )
            location = (locations or {}).get(str(session.id)) or self._review_location_wire(
                repo_id, session, metadata_cache=metadata_cache
            )
            branch = str(location.get("branch") or "").strip()
            branch_active = False
            subject = review_subject(
                f"local:{repo_id}", branch=branch, worktree_key="", range_mode=str(session.range_mode)
            )
            if subject is not None:
                active = bindings.get(
                    (subject.repository_identity.strip(), subject.subject_kind, subject.subject_key.strip())
                )
                branch_active = active is not None and active.review_id == session.id
            row = {
                "id": session.id,
                "short_id": short_id,
                "ref": f"r/{short_id}",
                "object": "change",
                "change_ref": f"r/{short_id}",
                "review_path": f"/r/{short_id}",
                "repo_id": repo_id,
                "title": manual_title or auto_title,
                "auto_title": auto_title,
                "title_is_custom": bool(manual_title),
                "subject_type": session.subject_type,
                "source_ref": session.source_ref,
                "range_mode": session.range_mode,
                "review_base_sha": session.review_base_sha,
                "branch_active": branch_active,
                "actor_type": session.actor_type,
                "status": session.status,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "revision_number": _public_snapshot_number(revision),
                "revision_id": "" if revision is None else revision.id,
                **location,
            }
            if attachment is not None:
                # The canonical Change is hosted's (RC5); the local id is provenance only.
                row.update(
                    attached_identity(
                        local_review_id=str(session.id),
                        origin=attachment.origin,
                        hosted_review_id=attachment.hosted_review_id,
                    )
                )
            rows.append((session, item, row))
        return rows

    def _review_titles(
        self,
        repo_id: str,
        store: ReviewStore,
        sessions: Sequence[Any],
        inputs: Mapping[str, ReviewListInputs],
    ) -> dict[str, str]:
        """Automatic titles; only a Snapshot without a stored title opens its packet."""

        titles: dict[str, str] = {}
        pending: dict[str, tuple[Any, Any, str]] = {}
        for session in sessions:
            revision = inputs[str(session.id)].revision
            if revision is None:
                titles[str(session.id)] = _automatic_review_title(session, None, None)
                continue
            author_title = (
                self.review_repository.author_snapshot_title(repo_id, revision.id)
                or revision.title
                or _author_task_title(store, session, revision, attributions=inputs[str(session.id)].attributions)
            )
            if author_title:
                titles[str(session.id)] = author_title
                continue
            pending[str(session.id)] = (session, revision, _stamp(_title_code_stamp(), revision, session.title))
        stored = store.derived_results("title", (revision.id for _session, revision, _stamp_ in pending.values()))
        fresh: list[DerivedResult] = []
        for session_id, (session, revision, stamp) in pending.items():
            hit = stored.get((revision.id, ""))
            if hit is not None and hit[0] == stamp and isinstance(hit[1].get("title"), str):
                titles[session_id] = str(hit[1]["title"])
                continue
            title = _automatic_review_title(session, revision, read_packet_json(store, revision))
            titles[session_id] = title
            fresh.append(
                DerivedResult(
                    review_id=session_id,
                    revision_id=revision.id,
                    kind="title",
                    scope_key="",
                    stamp=stamp,
                    payload={"title": title},
                )
            )
        _store_derived(store, fresh)
        return titles

    def _review_progress(
        self,
        store: ReviewStore,
        items: Sequence[tuple[Any, ReviewListInputs]],
        refresh: DerivedRefresh | None = None,
    ) -> dict[str, dict[str, int]]:
        """Target progress per Review, stored per (Snapshot, reviewer).

        The stamp is the Snapshot row (which fixes its units and packet), the
        reviewer's marks, the annotations and the deriving code. A change to
        any input re-derives; a change of deriving code alone re-derives
        within ``refresh``'s budget (see ``DerivedRefresh``). An unchanged
        Review is one stored read.
        """

        return stored_progress(
            store, items, refresh, read_packet=read_packet_json, reader_targets=self.review_reader.reader_targets
        )

    def review_count(self) -> int:
        """How many Reviews exist, without building a wire row (git reads) for each."""
        return len(self.review_repository.review_ids())

    # -- routes --------------------------------------------------------- #

    async def review_inbox(self, request: web.Request) -> web.StreamResponse:
        """List local Reviews without projecting hosted collaboration state."""

        self._principal(request)
        if request.query.get("summary") == "1":
            # Initial directory paint must not score all Reviews, read annotations,
            # generate titles, or run Git. Rich Inbox facts arrive separately.
            if (
                request.query.get("status", "open") != "open"
                or request.query.get("q", "")
                or request.query.get("cursor", "")
                or request.query.get("filter", "")
                or request.query.get("workflow", "")
                or request.query.get("view", "") not in {"", "all"}
            ):
                raise ServerError(ErrorCode.PAYLOAD_INVALID, "summary is only available for the default open list")
            try:
                limit = max(1, min(int(request.query.get("limit", "50")), 100))
            except ValueError as exc:
                raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer") from exc
            started = time.perf_counter()
            reviews = await self._in_worker(self._review_summary_rows, limit)
            result = json_response(
                {"reviews": reviews, "status": "open", "query": "", "next_cursor": "", "summary": True}
            )
            result.headers["Server-Timing"] = f"review_summary;dur={(time.perf_counter() - started) * 1000:.1f}"
            return result
        if wants_view_mode(request.query):
            return await self._inbox.views(request)
        # lc-debt: the legacy list (no view parameter) still clamps limit and never pages;
        # upgrade path: move its callers to the view mode, then retire this branch.
        status = request.query.get("status", "all").strip() or "all"
        if status not in {"all", "open", "finished", "archived"}:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "status must be one of all, open, finished, archived")
        query = request.query.get("q", "").strip().lower()
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError as exc:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer") from exc
        limit = max(1, min(limit, 100))
        # Listing opens every repository's Review store: off the loop.
        page_rows = await self._in_worker(self._review_inbox_page, status, query, limit)
        return json_response(
            {
                "reviews": page_rows,
                "status": status,
                "query": query,
                "next_cursor": "",
            }
        )

    def _review_summary_rows(self, limit: int) -> list[dict[str, Any]]:
        """Read-only, session-index first paint; never open Review bodies or Git.

        Opening every ReviewStore here triggers migrations and other setup for
        repositories the user is not even opening. Read the stable session
        columns directly; the authoritative view API handles rich projections.
        """
        repository = self.review_repository
        columns = (
            "id",
            "title",
            "source_ref",
            "range_mode",
            "status",
            "current_revision_id",
            "updated_at",
            "created_at",
        )
        candidates: list[tuple[str, dict[str, str]]] = []
        for repo_id in repository.list_repository_ids():
            db_path = repository.store_root(repo_id) / DB_NAME
            if not db_path.is_file():
                continue
            try:
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT s.id, s.title, s.source_ref, s.range_mode, s.status, "
                        "s.current_revision_id, s.updated_at, s.created_at "
                        "FROM review_sessions AS s WHERE s.status = 'open' "
                        "AND NOT EXISTS (SELECT 1 FROM review_relationships AS rel "
                        "WHERE rel.kind = 'fix_line' AND rel.target_review_id = s.id) "
                        "ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    candidates.extend((repo_id, {name: str(row[name] or "") for name in columns}) for row in rows)
            except sqlite3.Error:
                # An older schema still needs the normal store's migration path.
                # Never silently lose its Reviews from the initial list.
                sessions = repository.list_reviews(repo_id, status="open", limit=limit)
                if not sessions:
                    continue
                hidden = fix_lines.fix_child_review_ids(repository.store(repo_id))
                candidates.extend(
                    (repo_id, {name: str(getattr(session, name, "") or "") for name in columns})
                    for session in sessions
                    if session.id not in hidden
                )
        candidates.sort(key=lambda item: (item[1]["updated_at"], item[1]["id"]), reverse=True)
        summaries: list[dict[str, Any]] = []
        for repo_id, session in candidates[:limit]:
            review_id = session["id"]
            hint = repository.workspace_root_hint(repo_id, review_id)
            location = _review_location_hint(hint) if hint else {}
            branch = repository.workspace_branch_hint(repo_id, review_id)
            if branch:
                location["branch"] = branch
            summaries.append(
                {
                    "id": review_id,
                    "repo_id": repo_id,
                    "ref": f"r/{review_id}",
                    "review_path": f"/r/{review_id}",
                    "title": repository.manual_review_title(repo_id, review_id) or session["title"] or "Review",
                    "source_ref": session["source_ref"],
                    "range_mode": session["range_mode"],
                    "status": session["status"],
                    "revision_id": session["current_revision_id"],
                    "updated_at": session["updated_at"],
                    "created_at": session["created_at"],
                    **location,
                }
            )
        return summaries

    def review_rows(self, *, status: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        """Newest-first wire rows across repositories (no progress); *limit* builds only that page."""

        return [row for _repo_id, _session, _item, row in self._review_entries(status or "all", "", limit)]

    def _review_inbox_page(self, status: str, query: str, limit: int) -> list[dict[str, Any]]:
        page = self._review_entries(status, query, limit)
        by_repo: dict[str, list[tuple[Any, ReviewListInputs]]] = {}
        for repo_id, session, item, _row in page:
            by_repo.setdefault(repo_id, []).append((session, item))
        progress: dict[tuple[str, str], dict[str, int]] = {}
        handoff: dict[tuple[str, str], dict[str, Any]] = {}
        owed: dict[tuple[str, str], dict[str, Any]] = {}
        refresh = DerivedRefresh()  # one budget for the whole page
        for repo_id, items in by_repo.items():
            store = self.review_repository.store(repo_id)
            for session_id, value in self._review_progress(store, items, refresh).items():
                progress[(repo_id, session_id)] = value
            for session_id, fields in _review_handoff(store, items, progress, repo_id).items():
                handoff[(repo_id, session_id)] = fields
            for session, _item in items:
                if session.status != "archived":
                    continue
                # A provider cancellation owed by a discard stays visible until confirmed.
                pending = owed_cancellation(store, session.id)
                if pending is not None:
                    owed[(repo_id, str(session.id))] = pending
        return [
            {
                **row,
                "progress": progress[(repo_id, str(session.id))],
                **handoff[(repo_id, str(session.id))],
                **(
                    {"owed_cancellation": owed[(repo_id, str(session.id))]}
                    if (repo_id, str(session.id)) in owed
                    else {}
                ),
            }
            for repo_id, session, _item, row in page
        ]

    def _review_entries(
        self, status: str, query: str, limit: int | None
    ) -> list[tuple[str, Any, ReviewListInputs, dict[str, Any]]]:
        """Newest-first rows across repositories: one session read per repository,
        then one bundled read (plus one stored-titles read) per repository on the page."""

        repository = self.review_repository
        listed = {
            repo_id: repository.list_reviews(repo_id, status="", limit=None)
            for repo_id in repository.list_repository_ids()
        }
        # Short references are unique across every Review, whatever the filter;
        # the sessions just read are that whole domain.
        references = ShortReferenceIndex(
            (session.id for sessions in listed.values() for session in sessions), revision=False
        )
        hidden = {repo_id: fix_lines.fix_child_review_ids(repository.store(repo_id)) for repo_id in listed}
        wanted = sorted(
            (
                (repo_id, session)
                for repo_id, sessions in listed.items()
                for session in sessions
                if (status == "all" or session.status == status) and session.id not in hidden[repo_id]
            ),
            key=lambda item: (str(item[1].updated_at), str(item[1].id)),
            reverse=True,
        )
        # A query matches wire fields (title, location), so it needs every row.
        candidates = wanted if query or limit is None else wanted[:limit]
        grouped: dict[str, list[Any]] = {}
        for repo_id, session in candidates:
            grouped.setdefault(repo_id, []).append(session)
        built: dict[tuple[str, str], tuple[Any, ReviewListInputs, dict[str, Any]]] = {}
        checkout_metadata: dict[Path, dict[str, Any]] = {}
        for repo_id, sessions in grouped.items():
            for session, item, row in self._review_wire_rows(
                repo_id, sessions, references=references, location_metadata_cache=checkout_metadata
            ):
                built[(repo_id, str(session.id))] = (session, item, row)
        rows = [(repo_id, *built[(repo_id, str(session.id))]) for repo_id, session in candidates]
        if query:
            terms = query.split()
            rows = [
                entry
                for entry in rows
                if all(term in " ".join(str(entry[3].get(key) or "") for key in SEARCH_KEYS).lower() for term in terms)
            ]
        return rows if limit is None else rows[:limit]

    async def review_set_title(self, request: web.Request) -> web.StreamResponse:
        """Save a local display name; an empty name restores the current Snapshot title."""

        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        body = await self._body(request)
        title = body.get("title")
        if not isinstance(title, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "title must be a string")
        title = title.strip()
        if len(title) > 120 or any(ord(char) < 32 for char in title):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "title must be at most 120 characters on one line")

        def save() -> dict[str, Any]:
            _store, session = self.review_repository.require_review(repo_id, review_id)
            self.review_repository.set_manual_review_title(repo_id, session.id, title)
            return self.review_session_wire(repo_id, session)

        return json_response(await self._in_worker(save))

    async def review_branch_review(self, request: web.Request) -> web.StreamResponse:
        """``GET /api/branch-review``: the open Review a branch's captures continue, or ``null``.

        The same subject-keyed read hosted serves, with the repository named by
        ``repo_id``. A capture client asks before it freezes its range, so it can
        diff against the Review's recorded base. A local branch Review is keyed
        by repository and branch alone (one person's machine), as
        :meth:`LocalReviewRepository.capture` binds it.
        """

        self._principal(request)
        query = request.rel_url.query
        range_mode, branch = branch_review_query(query)
        repo_id = validate_opaque_id(query.get("repo_id", ""), "repo_id")

        def lookup() -> dict[str, Any] | None:
            repository = self.review_repository
            subject = review_subject(f"local:{repo_id}", branch=branch, worktree_key="", range_mode=range_mode)
            # A repository with no Review yet has no store, and a lookup creates none.
            if subject is None or subject.subject_kind != "branch" or not repository.store_root(repo_id).exists():
                return None
            store = repository.store(repo_id)
            binding = store.active_binding(
                repository_identity=subject.repository_identity,
                subject_kind=subject.subject_kind,
                subject_key=subject.subject_key,
            )
            session = None if binding is None else store.get_session(binding.review_id)
            if session is None or session.status != "open" or session.range_mode != range_mode:
                return None
            return branch_review_wire(session, repo_id)

        return json_response({"review": await self._in_worker(lookup)})

    # -- fix lines ------------------------------------------------------- #

    def _fix_scope(
        self,
        review_id: str,
        *,
        need_checkout: bool = True,
    ) -> tuple[str, ReviewStore, Any, Path | None]:
        resolved = self.review_reader.resolve(f"/api/reviews/{validate_opaque_id(review_id, 'review_id')}")
        session = resolved.store.get_session(resolved.review_id)
        if session is None:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the Review does not exist",
                details={"review_id": review_id},
            )
        root = self.review_repository.source_root_for(resolved.repo_id, session.id)
        if need_checkout and root is None:
            raise ServerError(
                ErrorCode.REVIEW_STATE_CONFLICT,
                "this Review has no trusted checkout on this machine; run lc review in it first",
                action=AgentAction.FIX_REQUEST,
            )
        return resolved.repo_id, resolved.store, session, root

    def _fix_line_wire(
        self,
        store: ReviewStore,
        line: fix_lines.FixLine,
        root: Path | None,
    ) -> dict[str, Any]:
        revisions = sorted(
            store.list_revisions(line.child_review_id),
            key=lambda item: item.revision_number,
        )
        child = store.get_session(line.child_review_id)
        unreviewed = 0
        if child is not None and revisions:
            latest = revisions[-1]
            frontier = compute_frontier(
                child.id,
                child.reviewer_id,
                latest,
                store.list_units(latest.id),
                store.list_marks(child.id, reviewer_id=child.reviewer_id),
            )
            unreviewed = sum(
                1
                for entry in frontier.entries
                if entry.state in {"unreviewed", "changed_since_review", "later"} or entry.changed_since_mark
            )
        checklist = fix_lines.fix_line_checklist(store, line, repo_root=root) if root is not None else ()
        return {
            "id": line.id,
            "review_id": line.parent_review_id,
            "snapshot_revision_id": line.snapshot_revision_id,
            "snapshot_number": line.snapshot_number,
            "child_review_id": line.child_review_id,
            "status": line.status,
            "worktree_path": line.worktree_path,
            "branch": line.branch,
            "base_commit": line.base_commit,
            "worktree_missing": line.worktree_missing,
            "applied_revision_id": line.applied_revision_id,
            "applied_files": list(line.applied_files),
            "applied_at": line.applied_at,
            "snapshots": [
                {
                    "id": item.id,
                    "fix_number": item.revision_number,
                    "display_number": item.revision_number - 1,
                    "created_at": item.created_at,
                }
                for item in revisions
            ],
            "checklist": [asdict(item) for item in checklist],
            "unreviewed": unreviewed,
        }

    async def review_fix_lines(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        return json_response(await self._in_worker(self._fix_lines_payload, request.match_info["review_id"]))

    def _fix_lines_payload(self, review_id: str) -> dict[str, Any]:
        repo_id, store, session, root = self._fix_scope(review_id, need_checkout=False)
        parent_line = fix_lines.fix_line_of_child(store, session.id)
        parent_root = (
            self.review_repository.source_root_for(repo_id, parent_line.parent_review_id) if parent_line else None
        )
        return {
            "fix_lines": [
                self._fix_line_wire(store, line, root) for line in fix_lines.list_fix_lines(store, session.id)
            ],
            "fix_of": (self._fix_line_wire(store, parent_line, parent_root) if parent_line else None),
        }

    async def review_start_fix_line(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        return json_response(
            await self._in_worker(
                self._start_fix_line,
                request.match_info["review_id"],
                request.match_info["revision_id"],
            )
        )

    def _start_fix_line(self, review_id: str, revision_id: str) -> dict[str, Any]:
        repo_id, store, session, root = self._fix_scope(review_id)
        assert root is not None
        snapshot = store.get_revision(validate_opaque_id(revision_id, "revision_id"))
        if snapshot is None or snapshot.review_id != session.id:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the revision is not part of this Review",
            )
        with _fix_line_errors():
            line, created = fix_lines.start_fix_line(
                store,
                session,
                snapshot,
                repo_root=root,
                repository_identity=f"local:{repo_id}",
                review_short=self.review_repository.short_review_reference(session.id),
                snapshot_number=_public_snapshot_number(snapshot),
            )
        self.review_repository.set_review_checkout_root(repo_id, session.id, root)
        self.review_repository.set_review_checkout_root(repo_id, line.child_review_id, Path(line.worktree_path))
        self.review_repository.set_workspace_root_hint(repo_id, line.child_review_id, line.worktree_path)
        self.review_repository.set_workspace_branch_hint(repo_id, line.child_review_id, line.branch)
        return {
            "fix_line": self._fix_line_wire(store, line, root),
            "created": created,
        }

    def _fix_target(self, review_id: str, fix_id: str) -> tuple[ReviewStore, Any, fix_lines.FixLine, Path]:
        _repo_id, store, session, root = self._fix_scope(review_id)
        assert root is not None
        with _fix_line_errors():
            line = fix_lines.get_fix_line(
                store,
                session.id,
                validate_opaque_id(fix_id, "fix_id"),
            )
        return store, session, line, root

    @staticmethod
    def _plan_wire(
        plan: fix_lines.FixApplyPlan,
        wire: Mapping[str, Any],
    ) -> dict[str, Any]:
        warnings = list(plan.warnings)
        unreviewed = int(wire["unreviewed"] or 0)
        if unreviewed:
            warnings.append(
                f"{part_count(unreviewed)} in the fix {'is' if unreviewed == 1 else 'are'} not reviewed yet"
            )
        still_open = sum(1 for item in wire["checklist"] if item["state"] != "resolved")
        if still_open:
            comments = "1 comment" if still_open == 1 else f"{still_open} comments"
            verb = "is" if still_open == 1 else "are"
            warnings.append(f"{comments} on {revision_name(wire['snapshot_number'])} {verb} still open")
        return {
            "fix_line_id": plan.fix_line_id,
            "revision_id": plan.revision_id,
            "fix_number": plan.fix_number,
            "files": [
                {
                    "path": item.path,
                    "outcome": item.outcome,
                    "reason": item.reason,
                }
                for item in plan.files
            ],
            "conflicts": [item.path for item in plan.conflicts],
            "warnings": warnings,
        }

    async def review_fix_apply_plan(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        review_id = request.match_info["review_id"]
        fix_id = request.match_info["fix_id"]

        def run() -> dict[str, Any]:
            store, session, line, root = self._fix_target(review_id, fix_id)
            with _fix_line_errors():
                fix_lines.recover_apply_journal(root, line)
                plan = fix_lines.plan_fix_apply(store, session, line, repo_root=root)
            return self._plan_wire(plan, self._fix_line_wire(store, line, root))

        return json_response(await self._in_worker(run))

    async def review_fix_apply(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        review_id = request.match_info["review_id"]
        fix_id = request.match_info["fix_id"]
        body = await self._body(request)
        markers = body.get("with_conflict_markers", False)
        if not isinstance(markers, bool):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "with_conflict_markers must be a boolean",
            )

        def run() -> dict[str, Any]:
            store, session, line, root = self._fix_target(review_id, fix_id)
            with _fix_line_errors():
                result = fix_lines.apply_fix_line(
                    store,
                    session,
                    line,
                    repo_root=root,
                    with_conflict_markers=markers,
                )
            wire = self._fix_line_wire(store, result.line, root)
            return {
                "applied": result.applied,
                "fix_line": wire,
                "plan": self._plan_wire(result.plan, wire),
            }

        return json_response(await self._in_worker(run))

    async def _close_fix(self, request: web.Request, action: str) -> web.StreamResponse:
        self._principal(request)
        review_id = request.match_info["review_id"]
        fix_id = request.match_info["fix_id"]
        body = await self._body(request)
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "force must be a boolean")

        def run() -> dict[str, Any]:
            store, _session, line, root = self._fix_target(review_id, fix_id)
            with _fix_line_errors():
                try:
                    if action == "resolved":
                        closed = fix_lines.resolve_fix_line(store, line, repo_root=root, force=force)
                    else:
                        closed = fix_lines.close_fix_line(
                            store,
                            line,
                            repo_root=root,
                            outcome="abandoned",
                            force=force,
                        )
                except fix_lines.FixLineDirty as dirty:
                    return {
                        "closed": False,
                        "dirty_files": list(dirty.files),
                        "fix_line": self._fix_line_wire(store, line, root),
                    }
            return {
                "closed": True,
                "dirty_files": [],
                "fix_line": self._fix_line_wire(store, closed, root),
            }

        return json_response(await self._in_worker(run))

    async def review_fix_resolved(self, request: web.Request) -> web.StreamResponse:
        return await self._close_fix(request, "resolved")

    async def review_fix_discard(self, request: web.Request) -> web.StreamResponse:
        return await self._close_fix(request, "discard")

    async def review_fix_recreate(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        review_id = request.match_info["review_id"]
        fix_id = request.match_info["fix_id"]

        def run() -> dict[str, Any]:
            store, _session, line, root = self._fix_target(review_id, fix_id)
            with _fix_line_errors():
                recreated = fix_lines.recreate_fix_worktree(store, line, repo_root=root)
            repo_id, _store, _session2, _root = self._fix_scope(review_id)
            self.review_repository.set_review_checkout_root(
                repo_id,
                recreated.child_review_id,
                Path(recreated.worktree_path),
            )
            return {"fix_line": self._fix_line_wire(store, recreated, root)}

        return json_response(await self._in_worker(run))

    async def review_list(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        return json_response({"reviews": await self._in_worker(self._review_list_rows, repo_id)})

    def _review_list_rows(self, repo_id: str) -> list[dict[str, Any]]:
        hidden = fix_lines.fix_child_review_ids(self.review_repository.store(repo_id))
        sessions = tuple(
            session for session in self.review_repository.list_reviews(repo_id, limit=200) if session.id not in hidden
        )
        references = self.review_repository.review_references()
        return [row for _session, _item, row in self._review_wire_rows(repo_id, sessions, references=references)]

    async def review_get(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        return json_response(await self._in_worker(self._review_get_payload, repo_id, review_id))

    def _review_get_payload(self, repo_id: str, review_id: str) -> dict[str, Any]:
        store, session = self.review_repository.require_review(repo_id, review_id)
        revision = store.latest_revision(session.id)
        return {
            "review": self.review_session_wire(repo_id, session),
            "change": change_context_payload(store, session),
            "revision": (
                None
                if revision is None
                else {
                    "id": revision.id,
                    "object": "snapshot",
                    "snapshot_ref": f"rr/{revision.id}",
                    "revision_number": _public_snapshot_number(revision),
                    "base_sha": revision.base_sha,
                    "head_sha": revision.head_sha,
                    "merge_base_sha": revision.merge_base_sha,
                    "source_fingerprint": revision.source_fingerprint,
                    "title": revision.title,
                    "dirty": revision.dirty,
                    "created_at": revision.created_at,
                }
            ),
            "revision_count": len(store.list_revisions(session.id)),
        }

    async def review_enrich_projection(self, request: web.Request) -> web.StreamResponse:
        """Identity-safe metadata enrichment for an already-visible revision."""

        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        revision_id = validate_opaque_id(request.match_info["revision_id"], "revision_id")
        body = await self._body(request)
        packet = require_object(body, "packet")
        require_packet_schema(packet)
        revision = await self._in_worker(
            lambda: self.review_repository.enrich_projection(
                repo_id=repo_id,
                review_id=review_id,
                revision_id=revision_id,
                packet=packet,
            )
        )
        self._schedule_warm(repo_id, review_id, "enriched")
        return json_response(
            {
                "review_id": review_id,
                "revision_id": revision.id,
                "revision_number": _public_snapshot_number(revision),
                "enriched": True,
            }
        )

    async def review_update_preparation(self, request: web.Request) -> web.StreamResponse:
        """Publish source-first preparation state for the waiting Reader."""

        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        body = await self._body(request)
        preparation_id = require_str(body, "preparation_id", max_len=128).strip()
        stage = require_str(body, "state", max_len=64).strip()
        detail_raw = body.get("detail", "")
        if not isinstance(detail_raw, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "detail must be a string")
        await self._in_worker(
            lambda: self.review_repository.update_preparation(
                repo_id=repo_id,
                review_id=review_id,
                preparation_id=preparation_id,
                stage=stage,
                detail=detail_raw,
            )
        )
        if stage == "ready":
            self._schedule_warm(repo_id, review_id, "ready")
        return json_response({"review_id": review_id, "preparation_id": preparation_id, "state": stage})

    async def review_intelligence(self, request: web.Request) -> web.StreamResponse:
        """This machine's effective AI route and background envelope (UX0a)."""

        from lemoncrow.pro.capabilities.review.background_local import local_intelligence_settings

        self._principal(request)
        return json_response(await self._in_worker(local_intelligence_settings))

    async def review_intelligence_settings_update(self, request: web.Request) -> web.StreamResponse:
        """Save a narrow, validated set of machine-local AI preferences."""

        from lemoncrow.pro.capabilities.review.background_local import (
            local_intelligence_settings,
            update_local_intelligence_settings,
        )

        self._principal(request)
        body = await self._body(request)
        try:
            await self._in_worker(update_local_intelligence_settings, body)
        except ValueError as exc:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), action=AgentAction.FIX_REQUEST) from exc
        response = json_response(await self._in_worker(local_intelligence_settings))
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _user_settings_wire(stored: Mapping[str, Any]) -> web.StreamResponse:
        from lemoncrow.pro.capabilities.review.user_settings import effective

        response = json_response({"settings": effective(stored), "set": sorted(stored)})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def user_settings_get(self, request: web.Request) -> web.StreamResponse:
        """The reviewer's personal settings, defaults filled in, and which keys they set (SPEC 4.11)."""

        self._principal(request)
        store = self.review_repository.user_store()
        return self._user_settings_wire(await self._in_worker(store.get_user_settings, _USER_SETTINGS_OWNER))

    async def user_settings_patch(self, request: web.Request) -> web.StreamResponse:
        """Validate and save a change; a null value forgets the key. A taken key is a 422 with the sentence to show."""

        from lemoncrow.pro.capabilities.review.user_settings import UserSettingsError

        self._principal(request)
        body = await self._body(request)
        store = self.review_repository.user_store()
        try:
            stored = await self._in_worker(store.patch_user_settings, _USER_SETTINGS_OWNER, body)
        except UserSettingsError as exc:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), action=AgentAction.FIX_REQUEST) from exc
        return self._user_settings_wire(stored)

    async def user_settings_reset(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        store = self.review_repository.user_store()
        await self._in_worker(store.reset_user_settings, _USER_SETTINGS_OWNER)
        return self._user_settings_wire({})

    async def review_api_directory(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        rows = await self._in_worker(self.review_rows)
        return json_response({"reviews": rows, "repo_root": "", "status_filter": "all"})
