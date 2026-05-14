---
id: WP-02
title: Implement foundational V2 Pydantic models + DDL
phase: A
pillar: 1, 2, 3
owner_agent: atelier:code
depends_on: [WP-01]
status: done
---

# WP-02 — Data model

## Why
Every other Phase B/C/D/F packet imports from these models. They must be merged first, with full
test coverage, so downstream subagents can rely on stable signatures and do not invent schema fields
inside implementation packets.

## Files touched (new unless marked)

- `src/atelier/core/foundation/memory_models.py` — new
- `src/atelier/core/foundation/lesson_models.py` — new
- `src/atelier/core/foundation/savings_models.py` — new
- `src/atelier/core/foundation/routing_models.py` — new
- `src/atelier/infra/storage/migrations/__init__.py` — new (registers migrations)
- `src/atelier/infra/storage/migrations/v2_001_memory.sql` — new
- `src/atelier/infra/storage/migrations/v2_002_lessons.sql` — new
- `src/atelier/infra/storage/migrations/v2_003_context_budget.sql` — new
- `src/atelier/infra/storage/migrations/v2_004_routing.sql` — new
- `src/atelier/infra/storage/migrations/v2_005_postgres_pgvector.sql` — new
- `src/atelier/infra/storage/ids.py` — new (uuid7 helper)
- `src/atelier/infra/storage/sqlite_store.py` — edit to call new migrations
- `src/atelier/infra/storage/postgres_store.py` — edit to call new migrations + pgvector guard
- `tests/core/test_memory_models.py` — new
- `tests/core/test_lesson_models.py` — new
- `tests/core/test_savings_models.py` — new
- `tests/core/test_routing_models.py` — new
- `tests/infra/test_v2_migrations_sqlite.py` — new

## How to execute

1. Read [IMPLEMENTATION_PLAN_V2_DATA_MODEL.md](../IMPLEMENTATION_PLAN_V2_DATA_MODEL.md) sections 2,
   3, 4, 5, and 6 *verbatim*. Do not invent fields outside that document.
2. Read `src/atelier/core/foundation/models.py` to copy the existing patterns:
   - `model_config = ConfigDict(extra="forbid")`
   - `_utcnow()` helper imported from the same module
   - All datetimes UTC, all IDs strings
3. Add `make_uuid7()` to `infra/storage/ids.py` using stdlib only — no new deps.
   Spec: 48-bit unix-ms timestamp prefix + 80 random bits, hex-encoded with `-` separators.
4. Write models exactly as documented. Add tests that:
   - reject extra fields
   - validate that `ArchivalPassage.dedup_hash` is non-empty
   - validate that `LessonCandidate.kind == "edit_block"` requires `target_id`
   - validate that `MemoryBlock.value` length ≤ `limit_chars`
   - validate `RouteDecision.confidence` is in `[0, 1]`
   - validate `VerificationEnvelope` stores observed validation results without executing commands
5. Wire migrations into `sqlite_store.py` and `postgres_store.py` such that `init` applies them
   idempotently and `verify` ensures all tables exist.
6. Run `make verify` (lint + typecheck + tests). Must be green.

## Acceptance tests

```bash
# Models import and instantiate cleanly
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run python -c "
from atelier.core.foundation.memory_models import MemoryBlock, ArchivalPassage, MemoryRecall, RunMemoryFrame
from atelier.core.foundation.lesson_models import LessonCandidate, LessonPromotion
from atelier.core.foundation.savings_models import ContextBudget
from atelier.core.foundation.routing_models import AgentRequest, RouteDecision, VerificationEnvelope
from atelier.infra.storage.ids import make_uuid7
b = MemoryBlock(id=make_uuid7(), agent_id='atelier:code', label='persona', value='...')
print('OK', b.id)
"

# All migrations apply idempotently
LOCAL=1 uv run pytest tests/infra/test_v2_migrations_sqlite.py -v

# All new unit tests pass
LOCAL=1 uv run pytest tests/core/test_memory_models.py \
                     tests/core/test_lesson_models.py \
                     tests/core/test_savings_models.py \
                     tests/core/test_routing_models.py -v

# No regression
make verify
```

## Definition of done
- [ ] All new files exist
- [ ] `extra="forbid"` enforced on every new model
- [ ] uuid7 helper used for every new `id` field
- [ ] Routing models and routing migration are implemented from the data-model doc
- [ ] Both SQLite and Postgres backends pass migration tests
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
