#!/usr/bin/env python3
"""Incremental, history-preserving git mirror: source branch → public repo.

Includes only paths listed in release/public-paths.txt (allowlist) from every
commit tree via git plumbing -- no squash, no wipe, real history preserved.

State (two refs, pushed to `origin`/lemoncrow-dev so any checkout can pick them up):
  refs/mirror/last      -- last mirrored source SHA (watermark)
  refs/mirror/last-pub  -- corresponding public SHA we created last run

Incrementality:
  - First run  : replays ALL commits from root, force-pushes public repo.
  - Later runs : only new commits since watermark, always fast-forward.

Usage:
    uv run python scripts/mirror.py [options]

Options:
    --dry-run           Print plan without pushing or updating watermark
    --source-ref REF    Branch to mirror (default: HEAD)
    --since DEV:PUB     Override watermark as dev_sha:pub_sha (both local)
    --public-remote URL Override public repo URL
    --force             Force-push (use after rebase / history rewrite)
    --verbose           Print one line per commit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PUBLIC_PATHS_FILE = REPO_ROOT / "release" / "public-paths.txt"
DEFAULT_SOURCE_REF = "HEAD"
MIRROR_DEV_TAG = "refs/mirror/last"  # watermark: last mirrored source SHA
MIRROR_PUB_TAG = "refs/mirror/last-pub"  # public SHA created by last run
DEFAULT_PUBLIC_REMOTE = "https://github.com/lemoncrowhq/lemoncrow.git"
DEV_REMOTE = "origin"  # lemoncrow-dev -- where the watermark refs live

# Files injected into the public repo that don't exist in the dev repo's public paths.
# Each entry is (source_path_in_dev_repo, dest_path_in_public_tree).
INJECTED_FILES: list[tuple[str, str]] = [
    ("release/lemoncrow-release.yml", ".github/workflows/release.yml"),
]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.run(cmd, check=True, text=True, capture_output=True, cwd=REPO_ROOT, **kwargs)


def git(*args: str) -> str:
    return _run(["git", *args]).stdout.strip()


def git_ok(*args: str) -> str | None:
    """Like git() but returns None on non-zero exit."""
    r = subprocess.run(["git", *args], text=True, capture_output=True, cwd=REPO_ROOT)
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# Public-path allowlist filtering
# ---------------------------------------------------------------------------


def load_public_prefixes() -> list[str]:
    prefixes = []
    for line in PUBLIC_PATHS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            prefixes.append(line.rstrip("/"))
    return prefixes


def is_public(path: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------


def get_blob_sha(commit_sha: str, path: str) -> str | None:
    """Return the blob SHA for a file at a given commit, or None if absent."""
    result = subprocess.run(
        ["git", "ls-tree", commit_sha, path],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    meta, _, _ = result.stdout.strip().partition("\t")
    _, _, sha = meta.split()
    return sha


def _index_env(index_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _update_index_info(index_path: Path, lines: list[str]) -> None:
    """Batch-add/update entries in the scratch index (one subprocess call, any count)."""
    if not lines:
        return
    subprocess.run(
        ["git", "update-index", "--index-info"],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env=_index_env(index_path),
    )


def _force_remove(index_path: Path, paths: list[str]) -> None:
    """Batch-remove paths from the scratch index (one subprocess call, any count)."""
    if not paths:
        return
    subprocess.run(
        ["git", "update-index", "--force-remove", "--stdin"],
        input="\n".join(paths) + "\n",
        text=True,
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env=_index_env(index_path),
    )


def _write_tree(index_path: Path) -> str:
    result = subprocess.run(
        ["git", "write-tree"],
        text=True,
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env=_index_env(index_path),
    )
    return result.stdout.strip()


def build_filtered_tree_full(commit_sha: str, public_prefixes: list[str], index_path: Path) -> str:
    """Full build: seed the scratch index from `commit_sha`'s tree, filtered to the
    public allowlist, then write it out.

    O(files) -- a single `update-index --index-info` + `write-tree` call. Tree
    hierarchy is reconstructed by git itself in one process, not by shelling out
    to `git mktree` once per directory (that fan-out -- ~3500 subprocess calls
    for this repo -- was the actual cost of every previous `make mirror` run).
    """
    if index_path.exists():
        index_path.unlink()
    ls = _run(["git", "ls-tree", "-r", "--full-tree", commit_sha]).stdout
    lines = []
    for line in ls.splitlines():
        meta, _, path = line.partition("\t")
        mode, _obj_type, sha = meta.split()
        if is_public(path, public_prefixes):
            lines.append(f"{mode} {sha} 0\t{path}")
    for src_path, dest_path in INJECTED_FILES:
        blob_sha = get_blob_sha(commit_sha, src_path)
        if blob_sha:
            lines.append(f"100644 {blob_sha} 0\t{dest_path}")
    _update_index_info(index_path, lines)
    return _write_tree(index_path)


def seed_index_from_tree(index_path: Path, tree_sha: str) -> None:
    """Seed the scratch index from an already-filtered public tree (e.g. last run's
    public HEAD), instead of rebuilding from the source tree. Lets an incremental
    run resume without ever re-scanning the full repo.
    """
    if index_path.exists():
        index_path.unlink()
    subprocess.run(
        ["git", "read-tree", tree_sha],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_index_env(index_path),
    )


def _diff_add_remove(prev_dev_sha: str, curr_dev_sha: str, public_prefixes: list[str]) -> tuple[list[str], list[str]]:
    """Diff two source commits; return (add_lines, remove_paths) restricted to the
    public allowlist. add_lines are ready for `update-index --index-info`
    ("<mode> <sha> 0\\t<path>"); remove_paths are plain paths for --force-remove.

    Uses `--raw` so mode + blob sha come straight out of the diff -- no extra
    per-changed-path `ls-tree`/`get_blob_sha` round trip.
    """
    out = _run(["git", "diff-tree", "-r", "--raw", prev_dev_sha, curr_dev_sha]).stdout
    add_lines: list[str] = []
    remove_paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, _, rest = line.partition("\t")
        meta_parts = meta.split()  # :old_mode new_mode old_sha new_sha status
        new_mode, new_sha, status = meta_parts[1], meta_parts[3], meta_parts[4]
        status_code = status[0]  # R100 -> R, etc.
        paths = rest.split("\t")
        if status_code == "D":
            remove_paths.append(paths[0])
            continue
        # A / M / T, and R / C (rename/copy target is the last field).
        target_path = paths[-1]
        if is_public(target_path, public_prefixes):
            add_lines.append(f"{new_mode} {new_sha} 0\t{target_path}")
        else:
            remove_paths.append(target_path)
        if status_code in ("R", "C") and len(paths) > 1:
            remove_paths.append(paths[0])
    return add_lines, remove_paths


def update_filtered_tree(
    prev_dev_sha: str,
    curr_dev_sha: str,
    public_prefixes: list[str],
    index_path: Path,
) -> str:
    """Incremental update: apply only the paths that changed between `prev_dev_sha`
    and `curr_dev_sha` to the scratch index, then write it out.

    O(files changed in this commit), not O(files in repo) -- the fix for the slow
    `make mirror` / `make release`: previously every commit re-walked and
    re-`mktree`'d the *entire* filtered tree regardless of diff size.
    """
    add_lines, remove_paths = _diff_add_remove(prev_dev_sha, curr_dev_sha, public_prefixes)
    for src_path, dest_path in INJECTED_FILES:
        blob_sha = get_blob_sha(curr_dev_sha, src_path)
        if blob_sha:
            add_lines.append(f"100644 {blob_sha} 0\t{dest_path}")
    _force_remove(index_path, remove_paths)
    _update_index_info(index_path, add_lines)
    return _write_tree(index_path)


# ---------------------------------------------------------------------------
# Commit metadata + creation
# ---------------------------------------------------------------------------

# A separator that cannot appear in git metadata fields (author/email/date).
# We read body separately to avoid conflicts with arbitrary commit messages.
META_SEP = "\x01\x02\x03"


def get_commit_metadata(sha: str) -> dict[str, str]:
    # Read scalar fields in one call, body separately to avoid separator conflicts.
    fmt = META_SEP.join(["%an", "%ae", "%aI", "%cn", "%ce", "%cI"])
    header = git("log", "-1", f"--format={fmt}", sha)
    body = git("log", "-1", "--format=%B", sha)
    parts = header.split(META_SEP, 5)
    return {
        "author_name": parts[0],
        "author_email": parts[1],
        "author_date": parts[2],
        "committer_name": parts[3],
        "committer_email": parts[4],
        "committer_date": parts[5] if len(parts) > 5 else "",
        "message": body,
    }


def get_commit_parents(sha: str) -> list[str]:
    out = git("log", "-1", "--format=%P", sha).strip()
    return out.split() if out else []


def create_filtered_commit(
    filtered_tree: str,
    parent_shas: list[str],
    meta: dict[str, str],
) -> str:
    """Create a commit via git commit-tree; return new SHA."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": meta["author_name"],
            "GIT_AUTHOR_EMAIL": meta["author_email"],
            "GIT_AUTHOR_DATE": meta["author_date"],
            "GIT_COMMITTER_NAME": meta["committer_name"],
            "GIT_COMMITTER_EMAIL": meta["committer_email"],
            "GIT_COMMITTER_DATE": meta["committer_date"],
        }
    )
    cmd = ["git", "commit-tree", filtered_tree]
    for p in parent_shas:
        cmd += ["-p", p]
    cmd += ["-m", meta["message"]]
    return subprocess.run(cmd, check=True, text=True, capture_output=True, cwd=REPO_ROOT, env=env).stdout.strip()


# ---------------------------------------------------------------------------
# Watermark (state)
# ---------------------------------------------------------------------------


def fetch_watermark() -> None:
    """Pull the watermark refs from origin so a fresh checkout can resume incrementally.

    Uses forced refspecs (+src:dst) so a stale local watermark can never silently
    block the update: without the force prefix, git refuses non-fast-forward ref
    updates and this fetch fails silently (capture_output swallows stderr), leaving
    get_watermark() to fall back to the stale local ref and report a bogus count.
    """
    result = subprocess.run(
        ["git", "fetch", DEV_REMOTE, f"+{MIRROR_DEV_TAG}:{MIRROR_DEV_TAG}", f"+{MIRROR_PUB_TAG}:{MIRROR_PUB_TAG}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: failed to fetch watermark refs from {DEV_REMOTE}: {result.stderr.strip()}", file=sys.stderr)


def get_watermark() -> tuple[str, str] | None:
    """Return (dev_sha, pub_sha) or None."""
    fetch_watermark()
    dev = git_ok("rev-parse", "--verify", MIRROR_DEV_TAG)
    pub = git_ok("rev-parse", "--verify", MIRROR_PUB_TAG)
    return (dev, pub) if dev and pub else None


def set_watermark(dev_sha: str, pub_sha: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would set {MIRROR_DEV_TAG} -> {dev_sha[:12]}")
        print(f"[dry-run] Would set {MIRROR_PUB_TAG} -> {pub_sha[:12]}")
        return
    subprocess.run(["git", "update-ref", MIRROR_DEV_TAG, dev_sha], check=True, cwd=REPO_ROOT)
    subprocess.run(["git", "update-ref", MIRROR_PUB_TAG, pub_sha], check=True, cwd=REPO_ROOT)
    subprocess.run(
        [
            "git",
            "push",
            "--no-verify",
            "--force",
            DEV_REMOTE,
            f"{MIRROR_DEV_TAG}:{MIRROR_DEV_TAG}",
            f"{MIRROR_PUB_TAG}:{MIRROR_PUB_TAG}",
        ],
        check=True,
        cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def build_remote_url(base_url: str) -> str:
    if "x-access-token" in base_url or ("@" in base_url.replace("https://", "", 1)):
        return base_url
    token = subprocess.run(["gh", "auth", "token"], check=True, text=True, capture_output=True).stdout.strip()
    return base_url.replace("https://", f"https://x-access-token:{token}@", 1)


def push_to_public(remote_url: str, final_sha: str, force: bool, dry_run: bool) -> None:
    safe = remote_url.split("@")[-1] if "@" in remote_url else remote_url
    if dry_run:
        flag = " --force" if force else ""
        print(f"[dry-run] Would push{flag} {final_sha[:12]} -> {safe} main")
        return
    cmd = ["git", "push", "--no-verify", remote_url, f"{final_sha}:refs/heads/main"]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-ref", default=DEFAULT_SOURCE_REF)
    parser.add_argument("--since", metavar="DEV:PUB", help="Override watermark (dev_sha:pub_sha, both local)")
    parser.add_argument("--public-remote", default=DEFAULT_PUBLIC_REMOTE)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    public_prefixes = load_public_prefixes()
    remote_url = build_remote_url(args.public_remote)
    dev_tip = git("rev-parse", args.source_ref)

    # Determine watermark
    fresh = False
    watermark_dev: str | None = None
    initial_pub_parent: str | None = None

    if args.since:
        wparts = args.since.split(":", 1)
        if len(wparts) != 2:
            print("ERROR: --since must be DEV_SHA:PUB_SHA", file=sys.stderr)
            return 1
        watermark_dev, initial_pub_parent = wparts
    else:
        wm = get_watermark()
        if wm is None:
            fresh = True
            print("No watermark -- mirroring full history (force-push).")
        else:
            watermark_dev, initial_pub_parent = wm

    # Collect commits
    if fresh:
        commits = git("rev-list", "--topo-order", "--reverse", args.source_ref).splitlines()
    else:
        assert watermark_dev is not None
        if watermark_dev == dev_tip:
            print("Already up to date.")
            return 0
        commits = [
            c
            for c in git(
                "rev-list",
                "--topo-order",
                "--reverse",
                f"{watermark_dev}..{args.source_ref}",
            ).splitlines()
            if c
        ]
        if not commits:
            print("No new commits.")
            return 0

    print(f"Found {len(commits)} commit(s) to mirror.")

    # Replay commits -- build filtered trees via a scratch git index (single
    # update-index + write-tree per commit; no per-directory `mktree` fan-out).
    # Incremental runs seed the index from last run's already-filtered public
    # tree, so we never rescan the full source repo -- only the commit diffs.
    index_path = Path(tempfile.mktemp(prefix="lemoncrow-mirror-index-"))
    try:
        dev_to_pub: dict[str, str] = {}
        current_pub_parent = initial_pub_parent  # None on fresh run
        prev_dev_sha = watermark_dev  # None on fresh run
        seeded = False

        for i, dev_sha in enumerate(commits):
            meta = get_commit_metadata(dev_sha)

            if not seeded:
                if prev_dev_sha is not None and initial_pub_parent is not None:
                    # Incremental run: resume from last run's public tree instead
                    # of rebuilding the filtered tree from the source repo.
                    pub_tree = git("rev-parse", f"{initial_pub_parent}^{{tree}}")
                    seed_index_from_tree(index_path, pub_tree)
                    filtered_tree = update_filtered_tree(prev_dev_sha, dev_sha, public_prefixes, index_path)
                else:
                    filtered_tree = build_filtered_tree_full(dev_sha, public_prefixes, index_path)
                seeded = True
            else:
                assert prev_dev_sha is not None
                filtered_tree = update_filtered_tree(prev_dev_sha, dev_sha, public_prefixes, index_path)
            prev_dev_sha = dev_sha

            dev_parents = get_commit_parents(dev_sha)

            if i == 0:
                pub_parents = [current_pub_parent] if current_pub_parent else []
            else:
                pub_parents = [dev_to_pub[dp] for dp in dev_parents if dp in dev_to_pub]
                # Merge commit whose other parents predate the watermark:
                # fall back to current tip so history stays connected.
                if not pub_parents and dev_parents and current_pub_parent:
                    pub_parents = [current_pub_parent]

            new_sha = create_filtered_commit(filtered_tree, pub_parents, meta)
            dev_to_pub[dev_sha] = new_sha
            current_pub_parent = new_sha

            if args.verbose:
                subject = (meta["message"].splitlines()[0] if meta["message"] else "")[:72]
                print(f"  {dev_sha[:12]} -> {new_sha[:12]}  {subject}")
            elif (i + 1) % 100 == 0 or (i + 1) == len(commits):
                print(f"  {i + 1}/{len(commits)} commits processed...")

        if not current_pub_parent:
            print("Nothing produced.")
            return 0

        push_to_public(
            remote_url,
            current_pub_parent,
            force=(fresh or args.force),
            dry_run=args.dry_run,
        )
        set_watermark(dev_tip, current_pub_parent, dry_run=args.dry_run)
        print(f"Done. Mirrored {len(commits)} commit(s). Public HEAD: {current_pub_parent[:12]}")
        return 0
    finally:
        if index_path.exists():
            index_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
