---
id: WP-05
title: Implement Embedder protocol + 4 backends
phase: A
pillar: 1, 2, 3
owner_agent: atelier:code
depends_on: [WP-02]
status: done
---

# WP-05 — Embedder

## Why

Pillars 1 (archival recall), 2 (lesson clustering), and 3 (scoped recall) all need a uniform
embedding interface. Centralising it lets us swap providers without touching call-sites.

## Files touched (all new unless marked)

- `src/atelier/infra/embeddings/__init__.py`
- `src/atelier/infra/embeddings/base.py` — `Embedder` Protocol + `EmbedResult` dataclass
- `src/atelier/infra/embeddings/local.py` — `LocalEmbedder` (sentence-transformers)
- `src/atelier/infra/embeddings/openai_embedder.py` — `OpenAIEmbedder`
- `src/atelier/infra/embeddings/letta_embedder.py` — `LettaEmbedder` (proxies sidecar)
- `src/atelier/infra/embeddings/null_embedder.py` — `NullEmbedder`
- `src/atelier/infra/embeddings/factory.py` — `make_embedder()`
- `tests/core/test_embedder_factory.py`
- `tests/core/test_null_embedder.py`
- `pyproject.toml` — append optional-dependency `embeddings = ["sentence-transformers>=2.7"]`

## How to execute

1. Define the `Embedder` Protocol in `base.py`:

   ```python
   from typing import Protocol, runtime_checkable

   @runtime_checkable
   class Embedder(Protocol):
       dim: int
       name: str
       def embed(self, texts: list[str]) -> list[list[float]]: ...
   ```

2. `NullEmbedder.embed` returns `[]` for every input. Its `dim = 0`. This is the default when no
   extra is installed; it forces consumers to fall back to FTS5.

3. `LocalEmbedder` lazy-imports `sentence_transformers`. Lazy-load the model on first `embed()`
   call. Cache the loaded model per-process. Use `all-MiniLM-L6-v2` → `dim=384`.

4. `OpenAIEmbedder` uses `openai` if it's already on the path (do **not** add to deps); otherwise
   uses raw `httpx`. Model: `text-embedding-3-small`. Dim: 1536. Read `OPENAI_API_KEY` from env.

5. `LettaEmbedder` calls the sidecar's embedding endpoint via `letta_client`. Falls back to raising
   if sidecar unreachable — caller must catch and degrade.

6. `make_embedder()` selection (in order):
   - `os.environ.get("ATELIER_EMBEDDER")` explicit pin (`local|openai|letta|null`)
   - `LettaAdapter.is_available()` → `LettaEmbedder`
   - `OPENAI_API_KEY` set → `OpenAIEmbedder`
   - `sentence_transformers` importable → `LocalEmbedder`
   - else → `NullEmbedder`

7. Tests:
   - `NullEmbedder` returns `[]`
   - `make_embedder()` returns a `NullEmbedder` in a stripped env
   - `make_embedder(pin="openai")` raises if `OPENAI_API_KEY` missing
   - All embedders satisfy `isinstance(e, Embedder)`

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_embedder_factory.py tests/core/test_null_embedder.py -v

LOCAL=1 uv run python -c "
from atelier.infra.embeddings.factory import make_embedder
e = make_embedder()
print('selected:', type(e).__name__, 'dim=', e.dim, 'name=', e.name)
v = e.embed(['hello world'])
print('vec:', len(v), 'x', len(v[0]) if v and v[0] else 0)
"

make verify
```

## Definition of done

- [ ] All four backends compile and pass `isinstance(e, Embedder)`
- [ ] Factory selection covered by tests for every branch
- [ ] No new top-level imports added to base `dependencies`
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
