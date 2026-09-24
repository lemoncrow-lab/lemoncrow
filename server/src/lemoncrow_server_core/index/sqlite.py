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

import json
import os
import sqlite3
import struct
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Final

from ..context import new_opaque_id
from ..errors import AgentAction, ErrorCode, ServerError
from .analysis import LexicalQuery, analyze, cosine, parse_query, score_weighted_match
from .contracts import (
    AnalysisArtifact,
    ContentRef,
    Edge,
    EdgeKind,
    IndexBackend,
    ManifestEntry,
    RepoIdentityClaim,
    SweepResult,
    SymbolDef,
    ViewOpenResult,
    ViewState,
    sha256_hex,
)
from .layers import DEFAULT_INLINE_REBUILD_BUDGET, LayerSources, LinkLayer, SemanticLayer
from .links import LinkGraph
from .manifest import manifest_root as canonical_manifest_root

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

CREATE TABLE IF NOT EXISTS views (
    view_id          TEXT    PRIMARY KEY,
    org_id           TEXT    NOT NULL,
    repo_id          TEXT    NOT NULL,
    base_revision    TEXT    NOT NULL,
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
    view_id TEXT PRIMARY KEY,
    org_id  TEXT NOT NULL,
    payload TEXT NOT NULL
);
""" + _counter_schema()  # triggers name the tables above, so they come last


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


def _absolute(path: str) -> str:
    """Pin a filesystem database path before the process can change directory.

    Tool dispatch binds the working directory to the tenant's tree for the
    duration of a call (``registry_dispatch._workspace_binding``), because the
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
        "_lock",
        "_owner",
        "_reader_conns",
        "_reader_lock",
        "_reader_uri",
        "_readers",
        "path",
    )

    def __init__(self, path: str) -> None:
        self.path = _absolute(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        self._depth = 0
        self._owner: int | None = None
        self._closed = False
        self._readers = threading.local()
        self._reader_conns: list[sqlite3.Connection] = []
        # Not ``_lock``: a writer holds that for its whole transaction, and a
        # thread's first read must not wait on it just to register its connection.
        self._reader_lock = threading.Lock()
        durable = path != ":memory:" and not path.startswith("file:")
        self._reader_uri = f"file:{urllib.parse.quote(self.path)}?mode=ro" if durable else ""
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            # The default page cache is 2 MB against a multi-gigabyte index.
            self._conn.execute("PRAGMA cache_size = -65536")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._backfill_counters()
        self._backfill_gram_stats()
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
        if self._has_table("views") and not self._has_column("views", "owner_subject"):
            self._conn.execute(f"ALTER TABLE views ADD COLUMN owner_subject TEXT NOT NULL DEFAULT '{_NO_OWNER}'")
            self._conn.execute(
                "UPDATE views SET owner_subject = content_subject "
                "WHERE owner_subject = ? AND content_subject NOT IN ('', ?)",
                (_NO_OWNER, _NO_SCOPE),
            )
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
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            self._owner = threading.get_ident()
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._depth = 0
                self._owner = None

    def _reader(self) -> sqlite3.Connection | None:
        """This thread's read-only connection, or ``None`` to read on the writer."""
        if not self._reader_uri or self._closed or self._owner == threading.get_ident():
            return None
        reader: sqlite3.Connection | None = getattr(self._readers, "conn", None)
        if reader is None:
            reader = sqlite3.connect(self._reader_uri, uri=True, check_same_thread=False, isolation_level=None)
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA busy_timeout = 5000")
            reader.execute("PRAGMA cache_size = -16384")
            with self._reader_lock:
                self._reader_conns.append(reader)
            self._readers.conn = reader
        return reader

    def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        reader = self._reader()
        if reader is not None:
            cursor = reader.execute(sql, tuple(parameters))
            try:
                return cursor.fetchall()
            finally:
                cursor.close()
        with self._lock:
            cursor = self._conn.execute(sql, tuple(parameters))
            try:
                return cursor.fetchall()
            finally:
                cursor.close()

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
                for reader in self._reader_conns:
                    with suppress(sqlite3.Error):
                        reader.close()
                self._reader_conns.clear()
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
            for row in self._db.rows("SELECT DISTINCT org_id FROM blobs"):
                org_id = str(row["org_id"])
                live = sorted(referenced.get(org_id, frozenset()))
                conn.execute(
                    "UPDATE blobs SET unreferenced_since = ? WHERE org_id = ? AND unreferenced_since IS NULL",
                    (now, org_id),
                )
                for chunk in _chunks(live):
                    conn.execute(
                        "UPDATE blobs SET unreferenced_since = NULL "
                        f"WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
                        (org_id, *chunk),
                    )
        return self._db.scalar_int("SELECT COUNT(*) FROM blobs WHERE unreferenced_since IS NOT NULL")

    def sweep_marked(self, before: float) -> SweepResult:
        dropped: dict[str, list[str]] = {}
        reclaimed: dict[str, int] = {}
        with self._db.transaction() as conn:
            rows = self._db.rows(
                "SELECT org_id, digest, size FROM blobs "
                "WHERE unreferenced_since IS NOT NULL AND unreferenced_since <= ?",
                (before,),
            )
            for row in rows:
                org_id = str(row["org_id"])
                dropped.setdefault(org_id, []).append(str(row["digest"]))
                reclaimed[org_id] = reclaimed.get(org_id, 0) + int(row["size"])
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
    """Possession rows, durable. Every statement names ``org_id``."""

    __slots__ = ("_db",)

    def __init__(self, db: _Db) -> None:
        self._db = db

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        if not repo_id or not subject or not digests:
            # An unattributed row would read exactly like the repository-wide
            # scope, which is the one thing that must not be forgeable.
            return 0
        wanted = tuple(dict.fromkeys(digests))
        written = 0
        with self._db.transaction() as conn:
            for digest in wanted:
                written += conn.execute(
                    "INSERT OR IGNORE INTO content_provenance (org_id, repo_id, subject, digest) "
                    "VALUES (?, ?, ?, ?)",
                    (org_id, repo_id, subject, digest),
                ).rowcount
        return written

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        wanted = set(digests)
        if not wanted or not repo_id:
            return frozenset()
        if subject:
            rows = self._db.rows(
                "SELECT digest FROM content_provenance " "WHERE org_id = ? AND repo_id = ? AND subject = ?",
                (org_id, repo_id, subject),
            )
        else:
            rows = self._db.rows(
                "SELECT DISTINCT digest FROM content_provenance INDEXED BY ix_provenance_repo "
                "WHERE org_id = ? AND repo_id = ?",
                (org_id, repo_id),
            )
        return frozenset(str(row["digest"]) for row in rows if str(row["digest"]) in wanted)

    def forget(self, org_id: str, digests: Sequence[str]) -> int:
        if not digests:
            return 0
        dropped = 0
        with self._db.transaction() as conn:
            for chunk in _chunks(tuple(dict.fromkeys(digests))):
                dropped += conn.execute(
                    "DELETE FROM content_provenance " f"WHERE org_id = ? AND digest IN ({_placeholders(len(chunk))})",
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
            "SELECT language, line_count FROM analysis " "WHERE org_id = ? AND digest = ? AND parser_profile = ?",
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
        artifact = artifact if artifact is not None else analyze(digest, parser_profile, data)
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
                    digest,
                    parser_profile,
                    artifact.language,
                    artifact.line_count,
                    artifact.byte_count,
                    payload,
                    len(payload),
                ),
            )
            if cursor.rowcount == 0:
                # Another writer derived it first. Write-once means the first
                # artifact for a scoped key is the artifact, forever.
                return self.get(org_id, digest, parser_profile) or artifact
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
            conn.executemany(
                """
                INSERT OR IGNORE INTO analysis_defs
                    (org_id, digest, parser_profile, name, kind, line, span_start, span_end, exported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        org_id,
                        digest,
                        parser_profile,
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
                (org_id, digest, _pack(artifact.embedding)),
            )
        with self._lock:
            self._derivations += 1
        self._clear_search_caches()
        return artifact

    def count(self, org_id: str) -> int:
        return self._db.scalar_int("SELECT COUNT(*) FROM analysis WHERE org_id = ?", (org_id,))

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
        document_count = len(set(digests))
        scores: dict[str, float] = {}
        for digest in set(matched_terms) | set(gram_hits):
            digest_terms = tuple(term for term in parsed.terms if term in matched_terms.get(digest, set()))
            inferred_gram_hits = len(parsed.grams) if digest_terms and not include_grams else 0
            score = score_weighted_match(
                parsed,
                matched_terms=digest_terms,
                term_document_frequency=term_df,
                document_count=document_count,
                gram_hits=gram_hits.get(digest, inferred_gram_hits),
                match_any_term=match_any_term,
            )
            if score > 0:
                scores[digest] = score
        return scores

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

        found: dict[str, set[str]] = {}
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
                "SELECT digest FROM analysis_grams INDEXED BY ix_analysis_grams " "WHERE org_id = ? AND gram = ?",
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
                "SELECT digest FROM analysis_grams INDEXED BY ix_analysis_grams " "WHERE org_id = ? AND gram = ?",
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

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        if not digests:
            return 0
        dropped = 0
        with self._db.transaction() as conn:
            for chunk in _chunks(tuple(digests)):
                places = _placeholders(len(chunk))
                dropped += conn.execute(
                    f"DELETE FROM analysis WHERE org_id = ? AND digest IN ({places})",
                    (org_id, *chunk),
                ).rowcount
                for table in ("analysis_terms", "analysis_defs", "analysis_vectors"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE org_id = ? AND digest IN ({places})",
                        (org_id, *chunk),
                    )
                gram_bytes_row = conn.execute(
                    f"SELECT COALESCE(SUM(LENGTH(gram)), 0) FROM analysis_grams "
                    f"WHERE org_id = ? AND digest IN ({places})",
                    (org_id, *chunk),
                ).fetchone()
                gram_bytes = int(gram_bytes_row[0] or 0) if gram_bytes_row is not None else 0
                conn.execute(
                    f"DELETE FROM analysis_grams WHERE org_id = ? AND digest IN ({places})",
                    (org_id, *chunk),
                )
                if gram_bytes:
                    conn.execute(
                        "INSERT INTO storage_counters (org_id, kind, bytes) VALUES (?, 'analysis', ?) "
                        "ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes",
                        (org_id, -gram_bytes),
                    )
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

    __slots__ = ("_clock", "_db", "_root_cache", "_root_cache_lock")

    def __init__(self, db: _Db, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._root_cache: OrderedDict[tuple[str, str], Mapping[str, ManifestEntry]] = OrderedDict()
        self._root_cache_lock = threading.Lock()

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

    def _effective(self, view: sqlite3.Row) -> dict[str, ManifestEntry]:
        org_id = str(view["org_id"])
        root = str(view["root"])
        if self._complete(org_id, root):
            key = (org_id, root)
            with self._root_cache_lock:
                cached = self._root_cache.get(key)
                if cached is not None:
                    self._root_cache.move_to_end(key)
            if cached is None:
                cached = {str(row["path"]): _entry_of(row) for row in self._base(view)}
                with self._root_cache_lock:
                    self._root_cache[key] = cached
                    self._root_cache.move_to_end(key)
                    while len(self._root_cache) > 32:
                        self._root_cache.popitem(last=False)
            merged = dict(cached)
        else:
            merged = {str(row["path"]): _entry_of(row) for row in self._base(view)}
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
        """How many paths the view has, without materializing any of them.

        :meth:`_effective` answers the same question by building every row,
        which at 50,000 files is most of what ``views/open`` costs -- and every
        caller of :meth:`_state` pays it, including the ones that only wanted a
        lease renewed. The arithmetic here is the same merge ``_effective``
        performs: manifest rows the overlay has said nothing about, plus the
        overlay's own live rows. Both halves are primary-key lookups.
        """
        view_id = str(view["view_id"])
        proved = int(self._complete(str(view["org_id"]), str(view["root"])))
        row = self._db.row(
            """
            WITH manifest AS (
                SELECT path FROM manifest_rows WHERE ? = 1 AND org_id = ? AND root = ?
                UNION ALL
                SELECT path FROM manifest_staging WHERE ? = 0 AND view_id = ?
            )
            SELECT
                (SELECT COUNT(*) FROM manifest m
                  WHERE NOT EXISTS (
                        SELECT 1 FROM overlay o WHERE o.view_id = ? AND o.path = m.path
                    ))
              + (SELECT COUNT(*) FROM overlay o2 WHERE o2.view_id = ? AND o2.tombstone = 0)
              AS total
            """,
            (proved, str(view["org_id"]), str(view["root"]), proved, view_id, view_id, view_id),
        )
        return int(row["total"]) if row is not None else 0

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
                    "INSERT INTO manifest_roots (org_id, root, complete, last_referenced_at) " "VALUES (?, ?, ?, ?)",
                    (org_id, manifest_root, complete, now),
                )
            else:
                conn.execute(
                    "UPDATE manifest_roots SET last_referenced_at = ? WHERE org_id = ? AND root = ?",
                    (now, org_id, manifest_root),
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
                    (view_id, org_id, repo_id, base_revision, root, revision,
                     lease_expires_at, last_client_seq, created_at, content_subject, owner_subject)
                VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?)
                """,
                (
                    view_id,
                    org_id,
                    repo_id,
                    base_revision,
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
                conn.execute("UPDATE manifest_roots SET complete = 1 WHERE org_id = ? AND root = ?", (org_id, root))
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
            last_seq = int(view["last_client_seq"])
            if client_seq <= last_seq:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "client_seq must increase monotonically within a view",
                    details={"client_seq": client_seq, "last_client_seq": last_seq},
                    action=AgentAction.FIX_REQUEST,
                )
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
            manifest_complete=bool(int(replay["manifest_complete"])),
            content_subject=str(view["content_subject"]),
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

    def membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        return self._effective(self._require(view_id, org_id))

    def declared_digests(self, org_id: str, view_id: str) -> tuple[str, ...]:
        """Digests this view's own manifest names, in canonical path order."""
        effective = self._effective(self._require(view_id, org_id))
        return tuple(dict.fromkeys(effective[path].content_digest for path in sorted(effective)))

    def renew(self, org_id: str, view_id: str, lease_s: float) -> ViewState:
        with self._db.transaction() as conn:
            self._require(view_id, org_id)
            conn.execute(
                "UPDATE views SET lease_expires_at = ? WHERE view_id = ?",
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

    __slots__ = ("_db",)

    def __init__(self, db: _Db) -> None:
        self._db = db

    def load(self, org_id: str, view_id: str) -> LinkGraph | None:
        row = self._db.row("SELECT payload FROM link_graphs WHERE view_id = ? AND org_id = ?", (view_id, org_id))
        if row is None:
            return None
        edges = frozenset(self.edges(view_id))
        return LinkGraph.from_json(json.loads(str(row["payload"])), edges)

    def edges(self, view_id: str) -> tuple[Edge, ...]:
        rows = self._db.rows(
            "SELECT kind, src_path, dst_path, symbol, src_line FROM link_edges WHERE view_id = ?",
            (view_id,),
        )
        return tuple(
            sorted(
                Edge(
                    kind=EdgeKind(str(row["kind"])),
                    src_path=str(row["src_path"]),
                    dst_path=str(row["dst_path"]),
                    symbol=str(row["symbol"]),
                    src_line=int(row["src_line"]),
                )
                for row in rows
            )
        )

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        wanted = tuple(dict.fromkeys(paths))
        if not wanted:
            return ()
        owner = self._db.row("SELECT org_id FROM link_graphs WHERE view_id = ?", (view_id,))
        if owner is None or str(owner["org_id"]) != org_id:
            return ()
        found: set[Edge] = set()
        for chunk in _chunks(wanted):
            places = _placeholders(len(chunk))
            for column in ("src_path", "dst_path"):
                rows = self._db.rows(
                    "SELECT kind, src_path, dst_path, symbol, src_line FROM link_edges "
                    f"WHERE view_id = ? AND {column} IN ({places})",
                    (view_id, *chunk),
                )
                found.update(
                    Edge(
                        kind=EdgeKind(str(row["kind"])),
                        src_path=str(row["src_path"]),
                        dst_path=str(row["dst_path"]),
                        symbol=str(row["symbol"]),
                        src_line=int(row["src_line"]),
                    )
                    for row in rows
                )
        return tuple(sorted(found))

    def save(self, org_id: str, view_id: str, graph: LinkGraph) -> None:
        payload = json.dumps(graph.to_json(), separators=(",", ":"), ensure_ascii=False)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM link_edges WHERE view_id = ?", (view_id,))
            conn.executemany(
                """
                INSERT OR IGNORE INTO link_edges (view_id, kind, src_path, dst_path, symbol, src_line)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (view_id, edge.kind.value, edge.src_path, edge.dst_path, edge.symbol, edge.src_line)
                    for edge in graph.edges
                ],
            )
            conn.execute(
                """
                INSERT INTO link_graphs (view_id, org_id, payload) VALUES (?, ?, ?)
                ON CONFLICT (view_id) DO UPDATE SET org_id = excluded.org_id, payload = excluded.payload
                """,
                (view_id, org_id, payload),
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
    ) -> None:
        self._db = _Db(str(path))
        self._content = SqliteContentStore(self._db, clock=clock)
        self._analysis = SqliteAnalysisStore(self._db)
        self._views = SqliteViewStore(self._db, clock=clock)
        self._repos = SqliteRepoIdentityService(self._db)
        self._provenance = SqliteContentProvenance(self._db)
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
        self._semantic = SemanticLayer(sources, inline_rebuild_budget=inline_rebuild_budget)
        self.backend = IndexBackend(
            content=self._content,
            analysis=self._analysis,
            views=self._views,
            links=self._links,
            semantic=self._semantic,
            repos=self._repos,
            provenance=self._provenance,
            check_ready=self._db.check_ready,
            close_backend=self._db.close,
        )

    @property
    def path(self) -> str:
        return self._db.path

    def database_bytes(self) -> int:
        return self._db.page_bytes()

    def close(self) -> None:
        self._db.close()


def open_sqlite_index(
    path: str | Path = ":memory:",
    *,
    clock: Callable[[], float] = time.time,
    content_size_cap: int,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
) -> SqliteIndex:
    return SqliteIndex(
        path,
        clock=clock,
        content_size_cap=content_size_cap,
        inline_rebuild_budget=inline_rebuild_budget,
    )


def build_sqlite_backend(
    path: str | Path = ":memory:",
    *,
    clock: Callable[[], float] = time.time,
    content_size_cap: int,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
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
    ).backend
