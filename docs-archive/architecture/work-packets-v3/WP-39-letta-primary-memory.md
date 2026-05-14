---
id: WP-39
title: Letta as primary memory backend behind the existing `memory_*` MCP tools (no more dual-write); ship self-hosted Docker setup
phase: I
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-35]
supersedes: []
status: done
---

# WP-39 — Letta primary memory + self-hosted Docker

## Why

WP-35 cleaned up the dual-write contradiction by deciding "single primary, picked by config".
WP-39 actually _implements_ the Letta-primary path so users who configure
`[memory].backend = "letta"` get a real Letta-backed store underneath the **existing**
`memory_*` MCP tools (`memory_upsert_block`, `memory_get_block`, `memory_recall`,
`memory_archive`, etc.).

The host CLI calling those MCP tools sees no difference. The store underneath changes from
"SQLite + dual-writing to Letta" to "Letta only" (or "SQLite only", per WP-35's config knob).

V3 also commits to **self-hosted Letta as the supported deployment**. Atelier ships a
`docker-compose.yml`, an `atelier letta` CLI subcommand for lifecycle management, and a
runbook. We do not depend on Letta Cloud or any hosted service.

Boundary: we do not write a vector store, a passage indexer, an embedding pipeline, or any
agent loop. Atelier maps its `MemoryBlock` / `ArchivalPassage` to Letta's `Block` / `Passage`
and applies its hybrid ranking _policy_ on top of Letta's results. **No LLM is called by
Atelier.** (Letta itself, running in its own Docker container, may call models the user has
configured for it — that's the user's choice and bill, not Atelier's.)

## Files touched

### Letta-primary implementation

- **EDIT:** `src/atelier/infra/memory_bridges/letta_adapter.py` — make every memory operation
  go through Letta when `[memory].backend = "letta"`:
  - `upsert_block` → Letta `client.agents.blocks.modify`
  - `get_block` → Letta `client.agents.blocks.retrieve`
  - `list_pinned` → Letta `client.agents.blocks.list` filtered by tag
  - `insert_passage` → Letta `client.agents.archival.insert`
  - `search_passages` → Letta `client.agents.archival.search`, post-ranked with the existing
    `0.6 * cosine + 0.4 * bm25_norm` policy
  - `record_recall` → Letta passage metadata update + Atelier-side `MemoryRecall` row (kept
    locally for trace continuity)
- **EDIT:** `src/atelier/infra/memory_bridges/letta_adapter.py` — implement metadata
  carry-overs from data-model § 4 (`atelier_session_id`, `atelier_last_recall_at`,
  `atelier_dedup_hash`).
- **EDIT:** `src/atelier/infra/storage/factory.py` — when `backend=letta`, instantiate
  `LettaMemoryStore` from `letta_adapter`; do not also instantiate `SqliteMemoryStore`.

### Self-hosted Docker

- **NEW:** `deploy/letta/docker-compose.yml` — Letta server with persistent volume:
  ```yaml
  services:
    letta:
      image: letta/letta:latest
      ports:
        - "$&#123;ATELIER_LETTA_PORT:-8283&#125;:8283"
      environment:
        - LETTA_PG_URI=$&#123;LETTA_PG_URI:-&#125; # blank => Letta uses its bundled SQLite
        - OPENAI_API_KEY=$&#123;OPENAI_API_KEY:-&#125; # optional; for Letta's internal calls
      volumes:
        - letta_data:/var/letta
      restart: unless-stopped
      healthcheck:
        test: ["CMD-SHELL", "curl -f http://localhost:8283/v1/health || exit 1"]
        interval: 10s
        timeout: 3s
        retries: 5
  volumes:
    letta_data:
  ```
- **NEW:** `deploy/letta/.env.example` — documented env vars (Letta port, Postgres URI if
  the user wants Postgres instead of bundled SQLite, optional API keys for Letta's own LLM
  features).
- **NEW:** `src/atelier/cli/letta.py` — `atelier letta` CLI subcommand group:
  - `atelier letta up` — `docker compose -f deploy/letta/docker-compose.yml up -d`
  - `atelier letta down` — stops and removes the container (preserves the volume)
  - `atelier letta logs [-f]`
  - `atelier letta status` — calls Letta's `/v1/health` and prints status + version
  - `atelier letta reset` — DANGER: removes the volume; requires `--yes` flag
    All commands are thin wrappers; Atelier does not reimplement docker-compose.
- **EDIT:** `pyproject.toml` — register `atelier letta` subcommand entry-point.

### Tests + docs

- **NEW:** `tests/infra/test_letta_primary_round_trip.py` — with a mocked Letta sidecar
  (via `respx`):
  - upsert_block → get_block → list_pinned: round-trips through Letta.
  - insert_passage → search_passages: returns Letta results ranked by Atelier policy.
  - record_recall: writes Atelier-side row AND Letta metadata.
- **NEW:** `tests/infra/test_letta_metadata_carryover.py` — assert Atelier metadata fields
  appear in Letta block/passage metadata.
- **NEW:** `tests/gateway/test_atelier_letta_cli.py` — smoke-test the CLI subcommand stubs
  (mocks `subprocess.run` for the docker calls; asserts the right compose command is built).
- **EDIT:** `tests/infra/test_letta_adapter_fallback.py` — finish the V3 contract: explicit
  `MemorySidecarUnavailable` on Letta down (no silent fallback).
- **NEW:** `docs/internal/engineering/runbooks/letta-self-hosted.md` — full runbook:
  - prerequisites (Docker, Docker Compose v2)
  - one-shell-command bring-up: `atelier letta up`
  - verifying: `atelier letta status` + manual `curl http://localhost:8283/v1/health`
  - configuring Atelier to talk to it: `[memory].backend = "letta"` +
    `ATELIER_LETTA_URL=http://localhost:8283`
  - upgrading Letta image
  - backup / restore of the persistent volume
  - resource expectations (memory, disk, optional Postgres for production scale)
  - what Letta needs in terms of LLM API keys (Letta's internal sleeptime / agent features
    — out of Atelier's scope)

## How to execute

1. **Read `letta-client` README and the V2 `letta_adapter.py`.** WP-35 left a clean slate
   (no dual-write); this packet fills in the real Letta-side calls.

2. **Map types carefully.** Letta `Block.metadata` is a dict; pack the Atelier-specific keys
   under a stable `atelier_*` namespace prefix so we can round-trip without collision.

3. **Apply the ranking policy on the result list, not at retrieval time.** Letta returns a
   ranked list; Atelier may re-rank using the cosine score from Letta's response (if
   exposed) plus its own BM25 over the passage text. If Letta doesn't expose the cosine
   score, the policy degrades to BM25-only and surfaces that in the result metadata.

4. **Keep the local `MemoryRecall` trail.** Even with Letta as primary, Atelier records its
   own recall events for trace continuity. They live in the SQLite `memory_recall` table
   regardless of memory backend.

5. **Ship the Docker setup.** Goal: a user can clone Atelier, run `atelier letta up`, set
   one config line, and have a working Letta-backed memory store within five minutes. The
   runbook is the proof.

6. **Test on a real Docker host.** The CLI smoke tests use mocks; before shipping, manually
   run `atelier letta up`, verify health, hit a memory op, and shut down cleanly.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/infra/test_letta_primary_round_trip.py \
                     tests/infra/test_letta_metadata_carryover.py \
                     tests/infra/test_letta_adapter_fallback.py \
                     tests/gateway/test_atelier_letta_cli.py -v

# Manual smoke (requires Docker):
LOCAL=1 atelier letta up
LOCAL=1 atelier letta status
# Expect: healthy, version printed.
ATELIER_MEMORY_BACKEND=letta ATELIER_LETTA_URL=http://localhost:8283 \
  LOCAL=1 atelier memory list
LOCAL=1 atelier letta down

make verify
```

## Definition of done

- [ ] All memory operations route through Letta when `backend=letta`; SQLite is not written
      to in that mode.
- [ ] Metadata carry-overs verified by `test_letta_metadata_carryover.py`.
- [ ] Hybrid ranking policy applied on Letta results; degrades cleanly when cosine is absent.
- [ ] `deploy/letta/docker-compose.yml` ships with a healthy default config + persistent
      volume.
- [ ] `atelier letta &#123;up|down|logs|status|reset&#125;` CLI subcommands work; smoke tests pass.
- [ ] Self-hosted runbook published; bring-up validated end-to-end on at least one machine.
- [ ] No new vector store, no new agent loop, no new model invocation written by Atelier
      (boundary check).
- [ ] `make verify` green.
- [ ] V3 INDEX updated. Trace recorded.
