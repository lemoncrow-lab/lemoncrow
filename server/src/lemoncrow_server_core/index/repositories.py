"""Repository-partitioned SQLite indexes behind one server.

The catalogue contains identities and view routes only. A repository owns its
content, compressed postings, manifests and views in one independently writable
database. Worktrees share that repository database. Unbound protocol uploads use
an explicitly separate staging namespace and are collected by normal retention.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, ServerError
from .contracts import IndexBackend, SweepResult
from .deferred import SqliteDeferredJobStore
from .layers import DEFAULT_INLINE_REBUILD_BUDGET
from .ops import IndexOpsMetrics
from .sqlite import SqliteIndex, SqliteRepoIdentityService, _Db

_UNASSIGNED = "-unassigned-"
_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS repository_indexes (
    org_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    PRIMARY KEY (org_id, repo_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS repository_views (
    view_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    repo_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS repository_views_owner ON repository_views(org_id, repo_id);
"""


def repository_index_root(legacy_path: str | Path) -> Path:
    path = Path(legacy_path)
    return path.with_name(path.stem + ".repositories")


#: Idle generations (no live View) whose link graphs stay warm. Every base tree
#: -- roughly every commit -- gets a full generation graph (a busy repository's
#: is ~266k edge rows), and each used to live until its manifest root left the
#: 7-day retention window: ~85 idle copies, ~14 GiB, for one repository in a day.
#: Roots stay warm for the whole window; an older generation rebuilds its graph
#: from stored analysis when a View returns to it.
KEEP_IDLE_GENERATION_GRAPHS = 4
#: Generation graphs retired per maintenance pass, so a backlog drains without
#: holding the repository write lock for one long pass.
_GENERATION_GRAPHS_PER_PASS = 4


def _idle_generation_graphs(db: _Db, *, keep: int, limit: int) -> list[str]:
    """Generation graph keys no live View reads, beyond the *keep* newest generations.

    Oldest first and at most *limit* keys. A key is ``gscope_<generation>_<scope>``
    (see ``repository_generation_scope_id``); a generation may own several scoped
    graphs, and they are kept or retired together.
    """

    def generation_of(key: str) -> str:
        return key[:-17] if len(key) > 24 and key[-17] == "_" else key

    live = {
        "gscope_" + str(row["generation_id"]).removeprefix("gen_")
        for row in db.rows("SELECT DISTINCT generation_id FROM views WHERE generation_id<>''")
    }
    # A dirty View's graph is a delta over its generation graph; that base must
    # outlive it even if the View has since moved to another generation.
    live |= {generation_of(str(row["base"])) for row in db.rows("SELECT DISTINCT base FROM link_graphs WHERE base<>''")}
    last_seen = {
        "gscope_" + str(row["generation_id"]).removeprefix("gen_"): float(row["seen"] or 0.0)
        for row in db.rows(
            "SELECT g.generation_id AS generation_id, MAX(COALESCE(r.last_referenced_at, 0)) AS seen "
            "FROM repository_generations g LEFT JOIN manifest_roots r ON r.org_id=g.org_id AND r.root=g.root "
            "GROUP BY g.generation_id"
        )
    }
    idle: dict[str, list[str]] = {}
    for row in db.rows("SELECT view_id FROM link_graphs WHERE substr(view_id, 1, 7)='gscope_'"):
        key = str(row["view_id"])
        generation = generation_of(key)
        if generation not in live:
            idle.setdefault(generation, []).append(key)
    newest_first = sorted(idle, key=lambda generation: (last_seen.get(generation, 0.0), generation), reverse=True)
    retired = [key for generation in reversed(newest_first[keep:]) for key in sorted(idle[generation])]
    return retired[:limit]


def _unknown() -> ServerError:
    return ServerError(ErrorCode.VIEW_UNKNOWN, "view is not available")


class RepositoryIndex:
    """Own the small catalogue and the lifetime of all repository connections."""

    def __init__(
        self,
        root: str | Path,
        *,
        content_size_cap: int,
        inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
        clock: Callable[[], float] = time.time,
        max_repository_bytes: int = 64 * 1024**3,
        max_wal_bytes: int = 128 * 1024**2,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._catalog = _Db(str(self.root / "catalog.sqlite3"), reclaim=True)
        self._catalog._conn.executescript(_CATALOG_SCHEMA)
        self.deferred_jobs = SqliteDeferredJobStore(self._catalog)
        self._cap = content_size_cap
        self._inline_budget = inline_rebuild_budget
        self._clock = clock
        self._max_repository_bytes = max_repository_bytes
        self._max_wal_bytes = max_wal_bytes
        self._lock = threading.RLock()
        self._indexes: dict[tuple[str, str], SqliteIndex] = {}
        self._backends: dict[tuple[str, str], IndexBackend] = {}
        self._closed = False
        self.ops = IndexOpsMetrics()
        self.repos = SqliteRepoIdentityService(self._catalog)
        self.backend = IndexBackend(
            content=_ContentRouter(self),
            analysis=_AnalysisRouter(self),
            views=_ViewRouter(self),
            links=_LayerRouter(self, "links"),
            semantic=_LayerRouter(self, "semantic"),
            repos=self.repos,
            provenance=_ProvenanceRouter(self),
            check_ready=self.check_ready,
            close_backend=self.close,
            ops=self.ops,
            deferred_jobs=self.deferred_jobs,
            repository_backend=self.for_repository,
            unbound_backend=self.for_unbound,
            partitions=self.partitions,
            reclaim_storage=self.reclaim_storage,
            disk_usage=self.disk_usage,
            content_io_bound=True,
            # View operations take a repository's write lock, which maintenance
            # and the deferred index worker hold for seconds at a time on a large
            # index. Inline on the event loop, one waiting edit sync stalled every
            # request -- health checks included -- for up to 20 s.
            authority_io_bound=True,
        )
        # Recover an open that committed its view just before a process crash
        # interrupted the catalogue update. Never infer ownership from a path.
        for org_id, repo_id in self.keys():
            index = self.index(org_id, repo_id, create=False)
            assert index is not None
            for row in index._db.rows("SELECT view_id FROM views WHERE org_id=?", (org_id,)):
                self.record_view(org_id, repo_id, str(row["view_id"]))

    def keys(self, org_id: str | None = None) -> tuple[tuple[str, str], ...]:
        where = "" if org_id is None else " WHERE org_id=?"
        return tuple(
            (str(row["org_id"]), str(row["repo_id"]))
            for row in self._catalog.rows(
                "SELECT org_id, repo_id FROM repository_indexes" + where + " ORDER BY org_id, repo_id",
                () if org_id is None else (org_id,),
            )
        )

    def repository_path(self, org_id: str, repo_id: str) -> Path:
        # Full hashes, with unambiguous tuple encoding: arbitrary tenant labels
        # cannot escape the root or alias another repository's database.
        key = hashlib.sha256(json.dumps([org_id, repo_id], separators=(",", ":")).encode()).hexdigest()
        return self.root / "repositories" / key / "index.sqlite3"

    def index(self, org_id: str, repo_id: str, *, create: bool = True) -> SqliteIndex | None:
        key = (org_id, repo_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("repository index is closed")
            existing = self._indexes.get(key)
            if existing is not None:
                return existing
            registered = (
                self._catalog.row("SELECT 1 FROM repository_indexes WHERE org_id=? AND repo_id=?", key) is not None
            )
            if not create and not registered:
                return None
            path = self.repository_path(*key)
            if registered and not path.is_file():
                raise RuntimeError("a registered repository index is missing")
            path.parent.mkdir(parents=True, exist_ok=True)
            index = SqliteIndex(
                path,
                content_size_cap=self._cap,
                inline_rebuild_budget=self._inline_budget,
                clock=self._clock,
                compact=True,
                max_bytes=self._max_repository_bytes,
                max_wal_bytes=self._max_wal_bytes,
                ops=self.ops,
            )
            try:
                with self._catalog.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO repository_indexes VALUES (?, ?)", key)
            except BaseException:
                index.close()
                raise
            self._indexes[key] = index
            self._backends[key] = replace(
                index.backend,
                repos=self.repos,
                views=_RepositoryViews(self, org_id, repo_id, index),
                deferred_jobs=self.deferred_jobs,
                partition_key=key,
            )
            return index

    def for_repository(self, org_id: str, repo_id: str) -> IndexBackend:
        self.index(org_id, repo_id)
        return self._backends[(org_id, repo_id)]

    def for_unbound(self, org_id: str) -> IndexBackend:
        return self.for_repository(org_id, _UNASSIGNED)

    def indexes(self, org_id: str | None = None) -> tuple[SqliteIndex, ...]:
        result = []
        for owner, repository in self.keys(org_id):
            index = self.index(owner, repository, create=False)
            assert index is not None
            result.append(index)
        return tuple(result)

    def partitions(self) -> tuple[IndexBackend, ...]:
        return tuple(self.for_repository(org, repo) for org, repo in self.keys())

    def route(self, org_id: str | None, view_id: str) -> IndexBackend:
        row = self._catalog.row("SELECT org_id, repo_id FROM repository_views WHERE view_id=?", (view_id,))
        if row is None or (org_id is not None and str(row["org_id"]) != org_id):
            raise _unknown()
        return self.for_repository(str(row["org_id"]), str(row["repo_id"]))

    def record_view(self, org_id: str, repo_id: str, view_id: str) -> None:
        with self._catalog.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO repository_views VALUES (?, ?, ?)", (view_id, org_id, repo_id))
            row = conn.execute("SELECT org_id, repo_id FROM repository_views WHERE view_id=?", (view_id,)).fetchone()
            if row is None or (str(row["org_id"]), str(row["repo_id"])) != (org_id, repo_id):
                raise RuntimeError("conflicting repository view ownership")

    def reclaim_storage(self) -> None:
        for org_id, repo_id in self.keys():
            index = self.index(org_id, repo_id, create=False)
            assert index is not None
            # Opens and route reclamation use the same shard -> catalogue lock
            # order, so a just-opened view cannot lose its route to a sweep.
            with index._db._lock:
                routes = self._catalog.rows(
                    "SELECT view_id FROM repository_views WHERE org_id=? AND repo_id=?", (org_id, repo_id)
                )
                obsolete = [
                    (str(row["view_id"]),)
                    for row in routes
                    if index._db.row("SELECT 1 FROM views WHERE view_id=?", (row["view_id"],)) is None
                ]
                if obsolete:
                    with self._catalog.transaction() as conn:
                        conn.executemany("DELETE FROM repository_views WHERE view_id=?", obsolete)
                # Clean Views read generation-scoped graphs. Per-View copies
                # from before generation sharing (or a reverted overlay) are
                # unreachable derived data even while their View lease is live.
                # Bound each pass; dirty Views retain their private graph.
                retired = index._db.rows(
                    "SELECT g.view_id FROM link_graphs g JOIN views v "
                    "ON v.view_id=g.view_id AND v.org_id=g.org_id "
                    "WHERE v.generation_id<>'' "
                    "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=v.view_id) "
                    "ORDER BY g.view_id LIMIT 8"
                )
                retired_keys = [str(graph["view_id"]) for graph in retired]
                # Generation graphs no live View reads, beyond the newest few.
                # Views open under this lock, so none can start reading one of
                # these generations while it is being retired.
                retired_keys += _idle_generation_graphs(
                    index._db, keep=KEEP_IDLE_GENERATION_GRAPHS, limit=_GENERATION_GRAPHS_PER_PASS
                )
                for key in retired_keys:
                    with index._db.transaction() as conn:
                        conn.execute("DELETE FROM link_edges WHERE view_id=?", (key,))
                        conn.execute("DELETE FROM link_graphs WHERE view_id=?", (key,))
                with index._db.transaction() as conn:
                    empty = index._db.scalar_int("SELECT COUNT(*) FROM search_documents") == 0
                    for table in ("search_terms", "search_grams"):
                        if empty:
                            conn.execute(f"INSERT INTO {table}({table}) VALUES ('delete-all')")
                        else:
                            conn.execute(f"INSERT INTO {table}({table}, rank) VALUES ('merge', 64)")
                index._db._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
                index._db._check_wal_budget()
                # Release up to 64 MiB at the default page size per pass. The
                # former 4 MiB pass took days to return a few GiB after cleanup.
                index._db._conn.execute("PRAGMA incremental_vacuum(16384)").fetchall()
                if self._max_repository_bytes:
                    page_size = index._db.scalar_int("PRAGMA page_size")
                    limit = (self._max_repository_bytes - self._max_wal_bytes) // page_size
                    index._db._conn.execute(f"PRAGMA max_page_count={limit}").fetchone()
        with self._catalog._lock:
            self._catalog._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._catalog._conn.execute("PRAGMA incremental_vacuum(256)").fetchall()

    def disk_usage(self, org_id: str) -> int:
        total = 0
        for owner, repository in self.keys(org_id):
            path = self.repository_path(owner, repository)
            for suffix in ("", "-wal", "-shm"):
                try:
                    total += Path(str(path) + suffix).stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def check_ready(self) -> None:
        self._catalog.check_ready()
        with self._lock:
            indexes = tuple(self._indexes.values())
        for index in indexes:
            index.backend.check_ready()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for index in self._indexes.values():
                index.close()
            self._indexes.clear()
            self._backends.clear()
            self._catalog.close()


class _RepositoryViews:
    def __init__(self, owner: RepositoryIndex, org_id: str, repo_id: str, index: SqliteIndex) -> None:
        self._owner, self._org, self._repo, self._index = owner, org_id, repo_id, index

    def open(self, **kwargs: Any) -> Any:
        if (kwargs.get("org_id"), kwargs.get("repo_id")) != (self._org, self._repo):
            raise _unknown()
        with self._index._db._lock:
            result = self._index.backend.views.open(**kwargs)
            try:
                self._owner.record_view(self._org, self._repo, result.state.view_id)
            except BaseException:
                self._index.backend.views.close(self._org, result.state.view_id)
                raise
            return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._index.backend.views, name)


class _ViewRouter:
    def __init__(self, owner: RepositoryIndex) -> None:
        self._owner = owner

    def open(self, **kwargs: Any) -> Any:
        return self._owner.for_repository(kwargs["org_id"], kwargs["repo_id"]).views.open(**kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in {
            "get",
            "put_manifest_chunk",
            "apply_overlay",
            "generation",
            "generation_membership",
            "overlay",
            "entry",
            "entries_for_digests",
            "path_candidates",
            "membership",
            "declared_digests",
            "renew",
            "close",
            "view_owner",
        }:

            def routed(*args: Any, **kwargs: Any) -> Any:
                org_id = args[0] if args else kwargs["org_id"]
                view_id = args[1] if len(args) > 1 else kwargs["view_id"]
                try:
                    backend = self._owner.route(org_id, view_id)
                except ServerError:
                    if name in {"close", "view_owner"}:
                        return None
                    raise
                return getattr(backend.views, name)(*args, **kwargs)

            return routed
        if name in {"view_count", "view_count_for_user", "root_bytes", "view_bytes", "sweep_manifest_roots"}:
            return lambda *args: sum(
                getattr(index.backend.views, name)(*args)
                for index in self._owner.indexes(None if name == "sweep_manifest_roots" else args[0])
            )
        if name in {"view_ids_for_org", "view_ids_for_user", "evict_expired"}:
            return lambda *args: tuple(
                view
                for index in self._owner.indexes(None if name == "evict_expired" else args[0])
                for view in getattr(index.backend.views, name)(*args)
            )
        raise AttributeError(name)

    @property
    def live_views(self) -> int:
        return sum(index._views.live_views for index in self._owner.indexes())

    @property
    def live_roots(self) -> int:
        return sum(index._views.live_roots for index in self._owner.indexes())

    def referenced_digests(self) -> Mapping[str, frozenset[str]]:
        result: dict[str, set[str]] = {}
        for index in self._owner.indexes():
            for org, digests in index.backend.views.referenced_digests().items():
                result.setdefault(org, set()).update(digests)
        return {org: frozenset(digests) for org, digests in result.items()}


class _LayerRouter:
    def __init__(self, owner: RepositoryIndex, layer: str) -> None:
        self._owner, self._layer = owner, layer

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def routed(*args: Any, **kwargs: Any) -> Any:
            if name == "forget_view":
                org_id, view_id = None, args[0] if args else kwargs["view_id"]
            else:
                org_id = args[0] if args else kwargs["org_id"]
                view_id = args[1] if len(args) > 1 else kwargs["view_id"]
            try:
                backend = self._owner.route(org_id, view_id)
            except ServerError:
                if name in {"forget", "forget_view"}:
                    return None
                raise
            return getattr(getattr(backend, self._layer), name)(*args, **kwargs)

        return routed


class _ProvenanceRouter:
    def __init__(self, owner: RepositoryIndex) -> None:
        self._owner = owner

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        return self._owner.for_repository(org_id, repo_id).provenance.record(org_id, repo_id, subject, digests)

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        index = self._owner.index(org_id, repo_id, create=False)
        return frozenset() if index is None else index.backend.provenance.held(org_id, repo_id, subject, digests)

    def forget(self, org_id: str, digests: Sequence[str]) -> int:
        return sum(index.backend.provenance.forget(org_id, digests) for index in self._owner.indexes(org_id))


class _ContentRouter:
    def __init__(self, owner: RepositoryIndex) -> None:
        self._owner = owner

    def get(self, org_id: str, digest: str) -> bytes | None:
        for index in self._owner.indexes(org_id):
            data = index.backend.content.get(org_id, digest)
            if data is not None:
                return data
        return None

    def get_many(self, org_id: str, digests: Sequence[str]) -> Mapping[str, bytes]:
        unresolved = list(dict.fromkeys(digests))
        found: dict[str, bytes] = {}
        for index in self._owner.indexes(org_id):
            if not unresolved:
                break
            batch = index.backend.content.get_many(org_id, unresolved)
            found.update(batch)
            unresolved = [digest for digest in unresolved if digest not in batch]
        return found

    def put(self, org_id: str, digest: str, data: bytes) -> Any:
        return self._owner.for_repository(org_id, _UNASSIGNED).content.put(org_id, digest, data)

    def missing(self, org_id: str, digests: Sequence[str]) -> tuple[str, ...]:
        missing = tuple(digests)
        for index in self._owner.indexes(org_id):
            missing = index.backend.content.missing(org_id, missing)
            if not missing:
                break
        return missing

    def storage_size(self, org_id: str, data: bytes) -> int:
        return len(data)

    def stored_bytes(self, org_id: str) -> int:
        return sum(index.backend.content.stored_bytes(org_id) for index in self._owner.indexes(org_id))

    def blob_count(self, org_id: str) -> int:
        return sum(index.backend.content.blob_count(org_id) for index in self._owner.indexes(org_id))

    def sweep(self, referenced: Mapping[str, frozenset[str]]) -> int:
        return sum(index.backend.content.sweep(referenced) for index in self._owner.indexes())

    def mark_unreferenced(self, referenced: Mapping[str, frozenset[str]], now: float) -> int:
        return sum(index.backend.content.mark_unreferenced(referenced, now) for index in self._owner.indexes())

    def sweep_marked(self, before: float) -> SweepResult:
        raise RuntimeError("garbage collection must run separately in each repository partition")


class _AnalysisRouter:
    def __init__(self, owner: RepositoryIndex) -> None:
        self._owner = owner

    def get(self, org_id: str, digest: str, parser_profile: str) -> Any:
        for index in self._owner.indexes(org_id):
            value = index.backend.analysis.get(org_id, digest, parser_profile)
            if value is not None:
                return value
        return None

    def metadata(self, org_id: str, digest: str, parser_profile: str) -> Any:
        for index in self._owner.indexes(org_id):
            value = index.backend.analysis.metadata(org_id, digest, parser_profile)
            if value is not None:
                return value
        return None

    def ensure(self, org_id: str, *args: Any, **kwargs: Any) -> Any:
        return self._owner.for_repository(org_id, _UNASSIGNED).analysis.ensure(org_id, *args, **kwargs)

    def storage_size(self, org_id: str, artifact: Any) -> int:
        return self._owner.for_repository(org_id, _UNASSIGNED).analysis.storage_size(org_id, artifact)

    def stored_bytes(self, org_id: str) -> int:
        return sum(index.backend.analysis.stored_bytes(org_id) for index in self._owner.indexes(org_id))

    def count(self, org_id: str) -> int:
        return sum(index.backend.analysis.count(org_id) for index in self._owner.indexes(org_id))

    def lexical_matches(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("select a repository backend before searching")

    def definitions_named(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("select a repository backend before searching")

    def nearest(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("select a repository backend before searching")

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        return sum(index.backend.analysis.sweep(org_id, digests) for index in self._owner.indexes(org_id))


def build_repository_backend(
    path: str | Path,
    *,
    content_size_cap: int,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
    max_repository_bytes: int = 64 * 1024**3,
    max_wal_bytes: int = 128 * 1024**2,
) -> IndexBackend:
    legacy = Path(path)
    root = repository_index_root(legacy)
    if legacy.exists():
        from .migration import migration_source, source_signature

        catalog = root / "catalog.sqlite3"
        if not catalog.is_file() or migration_source(catalog) != source_signature(legacy):
            raise RuntimeError(
                "legacy shared index requires migration with the server stopped: "
                f"lemoncrow-server migrate-index --database {legacy}"
            )
        wal = Path(str(legacy) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError("legacy index changed after migration; refusing to serve a stale repository copy")
    return RepositoryIndex(
        root,
        content_size_cap=content_size_cap,
        inline_rebuild_budget=inline_rebuild_budget,
        max_repository_bytes=max_repository_bytes,
        max_wal_bytes=max_wal_bytes,
    ).backend
