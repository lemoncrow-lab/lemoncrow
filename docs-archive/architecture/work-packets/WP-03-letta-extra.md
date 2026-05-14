---
id: WP-03
title: Add `letta-client` optional extra + letta_adapter stub
phase: A
pillar: 1
owner_agent: atelier:code
depends_on: [WP-02]
status: done
---

# WP-03 — `letta-client` optional extra

## Why
Pillar 1 vendors Letta as an HTTP/gRPC client only. We must add the dependency without making it
mandatory for any code path. WP-06 will then build the full `LettaMemoryStore` adapter on top of
this stub.

## Files touched

- `pyproject.toml` — add new optional-dependencies groups
- `src/atelier/infra/memory_bridges/letta_adapter.py` — new (stub class)
- `src/atelier/infra/memory_bridges/__init__.py` — export the stub
- `tests/core/test_letta_adapter_stub.py` — new
- `docs/engineering/storage.md` — append a new "Letta sidecar (optional)" section
- `.env.production.example` — add `ATELIER_LETTA_URL=` (commented)

## How to execute

1. Edit `pyproject.toml` `[project.optional-dependencies]`:

   ```toml
   memory = ["letta-client>=1.7.12"]
   memory-server = ["letta>=0.16.7"]
   ```

   Do **not** add either to the base `dependencies` list.

2. Create `letta_adapter.py` with this contract — implementation is a stub now, WP-06 fills it in:

   ```python
   from __future__ import annotations
   import os
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from atelier.core.foundation.memory_models import MemoryBlock, ArchivalPassage

   _HAS_LETTA = False
   try:
       from letta_client import LettaClient  # noqa: F401
       _HAS_LETTA = True
   except ImportError:
       LettaClient = None  # type: ignore[assignment,misc]

   class LettaAdapter:
       """Optional sidecar adapter. WP-06 implements full body."""

       source = "letta"

       def __init__(self, url: str | None = None, api_key: str | None = None) -> None:
           if not _HAS_LETTA:
               raise RuntimeError("letta-client not installed; install 'atelier[memory]'")
           self.url = url or os.environ.get("ATELIER_LETTA_URL", "")
           self.api_key = api_key or os.environ.get("ATELIER_LETTA_API_KEY", "")
           if not self.url:
               raise RuntimeError("ATELIER_LETTA_URL not set")
           # WP-06: instantiate LettaClient(self.url, self.api_key)

       @classmethod
       def is_available(cls) -> bool:
           return _HAS_LETTA and bool(os.environ.get("ATELIER_LETTA_URL"))
   ```

3. Document in `docs/engineering/storage.md`:
   - When to use the sidecar (multi-agent at scale, > 10k passages, organization-wide vector store)
   - Minimal docker-compose entry the user can paste in
   - Failure modes: if sidecar is down, Atelier transparently falls back to `SqliteMemoryStore`

4. The unit test verifies:
   - `LettaAdapter.is_available()` returns `False` in the test env (no `ATELIER_LETTA_URL`)
   - Constructing without env raises clearly
   - Importing `letta_adapter` does **not** import the real `letta_client` module unless the env is set

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

# Default install (no extra) still works
LOCAL=1 uv sync && LOCAL=1 uv run pytest tests/core/test_letta_adapter_stub.py -v

# Optional install pulls letta-client and the import succeeds
LOCAL=1 uv sync --extra memory
LOCAL=1 uv run python -c "from atelier.infra.memory_bridges.letta_adapter import LettaAdapter; print(LettaAdapter.is_available())"

# Verify storage docs were updated
grep -q "Letta sidecar" docs/engineering/storage.md
```

## Definition of done
- [ ] `pyproject.toml` updated; `uv sync` green both with and without `--extra memory`
- [ ] Stub adapter compiles and is import-safe in the no-extra env
- [ ] Test passes
- [ ] Docs updated
- [ ] `INDEX.md` updated; trace recorded
