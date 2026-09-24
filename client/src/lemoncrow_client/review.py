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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from .config import ClientConfig, load_config
from .errors import AgentAction, ClientError, ErrorCode
from .session import RemoteSession

__all__ = ["ReviewRange", "resolve_review_range", "review_main"]

_GIT_TIMEOUT_S: Final[float] = 30.0
_INTERNAL_PATHS: Final[tuple[str, ...]] = (".lc-worktrees", ".lemoncrow")


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


def _freeze_range(repo: Path, rng: ReviewRange, base: Path, head: Path) -> None:
    _materialize_commit(repo, rng.base_sha, base)
    if rng.mode == "working_tree":
        _materialize_worktree(repo, head)
    elif rng.mode == "staged":
        _materialize_index(repo, head)
    else:
        _materialize_commit(repo, rng.head_sha, head)


def _review_url(config: ClientConfig, remote: RemoteSession, payload: dict[str, object]) -> str:
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
    config: ClientConfig, rng: ReviewRange, *, with_impact: bool, restore_archived: bool
) -> tuple[dict[str, object], str]:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="review-", dir=config.state_dir) as temporary:
        root = Path(temporary)
        base_root, head_root = root / "base", root / "head"
        _freeze_range(config.repo_root, rng, base_root, head_root)
        remote = RemoteSession(config)
        try:
            remote.handshake(timeout_s=config.request_timeout_s)
            remote.open_session(timeout_s=config.request_timeout_s)
            base_opened = remote.open_review_snapshot(
                base_root,
                source_revision=rng.base_sha or "review-empty-base",
                timeout_s=config.tool_timeout_s,
            )
            head_opened = remote.open_review_snapshot(
                head_root,
                source_revision=rng.head_sha or rng.base_sha or f"review-{rng.mode}",
                timeout_s=config.tool_timeout_s,
            )
            repo_id = head_opened.get("repo_id")
            if not isinstance(repo_id, str) or not repo_id:
                raise ClientError(ErrorCode.VIEW_UNKNOWN, "server opened Review View without repository id")
            base_gitlinks, head_gitlinks = _gitlinks_for_range(config.repo_root, rng)
            response = dict(
                remote.capture_review_from_views(
                    repo_id=repo_id,
                    source_ref=rng.source_ref(config.repo_root),
                    title=rng.title,
                    base_view_id=str(base_opened.get("view_id") or ""),
                    base_view_revision=int(base_opened.get("view_revision") or 0),
                    range_spec=rng.to_wire(),
                    gitlinks={"base": base_gitlinks, "head": head_gitlinks},
                    with_impact=with_impact,
                    restore_archived=restore_archived,
                    timeout_s=config.tool_timeout_s,
                )
            )
            return response, _review_url(config, remote, response)
        finally:
            remote.close()


def review_main(arguments: list[str], *, sink: TextIO) -> int:
    """Thin ``review`` CLI. Capture first; richer terminal actions follow later."""

    if arguments and arguments[0] in {"-h", "--help", "help"}:
        sink.write(
            "usage: lemoncrow-client review [REV] [--working-tree|--staged] [--base REV] [--head REV]\n"
            "                               [--no-impact] [--no-open] [--reopen-review] [--json]\n"
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
        elif argument in {"--base", "--head"}:
            index += 1
            if index >= len(arguments):
                sink.write(f"{argument} requires a value\n")
                return 2
            if argument == "--base":
                base = arguments[index]
            else:
                head = arguments[index]
        elif argument.startswith("-"):
            sink.write(f"unknown review option: {argument}\n")
            return 2
        elif rev is None:
            rev = argument
        else:
            sink.write("review accepts at most one revision expression\n")
            return 2
        index += 1

    try:
        config = load_config()
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
        response, url = _capture(config, rng, with_impact=with_impact, restore_archived=restore_archived)
    except ClientError as exc:
        sink.write(f"{exc.server_code}: {exc.message}\n")
        return 1

    if as_json:
        import json

        sink.write(json.dumps({"review_url": url, **response}, sort_keys=True) + "\n")
    else:
        sink.write(f"Review  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return 0
