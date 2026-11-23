"""Standalone, foreground Review capture watcher; no Runtime/MCP dependency.

P1 watches source identity and submits settled revisions through the *existing*
Review capture protocol. A Review-only Git-backed delta manifest and durable
digest cursor negotiate missing blobs without copying the whole source tree.
The server remains the authority on accepted content and Snapshot revisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO

from .config import ClientConfig, load_config
from .errors import ClientError, ErrorCode
from .review import _INTERNAL_PATHS, _capture, _git_bytes, _head, _workspace_branch, resolve_review_range
from .review_delta import DeltaCursor, PublisherLock

__all__ = ["ReviewWatch", "SourceState", "source_state", "watch_main"]

_POLL_DEFAULT: Final[float] = 1.0
_SETTLE_DEFAULT: Final[float] = 2.0
_RETRY_MAX: Final[float] = 60.0
_PATHSPEC: Final[tuple[str, ...]] = ("--", ".", *[f":(exclude){path}" for path in _INTERNAL_PATHS])


@dataclass(frozen=True, slots=True)
class SourceState:
    fingerprint: str
    changed: bool


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _file_identity(repo: Path, relative: bytes, digest: _Digest) -> None:
    """Fingerprint changed files without following symlinks outside the checkout."""
    # Git emits undecoded filenames with -z. os.fsdecode keeps surrogate escapes.
    name = os.fsdecode(relative)
    path = repo / name
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing")
        return
    digest.update(str(stat.S_IFMT(info.st_mode)).encode("ascii"))
    digest.update(str(info.st_mode & 0o111).encode("ascii"))
    if stat.S_ISLNK(info.st_mode):
        digest.update(os.fsencode(os.readlink(path)))
    elif stat.S_ISREG(info.st_mode):
        # Defense in depth: malformed nested symlinks must not read outside repo.
        if not path.resolve().is_relative_to(repo.resolve()):
            digest.update(b"outside-repository")
            return
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    else:
        digest.update(b"special-file")


def source_state(repo: Path, *, staged: bool = False) -> SourceState:
    """Exact changed-content identity for coalescing; does not upload content.

    A Git status string alone is not sufficient: rewriting an already-modified
    file often leaves its status at `` M``. For working-tree mode we hash the
    bytes of modified/untracked files, plus staged object identities, and the
    branch/HEAD. Git-ignored and LemonCrow internal paths are excluded.
    """
    repo = repo.resolve()
    if not _git_bytes(repo, "rev-parse", "--git-dir", required=False):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"not a git repository: {repo}")
    digest = hashlib.sha256()
    head = _head(repo)
    branch = _workspace_branch(repo)
    digest.update(b"review-watch-v1\0")
    digest.update(head.encode("ascii"))
    digest.update(b"\0")
    digest.update(branch.encode("utf-8", "surrogateescape"))
    digest.update(b"\0staged\0" if staged else b"\0working-tree\0")
    if staged:
        # Cached index object IDs are exact even when the worktree has since
        # diverged. Don't hash working-tree bytes in staged-only mode.
        status = _git_bytes(repo, "diff", "--cached", "--raw", "--no-ext-diff", "--abbrev=40", *_PATHSPEC)
        digest.update(status)
        return SourceState(digest.hexdigest(), bool(status))

    status = _git_bytes(
        repo, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=dirty", *_PATHSPEC
    )
    digest.update(status)
    if status:
        # Modified tracked files, including entries differing from the index.
        # Staged-only additions must also be included: they might not appear
        # in `ls-files --modified` when the worktree equals the index.
        modified = _git_bytes(repo, "ls-files", "--modified", "--deleted", "-z", *_PATHSPEC)
        untracked = _git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z", *_PATHSPEC)
        staged_names = _git_bytes(repo, "diff", "--cached", "--name-only", "-z", *_PATHSPEC)
        files = sorted(set(b"\0".join((modified, untracked, staged_names)).split(b"\0")) - {b""})
        for relative in files:
            _file_identity(repo, relative, digest)
    return SourceState(digest.hexdigest(), bool(status))


class ReviewWatch:
    """Deterministic settle/dedupe/backoff gate; time and capture are injected."""

    def __init__(self, settle_s: float = _SETTLE_DEFAULT) -> None:
        self.settle_s = settle_s
        self._last_seen: str | None = None
        self._seen_at: float = 0.0
        self._last_captured: str | None = None
        self._retry_after: float = 0.0
        self._failures = 0

    def consider(self, state: SourceState, now: float, capture: Callable[[], str]) -> tuple[str, str]:
        """Return (status, URL/message). No false claim of a successful upload."""
        if state.fingerprint != self._last_seen:
            self._last_seen = state.fingerprint
            self._seen_at = now
            self._retry_after = now
            self._failures = 0
        if self._last_captured == state.fingerprint:
            return "unchanged", ""
        # On a clean initial checkout, do not create an empty Review. After the
        # first capture, a commit/checkout to clean HEAD must still advance it.
        if not state.changed and self._last_captured is None:
            return "idle", ""
        if now - self._seen_at < self.settle_s:
            return "settling", ""
        if now < self._retry_after:
            return "backoff", ""
        try:
            url = capture()
        except (ClientError, OSError, subprocess.SubprocessError) as exc:
            self._failures += 1
            self._retry_after = now + min(_RETRY_MAX, float(2 ** min(self._failures + 1, 6)))
            return "failed", f"{type(exc).__name__}: {exc} (retry scheduled)"
        self._last_captured = state.fingerprint
        self._retry_after = 0.0
        self._failures = 0
        return "captured", url


def _positive_number(raw: str, *, name: str, minimum: float, maximum: float) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} seconds")
    return value


def watch_main(
    arguments: list[str],
    *,
    sink: TextIO,
    on_poll: Callable[[], None] | None = None,
    config_override: ClientConfig | None = None,
) -> int:
    """`lcr watch`: explicit foreground lifetime, same hosted Review credentials."""
    if arguments and arguments[0] in ("--help", "-h", "help"):
        sink.write(
            "Usage: lcr watch [--staged] [--once] [--include-clean-head] [--json] [--no-impact] "
            "[--interval SECONDS] [--settle SECONDS]\n"
            "Continuously capture settled changes without Workspaces/MCP. "
            "Ctrl-C stops; no background service is installed.\n"
        )
        return 0
    staged, once, with_impact, as_json, include_clean_head = False, False, True, False, False
    interval, settle = _POLL_DEFAULT, _SETTLE_DEFAULT
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--staged":
            staged = True
        elif argument == "--once":
            once = True
        elif argument == "--include-clean-head":
            include_clean_head = True
        elif argument == "--json":
            as_json = True
        elif argument == "--no-impact":
            with_impact = False
        elif argument in ("--interval", "--settle"):
            index += 1
            if index >= len(arguments):
                sink.write(f"{argument} requires seconds\n")
                return 2
            try:
                number = _positive_number(
                    arguments[index],
                    name=argument,
                    minimum=0.2 if argument == "--interval" else 0.0,
                    maximum=60.0 if argument == "--interval" else 120.0,
                )
            except ValueError as exc:
                sink.write(f"{exc}\n")
                return 2
            if argument == "--interval":
                interval = number
            else:
                settle = number
        else:
            sink.write(f"unknown watch option: {argument}\n")
            return 2
        index += 1
    if include_clean_head and (not once or staged):
        sink.write("--include-clean-head is only valid with --once without --staged\n")
        return 2
    publisher: PublisherLock | None = None
    presence_close: Callable[[], None] | None = None
    try:
        config = config_override if config_override is not None else load_config(product="review")
        if config.hosted and not config.authenticated:
            sink.write("Hosted Review requires sign-in; run `lc auth login` first\n")
            return 1
        if on_poll is None and config.hosted:
            # Paired machines can report presence *through the same lcr watch*.
            # Unpaired Hosted Review is still a usable capture-only client.
            # Local OSS never reads paired credentials or calls hosted routes.
            from .review_companion_cli import optional_hosted_watch_presence

            try:
                paired = optional_hosted_watch_presence(config)
            except (ClientError, OSError, TimeoutError, ValueError):
                if as_json:
                    sink.write(json.dumps({"event": "connection_refused"}) + "\n")
                else:
                    sink.write("Review machine connection refused. Check pairing, access and signing key.\n")
                return 1
            if paired is not None:
                on_poll = paired.check_in
                presence_close = paired.close
        repo = config.repo_root
        state_dir = getattr(config, "review_state_dir", repo.parent / ".review-test-state")
        publisher = PublisherLock(repo, state_dir)
        publisher.acquire()
        cursor = DeltaCursor.load(repo, state_dir)

        def require_companion_presence() -> bool:
            if on_poll is None:
                return True
            try:
                on_poll()
            except (ClientError, OSError, TimeoutError, ValueError):
                # Never print machine bearer, remote response or pairing secret.
                # Revoked/expired machines may not silently keep capturing as
                # an "online" companion under a different human credential.
                if as_json:
                    sink.write(json.dumps({"event": "connection_refused"}) + "\n")
                else:
                    sink.write("Review machine check-in refused; capture stopped. Check pairing and access.\n")
                return False
            return True

        if once:
            if not require_companion_presence():
                return 1
            snapshot = source_state(repo, staged=staged)
            if not snapshot.changed and not include_clean_head:
                if as_json:
                    sink.write(json.dumps({"event": "idle", "changed": False}) + "\n")
                else:
                    sink.write("No staged changes.\n" if staged else "No working-tree changes.\n")
                return 0
            # Explicit checkpoint hooks may arrive after Git commit, when the
            # working tree is clean. Capture HEAD against its parent instead of
            # pretending no source changed. Ordinary lcr watch stays dirty-only.
            rng = (
                resolve_review_range(repo)
                if not snapshot.changed
                else resolve_review_range(repo, staged=staged, working_tree=not staged)
            )
            _, url = _capture(config, rng, with_impact=with_impact, restore_archived=False, delta_cursor=cursor)
            if source_state(repo, staged=staged).fingerprint != snapshot.fingerprint:
                raise ClientError(
                    ErrorCode.VIEW_REVISION_STALE,
                    "worktree changed during Review publish; new capture is required",
                    retryable=True,
                )
            if as_json:
                sink.write(
                    json.dumps({"event": "captured", "review_url": url, "source_fingerprint": snapshot.fingerprint})
                    + "\n"
                )
            else:
                sink.write(f"Review  {url}\n")
            return 0
        watch = ReviewWatch(settle_s=settle)
        if as_json:
            sink.write(json.dumps({"event": "watch_started", "mode": "staged" if staged else "working_tree"}) + "\n")
        else:
            sink.write(f"Watching {repo} ({'staged' if staged else 'working tree'}); Ctrl-C to stop.\n")
            if presence_close is not None:
                sink.write("Paired machine check-ins enabled; no agent or verification jobs run.\n")
        sink.flush()
        while True:
            if not require_companion_presence():
                return 1
            try:
                current = source_state(repo, staged=staged)
                observed_fingerprint = current.fingerprint

                def capture(expected: str = observed_fingerprint) -> str:
                    rng = resolve_review_range(repo, staged=staged, working_tree=not staged)
                    _, url = _capture(config, rng, with_impact=with_impact, restore_archived=False, delta_cursor=cursor)
                    if source_state(repo, staged=staged).fingerprint != expected:
                        raise ClientError(
                            ErrorCode.VIEW_REVISION_STALE,
                            "worktree changed during Review publish; next state must be captured",
                            retryable=True,
                        )
                    return url

                outcome, detail = watch.consider(current, time.monotonic(), capture)
                if outcome == "captured":
                    if as_json:
                        sink.write(
                            json.dumps(
                                {"event": "captured", "review_url": detail, "source_fingerprint": current.fingerprint}
                            )
                            + "\n"
                        )
                    else:
                        sink.write(f"Review updated  {detail}\n")
                    sink.flush()
                elif outcome == "failed":
                    if as_json:
                        sink.write(json.dumps({"event": "capture_failed", "detail": detail}) + "\n")
                    else:
                        sink.write(f"Review capture failed: {detail}\n")
                    sink.flush()
            except (ClientError, OSError, subprocess.SubprocessError) as exc:
                if as_json:
                    sink.write(json.dumps({"event": "source_error", "detail": f"{type(exc).__name__}: {exc}"}) + "\n")
                else:
                    sink.write(f"Review watch cannot inspect checkout: {type(exc).__name__}: {exc}\n")
                sink.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sink.write(json.dumps({"event": "watch_stopped"}) + "\n" if as_json else "Review watch stopped.\n")
        return 0
    except (ClientError, OSError, subprocess.SubprocessError) as exc:
        sink.write(f"Review watch failed: {type(exc).__name__}: {exc}\n")
        return 1
    finally:
        if publisher is not None:
            publisher.close()
        if presence_close is not None:
            presence_close()
