"""V2 storage migration registry."""

from __future__ import annotations

from importlib import resources

SQLITE_MIGRATIONS = (
    "v2_001_memory.sql",
    "v2_002_lessons.sql",
    "v2_003_context_budget.sql",
    "v2_004_routing.sql",
)
POSTGRES_VECTOR_MIGRATION = "v2_005_postgres_pgvector.sql"
V2_REQUIRED_TABLES = (
    "memory_block",
    "memory_block_history",
    "archival_passage",
    "archival_passage_fts",
    "memory_recall",
    "run_memory_frame",
    "lesson_candidate",
    "lesson_promotion",
    "context_budget",
    "route_decision",
    "verification_envelope",
)


def read_migration(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def sqlite_migration_scripts() -> list[str]:
    return [read_migration(name) for name in SQLITE_MIGRATIONS]


def postgres_migration_scripts() -> list[str]:
    scripts: list[str] = []
    for name in SQLITE_MIGRATIONS:
        sql = read_migration(name).replace("BLOB", "BYTEA")
        sql = _replace_sqlite_fts_with_postgres_table(sql)
        scripts.append(sql)
    return scripts


def postgres_vector_script(*, dim: int) -> str:
    return read_migration(POSTGRES_VECTOR_MIGRATION).format(dim=dim)


def _replace_sqlite_fts_with_postgres_table(sql: str) -> str:
    start = sql.find("CREATE VIRTUAL TABLE IF NOT EXISTS archival_passage_fts")
    if start == -1:
        return sql
    end = sql.find(");", start)
    if end == -1:
        return sql
    replacement = """CREATE TABLE IF NOT EXISTS archival_passage_fts (
  text TEXT,
  tags TEXT
);"""
    return sql[:start] + replacement + sql[end + 2 :]


__all__ = [
    "POSTGRES_VECTOR_MIGRATION",
    "SQLITE_MIGRATIONS",
    "V2_REQUIRED_TABLES",
    "postgres_migration_scripts",
    "postgres_vector_script",
    "read_migration",
    "sqlite_migration_scripts",
]
