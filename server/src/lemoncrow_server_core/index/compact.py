"""Compact, exact term/trigram postings backed by SQLite's compressed FTS5 lists.

Tokens are encoded, not re-tokenized: the analyzer, substring membership, tenant
boundary and scoring remain the same as SqliteAnalysisStore. FTS5 stores integer
document IDs once per posting instead of repeating an organization and SHA-256.
There are no positions, offsets, duplicated source text, or token-count columns.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence

from .contracts import AnalysisArtifact
from .sqlite import SqliteAnalysisStore, _chunks, _Db, _placeholders

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_documents (
    id INTEGER PRIMARY KEY,
    org_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    parser_profile TEXT NOT NULL,
    posting_bytes INTEGER NOT NULL,
    UNIQUE (org_id, digest)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_terms USING fts5(
    tokens, content='', detail=none, columnsize=0
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_grams USING fts5(
    tokens, content='', detail=none, columnsize=0
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_term_instances USING fts5vocab(search_terms, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS search_gram_instances USING fts5vocab(search_grams, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS search_gram_counts USING fts5vocab(search_grams, 'row');
CREATE TRIGGER IF NOT EXISTS search_documents_insert AFTER INSERT ON search_documents BEGIN
    INSERT INTO storage_counters (org_id, kind, bytes) VALUES (NEW.org_id, 'analysis', NEW.posting_bytes)
    ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes;
END;
CREATE TRIGGER IF NOT EXISTS search_documents_delete AFTER DELETE ON search_documents BEGIN
    INSERT INTO storage_counters (org_id, kind, bytes) VALUES (OLD.org_id, 'analysis', -OLD.posting_bytes)
    ON CONFLICT (org_id, kind) DO UPDATE SET bytes = bytes + excluded.bytes;
END;
"""


def _token(org_id: str, value: str) -> str:
    # An injective ASCII token, including for whitespace, quotes, NUL and Unicode.
    # The organization is in the term dictionary, not repeated in each posting.
    encoded = "x" + org_id.encode("utf-8").hex() + "z" + value.encode("utf-8").hex()
    # FTS5 truncates extremely long tokens. Use the same SHA-256 identity
    # assumption as content addressing for that case, never a truncated prefix.
    return encoded if len(encoded) <= 4096 else "y" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _tokens(org_id: str, values: Sequence[str]) -> str:
    return " ".join(_token(org_id, value) for value in sorted(set(values)))


class CompactAnalysisStore(SqliteAnalysisStore):
    """The same analysis contract, with compressed integer-ID posting lists."""

    def __init__(self, db: _Db) -> None:
        super().__init__(db)
        if not db._has_table("search_documents") and db.row("SELECT 1 FROM analysis LIMIT 1") is not None:
            raise RuntimeError("legacy index requires an offline repository migration")
        with db._lock:
            db._conn.executescript(_SCHEMA)

    def _put_postings(
        self,
        conn: sqlite3.Connection,
        org_id: str,
        digest: str,
        parser_profile: str,
        artifact: AnalysisArtifact,
    ) -> None:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO search_documents (org_id, digest, parser_profile, posting_bytes) "
            "VALUES (?, ?, ?, ?)",
            (
                org_id,
                digest,
                parser_profile,
                sum(len(term) for term in artifact.lexical_terms) + sum(len(gram) for gram in artifact.trigrams),
            ),
        )
        if cursor.rowcount == 0:
            return  # Terms/grams are content-specific, never copied per parser profile.
        document_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO search_terms (rowid, tokens) VALUES (?, ?)",
            (document_id, _tokens(org_id, artifact.lexical_terms)),
        )
        conn.execute(
            "INSERT INTO search_grams (rowid, tokens) VALUES (?, ?)", (document_id, _tokens(org_id, artifact.trigrams))
        )

    def _drop_postings(self, conn: sqlite3.Connection, org_id: str, digests: Sequence[str]) -> None:
        rows = conn.execute(
            "SELECT d.id, a.payload FROM search_documents d JOIN analysis a "
            "ON a.org_id=d.org_id AND a.digest=d.digest AND a.parser_profile=d.parser_profile "
            f"WHERE d.org_id=? AND d.digest IN ({_placeholders(len(digests))})",
            (org_id, *digests),
        ).fetchall()
        for row in rows:
            artifact = AnalysisArtifact.from_json(json.loads(str(row["payload"])))
            # Contentless FTS5 deletion needs precisely the original tokens. The
            # document remembers its first profile, whose immutable artifact stays
            # alive until this transaction deletes all profiles for the digest.
            for table, values in (("search_terms", artifact.lexical_terms), ("search_grams", artifact.trigrams)):
                conn.execute(
                    f"INSERT INTO {table} ({table}, rowid, tokens) VALUES ('delete', ?, ?)",
                    (row["id"], _tokens(org_id, values)),
                )
        conn.execute(
            f"DELETE FROM search_documents WHERE org_id=? AND digest IN ({_placeholders(len(digests))})",
            (org_id, *digests),
        )

    @staticmethod
    def _posting_table(table: str) -> tuple[str, str]:
        if table == "analysis_terms":
            return "search_terms", "search_term_instances"
        if table == "analysis_grams":
            return "search_grams", "search_gram_instances"
        raise ValueError("unknown lexical posting table")

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
        from .analysis import parse_query

        parsed = parse_query(query)
        if parsed.empty or limit < 1:
            return (), False
        cap = limit + 1
        candidates: set[str] = set()
        truncated = False
        terms = tuple(dict.fromkeys((*parsed.terms, *ignored_terms)))
        if terms:
            encoded = tuple(_token(org_id, term) for term in terms)
            rows = self._db.rows(
                "SELECT d.digest FROM search_term_instances v "
                "JOIN search_documents d ON d.id=v.doc "
                f"WHERE v.term IN ({_placeholders(len(encoded))}) AND d.org_id=? "
                "GROUP BY d.digest ORDER BY d.digest LIMIT ?",
                (*encoded, org_id, cap),
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
                expression = " AND ".join(_token(org_id, gram) for gram in anchors)
                rows = self._db.rows(
                    "SELECT d.digest FROM search_grams f "
                    "JOIN search_documents d ON d.id=f.rowid "
                    "WHERE search_grams MATCH ? AND d.org_id=? ORDER BY d.digest LIMIT ?",
                    (expression, org_id, cap),
                )
                candidates.update(str(row["digest"]) for row in rows[:limit])
                truncated = truncated or len(rows) > limit
        ordered = tuple(sorted(candidates))
        if len(ordered) > limit:
            truncated = True
        return ordered[:limit], truncated

    def _matched_values(
        self, org_id: str, digests: Sequence[str], table: str, column: str, needles: Sequence[str]
    ) -> dict[str, set[str]]:
        if not digests or not needles:
            return {}
        _fts, instances = self._posting_table(table)
        wanted = set(digests)
        found: dict[str, set[str]] = {}
        encoded = {_token(org_id, needle): needle for needle in needles}
        for chunk in _chunks(tuple(encoded)):
            rows = self._db.rows(
                f"SELECT d.digest, v.term FROM {instances} v JOIN search_documents d ON d.id=v.doc "
                f"WHERE v.term IN ({_placeholders(len(chunk))}) AND d.org_id=?",
                (*chunk, org_id),
            )
            for row in rows:
                digest = str(row["digest"])
                if digest in wanted:
                    found.setdefault(digest, set()).add(encoded[str(row["term"])])
        return found

    def _gram_frequencies(self, org_id: str, grams: Sequence[str]) -> dict[str, int]:
        # FTS5 maintains these exact counts during insert/delete. No stale snapshot
        # can suppress a new document, and reading a count never scans postings.
        encoded = {_token(org_id, gram): gram for gram in grams}
        result = dict.fromkeys(grams, 0)
        for chunk in _chunks(tuple(encoded)):
            for row in self._db.rows(
                f"SELECT term, doc FROM search_gram_counts WHERE term IN ({_placeholders(len(chunk))})", chunk
            ):
                result[encoded[str(row["term"])]] = int(row["doc"])
        return result

    def _gram_candidates(self, org_id: str, digests: Sequence[str], grams: Sequence[str]) -> frozenset[str]:
        if not digests or not grams:
            return frozenset()
        # Chunk the expression, not the result set. Intersect every chunk; no cap
        # may silently change long-query membership.
        candidates = set(digests)
        for chunk in _chunks(tuple(dict.fromkeys(grams))):
            expression = " AND ".join(_token(org_id, gram) for gram in chunk)
            rows = self._db.rows(
                "SELECT d.digest FROM search_grams f JOIN search_documents d ON d.id=f.rowid "
                "WHERE search_grams MATCH ? AND d.org_id=?",
                (expression, org_id),
            )
            candidates.intersection_update(str(row["digest"]) for row in rows)
            if not candidates:
                break
        return frozenset(candidates)

    def _rare_gram_candidates(
        self, org_id: str, digests: Sequence[str], grams: Sequence[str], *, count: int
    ) -> frozenset[str]:
        frequencies = self._gram_frequencies(org_id, grams)
        anchors = tuple(
            gram for gram in sorted(frequencies, key=lambda g: (frequencies[g], g)) if frequencies[gram] > 0
        )[: max(0, count)]
        return self._gram_candidates(org_id, digests, anchors)

    def _full_gram_hits(self, org_id: str, digests: Sequence[str], grams: Sequence[str]) -> dict[str, int]:
        unique = tuple(dict.fromkeys(grams))
        return dict.fromkeys(self._gram_candidates(org_id, digests, unique), len(unique))

    def _hits(
        self, org_id: str, digests: Sequence[str], table: str, column: str, needles: Sequence[str]
    ) -> dict[str, int]:
        if not digests or not needles:
            return {}
        _fts, instances = self._posting_table(table)
        found: dict[str, int] = {}
        # Each needle is counted once, including across SQL parameter batches.
        encoded = tuple(dict.fromkeys(_token(org_id, needle) for needle in needles))
        wanted = set(digests)
        for chunk in _chunks(encoded):
            rows = self._db.rows(
                f"SELECT d.digest, COUNT(*) AS hits FROM {instances} v "
                "JOIN search_documents d ON d.id=v.doc "
                f"WHERE v.term IN ({_placeholders(len(chunk))}) AND d.org_id=? GROUP BY d.digest",
                (*chunk, org_id),
            )
            for row in rows:
                digest = str(row["digest"])
                if digest in wanted:
                    found[digest] = found.get(digest, 0) + int(row["hits"])
        return found
