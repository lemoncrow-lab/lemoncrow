"""One server-owned Zoekt accelerator for revision-exact materialized Views.

This is deliberately not the legacy per-worktree ``ZoektServer`` lifecycle.
The LemonCrow server remains the sole authority for tenant/View scope and owns
one central Zoekt index directory plus one persistent webserver. Materialized
Views become uniquely named shards in that directory; every search is hard
repo-filtered back to exactly that shard and callers still filter returned paths
against the authoritative View before exposing them.

Zoekt is an optional derived cache. Any discovery, indexing, HTTP, or query
parser failure returns ``ready=False`` so the caller falls back to the complete
SQLite path. No correctness decision depends on this module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SharedZoektAccelerator", "ZoektSearchResult", "build_shared_zoekt_accelerator"]


@dataclass(frozen=True, slots=True)
class ZoektSearchResult:
    ready: bool
    paths: tuple[str, ...] = ()
    cold_build: bool = False


class SharedZoektAccelerator:
    """Central Zoekt process/index cache shared by every View on one server."""

    __slots__ = (
        "_build_lock",
        "_closed",
        "_index_root",
        "_indexer",
        "_lock",
        "_revisions",
        "_search_binary",
        "_webserver",
        "_webserver_proc",
        "_webserver_url",
    )

    def __init__(self, *, search_binary: Path, indexer: Path, webserver: Path) -> None:
        self._search_binary = search_binary
        self._indexer = indexer
        self._webserver = webserver
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._index_root: Path | None = None
        self._webserver_proc: subprocess.Popen[bytes] | None = None
        self._webserver_url: str | None = None
        self._revisions: dict[str, int] = {}
        self._closed = False

    @classmethod
    def from_environment(cls) -> SharedZoektAccelerator | None:
        mode = os.environ.get("LEMONCROW_ZOEKT_MODE", "off").strip().lower()
        if mode == "off":
            return None
        # The existing public runtime resolver remains the single source of
        # truth for pinned/installed Zoekt. The central server currently uses
        # the host-binary runtime; managed-Docker deployments degrade to the
        # authoritative SQLite path until the shared-container lifecycle is
        # wired rather than silently spawning one container per View.
        try:
            from lemoncrow.infra.code_intel.zoekt.binary import discover_zoekt_binary

            resolution = discover_zoekt_binary(Path.cwd())
        except Exception:
            return None
        if not resolution.available or resolution.runtime != "binary" or resolution.path is None:
            return None
        search_binary = Path(resolution.path).resolve()
        sibling = search_binary.parent
        indexer = sibling / "zoekt-index"
        webserver = sibling / "zoekt-webserver"
        if not all(path.is_file() and os.access(path, os.X_OK) for path in (search_binary, indexer, webserver)):
            return None
        return cls(search_binary=search_binary, indexer=indexer, webserver=webserver)

    @property
    def available(self) -> bool:
        return not self._closed

    def prepare(self, workspace_root: str | Path, *, revision: int) -> bool:
        """Build/update one materialized View shard without issuing a query.

        Returns True when this call built a new shard. Failures are reported as
        False: this is a best-effort accelerator and SQLite remains authoritative.
        """
        root = Path(workspace_root).resolve()
        if self._closed or not root.is_dir():
            return False
        repo = root.name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            return False
        try:
            built = self._ensure_index(root, repo, revision)
            self._ensure_webserver()
            return built
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            urllib.error.URLError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False

    def search(self, workspace_root: str | Path, *, revision: int, query: str, limit: int = 96) -> ZoektSearchResult:
        root = Path(workspace_root).resolve()
        if self._closed or not root.is_dir() or not query.strip():
            return ZoektSearchResult(ready=False)
        repo = root.name
        # Materializer slot names are opaque hex. Keep a defensive fallback for
        # fixed-workspace embedders, but never allow query syntax into repo name.
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            return ZoektSearchResult(ready=False)
        try:
            # A stale shard is safe as additive recall: authoritative SQLite
            # results stay first and every returned path is re-checked against
            # current View membership. Overlay refreshes run in the background,
            # so search must never wait for a whole-repo directory reindex.
            with self._lock:
                indexed_revision = self._revisions.get(repo)
            cold = False
            if indexed_revision is None:
                cold = self._ensure_index(root, repo, revision)
            url = self._ensure_webserver()
            paths = self._query(url, repo, query, limit=max(1, min(int(limit), 200)))
            return ZoektSearchResult(ready=True, paths=paths, cold_build=cold)
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            urllib.error.URLError,
            ValueError,
            json.JSONDecodeError,
        ):
            return ZoektSearchResult(ready=False)

    def forget(self, workspace_root: str | Path) -> None:
        repo = Path(workspace_root).resolve().name
        with self._lock:
            self._revisions.pop(repo, None)
            index_root = self._index_root
        if index_root is None or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            return
        # Shards are immutable/mmap-safe: unlinking a closed View's shard makes
        # it unavailable to new searches while an in-flight webserver reader can
        # finish from its existing file descriptor.
        for path in index_root.glob(f"{repo}_v*.zoekt*"):
            try:
                path.unlink()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._webserver_proc
            self._webserver_proc = None
            self._webserver_url = None
            index_root = self._index_root
            self._index_root = None
            self._revisions.clear()
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if index_root is not None:
            shutil.rmtree(index_root, ignore_errors=True)

    def _ensure_index(self, root: Path, repo: str, revision: int) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("Zoekt accelerator is closed")
            current = self._revisions.get(repo)
            if current == revision:
                return False
            parent = root.parent
            index_root = parent / ".lemoncrow-zoekt"
            if self._index_root is None:
                self._index_root = index_root
            elif self._index_root != index_root:
                raise RuntimeError("one shared accelerator cannot span materialization roots")
        # ``zoekt-index`` safely replaces the shard for an existing repository;
        # serialize builders because they mutate one shared directory.
        with self._build_lock:
            with self._lock:
                if self._revisions.get(repo) == revision:
                    return False
                index_root = self._index_root
                if index_root is None:
                    raise RuntimeError("Zoekt index root is unavailable")
            index_root.mkdir(parents=True, exist_ok=True)
            os.chmod(index_root, 0o700)
            completed = subprocess.run(
                [str(self._indexer), "-index", str(index_root), str(root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"zoekt-index exited {completed.returncode}")
            with self._lock:
                self._revisions[repo] = revision
            return True

    def _ensure_webserver(self) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("Zoekt accelerator is closed")
            proc = self._webserver_proc
            url = self._webserver_url
            if proc is not None and proc.poll() is None and url:
                return url
            index_root = self._index_root
            if index_root is None:
                raise RuntimeError("Zoekt has no index root")
            port = _free_loopback_port()
            url = f"http://127.0.0.1:{port}"
            proc = subprocess.Popen(
                [
                    str(self._webserver),
                    "-listen",
                    f"127.0.0.1:{port}",
                    "-index",
                    str(index_root),
                    "-rpc",
                    "-html=false",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._webserver_proc = proc
            self._webserver_url = url
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                self._query(url, "__lemoncrow_health__", "__lemoncrow_health__", limit=1)
                return url
            except (OSError, RuntimeError, urllib.error.URLError, ValueError, json.JSONDecodeError):
                time.sleep(0.02)
        raise RuntimeError("zoekt-webserver did not become ready")

    @staticmethod
    def _query(url: str, repo: str, query: str, *, limit: int) -> tuple[str, ...]:
        scoped = f"repo:^{re.escape(repo)}$ ({query})"
        body = json.dumps({"Q": scoped}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{url}/api/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read())
        files = (payload.get("Result") or {}).get("Files") or []
        out: list[str] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or str(item.get("Repository") or "") != repo:
                continue
            path = str(item.get("FileName") or "").replace("\\", "/")
            if not path or path in seen or path.startswith("/") or ".." in Path(path).parts:
                continue
            seen.add(path)
            out.append(path)
            if len(out) >= limit:
                break
        return tuple(out)


def build_shared_zoekt_accelerator() -> SharedZoektAccelerator | None:
    return SharedZoektAccelerator.from_environment()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
