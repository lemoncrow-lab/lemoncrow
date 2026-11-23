#!/usr/bin/env python3
"""Rebuild the read-burden corpus from GitHub, checking every file against the manifest.

The public benchmark ships no third-party code: it ships ``sources.json``, a list of pull requests, their revisions (base
and head commit) and the git blob id of every changed file. This script fetches those commits with plain ``git`` over
https (no token, no API rate limit), reads each file with ``git cat-file blob <sha>:<path>`` (the bytes ``git show``
prints, without a text-conversion filter), and keeps a file only when its git blob id equals the one the manifest
recorded. What it writes is the corpus layout the benchmark reads::

    <out>/prs/<owner>__<repo>_<number>.json.gz
    <out>/licences.json.gz
    <out>/manifest.json        (only for a complete, unfiltered fetch, copied from --manifest)

A commit GitHub no longer serves is reported by name (pull request, revision, sha) and never replaced by another; a file
whose hash differs is reported by path and not used; a pull request with any such problem is not written. A network
failure is reported for that pull request and the next one is tried. Your own git configuration is not read (a
``url.insteadOf`` rewrite or proxy setting there cannot change what is fetched; use ``HTTPS_PROXY`` for a proxy). OUT must be
empty, or pass --replace: everything is rebuilt in a temporary directory beside OUT and only the files rebuilt are moved
in once fetching is over, so a failed run deletes nothing (a pull request that fails keeps its old file; --pr touches only
the selected pull requests and never refreshes the manifest; stale pull request files are pruned only by a complete,
unfiltered run). OUT may not hold or contain the sources file. Exit status: 0 everything rebuilt, 1 something was
not, 2 unusable arguments or manifest.

Standard library and ``git`` only. Everything fetched is data: nothing from a fetched repository is checked out, run or
imported, and the clone cache lives outside any source tree.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCES_FORMAT = 1
DEFAULT_BASE_URL = "https://github.com"
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

#: What git prints when the server answers but does not have the commit (or the repository) any more. Anything else a
#: failed fetch prints is treated as a network problem: an unknown failure is never a claim that a commit is gone.
_GONE = (
    "not our ref",
    "couldn't find remote ref",
    "repository not found",
    "could not read username",
    "authentication failed",
    "does not appear to be a git repository",
    "unadvertised object",
    "no such remote ref",
)


class UsageError(Exception):
    """Arguments, sources file or output directory the script cannot use: exit status 2, nothing fetched."""


OK = "ok"
GONE = "gone"
NETWORK = "network"
LOCAL = "local"  # git could not run here (no git, a cache directory that cannot be created): not a claim about GitHub


def git_blob_id(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def dumps(value: Any) -> bytes:
    """The corpus's own JSON form (the collector writes the same), so rebuilt files can be byte-identical."""

    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(dumps(value), compresslevel=9, mtime=0))


def pr_filename(pr_id: str) -> str:
    return pr_id.replace("/", "__").replace("#", "_") + ".json.gz"


def load_sources(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read the sources file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != SOURCES_FORMAT:
        raise UsageError(f"{path} is not a sources file of format {SOURCES_FORMAT}")
    for pr in data.get("pull_requests", []):
        if not _REPOSITORY.fullmatch(str(pr.get("repository", ""))):
            raise UsageError(f"{pr.get('id')}: repository name is not owner/name")
        for revision in pr["revisions"]:
            for key in ("base_sha", "head_sha"):
                if not _SHA.fullmatch(str(revision[key])):
                    raise UsageError(f"{pr['id']} revision {revision['index']}: {key} is not a full commit sha")
    for repository, row in data.get("licences", {}).items():
        if not _REPOSITORY.fullmatch(repository) or not _SHA.fullmatch(str(row["sha"])):
            raise UsageError(f"{repository}: malformed licence entry")
    return data


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


class Git:
    """A cache of bare repositories, one per source repository, filled by ``git fetch --depth=1 <url> <sha>``."""

    def __init__(self, cache: Path, base_url: str, timeout: int, attempts: int, pause: float = 2.0) -> None:
        self.cache = cache
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.pause = pause
        self._state: dict[tuple[str, str], tuple[str, str]] = {}

    @staticmethod
    def _env() -> dict[str, str]:
        """The environment git runs in: the user's own git configuration is not read.

        A global ``url.<base>.insteadOf`` rewrite, ``http.proxy``, ``http.sslVerify`` or ``core.askPass`` would otherwise
        change what is fetched, or turn every fetch into a misleading network failure. A proxy is still taken from
        ``HTTPS_PROXY`` and a certificate bundle from ``GIT_SSL_CAINFO``.
        """

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k == "GIT_SSL_CAINFO"}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        return env

    def _config(self) -> list[str]:
        # The file transport is for a local mirror only: allowed when --base-url is itself a file:// URL.
        file_transport = "always" if self.base_url.startswith("file://") else "never"
        return [
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", f"protocol.file.allow={file_transport}",
            "-c", "credential.helper=",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
        ]  # fmt: skip

    def _run(self, repository: str, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        """Run git in the repository's cache; an OSError means git or the cache directory is unusable here."""

        return subprocess.run(
            ["git", *self._config(), *args],
            cwd=self._repo_dir(repository),
            env=self._env(),
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )

    def _repo_dir(self, repository: str) -> Path:
        path = self.cache / (repository.replace("/", "__") + ".git")
        if not (path / "HEAD").exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
                made = subprocess.run(
                    ["git", "-c", "init.templateDir=", "init", "--bare", "-q", str(path)],
                    env=self._env(),
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.SubprocessError as exc:
                raise OSError(f"cannot create the cache repository {path}: {exc}") from exc
            if made.returncode != 0:
                detail = made.stderr.decode("utf-8", "replace").strip().replace("\n", " ")[:200]
                raise OSError(f"cannot create the cache repository {path}: {detail}")
        return path

    def url(self, repository: str) -> str:
        return f"{self.base_url}/{repository}.git"

    def _has(self, repository: str, sha: str) -> bool:
        try:
            return self._run(repository, ["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _fetch(self, repository: str, ref: str) -> tuple[str, str]:
        message = ""
        for attempt in range(self.attempts):
            try:
                done = self._run(repository, ["fetch", "--depth=1", "--no-tags", "-q", self.url(repository), ref])
            except subprocess.TimeoutExpired:
                message = f"git fetch timed out after {self.timeout} s"
            except OSError as exc:
                return LOCAL, f"cannot run git: {exc}"
            else:
                if done.returncode == 0:
                    return OK, ""
                message = done.stderr.decode("utf-8", "replace").strip().replace("\n", " ")[:300]
                if any(marker in message.lower() for marker in _GONE):
                    return GONE, message
            if attempt + 1 < self.attempts:
                time.sleep(self.pause)
        return NETWORK, message

    def ensure_commit(self, repository: str, sha: str, pull_number: int | None = None) -> tuple[str, str]:
        """``(OK|GONE|NETWORK|LOCAL, detail)``: is the commit in the cache now. The answer is remembered."""

        known = self._state.get((repository, sha))
        if known is not None:
            return known
        if self._has(repository, sha):
            result = (OK, "")
        else:
            result = self._fetch(repository, sha)
            if result[0] == GONE and pull_number is not None:
                # GitHub serves a pull request's head commits through refs/pull/N/head even when no branch holds them.
                fallback = self._fetch(repository, f"refs/pull/{pull_number}/head")
                if fallback[0] in (NETWORK, LOCAL):
                    result = fallback  # the second question was not answered: the commit is not known to be gone
                elif self._has(repository, sha):
                    result = (OK, "")
            elif result[0] == OK and not self._has(repository, sha):
                result = (GONE, "the server answered without the commit")
        if result[0] in (OK, GONE):  # a network or local failure may pass; ask again next time
            self._state[(repository, sha)] = result
        return result

    def read(self, repository: str, sha: str, path: str) -> bytes | None:
        """The bytes of *path* at *sha*, or None when the commit has no such file."""

        try:
            done = self._run(repository, ["cat-file", "blob", f"{sha}:{path}"])
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None


# --------------------------------------------------------------------------- #
# rebuilding
# --------------------------------------------------------------------------- #


@dataclass
class Problem:
    kind: str  # gone | network | local | mismatch | missing_file | not_text
    pr: str
    revision: int | None
    sha: str
    path: str = ""
    detail: str = ""

    def line(self) -> str:
        where = f"{self.pr}" + (f" revision {self.revision}" if self.revision is not None else "")
        what = {
            "gone": f"commit {self.sha} is no longer served",
            "network": f"network failure fetching {self.sha}",
            "local": f"git could not run for {self.sha}",
            "mismatch": f"{self.path} at {self.sha} does not match the manifest; file not used",
            "missing_file": f"{self.path} is not in commit {self.sha}",
            "not_text": f"{self.path} at {self.sha} is not UTF-8 text; file not used",
        }[self.kind]
        return f"  {where}: {what}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Result:
    written: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _locations(pr: dict[str, Any]) -> list[tuple[int, str, str, str, str]]:
    """``(revision, sha, path, expected blob, side)`` of every file the pull request's revisions name, in order."""

    out: list[tuple[int, str, str, str, str]] = []
    for revision in pr["revisions"]:
        for row in revision["files"]:
            if row["base_blob"] is not None:
                out.append(
                    (revision["index"], revision["base_sha"], row["old_path"] or row["path"], row["base_blob"], "base")
                )
            if row["head_blob"] is not None:
                out.append((revision["index"], revision["head_sha"], row["path"], row["head_blob"], "head"))
    return out


def rebuild_pull_request(git: Git, pr: dict[str, Any], problems: list[Problem]) -> dict[str, Any] | None:
    """The stored record of *pr* rebuilt from GitHub, or None (with *problems* appended) when anything is off."""

    pr_id, repository = str(pr["id"]), str(pr["repository"])
    before = len(problems)
    for revision in pr["revisions"]:
        for sha in dict.fromkeys((revision["base_sha"], revision["head_sha"])):
            state, detail = git.ensure_commit(repository, sha, int(pr["number"]))
            if state != OK:
                problems.append(Problem(state, pr_id, revision["index"], sha, detail=detail))
                if state in (NETWORK, LOCAL):
                    return None  # the next pull request may still work; this one cannot be finished now
    if len(problems) > before:
        return None
    blobs: dict[str, str] = {}
    checked: set[tuple[str, str]] = set()
    for index, sha, path, expected, _side in _locations(pr):
        if (sha, path) in checked:
            continue  # one file at one commit is read and judged once, however many revisions name it
        checked.add((sha, path))
        raw = git.read(repository, sha, path)
        if raw is None:
            problems.append(Problem("missing_file", pr_id, index, sha, path))
            continue
        actual = git_blob_id(raw)
        if actual != expected:
            problems.append(Problem("mismatch", pr_id, index, sha, path, f"manifest {expected}, GitHub {actual}"))
            continue
        try:
            blobs[expected] = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(Problem("not_text", pr_id, index, sha, path))
    if len(problems) > before:
        return None
    return {**pr, "blobs": dict(sorted(blobs.items()))}


def rebuild_licences(
    git: Git, licences: dict[str, Any], repositories: set[str], problems: list[Problem]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for repository in sorted(repositories):
        row = licences.get(repository)
        if row is None:
            problems.append(
                Problem("missing_file", repository, None, "", "LICENSE", "no licence entry in the manifest")
            )
            continue
        state, detail = git.ensure_commit(repository, row["sha"])
        if state != OK:
            problems.append(Problem(state, repository, None, row["sha"], detail=detail))
            continue
        files: dict[str, Any] = {}
        before = len(problems)
        for path, expected in sorted(row["files"].items()):
            raw = git.read(repository, row["sha"], path)
            if raw is None:
                problems.append(Problem("missing_file", repository, None, row["sha"], path))
            elif git_blob_id(raw) != expected:
                problems.append(Problem("mismatch", repository, None, row["sha"], path, f"manifest {expected}"))
            else:
                try:
                    files[path] = {"blob": expected, "text": raw.decode("utf-8")}
                except UnicodeDecodeError:
                    problems.append(Problem("not_text", repository, None, row["sha"], path))
        if len(problems) == before:
            out[repository] = {"license": row["license"], "sha": row["sha"], "files": files}
    return out


def select(sources: dict[str, Any], wanted: Sequence[str]) -> list[dict[str, Any]]:
    prs: list[dict[str, Any]] = sources["pull_requests"]
    if not wanted:
        return prs
    keys = {item.lower() for item in wanted}
    chosen = [pr for pr in prs if str(pr["id"]).lower() in keys or str(pr["repository"]).lower() in keys]
    unknown = keys - {str(pr["id"]).lower() for pr in chosen} - {str(pr["repository"]).lower() for pr in chosen}
    if unknown:
        raise UsageError("not in the manifest: " + ", ".join(sorted(unknown)))
    return chosen


def check_out(out: Path, sources_file: Path | None, replace: bool, wanted: Sequence[str]) -> list[Path]:
    """Refuse an output directory that must not be written; return the corpus files already in it."""

    if sources_file is not None:
        here = out.resolve()
        if sources_file.resolve().is_relative_to(here) or (out / "sources.json").exists():
            raise UsageError(
                f"{out} holds (or contains) the sources file; use a separate, empty directory such as corpus/"
            )
    prs = out / "prs"
    if prs.is_symlink() or (prs.exists() and not prs.resolve().is_relative_to(out.resolve())):
        raise UsageError(f"{prs} is a symbolic link or leads outside {out}; refusing to write through it")
    existing = sorted(prs.glob("*.json.gz")) if prs.is_dir() else []
    existing += [out / name for name in ("licences.json.gz", "manifest.json") if (out / name).exists()]
    if existing and not replace:
        raise UsageError(
            f"{out} already holds {len(existing)} corpus files (such as {existing[0].name}); a rerun would mix old and "
            "new. Use an empty directory, or pass --replace to swap in the files this run rebuilds"
        )
    return existing


def _stored_licences(path: Path) -> dict[str, Any]:
    try:
        rows = json.loads(gzip.decompress(path.read_bytes()))["repositories"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return dict(rows) if isinstance(rows, dict) else {}


def fetch_corpus(
    sources: dict[str, Any],
    out: Path,
    git: Git,
    wanted: Sequence[str] = (),
    manifest: Path | None = None,
    log: Any = print,
    replace: bool = False,
    sources_file: Path | None = None,
) -> Result:
    """Rebuild the corpus into a temporary directory beside *out*, then move in what was rebuilt.

    Nothing in *out* is deleted or changed before the fetching is over. With ``replace`` the files this run rebuilt
    replace their old versions; the files of a pull request that failed this time stay as they were; and a complete,
    unfiltered run also removes the pull request files it no longer lists: stale pull request files are pruned only by a
    complete run, never by an incomplete or a ``--pr`` run. An incomplete or ``--pr`` run replaces only the pull
    requests it rebuilt and merges their licences into the stored ones (only for repositories the sources still list).
    The manifest is copied only by a complete, unfiltered run, so a ``--pr`` run never refreshes it (a warning says so
    when one is already there). The files are moved in with the pull requests first, then the licences, and the manifest
    last, so a manifest marks a finished run.
    """

    chosen = select(sources, wanted)
    existing = check_out(out, sources_file, replace, wanted)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}.fetch-", dir=out.parent))
    try:
        result = Result()
        repositories = {str(pr["repository"]) for pr in chosen}
        licences = rebuild_licences(git, sources.get("licences", {}), repositories, result.problems)
        for pr in chosen:
            pr_id = str(pr["id"])
            if pr["repository"] not in licences:
                result.incomplete.append(pr_id)
                log(f"INCOMPLETE {pr_id}: its licence text could not be rebuilt")
                continue
            start = len(result.problems)
            record = rebuild_pull_request(git, pr, result.problems)
            if record is None:
                result.incomplete.append(pr_id)
                log(f"INCOMPLETE {pr_id}: not written ({len(result.problems) - start} problems, listed at the end)")
                continue
            write_json(stage / "prs" / pr_filename(pr_id), record)
            result.written.append(pr_id)
            log(f"ok {pr_id}: {len(pr['revisions'])} revisions, {len(record['blobs'])} files")
        complete = not wanted and not result.incomplete
        stored = {repository: licences[repository] for repository in sorted(licences) if repository in repositories}
        if replace and not complete and (out / "licences.json.gz").exists():
            listed = set(sources.get("licences", {}))
            kept = {k: v for k, v in _stored_licences(out / "licences.json.gz").items() if k in listed}
            stored = {repository: row for repository, row in sorted({**kept, **stored}.items())}
        if stored:  # nothing rebuilt: no licence file either
            write_json(stage / "licences.json.gz", {"repositories": stored})
        if complete and manifest is not None and manifest.exists():
            shutil.copyfile(manifest, stage / "manifest.json")
        if wanted and (out / "manifest.json").exists():
            result.warnings.append(
                f"{out / 'manifest.json'} was left as it was: a run with --pr never refreshes the manifest"
            )
        # The fetching is over: only now does anything in `out` change. Pull requests first, the licences next, and the
        # manifest last: an interrupted run never leaves a new manifest beside old records.
        staged = [*sorted((stage / "prs").glob("*.json.gz")), stage / "licences.json.gz", stage / "manifest.json"]
        for path in staged:
            if path.exists():
                (out / path.relative_to(stage)).parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, out / path.relative_to(stage))
        if complete:
            keep = {pr_filename(pr_id) for pr_id in result.written}
            for old in existing:
                if old.parent.name == "prs" and old.name not in keep:
                    old.unlink()
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def default_cache() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "read-burden-fetch"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", type=Path, help="the sources file (sources.json)")
    parser.add_argument("out", type=Path, help="directory to write the rebuilt corpus into")
    parser.add_argument(
        "--pr",
        action="append",
        default=[],
        metavar="ID",
        help="only this pull request (owner/repo#N) or repository (owner/repo); repeatable",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="manifest.json to copy into a complete, unfiltered output (default: next to the sources file)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="clone cache (default: $XDG_CACHE_HOME/read-burden-fetch); keep it outside any source tree",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"where repositories are fetched from (default {DEFAULT_BASE_URL}; a file:// URL works for a local mirror)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="allow a non-empty OUT: the files this run rebuilds replace their old versions once fetching is over "
        "(nothing is deleted before then; a pull request that fails keeps its old file; a run with --pr touches only "
        "those pull requests and never refreshes the manifest; stale files are pruned only by a complete run). "
        "Without it a non-empty OUT is refused",
    )
    parser.add_argument("--timeout", type=int, default=300, help="seconds allowed for one git command")
    parser.add_argument("--attempts", type=int, default=2, help="tries for a fetch that fails on the network")
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.sources)
        manifest = args.manifest or args.sources.with_name("manifest.json")
        git = Git(args.cache or default_cache(), args.base_url, args.timeout, args.attempts)
        result = fetch_corpus(
            sources, args.out, git, args.pr, manifest, replace=args.replace, sources_file=args.sources
        )
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(result.written)} pull requests rebuilt, {len(result.incomplete)} not")
    for problem in result.problems:
        print(problem.line(), file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 1 if result.incomplete or result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
