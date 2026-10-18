"""Thin hosted Review capture using only Git and the standard library.

The client freezes two exact source snapshots and uploads them through the same
View protocol used by MCP. It does not build a Review packet, run code analysis,
or import the full LemonCrow package; the configured server derives the
canonical packet from those immutable Views.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

if TYPE_CHECKING:
    from .review_delta import DeltaCursor

from .config import ClientConfig, load_config
from .errors import AgentAction, ClientError, ErrorCode
from .review_base import EMPTY_TREE_SHA, branch_review_failed, capture_review_base, parse_branch_review
from .review_session import ReviewSession

__all__ = ["ReviewRange", "resolve_review_range", "review_main"]

_GIT_TIMEOUT_S: Final[float] = 30.0
_INTERNAL_PATHS: Final[tuple[str, ...]] = (".claude/worktrees", ".lc-worktrees", ".lemoncrow")
#: Reviews of these ranges route by branch and keep the base recorded at their first capture.
_MUTABLE_MODES: Final[frozenset[str]] = frozenset({"working_tree", "staged"})
# Flags of the full Review CLI (`uv run lc review` in a source checkout) that this
# client does not carry; the Review Reader covers the same human workflow.
_FULL_CLI_REVIEW_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--mark",
        "--marks",
        "--comment",
        "--comments",
        "--on",
        "--comment-kind",
        "--since-my-review",
        "--feedback",
        "--finish",
        "--current",
        "--resume-author",
        "--pr",
        "--publish",
        "--repo-root",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewRange:
    mode: str
    base_rev: str
    head_rev: str
    base_sha: str
    head_sha: str
    merge_base_sha: str = ""
    dirty: bool = False
    title: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "base_rev": self.base_rev,
            "head_rev": self.head_rev,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "dirty": self.dirty,
            "title": self.title,
        }

    def source_ref(self, repo: Path) -> str:
        if self.mode != "commit_range":
            return f"client:{self.mode}"
        base = _canonical_named_ref(repo, self.base_rev)
        head = _canonical_named_ref(repo, self.head_rev)
        if base and head:
            relation = "..." if self.merge_base_sha else ".."
            return f"named:{base}{relation}{head}"
        return f"{self.base_sha}..{self.head_sha}"


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def _git_bytes(repo: Path, *arguments: str, required: bool = True, index_file: str = "") -> bytes:
    env = _git_env()
    if index_file:
        env["GIT_INDEX_FILE"] = index_file
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0 and required:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "git command failed"
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            suffix[:512],
            action=AgentAction.FIX_REQUEST,
        )
    return completed.stdout if completed.returncode == 0 else b""


def _git_text(repo: Path, *arguments: str, required: bool = True) -> str:
    return _git_bytes(repo, *arguments, required=required).decode("utf-8", errors="replace").strip()


def _commit(repo: Path, spec: str) -> str:
    return _git_text(repo, "rev-parse", "--verify", f"{spec}^{{commit}}")


def _canonical_named_ref(repo: Path, spec: str) -> str:
    """Canonical ref for a genuine branch/tag name, never HEAD/SHA/expressions."""

    if not spec or spec == "HEAD":
        return ""
    # --symbolic-full-name returns an empty string for raw object ids and
    # relative expressions such as HEAD~1. It also DWIM-resolves branch/tag
    # shorthand to the canonical refs/... spelling used by durable Review ids.
    value = _git_text(repo, "rev-parse", "--symbolic-full-name", spec, required=False)
    return value if value.startswith("refs/") else ""


def _head(repo: Path) -> str:
    return _commit(repo, "HEAD") if _git_bytes(repo, "rev-parse", "--verify", "HEAD^{commit}", required=False) else ""


def _dirty(repo: Path) -> bool:
    pathspec = ["--", ".", *[f":(exclude){path}" for path in _INTERNAL_PATHS]]
    return bool(
        _git_bytes(
            repo,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=dirty",
            *pathspec,
        )
    )


def _summary(repo: Path, sha: str, fallback: str) -> str:
    if not sha:
        return fallback
    value = _git_text(repo, "log", "-1", "--format=%s", sha, required=False)
    return value or fallback


def _missing_parent_is_shallow(repo: Path, head_sha: str) -> bool:
    raw = _git_bytes(repo, "cat-file", "-p", head_sha, required=False)
    return any(line.startswith(b"parent ") for line in raw.splitlines())


def resolve_review_range(
    repo: Path,
    rev: str | None = None,
    *,
    base: str | None = None,
    head: str | None = None,
    staged: bool = False,
    working_tree: bool = False,
) -> ReviewRange:
    """Resolve the same core Review range choices without pygit2."""

    repo = repo.resolve()
    if not (repo / ".git").exists() and not _git_bytes(repo, "rev-parse", "--git-dir", required=False):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"not a git repository: {repo}", action=AgentAction.FIX_REQUEST)
    head_sha = _head(repo)
    dirty = _dirty(repo)

    if staged:
        return ReviewRange(
            mode="staged",
            base_rev="HEAD" if head_sha else "(empty tree)",
            head_rev="INDEX",
            base_sha=head_sha,
            head_sha="",
            dirty=dirty,
            title="staged changes",
        )
    if working_tree:
        return ReviewRange(
            mode="working_tree",
            base_rev="HEAD" if head_sha else "(empty tree)",
            head_rev="WORKDIR",
            base_sha=head_sha,
            head_sha="",
            dirty=dirty,
            title="working tree",
        )
    if base is not None or head is not None:
        head_spec = head or "HEAD"
        resolved_head = _commit(repo, head_spec)
        base_spec = base if base is not None else f"{head_spec}~1"
        resolved_base = _commit(repo, base_spec)
        return ReviewRange(
            mode="commit_range",
            base_rev=base_spec,
            head_rev=head_spec,
            base_sha=resolved_base,
            head_sha=resolved_head,
            dirty=dirty,
            title=_summary(repo, resolved_head, head_spec),
        )
    if rev:
        if "..." in rev:
            left, _, right = rev.partition("...")
            left_spec, right_spec = left or "HEAD", right or "HEAD"
            left_sha, right_sha = _commit(repo, left_spec), _commit(repo, right_spec)
            merge_base = _git_text(repo, "merge-base", left_sha, right_sha, required=False)
            return ReviewRange(
                mode="commit_range",
                base_rev=left_spec,
                head_rev=right_spec,
                base_sha=merge_base or left_sha,
                head_sha=right_sha,
                merge_base_sha=merge_base,
                dirty=dirty,
                title=_summary(repo, right_sha, right_spec),
            )
        if ".." in rev:
            left, _, right = rev.partition("..")
            left_spec, right_spec = left or "HEAD", right or "HEAD"
            left_sha, right_sha = _commit(repo, left_spec), _commit(repo, right_spec)
            return ReviewRange(
                mode="commit_range",
                base_rev=left_spec,
                head_rev=right_spec,
                base_sha=left_sha,
                head_sha=right_sha,
                dirty=dirty,
                title=_summary(repo, right_sha, right_spec),
            )
        resolved = _commit(repo, rev)
        if not head_sha:
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "cannot resolve HEAD", action=AgentAction.FIX_REQUEST)
        merge_base = _git_text(repo, "merge-base", resolved, head_sha, required=False)
        return ReviewRange(
            mode="commit_range",
            base_rev=rev,
            head_rev="HEAD",
            base_sha=merge_base or resolved,
            head_sha=head_sha,
            merge_base_sha=merge_base,
            dirty=dirty,
            title=_summary(repo, head_sha, "HEAD"),
        )
    if dirty:
        return ReviewRange(
            mode="working_tree",
            base_rev="HEAD" if head_sha else "(empty tree)",
            head_rev="WORKDIR",
            base_sha=head_sha,
            head_sha="",
            dirty=True,
            title="working tree",
        )
    if not head_sha:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "cannot resolve HEAD", action=AgentAction.FIX_REQUEST)
    parent = _git_text(repo, "rev-parse", "--verify", "HEAD~1^{commit}", required=False)
    if not parent:
        if _missing_parent_is_shallow(repo, head_sha):
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "cannot resolve HEAD~1 because the clone is shallow; fetch more history or pass an explicit range",
                action=AgentAction.FIX_REQUEST,
            )
        return ReviewRange(
            mode="commit_range",
            base_rev="(empty tree)",
            head_rev="HEAD",
            base_sha="",
            head_sha=head_sha,
            dirty=False,
            title=_summary(repo, head_sha, "HEAD"),
        )
    return ReviewRange(
        mode="commit_range",
        base_rev="HEAD~1",
        head_rev="HEAD",
        base_sha=parent,
        head_sha=head_sha,
        dirty=False,
        title=_summary(repo, head_sha, "HEAD"),
    )


def _review_scope_label(rng: ReviewRange) -> str:
    if rng.mode == "working_tree":
        return f"{rng.base_rev or 'HEAD'} → working tree"
    if rng.mode == "staged":
        return f"{rng.base_rev or 'HEAD'} → staged index"
    return f"{rng.base_rev} → {rng.head_rev}"


def _clear_directory(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _materialize_commit(repo: Path, sha: str, target: Path) -> None:
    _clear_directory(target)
    if not sha:
        return
    # A private throwaway index: the commit's exact tree (modes, symlinks, and
    # export-ignore paths included) is written without touching the user's index
    # and without unpacking an archive.
    prefix = str(target.resolve()) + os.sep
    with tempfile.TemporaryDirectory(prefix="lemoncrow-review-index-") as scratch:
        index_file = str(Path(scratch) / "index")
        _git_bytes(repo, "read-tree", sha, index_file=index_file)
        _git_bytes(repo, "checkout-index", "--all", f"--prefix={prefix}", index_file=index_file)


def _materialize_index(repo: Path, target: Path) -> None:
    _clear_directory(target)
    prefix = str(target.resolve()) + os.sep
    index_raw = _git_text(repo, "rev-parse", "--git-path", "index")
    index_path = Path(index_raw)
    if not index_path.is_absolute():
        index_path = (repo / index_path).resolve()
    before = index_path.stat() if index_path.exists() else None
    _git_bytes(repo, "checkout-index", "--all", f"--prefix={prefix}")
    after = index_path.stat() if index_path.exists() else None
    if (
        before is not None
        and after is not None
        and (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size)
    ):
        raise ClientError(
            ErrorCode.VIEW_REVISION_STALE,
            "Git index changed while the staged Review snapshot was being frozen; retry",
            retryable=True,
            action=AgentAction.RETRY_LATER,
        )


def _parse_gitlink_rows(raw: bytes, *, tree: bool) -> dict[str, str]:
    rows: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            fields = metadata.decode("ascii").split()
            if tree:
                mode, _kind, oid = fields[:3]
            else:
                mode, oid, stage = fields[:3]
                if stage != "0":
                    continue
        except (ValueError, UnicodeDecodeError):
            continue
        if mode != "160000":
            continue
        path = path_raw.decode("utf-8", errors="surrogateescape")
        rows[path] = oid
    return rows


def _gitlinks_for_commit(repo: Path, sha: str) -> dict[str, str]:
    if not sha:
        return {}
    return _parse_gitlink_rows(_git_bytes(repo, "ls-tree", "-rz", "--full-tree", sha), tree=True)


def _gitlinks_for_index(repo: Path) -> dict[str, str]:
    return _parse_gitlink_rows(_git_bytes(repo, "ls-files", "--stage", "-z"), tree=False)


def _gitlinks_for_worktree(repo: Path) -> dict[str, str]:
    # The parent index is the starting pointer set. A checked-out submodule may
    # have advanced its own HEAD without the parent index being staged; libgit2's
    # working-tree diff reports that pointer move, so mirror it here. A missing
    # submodule directory is a working-tree deletion and is omitted.
    result: dict[str, str] = {}
    for path, index_oid in _gitlinks_for_index(repo).items():
        nested = repo / path
        if not nested.exists():
            continue
        oid = _git_text(repo, "-C", path, "rev-parse", "--verify", "HEAD^{commit}", required=False)
        result[path] = oid or index_oid
    return result


def _gitlinks_for_range(repo: Path, rng: ReviewRange) -> tuple[dict[str, str], dict[str, str]]:
    base = _gitlinks_for_commit(repo, rng.base_sha)
    if rng.mode == "working_tree":
        head = _gitlinks_for_worktree(repo)
    elif rng.mode == "staged":
        head = _gitlinks_for_index(repo)
    else:
        head = _gitlinks_for_commit(repo, rng.head_sha)
    return base, head


def _worktree_paths(repo: Path) -> tuple[str, ...]:
    raw = _git_bytes(
        repo,
        "ls-files",
        "-co",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        *[f":(exclude){path}" for path in _INTERNAL_PATHS],
    )
    return tuple(item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item)


def _materialize_worktree(repo: Path, target: Path) -> None:
    for _attempt in range(2):
        _clear_directory(target)
        initial_paths = _worktree_paths(repo)
        observations: dict[str, tuple[int, int, int, int, int]] = {}
        absent: set[str] = set()
        changed = False
        for relative in initial_paths:
            source = repo / relative
            try:
                before = source.lstat()
            except FileNotFoundError:
                # ``git ls-files -c`` deliberately includes a tracked path that
                # the working tree deleted. Preserve that deletion, then prove
                # below that the path stayed absent for the whole freeze.
                absent.add(relative)
                continue
            except OSError:
                changed = True
                break
            if not stat.S_ISREG(before.st_mode):
                continue
            try:
                payload = source.read_bytes()
                after = source.lstat()
            except OSError:
                changed = True
                break
            before_key = (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode)
            after_key = (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode)
            if before_key != after_key:
                changed = True
                break
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            destination.chmod(0o755 if before.st_mode & stat.S_IXUSR else 0o644)
            observations[relative] = after_key
        if not changed:
            for relative, expected in observations.items():
                try:
                    current = (repo / relative).lstat()
                except OSError:
                    changed = True
                    break
                actual = (current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_mode)
                if actual != expected:
                    changed = True
                    break
        if not changed:
            for relative in absent:
                if (repo / relative).exists() or (repo / relative).is_symlink():
                    changed = True
                    break
        if not changed and _worktree_paths(repo) != initial_paths:
            # Catches files created/deleted while existing files were copied.
            changed = True
        if not changed:
            return
    raise ClientError(
        ErrorCode.VIEW_REVISION_STALE,
        "working tree changed while the Review snapshot was being frozen; retry",
        retryable=True,
        action=AgentAction.RETRY_LATER,
    )


def _freeze_head(repo: Path, rng: ReviewRange, head: Path) -> None:
    if rng.mode == "working_tree":
        _materialize_worktree(repo, head)
    elif rng.mode == "staged":
        _materialize_index(repo, head)
    else:
        _materialize_commit(repo, rng.head_sha, head)


def _freeze_range(repo: Path, rng: ReviewRange, base: Path, head: Path) -> None:
    _materialize_commit(repo, rng.base_sha, base)
    _freeze_head(repo, rng, head)


def _workspace_branch(repo: Path) -> str:
    """Short name of the checked-out branch; ``""`` on a detached ``HEAD``, a paused rebase included."""

    ref = _git_text(repo, "symbolic-ref", "--quiet", "HEAD", required=False)
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    return branch if len(branch) <= 256 and "\n" not in branch and "\r" not in branch else ""


def _active_review_base(
    remote: ReviewSession, repo_id: str, *, branch: str, range_mode: str, hosted: bool, timeout_s: float
) -> tuple[str, str]:
    """``(review_base_sha, review_base_policy)`` of the branch's open Review, or ``("", "")``.

    One read keyed by the capture's subject (``BRANCH_REVIEW_PATH``); on
    hosted the subject includes the signed-in author, so another author's
    Review on the same branch name never answers. A local server from before
    the lookup can still route by branch but answer 404: using ``HEAD`` then
    would silently hide committed work. Refuse that capture and require a server
    upgrade instead. Every other failure, including hosted 404, fails closed.
    """

    try:
        response = remote.branch_review(repo_id, branch=branch, range_mode=range_mode, timeout_s=timeout_s)
        if response.status == 404 and not hosted:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "this local Review server cannot read the branch Review base; upgrade the server",
                action=AgentAction.UPGRADE_SERVER,
            )
        found = parse_branch_review(response.require(), range_mode=range_mode)
    except ClientError as exc:
        raise branch_review_failed(branch, exc) from exc
    return found.base_sha, found.policy


def _review_base(repo: Path, recorded: str, policy: str) -> str:
    """The base this capture diffs against: a commit id, ``EMPTY_TREE_SHA``, or ``""`` for ``HEAD``.

    ``fixed`` keeps the recorded base as it is; any other policy is
    ``integrated`` and advances it past integrated work. A recorded base this
    clone lacks is ``""`` under both.
    """

    if policy != "fixed":
        return capture_review_base(repo, recorded)
    if recorded == EMPTY_TREE_SHA:
        return recorded
    if not recorded or recorded.startswith("-"):
        return ""
    return _git_text(repo, "rev-parse", "--verify", "--quiet", f"{recorded}^{{commit}}", required=False)


def _with_review_base(rng: ReviewRange, base: str) -> ReviewRange:
    """*rng* re-based onto its Review's base; ``""`` keeps ``HEAD``."""

    if base == EMPTY_TREE_SHA:
        return replace(rng, base_rev="(empty tree)", base_sha="")
    if not base or base == rng.base_sha:
        return rng
    return replace(rng, base_rev=base, base_sha=base)


def _review_url(config: ClientConfig, remote: ReviewSession, payload: dict[str, object]) -> str:
    raw = payload.get("review")
    review = raw if isinstance(raw, dict) else {}
    path = review.get("review_path")
    if isinstance(path, str) and path.startswith("/r/"):
        return config.endpoint(path)
    identifier = review.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ClientError(ErrorCode.REVIEW_UNKNOWN, "server captured Review without an id", action=AgentAction.ABANDON)
    return remote.review_url(identifier)


def _capture(
    config: ClientConfig,
    rng: ReviewRange,
    *,
    with_impact: bool,
    restore_archived: bool,
    title: str = "",
    delta_cursor: DeltaCursor | None = None,
) -> tuple[dict[str, object], str]:
    config.review_state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=config.review_state_dir) as temporary:
        root = Path(temporary)
        base_root, head_root = root / "base", root / "head"
        if delta_cursor is None:
            _freeze_head(config.repo_root, rng, head_root)
        else:
            from .review_delta import snapshot

            frozen_source = (
                ":index:" if rng.mode == "staged" else rng.head_sha if rng.mode == "commit_range" else rng.base_sha
            )
            head_snapshot = snapshot(
                config.repo_root,
                frozen_source,
                delta_cursor,
                working_tree=rng.mode == "working_tree",
            )
            frozen_gitlinks = _gitlinks_for_range(config.repo_root, rng)
            frozen_branch = _workspace_branch(config.repo_root) if rng.mode in _MUTABLE_MODES else ""
            frozen_head = _git_text(config.repo_root, "rev-parse", "HEAD", required=False)
        remote = ReviewSession(config)
        try:
            remote.handshake(timeout_s=config.request_timeout_s)
            remote.open_session(timeout_s=config.request_timeout_s)
            manifest = remote.state.review_manifest
            # A working-tree or staged Review routes by branch. A server that does
            # not route by branch is sent none and has no branch base to read:
            # HEAD stays the base, as before.
            branch = (
                _workspace_branch(config.repo_root)
                if rng.mode in _MUTABLE_MODES
                and manifest is not None
                and "workspace_branch" in manifest.capture_options
                else ""
            )
            capture_intent = ""

            def opened_head_view(opened: Mapping[str, object]) -> None:
                nonlocal capture_intent
                repo = opened.get("repo_id")
                if branch and isinstance(repo, str) and repo:
                    capture_intent = remote.begin_capture_intent(repo, branch=branch, mode=rng.mode)

            # The head View opens first: it supplies the repo identity for both
            # the branch ticket and base lookup. In delta mode, obtain the ticket
            # *before uploading bytes*, so slow first uploads cannot overtake a
            # later source revision that already started on another machine.
            if delta_cursor is None:
                head_opened = remote.open_snapshot(
                    head_root,
                    source_revision=rng.head_sha or rng.base_sha or f"review-{rng.mode}",
                    timeout_s=config.tool_timeout_s,
                )
                opened_head_view(dict(head_opened))
            else:
                head_opened = remote.open_delta_snapshot(
                    head_snapshot.entries,
                    source_revision=rng.head_sha or rng.base_sha or f"review-{rng.mode}",
                    payload=head_snapshot.payload,
                    on_open=opened_head_view,
                    timeout_s=config.tool_timeout_s,
                )
            head_view = (remote.state.view_id, remote.state.view_revision)
            repo_id = head_opened.get("repo_id")
            if not isinstance(repo_id, str) or not repo_id:
                raise ClientError(ErrorCode.VIEW_UNKNOWN, "server opened Review View without repository id")
            if branch:
                recorded, policy = _active_review_base(
                    remote,
                    repo_id,
                    branch=branch,
                    range_mode=rng.mode,
                    hosted=config.hosted,
                    timeout_s=config.request_timeout_s,
                )
                if recorded:
                    rng = _with_review_base(rng, _review_base(config.repo_root, recorded, policy))
            if delta_cursor is None:
                _materialize_commit(config.repo_root, rng.base_sha, base_root)
                base_opened = remote.open_snapshot(
                    base_root,
                    source_revision=rng.base_sha or "review-empty-base",
                    timeout_s=config.tool_timeout_s,
                )
            else:
                base_snapshot = snapshot(config.repo_root, rng.base_sha, delta_cursor)
                base_opened = remote.open_delta_snapshot(
                    base_snapshot.entries,
                    source_revision=rng.base_sha or "review-empty-base",
                    payload=base_snapshot.payload,
                    timeout_s=config.tool_timeout_s,
                )
            # The capture names the head View; the base View is named in its body.
            remote.state.view_id, remote.state.view_revision = head_view
            base_gitlinks, head_gitlinks = _gitlinks_for_range(config.repo_root, rng)
            if delta_cursor is not None and rng.mode in _MUTABLE_MODES:
                # A slow upload may race new local edits/checkout/HEAD movement.
                # Do not publish the previous View as the live branch after the
                # source has already moved, even before the watch-loop retries.
                current = snapshot(
                    config.repo_root,
                    frozen_source,
                    delta_cursor,
                    working_tree=rng.mode == "working_tree",
                )
                if (
                    current.entries != head_snapshot.entries
                    or _git_text(config.repo_root, "rev-parse", "HEAD", required=False) != frozen_head
                    or _workspace_branch(config.repo_root) != frozen_branch
                    or (base_gitlinks, head_gitlinks) != frozen_gitlinks
                ):
                    raise ClientError(
                        ErrorCode.VIEW_REVISION_STALE,
                        "source changed before Review publication; rescan and retry",
                        retryable=True,
                    )
            response = dict(
                remote.capture_from_views(
                    repo_id=repo_id,
                    source_ref=rng.source_ref(config.repo_root),
                    title=title or rng.title,
                    base_view_id=str(base_opened.get("view_id") or ""),
                    base_view_revision=int(base_opened.get("view_revision") or 0),
                    range_spec=rng.to_wire(),
                    gitlinks={"base": base_gitlinks, "head": head_gitlinks},
                    with_impact=with_impact,
                    restore_archived=restore_archived,
                    workspace_branch=branch,
                    capture_intent=capture_intent,
                    timeout_s=config.tool_timeout_s,
                )
            )
            url = _review_url(config, remote, response)
            if delta_cursor is not None:
                delta_cursor.save()
                response["_delta_upload"] = {
                    "head_uploaded_bytes": int(head_opened.get("uploaded_bytes") or 0),
                    "base_uploaded_bytes": int(base_opened.get("uploaded_bytes") or 0),
                }
            return response, url
        finally:
            remote.close()


def review_main(arguments: list[str], *, sink: TextIO) -> int:
    """Thin ``review`` CLI. Capture first; richer terminal actions follow later."""

    if arguments and arguments[0] == "service":
        from .review_service import service_main

        return service_main(arguments[1:], sink=sink)
    if arguments and arguments[0] == "hook":
        from .review_hooks import hook_main

        return hook_main(arguments[1:], sink=sink)

    if arguments and arguments[0] == "connect":
        from .review_device_connect import connect_main

        return connect_main(arguments[1:], sink=sink)

    # Public Review commands stay directly under `lcr`. The older companion
    # namespace is a compatibility alias, not a distinct product or daemon.
    if arguments and arguments[0] in {"pending", "device", "task", "companion"}:
        from .review_companion_cli import companion_main

        command, rest = arguments[0], arguments[1:]
        if command == "pending":
            return companion_main(["pending", *rest], sink=sink)
        if command == "device":
            if not rest or rest[0] not in {"status", "check", "online", "serve"}:
                sink.write("Usage: lcr device status | check | online [--once] | serve [--once]\n")
                return 0 if rest in ([], ["--help"], ["help"]) else 2
            if rest[0] == "serve":
                from .review_service import serve_main

                return serve_main(rest[1:], sink=sink)
            return companion_main(rest, sink=sink)
        if command == "task":
            if not rest or rest[0] != "run":
                sink.write("Usage: lcr task run JOB_ID [options]\n")
                return 0 if rest in ([], ["--help"], ["help"]) else 2
            return companion_main(rest, sink=sink)
        return companion_main(rest, sink=sink)

    if arguments and arguments[0] == "watch":
        from .review_watch import watch_main

        return watch_main(arguments[1:], sink=sink)

    if arguments and arguments[0] in {"-h", "--help", "help"}:
        sink.write(
            "LemonCrow Review — freeze the code you want reviewed and open the Review Reader.\n\n"
            "Usage:\n"
            "  lcr [REV] [OPTIONS]\n"
            "  lc review [REV] [OPTIONS]\n\n"
            "Common flows:\n"
            "  lcr                 Review working-tree changes; if clean, review the last commit\n"
            "  lcr --staged        Review exactly what is staged\n"
            "  lcr watch           Keep local or hosted Review current as source settles\n"
            "  lcr connect         Pair an optional Hosted Review machine with a one-time code\n"
            "  lcr pending         Show paired Hosted Review work waiting on this machine\n"
            "  lcr pending --watch Receive scoped work hints over WSS (HTTPS fallback)\n"
            "  lcr device check    Verify hosted connection and pinned worker signer\n"
            "  lcr device online   Announce presence without executing jobs\n"
            "  lcr task run JOB_ID Run one explicitly approved AI task with a local spend cap\n"
            "  lcr device serve    Foreground Review capture + machine presence on VPS/laptop\n"
            "  lcr service install Optional user-owned Review capture service (explicit opt-in)\n"
            "  lcr hook after-edit|after-checkpoint|on-exit  Agent checkpoint capture\n"
            "  lcr main...HEAD     Review the branch change from its merge base with main\n\n"
            "Range options:\n"
            "  --working-tree      Review current working-tree changes\n"
            "  --staged            Review staged changes\n"
            "  --base REV          Explicit base revision\n"
            "  --head REV          Explicit head revision (default HEAD)\n\n"
            "Review options:\n"
            "  --title TITLE       Set the Review title\n"
            "  --no-impact         Skip impact analysis\n"
            "  --open              Open the Review Reader (default)\n"
            "  --no-open           Capture without opening a browser\n"
            "  --reopen-review     Restore an archived matching Review\n"
            "  --json              Emit machine-readable output\n\n"
            "Hosted Review requires `lc auth login` once before first use.\n"
        )
        return 0
    rev: str | None = None
    base: str | None = None
    head: str | None = None
    staged = False
    working_tree = False
    with_impact = True
    open_browser = True
    restore_archived = False
    title = ""
    as_json = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--working-tree":
            working_tree = True
        elif argument == "--staged":
            staged = True
        elif argument == "--no-impact":
            with_impact = False
        elif argument == "--no-open":
            open_browser = False
        elif argument == "--open":
            open_browser = True
        elif argument == "--reopen-review":
            restore_archived = True
        elif argument == "--json":
            as_json = True
        elif argument in {"--base", "--head", "--title"}:
            index += 1
            if index >= len(arguments):
                sink.write(f"{argument} requires a value\n")
                return 2
            if argument == "--base":
                base = arguments[index]
            elif argument == "--head":
                head = arguments[index]
            else:
                title = arguments[index].strip()
                if not title or len(title) > 120 or any(ord(char) < 32 for char in title):
                    sink.write("--title must be a non-empty single-line title of at most 120 characters\n")
                    return 2
        elif argument.startswith("-"):
            sink.write(f"unknown review option: {argument}\n")
            if argument.partition("=")[0] in _FULL_CLI_REVIEW_OPTIONS:
                sink.write(
                    "That option belongs to the full Review CLI in a LemonCrow source checkout (`uv run lc review`).\n"
                    "With this client, mark, comment, see changes since your review and copy feedback"
                    " in the Review Reader: `lc review --open`.\n"
                )
            sink.write("Run `lc review --help` for the options this client supports.\n")
            return 2
        elif rev is None:
            rev = argument
        else:
            sink.write("review accepts at most one revision expression\n")
            return 2
        index += 1

    try:
        config = load_config(product="review")
        if config.hosted and not config.authenticated:
            raise ClientError(
                ErrorCode.UNAUTHENTICATED,
                "Hosted Review requires sign-in; run `lc auth login` first",
                action=AgentAction.REAUTHENTICATE,
            )
        rng = resolve_review_range(
            config.repo_root,
            rev,
            base=base,
            head=head,
            staged=staged,
            working_tree=working_tree,
        )
        if not as_json:
            sink.write(f"Review scope  {_review_scope_label(rng)}\n")
        response, url = _capture(config, rng, with_impact=with_impact, restore_archived=restore_archived, title=title)
    except ClientError as exc:
        sink.write(f"{exc.server_code}: {exc.message}\n")
        return 1

    if as_json:
        import json

        sink.write(json.dumps({"review_url": url, **response}, sort_keys=True) + "\n")
    else:
        sink.write(f"Review  {url}\n")
        review_value = response.get("review")
        review_row = review_value if isinstance(review_value, dict) else {}
        kept = str(review_row.get("title") or "")
        # A hosted server titles only a Review its capture creates; a local one
        # keeps the title as this Snapshot's author title (``auto_title``).
        if title and title not in {kept, str(review_row.get("auto_title") or "")}:
            sink.write(f'--title was not applied; the Review this Snapshot joined keeps the title "{kept}"\n')
    if open_browser:
        _arm_local_browser(config, url)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return 0


def _arm_local_browser(config: ClientConfig, url: str) -> None:
    """Arm the local server's browser pairing for *url* (best effort).

    The local Reader mints its browser capability only inside this short
    CLI-armed window; without it the page asks the user to reconnect.
    """
    if config.hosted or not config.token:
        return
    from urllib.parse import urlsplit

    from .session import RemoteSession

    try:
        remote = RemoteSession(config)
        try:
            remote.pair_local_review_browser(urlsplit(url).path or "/", timeout_s=2.0)
        finally:
            remote.close()
    except (ClientError, OSError, ValueError):
        return
