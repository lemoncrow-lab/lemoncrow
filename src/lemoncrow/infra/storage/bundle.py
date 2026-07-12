"""StoreBundle -- the six physically-split stores, held on one object.

Each attribute is a store scoped to one concern and one SQLite file (see the
module docstrings on ``lemoncrow.core.foundation.{history,knowledge,lessons,
jobs,telemetry}_store`` and ``infra.storage.sqlite_memory_store``). Call
sites reach the store they need explicitly -- ``store.history.record_trace(...)``,
``store.knowledge.upsert_block(...)``, ``store.jobs.enqueue_job(...)`` -- so a
code path that only touches jobs and history (e.g. ``servicectl tick``) only
ever opens those two files, never contending with knowledge reads or lesson
review writes on their own files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class StoreBundle:
    """Container for the six per-concern stores backing one LemonCrow root."""

    history: Any
    knowledge: Any
    lessons: Any
    jobs: Any
    memory: Any
    telemetry: Any

    @property
    def db_path(self) -> Path:
        """Marker path callers use for the "has `lc init` run here" check.

        Playbooks/rubrics are the baseline data every ``lc init`` seeds, so
        the knowledge store's file is the representative existence check.
        """
        return Path(self.knowledge.db_path)

    @property
    def root(self) -> Path:
        """Shared store root. All six sub-stores resolve to the same root dir."""
        return Path(self.knowledge.root)

    def init(self) -> None:
        """Initialise every store's schema/directories. Idempotent."""
        self.history.init()
        self.knowledge.init()
        self.lessons.init()
        self.jobs.init()
        self.memory.init()
        self.telemetry.init()

    def health_check(self) -> dict[str, Any]:
        """Aggregate health across every store; ``ok`` is the AND of all six."""
        checks = {
            "history": self.history.health_check(),
            "knowledge": self.knowledge.health_check(),
            "lessons": self.lessons.health_check(),
            "jobs": self.jobs.health_check(),
            "memory": self.memory.health_check(),
            "telemetry": self.telemetry.health_check(),
        }
        knowledge = checks["knowledge"]
        return {
            "ok": all(c.get("ok") for c in checks.values()),
            "backend": knowledge.get("backend", "sqlite"),
            "db_path": str(self.db_path),
            "block_count": knowledge.get("block_count", 0),
            "stores": checks,
        }

    def prune_jobs(self, *, older_than_days: int = 14) -> int:
        return int(self.jobs.prune_jobs(older_than_days=older_than_days))


def build_sqlite_store_bundle(
    root: Path | str,
    lessons_root: Path | str | None = None,
) -> StoreBundle:
    """Construct a StoreBundle backed by six separate SQLite files under *root*."""
    from lemoncrow.core.foundation.history_store import HistoryStore
    from lemoncrow.core.foundation.jobs_store import JobsStore
    from lemoncrow.core.foundation.knowledge_store import KnowledgeStore
    from lemoncrow.core.foundation.lessons_store import LessonsStore
    from lemoncrow.core.foundation.memory_tables_store import MemoryTablesStore
    from lemoncrow.core.foundation.telemetry_store import TelemetryStore

    return StoreBundle(
        history=HistoryStore(root),
        knowledge=KnowledgeStore(root, lessons_root),
        lessons=LessonsStore(root),
        jobs=JobsStore(root),
        memory=MemoryTablesStore(root),
        telemetry=TelemetryStore(root),
    )


__all__ = ["StoreBundle", "build_sqlite_store_bundle"]
