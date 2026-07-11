#!/usr/bin/env bash
# verify_lemoncrow_postgres.sh — Postgres/pgvector smoke test (skip if no URL set)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${LEMONCROW_DATABASE_URL:-}" ]; then
    echo "SKIPPED: LEMONCROW_DATABASE_URL not set"
    echo "Set LEMONCROW_DATABASE_URL=postgresql://user:pass@host/db to run"
    exit 0
fi

python3 -c "import psycopg" 2>/dev/null || {
    echo "SKIPPED: psycopg not installed"
    echo "Install with: uv sync --extra postgres"
    exit 0
}

echo "=== LemonCrow Postgres verification ==="
export LEMONCROW_STORAGE_BACKEND=postgres

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT" 2>/dev/null || true' EXIT

echo "--- PostgresStore connectivity ---"
uv run python3 - <<PYEOF
import os
from lemoncrow.infra.storage.postgres_store import PostgresStore

url = os.environ["LEMONCROW_DATABASE_URL"]
store = PostgresStore(database_url=url)
store.init_schema()
print(f"PASS: connected and schema initialised ({url})")
PYEOF

echo "--- enqueue and claim job ---"
uv run python3 - <<PYEOF
import os
from lemoncrow.infra.storage.postgres_store import PostgresStore
from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS

url = os.environ["LEMONCROW_DATABASE_URL"]
store = PostgresStore(database_url=url)
store.init_schema()

jid = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"test": True})
print(f"enqueued job: {jid}")

job = store.claim_job()
assert job is not None, "Expected to claim a job"
assert job.id == jid
store.complete_job(job.id)
print(f"PASS: job enqueue/claim/complete cycle succeeded")
PYEOF

echo "--- pgvector extension (optional) ---"
uv run python3 - <<PYEOF || echo "WARN: pgvector extension not available (non-fatal)"
import os
import psycopg
url = os.environ["LEMONCROW_DATABASE_URL"]
with psycopg.connect(url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'")
        row = cur.fetchone()
        if row:
            print("PASS: pgvector extension available")
        else:
            print("WARN: pgvector extension not installed in this Postgres instance")
PYEOF

echo "=== PASS: Postgres checks passed ==="
