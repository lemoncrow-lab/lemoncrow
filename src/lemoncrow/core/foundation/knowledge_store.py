"""KnowledgeStore -- playbooks and rubrics.

Read-heavy, slow-evolving. Both tables are read together on every
``get_context()`` call and every ``RuntimeSession.inject_context``; rubrics
seed alongside playbooks on init. Written by the consolidation worker,
optimization, the admin CLI, and seed data.

Also owns the filesystem mirror (Markdown blocks / YAML rubrics under
``<lessons_root>/blocks`` and ``<lessons_root>/rubrics``) so blocks/rubrics
can be reviewed in PRs without running tools.

Backed by ``lemoncrow_knowledge.db``, physically separate from history,
lessons, jobs, memory, and telemetry.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lemoncrow.core.foundation.models import Playbook, PlaybookStatus, Rubric, to_jsonable
from lemoncrow.core.foundation.paths import resolve_lessons_root, resolve_workspace_store_dir
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    usage_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playbooks_domain ON playbooks(domain);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status);

CREATE VIRTUAL TABLE IF NOT EXISTS playbooks_fts USING fts5(
    id UNINDEXED,
    title,
    triggers,
    situation,
    dead_ends,
    procedure,
    failure_signals,
    tokenize = 'porter'
);

CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class KnowledgeStore(SqliteTableStore):
    """SQLite-backed store for playbooks and rubrics, plus their file mirrors."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = ("playbooks", "playbooks_fts", "rubrics")

    def __init__(
        self,
        root: Path | str,
        lessons_root: Path | str | None = None,
        *,
        db_name: str = "lemoncrow_knowledge.db",
    ) -> None:
        super().__init__(root, db_name=db_name)

        # Blocks/rubrics default to the workspace-scoped runtime mirror under
        # the global store (resolve_workspace_store_dir) -- kept isolated per
        # project, not Git-tracked. An explicit lessons_root (constructor arg
        # or LEMONCROW_LESSONS_ROOT) opts into the Git-tracked
        # .lemoncrow/lessons location instead (see resolve_lessons_root()).
        if lessons_root is not None or os.environ.get("LEMONCROW_LESSONS_ROOT", "").strip():
            _k_root = resolve_lessons_root(self.root, lessons_root)
        else:
            _k_root = resolve_workspace_store_dir(self.root)
        self.blocks_dir = _k_root / "blocks"
        self.rubrics_dir = _k_root / "rubrics"

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self.rubrics_dir.mkdir(parents=True, exist_ok=True)
        super().init()
        # Seed packaged rubrics and sync user lessons outside the schema
        # transaction so each upsert gets its own committed transaction.
        self._seed_packaged_rubrics()
        self.sync_lessons()

    def health_check(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM playbooks").fetchone()
                block_count = count["n"] if count else 0
            return {
                "ok": True,
                "backend": "sqlite",
                "db_path": str(self.db_path),
                "block_count": block_count,
            }
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            return {"ok": False, "backend": "sqlite", "error": str(exc)}

    # ----- Playbooks -------------------------------------------------------- #

    def upsert_block(self, block: Playbook, *, write_markdown: bool = True) -> None:
        payload = json.dumps(to_jsonable(block), ensure_ascii=False)
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO playbooks (
                    id, title, domain, status,
                    usage_count, success_count, failure_count,
                    created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    domain=excluded.domain,
                    status=excluded.status,
                    usage_count=excluded.usage_count,
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    block.id,
                    block.title,
                    block.domain,
                    block.status,
                    block.usage_count,
                    block.success_count,
                    block.failure_count,
                    block.created_at.isoformat(),
                    block.updated_at.isoformat(),
                    payload,
                ),
            )
            cur.execute("DELETE FROM playbooks_fts WHERE id = ?", (block.id,))
            cur.execute(
                """
                INSERT INTO playbooks_fts (
                    id, title, triggers, situation, dead_ends, procedure, failure_signals
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block.id,
                    block.title,
                    " ; ".join(block.triggers),
                    block.situation,
                    " ; ".join(block.dead_ends),
                    " ; ".join(block.procedure),
                    " ; ".join(block.failure_signals),
                ),
            )
        if write_markdown:
            self._write_block_markdown(block)

    def get_block(self, block_id: str) -> Playbook | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM playbooks WHERE id = ?", (block_id,)).fetchone()
        if row is None:
            return None
        return Playbook.model_validate_json(row["payload"])

    def list_blocks(
        self,
        *,
        domain: str | None = None,
        status: PlaybookStatus | None = "active",
        include_deprecated: bool = False,
    ) -> list[Playbook]:
        sql = "SELECT payload FROM playbooks WHERE 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if status and not include_deprecated:
            sql += " AND status = ?"
            params.append(status)
        elif not include_deprecated:
            sql += " AND status != 'quarantined'"
        sql += " ORDER BY updated_at DESC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Playbook.model_validate_json(r["payload"]) for r in rows]

    def search_blocks(self, query: str, *, limit: int = 20) -> list[Playbook]:
        if not query.strip():
            return self.list_blocks()[:limit]
        fts_query = self._build_fts_prefix_query(query)
        sql = (
            "SELECT r.payload FROM playbooks_fts f "
            "JOIN playbooks r ON r.id = f.id "
            "WHERE playbooks_fts MATCH ? "
            "AND r.status != 'quarantined' "
            "ORDER BY rank LIMIT ?"
        )
        with self._transaction() as conn:
            rows = conn.execute(sql, (fts_query, limit)).fetchall()
        return [Playbook.model_validate_json(r["payload"]) for r in rows]

    def update_block_status(self, block_id: str, status: PlaybookStatus) -> bool:
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE playbooks SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.now(UTC).isoformat(), block_id),
            )
            changed = cur.rowcount > 0
        if changed:
            block = self.get_block(block_id)
            if block:
                self._write_block_markdown(block)
        return changed

    def delete_block(self, block_id: str) -> bool:
        """Hard-delete a Playbook from the DB, FTS index, and markdown."""
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM playbooks WHERE id = ?", (block_id,))
            deleted = cur.rowcount > 0
            cur.execute("DELETE FROM playbooks_fts WHERE id = ?", (block_id,))
        markdown = self.blocks_dir / f"{block_id}.md"
        if markdown.exists():
            markdown.unlink()
        return deleted

    def increment_usage(
        self,
        block_id: str,
        *,
        success: bool | None = None,
    ) -> None:
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE playbooks SET usage_count = usage_count + 1 WHERE id = ?",
                (block_id,),
            )
            if success is True:
                cur.execute(
                    "UPDATE playbooks SET success_count = success_count + 1 WHERE id = ?",
                    (block_id,),
                )
            elif success is False:
                cur.execute(
                    "UPDATE playbooks SET failure_count = failure_count + 1 WHERE id = ?",
                    (block_id,),
                )

    def import_blocks(self, blocks: Iterable[Playbook]) -> int:
        n = 0
        for b in blocks:
            self.upsert_block(b)
            n += 1
        return n

    # ----- Rubrics --------------------------------------------------------- #

    def upsert_rubric(self, rubric: Rubric, *, write_yaml: bool = True) -> None:
        payload = json.dumps(to_jsonable(rubric), ensure_ascii=False)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO rubrics (id, domain, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    domain = excluded.domain,
                    payload = excluded.payload
                """,
                (rubric.id, rubric.domain, payload),
            )
        if write_yaml:
            self._write_rubric_yaml(rubric)

    def get_rubric(self, rubric_id: str) -> Rubric | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
        if row is None:
            return None
        return Rubric.model_validate_json(row["payload"])

    def list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        sql = "SELECT payload FROM rubrics"
        params: list[Any] = []
        if domain:
            sql += " WHERE domain = ?"
            params.append(domain)
        sql += " ORDER BY id"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Rubric.model_validate_json(r["payload"]) for r in rows]

    def import_rubrics(self, rubrics: Iterable[Rubric]) -> int:
        n = 0
        for r in rubrics:
            self.upsert_rubric(r)
            n += 1
        return n

    # ----- Filesystem sync --------------------------------------------------- #

    def sync_lessons(self) -> dict[str, int]:
        """Sync blocks and rubrics from the filesystem to the database.

        Uses a file-mtime manifest stored alongside the SQLite DB so that
        unchanged files are skipped on subsequent calls -- safe to call
        repeatedly.
        """
        results = {"blocks": 0, "rubrics": 0}

        if self.blocks_dir.exists():
            from lemoncrow.core.foundation.parser import parse_block_markdown

            prev = self._load_sync_manifest("blocks")
            fresh: dict[str, int] = {}

            for path in sorted(self.blocks_dir.rglob("*.md")):
                key = str(path)
                mtime = path.stat().st_mtime_ns
                fresh[key] = mtime
                if prev.get(key) == mtime:
                    continue  # unchanged -- skip read/parse/upsert

                try:
                    content = path.read_text(encoding="utf-8")
                    block = parse_block_markdown(content)
                    self.upsert_block(block, write_markdown=False)
                    results["blocks"] += 1
                except Exception as exc:
                    logging.exception("Recovered from broad exception handler")
                    logger.warning("failed to sync lessons block from %s: %s", path, exc)
                    continue

            self._save_sync_manifest("blocks", fresh)

        if self.rubrics_dir.exists():
            rubric_paths = sorted(self.rubrics_dir.rglob("*.yaml")) + sorted(self.rubrics_dir.rglob("*.yml"))
            prev = self._load_sync_manifest("rubrics")
            fresh_rubrics: dict[str, int] = {}

            for path in rubric_paths:
                key = str(path)
                mtime = path.stat().st_mtime_ns
                fresh_rubrics[key] = mtime
                if prev.get(key) == mtime:
                    continue  # unchanged

                try:
                    content = path.read_text(encoding="utf-8")
                    data = yaml.safe_load(content) or {}
                    rubric = Rubric.model_validate(data)
                    self.upsert_rubric(rubric, write_yaml=False)
                    results["rubrics"] += 1
                except Exception as exc:
                    logging.exception("Recovered from broad exception handler")
                    logger.warning("failed to sync lessons rubric from %s: %s", path, exc)
                    continue

            self._save_sync_manifest("rubrics", fresh_rubrics)

        return results

    def _seed_packaged_rubrics(self) -> None:
        """Upsert rubrics shipped with the package into the DB.

        Called once per ``init()`` before ``sync_lessons()`` so packaged
        rubrics are always available. User-managed rubrics (synced from
        ``self.rubrics_dir``) are written afterward and will override any
        packaged rubric with the same ``id``.
        """
        try:
            from lemoncrow.core.foundation.rubric_gate import load_packaged_rubrics

            for rubric in load_packaged_rubrics():
                self.upsert_rubric(rubric, write_yaml=False)
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            logger.warning("failed to seed packaged rubrics: %s", exc)

    def _sync_manifest_path(self, kind: str) -> Path:
        """Return path to the incremental-sync manifest for *kind*."""
        return self.root / f".lessons_sync_{kind}.json"

    def _load_sync_manifest(self, kind: str) -> dict[str, int]:
        path = self._sync_manifest_path(kind)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return {k: int(v) for k, v in raw.items() if isinstance(v, int)}
            except Exception:
                logging.exception("Recovered from broad exception handler")
                return {}
        return {}

    def _save_sync_manifest(self, kind: str, manifest: dict[str, int]) -> None:
        path = self._sync_manifest_path(kind)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # ----- File mirrors ---------------------------------------------------- #

    def _write_block_markdown(self, block: Playbook) -> None:
        path = self.blocks_dir / f"{block.id}.md"
        from lemoncrow.core.foundation.renderer import render_playbook_markdown as render_block_markdown

        path.write_text(render_block_markdown(block), encoding="utf-8")

    def _write_rubric_yaml(self, rubric: Rubric) -> None:
        path = self.rubrics_dir / f"{rubric.id}.yaml"
        path.write_text(
            yaml.safe_dump(to_jsonable(rubric), sort_keys=False),
            encoding="utf-8",
        )


__all__ = ["KnowledgeStore"]
