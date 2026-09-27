"""Server-backed Review client bridge for ``lc review``.

Local and hosted installs deliberately use this exact path. The topology input
is only :class:`lemoncrow_client.config.ClientConfig`: capture builds the same
deterministic ReviewPacket, synchronizes the source View through the audited
thin client, and publishes into server-side Review storage. It never binds a
listener or starts a Review child process.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.liveview import load_live_view_binding
from lemoncrow_client.session import RemoteSession, workspace_state_fingerprint

from .gitdiff import BlobPair, RevRange, _open_repo, source_state
from .live_runtime import (
    LiveReviewBinding,
    _write_live_review_binding_unlocked,
    live_review_publication_guard,
    write_live_review_binding,
)
from .models import review_packet_from_dict
from .packet import (
    PacketBuild,
    attach_changed_symbols,
    attach_impact,
    attach_provenance,
    build_review_packet_with_blobs,
)
from .snapshot import materialize_git_revision
from .sources.local import source_ref


def _new_side_mode(repo_root: Path, rng: RevRange, path: str, *, fallback: int = 0o100644) -> int:
    """Best available executable-bit projection for a frozen dirty/staged file."""

    if rng.mode == "staged":
        try:
            entry = _open_repo(repo_root).index[path]
            return 0o100755 if int(entry.mode) & 0o111 else 0o100644
        except (KeyError, OSError, RuntimeError, ValueError):
            return fallback
    try:
        mode = (repo_root / path).stat().st_mode
    except OSError:
        return fallback
    return 0o100755 if mode & stat.S_IXUSR else 0o100644


def _remove_snapshot_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _materialize_capture_old_side(repo_root: Path, rng: RevRange, target: Path) -> None:
    """Freeze the complete exact base side used for hosted Compare/runtime execution."""

    target.mkdir(parents=True, exist_ok=True)
    if not rng.base_sha:
        # Root-commit reviews compare against Git's empty tree.
        return
    materialize_git_revision(repo_root, rng.base_sha, target)


def _materialize_capture_new_side(repo_root: Path, rng: RevRange, build: PacketBuild, target: Path) -> None:
    """Freeze the complete exact new side used for a hosted Review capture."""

    if rng.mode == "commit_range":
        if not rng.head_sha:
            raise ValueError("hosted Review commit range has no head revision")
        materialize_git_revision(repo_root, rng.head_sha, target)
        return
    if rng.mode not in {"working_tree", "staged"} or not rng.base_sha:
        raise ValueError("hosted Review source cannot be materialized exactly")

    materialize_git_revision(repo_root, rng.base_sha, target)
    root = target.resolve()
    for changed in build.packet.files:
        if changed.submodule_pointer is not None:
            # Gitlinks have no file bytes in the parent repository. They remain
            # represented by the Review packet; nested source needs its own
            # frozen submodule artifact/runtime support.
            continue
        old_rel = changed.old_path if changed.status == "renamed" and changed.old_path else ""
        old_mode = 0o100644
        if old_rel:
            old_target = (target / old_rel).resolve()
            if old_target == root or root in old_target.parents:
                try:
                    old_mode = 0o100755 if old_target.stat().st_mode & stat.S_IXUSR else 0o100644
                except OSError:
                    pass
                _remove_snapshot_path(old_target)
        destination = (target / changed.path).resolve()
        if destination != root and root not in destination.parents:
            raise ValueError(f"review snapshot path escapes repository: {changed.path}")
        if changed.status == "deleted":
            _remove_snapshot_path(destination)
            continue
        if changed.is_binary:
            payload = build.blobs.binary_new.get(changed.path)
        else:
            text = build.blobs.new.get(changed.path)
            payload = None if text is None else text.encode("utf-8")
        if payload is None:
            raise ValueError(
                f"hosted Review cannot freeze changed file exactly because its bytes were not captured: {changed.path}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        mode = _new_side_mode(repo_root, rng, changed.path, fallback=old_mode)
        destination.chmod(0o755 if mode & 0o111 else 0o644)


def _workspace_branch(repo_root: Path) -> str:
    """Best-effort human branch label for local Review identity."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    branch = result.stdout.strip()
    return branch if branch and len(branch) <= 256 and "\n" not in branch and "\r" not in branch else ""


def _worktree_holds_packet_bytes(repo_root: Path, build: PacketBuild) -> bool:
    """True when every changed file the packet captured still has those bytes on disk.

    Files whose bytes the packet did not capture (over-limit, unreadable,
    gitlinks) are not compared: the packet already marks them degraded, and
    the snapshot path cannot freeze them exactly either.
    """

    for changed in build.packet.files:
        if changed.submodule_pointer is not None:
            continue
        path = repo_root / changed.path
        if changed.status == "deleted":
            if path.exists() or path.is_symlink():
                return False
            continue
        if changed.is_binary:
            expected = build.blobs.binary_new.get(changed.path)
        else:
            text = build.blobs.new.get(changed.path)
            expected = None if text is None else text.encode("utf-8")
        if expected is None:
            continue
        try:
            actual = path.read_bytes()
        except OSError:
            return False
        if not changed.is_binary:
            actual = actual.decode("utf-8", errors="replace").encode("utf-8")
        if actual != expected:
            return False
    return True


CaptureProgress = Callable[[str, str], None]
CaptureReady = Callable[["ServerCapture"], None]


@dataclass(frozen=True, slots=True)
class ServerCapture:
    build: PacketBuild
    repo_id: str
    review_id: str
    review_url: str
    response: dict[str, Any]
    preparation_id: str = ""

    @property
    def canonical_url(self) -> str:
        """The stable Review link to print or share.

        ``review_url`` may carry the tab-scoped ``p=<preparation>&sr=1`` hint
        that only bootstraps the browser this capture opens. It differs on
        every capture, so terminal output must not repeat it.
        """

        if self.preparation_id:
            for separator in ("#", "&"):
                hint = f"{separator}p={self.preparation_id}&sr=1"
                if self.review_url.endswith(hint):
                    return self.review_url[: -len(hint)]
        return self.review_url


def _finalize_job_dir(store_root: Path) -> Path:
    root = Path(store_root).expanduser().resolve() / "review" / "finalize"
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


def _write_local_finalize_job(
    *,
    store_root: Path,
    repo_root: Path,
    rng: RevRange,
    build: PacketBuild,
    repo_id: str,
    review_id: str,
    revision_id: str,
    revision_number: int,
    review_short_id: str,
    revision_short_id: str,
    preparation_id: str,
    captured_source: str,
    session_id: str | None,
    session_id_is_hint: bool,
    limit: int,
    with_impact: bool,
    with_provenance: bool,
    advance_claimed_binding: bool,
) -> Path:
    payload = {
        "repo_root": str(repo_root),
        "store_root": str(store_root),
        "rng": {
            "mode": rng.mode,
            "base_rev": rng.base_rev,
            "head_rev": rng.head_rev,
            "base_sha": rng.base_sha,
            "head_sha": rng.head_sha,
            "merge_base_sha": rng.merge_base_sha,
            "dirty": rng.dirty,
            "title": rng.title,
            "submodule_dirt_discounted": rng.submodule_dirt_discounted,
        },
        "packet": build.packet.to_dict(),
        "blobs": {
            "old": build.blobs.old,
            "new": build.blobs.new,
            "degraded": list(build.blobs.degraded),
        },
        "source_fingerprint": build.source_fingerprint,
        "repo_id": repo_id,
        "review_id": review_id,
        "revision_id": revision_id,
        "revision_number": int(revision_number),
        "review_short_id": review_short_id,
        "revision_short_id": revision_short_id,
        "preparation_id": preparation_id,
        "captured_source": captured_source,
        "session_id": session_id or "",
        "session_id_is_hint": bool(session_id_is_hint),
        "limit": int(limit),
        "with_impact": bool(with_impact),
        "with_provenance": bool(with_provenance),
        "advance_claimed_binding": bool(advance_claimed_binding),
    }
    path = _finalize_job_dir(store_root) / f"{uuid.uuid4().hex}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _launch_local_finalize_job(job_path: Path) -> bool:
    try:
        subprocess.Popen(
            [sys.executable, "-m", "lemoncrow.pro.capabilities.review.hosted", "--finalize-job", str(job_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _run_local_finalize_job(job_path: Path) -> int:
    from lemoncrow_client.config import load_config

    try:
        with gzip.open(job_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        repo_root = Path(str(payload["repo_root"])).expanduser().resolve()
        store_root = Path(str(payload["store_root"])).expanduser().resolve()
        rng = RevRange(**dict(payload["rng"]))
        packet = review_packet_from_dict(dict(payload["packet"]))
        blobs_raw = dict(payload.get("blobs") or {})
        build = PacketBuild(
            packet=packet,
            blobs=BlobPair(
                old=dict(blobs_raw.get("old") or {}),
                new=dict(blobs_raw.get("new") or {}),
                degraded=tuple(str(item) for item in (blobs_raw.get("degraded") or ())),
            ),
            source_fingerprint=str(payload.get("source_fingerprint") or ""),
        )
        config = load_config(cwd=repo_root)
        remote = RemoteSession(config, publish_live_view=False)
        repo_id = str(payload["repo_id"])
        review_id = str(payload["review_id"])
        revision_id = str(payload["revision_id"])
        revision_number = int(payload.get("revision_number") or 0)
        preparation_id = str(payload.get("preparation_id") or "")
        captured_source = str(payload.get("captured_source") or "")
        review_short_id = str(payload.get("review_short_id") or "")
        revision_short_id = str(payload.get("revision_short_id") or "")
        with_impact = bool(payload.get("with_impact"))
        with_provenance = bool(payload.get("with_provenance"))
        session_id = str(payload.get("session_id") or "") or None
        session_id_is_hint = bool(payload.get("session_id_is_hint"))
        limit = int(payload.get("limit") or 40)
        advance_claimed_binding = bool(payload.get("advance_claimed_binding"))
        enrichment_superseded = False
        try:
            remote.handshake(timeout_s=config.request_timeout_s)
            remote.open_session(timeout_s=config.request_timeout_s)
            if preparation_id and (with_impact or with_provenance):
                try:
                    current = source_state(repo_root, rng).fingerprint
                    if captured_source and current != captured_source:
                        enrichment_superseded = True
                        raise ValueError("source changed before background Review enrichment")
                    enriched = build
                    if with_impact:
                        enriched = attach_impact(repo_root, rng, enriched, limit=limit)
                    if with_provenance:
                        enriched = attach_provenance(
                            store_root,
                            repo_root,
                            rng,
                            enriched,
                            session_id=session_id,
                            session_id_is_hint=session_id_is_hint,
                        )
                    current = source_state(repo_root, rng).fingerprint
                    if captured_source and current != captured_source:
                        enrichment_superseded = True
                        raise ValueError("source changed during background Review enrichment")
                    remote.update_review_preparation(
                        repo_id=repo_id,
                        review_id=review_id,
                        preparation_id=preparation_id,
                        state="finalizing_background",
                        detail="applying impact, provenance, ranking, and agent context",
                        timeout_s=config.request_timeout_s,
                    )
                    remote.enrich_review_projection(
                        repo_id=repo_id,
                        review_id=review_id,
                        revision_id=revision_id,
                        packet=enriched.packet.to_dict(),
                        timeout_s=config.tool_timeout_s,
                    )
                    if enriched.packet.provenance.certainty == "exact":
                        with contextlib.suppress(OSError, TypeError, ValueError):
                            write_live_review_binding(
                                store_root,
                                repo_root,
                                LiveReviewBinding(
                                    server_url=config.url,
                                    repo_id=repo_id,
                                    review_id=review_id,
                                    revision_id=revision_id,
                                    revision_number=revision_number,
                                    source_fingerprint=captured_source,
                                    author_host=enriched.packet.provenance.host or "",
                                    author_session_id=enriched.packet.provenance.session_id or "",
                                    review_short_id=review_short_id,
                                    revision_short_id=revision_short_id,
                                ),
                                allow_advance_claimed=advance_claimed_binding,
                            )
                except (ClientError, OSError, RuntimeError, TypeError, ValueError):
                    pass
                finally:
                    with contextlib.suppress(ClientError):
                        remote.update_review_preparation(
                            repo_id=repo_id,
                            review_id=review_id,
                            preparation_id=preparation_id,
                            state="ready",
                            detail=(
                                "source is ready; enrichment was superseded by a newer live generation"
                                if enrichment_superseded
                                else "source and available review context are ready"
                            ),
                            timeout_s=config.request_timeout_s,
                        )

            with tempfile.TemporaryDirectory(prefix="lemoncrow-review-base-") as base_temporary:
                base_root = Path(base_temporary)
                _materialize_capture_old_side(repo_root, rng, base_root)
                base_opened = remote.open_review_snapshot(
                    base_root,
                    source_revision=rng.base_sha or "review-empty-base",
                    timeout_s=config.tool_timeout_s,
                    allow_oversize=True,
                )
                base_view_id = str(base_opened.get("view_id") or "")
                base_view_revision = int(base_opened.get("view_revision") or 0)
                if base_view_id:
                    remote.attach_review_base(
                        repo_id=repo_id,
                        review_id=review_id,
                        revision_id=revision_id,
                        base_view_id=base_view_id,
                        base_view_revision=base_view_revision,
                        timeout_s=config.tool_timeout_s,
                    )
            return 0
        finally:
            remote.close()
    except Exception:
        return 1
    finally:
        with contextlib.suppress(OSError):
            job_path.unlink(missing_ok=True)


def capture_server_review(
    config: ClientConfig,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None,
    session_id_is_hint: bool = False,
    limit: int,
    with_impact: bool,
    with_provenance: bool,
    restore_archived: bool = False,
    progress: CaptureProgress | None = None,
    ready: CaptureReady | None = None,
    source_ref_override: str = "",
    title_override: str = "",
    advance_claimed_binding: bool = False,
    respect_human_claim: bool = False,
    superseded: Callable[[], bool] | None = None,
    defer_post_ready: bool = False,
) -> ServerCapture:
    """Capture *rng* into the configured LemonCrow server without binding locally.

    ``source_ref_override`` names the durable Review explicitly (a GitHub pull
    request is ``github:owner/repo#N`` whatever its head SHA), and
    ``title_override`` titles a Review this capture creates.
    """

    def report(stage: str, detail: str = "") -> None:
        if progress is not None:
            progress(stage, detail)

    def is_superseded() -> bool:
        try:
            return bool(superseded is not None and superseded())
        except (OSError, TypeError, ValueError):
            return False

    packet_stage: str | None = None
    packet_start = {
        "diff": "Reading changed files and diff",
        "source": "Loading exact old/new source snapshots",
        "impact": "Analyzing symbols, callers, and change impact",
        "provenance": "Correlating the authoring agent session",
        "ranking": "Ranking the human review order",
    }
    packet_done = {
        "diff": "Diff captured",
        "source": "Source snapshots loaded",
        "impact": "Impact analysis complete",
        "provenance": "Author provenance checked",
        "ranking": "Review order ready",
    }

    def packet_progress(stage: str) -> None:
        nonlocal packet_stage
        if packet_stage is not None:
            report(f"packet_{packet_stage}_done", packet_done.get(packet_stage, packet_stage))
        packet_stage = stage
        report(f"packet_{stage}", packet_start.get(stage, stage))

    local_working_tree = bool(not config.hosted and rng.mode == "working_tree")
    progressive_local = bool(local_working_tree and (with_impact or with_provenance))

    # First-open latency is the max of source capture and View sync, not their
    # sum. They are independent until the exact packet bytes are pinned below.
    # A matching live View is already cheaper than a new sync, so preserve that
    # path and parallelize only the cold/stale case.
    remote = RemoteSession(config, publish_live_view=False)
    parallel_pool: ThreadPoolExecutor | None = None
    parallel_open: Any = None
    observed_workspace = ""
    live_binding = None
    reused_live_view = False
    if not config.hosted and rng.mode == "working_tree":
        observed_workspace = workspace_state_fingerprint(repo_root)
        live_binding = load_live_view_binding(
            config.state_dir,
            repo_root,
            server_url=config.url,
        )
        reused_live_view = bool(
            live_binding and observed_workspace and live_binding.workspace_fingerprint == observed_workspace
        )
        if not reused_live_view:
            parallel_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-review-view")

            def prepare_source_view() -> dict[str, Any]:
                # A freshly started local server may still be warming its native
                # index when the first Review arrives. Treat the whole cold View
                # preparation as one tool operation; the generic 10s request
                # budget is appropriate for steady-state API calls, not cold
                # bootstrap.
                remote.handshake(timeout_s=config.tool_timeout_s)
                remote.open_session(timeout_s=config.tool_timeout_s)
                return dict(remote.open_view(timeout_s=config.tool_timeout_s))

            parallel_open = parallel_pool.submit(prepare_source_view)

    try:
        build = build_review_packet_with_blobs(
            repo_root,
            rng,
            store_root=store_root,
            # Local working-tree Review establishes exact source/target identity
            # first. Caller fan-out and author correlation enrich that same visible
            # revision after Reader opens. Conservative capture modes remain whole.
            with_impact=False if progressive_local else with_impact,
            with_provenance=False if progressive_local else with_provenance,
            session_id=session_id,
            session_id_is_hint=session_id_is_hint,
            limit=limit,
            with_patch_text=True,
            unbounded_patch_text=True,
            progress=packet_progress,
        )
        # Stable symbol targets are part of Snapshot identity, not optional impact
        # enrichment. Establish them for every local working-tree publication so
        # a later normal Review can enrich a Snapshot first opened with
        # --no-impact/--no-provenance without adding visible targets.
        if local_working_tree:
            report("packet_symbols", "Identifying changed symbols for stable Review targets")
            build = attach_changed_symbols(repo_root, build, limit=limit)
            report("packet_symbols_done", "Stable Review targets ready")
        if packet_stage is not None:
            report(f"packet_{packet_stage}_done", packet_done.get(packet_stage, packet_stage))
        packet = build.packet
        report(
            "packet_ready_done",
            f"Change packet ready · {len(packet.files)} file{'s' if len(packet.files) != 1 else ''}",
        )
        stable_source_ref = source_ref_override or source_ref(rng, repo_root=repo_root) or f"client:{rng.mode}"
        review_title = title_override or packet.title
        workspace_branch = _workspace_branch(repo_root) if not config.hosted else ""

        # An adopted View belongs to the authoring process. Its client_seq is
        # private to that process, so Review must only read it. If the checkout
        # moved while the packet was built, open our own View and pin the bytes
        # already captured by the packet there.
        if reused_live_view and (
            workspace_state_fingerprint(repo_root) != observed_workspace
            or not _worktree_holds_packet_bytes(repo_root, build)
        ):
            reused_live_view = False

        report("connect", "Connecting to Review server")
        if reused_live_view or config.hosted or rng.mode != "working_tree":
            remote.handshake(timeout_s=config.request_timeout_s)
            remote.open_session(timeout_s=config.request_timeout_s)
            report("connect_done", "Review server ready")

        # Local working-tree Review already has the exact current checkout on
        # the same machine. Synchronize a View and publish readable diffs from
        # it directly. Oversized files stay in the manifest with an explicit
        # diff omission; execution still requires every source blob.
        # The server snapshots the View membership into the immutable Review, so
        # later edits cannot mutate what the reviewer was shown. The complete
        # base tree is attached *after* publication; Reader can render the packet
        # immediately while Compare/runtime materialization finishes.
        if not config.hosted and rng.mode == "working_tree":
            captured_source = build.source_fingerprint or source_state(repo_root, rng).fingerprint
            if reused_live_view and live_binding is not None:
                report("sync_current", "Reusing live authoring source")
                remote.adopt_live_view(live_binding)
                repo_id = live_binding.repo_id
                report("sync_current_done", "Live authoring source reused")
            else:
                report("sync_current", "Synchronizing current source in parallel")
                opened = (
                    parallel_open.result()
                    if parallel_open is not None
                    else remote.open_view(timeout_s=config.tool_timeout_s)
                )
                report("connect_done", "Review server ready")
                repo_id_raw = opened.get("repo_id")
                if not isinstance(repo_id_raw, str) or not repo_id_raw:
                    raise ValueError("LemonCrow server opened a source View without a repository id")
                repo_id = repo_id_raw
                report("sync_current_done", "Current source synchronized")

            deleted_paths = tuple(
                dict.fromkeys(
                    path
                    for changed in packet.files
                    for path in (
                        (changed.path,)
                        if changed.status == "deleted"
                        else ((changed.old_path,) if changed.status == "renamed" and changed.old_path else ())
                    )
                )
            )
            if not reused_live_view:
                remote.pin_review_snapshot(
                    build.blobs.new,
                    binary_new_blobs=build.blobs.binary_new,
                    deleted_paths=deleted_paths,
                )

            report("publish", "Publishing reviewable source revision")
            publication_guard = (
                live_review_publication_guard(store_root, repo_root)
                if respect_human_claim
                else contextlib.nullcontext()
            )
            with publication_guard:
                try:
                    raw = remote.capture_review(
                        repo_id=repo_id,
                        source_ref=stable_source_ref,
                        title=review_title,
                        packet=packet.to_dict(),
                        new_blobs={},
                        restore_archived=restore_archived,
                        source_fingerprint=captured_source,
                        workspace_branch=workspace_branch,
                        timeout_s=config.tool_timeout_s,
                    )
                except ClientError as exc:
                    if not reused_live_view or exc.code not in {ErrorCode.VIEW_UNKNOWN, ErrorCode.VIEW_REVISION_STALE}:
                        raise
                    # The plugin lease outlived the server View (restart/lease eviction).
                    # Re-sync once and retry; source identity is checked again before
                    # publishing so fallback cannot silently capture newer bytes.
                    report("sync_current", "Live authoring source expired; synchronizing current source")
                    opened = remote.open_view(timeout_s=config.tool_timeout_s)
                    repo_id_raw = opened.get("repo_id")
                    if not isinstance(repo_id_raw, str) or not repo_id_raw:
                        raise ValueError("LemonCrow server opened a source View without a repository id") from exc
                    repo_id = repo_id_raw
                    remote.pin_review_snapshot(
                        build.blobs.new,
                        binary_new_blobs=build.blobs.binary_new,
                        deleted_paths=deleted_paths,
                    )
                    report("sync_current_done", "Current source synchronized and Snapshot pinned")
                    raw = remote.capture_review(
                        repo_id=repo_id,
                        source_ref=stable_source_ref,
                        title=review_title,
                        packet=packet.to_dict(),
                        new_blobs={},
                        restore_archived=restore_archived,
                        source_fingerprint=captured_source,
                        workspace_branch=workspace_branch,
                        timeout_s=config.tool_timeout_s,
                    )
                response = dict(raw)
                review = response.get("review")
                revision = response.get("revision")
                if not isinstance(review, dict) or not isinstance(review.get("id"), str) or not review.get("id"):
                    raise ValueError("LemonCrow server captured the diff without returning a Review id")
                if not isinstance(revision, dict) or not isinstance(revision.get("id"), str) or not revision.get("id"):
                    raise ValueError("LemonCrow server captured the diff without returning a Review revision id")
                review_id = str(review["id"])
                review_short_id = str(review.get("short_id") or "")
                revision_id = str(revision["id"])
                revision_short_id = str(revision.get("short_id") or "")
                revision_number = revision.get("revision_number")
                review_path = review.get("review_path")
                review_url = (
                    config.endpoint(review_path)
                    if isinstance(review_path, str) and review_path.startswith("/r/")
                    else remote.review_url(str(review.get("ref") or f"r/{review_id}"))
                )
                preparation_id = ""
                if progressive_local:
                    preparation_id = f"prep-{uuid.uuid4().hex}"
                    remote.update_review_preparation(
                        repo_id=str(repo_id),
                        review_id=review_id,
                        preparation_id=preparation_id,
                        state="impact_background",
                        detail="exact source and Review targets are ready; analysis continues",
                        timeout_s=config.request_timeout_s,
                    )
                    separator = "&" if "#" in review_url else "#"
                    review_url = f"{review_url}{separator}p={preparation_id}&sr=1"
                captured = ServerCapture(
                    build=build,
                    repo_id=str(repo_id),
                    review_id=review_id,
                    review_url=review_url,
                    response=response,
                    preparation_id=preparation_id,
                )
                report(
                    "publish_done",
                    (
                        f"Snapshot {revision_number} ready for reading"
                        if isinstance(revision_number, int)
                        else "Snapshot ready for reading"
                    ),
                )
                # Publish the reusable Review only after both the packet-build gate
                # and server View validation proved this exact source generation.
                try:
                    binding_writer = (
                        _write_live_review_binding_unlocked if respect_human_claim else write_live_review_binding
                    )
                    binding_writer(
                        store_root,
                        repo_root,
                        LiveReviewBinding(
                            server_url=config.url,
                            repo_id=str(repo_id),
                            review_id=review_id,
                            revision_id=revision_id,
                            revision_number=revision_number if isinstance(revision_number, int) else 0,
                            source_fingerprint=captured_source,
                            author_host=build.packet.provenance.host or "",
                            review_short_id=review_short_id,
                            revision_short_id=revision_short_id,
                            author_session_id=(
                                build.packet.provenance.session_id or ""
                                if session_id_is_hint
                                else session_id or build.packet.provenance.session_id or ""
                            ),
                        ),
                        allow_advance_claimed=advance_claimed_binding,
                    )
                except (OSError, TypeError, ValueError):
                    pass
            if ready is not None:
                ready(captured)

            if defer_post_ready and local_working_tree:
                job_path = _write_local_finalize_job(
                    store_root=store_root,
                    repo_root=repo_root,
                    rng=rng,
                    build=build,
                    repo_id=str(repo_id),
                    review_id=review_id,
                    revision_id=revision_id,
                    revision_number=revision_number if isinstance(revision_number, int) else 0,
                    review_short_id=review_short_id,
                    revision_short_id=revision_short_id,
                    preparation_id=preparation_id,
                    captured_source=captured_source,
                    session_id=session_id,
                    session_id_is_hint=session_id_is_hint,
                    limit=limit,
                    with_impact=with_impact,
                    with_provenance=with_provenance,
                    advance_claimed_binding=advance_claimed_binding,
                )
                if _launch_local_finalize_job(job_path):
                    report("background_handoff_done", "Remaining Review context continues in background")
                    return captured
                with contextlib.suppress(OSError):
                    job_path.unlink(missing_ok=True)

            if progressive_local and preparation_id:
                enrichment_superseded = False
                try:
                    analysis_before = source_state(repo_root, rng).fingerprint
                    if captured_source and analysis_before != captured_source:
                        raise ValueError("source changed before background Review enrichment")
                    if is_superseded():
                        enrichment_superseded = True
                        raise ValueError("newer live Review generation superseded background enrichment")
                    enriched = build
                    if with_impact:
                        report("packet_impact", "Analyzing callers and change impact in background")
                        enriched = attach_impact(repo_root, rng, enriched, limit=limit)
                        report("packet_impact_done", "Impact analysis complete")
                        if is_superseded():
                            enrichment_superseded = True
                            raise ValueError("newer live Review generation superseded background enrichment")
                    if with_provenance:
                        report("packet_provenance", "Correlating author context in background")
                        enriched = attach_provenance(
                            store_root,
                            repo_root,
                            rng,
                            enriched,
                            session_id=session_id,
                            session_id_is_hint=session_id_is_hint,
                        )
                        report("packet_provenance_done", "Author context ready")
                        if is_superseded():
                            enrichment_superseded = True
                            raise ValueError("newer live Review generation superseded background enrichment")
                    analysis_after = source_state(repo_root, rng).fingerprint
                    if captured_source and analysis_after != captured_source:
                        raise ValueError("source changed during background Review enrichment")
                    remote.update_review_preparation(
                        repo_id=str(repo_id),
                        review_id=review_id,
                        preparation_id=preparation_id,
                        state="finalizing_background",
                        detail="applying impact, provenance, ranking, and agent context",
                        timeout_s=config.request_timeout_s,
                    )
                    remote.enrich_review_projection(
                        repo_id=str(repo_id),
                        review_id=review_id,
                        revision_id=revision_id,
                        packet=enriched.packet.to_dict(),
                        timeout_s=config.tool_timeout_s,
                    )
                    if enriched.packet.provenance.certainty == "exact":
                        with contextlib.suppress(OSError, TypeError, ValueError):
                            write_live_review_binding(
                                store_root,
                                repo_root,
                                LiveReviewBinding(
                                    server_url=config.url,
                                    repo_id=str(repo_id),
                                    review_id=review_id,
                                    revision_id=revision_id,
                                    revision_number=revision_number if isinstance(revision_number, int) else 0,
                                    source_fingerprint=captured_source,
                                    author_host=enriched.packet.provenance.host or "",
                                    author_session_id=enriched.packet.provenance.session_id or "",
                                    review_short_id=review_short_id,
                                    revision_short_id=revision_short_id,
                                ),
                                allow_advance_claimed=advance_claimed_binding,
                            )
                    build = enriched
                    captured = ServerCapture(
                        build=build,
                        repo_id=str(repo_id),
                        review_id=review_id,
                        review_url=review_url,
                        response=response,
                        preparation_id=preparation_id,
                    )
                    report("packet_ranking_done", "Review context enriched in place")
                except (ClientError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    report("packet_enrichment_failed", f"Source remains reviewable; enrichment skipped · {exc}")
                finally:
                    try:
                        remote.update_review_preparation(
                            repo_id=str(repo_id),
                            review_id=review_id,
                            preparation_id=preparation_id,
                            state="ready",
                            detail=(
                                "source is ready; enrichment was superseded by a newer live generation"
                                if enrichment_superseded
                                else "source and available review context are ready"
                            ),
                            timeout_s=config.request_timeout_s,
                        )
                    except ClientError:
                        pass

            report("freeze_base", "Freezing base snapshot in background")
            with tempfile.TemporaryDirectory(prefix="lemoncrow-review-base-") as base_temporary:
                base_root = Path(base_temporary)
                _materialize_capture_old_side(repo_root, rng, base_root)
                base_opened = remote.open_review_snapshot(
                    base_root,
                    source_revision=rng.base_sha or "review-empty-base",
                    timeout_s=config.tool_timeout_s,
                    allow_oversize=True,
                )
                base_view_id = str(base_opened.get("view_id") or "")
                base_view_revision = int(base_opened.get("view_revision") or 0)
                if not base_view_id:
                    raise ValueError("LemonCrow server opened a base Review View without an id")
                remote.attach_review_base(
                    repo_id=str(repo_id),
                    review_id=review_id,
                    revision_id=revision_id,
                    base_view_id=base_view_id,
                    base_view_revision=base_view_revision,
                    timeout_s=config.tool_timeout_s,
                )
            report("freeze_base_done", "Base snapshot attached")
            return captured

        # Hosted, staged and commit-range capture retain the conservative exact
        # two-snapshot path until their live-View equivalents are available.
        report("freeze_base", "Freezing base snapshot")
        with tempfile.TemporaryDirectory(prefix="lemoncrow-review-base-") as base_temporary:
            base_root = Path(base_temporary)
            _materialize_capture_old_side(repo_root, rng, base_root)
            base_opened = remote.open_review_snapshot(
                base_root,
                source_revision=rng.base_sha or "review-empty-base",
                timeout_s=config.tool_timeout_s,
            )
            base_view_id = str(base_opened.get("view_id") or "")
            base_view_revision = int(base_opened.get("view_revision") or 0)
        report("freeze_base_done", "Base snapshot frozen")
        report("freeze_current", "Freezing current snapshot")
        with tempfile.TemporaryDirectory(prefix="lemoncrow-review-snapshot-") as temporary:
            snapshot_root = Path(temporary)
            _materialize_capture_new_side(repo_root, rng, build, snapshot_root)
            opened = remote.open_review_snapshot(
                snapshot_root,
                source_revision=rng.head_sha or rng.base_sha or "review-snapshot",
                timeout_s=config.tool_timeout_s,
            )
        report("freeze_current_done", "Current snapshot frozen")
        repo_id = opened.get("repo_id")
        if not isinstance(repo_id, str) or not repo_id:
            raise ValueError("LemonCrow server opened a source View without a repository id")
        report("publish", "Publishing immutable Review revision")
        raw = remote.capture_review(
            repo_id=repo_id,
            source_ref=stable_source_ref,
            title=review_title,
            packet=packet.to_dict(),
            new_blobs={},
            base_view_id=base_view_id,
            base_view_revision=base_view_revision,
            restore_archived=restore_archived,
            workspace_branch=workspace_branch,
            timeout_s=config.tool_timeout_s,
        )
        response = dict(raw)
        review = response.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("id"), str) or not review.get("id"):
            raise ValueError("LemonCrow server captured the diff without returning a Review id")
        review_id = str(review["id"])
        revision = response.get("revision")
        revision_number = revision.get("revision_number") if isinstance(revision, dict) else None
        report(
            "publish_done",
            (f"Snapshot {revision_number} published" if isinstance(revision_number, int) else "Snapshot published"),
        )
        review_path = review.get("review_path")
        review_url = (
            config.endpoint(review_path)
            if isinstance(review_path, str) and review_path.startswith("/r/")
            else remote.review_url(str(review.get("ref") or f"r/{review_id}"))
        )
        captured = ServerCapture(
            build=build,
            repo_id=str(repo_id),
            review_id=review_id,
            review_url=review_url,
            response=response,
        )
        if ready is not None:
            ready(captured)
        return captured
    finally:
        if parallel_pool is not None:
            parallel_pool.shutdown(wait=True, cancel_futures=True)
        remote.close()


def server_review_rows(config: ClientConfig) -> list[dict[str, Any]]:
    """Return Review Inbox rows from the configured LemonCrow server."""

    remote = RemoteSession(config)
    payload = remote.review_inbox(timeout_s=config.request_timeout_s)
    rows = payload.get("reviews")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def server_review_detail(config: ClientConfig, repo_id: str, review_id: str) -> dict[str, Any]:
    """Load one Review after its repository scope is known."""

    remote = RemoteSession(config)
    return dict(remote.review_detail(repo_id, review_id, timeout_s=config.request_timeout_s))


def server_review_url(config: ClientConfig, review_id: str) -> str:
    """Canonical Reader URL on the configured LemonCrow origin."""

    return RemoteSession(config).review_url(review_id)


def pair_local_review_browser(config: ClientConfig, path: str) -> dict[str, Any]:
    """Arm a clean local Review URL before opening it in the browser."""

    remote = RemoteSession(config)
    return dict(remote.pair_local_review_browser(path, timeout_s=config.request_timeout_s))


def server_review_request(
    config: ClientConfig,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one terminal/Reader Review operation on the configured server."""

    remote = RemoteSession(config)
    return dict(
        remote.review_reader_request(
            method,
            path,
            body=body,
            timeout_s=config.tool_timeout_s,
        )
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--finalize-job")
    args = parser.parse_args(argv)
    if not args.finalize_job:
        return 2
    return _run_local_finalize_job(Path(args.finalize_job))


if __name__ == "__main__":
    raise SystemExit(_main())
