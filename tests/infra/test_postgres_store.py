"""Tests for PostgresStore — skipped when ATELIER_DATABASE_URL is not set.

These tests require a real Postgres instance with the psycopg package
installed.  Set ATELIER_DATABASE_URL to enable them.

Example:
    ATELIER_DATABASE_URL=postgresql://atelier:atelier@localhost:5432/atelier_test
    uv run pytest tests/test_postgres_store.py -v
"""

from __future__ import annotations

import os

import pytest

ATELIER_DATABASE_URL = os.environ.get("ATELIER_DATABASE_URL", "")
psycopg_available = False
try:
    import psycopg  # noqa: F401

    psycopg_available = True
except ImportError:
    pass

SKIP_REASON = (
    "PostgresStore tests require ATELIER_DATABASE_URL env var and psycopg installed. "
    "Set ATELIER_DATABASE_URL=postgresql://... to run."
)

skip_postgres = pytest.mark.skipif(
    not (ATELIER_DATABASE_URL and psycopg_available),
    reason=SKIP_REASON,
)


# --------------------------------------------------------------------------- #
# Import-time safety (always run)                                            #
# --------------------------------------------------------------------------- #


def test_postgres_store_importable() -> None:
    """Importing postgres_store must not raise even without psycopg."""
    from atelier.infra.storage.postgres_store import SCHEMA_DDL, PostgresStore

    assert PostgresStore is not None
    assert isinstance(SCHEMA_DDL, str)


def test_postgres_store_no_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgresStore.__init__ raises RuntimeError when psycopg is unavailable."""
    import atelier.infra.storage.postgres_store as pg_mod

    original = pg_mod._psycopg
    try:
        pg_mod._psycopg = None
        with pytest.raises(RuntimeError, match="psycopg"):
            pg_mod.PostgresStore(database_url="postgresql://localhost/test")
    finally:
        pg_mod._psycopg = original


# --------------------------------------------------------------------------- #
# Live Postgres tests                                                        #
# --------------------------------------------------------------------------- #


@skip_postgres
def test_postgres_store_init() -> None:
    """PostgresStore.init() must create all 15 tables without error."""
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()  # must not raise


@skip_postgres
def test_postgres_store_health_check() -> None:
    """health_check returns ok=True after init."""
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()
    result = store.health_check()
    assert result["ok"] is True
    assert result["backend"] == "postgres"


@skip_postgres
def test_postgres_store_round_trip_block() -> None:
    """upsert_block / get_block round-trip must preserve all fields."""
    from atelier.core.foundation.models import ReasonBlock
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()

    block = ReasonBlock(
        id="pg-test-block",
        title="Postgres Test Block",
        domain="test",
        situation="Testing round-trip persistence",
        triggers=["postgres", "test"],
        procedure=["Step 1", "Step 2"],
    )
    store.upsert_block(block, write_markdown=False)
    fetched = store.get_block("pg-test-block")

    assert fetched is not None
    assert fetched.id == "pg-test-block"
    assert fetched.title == "Postgres Test Block"
    assert fetched.domain == "test"
    assert "postgres" in fetched.triggers


@skip_postgres
def test_postgres_store_list_blocks_by_domain() -> None:
    """list_blocks(domain=...) must return only matching blocks."""
    from atelier.core.foundation.models import ReasonBlock
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()

    block_a = ReasonBlock(
        id="pg-domain-a",
        title="Domain A Block",
        domain="alpha",
        situation="Alpha domain block",
        procedure=["Do alpha"],
    )
    block_b = ReasonBlock(
        id="pg-domain-b",
        title="Domain B Block",
        domain="beta",
        situation="Beta domain block",
        procedure=["Do beta"],
    )
    store.upsert_block(block_a, write_markdown=False)
    store.upsert_block(block_b, write_markdown=False)

    alpha_blocks = store.list_blocks(domain="alpha")
    alpha_ids = {b.id for b in alpha_blocks}
    assert "pg-domain-a" in alpha_ids
    assert "pg-domain-b" not in alpha_ids


@skip_postgres
def test_postgres_store_update_block_status() -> None:
    """update_block_status must return True and persist the change."""
    from atelier.core.foundation.models import ReasonBlock
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()

    block = ReasonBlock(
        id="pg-status-test",
        title="Status Test",
        domain="test",
        situation="Testing status update",
        procedure=["Step 1"],
    )
    store.upsert_block(block, write_markdown=False)
    changed = store.update_block_status("pg-status-test", "deprecated")
    assert changed is True

    fetched = store.get_block("pg-status-test")
    assert fetched is not None
    assert fetched.status == "deprecated"


@skip_postgres
def test_postgres_store_record_and_get_trace() -> None:
    """record_trace / get_trace round-trip must work."""
    from atelier.core.foundation.models import Trace
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()

    trace = Trace(
        id="pg-trace-001",
        agent="test-agent",
        domain="test",
        task="Verify Postgres trace round-trip",
        status="success",
    )
    store.record_trace(trace, write_json=False)
    fetched = store.get_trace("pg-trace-001")

    assert fetched is not None
    assert fetched.id == "pg-trace-001"
    assert fetched.agent == "test-agent"
    assert fetched.status == "success"


@skip_postgres
def test_postgres_store_upsert_run_ledger() -> None:
    """upsert_run_ledger / get_run_ledger round-trip must work."""
    from atelier.infra.storage.postgres_store import PostgresStore

    store = PostgresStore(database_url=ATELIER_DATABASE_URL)
    store.init()

    store.upsert_run_ledger(
        "run-pg-001",
        task="Ledger test",
        state={"step": 1, "ok": True},
        domain="test",
    )
    result = store.get_run_ledger("run-pg-001")

    assert result is not None
    assert result["session_id"] == "run-pg-001"
    assert result["state"]["step"] == 1
    assert result["state"]["ok"] is True
