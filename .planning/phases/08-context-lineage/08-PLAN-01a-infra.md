---
phase: 08
plan: "01a"
title: "Core Infra — Models, Walker, Summarizer, Embedder"
status: pending
created: 2025-07-15
requires: []
requirements:
  - LINEAGE-01
  - LINEAGE-05
---

# Phase 08 — Context Lineage: Core Infra (New Files)

## Goal

Create the foundational data contracts and new-file modules: data models, commit walker, LLM summariser, and embedding helper. All changes are purely additive (new files + model field extension). No existing files are modified except `models.py`.

---

## Tasks
### Task 1: Add CommitRecord / CommitSummary / CommitChunk dataclasses and extend SymbolRecord

**Files:**
- `src/atelier/infra/code_intel/git_history/models.py` (extend)
- `src/atelier/core/capabilities/code_context/models.py` (extend)

**Why:** Typed contracts must exist before any other module imports them. `SymbolRecord` must carry an optional `commit_sha` field so commit results can surface the SHA to callers; its `extra="forbid"` model prevents adding ad-hoc keys at runtime.

**What:**

In `src/atelier/infra/code_intel/git_history/models.py`, append three frozen dataclasses after the existing ones. Do **not** modify any existing dataclass:

```python
@dataclass(frozen=True)
class CommitRecord:
    """Raw enumerated commit before summarisation."""
    sha: str
    author_date: int          # unix seconds (commit.commit_time)
    message: str              # strip()[:2000]
    files_touched: list[str]  # [patch.delta.new_file.path for patch in diff]
    is_merge: bool            # len(commit.parents) > 1

@dataclass(frozen=True)
class CommitSummary:
    """LLM-generated semantic summary of a single commit."""
    sha: str
    author_date: int
    files_touched: list[str]
    summary: str              # ≤200 tokens, produced by _PROMPT_V1
    summary_model: str        # e.g. "claude-haiku-4-5", "llama3.2:3b"
    prompt_version: str       # "v1" — matches _CURRENT_PROMPT_VERSION in summarizer.py

@dataclass(frozen=True)
class CommitChunk:
    """Persisted commit chunk as read back from SQLite."""
    commit_sha: str
    author_date: int
    files_touched: list[str]       # JSON-deserialised
    symbols_touched: list[str] | None
    summary: str
    summary_model: str
    embedding: list[float] | None  # decoded from BLOB; None if not yet embedded
    index_version: int
```

In `src/atelier/core/capabilities/code_context/models.py`, add one optional field to `SymbolRecord` **only**:

```python
# Add after `cross_lang_refs: list[CrossLangReference] | None = None`
commit_sha: str | None = None
```

The field defaults to `None` so all existing `SymbolRecord` instantiation sites continue to compile. Add `"commit_sha"` to the `__all__` export only if you add it to the list; otherwise the list is for classes, not fields, so no change needed.

**Test:**
```
uv run python -c "
from atelier.infra.code_intel.git_history.models import CommitRecord, CommitSummary, CommitChunk
from atelier.core.capabilities.code_context.models import SymbolRecord
r = CommitRecord(sha='abc', author_date=0, message='m', files_touched=['f.py'], is_merge=False)
s = CommitSummary(sha='abc', author_date=0, files_touched=['f.py'], summary='s', summary_model='haiku', prompt_version='v1')
c = CommitChunk(commit_sha='abc', author_date=0, files_touched=['f.py'], symbols_touched=None, summary='s', summary_model='haiku', embedding=None, index_version=1)
sr = SymbolRecord(symbol_id='x', repo_id='r', file_path='f.py', language='py', symbol_name='fn', qualified_name='fn', kind='function', signature='def fn()', start_byte=0, end_byte=10, start_line=1, end_line=1, content_hash='h', commit_sha='abc123')
print('OK')
"
```

**Depends on:** nothing

---

### Task 2: Add `iter_commit_records()` generator to `walker.py`

**File:** `src/atelier/infra/code_intel/git_history/walker.py` (extend)

**Why:** The engine's `_walk_and_summarise()` method needs to enumerate commits with skip filters and resume support. The existing `walk_history()` function is unsuitable: it uses TOPOLOGICAL sort, visits all commits, and returns GraveyardEntry objects. A purpose-built generator using TIME sort with a `since_sha` cursor and configurable `limit` is required.

**What:**

Add the following after the existing imports (add `from collections.abc import Iterator` and `from atelier.infra.code_intel.git_history.models import CommitRecord` to imports):

```python
def iter_commit_records(
    repo_path: str | Path,
    *,
    limit: int = 500,
    since_sha: str | None = None,
) -> Iterator[CommitRecord]:
    """Yield up to `limit` CommitRecord objects in reverse-chronological order.

    Stops when `since_sha` is encountered (resume support: that SHA was the last
    successfully processed commit in a prior interrupted walk).

    Skip rules (LINEAGE-01):
    - Merge commits whose diff has zero patches (pure topology-only merges).
    - Commits with >50 files touched, unless message contains "[lineage:keep]".
    - Bot commits whose author email contains "dependabot" or "renovate[bot]",
      unless message contains "[lineage:keep]".
    - Initial commits (no parents).
    """
    pygit2 = require_pygit2()
    repo = pygit2.Repository(str(repo_path))
    try:
        head = repo.revparse_single("HEAD")
    except Exception:
        return
    count = 0
    for commit in repo.walk(head.id, pygit2.enums.SortMode.TIME):
        if count >= limit:
            break
        sha = str(commit.id)
        if since_sha is not None and sha == since_sha:
            break  # resume: reached last processed sha, stop
        if not commit.parents:
            continue  # initial commit
        is_merge = len(commit.parents) > 1
        parent = commit.parents[0]
        diff = parent.tree.diff_to_tree(commit.tree)
        patches = list(diff)
        if is_merge and len(patches) == 0:
            continue  # pure merge commit, no file-level diff
        files_touched = [p.delta.new_file.path for p in patches if p.delta.new_file.path]
        msg = commit.message.strip()
        keep_override = "[lineage:keep]" in msg
        if not keep_override and len(files_touched) > 50:
            continue  # likely codegen/vendor commit
        author_email = (commit.author.email or "").lower()
        is_bot = "dependabot" in author_email or "renovate[bot]" in author_email
        if is_bot and not keep_override:
            continue
        yield CommitRecord(
            sha=sha,
            author_date=commit.commit_time,
            message=msg[:2000],
            files_touched=files_touched,
            is_merge=is_merge,
        )
        count += 1
```

Add to `__all__` if it exists, otherwise leave as-is. The function is importable via `from atelier.infra.code_intel.git_history.walker import iter_commit_records`.

**Test:**
```bash
uv run python -c "
import tempfile, subprocess, pathlib
from atelier.infra.code_intel.git_history.walker import iter_commit_records

td = pathlib.Path(tempfile.mkdtemp())
subprocess.run(['git','init'], cwd=td, check=True)
subprocess.run(['git','config','user.name','T'], cwd=td, check=True)
subprocess.run(['git','config','user.email','t@t.com'], cwd=td, check=True)
(td/'a.py').write_text('x=1')
subprocess.run(['git','add','-A'], cwd=td, check=True)
subprocess.run(['git','commit','-m','first'], cwd=td, check=True)
sha1 = subprocess.check_output(['git','rev-parse','HEAD'], cwd=td, text=True).strip()
(td/'b.py').write_text('y=2')
subprocess.run(['git','add','-A'], cwd=td, check=True)
subprocess.run(['git','commit','-m','second'], cwd=td, check=True)
records = list(iter_commit_records(td, limit=500))
assert len(records) == 1, f'Expected 1 non-initial commit (initial skipped), got {len(records)}'
records_since = list(iter_commit_records(td, limit=500, since_sha=sha1))
assert len(records_since) == 1, f'Resume should yield 1, got {len(records_since)}'
print('OK')
"
```

**Depends on:** Task 1 (CommitRecord)

---

### Task 3: Create `summarizer.py`

**File:** `src/atelier/infra/code_intel/git_history/summarizer.py` (new)

**Why:** `CommitRecord → CommitSummary` transformation via internal LLM. Must be a standalone module so the engine can import it without creating circular dependencies. Uses `_PROMPT_V1` as a version-pinned constant so bumping the version triggers re-summarisation (LINEAGE-05).

**What:**

Create the file with this exact structure:

```python
"""LLM-based commit summariser for Context Lineage (M1).

Uses the internal_llm.chat() abstraction so Ollama and OpenAI backends
both work without change. Model is configurable via ATELIER_LINEAGE_MODEL
env var; defaults to "claude-haiku-4-5".
"""
from __future__ import annotations

import os
from atelier.infra.code_intel.git_history.models import CommitRecord, CommitSummary
from atelier.infra.internal_llm import chat  # established abstraction


# Version-pinned prompt. Bumping the string constant to _PROMPT_V2 AND
# incrementing _CURRENT_PROMPT_VERSION_INT in engine.py triggers re-summarisation
# of all rows where index_version < current. Old summaries remain searchable
# until the background re-summarisation pass completes.
_CURRENT_PROMPT_VERSION = "v1"

_PROMPT_V1 = (
    "Summarise this commit in 80-120 words. Cover:\n"
    "1. Primary objective (what problem was solved)\n"
    "2. Key files and functions changed\n"
    "3. Technical terminology a future reader would search for\n\n"
    "Do not include the commit hash or author. Do not include any code.\n"
    "Do not editorialise. Plain prose only.\n\n"
    "<COMMIT_MESSAGE>\n{message}\n</COMMIT_MESSAGE>\n\n"
    "<DIFF_TRUNCATED_TO_2K_TOKENS>\n{diff}\n</DIFF_TRUNCATED_TO_2K_TOKENS>"
)

_DEFAULT_MODEL = "claude-haiku-4-5"
_ENV_MODEL_KEY = "ATELIER_LINEAGE_MODEL"


class SummarizerError(Exception):
    """Raised when the LLM call fails or returns an unusable response."""


def _resolve_model() -> str:
    return os.environ.get(_ENV_MODEL_KEY, _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def summarize_commit(
    record: CommitRecord,
    *,
    diff_text: str = "",
    model: str | None = None,
) -> CommitSummary:
    """Summarise `record` using _PROMPT_V1.

    Args:
        record: CommitRecord from iter_commit_records().
        diff_text: Raw unified diff text. Truncated to ~8000 chars (≈2000 tokens)
            before sending to the model. Pass "" when diff is unavailable.
        model: Override model name. Defaults to ATELIER_LINEAGE_MODEL env var
            or "claude-haiku-4-5".

    Returns:
        CommitSummary with prompt_version="v1".

    Raises:
        SummarizerError: If the LLM call fails or returns an empty string.
    """
    effective_model = model or _resolve_model()
    truncated_diff = diff_text[:8000] if diff_text else "(no diff available)"
    prompt = _PROMPT_V1.format(
        message=record.message,
        diff=truncated_diff,
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        raw = chat(messages, model=effective_model)
    except Exception as exc:
        raise SummarizerError(f"LLM call failed for {record.sha[:8]}: {exc}") from exc
    if not raw or not raw.strip():
        raise SummarizerError(f"LLM returned empty summary for {record.sha[:8]}")
    return CommitSummary(
        sha=record.sha,
        author_date=record.author_date,
        files_touched=record.files_touched,
        summary=raw.strip(),
        summary_model=effective_model,
        prompt_version=_CURRENT_PROMPT_VERSION,
    )


__all__ = ["SummarizerError", "_PROMPT_V1", "_CURRENT_PROMPT_VERSION", "summarize_commit"]
```

**Note on `chat()` import path:** Verify the exact import path by running `grep -rn "^from atelier.infra.internal_llm" src/atelier/ | head -5` before coding. If the module is `atelier.infra.internal_llm.client` with a `chat` function, adjust accordingly. The import must go through the existing abstraction — never call `anthropic.Anthropic()` directly.

**Test:**
```bash
uv run python -c "
import pytest, sys
# Quick smoke test via monkeypatch
from unittest.mock import patch
from atelier.infra.code_intel.git_history.models import CommitRecord
from atelier.infra.code_intel.git_history.summarizer import summarize_commit, SummarizerError

record = CommitRecord(sha='a'*40, author_date=0, message='fix: auth bug', files_touched=['auth.py'], is_merge=False)

with patch('atelier.infra.code_intel.git_history.summarizer.chat',
           return_value='Fixed authentication session leak in login flow. Key changes in auth.py login() function. Affects session management, token expiry, and cookie handling.'):
    summary = summarize_commit(record, diff_text='- old code\n+ new code')

assert summary.sha == 'a'*40
assert summary.prompt_version == 'v1'
assert len(summary.summary) > 10
assert 'def ' not in summary.summary  # no code snippets

with patch('atelier.infra.code_intel.git_history.summarizer.chat', side_effect=RuntimeError('timeout')):
    try:
        summarize_commit(record)
        assert False, 'should raise'
    except SummarizerError:
        pass

print('OK')
"
```

Also run: `uv run pytest tests/infra/code_intel/git_history/test_summarizer.py -x` (after Task 8 creates the file).

**Depends on:** Task 1 (CommitRecord, CommitSummary)

---

### Task 4: Create `embedder.py`

**File:** `src/atelier/infra/code_intel/git_history/embedder.py` (new)

**Why:** `CommitSummary → bytes` (384-dim float32 BLOB) using `LocalEmbedder`, same embedder instance as `SemanticSearchRanker`. Dimension must match symbol embeddings for merged cosine-similarity ranking. This module is intentionally thin — it wraps `LocalEmbedder.embed()` and handles BLOB serialisation/deserialisation.

**What:**

```python
"""Embedding helper for Context Lineage commit summaries.

Uses LocalEmbedder (384-dim feature-hash) — the SAME embedder as
SemanticSearchRanker — so commit and symbol embeddings are directly
comparable in reciprocal_rank_fuse().

CRITICAL: Do NOT use make_embedder() or generate_embedding() from
infra/storage/vector.py — those may use ATELIER_EMBEDDING_DIM (default
1536) which is a different dimension. Always instantiate LocalEmbedder
directly.
"""
from __future__ import annotations

import struct
from atelier.infra.embeddings.local import LocalEmbedder
from atelier.infra.code_intel.git_history.models import CommitSummary

_DIM = 384
_EMBEDDER: LocalEmbedder | None = None  # lazy singleton


def _get_embedder() -> LocalEmbedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = LocalEmbedder()  # dim=384, model="hashing"
    return _EMBEDDER


def embed_summary(summary: CommitSummary) -> bytes:
    """Embed the summary text + top-10 files into a 384-dim float32 BLOB.

    Text format: "{summary}\\n{space-joined files[:10]}"
    Storage: struct.pack(f'{dim}f', *vector) — little-endian float32, 1536 bytes.
    """
    text = f"{summary.summary}\n{' '.join(summary.files_touched[:10])}"
    embedder = _get_embedder()
    vectors = embedder.embed([text])  # returns list[list[float]]
    vec = vectors[0]
    return struct.pack(f"{len(vec)}f", *vec)


def decode_embedding(blob: bytes) -> list[float]:
    """Deserialise a BLOB back to list[float]."""
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


def embedding_dim() -> int:
    """Return the expected embedding dimension (384)."""
    return _DIM


__all__ = ["embed_summary", "decode_embedding", "embedding_dim"]
```

**Test:**
```bash
uv run python -c "
from atelier.infra.code_intel.git_history.models import CommitSummary
from atelier.infra.code_intel.git_history.embedder import embed_summary, decode_embedding, embedding_dim

s = CommitSummary(
    sha='abc',
    author_date=0,
    files_touched=['src/auth.py', 'src/session.py'],
    summary='Fixed authentication session leak in login flow.',
    summary_model='haiku',
    prompt_version='v1',
)
blob = embed_summary(s)
assert isinstance(blob, bytes), 'must be bytes'
assert len(blob) == 384 * 4, f'expected 1536 bytes, got {len(blob)}'
vec = decode_embedding(blob)
assert len(vec) == 384, f'expected 384 floats, got {len(vec)}'
assert embedding_dim() == 384
print('BLOB roundtrip OK, dim=384')
"
```

**Depends on:** Task 1 (CommitSummary)
