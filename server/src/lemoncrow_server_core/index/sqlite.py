"""The durable index: the same contracts, backed by SQLite rows.

This is the product store. It is deliberately *not* a second implementation of
the index -- every algorithm it runs is the one :mod:`.memory` runs. Analysis
comes from :mod:`.analysis`, link resolution from :mod:`.links`, build
scheduling from :mod:`.layers`, query scoring from the shared
:func:`~.analysis.score_match`. What differs is where bytes live, which is the
only thing that should differ, and it is why the conformance suite can assert
that both backends answer identically rather than hope so.

The row layout follows the layers:

*Layer 1.* ``blobs`` holds content keyed ``(org_id, digest)``. ``analysis``
holds the derived artifact for ``(org_id, digest, parser_profile)``. Its
searchable rows are split out into ``analysis_terms``, ``analysis_grams``,
``analysis_defs`` and ``analysis_vectors`` so a query is an indexed lookup
rather than a scan over JSON. Terms, trigrams and vectors are read off the raw
bytes and are identical under every profile, so they are keyed by digest alone;
definitions depend on how the bytes were read, so they carry the profile.

*Layer 2.* ``manifest_roots`` and ``manifest_rows`` are keyed by root, **not**
by view. That is the storage claim expressed as a schema decision: worktrees
that share a file set share one set of rows, and a view costs its ``overlay``
plus its link edges.

*Layer 3.* ``link_edges`` holds resolved edges as queryable rows, and
``link_graphs`` holds the index tables an incremental update needs so it does
not have to re-read every file's facts.

No table anywhere is keyed by content without also being keyed by ``org_id``,
which makes "never deduplicate across organizations" a property of the schema
rather than of whichever query happens to be written next.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import struct
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from ..context import new_opaque_id
from ..errors import AgentAction, ErrorCode, ServerError
from ..read_budget import current_read_budget
from .analysis import LexicalQuery, analyze, cosine, parse_query, score_weighted_match
from .contracts import (
    REPOSITORY_GENERATION_ANALYZER_VERSION,
    REPOSITORY_GENERATION_INDEX_VERSION,
    AnalysisArtifact,
    ContentRef,
    Edge,
    EdgeKind,
    IndexBackend,
    ManifestEntry,
    RepoIdentityClaim,
    RepositoryGeneration,
    SemanticUnit,
    SweepResult,
    SymbolDef,
    ViewContentReachability,
    ViewOpenResult,
    ViewState,
    repository_generation_id,
    sha256_hex,
)
from .layers import DEFAULT_INLINE_REBUILD_BUDGET, LayerSources, LinkLayer
from .links import LinkGraph
from .manifest import manifest_root as canonical_manifest_root
from .ops import IndexOpsMetrics
from .semantic import create_semantic_index

__all__ = ["SCHEMA_VERSION", "SqliteIndex", "build_sqlite_backend", "open_sqlite_index"]

#: 3 -- ``replays`` records the whole acknowledgement, not only its revision,
#: so a lost-ACK retry re-reads the answer it lost instead of the view's
#: current state. See :meth:`SqliteIndex.apply_overlay`.
#:
#: Not every schema change moves this number. The versions above it belong to
#: the ops migration ledger (``ops/migrate.py``), and a change whose migration
#: is *self-checking* -- it asks the store whether it has already been applied
#: -- needs no number to be correct or to be re-runnable. The boundary schema
#: (``content_provenance``, ``views.content_subject``) is one of those; see
#: :meth:`_Db._migrate`.
SCHEMA_VERSION: Final[int] = 3

#: SQLite's bound-parameter ceiling is generous on modern builds and small on
#: old ones. Chunking every ``IN`` list keeps a large view from depending on
#: which libsqlite3 the host happens to ship.
_PARAM_CHUNK: Final[int] = 400
#: Idle read-only connections kept per database (see ``_Db._borrow_reader``).
_IDLE_READERS: Final[int] = 2
#: Statements this process runs at once on read-only connections, across every
#: database. SQLite serializes its allocator process-wide (MEMSTATUS is on in
#: the shipped builds), so parallel statements contend instead of scaling:
#: replaying 32 searches' SQL on 8 threads took 37 s against 15 s one after
#: another, and 11.6 s with at most 2 at once. Separate processes scale.
#: lc-debt: process-wide read cap; upgrade: search in worker processes, or an
#: SQLite built with SQLITE_DEFAULT_MEMSTATUS=0.
_CONCURRENT_READS: Final[int] = 3
_READ_GATE: Final[threading.BoundedSemaphore] = threading.BoundedSemaphore(_CONCURRENT_READS)
#: How long a write over the WAL budget waits for the reader pinning the WAL to
#: finish before refusing. Reads are bounded (``read_budget``), so most finish
#: well inside this; refusing at once turned every such overlap into an error.
#: Never on an event loop thread: local stores run inline there, and a sleep
#: would stall every request. Those refusals are retried by the client instead.
_WAL_PIN_WAIT_S: Final[float] = 2.0


def _on_event_loop() -> bool:
    """Whether this thread is running an asyncio loop, where nothing may sleep."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _read_slot() -> AbstractContextManager[Any]:
    """A turn under :data:`_READ_GATE`; an event loop never queues behind executor reads."""
    return nullcontext() if _on_event_loop() else _READ_GATE


_REPLAY_HISTORY: Final[int] = 64

#: The content scope given to a view that predates the boundary. It begins with
#: a hyphen, so ``validate_opaque_id`` can never mint a subject that spells it
#: and no possession row can ever name it: such a view addresses nothing until
#: it is reopened. Fail closed, and visibly so.
_NO_SCOPE: Final[str] = "-pre-boundary-"
#: Existing shared views predate durable opener ownership. They cannot be
#: assigned safely, so they fail closed until reopened.
_NO_OWNER: Final[str] = "-pre-owner-"

#: Tables whose rows count toward a byte census: (table, counter kind, SQL for one row's bytes).
#: ``{r}`` is ``NEW`` or ``OLD``. These expressions are the definition of
#: ``stored_bytes`` -- the storage census the quota governor asks on every upload.
_COUNTED: Final[tuple[tuple[str, str, str], ...]] = (
    ("blobs", "content", "{r}.size"),
    ("analysis", "analysis", "{r}.payload_bytes"),
    ("analysis_terms", "analysis", "LENGTH({r}.term)"),
    ("analysis_grams", "analysis", "LENGTH({r}.gram)"),
    ("analysis_vectors", "analysis", "LENGTH({r}.vector)"),
    ("analysis_semantic_units", "analysis", "{r}.payload_bytes"),
)

# Trigram ingest is the only extremely high-cardinality write path: a medium
# repository can add millions of rows during its first sync. Updating the same
# storage_counters row through an AFTER INSERT trigger for every gram turns that
# into tens of gigabytes of SQLite/WAL writes. Keep grams in the logical census,
# but account for them once per analysis transaction instead. All other tables
# retain trigger-based accounting because their row counts are comparatively
# small and they have more than one write path.
_TRIGGER_COUNTED: Final[tuple[tuple[str, str, str], ...]] = tuple(
    item for item in _COUNTED if item[0] != "analysis_grams"
)

#: Marker row (its own ``kind``, so it can never be read as a count) saying the
#: counters were derived from the tables and are kept by the triggers since.
_COUNTERS_READY: Final[str] = "_ready"
_GRAM_STATS_READY: Final[str] = "_gram_stats_ready"
_PATH_SEARCH_READY: Final[str] = "_path_search_ready"


def _counter_schema() -> str:
    """Byte counters kept by triggers, so ``stored_bytes`` is a point read.

    The census was ``SUM(LENGTH(...))`` over the derived tables: a full scan of
    multi-gigabyte tables on every upload and on every start. Triggers keep the
    same numbers current in the writing transaction, so they cover every write
    path -- ingest, sweep, blob GC, purge -- with no caller to forget. Rows that
    ``INSERT OR IGNORE`` drops and the ``ON CONFLICT DO UPDATE`` path of a blob
    re-put fire no insert trigger, and neither changes a byte total.
    """
    parts = [
        "CREATE TABLE IF NOT EXISTS storage_counters (\n"
        "    org_id TEXT NOT NULL,\n"
        "    kind   TEXT NOT NULL,\n"
        "    bytes  INTEGER NOT NULL,\n"
        "    PRIMARY KEY (org_id, kind)\n"
        ") WITHOUT ROWID;"
    ]
    for table, kind, size in _TRIGGER_COUNTED:
        for event, row, sign in (("INSERT", "NEW", ""), ("DELETE", "OLD", "-")):
            amount = size.format(r=row)
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_counter_{table}_{event.lower()} "
                f"AFTER {event} ON {table} BEGIN "
                f"INSERT INTO storage_counters (org_id, kind, bytes) "
                f"VALUES ({row}.org_id, '{kind}', {sign}({amount})) "
                f"ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes; "
                f"END;"
            )
    return "\n".join(parts) + "\n"


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS blobs (
    org_id              TEXT    NOT NULL,
    digest              TEXT    NOT NULL,
    size                INTEGER NOT NULL,
    data                BLOB    NOT NULL,
    created_at          REAL    NOT NULL,
    unreferenced_since  REAL,
    PRIMARY KEY (org_id, digest)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_blobs_unreferenced ON blobs (unreferenced_since);
-- "Do you already hold this digest?" is asked once per manifest row on every
-- ``views/open``, and the primary key cannot answer it cheaply: this is a
-- WITHOUT ROWID table, so its key *is* the row, and every probe drags a leaf
-- page carrying content with it. At 50,000 files over 300 MB that was 800 ms
-- of a warm SessionStart. A covering index over just the two key columns is a
-- few megabytes, stays in the page cache, and answers the same question in
-- about 70 ms -- and, being small, keeps that cost independent of what the
-- operating system happens to have cached: with the cache dropped between runs
-- the same check was 7.1 s without this index and 79 ms with it. What it costs
-- is on the ingest side, measured over 4,000 blobs: about 23% more bytes written
-- and 3% more space. It back-fills itself: the schema script runs on every open.
CREATE INDEX IF NOT EXISTS ix_blobs_present ON blobs (org_id, digest);

CREATE TABLE IF NOT EXISTS analysis (
    org_id          TEXT    NOT NULL,
    digest          TEXT    NOT NULL,
    parser_profile  TEXT    NOT NULL,
    language        TEXT    NOT NULL,
    line_count      INTEGER NOT NULL,
    byte_count      INTEGER NOT NULL,
    payload         TEXT    NOT NULL,
    payload_bytes   INTEGER NOT NULL,
    PRIMARY KEY (org_id, digest, parser_profile)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS analysis_terms (
    org_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    term   TEXT NOT NULL,
    PRIMARY KEY (org_id, digest, term)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_analysis_terms ON analysis_terms (org_id, term);

CREATE TABLE IF NOT EXISTS analysis_grams (
    org_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    gram   TEXT NOT NULL,
    PRIMARY KEY (org_id, digest, gram)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_analysis_grams ON analysis_grams (org_id, gram);

-- Query-planning hint only. These counts choose a selective trigram posting
-- list; the selected candidates are always verified against analysis_grams, so
-- stale counts cannot change search correctness. Existing stores backfill this
-- once, while grams added later fall back to a live count on first use.
CREATE TABLE IF NOT EXISTS analysis_gram_stats (
    org_id TEXT NOT NULL,
    gram   TEXT NOT NULL,
    n      INTEGER NOT NULL,
    PRIMARY KEY (org_id, gram)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS analysis_defs (
    org_id          TEXT    NOT NULL,
    digest          TEXT    NOT NULL,
    parser_profile  TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    line            INTEGER NOT NULL,
    span_start      INTEGER NOT NULL,
    span_end        INTEGER NOT NULL,
    exported        INTEGER NOT NULL,
    PRIMARY KEY (org_id, digest, parser_profile, name, span_start)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_analysis_defs ON analysis_defs (org_id, name);

CREATE TABLE IF NOT EXISTS analysis_vectors (
    org_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (org_id, digest)
) WITHOUT ROWID;

-- Chunk vectors for an artifact analyzed without them. Uploads defer them
-- (``analyze_foreground``) and the semantic builder derives them from the
-- digest's bytes; kept here, a restart reads them instead of deriving them
-- again. Layer-1 data like the rows above: it follows its digest out on sweep.
-- ``units`` is JSON [unit_id, start_line, end_line, symbol, dims] per unit;
-- ``vectors`` their embeddings, packed float64 back to back.
CREATE TABLE IF NOT EXISTS analysis_semantic_units (
    org_id            TEXT    NOT NULL,
    digest            TEXT    NOT NULL,
    parser_profile    TEXT    NOT NULL,
    embedding_version TEXT    NOT NULL,
    units             TEXT    NOT NULL,
    vectors           BLOB    NOT NULL,
    payload_bytes     INTEGER NOT NULL,
    PRIMARY KEY (org_id, digest, parser_profile, embedding_version)
) WITHOUT ROWID;

-- Who actually handed the server these bytes, and for which repository. Never
-- written by a manifest row: only by a verified upload, or a verified read off
-- a proven same-host worktree. A digest with no row here is addressable by no
-- view at all, which is what makes declaring a digest stop being possessing the
-- file. Rows follow their content out on GC, so a re-uploaded digest never
-- inherits a scope that demonstrated nothing.
CREATE TABLE IF NOT EXISTS content_provenance (
    org_id  TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    digest  TEXT NOT NULL,
    PRIMARY KEY (org_id, repo_id, subject, digest)
) WITHOUT ROWID;
-- The repository-wide read, for a view whose opener holds a binding naming the
-- repository as a sharing unit. Without it that read is a scan of the primary
-- key across every subject in the tenant.
CREATE INDEX IF NOT EXISTS ix_provenance_repo ON content_provenance (org_id, repo_id, digest);
-- And the GC read, which knows only the digests whose bytes it just deleted.
CREATE INDEX IF NOT EXISTS ix_provenance_digest ON content_provenance (org_id, digest);

CREATE TABLE IF NOT EXISTS repos (
    org_id    TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    repo_id   TEXT NOT NULL,
    PRIMARY KEY (org_id, claim_key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS manifest_roots (
    org_id             TEXT    NOT NULL,
    root               TEXT    NOT NULL,
    complete           INTEGER NOT NULL DEFAULT 0,
    -- When a view last named this root. A root outlives the views that named
    -- it, so this is what the retention window is measured from.
    last_referenced_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, root)
) WITHOUT ROWID;

-- What one *view* announced at ``views/open`` and what it has since
-- delivered. Keyed by view rather than by root: a root is shared by everyone
-- who names it, so a root-keyed pending set let any principal who reached
-- another engineer's in-flight root add a chunk to what she owed -- and a row
-- with it, which her assembly then could never hash past.
CREATE TABLE IF NOT EXISTS manifest_chunks (
    view_id   TEXT    NOT NULL,
    chunk_id  TEXT    NOT NULL,
    delivered INTEGER NOT NULL,
    PRIMARY KEY (view_id, chunk_id)
) WITHOUT ROWID;

-- Rows a fill has delivered and nothing has yet proved. Private to the view
-- that delivered them, and deleted with it; they become ``manifest_rows`` in
-- one step, at the moment their own assembly hashes to the announced root.
CREATE TABLE IF NOT EXISTS manifest_staging (
    view_id        TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    content_digest TEXT    NOT NULL,
    parser_profile TEXT    NOT NULL,
    mode           INTEGER NOT NULL,
    size           INTEGER NOT NULL,
    PRIMARY KEY (view_id, path)
) WITHOUT ROWID;

-- Proved rows only: the manifest a root names, written once by the view whose
-- assembly hashed to it and shared by every view that names it afterwards.
CREATE TABLE IF NOT EXISTS manifest_rows (
    org_id         TEXT    NOT NULL,
    root           TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    content_digest TEXT    NOT NULL,
    parser_profile TEXT    NOT NULL,
    mode           INTEGER NOT NULL,
    size           INTEGER NOT NULL,
    PRIMARY KEY (org_id, root, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_manifest_rows_digest ON manifest_rows (org_id, root, content_digest);
CREATE INDEX IF NOT EXISTS ix_manifest_staging_digest ON manifest_staging (view_id, content_digest);

-- FTS5's trigram tokenizer gives exact case-insensitive substring candidates
-- for path needles of three or more characters. Keep this generation-scoped:
-- immutable roots pay the index once, while tiny per-view overlays remain cheap
-- to scan directly. The authoritative manifest row is still joined/rechecked.
CREATE VIRTUAL TABLE IF NOT EXISTS manifest_path_search USING fts5(
    org_id UNINDEXED,
    root UNINDEXED,
    path,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS repository_generations (
    org_id           TEXT NOT NULL,
    generation_id    TEXT NOT NULL,
    repo_id           TEXT NOT NULL,
    base_revision     TEXT NOT NULL,
    root              TEXT NOT NULL,
    entry_count       INTEGER NOT NULL DEFAULT 0,
    analyzer_version  TEXT NOT NULL,
    index_version     TEXT NOT NULL,
    PRIMARY KEY (org_id, generation_id),
    UNIQUE (org_id, repo_id, base_revision, root, analyzer_version, index_version),
    FOREIGN KEY (org_id, root) REFERENCES manifest_roots (org_id, root) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_generations_repo_revision
    ON repository_generations (org_id, repo_id, base_revision);

CREATE TABLE IF NOT EXISTS views (
    view_id          TEXT    PRIMARY KEY,
    org_id           TEXT    NOT NULL,
    repo_id          TEXT    NOT NULL,
    base_revision    TEXT    NOT NULL,
    generation_id    TEXT    NOT NULL DEFAULT '',
    root             TEXT    NOT NULL,
    revision         INTEGER NOT NULL,
    lease_expires_at REAL    NOT NULL,
    last_client_seq  INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    -- Whose demonstrated possession this view reads content through, inside
    -- its repository. Empty is the repository as a whole; a subject is that
    -- engineer alone. Fixed at open, because the derived layers and the
    -- materialized tree are cached per view and a reach that widened for one
    -- caller would stay cached for the next.
    content_subject  TEXT    NOT NULL DEFAULT '',
    owner_subject    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_views_org ON views (org_id);
CREATE INDEX IF NOT EXISTS ix_views_lease ON views (lease_expires_at);

CREATE TABLE IF NOT EXISTS overlay (
    view_id        TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    content_digest TEXT    NOT NULL,
    parser_profile TEXT    NOT NULL,
    mode           INTEGER NOT NULL,
    size           INTEGER NOT NULL,
    tombstone      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (view_id, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_overlay_digest ON overlay (view_id, content_digest, tombstone);

-- The acknowledgement a write produced, kept verbatim. Storing only the
-- revision was a bug: the replay branch then had nothing to answer with but
-- the view's *current* state, so a retry that raced another writer's commit
-- reported a revision -- and an entry count -- the retrying client had never
-- observed, and the client adopted it as its bound revision.
CREATE TABLE IF NOT EXISTS replays (
    view_id           TEXT    NOT NULL,
    client_seq        INTEGER NOT NULL,
    revision          INTEGER NOT NULL,
    entry_count       INTEGER NOT NULL,
    lease_expires_at  REAL    NOT NULL,
    pending_chunks    TEXT    NOT NULL,
    manifest_complete INTEGER NOT NULL,
    PRIMARY KEY (view_id, client_seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS link_edges (
    view_id  TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    src_path TEXT    NOT NULL,
    dst_path TEXT    NOT NULL,
    symbol   TEXT    NOT NULL,
    src_line INTEGER NOT NULL,
    PRIMARY KEY (view_id, kind, src_path, dst_path, symbol, src_line)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_link_edges_src ON link_edges (view_id, src_path);
CREATE INDEX IF NOT EXISTS ix_link_edges_dst ON link_edges (view_id, dst_path);

CREATE TABLE IF NOT EXISTS link_graphs (
    view_id      TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    -- A dirty View's graph is stored as ``delta`` over its generation graph
    -- ``base`` (see SqliteLinkGraphStore.save); empty for a full edge set.
    base         TEXT NOT NULL DEFAULT '',
    delta        TEXT NOT NULL DEFAULT '',
    -- Durable cache identity. Payload excludes edges, and edge counts can stay
    -- constant across a rewrite, so neither is sufficient to identify a graph.
    edge_version INTEGER NOT NULL DEFAULT 0
);
""" + _counter_schema()  # triggers name the tables above, so they come last


_EdgeRow = tuple[str, str, str, str, int]
_EDGE_COLUMNS: Final[str] = "kind, src_path, dst_path, symbol, src_line"
#: A dirty View's graph is kept as a delta over its generation graph while the
#: delta is at most this many edges or 1/_DELTA_FRACTION of the graph, whichever
#: is more. At a tenth, a worktree with a few hundred uncommitted files (~12% of
#: a 168k-edge graph) stored every agent's View as a full copy, and each edit
#: re-read and rewrote ~168k rows per View; a quarter keeps those as deltas.
_DELTA_MIN_EDGES: Final[int] = 2_000
_DELTA_FRACTION: Final[int] = 4
#: Full link graphs kept decoded per store (a few generations per repository).
# lc-debt: count-bounded (~50 MB per 150k-edge graph); bound by edge total if many dirty Views churn.
_FULL_GRAPH_CACHE: Final[int] = 4


def _edge_rows(rows: Iterator[Any] | Sequence[Any]) -> set[_EdgeRow]:
    return {(str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4])) for row in rows}


def _as_edge(row: _EdgeRow) -> Edge:
    return Edge(kind=EdgeKind(row[0]), src_path=row[1], dst_path=row[2], symbol=row[3], src_line=row[4])


def _edge_row(edge: Edge) -> _EdgeRow:
    return (edge.kind.value, edge.src_path, edge.dst_path, edge.symbol, edge.src_line)


def _encode_delta(added: set[_EdgeRow], removed: set[_EdgeRow]) -> str:
    return json.dumps({"added": sorted(added), "removed": sorted(removed)}, separators=(",", ":"), ensure_ascii=False)


def _decode_delta(raw: str) -> tuple[set[_EdgeRow], set[_EdgeRow]]:
    data = json.loads(raw) if raw else {}
    return (
        {(str(a), str(b), str(c), str(d), int(e)) for a, b, c, d, e in data.get("added", ())},
        {(str(a), str(b), str(c), str(d), int(e)) for a, b, c, d, e in data.get("removed", ())},
    )


def _chunks(values: Sequence[str], size: int = _PARAM_CHUNK) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}d", *vector)


def _unpack(raw: bytes) -> tuple[float, ...]:
    if not raw or len(raw) % 8:
        return ()
    return struct.unpack(f"<{len(raw) // 8}d", raw)


def _encode_semantic_units(units: Sequence[SemanticUnit]) -> tuple[str, bytes]:
    meta = json.dumps(
        [[unit.unit_id, unit.start_line, unit.end_line, unit.symbol, len(unit.embedding)] for unit in units],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return meta, b"".join(_pack(unit.embedding) for unit in units)


def _decode_semantic_units(meta: str, vectors: bytes, embedding_version: str) -> tuple[SemanticUnit, ...]:
    values = _unpack(vectors)
    units: list[SemanticUnit] = []
    offset = 0
    for unit_id, start_line, end_line, symbol, dims in json.loads(meta):
        units.append(
            SemanticUnit(
                unit_id=str(unit_id),
                start_line=int(start_line),
                end_line=int(end_line),
                symbol=str(symbol),
                embedding_version=embedding_version,
                embedding=tuple(values[offset : offset + int(dims)]),
            )
        )
        offset += int(dims)
    return tuple(units)


def _absolute(path: str) -> str:
    """Pin a filesystem database path before the process can change directory.

    Tool dispatch binds the working directory to the tenant's tree for the
    duration of a call (hosted ``lemoncrow_server.registry_dispatch._workspace_binding``), because the
    public tools relativize their output against the process cwd. A durable
    index opened at a relative path would then follow that binding and land
    somewhere else, so the path is resolved once, here, at open time.

    ``:memory:`` and SQLite's ``file:`` URIs are left exactly as given.
    """
    if path == ":memory:" or path.startswith("file:"):
        return path
    return str(Path(path).resolve())


class _Db:
    """One writer connection and lock, explicit transactions, per-thread readers.

    Writes stay on a single serialized connection: one writer removes an entire
    class of interleaving bug from a store whose correctness the differential
    oracle depends on. Reads used to share that lock, so one slow read stalled
    every other request. A durable database is WAL, where readers never block
    the writer or each other, so each thread reads on its own read-only
    connection. A thread inside its own write transaction still reads on the
    writer, so it sees its uncommitted rows.
    """

    __slots__ = (
        "_closed",
        "_conn",
        "_depth",
        "_idle_readers",
        "_lock",
        "_max_wal_bytes",
        "_owner",
        "_reader_lock",
        "_reader_uri",
        "path",
    )

    def __init__(self, path: str, *, reclaim: bool = False, max_bytes: int = 0, max_wal_bytes: int = 0) -> None:
        self.path = _absolute(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        self._depth = 0
        self._max_wal_bytes = max_wal_bytes
        self._owner: int | None = None
        self._closed = False
        self._idle_readers: list[sqlite3.Connection] = []
        # Not ``_lock``: a writer holds that for its whole transaction, and a
        # read must not wait on it just to borrow a connection.
        self._reader_lock = threading.Lock()
        durable = path != ":memory:" and not path.startswith("file:")
        self._reader_uri = f"file:{urllib.parse.quote(self.path)}?mode=ro" if durable else ""
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if reclaim:
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._conn.execute("PRAGMA journal_size_limit = 16777216")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            # The default page cache is 2 MB against a multi-gigabyte index.
            self._conn.execute("PRAGMA cache_size = -8192" if reclaim else "PRAGMA cache_size = -65536")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._backfill_generations()
        self._backfill_counters()
        self._backfill_path_search()
        self._backfill_gram_stats()
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if max_bytes:
            if max_bytes <= max_wal_bytes:
                raise ValueError("repository index budget must exceed its WAL budget")
            page_size = int(self._conn.execute("PRAGMA page_size").fetchone()[0])
            page_limit = (max_bytes - max_wal_bytes) // page_size
            actual = int(self._conn.execute(f"PRAGMA max_page_count = {page_limit}").fetchone()[0])
            # SQLite cannot lower the ceiling below existing pages. Keep its
            # returned no-growth ceiling so reads and reclamation remain usable
            # after a budget reduction or schema upgrade; rejecting startup
            # would also disable the maintenance needed to get back under budget.
            assert actual >= page_limit

    def _backfill_generations(self) -> None:
        """Attach pre-I3 Views to deterministic immutable generations."""
        rows = self._conn.execute(
            "SELECT view_id, org_id, repo_id, base_revision, root, generation_id FROM views"
        ).fetchall()
        if not rows:
            return
        with self.transaction() as conn:
            for row in rows:
                org_id = str(row["org_id"])
                repo_id = str(row["repo_id"])
                base_revision = str(row["base_revision"])
                root = str(row["root"])
                expected = repository_generation_id(org_id, repo_id, base_revision, root)
                current = str(row["generation_id"] or "")
                if current and current != expected:
                    raise RuntimeError("stored View generation identity does not match its immutable base")
                count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM manifest_rows WHERE org_id=? AND root=?",
                    (org_id, root),
                ).fetchone()
                entry_count = 0 if count_row is None else int(count_row["n"])
                conn.execute(
                    """
                    INSERT OR IGNORE INTO repository_generations
                        (org_id, generation_id, repo_id, base_revision, root, entry_count,
                         analyzer_version, index_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        org_id,
                        expected,
                        repo_id,
                        base_revision,
                        root,
                        entry_count,
                        REPOSITORY_GENERATION_ANALYZER_VERSION,
                        REPOSITORY_GENERATION_INDEX_VERSION,
                    ),
                )
                if current != expected:
                    conn.execute("UPDATE views SET generation_id=? WHERE view_id=?", (expected, row["view_id"]))

    def _backfill_counters(self) -> None:
        """Derive the byte counters once for a store that predates them.

        Self-checking rather than versioned, like the other steps here: the
        marker row is written in the same transaction as the counts, so an
        interrupted backfill rolls back whole and runs again. The scan happens
        exactly once per store, never per start or per upload.
        """
        if self._conn.execute(
            "SELECT 1 FROM storage_counters WHERE org_id = '' AND kind = ?", (_COUNTERS_READY,)
        ).fetchone():
            return
        with self.transaction() as conn:
            conn.execute("DELETE FROM storage_counters")
            for kind in sorted({kind for _table, kind, _size in _COUNTED}):
                rows = " UNION ALL ".join(
                    f"SELECT org_id, SUM({size.format(r=table)}) AS bytes FROM {table} GROUP BY org_id"
                    for table, counted, size in _COUNTED
                    if counted == kind
                )
                conn.execute(
                    "INSERT INTO storage_counters (org_id, kind, bytes) "
                    f"SELECT org_id, ?, SUM(bytes) FROM ({rows}) GROUP BY org_id",
                    (kind,),
                )
            conn.execute("INSERT INTO storage_counters (org_id, kind, bytes) VALUES ('', ?, 0)", (_COUNTERS_READY,))

    def _backfill_path_search(self) -> None:
        """Build the immutable-root path substring index once for older stores."""
        if self._conn.execute(
            "SELECT 1 FROM storage_counters WHERE org_id = '' AND kind = ?", (_PATH_SEARCH_READY,)
        ).fetchone():
            return
        with self.transaction() as conn:
            conn.execute("DELETE FROM manifest_path_search")
            conn.execute(
                "INSERT INTO manifest_path_search (org_id, root, path) SELECT org_id, root, path FROM manifest_rows"
            )
            conn.execute(
                "INSERT INTO storage_counters (org_id, kind, bytes) VALUES ('', ?, 0) "
                "ON CONFLICT (org_id, kind) DO UPDATE SET bytes = 0",
                (_PATH_SEARCH_READY,),
            )

    def _backfill_gram_stats(self) -> None:
        """Snapshot trigram posting sizes once for rarest-gram query planning.

        The table is deliberately a hint rather than a write-maintained index.
        Search still verifies every candidate against ``analysis_grams``; stale
        positive counts only choose a less-optimal seed, while a gram absent
        from the snapshot falls back to a live count. This keeps ingest writes
        unchanged while taking the expensive ``COUNT/GROUP BY`` work off the
        query path.
        """
        if self._conn.execute(
            "SELECT 1 FROM storage_counters WHERE org_id = '' AND kind = ?", (_GRAM_STATS_READY,)
        ).fetchone():
            return
        with self.transaction() as conn:
            conn.execute("DELETE FROM analysis_gram_stats")
            conn.execute(
                "INSERT INTO analysis_gram_stats (org_id, gram, n) "
                "SELECT org_id, gram, COUNT(*) FROM analysis_grams GROUP BY org_id, gram"
            )
            conn.execute(
                "INSERT INTO storage_counters (org_id, kind, bytes) VALUES ('', ?, 0) "
                "ON CONFLICT (org_id, kind) DO UPDATE SET bytes = 0",
                (_GRAM_STATS_READY,),
            )

    def check_ready(self) -> None:
        """Cheap readiness proof for the authoritative SQLite store.

        A liveness probe must never touch storage. Readiness may, but it must not
        contend with ordinary writes every five seconds. A point read proves the
        live connection/database is usable; the filesystem checks catch a
        disappeared/read-only/full durable volume without starting a write
        transaction. The next real write remains the final authority.
        """

        if self._closed:
            raise RuntimeError("SQLite index is closed")
        self._conn.execute("SELECT 1").fetchone()
        if self.path == ":memory:" or self.path.startswith("file:"):
            return
        parent = Path(self.path).parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise OSError(f"SQLite parent directory is not writable: {parent}")
        stats = os.statvfs(parent)
        if stats.f_bavail <= 0 or stats.f_frsize <= 0:
            raise OSError(f"SQLite filesystem has no writable free blocks: {parent}")

    def counter(self, org_id: str, kind: str) -> int:
        """Bytes of ``kind`` held for the organization -- a point read."""
        return self.scalar_int("SELECT bytes FROM storage_counters WHERE org_id = ? AND kind = ?", (org_id, kind))

    def _migrate(self) -> None:
        """Bring an older database forward before the schema script runs.

        ``CREATE TABLE IF NOT EXISTS`` never widens a table that already
        exists, so a column added to an existing table has to be dealt with
        here or the next query fails on a live deployment.

        ``replays`` is a bounded, per-view idempotency cache for writes that are
        still in flight -- so it is dropped rather than back-filled. Dropping it
        cannot cause a double commit: a retry whose replay row is gone is
        refused by the monotonic ``client_seq`` check (``client_seq <=
        last_client_seq``) instead of being applied a second time. Fail-closed,
        and the client's own retry path treats that refusal as the write having
        landed already.

        ``views.content_subject`` is added *unconditionally and closed*, outside
        the version ladder. Outside, because the versions above
        :data:`SCHEMA_VERSION` belong to the ops ledger and this step asks the
        store whether it has already run rather than asking a number. Closed,
        because a view carried across from a store that predates the boundary
        was opened before anything recorded who had demonstrated possession, so
        there is no honest scope to give it: :data:`_NO_SCOPE` matches no
        possession row and no subject this server can mint, which costs whoever
        holds that view one reopen and costs a stranger holding its identifier
        nothing at all. ``content_provenance`` starts empty for the same reason,
        so the first sync after an upgrade re-uploads -- once, per engineer, per
        repository -- and every sync after it is warm again.

        ``manifest_chunks`` moves from the root to the *view*, and the two are
        not convertible: a root-keyed pending set does not record which view
        announced what, and the rows an unproved fill had already merged into
        ``manifest_rows`` do not record who delivered them. Both are therefore
        dropped rather than back-filled, which costs a fill that was in flight
        across the upgrade one re-announcement -- the client sends the chunk
        ids it always sends at ``views/open`` and the server asks for all of
        them. A *proved* root is untouched, so every warm open stays warm.
        """
        # Older stores may already have the per-row gram counter triggers.
        # They must be retired before any new analysis writes happen or the
        # batched accounting in SqliteAnalysisStore would double-count grams.
        self._conn.execute("DROP TRIGGER IF EXISTS trg_counter_analysis_grams_insert")
        self._conn.execute("DROP TRIGGER IF EXISTS trg_counter_analysis_grams_delete")
        if self._has_table("views") and not self._has_column("views", "content_subject"):
            self._conn.execute(f"ALTER TABLE views ADD COLUMN content_subject TEXT NOT NULL DEFAULT '{_NO_SCOPE}'")
        if self._has_table("views") and not self._has_column("views", "generation_id"):
            self._conn.execute("ALTER TABLE views ADD COLUMN generation_id TEXT NOT NULL DEFAULT ''")
        if self._has_table("views") and not self._has_column("views", "owner_subject"):
            self._conn.execute(f"ALTER TABLE views ADD COLUMN owner_subject TEXT NOT NULL DEFAULT '{_NO_OWNER}'")
            self._conn.execute(
                "UPDATE views SET owner_subject = content_subject "
                "WHERE owner_subject = ? AND content_subject NOT IN ('', ?)",
                (_NO_OWNER, _NO_SCOPE),
            )
        for column in ("base", "delta"):
            if self._has_table("link_graphs") and not self._has_column("link_graphs", column):
                self._conn.execute(f"ALTER TABLE link_graphs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if self._has_table("link_graphs") and not self._has_column("link_graphs", "edge_version"):
            self._conn.execute("ALTER TABLE link_graphs ADD COLUMN edge_version INTEGER NOT NULL DEFAULT 0")
        if self._has_table("manifest_chunks") and not self._has_column("manifest_chunks", "view_id"):
            self._conn.execute(
                "DELETE FROM manifest_rows WHERE (org_id, root) IN "
                "(SELECT org_id, root FROM manifest_roots WHERE complete = 0)"
            )
            self._conn.execute("DROP TABLE manifest_chunks")
        row = self._conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        if version == 0 or version >= SCHEMA_VERSION:
            # 0 is a database that does not exist yet; the schema script below
            # creates it at the current version.
            return
        if version < 3:
            self._conn.execute("DROP TABLE IF EXISTS replays")

    def _has_table(self, table: str) -> bool:
        found = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        return found is not None

    def _has_column(self, table: str, column: str) -> bool:
        """Whether the column is already there. Every step here must be re-runnable.

        A migration that assumed it ran exactly once would turn an interrupted
        upgrade -- or a restore that recorded a different ``user_version`` than
        the file actually carries -- into a server that cannot open its own
        store.
        """
        cursor = self._conn.execute(f"PRAGMA table_info({table})")
        try:
            return any(str(row[1]) == column for row in cursor.fetchall())
        finally:
            cursor.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize and make the write atomic. Re-entrant."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._conn
                finally:
                    self._depth -= 1
                return
            self._check_wal_budget()
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            self._owner = threading.get_ident()
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if (
                    isinstance(exc, sqlite3.OperationalError)
                    and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
                ):
                    raise ServerError(
                        ErrorCode.STORAGE_LIMIT,
                        "repository index has reached its disk budget or the filesystem is full",
                    ) from exc
                raise
            finally:
                self._depth = 0
                self._owner = None

    def _check_wal_budget(self) -> None:
        if not self._max_wal_bytes or not self._reader_uri:
            return
        wal = Path(self.path + "-wal")
        try:
            size = wal.stat().st_size
        except FileNotFoundError:
            return
        if size <= self._max_wal_bytes:
            return
        # A long-lived reader can pin the WAL despite automatic checkpoints.
        # Apply backpressure before the next bounded write, never keep appending.
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        timeout = int(self._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        self._conn.execute("PRAGMA busy_timeout=0")
        deadline = time.monotonic() + (0.0 if _on_event_loop() else _WAL_PIN_WAIT_S)
        delay = 0.05
        try:
            while True:
                busy = int(self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
                remaining = deadline - time.monotonic()
                if not busy or remaining <= 0:
                    break
                # Backpressure: wait for the pinning read to end, bounded.
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.4)
        finally:
            self._conn.execute(f"PRAGMA busy_timeout={timeout}")
        if busy:
            raise ServerError(
                ErrorCode.STORAGE_LIMIT,
                "repository WAL is pinned by a reader; retry after that read completes",
                retryable=True,
                action=AgentAction.RETRY_LATER,
                details={"kind": "wal", "limit_bytes": self._max_wal_bytes},
            )

    def _borrow_reader(self) -> sqlite3.Connection | None:
        """An idle read-only connection, or ``None`` to read on the writer.

        Readers are pooled per database, not held per thread: a per-thread
        reader lived as long as the process, so every executor thread that
        touched a repository kept its own connection and page cache. Maintenance
        passes land on rotating threads, and each added a connection and up to
        16 MB per repository -- gigabytes on a store with many repositories.
        """
        if not self._reader_uri or self._closed or self._owner == threading.get_ident():
            return None
        with self._reader_lock:
            if self._idle_readers:
                return self._idle_readers.pop()
        reader = sqlite3.connect(self._reader_uri, uri=True, check_same_thread=False, isolation_level=None)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA busy_timeout = 5000")
        reader.execute("PRAGMA cache_size = -16384")
        return reader

    def _return_reader(self, reader: sqlite3.Connection) -> None:
        with self._reader_lock:
            if not self._closed and len(self._idle_readers) < _IDLE_READERS:
                self._idle_readers.append(reader)
                return
        with suppress(sqlite3.Error):
            reader.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """One consistent SQLite snapshot across multiple related SELECTs.

        WAL readers normally run each SELECT in its own implicit snapshot. That
        is ideal for unrelated reads, but compound records such as a link-graph
        payload plus its edge rows must not straddle a concurrent writer commit.
        A durable store gets a short explicit read transaction on one pooled
        reader; an in-memory store holds the writer lock for the same guarantee.
        """

        if self._reader_uri and self._owner != threading.get_ident():
            with _read_slot():
                reader = self._borrow_reader()
                if reader is not None:
                    try:
                        reader.execute("BEGIN")
                        yield reader
                    finally:
                        if reader.in_transaction:
                            reader.execute("ROLLBACK")
                        self._return_reader(reader)
                    return
        with self._lock:
            yield self._conn

    def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if self._reader_uri and self._owner != threading.get_ident():
            with _read_slot():
                reader = self._borrow_reader()
                if reader is not None:
                    try:
                        return self._read_rows(reader, sql, parameters)
                    finally:
                        self._return_reader(reader)
        with self._lock:
            return self._read_rows(self._conn, sql, parameters)

    @staticmethod
    def _read_rows(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any]) -> list[sqlite3.Row]:
        budget = current_read_budget()
        if budget is not None:
            budget.check()
            connection.set_progress_handler(budget.interrupted, 1000)
        try:
            cursor = connection.execute(sql, tuple(parameters))
            try:
                result = cursor.fetchall()
                if budget is not None:
                    budget.check()
                return result
            finally:
                cursor.close()
        except sqlite3.OperationalError as exc:
            if budget is not None and exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                budget.check()
            raise
        finally:
            if budget is not None:
                connection.set_progress_handler(None, 0)

    def row(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        found = self.rows(sql, parameters)
        return found[0] if found else None

    def scalar_int(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        found = self.row(sql, parameters)
        if found is None or found[0] is None:
            return 0
        return int(found[0])

    def close(self) -> None:
        with self._lock:
            self._closed = True
            with self._reader_lock:
                for reader in self._idle_readers:
                    with suppress(sqlite3.Error):
                        reader.close()
                self._idle_readers.clear()
            self._conn.close()

    def page_bytes(self) -> int:
        """Physical size of the database, for a storage report's context."""
        return self.scalar_int("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()")


# --------------------------------------------------------------------------- #
# Layer 1                                                                     #
# --------------------------------------------------------------------------- #


class SqliteContentStore:
    """Layer 1 physical store. Every statement names ``org_id``."""

    __slots__ = ("_clock", "_db")

    def __init__(self, db: _Db, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    def missing(self, org_id: str, digests: Sequence[str]) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for digest in digests:
            if digest not in seen:
                seen.add(digest)
                ordered.append(digest)
        if not ordered:
            return ()
        held: set[str] = set()
        for chunk in _chunks(ordered):
            rows = self._db.rows(
                f"SELECT digest FROM blobs WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                (org_id, *chunk),
            )
            held.update(str(row["digest"]) for row in rows)
        return tuple(digest for digest in ordered if digest not in held)

    def put(self, org_id: str, digest: str, data: bytes) -> ContentRef:
        actual = sha256_hex(data)
        if actual != digest:
            raise ServerError(
                ErrorCode.BLOB_DIGEST_MISMATCH,
                "uploaded content does not match the declared digest",
                details={"declared": digest, "computed": actual, "size": len(data)},
                action=AgentAction.FIX_REQUEST,
            )
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO blobs (org_id, digest, size, data, created_at, unreferenced_since)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT (org_id, digest) DO UPDATE SET unreferenced_since = NULL
                """,
                (org_id, digest, len(data), data, float(self._clock())),
            )
        return ContentRef(digest=digest, size=len(data))

    def storage_size(self, org_id: str, data: bytes) -> int:
        return len(data)

    def get(self, org_id: str, digest: str) -> bytes | None:
        row = self._db.row("SELECT data FROM blobs WHERE org_id = ? AND digest = ?", (org_id, digest))
        return None if row is None else bytes(row["data"])

    def get_many(self, org_id: str, digests: Sequence[str]) -> Mapping[str, bytes]:
        wanted = tuple(dict.fromkeys(digests))
        if not wanted:
            return {}
        found: dict[str, bytes] = {}
        for chunk in _chunks(wanted):
            rows = self._db.rows(
                f"SELECT digest, data FROM blobs WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                (org_id, *chunk),
            )
            found.update((str(row["digest"]), bytes(row["data"])) for row in rows)
        return found

    def stored_bytes(self, org_id: str) -> int:
        return self._db.counter(org_id, "content")

    def blob_count(self, org_id: str) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM blobs WHERE org_id = ?", (org_id,))

    def sweep(self, referenced: Mapping[str, frozenset[str]]) -> int:
        dropped = 0
        with self._db.transaction() as conn:
            for row in self._db.rows("SELECT DISTINCT org_id FROM blobs"):
                org_id = str(row["org_id"])
                live = referenced.get(org_id, frozenset())
                held = [
                    str(item["digest"])
                    for item in self._db.rows("SELECT digest FROM blobs WHERE org_id = ?", (org_id,))
                ]
                doomed = [digest for digest in held if digest not in live]
                for chunk in _chunks(doomed):
                    conn.execute(
                        f"DELETE FROM blobs WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                        (org_id, *chunk),
                    )
                dropped += len(doomed)
        return dropped

    def mark_unreferenced(self, referenced: Mapping[str, frozenset[str]], now: float) -> int:
        with self._db.transaction() as conn:
            # Touch only the rows whose mark changes. Blob bytes live inline in
            # these WITHOUT ROWID rows, so the former "mark every row, then unmark
            # the live set" rewrote nearly every blob twice on each maintenance
            # pass -- gigabytes of writes for a busy repository every few minutes.
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS gc_live (digest TEXT PRIMARY KEY) WITHOUT ROWID")
            for row in self._db.rows("SELECT DISTINCT org_id FROM blobs"):
                org_id = str(row["org_id"])
                conn.execute("DELETE FROM temp.gc_live")
                conn.executemany(
                    "INSERT OR IGNORE INTO temp.gc_live (digest) VALUES (?)",
                    [(digest,) for digest in referenced.get(org_id, frozenset())],
                )
                conn.execute(
                    "UPDATE blobs SET unreferenced_since = ? WHERE org_id = ? AND unreferenced_since IS NULL "
                    "AND digest NOT IN (SELECT digest FROM temp.gc_live)",
                    (now, org_id),
                )
                conn.execute(
                    "UPDATE blobs SET unreferenced_since = NULL WHERE org_id = ? AND unreferenced_since IS NOT NULL "
                    "AND digest IN (SELECT digest FROM temp.gc_live)",
                    (org_id,),
                )
            conn.execute("DELETE FROM temp.gc_live")
        return self._db.scalar_int("SELECT COUNT(*) FROM blobs WHERE unreferenced_since IS NOT NULL")

    def sweep_marked(self, before: float, *, limit: int | None = None, dry_run: bool = False) -> SweepResult:
        dropped: dict[str, list[str]] = {}
        reclaimed: dict[str, int] = {}
        batch_limit = None if limit is None else max(0, int(limit))
        query = (
            "SELECT org_id, digest, size FROM blobs "
            "WHERE unreferenced_since IS NOT NULL AND unreferenced_since <= ? "
            "ORDER BY org_id, digest"
        )
        params: tuple[object, ...] = (before,)
        if batch_limit is not None:
            query += " LIMIT ?"
            params = (before, batch_limit)
        with self._db.transaction() as conn:
            rows = self._db.rows(query, params)
            for row in rows:
                org_id = str(row["org_id"])
                dropped.setdefault(org_id, []).append(str(row["digest"]))
                reclaimed[org_id] = reclaimed.get(org_id, 0) + int(row["size"])
            if not dry_run:
                for org_id, digests in dropped.items():
                    for chunk in _chunks(digests):
                        conn.execute(
                            f"DELETE FROM blobs WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                            (org_id, *chunk),
                        )
        return SweepResult(
            dropped={org_id: tuple(sorted(digests)) for org_id, digests in dropped.items()},
            bytes_reclaimed=reclaimed,
        )

    @property
    def marked_unreferenced(self) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM blobs WHERE unreferenced_since IS NOT NULL")


class SqliteContentProvenance:
    """Durable exact opaque possession scopes."""

    __slots__ = ("_db",)

    def __init__(self, db: _Db) -> None:
        self._db = db

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        if not repo_id or not digests:
            return 0
        wanted = tuple(dict.fromkeys(digests))
        written = 0
        with self._db.transaction() as conn:
            for digest in wanted:
                written += conn.execute(
                    "INSERT OR IGNORE INTO content_provenance (org_id, repo_id, subject, digest) VALUES (?, ?, ?, ?)",
                    (org_id, repo_id, subject, digest),
                ).rowcount
        return written

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        wanted = tuple(dict.fromkeys(digests))
        if not wanted or not repo_id:
            return frozenset()
        found: set[str] = set()
        for chunk in _chunks(wanted):
            places = _placeholders(len(chunk))
            rows = self._db.rows(
                "SELECT digest FROM content_provenance "
                f"WHERE org_id=? AND repo_id=? AND subject=? AND digest IN ({places})",
                (org_id, repo_id, subject, *chunk),
            )
            found.update(str(row["digest"]) for row in rows)
        return frozenset(found)

    def forget(self, org_id: str, digests: Sequence[str]) -> int:
        if not digests:
            return 0
        dropped = 0
        with self._db.transaction() as conn:
            for chunk in _chunks(tuple(dict.fromkeys(digests))):
                dropped += conn.execute(
                    f"DELETE FROM content_provenance WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                    (org_id, *chunk),
                ).rowcount
        return dropped

    @property
    def rows(self) -> int:
        """Total possession rows, for the storage report and for tests."""
        return self._db.scalar_int("SELECT COUNT(*) FROM content_provenance")


class SqliteAnalysisStore:
    """Layer 1 derived store: one artifact row plus its searchable rows."""

    __slots__ = (
        "_cache_lock",
        "_db",
        "_derivations",
        "_gram_frequency_cache",
        "_lock",
        "_term_posting_cache",
    )

    def __init__(self, db: _Db) -> None:
        self._db = db
        self._derivations = 0
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._gram_frequency_cache: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._term_posting_cache: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()

    def _clear_search_caches(self) -> None:
        with self._cache_lock:
            self._gram_frequency_cache.clear()
            self._term_posting_cache.clear()

    @property
    def derivations(self) -> int:
        """How many times analysis was actually computed in this process."""
        return self._derivations

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group a bulk ingest into one re-entrant SQLite transaction.

        ``ensure()`` deliberately owns its transaction for ordinary single-file
        writes. Cold same-host View sync, however, can feed thousands of files
        in one pass. Letting every file commit independently causes SQLite/WAL
        checkpoint amplification that dwarfs the repository itself. The shared
        ``_Db.transaction`` is re-entrant, so existing ``ensure()`` and content
        writes keep exactly the same atomic code paths while the outer bulk
        caller controls the commit boundary.
        """
        with self._db.transaction():
            yield

    def get(self, org_id: str, digest: str, parser_profile: str) -> AnalysisArtifact | None:
        row = self._db.row(
            "SELECT payload FROM analysis WHERE org_id = ? AND digest = ? AND parser_profile = ?",
            (org_id, digest, parser_profile),
        )
        if row is None:
            return None
        return AnalysisArtifact.from_json(json.loads(str(row["payload"])))

    def metadata(self, org_id: str, digest: str, parser_profile: str) -> tuple[str, int] | None:
        row = self._db.row(
            "SELECT language, line_count FROM analysis WHERE org_id = ? AND digest = ? AND parser_profile = ?",
            (org_id, digest, parser_profile),
        )
        return None if row is None else (str(row["language"]), int(row["line_count"]))

    def ensure(
        self,
        org_id: str,
        digest: str,
        parser_profile: str,
        data: bytes,
        *,
        artifact: AnalysisArtifact | None = None,
    ) -> AnalysisArtifact:
        existing = self.get(org_id, digest, parser_profile)
        if existing is not None:
            return existing
        prepared = artifact if artifact is not None else analyze(digest, parser_profile, data)
        stored, inserted = self._store_artifact(org_id, prepared)
        if inserted:
            with self._lock:
                self._derivations += 1
        return stored

    def restore(self, org_id: str, artifact: AnalysisArtifact) -> AnalysisArtifact:
        stored, _inserted = self._store_artifact(org_id, artifact)
        return stored

    def _store_artifact(self, org_id: str, artifact: AnalysisArtifact) -> tuple[AnalysisArtifact, bool]:
        existing = self.get(org_id, artifact.digest, artifact.parser_profile)
        if existing is not None:
            return existing, False
        payload = json.dumps(artifact.to_json(), separators=(",", ":"), ensure_ascii=False)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO analysis
                    (org_id, digest, parser_profile, language, line_count, byte_count, payload, payload_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org_id,
                    artifact.digest,
                    artifact.parser_profile,
                    artifact.language,
                    artifact.line_count,
                    artifact.byte_count,
                    payload,
                    len(payload),
                ),
            )
            if cursor.rowcount == 0:
                return self.get(org_id, artifact.digest, artifact.parser_profile) or artifact, False
            self._put_postings(conn, org_id, artifact.digest, artifact.parser_profile, artifact)
            conn.executemany(
                """
                INSERT OR IGNORE INTO analysis_defs
                    (org_id, digest, parser_profile, name, kind, line, span_start, span_end, exported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        org_id,
                        artifact.digest,
                        artifact.parser_profile,
                        item.name,
                        item.kind,
                        item.line,
                        item.start,
                        item.end,
                        int(item.exported),
                    )
                    for item in artifact.definitions
                ],
            )
            conn.execute(
                "INSERT OR IGNORE INTO analysis_vectors (org_id, digest, vector) VALUES (?, ?, ?)",
                (org_id, artifact.digest, _pack(artifact.embedding)),
            )
        self._clear_search_caches()
        return artifact, True

    def _put_postings(
        self,
        conn: sqlite3.Connection,
        org_id: str,
        digest: str,
        parser_profile: str,
        artifact: AnalysisArtifact,
    ) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO analysis_terms (org_id, digest, term) VALUES (?, ?, ?)",
            [(org_id, digest, term) for term in artifact.lexical_terms],
        )
        gram_cursor = conn.executemany(
            "INSERT OR IGNORE INTO analysis_grams (org_id, digest, gram) VALUES (?, ?, ?)",
            [(org_id, digest, gram) for gram in artifact.trigrams],
        )
        inserted_grams = max(0, int(gram_cursor.rowcount))
        if inserted_grams:
            # AnalysisArtifact.trigrams is the canonical three-character
            # gram set, so this is exactly SQLite's SUM(LENGTH(gram)) for
            # the rows this executemany inserted without rereading them.
            gram_bytes = inserted_grams * 3
            conn.execute(
                "INSERT INTO storage_counters (org_id, kind, bytes) VALUES (?, 'analysis', ?) "
                "ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes",
                (org_id, gram_bytes),
            )

    def count(self, org_id: str) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM analysis WHERE org_id = ?", (org_id,))

    def stored_semantic_units(
        self, org_id: str, keys: Sequence[tuple[str, str]], embedding_version: str
    ) -> dict[tuple[str, str], tuple[SemanticUnit, ...]]:
        wanted = set(keys)
        found: dict[tuple[str, str], tuple[SemanticUnit, ...]] = {}
        for chunk in _chunks(tuple(dict.fromkeys(digest for digest, _profile in wanted))):
            rows = self._db.rows(
                "SELECT digest, parser_profile, units, vectors FROM analysis_semantic_units "
                f"WHERE org_id = ? AND embedding_version = ? AND digest IN ({_placeholders(len(chunk))})",
                (org_id, embedding_version, *chunk),
            )
            for row in rows:
                key = (str(row["digest"]), str(row["parser_profile"]))
                if key in wanted:
                    found[key] = _decode_semantic_units(str(row["units"]), bytes(row["vectors"]), embedding_version)
        return found

    def store_semantic_units(
        self,
        org_id: str,
        rows: Sequence[tuple[str, str, tuple[SemanticUnit, ...]]],
        embedding_version: str,
    ) -> None:
        encoded = []
        for digest, parser_profile, units in rows:
            if any(unit.embedding_version != embedding_version for unit in units):
                raise ValueError("semantic units must all carry the stored embedding version")
            meta, vectors = _encode_semantic_units(units)
            encoded.append((org_id, digest, parser_profile, embedding_version, meta, vectors, len(meta) + len(vectors)))
        if not encoded:
            return
        with self._db.transaction() as conn:
            # Only beside a live analysis row, in the sweep's own lock order: a
            # row written after its digest was swept would never be swept.
            conn.executemany(
                "INSERT OR IGNORE INTO analysis_semantic_units "
                "(org_id, digest, parser_profile, embedding_version, units, vectors, payload_bytes) "
                "SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM analysis "
                "WHERE org_id = ?1 AND digest = ?2 AND parser_profile = ?3)",
                encoded,
            )

    def storage_size(self, org_id: str, artifact: AnalysisArtifact) -> int:
        # Other profiles may already share terms, grams or vectors. Count them
        # all for admission, then reconcile the actual delta after the write.
        payload = json.dumps(artifact.to_json(), separators=(",", ":"), ensure_ascii=False)
        return (
            len(payload)
            + sum(len(term) for term in artifact.lexical_terms)
            + sum(len(gram) for gram in artifact.trigrams)
            + len(_pack(artifact.embedding))
        )

    def stored_bytes(self, org_id: str) -> int:
        return self._db.counter(org_id, "analysis")

    def lexical_matches(
        self,
        org_id: str,
        digests: Sequence[str],
        query: str,
        *,
        match_any_term: bool = False,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
        document_count: int | None = None,
    ) -> Mapping[str, float]:
        parsed: LexicalQuery = parse_query(query)
        ignored = set(ignored_terms)
        if parsed.empty or not digests:
            return {}
        searchable_terms = tuple(term for term in parsed.terms if term not in ignored)
        matched_terms = self._matched_values(org_id, digests, "analysis_terms", "term", searchable_terms)
        ignored_matches = self._matched_values(org_id, digests, "analysis_terms", "term", tuple(ignored))
        if ignored_matches and parsed.grams:
            anchored = self._rare_gram_candidates(org_id, digests, parsed.grams, count=3)
            for digest in anchored:
                terms = ignored_matches.get(digest)
                if terms:
                    matched_terms.setdefault(digest, set()).update(terms)
        gram_hits: dict[str, int] = {}
        if include_grams and parsed.grams:
            # Exact-term candidates may receive a partial trigram bonus, so
            # count grams only for those candidates. Substring-only matches, on
            # the other hand, score only when *all* query grams are present;
            # find those with an inverted rarest-gram intersection instead of
            # probing every view digest for every gram.
            if matched_terms:
                gram_hits.update(
                    self._hits(
                        org_id,
                        tuple(matched_terms),
                        "analysis_grams",
                        "gram",
                        parsed.grams,
                    )
                )
            if include_substring_only:
                gram_hits.update(self._full_gram_hits(org_id, digests, parsed.grams))
        term_df: dict[str, int] = {}
        for terms in matched_terms.values():
            for term in terms:
                if term not in ignored:
                    term_df[term] = term_df.get(term, 0) + 1
        for term in ignored:
            term_df[term] = sum(1 for terms in ignored_matches.values() if term in terms)
        effective_document_count = max(len(set(digests)), int(document_count or 0))
        scores: dict[str, float] = {}
        for digest in set(matched_terms) | set(gram_hits):
            digest_terms = tuple(term for term in parsed.terms if term in matched_terms.get(digest, set()))
            inferred_gram_hits = len(parsed.grams) if digest_terms and not include_grams else 0
            score = score_weighted_match(
                parsed,
                matched_terms=digest_terms,
                term_document_frequency=term_df,
                document_count=effective_document_count,
                gram_hits=gram_hits.get(digest, inferred_gram_hits),
                match_any_term=match_any_term,
            )
            if score > 0:
                scores[digest] = score
        return scores

    def lexical_candidates(
        self,
        org_id: str,
        query: str,
        *,
        limit: int,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
    ) -> tuple[tuple[str, ...], bool]:
        parsed = parse_query(query)
        if parsed.empty or limit < 1:
            return (), False
        cap = limit + 1
        candidates: set[str] = set()
        truncated = False
        terms = tuple(dict.fromkeys((*parsed.terms, *ignored_terms)))
        if terms:
            rows = self._db.rows(
                "SELECT digest FROM analysis_terms INDEXED BY ix_analysis_terms "
                f"WHERE org_id=? AND term IN ({_placeholders(len(terms))}) "
                "GROUP BY digest ORDER BY digest LIMIT ?",
                (org_id, *terms, cap),
            )
            candidates.update(str(row["digest"]) for row in rows[:limit])
            truncated = truncated or len(rows) > limit
        if include_grams and include_substring_only and parsed.grams:
            frequencies = self._gram_frequencies(org_id, parsed.grams)
            anchors = tuple(
                gram
                for gram in sorted(dict.fromkeys(parsed.grams), key=lambda gram: (frequencies.get(gram, 0), gram))
                if frequencies.get(gram, 0) > 0
            )[:8]
            if anchors:
                rows = self._db.rows(
                    "SELECT digest FROM analysis_grams INDEXED BY ix_analysis_grams "
                    f"WHERE org_id=? AND gram IN ({_placeholders(len(anchors))}) "
                    "GROUP BY digest HAVING COUNT(DISTINCT gram)=? ORDER BY digest LIMIT ?",
                    (org_id, *anchors, len(anchors), cap),
                )
                candidates.update(str(row["digest"]) for row in rows[:limit])
                truncated = truncated or len(rows) > limit
        ordered = tuple(sorted(candidates))
        if len(ordered) > limit:
            truncated = True
        return ordered[:limit], truncated

    def definition_candidates(self, org_id: str, name: str, *, limit: int) -> tuple[tuple[str, ...], bool]:
        if not name or limit < 1:
            return (), False
        rows = self._db.rows(
            "SELECT digest FROM analysis_defs INDEXED BY ix_analysis_defs "
            "WHERE org_id=? AND name=? GROUP BY digest ORDER BY digest LIMIT ?",
            (org_id, name, limit + 1),
        )
        return tuple(str(row["digest"]) for row in rows[:limit]), len(rows) > limit

    def _matched_values(
        self,
        org_id: str,
        digests: Sequence[str],
        table: str,
        column: str,
        needles: Sequence[str],
    ) -> dict[str, set[str]]:
        if not digests or not needles:
            return {}
        wanted = set(digests)
        if table == "analysis_terms" and column == "term":
            posting_by_term: dict[str, frozenset[str]] = {}
            missing: list[str] = []
            with self._cache_lock:
                for term in dict.fromkeys(needles):
                    key = (org_id, term)
                    posting = self._term_posting_cache.get(key)
                    if posting is None:
                        missing.append(term)
                    else:
                        self._term_posting_cache.move_to_end(key)
                        posting_by_term[term] = posting
            for term_chunk in _chunks(tuple(missing)):
                rows = self._db.rows(
                    "SELECT digest, term FROM analysis_terms INDEXED BY ix_analysis_terms "
                    f"WHERE org_id = ? AND term IN ({_placeholders(len(term_chunk))})",
                    (org_id, *term_chunk),
                )
                grouped: dict[str, set[str]] = {term: set() for term in term_chunk}
                for row in rows:
                    grouped[str(row["term"])].add(str(row["digest"]))
                with self._cache_lock:
                    for term in term_chunk:
                        posting = frozenset(grouped[term])
                        key = (org_id, term)
                        self._term_posting_cache[key] = posting
                        self._term_posting_cache.move_to_end(key)
                        while len(self._term_posting_cache) > 512:
                            self._term_posting_cache.popitem(last=False)
                        posting_by_term[term] = posting
            found: dict[str, set[str]] = {}
            for term in needles:
                for digest in posting_by_term.get(term, frozenset()) & wanted:
                    found.setdefault(digest, set()).add(term)
            return found

        found = {}
        for needle_chunk in _chunks(tuple(needles)):
            rows = self._db.rows(
                f"SELECT digest, {column} AS value FROM {table} "
                f"WHERE org_id = ? AND {column} IN ({_placeholders(len(needle_chunk))})",
                (org_id, *needle_chunk),
            )
            for row in rows:
                digest = str(row["digest"])
                if digest in wanted:
                    found.setdefault(digest, set()).add(str(row["value"]))
        return found

    def _gram_frequencies(self, org_id: str, grams: Sequence[str]) -> dict[str, int]:
        unique_grams = tuple(dict.fromkeys(grams))
        frequencies: dict[str, int] = {}
        missing: list[str] = []
        with self._cache_lock:
            for gram in unique_grams:
                key = (org_id, gram)
                frequency = self._gram_frequency_cache.get(key)
                if frequency is None:
                    missing.append(gram)
                else:
                    self._gram_frequency_cache.move_to_end(key)
                    frequencies[gram] = frequency
        for gram_chunk in _chunks(tuple(missing)):
            stats_rows = self._db.rows(
                "SELECT gram, n FROM analysis_gram_stats "
                f"WHERE org_id = ? AND gram IN ({_placeholders(len(gram_chunk))})",
                (org_id, *gram_chunk),
            )
            counted = {str(row["gram"]): int(row["n"]) for row in stats_rows}
            absent = tuple(gram for gram in gram_chunk if gram not in counted)
            if absent:
                live_rows = self._db.rows(
                    "SELECT gram, COUNT(*) AS n FROM analysis_grams INDEXED BY ix_analysis_grams "
                    f"WHERE org_id = ? AND gram IN ({_placeholders(len(absent))}) GROUP BY gram",
                    (org_id, *absent),
                )
                counted.update({str(row["gram"]): int(row["n"]) for row in live_rows})
            with self._cache_lock:
                for gram in gram_chunk:
                    frequency = counted.get(gram, 0)
                    key = (org_id, gram)
                    self._gram_frequency_cache[key] = frequency
                    self._gram_frequency_cache.move_to_end(key)
                    while len(self._gram_frequency_cache) > 8192:
                        self._gram_frequency_cache.popitem(last=False)
                    frequencies[gram] = frequency
        return frequencies

    def _rare_gram_candidates(
        self,
        org_id: str,
        digests: Sequence[str],
        grams: Sequence[str],
        *,
        count: int,
    ) -> frozenset[str]:
        wanted = set(digests)
        unique_grams = tuple(dict.fromkeys(grams))
        if not wanted or not unique_grams or count <= 0:
            return frozenset()
        frequencies = self._gram_frequencies(org_id, unique_grams)
        anchors = tuple(
            gram
            for gram in sorted(unique_grams, key=lambda gram: (frequencies.get(gram, 0), gram))
            if frequencies.get(gram, 0) > 0
        )[:count]
        if not anchors:
            return frozenset()
        matched: set[str] | None = None
        for gram in anchors:
            rows = self._db.rows(
                "SELECT digest FROM analysis_grams INDEXED BY ix_analysis_grams WHERE org_id = ? AND gram = ?",
                (org_id, gram),
            )
            posting = {str(row["digest"]) for row in rows if str(row["digest"]) in wanted}
            matched = posting if matched is None else matched & posting
            if not matched:
                return frozenset()
        return frozenset(matched or ())

    def _full_gram_hits(
        self,
        org_id: str,
        digests: Sequence[str],
        grams: Sequence[str],
    ) -> dict[str, int]:
        """Digest -> ``len(grams)`` for full substring matches in ``digests``.

        ``analysis_grams`` is an inverted index, so intersect from the rarest
        query gram instead of issuing ``len(digests) x len(grams)`` primary-key
        probes. The final verification still uses the canonical row table,
        preserving exact substring semantics byte-for-byte with the memory
        backend.
        """
        wanted = set(digests)
        unique_grams = tuple(dict.fromkeys(grams))
        if not wanted or not unique_grams:
            return {}

        frequencies = self._gram_frequencies(org_id, unique_grams)
        if any(frequencies.get(gram, 0) == 0 for gram in unique_grams):
            return {}
        rarest = min(unique_grams, key=lambda gram: (frequencies[gram], gram))
        candidates = tuple(
            str(row["digest"])
            for row in self._db.rows(
                "SELECT digest FROM analysis_grams INDEXED BY ix_analysis_grams WHERE org_id = ? AND gram = ?",
                (org_id, rarest),
            )
            if str(row["digest"]) in wanted
        )
        if not candidates:
            return {}
        counts = self._hits(org_id, candidates, "analysis_grams", "gram", unique_grams)
        required = len(unique_grams)
        return {digest: required for digest, count in counts.items() if count == required}

    def _hits(
        self,
        org_id: str,
        digests: Sequence[str],
        table: str,
        column: str,
        needles: Sequence[str],
    ) -> dict[str, int]:
        if not needles:
            return {}
        counts: dict[str, int] = {}
        for digest_chunk in _chunks(tuple(digests)):
            for needle_chunk in _chunks(tuple(needles)):
                rows = self._db.rows(
                    f"SELECT digest, COUNT(*) AS hits FROM {table} "
                    f"WHERE org_id = ? AND digest IN ({_placeholders(len(digest_chunk))}) "
                    f"AND {column} IN ({_placeholders(len(needle_chunk))}) GROUP BY digest",
                    (org_id, *digest_chunk, *needle_chunk),
                )
                for row in rows:
                    digest = str(row["digest"])
                    counts[digest] = counts.get(digest, 0) + int(row["hits"])
        return counts

    def definitions_named(self, org_id: str, digests: Sequence[str], name: str) -> Mapping[str, tuple[SymbolDef, ...]]:
        if not digests or not name:
            return {}
        wanted = set(digests)
        found: dict[str, set[SymbolDef]] = {}
        rows = self._db.rows(
            "SELECT digest, name, kind, line, span_start, span_end, exported "
            "FROM analysis_defs INDEXED BY ix_analysis_defs WHERE org_id = ? AND name = ?",
            (org_id, name),
        )
        for row in rows:
            digest = str(row["digest"])
            if digest not in wanted:
                continue
            found.setdefault(digest, set()).add(
                SymbolDef(
                    name=str(row["name"]),
                    kind=str(row["kind"]),
                    line=int(row["line"]),
                    start=int(row["span_start"]),
                    end=int(row["span_end"]),
                    exported=bool(row["exported"]),
                )
            )
        return {digest: tuple(sorted(items)) for digest, items in found.items()}

    def nearest(
        self, org_id: str, digests: Sequence[str], vector: Sequence[float], limit: int
    ) -> tuple[tuple[str, float], ...]:
        if not digests or not vector:
            return ()
        scored: dict[str, float] = {}
        for chunk in _chunks(tuple(digests)):
            rows = self._db.rows(
                "SELECT digest, vector FROM analysis_vectors "
                f"WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                (org_id, *chunk),
            )
            for row in rows:
                similarity = cosine(vector, _unpack(bytes(row["vector"])))
                if similarity > 0.0:
                    scored[str(row["digest"])] = similarity
        ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        return tuple(ranked[:limit])

    def _drop_postings(self, conn: sqlite3.Connection, org_id: str, digests: Sequence[str]) -> None:
        places = _placeholders(len(digests))
        conn.execute(f"DELETE FROM analysis_terms WHERE org_id = ? AND digest IN ({places})", (org_id, *digests))
        row = conn.execute(
            f"SELECT COALESCE(SUM(LENGTH(gram)), 0) FROM analysis_grams WHERE org_id = ? AND digest IN ({places})",
            (org_id, *digests),
        ).fetchone()
        gram_bytes = int(row[0] or 0) if row is not None else 0
        conn.execute(f"DELETE FROM analysis_grams WHERE org_id = ? AND digest IN ({places})", (org_id, *digests))
        if gram_bytes:
            conn.execute(
                "INSERT INTO storage_counters (org_id, kind, bytes) VALUES (?, 'analysis', ?) "
                "ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes",
                (org_id, -gram_bytes),
            )

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        if not digests:
            return 0
        dropped = 0
        with self._db.transaction() as conn:
            for chunk in _chunks(tuple(digests)):
                places = _placeholders(len(chunk))
                self._drop_postings(conn, org_id, chunk)
                dropped += conn.execute(
                    f"DELETE FROM analysis WHERE org_id = ? AND digest IN ({places})", (org_id, *chunk)
                ).rowcount
                for table in ("analysis_defs", "analysis_vectors", "analysis_semantic_units"):
                    conn.execute(f"DELETE FROM {table} WHERE org_id = ? AND digest IN ({places})", (org_id, *chunk))
        if dropped:
            self._clear_search_caches()
        return dropped


# --------------------------------------------------------------------------- #
# Layer 2                                                                     #
# --------------------------------------------------------------------------- #


def _entry_of(row: sqlite3.Row) -> ManifestEntry:
    return ManifestEntry(
        path=str(row["path"]),
        content_digest=str(row["content_digest"]),
        parser_profile=str(row["parser_profile"]),
        mode=int(row["mode"]),
        size=int(row["size"]),
    )


class SqliteViewStore:
    """Layer 2: roots shared across views, plus a revisioned per-view overlay."""

    __slots__ = ("_clock", "_db", "_entry_count_cache", "_root_cache", "_root_cache_lock")

    def __init__(self, db: _Db, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._root_cache: OrderedDict[tuple[str, str], Mapping[str, ManifestEntry]] = OrderedDict()
        self._root_cache_lock = threading.Lock()
        # Counts are immutable within a complete View revision. Repeated get()
        # calls must not join a large deletion overlay on every candidate probe.
        self._entry_count_cache: OrderedDict[tuple[str, str, str, str, int], int] = OrderedDict()

    def _now(self) -> float:
        return float(self._clock())

    def _require(self, view_id: str, org_id: str) -> sqlite3.Row:
        row = self._db.row("SELECT * FROM views WHERE view_id = ?", (view_id,))
        if row is None or str(row["org_id"]) != org_id:
            # Byte-identical refusal either way: a 404 that distinguished
            # "not yours" from "absent" would be an existence oracle.
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "view is not available",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )
        if float(row["lease_expires_at"]) <= self._now():
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "view lease has expired",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )
        return row

    def _base(self, view: sqlite3.Row) -> list[sqlite3.Row]:
        """The manifest rows this view reads: proved ones, or its own staged.

        A proved root is shared, which is what makes a warm open cheap. An
        unproved fill is not, which is what stops one caller's rows from
        deciding whether another caller's manifest hashes to its root.
        """
        if self._complete(str(view["org_id"]), str(view["root"])):
            return self._db.rows(
                "SELECT path, content_digest, parser_profile, mode, size FROM manifest_rows "
                "WHERE org_id = ? AND root = ?",
                (str(view["org_id"]), str(view["root"])),
            )
        return self._db.rows(
            "SELECT path, content_digest, parser_profile, mode, size FROM manifest_staging WHERE view_id = ?",
            (str(view["view_id"]),),
        )

    def _base_entries(self, view: sqlite3.Row) -> Mapping[str, ManifestEntry]:
        org_id = str(view["org_id"])
        root = str(view["root"])
        if not self._complete(org_id, root):
            # A staged manifest is still mutable and belongs to this View.
            return {str(row["path"]): _entry_of(row) for row in self._base(view)}
        key = (org_id, root)
        with self._root_cache_lock:
            cached = self._root_cache.get(key)
            if cached is not None:
                self._root_cache.move_to_end(key)
                return cached
        cached = MappingProxyType({str(row["path"]): _entry_of(row) for row in self._base(view)})
        # Do not retain rows from a manifest transaction that may roll back.
        if self._db._owner != threading.get_ident():
            with self._root_cache_lock:
                self._root_cache[key] = cached
                self._root_cache.move_to_end(key)
                while len(self._root_cache) > 32:
                    self._root_cache.popitem(last=False)
        return cached

    def _effective(self, view: sqlite3.Row) -> dict[str, ManifestEntry]:
        merged = dict(self._base_entries(view))
        for row in self._db.rows(
            "SELECT path, content_digest, parser_profile, mode, size, tombstone FROM overlay WHERE view_id = ?",
            (str(view["view_id"]),),
        ):
            path = str(row["path"])
            if int(row["tombstone"]):
                merged.pop(path, None)
            else:
                merged[path] = _entry_of(row)
        return merged

    def _entry_count(self, view: sqlite3.Row) -> int:
        """How many effective paths the view has without scanning its base.

        A complete View names an immutable RepositoryGeneration whose entry
        count is persisted when the manifest root is proved. Re-counting the
        base manifest here made every views.get() O(repository files), so even
        an exact one-candidate query paid a full manifest walk. The only
        mutable part of a complete View is its overlay: derive the count from
        the generation counter plus an overlay-sized delta, probing the base
        primary key only for paths the overlay actually touches.

        Incomplete manifests do not have a proved generation count yet, so they
        retain a staging-row count until the last chunk proves the root.
        """
        view_id = str(view["view_id"])
        org_id = str(view["org_id"])
        root = str(view["root"])
        if self._complete(org_id, root):
            cache_key = (org_id, view_id, root, str(view["generation_id"]), int(view["revision"]))
            # A rolled-back writer can reuse its tentative revision. Never
            # remember an uncommitted count under that revision.
            cacheable = self._db._owner != threading.get_ident()
            if cacheable:
                with self._root_cache_lock:
                    cached_count = self._entry_count_cache.get(cache_key)
                    if cached_count is not None:
                        self._entry_count_cache.move_to_end(cache_key)
                        return cached_count
            row = self._db.row(
                "SELECT entry_count FROM repository_generations WHERE org_id=? AND generation_id=?",
                (org_id, str(view["generation_id"])),
            )
            if row is None:
                raise RuntimeError("view generation metadata is missing")
            base_count = int(row["entry_count"])
            delta = self._db.row(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN o.tombstone = 1 AND m.path IS NOT NULL THEN -1
                        WHEN o.tombstone = 0 AND m.path IS NULL THEN 1
                        ELSE 0
                    END
                ), 0) AS delta
                  FROM overlay o
             LEFT JOIN manifest_rows m
                    ON m.org_id = ? AND m.root = ? AND m.path = o.path
                 WHERE o.view_id = ?
                """,
                (org_id, root, view_id),
            )
            count = base_count + (0 if delta is None else int(delta["delta"]))
            if cacheable:
                current = self._db.row(
                    "SELECT root, generation_id, revision FROM views WHERE org_id=? AND view_id=?",
                    (org_id, view_id),
                )
                # A writer may have committed between reading the View and its
                # overlay delta. Such a count cannot be cached for the old View.
                if current is not None and (
                    str(current["root"]),
                    str(current["generation_id"]),
                    int(current["revision"]),
                ) == (root, str(view["generation_id"]), int(view["revision"])):
                    with self._root_cache_lock:
                        self._entry_count_cache[cache_key] = count
                        self._entry_count_cache.move_to_end(cache_key)
                        while len(self._entry_count_cache) > 256:
                            self._entry_count_cache.popitem(last=False)
            return count
        else:
            row = self._db.row("SELECT COUNT(*) AS n FROM manifest_staging WHERE view_id=?", (view_id,))
            base_count = 0 if row is None else int(row["n"])
            delta = self._db.row(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN o.tombstone = 1 AND m.path IS NOT NULL THEN -1
                        WHEN o.tombstone = 0 AND m.path IS NULL THEN 1
                        ELSE 0
                    END
                ), 0) AS delta
                  FROM overlay o
             LEFT JOIN manifest_staging m
                    ON m.view_id = o.view_id AND m.path = o.path
                 WHERE o.view_id = ?
                """,
                (view_id,),
            )
        return base_count + (0 if delta is None else int(delta["delta"]))

    def _pending(self, view_id: str) -> tuple[str, ...]:
        rows = self._db.rows(
            "SELECT chunk_id FROM manifest_chunks WHERE view_id = ? AND delivered = 0 ORDER BY chunk_id",
            (view_id,),
        )
        return tuple(str(row["chunk_id"]) for row in rows)

    def _complete(self, org_id: str, root: str) -> bool:
        row = self._db.row("SELECT complete FROM manifest_roots WHERE org_id = ? AND root = ?", (org_id, root))
        return bool(row is not None and int(row["complete"]))

    def _state(self, view: sqlite3.Row) -> ViewState:
        org_id = str(view["org_id"])
        root = str(view["root"])
        complete = self._complete(org_id, root)
        return ViewState(
            view_id=str(view["view_id"]),
            org_id=org_id,
            repo_id=str(view["repo_id"]),
            base_revision=str(view["base_revision"]),
            view_revision=int(view["revision"]),
            manifest_root=root,
            entry_count=self._entry_count(view),
            lease_expires_at=float(view["lease_expires_at"]),
            pending_chunks=() if complete else self._pending(str(view["view_id"])),
            generation_id=str(view["generation_id"]),
            manifest_complete=complete,
            content_subject=str(view["content_subject"]),
            owner_subject=str(view["owner_subject"]),
        )

    def open(
        self,
        *,
        org_id: str,
        repo_id: str,
        base_revision: str,
        manifest_root: str,
        chunk_ids: Sequence[str],
        lease_s: float,
        content_subject: str = "",
        owner_subject: str = "",
    ) -> ViewOpenResult:
        view_id = new_opaque_id("view")
        generation_id = repository_generation_id(org_id, repo_id, base_revision, manifest_root)
        now = self._now()
        with self._db.transaction() as conn:
            known = self._db.row(
                "SELECT complete FROM manifest_roots WHERE org_id = ? AND root = ?", (org_id, manifest_root)
            )
            warm = bool(known is not None and int(known["complete"]))
            if known is None:
                # An empty manifest is a legitimate view -- but only if the
                # announced root is the root an empty manifest actually has.
                empty = manifest_root == canonical_manifest_root(())
                complete = 1 if (not chunk_ids and empty) else 0
                conn.execute(
                    "INSERT INTO manifest_roots (org_id, root, complete, last_referenced_at) VALUES (?, ?, ?, ?)",
                    (org_id, manifest_root, complete, now),
                )
            else:
                conn.execute(
                    "UPDATE manifest_roots SET last_referenced_at = ? WHERE org_id = ? AND root = ?",
                    (now, org_id, manifest_root),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO repository_generations
                    (org_id, generation_id, repo_id, base_revision, root, entry_count,
                     analyzer_version, index_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org_id,
                    generation_id,
                    repo_id,
                    base_revision,
                    manifest_root,
                    (
                        self._db.scalar_int(
                            "SELECT COUNT(*) FROM manifest_rows WHERE org_id=? AND root=?",
                            (org_id, manifest_root),
                        )
                        if warm
                        else 0
                    ),
                    REPOSITORY_GENERATION_ANALYZER_VERSION,
                    REPOSITORY_GENERATION_INDEX_VERSION,
                ),
            )
            if chunk_ids:
                # What *this* view owes, and nothing another view announced. A
                # warm open owes none of them -- the root is already proved --
                # but they stay announced so that re-delivering one is the
                # no-op it has always been rather than a refusal.
                conn.executemany(
                    "INSERT OR IGNORE INTO manifest_chunks (view_id, chunk_id, delivered) VALUES (?, ?, ?)",
                    [(view_id, chunk_id, int(warm)) for chunk_id in chunk_ids],
                )
            conn.execute(
                """
                INSERT INTO views
                    (view_id, org_id, repo_id, base_revision, generation_id, root, revision,
                     lease_expires_at, last_client_seq, created_at, content_subject, owner_subject)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?)
                """,
                (
                    view_id,
                    org_id,
                    repo_id,
                    base_revision,
                    generation_id,
                    manifest_root,
                    now + lease_s,
                    now,
                    content_subject,
                    owner_subject,
                ),
            )
        view = self._require(view_id, org_id)
        state = self._state(view)
        declared = tuple(
            dict.fromkeys(
                str(row["content_digest"])
                for row in self._db.rows(
                    "SELECT content_digest FROM manifest_rows WHERE org_id = ? AND root = ? ORDER BY path",
                    (org_id, manifest_root),
                )
            )
        )
        return ViewOpenResult(
            state=state,
            declared_digests=declared,
            have_link_ancestor=False,
            warm=warm,
            needs_manifest=not state.manifest_complete and not state.pending_chunks,
        )

    def get(self, org_id: str, view_id: str) -> ViewState:
        return self._state(self._require(view_id, org_id))

    def generation(self, org_id: str, view_id: str) -> RepositoryGeneration:
        view = self._require(view_id, org_id)
        row = self._db.row(
            """
            SELECT generation_id, repo_id, base_revision, root, entry_count,
                   analyzer_version, index_version
              FROM repository_generations
             WHERE org_id=? AND generation_id=?
            """,
            (org_id, str(view["generation_id"])),
        )
        if row is None:
            raise RuntimeError("view generation metadata is missing")
        return RepositoryGeneration(
            generation_id=str(row["generation_id"]),
            org_id=org_id,
            repo_id=str(row["repo_id"]),
            base_revision=str(row["base_revision"]),
            manifest_root=str(row["root"]),
            entry_count=int(row["entry_count"]),
            analyzer_version=str(row["analyzer_version"]),
            index_version=str(row["index_version"]),
        )

    def generation_membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        view = self._require(view_id, org_id)
        return self._base_entries(view)

    def overlay(self, org_id: str, view_id: str) -> tuple[Mapping[str, ManifestEntry], frozenset[str]]:
        self._require(view_id, org_id)
        rows = self._db.rows(
            "SELECT path, content_digest, parser_profile, mode, size, tombstone FROM overlay WHERE view_id=?",
            (view_id,),
        )
        entries: dict[str, ManifestEntry] = {}
        tombstones: set[str] = set()
        for row in rows:
            path = str(row["path"])
            if int(row["tombstone"]):
                tombstones.add(path)
            else:
                entries[path] = _entry_of(row)
        return entries, frozenset(tombstones)

    def put_manifest_chunk(
        self,
        *,
        org_id: str,
        view_id: str,
        chunk_id: str,
        entries: Sequence[ManifestEntry],
    ) -> ViewState:
        with self._db.transaction() as conn:
            view = self._require(view_id, org_id)
            root = str(view["root"])
            announced = self._db.row(
                "SELECT delivered FROM manifest_chunks WHERE view_id = ? AND chunk_id = ?",
                (view_id, chunk_id),
            )
            if announced is None:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "chunk_id was not announced at views/open",
                    details={"chunk_id": chunk_id},
                    action=AgentAction.REOPEN_VIEW,
                )
            conn.execute(
                "UPDATE manifest_chunks SET delivered = 1 WHERE view_id = ? AND chunk_id = ?",
                (view_id, chunk_id),
            )
            if self._complete(org_id, root):
                # Somebody proved this root while this fill was in flight. The
                # rows are the root's now and they are the rows the root names,
                # so there is nothing left for this chunk to contribute.
                conn.execute("DELETE FROM manifest_staging WHERE view_id = ?", (view_id,))
                return self._state(self._require(view_id, org_id))
            conn.executemany(
                """
                INSERT INTO manifest_staging (view_id, path, content_digest, parser_profile, mode, size)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (view_id, path) DO UPDATE SET
                    content_digest = excluded.content_digest,
                    parser_profile = excluded.parser_profile,
                    mode = excluded.mode,
                    size = excluded.size
                """,
                [
                    (view_id, entry.path, entry.content_digest, entry.parser_profile, entry.mode, entry.size)
                    for entry in entries
                ],
            )
            if not self._pending(view_id):
                # The last announced chunk is where the root is proved, and it
                # is proved against what *this* view delivered. Rows are shared
                # by every view naming this root, so they must be the rows the
                # root names or a warm open inherits the wrong tree.
                staged = {str(row["path"]): _entry_of(row) for row in self._base(self._require(view_id, org_id))}
                assembled = canonical_manifest_root(staged.values())
                if assembled != root:
                    # Rolls the whole delivery back, so a refused completion
                    # leaves the view exactly where it was.
                    raise ServerError(
                        ErrorCode.MANIFEST_INCOMPLETE,
                        "assembled manifest does not hash to the announced manifest_root",
                        details={
                            "manifest_root": root,
                            "assembled_root": assembled,
                            "entry_count": len(staged),
                        },
                    )
                conn.executemany(
                    """
                    INSERT INTO manifest_rows (org_id, root, path, content_digest, parser_profile, mode, size)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (org_id, root, path) DO UPDATE SET
                        content_digest = excluded.content_digest,
                        parser_profile = excluded.parser_profile,
                        mode = excluded.mode,
                        size = excluded.size
                    """,
                    [
                        (org_id, root, entry.path, entry.content_digest, entry.parser_profile, entry.mode, entry.size)
                        for entry in staged.values()
                    ],
                )
                conn.execute(
                    "INSERT INTO manifest_path_search (org_id, root, path) "
                    "SELECT org_id, root, path FROM manifest_rows WHERE org_id=? AND root=?",
                    (org_id, root),
                )
                conn.execute("UPDATE manifest_roots SET complete = 1 WHERE org_id = ? AND root = ?", (org_id, root))
                conn.execute(
                    "UPDATE repository_generations SET entry_count=? WHERE org_id=? AND generation_id=?",
                    (len(staged), org_id, str(view["generation_id"])),
                )
                conn.execute("DELETE FROM manifest_staging WHERE view_id = ?", (view_id,))
        return self._state(self._require(view_id, org_id))

    def apply_overlay(
        self,
        *,
        org_id: str,
        view_id: str,
        expected_view_revision: int,
        client_seq: int,
        entries: Sequence[ManifestEntry],
        removed_paths: Sequence[str],
    ) -> ViewState:
        with self._db.transaction() as conn:
            view = self._require(view_id, org_id)
            replay = self._db.row(
                "SELECT revision, entry_count, lease_expires_at, pending_chunks, manifest_complete "
                "FROM replays WHERE view_id = ? AND client_seq = ?",
                (view_id, client_seq),
            )
            if replay is not None:
                # A retried write after a lost ACK re-reads *its own* committed
                # acknowledgement -- not the view's current state. The
                # difference is not cosmetic: between the lost ACK and the
                # retry another writer may have committed, and answering with
                # the current revision would hand this client a revision it
                # never observed. The client adopts whatever ``view_revision``
                # comes back as its bound revision, so it would then pass the
                # acknowledged-revision check while reading somebody else's
                # overlay. Idempotent, not an error, never a second bump.
                return self._replayed_state(view, replay)
            # Report stale revision before monotonic client sequence:
            # both can be stale after another replica wins the row CAS.
            committed = int(view["revision"])
            if expected_view_revision != committed:
                raise ServerError(
                    ErrorCode.VIEW_REVISION_CONFLICT,
                    "overlay write is based on a revision that is no longer current",
                    details={
                        "expected_view_revision": expected_view_revision,
                        "committed_view_revision": committed,
                    },
                    action=AgentAction.REFRESH_VIEW_REVISION,
                )
            last_seq = int(view["last_client_seq"])
            if client_seq <= last_seq:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "client_seq must increase monotonically within a view",
                    details={"client_seq": client_seq, "last_client_seq": last_seq},
                    action=AgentAction.FIX_REQUEST,
                )
            conn.executemany(
                """
                INSERT INTO overlay (view_id, path, content_digest, parser_profile, mode, size, tombstone)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT (view_id, path) DO UPDATE SET
                    content_digest = excluded.content_digest,
                    parser_profile = excluded.parser_profile,
                    mode = excluded.mode,
                    size = excluded.size,
                    tombstone = 0
                """,
                [
                    (view_id, entry.path, entry.content_digest, entry.parser_profile, entry.mode, entry.size)
                    for entry in entries
                ],
            )
            conn.executemany(
                """
                INSERT INTO overlay (view_id, path, content_digest, parser_profile, mode, size, tombstone)
                VALUES (?, ?, '', '', 0, 0, 1)
                ON CONFLICT (view_id, path) DO UPDATE SET tombstone = 1
                """,
                [(view_id, path) for path in removed_paths],
            )
            if self._complete(org_id, str(view["root"])):
                # Normalize only incoming paths, preserving delta-sized work.
                conn.executemany(
                    """DELETE FROM overlay WHERE view_id = ? AND path = ? AND tombstone = 0
                    AND EXISTS (SELECT 1 FROM manifest_rows b WHERE b.org_id = ? AND b.root = ?
                    AND b.path = overlay.path AND b.content_digest = overlay.content_digest
                    AND b.parser_profile = overlay.parser_profile AND b.mode = overlay.mode AND b.size = overlay.size)""",
                    [(view_id, entry.path, org_id, str(view["root"])) for entry in entries],
                )
                conn.executemany(
                    """DELETE FROM overlay WHERE view_id = ? AND path = ? AND tombstone = 1
                    AND NOT EXISTS (SELECT 1 FROM manifest_rows b WHERE b.org_id = ? AND b.root = ?
                    AND b.path = overlay.path)""",
                    [(view_id, path, org_id, str(view["root"])) for path in removed_paths],
                )
            new_revision = committed + 1
            conn.execute(
                "UPDATE views SET revision = ?, last_client_seq = ? WHERE view_id = ?",
                (new_revision, client_seq, view_id),
            )
            acknowledged = self._state(self._require(view_id, org_id))
            conn.execute(
                "INSERT INTO replays "
                "(view_id, client_seq, revision, entry_count, lease_expires_at, pending_chunks, manifest_complete) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    view_id,
                    client_seq,
                    acknowledged.view_revision,
                    acknowledged.entry_count,
                    acknowledged.lease_expires_at,
                    json.dumps(list(acknowledged.pending_chunks)),
                    int(acknowledged.manifest_complete),
                ),
            )
            conn.execute(
                """
                DELETE FROM replays WHERE view_id = ? AND client_seq NOT IN (
                    SELECT client_seq FROM replays WHERE view_id = ? ORDER BY client_seq DESC LIMIT ?
                )
                """,
                (view_id, view_id, _REPLAY_HISTORY),
            )
        # The recorded acknowledgement *is* the answer, so the write and its
        # replay cannot drift apart: there is one object, stored and returned.
        return acknowledged

    def _replayed_state(self, view: sqlite3.Row, replay: sqlite3.Row) -> ViewState:
        """Rebuild the acknowledgement a previous write already returned.

        Only the five fields that move with a commit are recorded; the rest
        identify the view and cannot change while it exists.
        """
        chunks = json.loads(str(replay["pending_chunks"]))
        return ViewState(
            view_id=str(view["view_id"]),
            org_id=str(view["org_id"]),
            repo_id=str(view["repo_id"]),
            base_revision=str(view["base_revision"]),
            view_revision=int(replay["revision"]),
            manifest_root=str(view["root"]),
            entry_count=int(replay["entry_count"]),
            lease_expires_at=float(replay["lease_expires_at"]),
            pending_chunks=tuple(str(chunk) for chunk in chunks),
            generation_id=str(view["generation_id"]),
            manifest_complete=bool(int(replay["manifest_complete"])),
            content_subject=str(view["content_subject"]),
            owner_subject=str(view["owner_subject"]),
        )

    def entry(self, org_id: str, view_id: str, path: str) -> ManifestEntry | None:
        view = self._require(view_id, org_id)
        row = self._db.row(
            "SELECT path, content_digest, parser_profile, mode, size, tombstone FROM overlay "
            "WHERE view_id = ? AND path = ?",
            (view_id, path),
        )
        if row is not None:
            return None if int(row["tombstone"]) else _entry_of(row)
        # Same switch as :meth:`_base`, spelled as a primary-key lookup because
        # this is the per-path read and a scan of the manifest would not be.
        if self._complete(org_id, str(view["root"])):
            base = self._db.row(
                "SELECT path, content_digest, parser_profile, mode, size FROM manifest_rows "
                "WHERE org_id = ? AND root = ? AND path = ?",
                (org_id, str(view["root"]), path),
            )
        else:
            base = self._db.row(
                "SELECT path, content_digest, parser_profile, mode, size FROM manifest_staging "
                "WHERE view_id = ? AND path = ?",
                (view_id, path),
            )
        return None if base is None else _entry_of(base)

    def entries_for_digests(self, org_id: str, view_id: str, digests: Sequence[str]) -> Mapping[str, ManifestEntry]:
        if not digests:
            return {}
        view = self._require(view_id, org_id)
        complete = self._complete(org_id, str(view["root"]))
        found: dict[str, ManifestEntry] = {}
        for chunk in _chunks(tuple(dict.fromkeys(digests))):
            places = _placeholders(len(chunk))
            overlay_rows = self._db.rows(
                "SELECT path, content_digest, parser_profile, mode, size FROM overlay "
                f"WHERE view_id=? AND tombstone=0 AND content_digest IN ({places})",
                (view_id, *chunk),
            )
            for row in overlay_rows:
                found[str(row["path"])] = _entry_of(row)
            if complete:
                base_rows = self._db.rows(
                    "SELECT m.path, m.content_digest, m.parser_profile, m.mode, m.size "
                    "FROM manifest_rows m INDEXED BY ix_manifest_rows_digest "
                    f"WHERE m.org_id=? AND m.root=? AND m.content_digest IN ({places}) "
                    "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)",
                    (org_id, str(view["root"]), *chunk, view_id),
                )
            else:
                base_rows = self._db.rows(
                    "SELECT m.path, m.content_digest, m.parser_profile, m.mode, m.size "
                    "FROM manifest_staging m INDEXED BY ix_manifest_staging_digest "
                    f"WHERE m.view_id=? AND m.content_digest IN ({places}) "
                    "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)",
                    (view_id, *chunk, view_id),
                )
            for row in base_rows:
                found[str(row["path"])] = _entry_of(row)
        return found

    def path_candidates(
        self, org_id: str, view_id: str, needle: str, *, limit: int
    ) -> tuple[Mapping[str, ManifestEntry], bool]:
        if not needle or limit < 1:
            return {}, False
        view = self._require(view_id, org_id)
        complete = self._complete(org_id, str(view["root"]))
        root = str(view["root"])
        lowered = needle.lower()
        if complete and len(lowered) >= 3:
            phrase = '"' + lowered.replace('"', '""') + '"'
            base_rows = self._db.rows(
                """
                SELECT m.path, m.content_digest, m.parser_profile, m.mode, m.size
                  FROM manifest_path_search s
                  JOIN manifest_rows m
                    ON m.org_id=s.org_id AND m.root=s.root AND m.path=s.path
                 WHERE s.path MATCH ? AND s.org_id=? AND s.root=?
                   AND NOT EXISTS (
                       SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path
                   )
                 LIMIT ?
                """,
                (phrase, org_id, root, view_id, limit + 1),
            )
            overlay_rows = self._db.rows(
                "SELECT path, content_digest, parser_profile, mode, size FROM overlay "
                "WHERE view_id=? AND tombstone=0 AND instr(lower(path), ?) > 0 LIMIT ?",
                (view_id, lowered, limit + 1),
            )
            rows = sorted((*base_rows, *overlay_rows), key=lambda row: str(row["path"]))[: limit + 1]
        else:
            if complete:
                base_sql = (
                    "SELECT m.path, m.content_digest, m.parser_profile, m.mode, m.size FROM manifest_rows m "
                    "WHERE m.org_id=? AND m.root=? "
                    "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)"
                )
                base_params: tuple[Any, ...] = (org_id, root, view_id)
            else:
                base_sql = (
                    "SELECT m.path, m.content_digest, m.parser_profile, m.mode, m.size FROM manifest_staging m "
                    "WHERE m.view_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)"
                )
                base_params = (view_id, view_id)
            rows = self._db.rows(
                "SELECT * FROM ("
                + base_sql
                + " UNION ALL SELECT path, content_digest, parser_profile, mode, size FROM overlay "
                "WHERE view_id=? AND tombstone=0) effective "
                "WHERE instr(lower(path), ?) > 0 ORDER BY path LIMIT ?",
                (*base_params, view_id, lowered, limit + 1),
            )
        selected = rows[:limit]
        return {str(row["path"]): _entry_of(row) for row in selected}, len(rows) > limit

    def membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        return self._effective(self._require(view_id, org_id))

    def content_reachability(self, org_id: str, view_id: str, size_cap: int) -> ViewContentReachability:
        """Missing required content without materializing the whole View in Python.

        This is the same possession rule as ``boundary.addressable`` expressed
        as a scoped SQL anti-join. A manifest declaration never probes the
        org-wide blob table: only provenance for this View's repository/content
        subject can satisfy it, so the no-existence-oracle boundary is
        unchanged. The effective manifest CTE applies overlay shadowing before
        grouping by digest and preserves the old first-path ordering.
        """
        view = self._require(view_id, org_id)
        root = str(view["root"])
        if self._complete(org_id, root):
            base_sql = (
                "SELECT m.path, m.content_digest, m.size FROM manifest_rows m "
                "WHERE m.org_id=? AND m.root=? "
                "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)"
            )
            base_params: tuple[Any, ...] = (org_id, root, view_id)
        else:
            base_sql = (
                "SELECT m.path, m.content_digest, m.size FROM manifest_staging m "
                "WHERE m.view_id=? "
                "AND NOT EXISTS (SELECT 1 FROM overlay o WHERE o.view_id=? AND o.path=m.path)"
            )
            base_params = (view_id, view_id)
        effective_sql = (
            base_sql + " UNION ALL SELECT o.path, o.content_digest, o.size FROM overlay o "
            "WHERE o.view_id=? AND o.tombstone=0"
        )
        effective_params = (*base_params, view_id)
        required_paths = self._db.scalar_int(
            "SELECT COUNT(*) FROM (" + effective_sql + ") effective WHERE size <= ?",
            (*effective_params, size_cap),
        )
        subject = str(view["content_subject"])
        repo_id = str(view["repo_id"])
        provenance_sql = (
            "SELECT 1 FROM content_provenance p "
            "WHERE p.org_id=? AND p.repo_id=? AND p.subject=? AND p.digest=r.content_digest"
        )
        provenance_params: tuple[Any, ...] = (org_id, repo_id, subject)
        rows = self._db.rows(
            "WITH effective AS (" + effective_sql + "), required AS ("
            "SELECT content_digest, MIN(path) AS first_path, COUNT(*) AS path_count "
            "FROM effective WHERE size <= ? GROUP BY content_digest) "
            "SELECT r.content_digest, r.path_count FROM required r "
            "WHERE NOT EXISTS (" + provenance_sql + ") ORDER BY r.first_path",
            (*effective_params, size_cap, *provenance_params),
        )
        return ViewContentReachability(
            missing_digests=tuple(str(row["content_digest"]) for row in rows),
            required_paths=required_paths,
            missing_paths=sum(int(row["path_count"]) for row in rows),
        )

    def declared_digests(self, org_id: str, view_id: str) -> tuple[str, ...]:
        """Digests this view's own manifest names, in canonical path order."""
        effective = self._effective(self._require(view_id, org_id))
        return tuple(dict.fromkeys(effective[path].content_digest for path in sorted(effective)))

    def renew(self, org_id: str, view_id: str, lease_s: float) -> ViewState:
        # Extends, never shortens: a View opened with a longer lease (a Review
        # snapshot) keeps it when its session's activity renews it.
        with self._db.transaction() as conn:
            self._require(view_id, org_id)
            conn.execute(
                "UPDATE views SET lease_expires_at = MAX(lease_expires_at, ?) WHERE view_id = ?",
                (self._now() + lease_s, view_id),
            )
        return self._state(self._require(view_id, org_id))

    def close(self, org_id: str, view_id: str) -> None:
        with self._db.transaction() as conn:
            row = self._db.row("SELECT org_id FROM views WHERE view_id = ?", (view_id,))
            if row is None or str(row["org_id"]) != org_id:
                return
            self._delete_locked(conn, (view_id,))

    def sweep_manifest_roots(self, before: float) -> int:
        """Drop roots no live view names and nobody has opened since ``before``.

        Closing a view must *not* take its manifest root with it. The thin
        client's ``SessionStart`` hook opens a view, syncs, and closes it again
        -- so a root that died with its last view would make every session
        cold, re-sending thousands of manifest rows for an unchanged tree and
        putting the plan's warm target out of reach. A root is org-scoped,
        content-addressed and immutable, so it is safe to keep; what bounds it
        is the retention window, exactly like an unreferenced blob.
        """
        with self._db.transaction() as conn:
            doomed = tuple(
                (str(row["org_id"]), str(row["root"]))
                for row in self._db.rows(
                    "SELECT org_id, root FROM manifest_roots "
                    "WHERE last_referenced_at <= ? "
                    "  AND (org_id, root) NOT IN (SELECT org_id, root FROM views)",
                    (before,),
                )
            )
            for org_id, root in doomed:
                generations = tuple(
                    str(row["generation_id"])
                    for row in conn.execute(
                        "SELECT generation_id FROM repository_generations WHERE org_id=? AND root=?",
                        (org_id, root),
                    ).fetchall()
                )
                for generation_id in generations:
                    prefix = "gscope_" + generation_id.removeprefix("gen_") + "_"
                    keys = tuple(
                        str(row["view_id"])
                        for row in conn.execute(
                            "SELECT view_id FROM link_graphs WHERE org_id=? AND substr(view_id,1,?)=?",
                            (org_id, len(prefix), prefix),
                        ).fetchall()
                    )
                    if keys:
                        for chunk in _chunks(keys):
                            places = _placeholders(len(chunk))
                            conn.execute(f"DELETE FROM link_edges WHERE view_id IN ({places})", chunk)
                            conn.execute(f"DELETE FROM link_graphs WHERE view_id IN ({places})", chunk)
                conn.execute("DELETE FROM manifest_path_search WHERE org_id=? AND root=?", (org_id, root))
                for table in ("manifest_rows", "manifest_roots"):
                    conn.execute(f"DELETE FROM {table} WHERE org_id = ? AND root = ?", (org_id, root))
            with self._root_cache_lock:
                for key in doomed:
                    self._root_cache.pop(key, None)
            return len(doomed)

    def evict_expired(self, now: float) -> tuple[str, ...]:
        with self._db.transaction() as conn:
            expired = tuple(
                sorted(
                    str(row["view_id"])
                    for row in self._db.rows("SELECT view_id FROM views WHERE lease_expires_at <= ?", (now,))
                )
            )
            if expired:
                self._delete_locked(conn, expired)
            return expired

    def _delete_locked(self, conn: sqlite3.Connection, view_ids: Sequence[str]) -> None:
        for chunk in _chunks(tuple(view_ids)):
            places = _placeholders(len(chunk))
            for table in ("overlay", "replays", "link_edges", "link_graphs", "manifest_chunks", "manifest_staging"):
                conn.execute(f"DELETE FROM {table} WHERE view_id IN ({places})", chunk)
            conn.execute(f"DELETE FROM views WHERE view_id IN ({places})", chunk)
        # The manifest root deliberately stays. See
        # :meth:`sweep_manifest_roots` -- a root that died with its last view
        # would make every ``SessionStart`` cold, because the hook opens a
        # view, syncs and closes it again. Retention bounds it instead, and the
        # window starts here, when the *last* view lets the root go: measuring
        # from the open would reclaim the root of a long-lived session the
        # moment it ended, which is exactly the session that should reopen warm.
        conn.execute(
            "UPDATE manifest_roots SET last_referenced_at = ? "
            "WHERE (org_id, root) NOT IN (SELECT org_id, root FROM views)",
            (self._now(),),
        )

    def referenced_digests(self) -> Mapping[str, frozenset[str]]:
        # A retained manifest root references its content just as a live view
        # does. Roots now outlive their views (see ``sweep_manifest_roots``),
        # and a warm root whose blobs had been swept underneath it would be
        # warm in name only -- the next session would open warm and then have
        # to re-upload the whole tree. Root and content therefore share one
        # retention window rather than racing on two.
        rows = self._db.rows("""
            SELECT v.org_id AS org_id, m.content_digest AS digest
              FROM views v
              JOIN manifest_rows m ON m.org_id = v.org_id AND m.root = v.root
             WHERE NOT EXISTS (
                   SELECT 1 FROM overlay o WHERE o.view_id = v.view_id AND o.path = m.path)
            UNION
            SELECT v.org_id AS org_id, o.content_digest AS digest
              FROM views v
              JOIN overlay o ON o.view_id = v.view_id
             WHERE o.tombstone = 0
            UNION
            -- A fill in flight references the blobs it is uploading before any
            -- root proves them, and a sweep that could not see those rows would
            -- collect the blobs out from under it.
            SELECT v.org_id AS org_id, s.content_digest AS digest
              FROM views v
              JOIN manifest_staging s ON s.view_id = v.view_id
            UNION
            SELECT r.org_id AS org_id, m.content_digest AS digest
              FROM manifest_roots r
              JOIN manifest_rows m ON m.org_id = r.org_id AND m.root = r.root
            """)
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(str(row["org_id"]), set()).add(str(row["digest"]))
        return {org_id: frozenset(digests) for org_id, digests in out.items()}

    # -- introspection --------------------------------------------------- #

    @property
    def live_views(self) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM views")

    @property
    def live_roots(self) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM manifest_roots")

    def view_count(self, org_id: str) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM views WHERE org_id = ?", (org_id,))

    def view_owner(self, org_id: str, view_id: str) -> str | None:
        rows = self._db.rows("SELECT owner_subject FROM views WHERE org_id = ? AND view_id = ?", (org_id, view_id))
        if not rows:
            return None
        subject = str(rows[0]["owner_subject"])
        return subject or None

    def view_ids_for_org(self, org_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["view_id"])
            for row in self._db.rows("SELECT view_id FROM views WHERE org_id = ? ORDER BY view_id", (org_id,))
        )

    def view_ids_for_user(self, org_id: str, subject: str) -> tuple[str, ...]:
        return tuple(
            str(row["view_id"])
            for row in self._db.rows(
                "SELECT view_id FROM views WHERE org_id = ? AND owner_subject = ? ORDER BY view_id",
                (org_id, subject),
            )
        )

    def view_count_for_user(self, org_id: str, subject: str) -> int:
        return self._db.scalar_int(
            "SELECT COUNT(*) FROM views WHERE org_id = ? AND owner_subject = ?", (org_id, subject)
        )

    def view_bytes(self, org_id: str) -> int:
        """Per-view Layer-2 cost: overlay rows only.

        Base manifest rows belong to the shared root, not to the view, because
        that sharing is exactly the claim being measured.
        """
        return (
            self._db.scalar_int(
                """
            SELECT COALESCE(SUM(LENGTH(o.path) + LENGTH(o.content_digest)
                                + LENGTH(o.parser_profile) + 16), 0)
              FROM overlay o JOIN views v ON v.view_id = o.view_id
             WHERE v.org_id = ?
            """,
                (org_id,),
            )
            + self._db.scalar_int(
                """
            SELECT COALESCE(SUM(LENGTH(e.src_path) + LENGTH(e.dst_path)
                                + LENGTH(e.symbol) + LENGTH(e.kind) + 8), 0)
              FROM link_edges e JOIN views v ON v.view_id = e.view_id
             WHERE v.org_id = ?
            """,
                (org_id,),
            )
            + self._db.scalar_int(
                "SELECT COALESCE(SUM(LENGTH(g.delta)), 0) FROM link_graphs g "
                "JOIN views v ON v.view_id = g.view_id WHERE v.org_id = ?",
                (org_id,),
            )
        )

    def root_bytes(self, org_id: str) -> int:
        return self._db.scalar_int(
            """
            SELECT COALESCE(SUM(LENGTH(path) + LENGTH(content_digest) + LENGTH(parser_profile) + 16), 0)
              FROM manifest_rows WHERE org_id = ?
            """,
            (org_id,),
        )


# --------------------------------------------------------------------------- #
# Layer 3 persistence                                                         #
# --------------------------------------------------------------------------- #


class SqliteLinkGraphStore:
    """Edges as queryable rows; index tables as one payload beside them.

    The split is deliberate. Edges are what a relations query reads, so they
    are indexed rows. The reverse symbol and module indexes are only ever read
    whole, by the next incremental update, so storing them as one blob costs
    nothing and keeps the schema honest about which rows are a query surface.
    """

    __slots__ = ("_db", "_full_graphs", "_full_lock")

    def __init__(self, db: _Db) -> None:
        self._db = db
        # Full (base-less) graphs are what every refresh of a dirty View reads
        # back first: ~146k edges rebuilt into objects on each keystroke-sized
        # revision. The durable edge_version is part of the key because payload
        # excludes edges and an equal-cardinality rewrite can keep both payload
        # and edge count unchanged.
        self._full_graphs: OrderedDict[tuple[str, str], tuple[str, int, int, LinkGraph]] = OrderedDict()
        self._full_lock = threading.Lock()

    def _cached_full_graph(
        self, org_id: str, view_id: str, payload: str, count: int, edge_version: int
    ) -> LinkGraph | None:
        key = (org_id, view_id)
        with self._full_lock:
            cached = self._full_graphs.get(key)
            if cached is None or cached[2] != edge_version or cached[1] != count or cached[0] != payload:
                return None
            self._full_graphs.move_to_end(key)
            return cached[3]

    def _remember_full_graph(
        self, org_id: str, view_id: str, payload: str, count: int, edge_version: int, graph: LinkGraph
    ) -> None:
        key = (org_id, view_id)
        with self._full_lock:
            self._full_graphs[key] = (payload, count, edge_version, graph)
            self._full_graphs.move_to_end(key)
            while len(self._full_graphs) > _FULL_GRAPH_CACHE:
                self._full_graphs.popitem(last=False)

    def _stored_edges(self, key: str, connection: sqlite3.Connection | None = None) -> set[_EdgeRow]:
        sql = f"SELECT {_EDGE_COLUMNS} FROM link_edges WHERE view_id = ?"
        rows = self._db.rows(sql, (key,)) if connection is None else self._db._read_rows(connection, sql, (key,))
        return _edge_rows(rows)

    def _row_in_snapshot(
        self, connection: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        rows = self._db._read_rows(connection, sql, parameters)
        return rows[0] if rows else None

    def _composed(
        self, view_id: str, base: str, delta: str, connection: sqlite3.Connection | None = None
    ) -> set[_EdgeRow] | None:
        """This graph's edges, or ``None`` when the generation graph it extends is gone."""
        if not base:
            return self._stored_edges(view_id, connection)
        owner = (
            self._db.row("SELECT 1 FROM link_graphs WHERE view_id = ?", (base,))
            if connection is None
            else self._row_in_snapshot(connection, "SELECT 1 FROM link_graphs WHERE view_id = ?", (base,))
        )
        if owner is None:
            return None
        added, removed = _decode_delta(delta)
        return (self._stored_edges(base, connection) - removed) | added

    def load(self, org_id: str, view_id: str, base: str = "", base_graph: LinkGraph | None = None) -> LinkGraph | None:
        """The stored graph; one extending *base* is composed over *base_graph* when given.

        A caller that already holds the generation graph saves re-reading and
        converting every one of its edges (~146k for a busy repository). The
        payload, delta and edge rows are read from one SQLite snapshot so a
        concurrent refresh can never splice two graph revisions together.
        """
        with self._db.read_snapshot() as connection:
            row = self._row_in_snapshot(
                connection,
                "SELECT payload, base, delta, edge_version, CASE WHEN base = '' THEN "
                "(SELECT count(*) FROM link_edges e WHERE e.view_id = g.view_id) ELSE 0 END AS edge_count "
                "FROM link_graphs g WHERE view_id = ? AND org_id = ?",
                (view_id, org_id),
            )
            if row is None:
                return None
            payload = str(row["payload"])
            stored_base = str(row["base"])
            edge_version = int(row["edge_version"])
            if not stored_base:
                count = int(row["edge_count"])
                cached = self._cached_full_graph(org_id, view_id, payload, count, edge_version)
                if cached is not None:
                    return cached
                stored = self._stored_edges(view_id, connection)
                edges = frozenset(_as_edge(edge) for edge in stored)
            elif stored_base == base and base_graph is not None:
                added, removed = _decode_delta(str(row["delta"]))
                edges = (base_graph.edges - {_as_edge(edge) for edge in removed}) | {_as_edge(edge) for edge in added}
                count = 0
            else:
                composed = self._composed(view_id, stored_base, str(row["delta"]), connection)
                if composed is None:
                    # Rebuild from the current generation graph rather than advance
                    # a graph missing most of its edges.
                    return None
                edges = frozenset(_as_edge(edge) for edge in composed)
                count = 0
        graph = LinkGraph.from_json(json.loads(payload), edges)
        if not stored_base:
            self._remember_full_graph(org_id, view_id, payload, count, edge_version, graph)
        return graph

    def edges(self, view_id: str) -> tuple[Edge, ...]:
        with self._db.read_snapshot() as connection:
            row = self._row_in_snapshot(connection, "SELECT base, delta FROM link_graphs WHERE view_id = ?", (view_id,))
            edges = (
                self._composed(view_id, str(row["base"]), str(row["delta"]), connection)
                if row is not None
                else self._stored_edges(view_id, connection)
            )
        return tuple(sorted(_as_edge(edge) for edge in edges or ()))

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        wanted = tuple(dict.fromkeys(paths))
        if not wanted:
            return ()
        with self._db.read_snapshot() as connection:
            owner = self._row_in_snapshot(
                connection, "SELECT org_id, base, delta FROM link_graphs WHERE view_id = ?", (view_id,)
            )
            if owner is None or str(owner["org_id"]) != org_id:
                return ()
            base = str(owner["base"])
            if (
                base
                and self._row_in_snapshot(connection, "SELECT 1 FROM link_graphs WHERE view_id = ?", (base,)) is None
            ):
                return ()
            found: set[_EdgeRow] = set()
            for chunk in _chunks(wanted):
                places = _placeholders(len(chunk))
                for column in ("src_path", "dst_path"):
                    found |= _edge_rows(
                        self._db._read_rows(
                            connection,
                            f"SELECT {_EDGE_COLUMNS} FROM link_edges WHERE view_id = ? AND {column} IN ({places})",
                            (base or view_id, *chunk),
                        )
                    )
            if base:
                added, removed = _decode_delta(str(owner["delta"]))
                asked = set(wanted)
                found = (found - removed) | {edge for edge in added if edge[1] in asked or edge[2] in asked}
        return tuple(sorted(_as_edge(edge) for edge in found))

    def save(
        self, org_id: str, view_id: str, graph: LinkGraph, base: str = "", base_graph: LinkGraph | None = None
    ) -> None:
        """Store *graph*; given its generation graph key *base*, as a delta over it.

        A dirty View's graph is its generation graph plus the edited files'
        edges. Stored as full rows, every live dirty View carried its own copy
        of the whole repository graph (~266k rows, ~160 MB for a busy
        repository) for its entire lease. A graph that drifted far from its base
        keeps full rows, written as a difference against what is stored.
        *base_graph*, the graph stored under *base* when the caller holds it,
        spares reading every base edge back to compute the delta.
        """
        payload = json.dumps(graph.to_json(), separators=(",", ":"), ensure_ascii=False)
        wanted: set[_EdgeRow] | None = None
        with self._db.transaction() as conn:
            delta = ""
            if (
                base
                and base != view_id
                and conn.execute(
                    "SELECT 1 FROM link_graphs WHERE view_id = ? AND org_id = ? AND base = ''", (base, org_id)
                ).fetchone()
                is not None
            ):
                if base_graph is not None:
                    over_base = {_edge_row(edge) for edge in graph.edges - base_graph.edges}
                    under_base = {_edge_row(edge) for edge in base_graph.edges - graph.edges}
                else:
                    base_edges = _edge_rows(
                        conn.execute(f"SELECT {_EDGE_COLUMNS} FROM link_edges WHERE view_id = ?", (base,)).fetchall()
                    )
                    wanted = {_edge_row(edge) for edge in graph.edges}
                    over_base, under_base = wanted - base_edges, base_edges - wanted
                if len(over_base) + len(under_base) <= max(_DELTA_MIN_EDGES, len(graph.edges) // _DELTA_FRACTION):
                    delta = _encode_delta(over_base, under_base)
            if delta:
                conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
            else:
                base = ""
                if wanted is None:
                    wanted = {_edge_row(edge) for edge in graph.edges}
                # Saved again on every revision, a full graph differs from the
                # stored one only by the edited files' edges: write that difference.
                stored = _edge_rows(
                    conn.execute(f"SELECT {_EDGE_COLUMNS} FROM link_edges WHERE view_id = ?", (view_id,)).fetchall()
                )
                removed = stored - wanted
                self._rebase_dependents(conn, org_id, view_id, stored, wanted - stored, removed)
                if len(removed) * 2 > len(stored):
                    conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
                    added = wanted
                else:
                    conn.executemany(
                        "DELETE FROM link_edges WHERE view_id = ? AND kind = ? AND src_path = ? AND dst_path = ? "
                        "AND symbol = ? AND src_line = ?",
                        [(view_id, *row) for row in removed],
                    )
                    added = wanted - stored
                conn.executemany(
                    f"INSERT OR IGNORE INTO link_edges (view_id, {_EDGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                    [(view_id, *row) for row in added],
                )
            conn.execute(
                """
                INSERT INTO link_graphs (view_id, org_id, payload, base, delta, edge_version)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (view_id) DO UPDATE SET org_id = excluded.org_id, payload = excluded.payload,
                    base = excluded.base, delta = excluded.delta, edge_version = link_graphs.edge_version + 1
                """,
                (view_id, org_id, payload, base, delta),
            )
            version_row = conn.execute("SELECT edge_version FROM link_graphs WHERE view_id = ?", (view_id,)).fetchone()
            edge_version = 0 if version_row is None else int(version_row[0])
        # A dirty View stored whole is loaded back on its next revision: keep
        # what was just written so that read is not a full decode.
        with self._full_lock:
            if delta:
                self._full_graphs.pop((org_id, view_id), None)
            else:
                self._full_graphs[(org_id, view_id)] = (payload, len(graph.edges), edge_version, graph)
                self._full_graphs.move_to_end((org_id, view_id))
                while len(self._full_graphs) > _FULL_GRAPH_CACHE:
                    self._full_graphs.popitem(last=False)

    @staticmethod
    def _rebase_dependents(
        conn: Any,
        org_id: str,
        base: str,
        stored: set[_EdgeRow],
        base_added: set[_EdgeRow],
        base_removed: set[_EdgeRow],
    ) -> None:
        """Carry the deltas stored over *base* onto its new edge set.

        A generation graph grows when more of its content becomes addressable.
        A dirty View's delta was taken against the old edges: composed over the
        new ones it would gain edges for files it deleted, while its own
        revision (and so its READY status) stayed put. Rebasing needs only the
        base's own difference, since a delta's ``added`` never overlaps the old
        base and its ``removed`` lies within it.
        """
        if not base_added and not base_removed:
            return
        dependents = conn.execute(
            "SELECT view_id, delta FROM link_graphs WHERE base = ? AND org_id = ?", (base, org_id)
        ).fetchall()
        for dependent in dependents:
            view_id, delta = str(dependent[0]), str(dependent[1])
            added, removed = _decode_delta(delta)
            rebased_added = (base_removed - removed) | (added - base_added)
            rebased_removed = (removed - base_removed) | (base_added - added)
            size = len(stored) - len(removed) + len(added)
            if len(rebased_added) + len(rebased_removed) <= max(_DELTA_MIN_EDGES, size // _DELTA_FRACTION):
                conn.execute(
                    "UPDATE link_graphs SET delta = ?, edge_version = edge_version + 1 WHERE view_id = ?",
                    (_encode_delta(rebased_added, rebased_removed), view_id),
                )
                continue
            # Drifted too far from the new base to stay a delta: keep it whole.
            conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
            conn.executemany(
                f"INSERT OR IGNORE INTO link_edges (view_id, {_EDGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                [(view_id, *row) for row in (stored - removed) | added],
            )
            conn.execute(
                "UPDATE link_graphs SET base = '', delta = '', edge_version = edge_version + 1 WHERE view_id = ?",
                (view_id,),
            )

    def drop(self, org_id: str, view_id: str) -> None:
        with self._db.transaction() as conn:
            row = self._db.row("SELECT org_id FROM link_graphs WHERE view_id = ?", (view_id,))
            if row is not None and str(row["org_id"]) != org_id:
                return
            conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
            conn.execute("DELETE FROM link_graphs WHERE view_id = ?", (view_id,))

    def drop_view(self, view_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
            conn.execute("DELETE FROM link_graphs WHERE view_id = ?", (view_id,))


class SqliteRepoIdentityService:
    """Issues opaque, org-scoped ``repo_id`` values and persists them.

    A claim selects an identity; it never becomes one. Two organizations
    presenting the identical claim receive different ids, so a repository
    cannot be correlated across tenants even by someone holding both rows.
    """

    __slots__ = ("_db",)

    def __init__(self, db: _Db) -> None:
        self._db = db

    def canonical_repo_id(self, org_id: str, claim: RepoIdentityClaim) -> str:
        key = claim.key()
        existing = self._db.row("SELECT repo_id FROM repos WHERE org_id = ? AND claim_key = ?", (org_id, key))
        if existing is not None:
            return str(existing["repo_id"])
        repo_id = new_opaque_id("repo")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO repos (org_id, claim_key, repo_id) VALUES (?, ?, ?)",
                (org_id, key, repo_id),
            )
        settled = self._db.row("SELECT repo_id FROM repos WHERE org_id = ? AND claim_key = ?", (org_id, key))
        return str(settled["repo_id"]) if settled is not None else repo_id


# --------------------------------------------------------------------------- #
# Composition                                                                 #
# --------------------------------------------------------------------------- #


class SqliteIndex:
    """The durable backend and its connection, as one closeable handle."""

    __slots__ = (
        "_analysis",
        "_content",
        "_db",
        "_links",
        "_provenance",
        "_repos",
        "_semantic",
        "_views",
        "backend",
    )

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], float] = time.time,
        content_size_cap: int,
        inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
        compact: bool = False,
        max_bytes: int = 0,
        max_wal_bytes: int = 0,
        ops: IndexOpsMetrics | None = None,
    ) -> None:
        self._db = _Db(str(path), reclaim=compact, max_bytes=max_bytes, max_wal_bytes=max_wal_bytes)
        self._content = SqliteContentStore(self._db, clock=clock)
        self._analysis: SqliteAnalysisStore
        if compact:
            from .compact import CompactAnalysisStore

            self._analysis = CompactAnalysisStore(self._db)
        else:
            self._analysis = SqliteAnalysisStore(self._db)
        self._views = SqliteViewStore(self._db, clock=clock)
        self._repos = SqliteRepoIdentityService(self._db)
        self._provenance = SqliteContentProvenance(self._db)
        ops = ops or IndexOpsMetrics()
        sources = LayerSources(
            views=self._views,
            content=self._content,
            analysis=self._analysis,
            provenance=self._provenance,
            content_size_cap=content_size_cap,
        )
        self._links = LinkLayer(
            sources,
            SqliteLinkGraphStore(self._db),
            inline_rebuild_budget=inline_rebuild_budget,
        )
        self._semantic = create_semantic_index(sources, inline_rebuild_budget=inline_rebuild_budget)
        self.backend = IndexBackend(
            content=self._content,
            analysis=self._analysis,
            views=self._views,
            links=self._links,
            semantic=self._semantic,
            repos=self._repos,
            provenance=self._provenance,
            check_ready=self._db.check_ready,
            view_content_reachability=self._views.content_reachability,
            close_backend=self._db.close,
            ops=ops,
        )

    @property
    def path(self) -> str:
        return self._db.path

    def database_bytes(self) -> int:
        return self._db.page_bytes()

    def close(self) -> None:
        self._db.close()


def _resolved_compact_mode(path: str | Path, compact: bool | None) -> bool:
    """Resolve ``None`` to compact for new/already-compact durable stores.

    Existing normalized stores stay normalized so a binary upgrade never turns
    startup into an implicit multi-gigabyte posting migration. New shared
    server stores use the same compressed FTS5 representation already shipped
    by per-repository local indexes, and an already-compact store reopens in the
    same mode. Explicit ``True``/``False`` remains available to tests, migration
    tooling and callers that own the physical format.
    """
    if compact is not None:
        return compact
    raw = str(path)
    if raw == ":memory:" or raw.startswith("file:"):
        return True
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.stat().st_size == 0:
            return True
        conn = sqlite3.connect(
            "file:" + urllib.parse.quote(str(candidate.resolve())) + "?mode=ro",
            uri=True,
            timeout=1,
        )
        try:
            return (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_documents'").fetchone()
                is not None
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        # Let the ordinary open path own the useful error. Falling back to the
        # legacy layout here avoids treating an unreadable/corrupt existing file
        # as permission to initialize a different physical format over it.
        return False


def open_sqlite_index(
    path: str | Path = ":memory:",
    *,
    clock: Callable[[], float] = time.time,
    content_size_cap: int,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
    compact: bool | None = False,
) -> SqliteIndex:
    return SqliteIndex(
        path,
        clock=clock,
        content_size_cap=content_size_cap,
        inline_rebuild_budget=inline_rebuild_budget,
        compact=_resolved_compact_mode(path, compact),
    )


def build_sqlite_backend(
    path: str | Path = ":memory:",
    *,
    clock: Callable[[], float] = time.time,
    content_size_cap: int,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
    compact: bool | None = False,
) -> IndexBackend:
    """Open a durable index and hand back just its ports.

    The :class:`SqliteIndex` stays alive through the stores that reference it,
    so a caller that never needs to close the connection -- the server process
    -- does not have to hold a second handle.
    """
    return open_sqlite_index(
        path,
        clock=clock,
        content_size_cap=content_size_cap,
        inline_rebuild_budget=inline_rebuild_budget,
        compact=compact,
    ).backend
