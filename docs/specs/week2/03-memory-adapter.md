# Spec 03 — Memory Adapter: Read Native Memories

> Phase 1. The cross-vendor memory wedge starts here.

## Why

Anthropic, OpenAI, and Google all shipped persistent memory in 2026. Each is locked to its own vendor. A developer using two or three native CLIs ends up with two or three disconnected knowledge bases — nothing flows between them. Atelier becomes useful by being the **single inspectable view** across all three.

This spec ships **read-only** ingestion. We don't write to the native memories yet; we surface what each vendor has stored. Writing back (or two-way sync) is a later spec.

## What — user-visible

```bash
$ atelier memory list
Memory facts (74 total, 3 vendors)

Anthropic — Claude Code (32 facts)
  ~/.claude/projects/atelier/CLAUDE.md  (auto-memory)
    - Pankaj prefers explicit type hints (Python 3.13+ syntax)
    - Repo uses uv, not pip/poetry
    - Tests in tests/, mirror src/atelier/ structure
    ... 29 more

OpenAI — Codex (28 facts)
  ~/.codex/memories/atelier-project.md
    - Engineering style: "hard-remove, never deprecate"
    - Telemetry stack: OTel → PostHog + GCP
    ... 26 more

Google — Gemini CLI (14 facts)
  ~/.gemini/GEMINI.md (global)
    - Email: pankaj4u4m@gmail.com
  ./GEMINI.md (project)
    - Branch convention: feat/, fix/, refactor/, chore/
    ... 12 more

$ atelier memory list --vendor claude
... (only Claude memories)

$ atelier memory show <fact-id>
... (full text + provenance)

$ atelier memory find "branch convention"
Found 2 matches:
  [gemini-12] Branch convention: feat/, fix/, refactor/, chore/
  [claude-08] Branches off `main`. Branch name: spec/<spec-number>-<short-name>
```

## Where — files

| File                                                                    | What changes                                                  |
| ----------------------------------------------------------------------- | ------------------------------------------------------------- |
| `src/atelier/core/capabilities/cross_vendor_memory/__init__.py`       | **New package.**                                        |
| `src/atelier/core/capabilities/cross_vendor_memory/base.py`           | Abstract `MemoryAdapter` interface                          |
| `src/atelier/core/capabilities/cross_vendor_memory/claude_adapter.py` | Reads `CLAUDE.md` files + session-memory directory          |
| `src/atelier/core/capabilities/cross_vendor_memory/codex_adapter.py`  | Reads `~/.codex/memories/` markdown files                   |
| `src/atelier/core/capabilities/cross_vendor_memory/gemini_adapter.py` | Reads `GEMINI.md` hierarchy (global, project, subdirectory) |
| `src/atelier/core/capabilities/cross_vendor_memory/registry.py`       | Aggregator across all adapters                                |
| `src/atelier/gateway/adapters/cli.py`                                 | Add `memory` command group: `list`, `show`, `find`    |
| `tests/core/capabilities/cross_vendor_memory/...`                     | Fixture-based tests for each adapter                          |

## Data model

```python
@dataclass(frozen=True)
class MemoryFact:
    fact_id: str            # stable across reads; e.g., "claude-08"
    vendor: str             # "claude" | "codex" | "gemini"
    source_path: Path       # where it was read from
    source_kind: str        # "claude-md" | "auto-memory" | "session-memory" | "codex-mem" | "gemini-md-global" | etc.
    content: str            # the actual fact text
    line_number: int | None # for editable sources
    captured_at: datetime   # when we read it
    raw_meta: dict[str, Any] = field(default_factory=dict)  # vendor-specific extras
```

### Adapter interface

```python
class MemoryAdapter(Protocol):
    vendor: str

    def is_available(self) -> bool:
        """Return True if this vendor's memory files exist on this machine."""

    def list_facts(self) -> list[MemoryFact]:
        """Read and parse all facts. Pure read; no side effects."""

    def source_paths(self) -> list[Path]:
        """All file paths this adapter reads from."""
```

## Adapter details

### Claude adapter

Reads:

- `~/.claude/CLAUDE.md` (global instructions, if present)
- `~/.claude/projects/<project>/CLAUDE.md` (per-project, multiple)
- `~/.claude/projects/<project>/memory/MEMORY.md` (auto-memory)
- `~/.claude/projects/<project>/session_memory/*.md` (Session Memory output)

Parse rule for `CLAUDE.md` / `MEMORY.md`:

- Bullet points (`- foo`, `* foo`) → one fact per line
- Headings ignored as facts but kept as `raw_meta["section"]`
- Code blocks treated as one multi-line fact

`fact_id` format: `claude-<sha1(content)[:8]>`

### Codex adapter

Reads:

- `~/.codex/memories/*.md` (consolidated session summaries)
- `~/.codex/memories/global.md` if present

Parse rule:

- Each `## Heading` block is one fact, with the heading as `raw_meta["heading"]`
- Bullet points under a heading become sub-facts only if they're standalone declarations (line starts with capital letter, ends with `.` or `;`)

`fact_id` format: `codex-<sha1(content)[:8]>`

### Gemini adapter

Reads in hierarchy order (most specific wins):

- `~/.gemini/GEMINI.md` (global)
- `<repo-root>/GEMINI.md` (project)
- `<cwd>/GEMINI.md` (subdirectory, if different from project root)

Repo root detection: walk up from `cwd` looking for `.git/`.

Parse rule: same bullet-point rule as Claude.

`fact_id` format: `gemini-<sha1(content)[:8]>`

## Aggregator

```python
class MemoryRegistry:
    def __init__(self, adapters: list[MemoryAdapter] | None = None) -> None: ...
    def all_facts(self) -> list[MemoryFact]: ...
    def by_vendor(self, vendor: str) -> list[MemoryFact]: ...
    def find(self, query: str, *, limit: int = 20) -> list[MemoryFact]: ...
    def show(self, fact_id: str) -> MemoryFact | None: ...
```

`find()` uses simple substring + fuzzy match (Levenshtein) on lowered content. Tokenised search and embeddings are deferred to a later spec.

## CLI behaviour

```bash
atelier memory list                 # all adapters
atelier memory list --vendor claude
atelier memory list --vendor codex
atelier memory list --vendor gemini
atelier memory show <fact-id>
atelier memory find "<query>"
atelier memory paths                # show which files we read from
atelier memory list --json
```

## Out of scope

- **Writing facts back to vendor files.** Read-only this round.
- **Two-way sync between vendors** (read from Claude, write to Gemini). Future spec.
- **Embedding-based semantic search.** Substring + fuzzy is enough for v1.
- **Memory editing UI.** Spec 08 (audit viewer).
- **Conflict resolution** between contradictory facts. Mark as `flagged_conflict` in `raw_meta` for now, no resolution.

## Acceptance criteria

- [X] All three adapters parse real native memory files on a representative dev machine
- [X] `atelier memory list` shows all detected facts grouped by vendor, sorted by `source_path`
- [X] `atelier memory list --vendor X` filters correctly
- [X] `atelier memory show <fact-id>` returns the full fact with provenance
- [X] `atelier memory find "<query>"` does substring + fuzzy match, top 20 results
- [X] Missing memory files don't crash — adapter returns `is_available() == False`, registry skips it
- [X] `--json` flag works on every subcommand
- [X] Unit tests use fixtures in `tests/fixtures/memory/<vendor>/` — committed test files mimicking real layouts
- [X] Reading 1000 facts from disk takes under 100ms (validated)

## Open questions for the executor

1. The vendor source paths can vary by OS. Confirm Windows / macOS / Linux paths in the adapter docstrings (look at native vendor docs).
2. If Claude's `Session Memory` files are JSON (not markdown), do we parse them? **Default: yes, treat each top-level entry as a fact.**
3. If a fact appears in multiple Claude `CLAUDE.md` files (global + project), do we dedupe? **Default: no — keep both, list separately with their source paths.**

## Implementation order

1. `MemoryFact` and `MemoryAdapter` protocol
2. Claude adapter + tests
3. Codex adapter + tests
4. Gemini adapter + tests
5. `MemoryRegistry` + CLI commands
6. End-to-end test reading from a real fixture directory

## Status

- [X] Pending
- [X] In progress
- [X] Shipped
