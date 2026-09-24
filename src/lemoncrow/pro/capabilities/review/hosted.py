"""Server-backed Review client bridge for ``lc review``.

Local and hosted installs deliberately use this exact path. The topology input
is only :class:`lemoncrow_client.config.ClientConfig`: capture builds the same
deterministic ReviewPacket, synchronizes the source View through the audited
thin client, and publishes into server-side Review storage. It never binds a
listener or starts a Review child process.
"""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow_client.config import ClientConfig
from lemoncrow_client.session import RemoteSession

from .gitdiff import RevRange, _open_repo
from .packet import PacketBuild, build_review_packet_with_blobs
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


def capture_server_review(
    config: ClientConfig,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None,
    limit: int,
    with_impact: bool,
    with_provenance: bool,
    restore_archived: bool = False,
    progress: CaptureProgress | None = None,
    ready: CaptureReady | None = None,
) -> ServerCapture:
    """Capture *rng* into the configured LemonCrow server without binding locally."""

    def report(stage: str, detail: str = "") -> None:
        if progress is not None:
            progress(stage, detail)

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

    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=with_impact,
        with_provenance=with_provenance,
        session_id=session_id,
        limit=limit,
        with_patch_text=True,
        unbounded_patch_text=True,
        progress=packet_progress,
    )
    if packet_stage is not None:
        report(f"packet_{packet_stage}_done", packet_done.get(packet_stage, packet_stage))
    packet = build.packet
    report(
        "packet_ready_done", f"Change packet ready · {len(packet.files)} file{'s' if len(packet.files) != 1 else ''}"
    )
    stable_source_ref = source_ref(rng, repo_root=repo_root) or f"client:{rng.mode}"

    remote = RemoteSession(config)
    try:
        report("connect", "Connecting to Review server")
        remote.handshake(timeout_s=config.request_timeout_s)
        remote.open_session(timeout_s=config.request_timeout_s)
        report("connect_done", "Review server ready")

        # Local working-tree Review already has the exact current checkout on
        # the same machine. Synchronize that ordinary View and publish from it
        # directly instead of materializing + uploading a second complete copy.
        # The server snapshots the View membership into the immutable Review, so
        # later edits cannot mutate what the reviewer was shown. The complete
        # base tree is attached *after* publication; Reader can render the packet
        # immediately while Compare/runtime materialization finishes.
        live_source = ""
        if not config.hosted and rng.mode == "working_tree":
            from .gitdiff import source_state

            live_source = source_state(repo_root, rng).fingerprint
            if not _worktree_holds_packet_bytes(repo_root, build):
                # A save landed while the packet was built. The live View would
                # publish bytes the reviewer was never shown, so freeze the
                # packet's own bytes through the exact snapshot path instead.
                live_source = ""
        if live_source:
            captured_source = live_source
            report("sync_current", "Synchronizing current source")
            opened = remote.open_view(timeout_s=config.tool_timeout_s)
            current_source = source_state(repo_root, rng).fingerprint
            if captured_source and current_source and captured_source != current_source:
                raise ValueError("working tree changed while Review was being captured; run lcr again")
            report("sync_current_done", "Current source synchronized")
            repo_id = opened.get("repo_id")
            if not isinstance(repo_id, str) or not repo_id:
                raise ValueError("LemonCrow server opened a source View without a repository id")

            report("publish", "Publishing reviewable source revision")
            raw = remote.capture_review(
                repo_id=repo_id,
                source_ref=stable_source_ref,
                title=packet.title,
                packet=packet.to_dict(),
                new_blobs={},
                restore_archived=restore_archived,
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
            revision_id = str(revision["id"])
            revision_number = revision.get("revision_number")
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
            report(
                "publish_done",
                (
                    f"Review revision {revision_number} ready for reading"
                    if isinstance(revision_number, int)
                    else "Review revision ready for reading"
                ),
            )
            if ready is not None:
                ready(captured)

            report("freeze_base", "Freezing base snapshot in background")
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
            title=packet.title,
            packet=packet.to_dict(),
            new_blobs={},
            base_view_id=base_view_id,
            base_view_revision=base_view_revision,
            restore_archived=restore_archived,
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
            (
                f"Review revision {revision_number} published"
                if isinstance(revision_number, int)
                else "Review revision published"
            ),
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
