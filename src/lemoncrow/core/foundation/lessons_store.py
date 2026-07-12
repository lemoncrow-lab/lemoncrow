"""LessonsStore -- lesson candidates, promotions, consolidation candidates.

The review pipeline. ``lesson_promotion`` has an FK to ``lesson_candidate``;
the consolidation worker reads both in one function. Written by failure
clustering, the consolidation worker, and the lesson review CLI. Read by the
weekly report and lesson routing.

``lesson_candidate.evidence_trace_ids`` stores trace IDs as a JSON array, not
a foreign key -- it always has (traces live in HistoryStore, a different
physical file, so a real FK was never possible here); the split doesn't
change that. Callers that need the referenced traces look them up in
HistoryStore explicitly by ID.

Backed by ``lemoncrow_lessons.db``, physically separate from history,
knowledge, jobs, memory, and telemetry.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.lesson_models import LessonCandidate, LessonPromotion
from lemoncrow.core.foundation.models import ConsolidationCandidate, Playbook, to_jsonable
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS lesson_candidate (
    id                     TEXT PRIMARY KEY,
    domain                 TEXT NOT NULL,
    cluster_fingerprint    TEXT NOT NULL DEFAULT '',
    kind                   TEXT NOT NULL,
    target_id              TEXT,
    proposed_block_json    TEXT,
    proposed_rubric_check  TEXT,
    evidence_trace_ids     TEXT NOT NULL,
    body                   TEXT NOT NULL DEFAULT '',
    evidence_json          TEXT NOT NULL DEFAULT '{}',
    embedding              BLOB,
    embedding_provenance   TEXT NOT NULL DEFAULT 'legacy_stub',
    confidence             REAL NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'inbox',
    reviewer               TEXT,
    decision_at            TEXT,
    decision_reason        TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lesson_candidate_domain_status_at
    ON lesson_candidate(domain, status, created_at DESC);

CREATE TABLE IF NOT EXISTS lesson_promotion (
    id                  TEXT PRIMARY KEY,
    lesson_id           TEXT NOT NULL REFERENCES lesson_candidate(id),
    published_block_id  TEXT,
    edited_block_id     TEXT,
    pr_url              TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidation_candidate (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    affected_block_ids  TEXT NOT NULL,
    proposed_action     TEXT NOT NULL,
    proposed_body       TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decided_by          TEXT,
    decision            TEXT
);
CREATE INDEX IF NOT EXISTS ix_consolidation_candidate_pending
    ON consolidation_candidate(decided_at, created_at DESC);
"""


class LessonsStore(SqliteTableStore):
    """SQLite-backed store for the lesson review pipeline."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = ("lesson_candidate", "lesson_promotion", "consolidation_candidate")

    def __init__(self, root: Path | str, *, db_name: str = "lemoncrow_lessons.db") -> None:
        super().__init__(root, db_name=db_name)

    # ----- Lesson candidates ------------------------------------------------ #

    def upsert_lesson_candidate(self, candidate: LessonCandidate) -> None:
        proposed_block_json = (
            json.dumps(to_jsonable(candidate.proposed_block), ensure_ascii=False)
            if candidate.proposed_block is not None
            else None
        )
        embedding_json = json.dumps(candidate.embedding, ensure_ascii=False) if candidate.embedding else None
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO lesson_candidate (
                    id, domain, cluster_fingerprint, kind, target_id,
                    proposed_block_json, proposed_rubric_check, evidence_trace_ids,
                    body, evidence_json, embedding, embedding_provenance,
                    confidence, status, reviewer, decision_at,
                    decision_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    domain = excluded.domain,
                    cluster_fingerprint = excluded.cluster_fingerprint,
                    kind = excluded.kind,
                    target_id = excluded.target_id,
                    proposed_block_json = excluded.proposed_block_json,
                    proposed_rubric_check = excluded.proposed_rubric_check,
                    evidence_trace_ids = excluded.evidence_trace_ids,
                    body = excluded.body,
                    evidence_json = excluded.evidence_json,
                    embedding = excluded.embedding,
                    embedding_provenance = excluded.embedding_provenance,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    reviewer = excluded.reviewer,
                    decision_at = excluded.decision_at,
                    decision_reason = excluded.decision_reason
                """,
                (
                    candidate.id,
                    candidate.domain,
                    candidate.cluster_fingerprint,
                    candidate.kind,
                    candidate.target_id,
                    proposed_block_json,
                    candidate.proposed_rubric_check,
                    json.dumps(candidate.evidence_trace_ids, ensure_ascii=False),
                    candidate.body,
                    json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True),
                    embedding_json,
                    candidate.embedding_provenance,
                    candidate.confidence,
                    candidate.status,
                    candidate.reviewer,
                    candidate.decision_at.isoformat() if candidate.decision_at else None,
                    candidate.decision_reason,
                    candidate.created_at.isoformat(),
                ),
            )

    def get_lesson_candidate(self, lesson_id: str) -> LessonCandidate | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM lesson_candidate WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_lesson_candidate(row)

    def list_lesson_candidates(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[LessonCandidate]:
        sql = "SELECT * FROM lesson_candidate WHERE 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_lesson_candidate(r) for r in rows]

    # ----- Lesson promotions ------------------------------------------------ #

    def upsert_lesson_promotion(self, promotion: LessonPromotion) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO lesson_promotion (
                    id, lesson_id, published_block_id, edited_block_id, pr_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lesson_id = excluded.lesson_id,
                    published_block_id = excluded.published_block_id,
                    edited_block_id = excluded.edited_block_id,
                    pr_url = excluded.pr_url
                """,
                (
                    promotion.id,
                    promotion.lesson_id,
                    promotion.published_block_id,
                    promotion.edited_block_id,
                    promotion.pr_url,
                    promotion.created_at.isoformat(),
                ),
            )

    def list_lesson_promotions(self, *, limit: int = 100) -> list[LessonPromotion]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM lesson_promotion ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            LessonPromotion(
                id=r["id"],
                lesson_id=r["lesson_id"],
                published_block_id=r["published_block_id"],
                edited_block_id=r["edited_block_id"],
                pr_url=r["pr_url"] or "",
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ----- Consolidation candidates ------------------------------------------ #

    def upsert_consolidation_candidate(self, candidate: ConsolidationCandidate) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO consolidation_candidate (
                    id, kind, affected_block_ids, proposed_action, proposed_body,
                    evidence_json, created_at, decided_at, decided_by, decision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    affected_block_ids = excluded.affected_block_ids,
                    proposed_action = excluded.proposed_action,
                    proposed_body = excluded.proposed_body,
                    evidence_json = excluded.evidence_json,
                    decided_at = excluded.decided_at,
                    decided_by = excluded.decided_by,
                    decision = excluded.decision
                """,
                (
                    candidate.id,
                    candidate.kind,
                    json.dumps(candidate.affected_block_ids, ensure_ascii=False),
                    candidate.proposed_action,
                    candidate.proposed_body,
                    json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True),
                    candidate.created_at.isoformat(),
                    candidate.decided_at.isoformat() if candidate.decided_at else None,
                    candidate.decided_by,
                    candidate.decision,
                ),
            )

    def list_consolidation_candidates(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> list[ConsolidationCandidate]:
        sql = "SELECT * FROM consolidation_candidate"
        if pending_only:
            sql += " WHERE decided_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._transaction() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_consolidation_candidate(row) for row in rows]

    def get_consolidation_candidate(self, candidate_id: str) -> ConsolidationCandidate | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM consolidation_candidate WHERE id = ?", (candidate_id,)).fetchone()
        return self._row_to_consolidation_candidate(row) if row is not None else None

    # ----- Deserialization ---------------------------------------------------- #

    def _row_to_consolidation_candidate(self, row: sqlite3.Row) -> ConsolidationCandidate:
        return ConsolidationCandidate(
            id=row["id"],
            kind=row["kind"],
            affected_block_ids=json.loads(row["affected_block_ids"] or "[]"),
            proposed_action=row["proposed_action"],
            proposed_body=row["proposed_body"],
            evidence=json.loads(row["evidence_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            decided_by=row["decided_by"],
            decision=row["decision"],
        )

    def _row_to_lesson_candidate(self, row: sqlite3.Row) -> LessonCandidate:
        row_keys = set(row.keys())
        proposed_block = None
        if row["proposed_block_json"]:
            proposed_block = Playbook.model_validate_json(row["proposed_block_json"])
        embedding = None
        if row["embedding"]:
            raw_embedding = row["embedding"]
            if isinstance(raw_embedding, bytes):
                raw_embedding = raw_embedding.decode("utf-8", errors="replace")
            embedding = json.loads(raw_embedding)
        decision_at = datetime.fromisoformat(row["decision_at"]) if row["decision_at"] else None
        return LessonCandidate(
            id=row["id"],
            domain=row["domain"],
            cluster_fingerprint=row["cluster_fingerprint"] or "",
            kind=row["kind"],
            target_id=row["target_id"],
            proposed_block=proposed_block,
            proposed_rubric_check=row["proposed_rubric_check"],
            evidence_trace_ids=json.loads(row["evidence_trace_ids"]),
            body=row["body"] if "body" in row_keys else "",
            evidence=(json.loads(row["evidence_json"] or "{}") if "evidence_json" in row_keys else {}),
            embedding=embedding,
            embedding_provenance=(row["embedding_provenance"] if "embedding_provenance" in row_keys else "legacy_stub"),
            confidence=float(row["confidence"]),
            status=row["status"],
            reviewer=row["reviewer"],
            decision_at=decision_at,
            decision_reason=row["decision_reason"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
        )


__all__ = ["LessonsStore"]
