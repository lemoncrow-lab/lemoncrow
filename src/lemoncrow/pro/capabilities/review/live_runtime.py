"""Live Review preparation shared by authoring hooks and ``lc review``.

A coding host already knows when source changed.  Instead of making the human
pay for Review construction later, edit hooks signal this module and return
immediately.  A detached, coalescing worker captures the latest stable
working-tree Review and publishes a tiny rebuildable binding.  ``lcr`` may open
that binding immediately only when the current source fingerprint still matches.

The binding contains identifiers and hashes only -- never source bytes or
credentials.  ReviewStore remains the durable authority for source, comments,
marks, evidence, and immutable revisions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from lemoncrow.core.foundation.paths import safe_segment

_LIVE_DIR = "live-runtime"
_DEFAULT_DEBOUNCE_S = 0.45
_DEFAULT_WATCH_IDLE_S = 30 * 60.0
_DEFAULT_POLL_WATCH_IDLE_S = 60.0
_DEFAULT_POLL_INTERVAL_S = 5.0
_WATCH_IGNORED_ROOTS = frozenset({".lc-worktrees", ".lemoncrow", ".venv", "node_modules", "__pycache__"})


@dataclass(frozen=True, slots=True)
class LiveReviewBinding:
    server_url: str
    repo_id: str
    review_id: str
    revision_id: str
    revision_number: int
    source_fingerprint: str
    author_host: str = ""
    author_session_id: str = ""
    review_short_id: str = ""
    revision_short_id: str = ""
    updated_at: float = 0.0
    claimed_at: float = 0.0


@dataclass(frozen=True, slots=True)
class LiveReviewRequest:
    request_id: str
    host: str
    session_id: str
    created_at: float


def _workspace_key(repo_root: Path) -> str:
    raw = str(repo_root.expanduser().resolve()).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(raw).hexdigest()[:32]


def _workspace_dir(store_root: Path | str, repo_root: Path) -> Path:
    return Path(store_root).expanduser().resolve() / "review" / _LIVE_DIR / _workspace_key(repo_root)


def _binding_path(store_root: Path | str, repo_root: Path) -> Path:
    return _workspace_dir(store_root, repo_root) / "binding.json"


def _request_path(store_root: Path | str, repo_root: Path) -> Path:
    return _workspace_dir(store_root, repo_root) / "request.json"


def _lock_path(store_root: Path | str, repo_root: Path) -> Path:
    return _workspace_dir(store_root, repo_root) / "worker.lock"


def _binding_lock_path(store_root: Path | str, repo_root: Path) -> Path:
    return _workspace_dir(store_root, repo_root) / "binding.lock"


def _request_handoff_lock_path(store_root: Path | str, repo_root: Path) -> Path:
    return _workspace_dir(store_root, repo_root) / "request-handoff.lock"


_BINDING_PROCESS_LOCK = threading.RLock()
_REQUEST_PROCESS_LOCK = threading.RLock()


@contextlib.contextmanager
def _cross_process_lock(path: Path, process_lock: threading.RLock) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with process_lock:
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                api: Any = msvcrt
                api.locking(handle.fileno(), api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    api = msvcrt
                    api.locking(handle.fileno(), api.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


@contextlib.contextmanager
def _binding_lock(store_root: Path | str, repo_root: Path) -> Iterator[None]:
    """Serialize binding read-modify-write across threads and processes."""

    with _cross_process_lock(_binding_lock_path(store_root, repo_root), _BINDING_PROCESS_LOCK):
        yield


@contextlib.contextmanager
def _request_handoff_lock(store_root: Path | str, repo_root: Path) -> Iterator[None]:
    """Serialize request publication with the final worker ownership handoff."""

    with _cross_process_lock(_request_handoff_lock_path(store_root, repo_root), _REQUEST_PROCESS_LOCK):
        yield


class LiveReviewClaimed(RuntimeError):
    """Background publication lost the race to a human Review claim."""


@contextlib.contextmanager
def live_review_publication_guard(store_root: Path | str, repo_root: Path) -> Iterator[None]:
    """Serialize background server publication with the exact human claim.

    Packet construction stays outside this lock. Only the final server revision
    append plus local binding update belongs inside it, so either background
    publication wins first and the human claims that new exact revision, or the
    human claim wins first and background publication is refused.
    """

    with _binding_lock(store_root, repo_root):
        binding = _read_binding_unscoped(_binding_path(store_root, repo_root))
        if binding is not None and binding.claimed_at > 0:
            raise LiveReviewClaimed(
                "Review was claimed by a human before background publication; leaving the prepared generation unpublished"
            )
        yield


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _write_live_review_binding_unlocked(
    store_root: Path | str,
    repo_root: Path,
    binding: LiveReviewBinding,
    *,
    allow_advance_claimed: bool = False,
) -> bool:
    if not (
        binding.server_url
        and binding.repo_id
        and binding.review_id
        and binding.revision_id
        and binding.source_fingerprint
    ):
        raise ValueError("live Review binding requires server/repository/review/revision/source identity")
    path = _binding_path(store_root, repo_root)
    existing = _read_binding_unscoped(path)
    normalized_url = binding.server_url.rstrip("/")
    if (
        existing is not None
        and existing.claimed_at > 0
        and existing.server_url == normalized_url
        and existing.review_id == binding.review_id
        and existing.revision_id != binding.revision_id
        and not allow_advance_claimed
    ):
        # Once a human has claimed one exact revision, background preparation may
        # create future server revisions but must never move the local human-owned
        # pointer underneath them. Explicit human refresh owns that transition.
        return False

    claimed_at = max(0.0, float(binding.claimed_at or 0.0))
    if (
        claimed_at <= 0
        and existing is not None
        and existing.review_id == binding.review_id
        and existing.server_url == normalized_url
        and existing.revision_id == binding.revision_id
    ):
        claimed_at = existing.claimed_at

    author_host = binding.author_host.strip().lower()
    author_session_id = binding.author_session_id.strip()
    if (
        existing is not None
        and existing.server_url == normalized_url
        and existing.review_id == binding.review_id
        and existing.revision_id == binding.revision_id
        and existing.author_host
        and existing.author_session_id
        and (existing.author_host != author_host or existing.author_session_id != author_session_id)
    ):
        # Same source revision cannot acquire a different author merely because a
        # second agent emitted a prompt heartbeat in the same workspace.
        author_host = existing.author_host
        author_session_id = existing.author_session_id

    _atomic_json(
        path,
        {
            "server_url": normalized_url,
            "repo_id": binding.repo_id,
            "review_id": safe_segment(binding.review_id, field="review_id"),
            "revision_id": safe_segment(binding.revision_id, field="revision_id"),
            "revision_number": max(0, int(binding.revision_number)),
            "source_fingerprint": binding.source_fingerprint,
            "author_host": author_host,
            "author_session_id": author_session_id,
            "review_short_id": binding.review_short_id,
            "revision_short_id": binding.revision_short_id,
            "updated_at": binding.updated_at or time.time(),
            "claimed_at": claimed_at,
        },
    )
    return True


def write_live_review_binding(
    store_root: Path | str,
    repo_root: Path,
    binding: LiveReviewBinding,
    *,
    allow_advance_claimed: bool = False,
) -> bool:
    with _binding_lock(store_root, repo_root):
        return _write_live_review_binding_unlocked(
            store_root,
            repo_root,
            binding,
            allow_advance_claimed=allow_advance_claimed,
        )


def load_live_review_binding(
    store_root: Path | str,
    repo_root: Path,
    *,
    server_url: str,
) -> LiveReviewBinding | None:
    try:
        raw = json.loads(_binding_path(store_root, repo_root).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        binding = LiveReviewBinding(
            server_url=str(raw.get("server_url") or "").rstrip("/"),
            repo_id=str(raw.get("repo_id") or ""),
            review_id=safe_segment(str(raw.get("review_id") or ""), field="review_id"),
            revision_id=safe_segment(str(raw.get("revision_id") or ""), field="revision_id"),
            revision_number=int(raw.get("revision_number") or 0),
            source_fingerprint=str(raw.get("source_fingerprint") or ""),
            author_host=str(raw.get("author_host") or "").strip().lower(),
            author_session_id=str(raw.get("author_session_id") or "").strip(),
            review_short_id=str(raw.get("review_short_id") or "").strip(),
            revision_short_id=str(raw.get("revision_short_id") or "").strip(),
            updated_at=float(raw.get("updated_at") or 0.0),
            claimed_at=float(raw.get("claimed_at") or 0.0),
        )
    except (TypeError, ValueError):
        return None
    if binding.server_url != server_url.rstrip("/") or not binding.repo_id or not binding.source_fingerprint:
        return None
    return binding


def claim_live_review_binding(
    store_root: Path | str,
    repo_root: Path,
    *,
    server_url: str,
    expected_source_fingerprint: str = "",
    expected_revision_id: str = "",
) -> LiveReviewBinding | None:
    """Atomically claim the exact prepared binding the human is opening."""

    normalized_url = server_url.rstrip("/")
    with _binding_lock(store_root, repo_root):
        binding = _read_binding_unscoped(_binding_path(store_root, repo_root))
        if (
            binding is None
            or binding.server_url != normalized_url
            or not binding.repo_id
            or not binding.source_fingerprint
        ):
            return None
        if expected_source_fingerprint and binding.source_fingerprint != expected_source_fingerprint:
            return None
        if expected_revision_id and binding.revision_id != expected_revision_id:
            return None
        claimed = LiveReviewBinding(
            server_url=binding.server_url,
            repo_id=binding.repo_id,
            review_id=binding.review_id,
            revision_id=binding.revision_id,
            revision_number=binding.revision_number,
            source_fingerprint=binding.source_fingerprint,
            author_host=binding.author_host,
            author_session_id=binding.author_session_id,
            review_short_id=binding.review_short_id,
            revision_short_id=binding.revision_short_id,
            updated_at=binding.updated_at,
            claimed_at=time.time(),
        )
        _write_live_review_binding_unlocked(store_root, repo_root, claimed)
        return _read_binding_unscoped(_binding_path(store_root, repo_root))


def clear_live_review_binding(
    store_root: Path | str,
    repo_root: Path,
    *,
    expected_revision_id: str = "",
) -> None:
    path = _binding_path(store_root, repo_root)
    with _binding_lock(store_root, repo_root):
        if expected_revision_id:
            binding = _read_binding_unscoped(path)
            if binding is None or binding.revision_id != expected_revision_id:
                return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _read_binding_unscoped(path: Path) -> LiveReviewBinding | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return LiveReviewBinding(
            server_url=str(raw.get("server_url") or "").rstrip("/"),
            repo_id=str(raw.get("repo_id") or ""),
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            revision_number=int(raw.get("revision_number") or 0),
            source_fingerprint=str(raw.get("source_fingerprint") or ""),
            author_host=str(raw.get("author_host") or ""),
            author_session_id=str(raw.get("author_session_id") or ""),
            review_short_id=str(raw.get("review_short_id") or ""),
            revision_short_id=str(raw.get("revision_short_id") or ""),
            updated_at=float(raw.get("updated_at") or 0.0),
            claimed_at=float(raw.get("claimed_at") or 0.0),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_request(
    store_root: Path | str,
    repo_root: Path,
    *,
    host: str,
    session_id: str,
) -> LiveReviewRequest:
    request = LiveReviewRequest(
        request_id=uuid.uuid4().hex,
        host=host.strip().lower(),
        session_id=session_id.strip(),
        created_at=time.time(),
    )
    with _request_handoff_lock(store_root, repo_root):
        _atomic_json(
            _request_path(store_root, repo_root),
            {
                "request_id": request.request_id,
                "host": request.host,
                "session_id": request.session_id,
                "created_at": request.created_at,
            },
        )
    return request


def _read_request(store_root: Path | str, repo_root: Path) -> LiveReviewRequest | None:
    try:
        raw = json.loads(_request_path(store_root, repo_root).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    request_id = str(raw.get("request_id") or "")
    if not request_id:
        return None
    return LiveReviewRequest(
        request_id=request_id,
        host=str(raw.get("host") or "").strip().lower(),
        session_id=str(raw.get("session_id") or "").strip(),
        created_at=float(raw.get("created_at") or 0.0),
    )


def request_live_review_prepare(
    store_root: Path | str,
    repo_root: Path | str,
    *,
    host: str,
    session_id: str,
) -> LiveReviewRequest | None:
    """Signal source change and detach one fail-open preparation attempt."""

    if os.environ.get("LEMONCROW_LIVE_REVIEW_PREWARM", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    root = Path(store_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        return None
    try:
        request = _write_request(root, repo, host=host, session_id=session_id)
    except (OSError, TypeError, ValueError):
        return None

    override = os.environ.get("LEMONCROW_LIVE_REVIEW_PREPARE_CMD", "").strip()
    command = (
        shlex.split(override)
        if override
        else [
            sys.executable,
            "-m",
            "lemoncrow.pro.capabilities.review.live_runtime",
            "--worker",
            "--root",
            str(root),
            "--repo-root",
            str(repo),
        ]
    )
    try:
        subprocess.Popen(
            command,
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=dict(os.environ),
        )
    except OSError:
        # The signal remains on disk; a later edit can start another worker.
        return request
    return request


def _try_worker_lock(path: Path) -> IO[bytes] | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt_api: Any = msvcrt
            msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return None
    return handle


def _release_worker_lock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt_api: Any = msvcrt
            msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _watch_path_relevant(repo_root: Path, raw_path: str) -> bool:
    try:
        relative = Path(raw_path).expanduser().resolve().relative_to(repo_root)
    except (OSError, ValueError):
        return False
    if not relative.parts:
        return False
    first = relative.parts[0]
    if first in _WATCH_IGNORED_ROOTS:
        return False
    if first != ".git":
        return True
    # Source/base identity can change without a working-tree file event when an
    # editor stages, commits, checks out, rebases, or resets. Ignore noisy Git
    # object/log writes but keep the refs that can change Review semantics.
    git_relative = relative.as_posix()
    return git_relative in {".git/HEAD", ".git/index"} or git_relative.startswith(".git/refs/")


def _source_fingerprint(repo_root: Path) -> str:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    rng = resolve_rev_range(repo_root, working_tree=True)
    return source_state(repo_root, rng).fingerprint


def _binding_claimed(store_root: Path, repo_root: Path) -> bool:
    binding = _read_binding_unscoped(_binding_path(store_root, repo_root))
    return binding is not None and binding.claimed_at > 0


def _wait_with_watchdog(
    store_root: Path,
    repo_root: Path,
    *,
    baseline_fingerprint: str,
    baseline_request_id: str = "",
    timeout_s: float,
    debounce_s: float,
) -> bool | str | None:
    """Return a source/request wake reason, False on idle, None when unavailable."""

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    changed = threading.Event()

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            candidates = [str(getattr(event, "src_path", "") or ""), str(getattr(event, "dest_path", "") or "")]
            if any(path and _watch_path_relevant(repo_root, path) for path in candidates):
                changed.set()

    observer = Observer()
    observer.schedule(_Handler(), str(repo_root), recursive=True)
    try:
        observer.start()
    except Exception:
        with contextlib.suppress(Exception):
            observer.stop()
        return None
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while time.monotonic() < deadline:
            if _binding_claimed(store_root, repo_root):
                return False
            latest_request = _read_request(store_root, repo_root)
            if baseline_request_id and latest_request is not None and latest_request.request_id != baseline_request_id:
                return "request"
            is_alive = getattr(observer, "is_alive", None)
            if callable(is_alive) and not is_alive():
                # A dead observer is not an idle repository. Fall back to the
                # semantic poller instead of silently trusting a dead watcher.
                return None
            remaining = max(0.0, deadline - time.monotonic())
            event_seen = changed.wait(timeout=min(max(0.2, _DEFAULT_POLL_INTERVAL_S), remaining))
            if event_seen:
                changed.clear()
                if debounce_s > 0:
                    time.sleep(debounce_s)
            # Watchdog is only a wake-up accelerator. Semantic identity remains
            # authoritative even if an OS event is coalesced or dropped.
            try:
                if _source_fingerprint(repo_root) != baseline_fingerprint:
                    return "source"
            except (OSError, TypeError, ValueError):
                continue
        return False
    finally:
        observer.stop()
        with contextlib.suppress(Exception):
            observer.join(timeout=2.0)


def _wait_for_out_of_band_change(
    store_root: Path,
    repo_root: Path,
    *,
    baseline_fingerprint: str,
    baseline_request_id: str = "",
    watch_idle_s: float,
    debounce_s: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> bool | str:
    """Wait for source changes or a newer durable watcher heartbeat.

    Returns ``"source"`` or ``"request"`` when the worker should loop, and
    ``False`` on idle/claim. Boolean test doubles remain compatible: ``True``
    is treated by the caller as a source-change signal.
    """

    if watch_idle_s <= 0 or not baseline_fingerprint:
        return False

    watched = _wait_with_watchdog(
        store_root,
        repo_root,
        baseline_fingerprint=baseline_fingerprint,
        baseline_request_id=baseline_request_id,
        timeout_s=watch_idle_s,
        debounce_s=debounce_s,
    )
    if watched is not None:
        return watched

    # Minimal installs without watchdog use a bounded semantic poll.
    deadline = time.monotonic() + min(max(0.0, watch_idle_s), _DEFAULT_POLL_WATCH_IDLE_S)
    interval = max(0.2, poll_interval_s)
    while time.monotonic() < deadline:
        if _binding_claimed(store_root, repo_root):
            return False
        latest_request = _read_request(store_root, repo_root)
        if baseline_request_id and latest_request is not None and latest_request.request_id != baseline_request_id:
            return "request"
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        try:
            if _source_fingerprint(repo_root) != baseline_fingerprint:
                return "source"
        except (OSError, TypeError, ValueError):
            continue
    return False


def prepare_live_review_worker(
    store_root: Path | str,
    repo_root: Path | str,
    *,
    debounce_s: float = _DEFAULT_DEBOUNCE_S,
    watch_idle_s: float = 0.0,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> int:
    """Capture the latest requested working-tree Review, coalescing edit bursts."""

    root = Path(store_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    lock = _try_worker_lock(_lock_path(root, repo))
    if lock is None:
        return 0
    try:
        # One lock owner follows the request marker until no newer edit arrived
        # while it was preparing. Other detached workers can exit immediately.
        while True:
            if debounce_s > 0:
                time.sleep(debounce_s)
            request = _read_request(root, repo)
            if request is None:
                return 0
            request_id = request.request_id
            existing_binding = _read_binding_unscoped(_binding_path(root, repo))
            if existing_binding is not None and existing_binding.claimed_at > 0:
                # Human review now owns checkpoint cadence. The live authoring
                # View continues to update independently, while Review Reader's
                # feedback/addressed flow decides when to capture a new rr/.
                return 0
            try:
                from lemoncrow_client.config import load_config

                from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state
                from lemoncrow.pro.capabilities.review.hosted import capture_server_review

                config = load_config(cwd=repo)
                if config.hosted:
                    return 0  # local prewarm first; hosted needs tenant worker ownership
                rng = resolve_rev_range(repo, working_tree=True)
                if rng.mode != "working_tree":
                    clear_live_review_binding(root, repo)
                    return 0
                before = source_state(repo, rng)
                if not before.paths:
                    if existing_binding is not None:
                        clear_live_review_binding(root, repo, expected_revision_id=existing_binding.revision_id)
                    return 0
                if (
                    existing_binding is not None
                    and existing_binding.server_url == config.url.rstrip("/")
                    and existing_binding.source_fingerprint == before.fingerprint
                ):
                    # A prompt heartbeat proves session liveness, not authorship.
                    # Never retarget exact Review feedback unless source changed
                    # and a capture/provenance step established the new author.
                    after = before
                else:
                    baseline_fingerprint = before.fingerprint

                    def superseded(
                        request_id: str = request_id,
                        baseline_fingerprint: str = baseline_fingerprint,
                    ) -> bool:
                        latest_request = _read_request(root, repo)
                        return (
                            latest_request is not None and latest_request.request_id != request_id
                        ) or _source_fingerprint(repo) != baseline_fingerprint

                    captured = capture_server_review(
                        config,
                        repo,
                        rng,
                        store_root=root,
                        session_id=request.session_id or None,
                        session_id_is_hint=True,
                        limit=40,
                        with_impact=True,
                        with_provenance=True,
                        respect_human_claim=True,
                        superseded=superseded,
                    )
                    after = source_state(repo, rng)
                    response_revision = captured.response.get("revision")
                    revision = response_revision if isinstance(response_revision, dict) else {}
                    revision_id = str(revision.get("id") or "")
                    revision_number = int(revision.get("revision_number") or 0)
                    if before.fingerprint == after.fingerprint and revision_id:
                        author_host = ""
                        author_session_id = ""
                        try:
                            provenance = captured.build.packet.provenance
                            if provenance.certainty == "exact":
                                author_host = str(provenance.host or "")
                                author_session_id = str(provenance.session_id or "")
                        except (AttributeError, TypeError, ValueError):
                            # Source/revision identity is authoritative even when
                            # optional provenance enrichment is unavailable.
                            pass
                        write_live_review_binding(
                            root,
                            repo,
                            LiveReviewBinding(
                                server_url=config.url,
                                repo_id=captured.repo_id,
                                review_id=captured.review_id,
                                revision_id=revision_id,
                                revision_number=revision_number,
                                source_fingerprint=after.fingerprint,
                                author_host=author_host,
                                author_session_id=author_session_id,
                            ),
                        )
            except Exception:
                # Background prewarm is strictly an optimization. A normal lcr
                # invocation retains the complete synchronous fallback path.
                pass

            latest = _read_request(root, repo)
            if latest is not None and latest.request_id != request_id:
                continue
            baseline = _read_binding_unscoped(_binding_path(root, repo))
            baseline_fingerprint = baseline.source_fingerprint if baseline is not None else ""
            wake_reason = _wait_for_out_of_band_change(
                root,
                repo,
                baseline_fingerprint=baseline_fingerprint,
                baseline_request_id=request_id,
                watch_idle_s=watch_idle_s,
                debounce_s=debounce_s,
                poll_interval_s=poll_interval_s,
            )
            if wake_reason:
                if wake_reason != "request":
                    _write_request(
                        root,
                        repo,
                        host=request.host,
                        session_id=request.session_id,
                    )
                continue

            # The final request check and worker unlock are one ownership handoff.
            # A writer either publishes before this check, in which case this
            # worker loops, or waits until worker.lock is free and its detached
            # replacement can acquire it. There is no check -> unlock lost-wakeup
            # window where both workers can leave a durable request stranded.
            with _request_handoff_lock(root, repo):
                latest = _read_request(root, repo)
                if latest is not None and latest.request_id != request_id:
                    continue
                _release_worker_lock(lock)
                lock = None
                return 0
        return 0
    finally:
        if lock is not None:
            _release_worker_lock(lock)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args(argv)
    if not args.worker:
        return 2
    try:
        watch_idle_s = float(os.environ.get("LEMONCROW_LIVE_REVIEW_WATCH_IDLE_S", str(_DEFAULT_WATCH_IDLE_S)))
    except ValueError:
        watch_idle_s = _DEFAULT_WATCH_IDLE_S
    return prepare_live_review_worker(
        Path(args.root),
        Path(args.repo_root),
        watch_idle_s=max(0.0, watch_idle_s),
    )


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "LiveReviewBinding",
    "LiveReviewClaimed",
    "LiveReviewRequest",
    "clear_live_review_binding",
    "live_review_publication_guard",
    "load_live_review_binding",
    "prepare_live_review_worker",
    "request_live_review_prepare",
    "write_live_review_binding",
]
