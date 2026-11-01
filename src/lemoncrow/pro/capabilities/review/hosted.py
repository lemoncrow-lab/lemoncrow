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
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.liveview import load_live_view_binding
from lemoncrow_client.session import RemoteSession, workspace_state_fingerprint

from .gitdiff import (
    BlobPair,
    RevRange,
    _open_repo,
    current_branch_ref,
    legacy_source_fingerprint,
    source_state,
    with_review_base,
)
from .live_runtime import (
    LiveReviewBinding,
    _write_live_review_binding_unlocked,
    live_review_publication_guard,
    load_live_review_binding,
    write_live_review_binding,
)
from .models import ReviewPacket, review_packet_from_dict
from .packet import (
    PacketBuild,
    attach_changed_symbols,
    attach_impact,
    attach_provenance,
    attach_workspace_impact,
    build_review_packet_with_blobs,
)
from .precompute import base_snapshot, load_source_build, save_source_build
from .snapshot import materialize_git_revision
from .sources.local import source_ref
from .workspace_impact import PendingWorkspaceImpact, packet_rows


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
    """Short branch name routing local Review identity; a paused rebase keeps its branch."""

    branch = current_branch_ref(repo_root).removeprefix("refs/heads/")
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
    publish_live_binding: bool = True,
    attach_base: bool = True,
    with_workspace: bool = False,
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
            "paths": list(rng.paths),
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
        "with_workspace": bool(with_workspace),
        "advance_claimed_binding": bool(advance_claimed_binding),
        "publish_live_binding": bool(publish_live_binding),
        "attach_base": bool(attach_base),
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
        range_data = dict(payload["rng"])
        range_data["paths"] = tuple(range_data.get("paths") or ())
        rng = RevRange(**range_data)
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
        with_workspace = bool(payload.get("with_workspace")) and with_impact and not config.hosted
        session_id = str(payload.get("session_id") or "") or None
        session_id_is_hint = bool(payload.get("session_id_is_hint"))
        limit = int(payload.get("limit") or 40)
        advance_claimed_binding = bool(payload.get("advance_claimed_binding"))
        publish_live_binding = bool(payload.get("publish_live_binding", True))
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
                    pending_workspace = _workspace_scan(repo_root, build, enabled=with_workspace)
                    if with_impact:
                        enriched = attach_impact(repo_root, rng, enriched, limit=limit)
                    enriched = _with_workspace_impact(enriched, pending_workspace)
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
                        packet=_server_packet(config, enriched.packet),
                        timeout_s=config.tool_timeout_s,
                    )
                    if publish_live_binding and enriched.packet.provenance.certainty == "exact":
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
                                    branch_ref=(
                                        f"refs/heads/{_workspace_branch(repo_root)}"
                                        if _workspace_branch(repo_root)
                                        else ""
                                    ),
                                    worktree_key=hashlib.sha256(
                                        str(repo_root.resolve()).encode("utf-8", errors="surrogateescape")
                                    ).hexdigest()[:32],
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

            if not payload.get("attach_base", True):
                return 0
            with base_snapshot(store_root, repo_root, rng, _materialize_capture_old_side) as base_root:
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


def _effective_local_review_base(repo_root: Path, payload: Mapping[str, Any], frozen: str) -> str:
    """Preserve integration for legacy Reviews; explicit new Reviews stay fixed.

    Blank when this clone lacks the recorded base commit: the capture keeps
    ``HEAD`` as its base under either policy.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import review_branch_base

    return review_branch_base(repo_root, frozen, advance=payload.get("review_base_policy") != "fixed")


def _local_review_base_for_reference(
    config: ClientConfig,
    *,
    repo_root: Path,
    reference: str,
    range_mode: str,
) -> tuple[str, str]:
    """Resolve one public/canonical local Review reference to its frozen base."""

    wanted = reference.strip()
    if not wanted or config.hosted:
        return "", ""
    try:
        rows = server_review_rows(config)
    except (ClientError, AttributeError, TypeError):
        return "", ""
    resolved_root = str(repo_root.resolve())
    token = wanted.removeprefix("r/")
    matches: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "") != "open" or str(row.get("range_mode") or "") != range_mode:
            continue
        worktree_root = str(row.get("worktree_root") or "")
        if worktree_root and worktree_root != resolved_root:
            continue
        row_id = str(row.get("id") or "")
        row_ref = str(row.get("ref") or "")
        short_id = str(row.get("short_id") or "")
        matched = row_ref == wanted or row_id == wanted or short_id == token
        if not matched and token and len(token) >= 8:
            matched = row_id.startswith(token)
        if matched:
            matches.append(row)
    if len(matches) != 1:
        return "", ""
    row = matches[0]
    review_id = str(row.get("id") or "").strip()
    frozen = str(row.get("review_base_sha") or "").strip()
    if review_id and frozen:
        return review_id, _effective_local_review_base(repo_root, row, frozen)
    if not review_id:
        return "", ""
    try:
        overview = server_review_request(config, "GET", f"/api/reviews/{review_id}")
    except ClientError:
        return "", ""
    raw = overview.get("session") if isinstance(overview, dict) else None
    session = raw if isinstance(raw, dict) else {}
    frozen = str(session.get("review_base_sha") or "").strip()
    return (review_id, _effective_local_review_base(repo_root, session, frozen)) if frozen else ("", "")


def _branch_bound_review(repo_root: Path, branch: str) -> str:
    """Return the Review explicitly bound to a local fix branch."""

    from lemoncrow.pro.capabilities.review.fix_lines import BRANCH_REVIEW_KEY

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "config",
            "--get",
            f"branch.{branch}.{BRANCH_REVIEW_KEY}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def active_local_review_base(
    config: ClientConfig,
    *,
    repo_root: Path,
    store_root: Path,
    range_mode: str,
) -> tuple[str, str]:
    """Resolve this worktree branch's active mutable Review and frozen base.

    The live binding is only a fast pointer to the last Review used in this
    worktree. Branch switching (and staged-vs-working-tree review) can make that
    pointer irrelevant, so the server's durable branch binding is authoritative.
    """

    if config.hosted or range_mode not in {"working_tree", "staged"}:
        return "", ""
    branch = _workspace_branch(repo_root)
    if not branch:
        return "", ""
    branch_ref = f"refs/heads/{branch}"
    resolved_root = str(repo_root.resolve())

    def review_base(review_id: str) -> tuple[str, str]:
        try:
            overview = server_review_request(config, "GET", f"/api/reviews/{review_id}")
        except ClientError:
            return "", ""
        raw = overview.get("session") if isinstance(overview, dict) else None
        session = raw if isinstance(raw, dict) else {}
        if session.get("status") != "open" or session.get("range_mode") != range_mode:
            return "", ""
        frozen = str(session.get("review_base_sha") or "").strip()
        if not frozen:
            try:
                history = server_review_request(config, "GET", f"/api/reviews/{review_id}/revisions")
            except ClientError:
                history = {}
            revisions = history.get("revisions") if isinstance(history, dict) else None
            if isinstance(revisions, list):
                for item in revisions:
                    if isinstance(item, dict):
                        frozen = str(item.get("base_sha") or "").strip()
                        if frozen:
                            break
        return (review_id, _effective_local_review_base(repo_root, session, frozen)) if frozen else ("", "")

    # Fix-line Reviews are hidden from the inbox, so the branch itself carries
    # the child Review id and keeps its frozen base stable after fixer commits.
    bound = _branch_bound_review(repo_root, branch)
    if bound:
        # A newly-created fix child has no Snapshot yet, so the generic Reader
        # overview quite correctly refuses it. The fix-line metadata is already
        # durable, though, and carries the frozen commit needed by capture #1.
        try:
            fix_meta = server_review_request(config, "GET", f"/api/reviews/{bound}/fix-lines")
        except ClientError:
            fix_meta = {}
        raw_fix_of = fix_meta.get("fix_of") if isinstance(fix_meta, dict) else None
        fix_of = raw_fix_of if isinstance(raw_fix_of, dict) else {}
        frozen_fix_base = str(fix_of.get("base_commit") or "").strip()
        if frozen_fix_base:
            return bound, frozen_fix_base
        resolved = review_base(bound)
        if resolved[0]:
            return resolved

    binding = load_live_review_binding(store_root, repo_root, server_url=config.url)
    if binding is not None and binding.review_id and binding.branch_ref == branch_ref:
        resolved = review_base(binding.review_id)
        if resolved[0]:
            return resolved

    try:
        rows = server_review_rows(config)
    except (ClientError, AttributeError, TypeError):
        return "", ""
    candidates = [
        row
        for row in rows
        if str(row.get("status") or "") == "open"
        and str(row.get("range_mode") or "") == range_mode
        and str(row.get("branch") or "") == branch
        and str(row.get("worktree_root") or "") == resolved_root
    ]
    active = [row for row in candidates if row.get("branch_active") is True]
    chosen: dict[str, Any] | None = None
    if len(active) == 1:
        chosen = active[0]
    elif len(active) > 1:
        return "", ""
    elif len(candidates) == 1:
        # Upgrade path before ReviewBinding existed: one unambiguous open Review
        # for the exact worktree+branch may be adopted safely.
        chosen = candidates[0]
    if chosen is None:
        return "", ""
    review_id = str(chosen.get("id") or chosen.get("ref") or "").strip()
    frozen = str(chosen.get("review_base_sha") or "").strip()
    if review_id and frozen:
        return review_id, _effective_local_review_base(repo_root, chosen, frozen)
    return review_base(review_id) if review_id else ("", "")


def hosted_branch_review_base(
    remote: RemoteSession,
    config: ClientConfig,
    *,
    repo_root: Path,
    range_mode: str,
) -> tuple[str, str, str]:
    """The branch this hosted capture names, and that branch's open Review and base.

    Returns ``(workspace_branch, review_id, base)``. Hosted routes a
    working-tree or staged Review by repository, branch and the signed-in
    author (plan 2026-10-10, D1), so the server answers only for the caller.
    The base is the recorded one advanced past integrated work under the
    ``integrated`` policy, as :func:`active_local_review_base` does locally;
    blank when there is no Review, it recorded no base, or this clone lacks
    the base commit. A server that does not honor ``workspace_branch`` gets
    no branch and is not asked: the capture routes as it always did. One
    that does serves the lookup (``BRANCH_REVIEW_PATH``, beside the Reader
    API, so it rides the session's refreshing transport); if the lookup
    fails, the capture fails with a retryable refusal.
    """

    if not config.hosted or range_mode not in {"working_tree", "staged"}:
        return "", "", ""
    branch = _workspace_branch(repo_root)
    if not branch:
        return "", "", ""
    if "workspace_branch" not in remote.review_manifest(timeout_s=config.request_timeout_s).capture_options:
        return "", "", ""
    from urllib.parse import urlencode

    from lemoncrow_client.config import repo_identity
    from lemoncrow_client.review_base import BRANCH_REVIEW_PATH, branch_review_failed, parse_branch_review

    # The engine has no repository id before it opens a View: it names the
    # repository by the identity claim a View open sends.
    query = urlencode({"range_mode": range_mode, "branch": branch, **repo_identity(repo_root).to_wire()})
    try:
        payload = remote.review_reader_request(
            "GET", f"{BRANCH_REVIEW_PATH}?{query}", timeout_s=config.request_timeout_s
        )
        found = parse_branch_review(payload, range_mode=range_mode)
    except ClientError as exc:
        raise branch_review_failed(branch, exc) from exc
    if not found.review_id or not found.base_sha:
        return branch, "", ""
    base = _effective_local_review_base(repo_root, {"review_base_policy": found.policy}, found.base_sha)
    return branch, found.review_id, base


def _server_packet(config: ClientConfig, packet: ReviewPacket) -> dict[str, Any]:
    """The packet as a Review server receives it.

    The ``workspace_impact`` slice names other repositories of this machine and their paths: only a local server
    stores it. A hosted server always receives it empty, whatever the packet carries.
    """

    payload = packet.to_dict()
    if config.hosted:
        payload["workspace_impact"] = {}
    return payload


def _workspace_scan(repo_root: Path, build: PacketBuild, *, enabled: bool) -> PendingWorkspaceImpact | None:
    """Start the workspace scan for *build*'s changed files on its own thread; ``None`` when not asked."""

    if not enabled or not build.packet.files:
        return None
    return PendingWorkspaceImpact(repo_root, packet_rows(build.packet.files))


def _with_workspace_impact(build: PacketBuild, pending: PendingWorkspaceImpact | None) -> PacketBuild:
    return build if pending is None else attach_workspace_impact(build, pending.result())


def _local_source_build(
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None,
    session_id_is_hint: bool,
    limit: int,
    progress: Callable[[str], None] | None = None,
    report: Callable[[str, str], None] | None = None,
) -> PacketBuild:
    """The cacheable working-tree build: exact diff, source bytes and changed symbols.

    Persist only source-derived data. Evidence and graph results (impact,
    provenance) are refreshed after publication even when this build is reused.
    """

    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_impact=False,
        with_provenance=False,
        session_id=session_id,
        session_id_is_hint=session_id_is_hint,
        limit=limit,
        with_patch_text=True,
        unbounded_patch_text=True,
        progress=progress,
    )
    if report is not None:
        report("packet_symbols", "Identifying changed symbols for stable Review targets")
    build = attach_changed_symbols(repo_root, build, limit=limit)
    if report is not None:
        report("packet_symbols_done", "Stable Review targets ready")
    return build


def prepare_local_source_build(
    config: ClientConfig,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None,
    limit: int,
) -> str:
    """Draft the next working-tree Snapshot locally; return its source fingerprint.

    Nothing is written to the Review server: no Review and no Snapshot exist
    until ``lc review`` (or the Reader's Snapshot action) publishes one, and
    :func:`capture_server_review` then reuses this exact build instead of
    rebuilding it. Returns ``""`` when there is nothing to prepare.
    """

    if config.hosted or rng.mode != "working_tree" or rng.paths:
        return ""
    if _workspace_branch(repo_root):
        # The range a plain `lc review` captures: the branch Review's frozen base.
        review_id, frozen_base = active_local_review_base(
            config, repo_root=repo_root, store_root=store_root, range_mode=rng.mode
        )
        if review_id and frozen_base:
            rng = with_review_base(repo_root, frozen_base, rng)
    fingerprint = source_state(repo_root, rng).fingerprint
    if load_source_build(store_root, repo_root, rng, fingerprint) is not None:
        return fingerprint
    build = _local_source_build(
        repo_root, rng, store_root=store_root, session_id=session_id, session_id_is_hint=True, limit=limit
    )
    save_source_build(store_root, repo_root, rng, build)
    return build.source_fingerprint


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
    force_new_review: bool = False,
    continue_review_id: str = "",
    reuse_closed_review: bool = False,
    paths: tuple[str, ...] = (),
    review_scope: str = "",
    with_workspace: bool = False,
) -> ServerCapture:
    """Capture *rng* into the configured LemonCrow server without binding locally.

    ``source_ref_override`` names the durable Review explicitly (a GitHub pull
    request is ``github:owner/repo#N`` whatever its head SHA), and
    ``title_override`` supplies the author title for this captured Snapshot
    on a local server; a hosted server has no per-Snapshot title and titles
    only a Review this capture creates.

    ``paths`` limits the Change to those files and ``review_scope`` names the
    workstream it belongs to (one agent session in a shared worktree): a scoped
    Review is its own Change, advanced by later scoped captures, and never
    continues or takes over the branch's Review.

    ``with_workspace`` adds the packet's ``workspace_impact`` slice: which other
    repositories of the reviewer's workspace depend on what changed. Only a
    review a person asked for on a local server turns it on; it needs the
    impact pass, and a hosted server never receives it. The working-tree and
    commit-range paths scan beside the post-publication impact pass, so the
    first readable Snapshot never waits for it.
    """

    if paths:
        rng = dataclass_replace(rng, paths=tuple(sorted({path for path in paths if path})))
    # Other repositories' names and paths stay on this machine.
    with_workspace = bool(with_workspace and with_impact and not config.hosted)
    publish_live_binding = not (review_scope or rng.paths)
    capture_started = time.perf_counter()
    stage_started: dict[str, float] = {}
    print_timings = os.environ.get("LEMONCROW_REVIEW_TIMINGS", "").lower() in {"1", "true", "yes"}

    def report(stage: str, detail: str = "") -> None:
        now = time.perf_counter()
        if stage.endswith("_done"):
            phase = stage[:-5]
            began = stage_started.pop(phase, None)
            if began is not None and print_timings:
                print(f"[review-timing] {phase}={(now - began) * 1000:.1f}ms", file=sys.stderr)
            if stage == "publish_done" and print_timings:
                print(f"[review-timing] first_snapshot={(now - capture_started) * 1000:.1f}ms", file=sys.stderr)
        else:
            stage_started.setdefault(stage, now)
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
    local_mutable_review = bool(not config.hosted and rng.mode in {"working_tree", "staged"})
    workspace_branch = _workspace_branch(repo_root) if not config.hosted and not review_scope else ""
    branch_ref = f"refs/heads/{workspace_branch}" if workspace_branch else ""
    target_review_id = continue_review_id.strip()
    if local_mutable_review and branch_ref and not force_new_review:
        frozen_base = ""
        if target_review_id:
            target_review_id, frozen_base = _local_review_base_for_reference(
                config, repo_root=repo_root, reference=target_review_id, range_mode=rng.mode
            )
        else:
            target_review_id, frozen_base = active_local_review_base(
                config, repo_root=repo_root, store_root=store_root, range_mode=rng.mode
            )
        if target_review_id and frozen_base:
            rng = with_review_base(repo_root, frozen_base, rng)
    # Immutable commit ranges have the same source-first contract: expensive
    # impact and provenance cannot hold the first reviewable diff hostage.
    progressive_commit_range = bool(
        not config.hosted and rng.mode == "commit_range" and (with_impact or with_provenance)
    )
    progressive_local = bool((local_working_tree or progressive_commit_range) and (with_impact or with_provenance))

    # First-open latency is the max of source capture and View sync, not their
    # sum. They are independent until the exact packet bytes are pinned below.
    # A matching live View is already cheaper than a new sync, so preserve that
    # path and parallelize only the cold/stale case.
    remote = RemoteSession(config, publish_live_view=False)
    if config.hosted and not review_scope:
        try:
            workspace_branch, _review_id, hosted_base = hosted_branch_review_base(
                remote, config, repo_root=repo_root, range_mode=rng.mode
            )
        except BaseException:
            remote.close()
            raise
        if hosted_base:
            rng = with_review_base(repo_root, hosted_base, rng)
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
        build = None
        # A scoped build must never be served as, or overwrite, the whole-tree one.
        if local_working_tree and not rng.paths:
            fingerprint = source_state(repo_root, rng).fingerprint
            build = load_source_build(store_root, repo_root, rng, fingerprint)
            if build is not None and source_state(repo_root, rng).fingerprint != fingerprint:
                build = None
        if build is None:
            if local_working_tree:
                build = _local_source_build(
                    repo_root,
                    rng,
                    store_root=store_root,
                    session_id=session_id,
                    session_id_is_hint=session_id_is_hint,
                    limit=limit,
                    progress=packet_progress,
                    report=report,
                )
                if not rng.paths:
                    save_source_build(store_root, repo_root, rng, build)
            else:
                build = build_review_packet_with_blobs(
                    repo_root,
                    rng,
                    store_root=store_root,
                    with_impact=with_impact and not progressive_commit_range,
                    with_provenance=with_provenance and not progressive_commit_range,
                    session_id=session_id,
                    session_id_is_hint=session_id_is_hint,
                    limit=limit,
                    with_patch_text=True,
                    unbounded_patch_text=True,
                    progress=packet_progress,
                    with_workspace=with_workspace and not progressive_commit_range,
                )
                if not with_impact and not (progressive_commit_range and len(build.packet.files) > 250):
                    # For small Reviews, publish stable symbols immediately.
                    # For large local commit ranges, initial file targets already
                    # allow diff review; all symbol/caller intelligence follows
                    # in the immutable Snapshot's background enrichment.
                    build = attach_changed_symbols(repo_root, build, limit=limit)
        else:
            report("packet_source_done", "Prepared diff, source snapshots, and Review targets reused")
        if packet_stage is not None:
            report(f"packet_{packet_stage}_done", packet_done.get(packet_stage, packet_stage))
        packet = build.packet
        report(
            "packet_ready_done",
            f"Change packet ready · {len(packet.files)} file{'s' if len(packet.files) != 1 else ''}",
        )
        stable_source_ref = source_ref_override or source_ref(rng, repo_root=repo_root) or f"client:{rng.mode}"
        review_title = title_override or packet.title
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
            # The server matches a Snapshot recorded before the tree-id identity by this form.
            captured_legacy = legacy_source_fingerprint(repo_root, rng, captured_source)
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
                    timeout_s=config.tool_timeout_s,
                )

            report(
                "publish",
                "Publishing new review" if force_new_review or not target_review_id else "Creating new snapshot",
            )
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
                        packet=_server_packet(config, packet),
                        new_blobs={},
                        restore_archived=restore_archived,
                        source_fingerprint=captured_source,
                        legacy_source_fingerprint=captured_legacy,
                        workspace_branch=workspace_branch,
                        force_new_review=force_new_review,
                        continue_review_id=continue_review_id,
                        reuse_closed_review=reuse_closed_review,
                        review_scope=review_scope,
                        timeout_s=config.tool_timeout_s,
                        progress=lambda detail: report("publish", f"Creating new snapshot · {detail}"),
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
                        timeout_s=config.tool_timeout_s,
                    )
                    report("sync_current_done", "Current source synchronized and Snapshot pinned")
                    raw = remote.capture_review(
                        repo_id=repo_id,
                        source_ref=stable_source_ref,
                        title=review_title,
                        packet=_server_packet(config, packet),
                        new_blobs={},
                        restore_archived=restore_archived,
                        source_fingerprint=captured_source,
                        legacy_source_fingerprint=captured_legacy,
                        workspace_branch=workspace_branch,
                        force_new_review=force_new_review,
                        continue_review_id=continue_review_id,
                        reuse_closed_review=reuse_closed_review,
                        review_scope=review_scope,
                        timeout_s=config.tool_timeout_s,
                        progress=lambda detail: report("publish", f"Creating new snapshot · {detail}"),
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
                # A scoped Review is its own Change: its source fingerprint covers
                # the whole worktree, so as the warm pointer plain `lc review`
                # reopens it would open the scoped Review instead of the branch's.
                if publish_live_binding:
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
                                branch_ref=branch_ref,
                                worktree_key=hashlib.sha256(
                                    str(repo_root.resolve()).encode("utf-8", errors="surrogateescape")
                                ).hexdigest()[:32],
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
                    publish_live_binding=publish_live_binding,
                    with_workspace=with_workspace,
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
                    pending_workspace = _workspace_scan(repo_root, build, enabled=with_workspace)
                    if with_impact:
                        report("packet_impact", "Analyzing callers and change impact in background")
                        enriched = attach_impact(repo_root, rng, enriched, limit=limit)
                        report("packet_impact_done", "Impact analysis complete")
                        if is_superseded():
                            enrichment_superseded = True
                            raise ValueError("newer live Review generation superseded background enrichment")
                    enriched = _with_workspace_impact(enriched, pending_workspace)
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
                        packet=_server_packet(config, enriched.packet),
                        timeout_s=config.tool_timeout_s,
                    )
                    if publish_live_binding and enriched.packet.provenance.certainty == "exact":
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
                                    branch_ref=branch_ref,
                                    worktree_key=hashlib.sha256(
                                        str(repo_root.resolve()).encode("utf-8", errors="surrogateescape")
                                    ).hexdigest()[:32],
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
            with base_snapshot(store_root, repo_root, rng, _materialize_capture_old_side) as base_root:
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
        with base_snapshot(store_root, repo_root, rng, _materialize_capture_old_side) as base_root:
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
            packet=_server_packet(config, packet),
            new_blobs={},
            base_view_id=base_view_id,
            base_view_revision=base_view_revision,
            restore_archived=restore_archived,
            workspace_branch=workspace_branch,
            force_new_review=force_new_review,
            continue_review_id=continue_review_id,
            reuse_closed_review=reuse_closed_review,
            review_scope=review_scope,
            timeout_s=config.tool_timeout_s,
        )
        response = dict(raw)
        review = response.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("id"), str) or not review.get("id"):
            raise ValueError("LemonCrow server captured the diff without returning a Review id")
        review_id = str(review["id"])
        revision = response.get("revision")
        revision_number = revision.get("revision_number") if isinstance(revision, dict) else None
        revision_id = str(revision.get("id") or "") if isinstance(revision, dict) else ""
        if progressive_commit_range and not revision_id:
            raise ValueError("LemonCrow server captured the diff without returning a Snapshot id")
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
        preparation_id = ""
        if progressive_commit_range:
            preparation_id = f"prep-{uuid.uuid4().hex}"
            remote.update_review_preparation(
                repo_id=str(repo_id),
                review_id=review_id,
                preparation_id=preparation_id,
                state="impact_background",
                detail="exact diff is available; intelligence continues",
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
        if ready is not None:
            ready(captured)
        if progressive_commit_range:
            if defer_post_ready:
                job_path = _write_local_finalize_job(
                    store_root=store_root,
                    repo_root=repo_root,
                    rng=rng,
                    build=build,
                    repo_id=str(repo_id),
                    review_id=review_id,
                    revision_id=revision_id,
                    revision_number=revision_number if isinstance(revision_number, int) else 0,
                    review_short_id=str(review.get("short_id") or ""),
                    revision_short_id=str(revision.get("short_id") or "") if isinstance(revision, dict) else "",
                    preparation_id=preparation_id,
                    captured_source=build.source_fingerprint,
                    session_id=session_id,
                    session_id_is_hint=session_id_is_hint,
                    limit=limit,
                    with_impact=with_impact,
                    with_provenance=with_provenance,
                    advance_claimed_binding=False,
                    publish_live_binding=False,
                    attach_base=False,  # Immutable base was attached before publication.
                    with_workspace=with_workspace,
                )
                if _launch_local_finalize_job(job_path):
                    report("background_handoff_done", "Commit-range intelligence continues in background")
                    return captured
                with contextlib.suppress(OSError):
                    job_path.unlink(missing_ok=True)
            try:
                enriched = build
                pending_workspace = _workspace_scan(repo_root, build, enabled=with_workspace)
                if with_impact:
                    report("packet_impact", "Analyzing callers and impact after source publication")
                    enriched = attach_impact(repo_root, rng, enriched, limit=limit)
                    report("packet_impact_done", "Impact ready")
                enriched = _with_workspace_impact(enriched, pending_workspace)
                if with_provenance:
                    report("packet_provenance", "Analyzing author context after source publication")
                    enriched = attach_provenance(
                        store_root,
                        repo_root,
                        rng,
                        enriched,
                        session_id=session_id,
                        session_id_is_hint=session_id_is_hint,
                    )
                    report("packet_provenance_done", "Author context ready")
                remote.enrich_review_projection(
                    repo_id=str(repo_id),
                    review_id=review_id,
                    revision_id=revision_id,
                    packet=_server_packet(config, enriched.packet),
                    timeout_s=config.tool_timeout_s,
                )
                captured = dataclass_replace(captured, build=enriched)
            except (ClientError, OSError, RuntimeError, TypeError, ValueError) as exc:
                report("packet_enrichment_failed", f"Source remains reviewable; enrichment skipped · {exc}")
            finally:
                with contextlib.suppress(ClientError):
                    remote.update_review_preparation(
                        repo_id=str(repo_id),
                        review_id=review_id,
                        preparation_id=preparation_id,
                        state="ready",
                        detail="source and available Review context are ready",
                        timeout_s=config.request_timeout_s,
                    )
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
