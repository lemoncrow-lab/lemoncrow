"""Dependency cache for hermetic fetch/build stages, shared by Review and Workspaces.

Fetched tarballs and wheels and built dependency trees live under
``<store>/review/hermetic/<namespace>``. Writes are isolated by source trust:
a run writes only its own :data:`~lemoncrow.pro.environment.execution.CacheNamespace`
and reads only what :func:`~lemoncrow.pro.environment.execution.readable_cache_namespaces`
allows. :meth:`DependencyCache.prune` is the closer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Final

from lemoncrow.pro.environment.execution import CacheNamespace, readable_cache_namespaces

KEEP_TREES_PER_NAMESPACE: Final[int] = 8
TREE_IDLE_SECONDS: Final[float] = 7 * 24 * 3600
TARBALL_IDLE_SECONDS: Final[float] = 30 * 24 * 3600
_PRUNE_EVERY_SECONDS: Final[float] = 600.0

_PRUNE_GUARD = threading.Lock()
_LAST_PRUNE: dict[str, float] = {}
_TREE_LOCKS: dict[str, threading.Lock] = {}


def tree_lock(key: str) -> threading.Lock:
    # lc-debt: process-local lock map grows with distinct plan digests; bounded by trees provisioned per process.
    with _PRUNE_GUARD:
        return _TREE_LOCKS.setdefault(key, threading.Lock())


class DependencyCache:
    """Fetch/build caches under ``<store>/review/hermetic/<namespace>``.

    A run writes only to its own namespace and reads only the namespaces
    :func:`readable_cache_namespaces` allows. Writers touch entries on use and
    :meth:`prune` (run by the writer, amortized) is the closer.
    """

    def __init__(self, root: Path, namespace: CacheNamespace) -> None:
        self.root = root
        self.namespace = namespace

    @staticmethod
    def for_store(store_root: Path, namespace: CacheNamespace) -> DependencyCache:
        return DependencyCache(store_root / "review" / "hermetic", namespace)

    def _dir(self, namespace: CacheNamespace, kind: str) -> Path:
        return self.root / namespace / kind

    @property
    def writable_tarballs(self) -> Path:
        return self._dir(self.namespace, "tarballs")

    def readable_tarball_dirs(self) -> tuple[Path, ...]:
        return tuple(self._dir(ns, "tarballs") for ns in readable_cache_namespaces(self.namespace))

    def tarball(self, sha512_hex: str) -> Path | None:
        for directory in self.readable_tarball_dirs():
            candidate = directory / f"{sha512_hex}.tgz"
            try:
                if candidate.is_file() and hashlib.sha512(candidate.read_bytes()).hexdigest() == sha512_hex:
                    os.utime(candidate)
                    return candidate
            except OSError:
                continue
        return None

    def store_tarball(self, sha512_hex: str, payload: bytes) -> Path:
        return self._store_blob(self.writable_tarballs, f"{sha512_hex}.tgz", payload)

    @property
    def writable_wheels(self) -> Path:
        return self._dir(self.namespace, "wheels")

    def readable_wheel_dirs(self) -> tuple[Path, ...]:
        return tuple(self._dir(ns, "wheels") for ns in readable_cache_namespaces(self.namespace))

    def wheel(self, sha256_hex: str) -> Path | None:
        """A cached wheel whose bytes still hash to *sha256_hex*."""

        for directory in self.readable_wheel_dirs():
            candidate = directory / f"{sha256_hex}.whl"
            try:
                if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha256_hex:
                    os.utime(candidate)
                    return candidate
            except OSError:
                continue
        return None

    def store_wheel(self, sha256_hex: str, payload: bytes) -> Path:
        return self._store_blob(self.writable_wheels, f"{sha256_hex}.whl", payload)

    @staticmethod
    def _store_blob(directory: Path, name: str, payload: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        tmp = directory / f".{name}.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(payload)
        tmp.replace(target)
        return target

    def tree(self, plan_digest: str, content: str = "node_modules") -> Path | None:
        for namespace in readable_cache_namespaces(self.namespace):
            entry = self._dir(namespace, "trees") / plan_digest
            try:
                marker = json.loads((entry / "tree.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if marker.get("plan_digest") == plan_digest and (entry / content).is_dir():
                try:
                    os.utime(entry)
                except OSError:
                    pass
                return entry / content
        return None

    def new_tree_dir(self, plan_digest: str) -> tuple[Path, Path]:
        trees = self._dir(self.namespace, "trees")
        trees.mkdir(parents=True, exist_ok=True)
        staging = trees / f".{plan_digest}.{uuid.uuid4().hex}.tmp"
        staging.mkdir()
        return staging, trees / plan_digest

    def prune(self, *, force: bool = False) -> None:
        """Closer: drop idle trees beyond the per-namespace budget and idle tarballs."""

        key = str(self.root.resolve()) if self.root.exists() else str(self.root)
        now = time.time()
        with _PRUNE_GUARD:
            if not force and now - _LAST_PRUNE.get(key, 0.0) < _PRUNE_EVERY_SECONDS:
                return
            _LAST_PRUNE[key] = now
        for namespace in readable_cache_namespaces(self.namespace)[:1]:
            trees = self._dir(namespace, "trees")
            try:
                rows = sorted(
                    (item for item in trees.iterdir() if item.is_dir()),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                rows = []
            for index, entry in enumerate(rows):
                try:
                    idle = now - entry.stat().st_mtime
                except OSError:
                    continue
                stale_tmp = entry.name.startswith(".") and idle > 3600
                if (
                    stale_tmp
                    or (index >= KEEP_TREES_PER_NAMESPACE and idle >= TREE_IDLE_SECONDS)
                    or idle >= 4 * TREE_IDLE_SECONDS
                ):
                    shutil.rmtree(entry, ignore_errors=True)
            for kind in ("tarballs", "wheels"):
                try:
                    for item in self._dir(namespace, kind).iterdir():
                        if now - item.stat().st_mtime >= TARBALL_IDLE_SECONDS:
                            item.unlink(missing_ok=True)
                except OSError:
                    continue


__all__ = [
    "KEEP_TREES_PER_NAMESPACE",
    "TARBALL_IDLE_SECONDS",
    "TREE_IDLE_SECONDS",
    "DependencyCache",
    "tree_lock",
]
