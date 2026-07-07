#!/usr/bin/env python3
"""Incremental, history-preserving git mirror: source branch → public repo.

Includes only paths listed in release/public-paths.txt (allowlist) from every
commit tree via git plumbing -- no squash, no wipe, real history preserved.

State (two refs, pushed to `origin`/atelier-dev so any checkout can pick them up):
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
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PUBLIC_PATHS_FILE = REPO_ROOT / "release" / "public-paths.txt"
DEFAULT_SOURCE_REF = "HEAD"
MIRROR_DEV_TAG = "refs/mirror/last"  # watermark: last mirrored source SHA
MIRROR_PUB_TAG = "refs/mirror/last-pub"  # public SHA created by last run
DEFAULT_PUBLIC_REMOTE = "https://github.com/atelier-ws/atelier.git"
DEV_REMOTE = "origin"  # atelier-dev -- where the watermark refs live
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Files injected into the public repo that don't exist in the dev repo's public paths.
# Each entry is (source_path_in_dev_repo, dest_path_in_public_tree).
INJECTED_FILES: list[tuple[str, str]] = [
    ("release/atelier-release.yml", ".github/workflows/release.yml"),
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


def build_filtered_tree(commit_sha: str, public_prefixes: list[str]) -> str:
    """Return a new tree SHA with only files matching the public allowlist."""
    ls = _run(["git", "ls-tree", "-r", "--full-tree", commit_sha]).stdout
    blobs: list[tuple[str, str, str, str]] = []
    for line in ls.splitlines():
        meta, _, path = line.partition("\t")
        mode, obj_type, sha = meta.split()
        if is_public(path, public_prefixes):
            blobs.append((mode, obj_type, sha, path))

    # Inject files from private paths into new public locations.
    for src_path, dest_path in INJECTED_FILES:
        blob_sha = get_blob_sha(commit_sha, src_path)
        if blob_sha:
            blobs.append(("100644", "blob", blob_sha, dest_path))

    return _make_tree(blobs, "") if blobs else EMPTY_TREE


def _make_tree(blobs: list[tuple[str, str, str, str]], prefix: str) -> str:
    """Recursively build a git tree from a flat blob list."""
    direct: dict[str, tuple[str, str, str]] = {}
    subdirs: dict[str, list[tuple[str, str, str, str]]] = {}

    for mode, obj_type, sha, path in blobs:
        rel = path[len(prefix) :].lstrip("/") if prefix else path
        slash = rel.find("/")
        if slash == -1:
            direct[rel] = (mode, obj_type, sha)
        else:
            name = rel[:slash]
            subdirs.setdefault(name, []).append((mode, obj_type, sha, path))

    entries: list[str] = []
    for name, sub_blobs in subdirs.items():
        sub_prefix = (prefix + "/" + name).lstrip("/")
        sub_tree = _make_tree(sub_blobs, sub_prefix)
        entries.append(f"040000 tree {sub_tree}\t{name}")
    for name, (mode, obj_type, sha) in direct.items():
        entries.append(f"{mode} {obj_type} {sha}\t{name}")

    stdin = "\n".join(entries) + "\n" if entries else ""
    return _run(["git", "mktree"], input=stdin).stdout.strip()


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
    """Best-effort: pull the watermark refs from origin so a fresh checkout can resume incrementally."""
    subprocess.run(
        ["git", "fetch", DEV_REMOTE, f"{MIRROR_DEV_TAG}:{MIRROR_DEV_TAG}", f"{MIRROR_PUB_TAG}:{MIRROR_PUB_TAG}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )


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

    # Replay commits -- build filtered objects in the local object store
    dev_to_pub: dict[str, str] = {}
    current_pub_parent = initial_pub_parent  # None on fresh run

    for i, dev_sha in enumerate(commits):
        meta = get_commit_metadata(dev_sha)
        filtered_tree = build_filtered_tree(dev_sha, public_prefixes)
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


if __name__ == "__main__":
    sys.exit(main())
