---
id: WP-V3.1-B
title: PageRank repo map (Aider-style) as `atelier_repo_map` MCP tool
phase: V3.1
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-33, WP-34, WP-35, WP-36, WP-39, WP-47, WP-49, WP-50]
supersedes: []
status: done
---

# WP-V3.1-B — PageRank repo map

## Why

V2 ships `search` (grep + AST outline + token counting). It answers "where is
this string?" well. It does not answer **"what code matters for this task?"** Aider's
PageRank repo map does — and it's fully deterministic, MIT-licensed, and well-documented.

The technique:

1. Walk the repo with **tree-sitter**; emit tags classifying each symbol as a _definition_
   or a _reference_.
2. Build a directed graph: edges from files that _reference_ a symbol to files that
   _define_ it. Weights account for symbol frequency.
3. Run **personalized PageRank** seeded by the files currently in the trace's working set
   (or supplied by the host).
4. Binary-search the top-N tags to fit a token budget — return a budgeted, ranked outline of
   "the code that matters here".

This is the deterministic algorithm Aider has been refining for 18+ months. It directly
beats grep+outline for repo navigation: grep finds _where_ a name appears, PageRank finds
_what is most relevant given where you are_.

Side effect: forces tree-sitter for languages beyond Python. V2's AST outline only handles
Python (via stdlib `ast`). V3.1-B brings JS/TS/Go/Rust along for the ride.

## Files touched

### Tree-sitter foundation

- **EDIT:** `pyproject.toml` — add `tree-sitter`, `tree-sitter-languages`, `networkx` to a
  new optional extra `[repo-map]`.
- **NEW:** `src/atelier/infra/tree_sitter/__init__.py` — namespace.
- **NEW:** `src/atelier/infra/tree_sitter/tags.py`:
  - `extract_tags(file_path, language) -> list[Tag]` where `Tag` carries (name, kind,
    file, line, byte_range).
  - Per-language tags.scm queries (steal Aider's exact queries — they're MIT-licensed and
    battle-tested). Languages: Python, JS, TS, Go, Rust at minimum.
- **NEW:** `src/atelier/infra/tree_sitter/queries/` — one `.scm` file per language.

### Repo map capability

- **NEW:** `src/atelier/core/capabilities/repo_map/__init__.py`
- **NEW:** `src/atelier/core/capabilities/repo_map/graph.py`:
  - `build_reference_graph(repo_root, files) -> nx.DiGraph` —
    walks repo, extracts tags, builds graph (definition_file ← reference_file edges).
  - Skips `.gitignore`-d paths, `node_modules`, build artifacts.
  - Caches per-file tag extraction by file mtime + content hash (reuse V2 cache pattern
    from `search_read`).
- **NEW:** `src/atelier/core/capabilities/repo_map/pagerank.py`:
  - `personalized_pagerank(graph, seed_files, alpha=0.85) -> dict[str, float]` — uses
    `networkx.pagerank` with `personalization` keyed by `seed_files`.
- **NEW:** `src/atelier/core/capabilities/repo_map/budget.py`:
  - `fit_to_budget(ranked_tags, budget_tokens) -> list[Tag]` — binary-searches the largest
    prefix of ranked tags whose serialized outline fits the budget. Uses tiktoken (already
    a V2 dep).
- **NEW:** `src/atelier/core/capabilities/repo_map/render.py`:
  - `render_outline(tags) -> str` — produces Aider-style repo map output (file headers,
    indented signatures, line numbers).
- **EDIT:** `src/atelier/gateway/mcp_server.py` — register MCP tool:
  ```python
  atelier_repo_map(
      seed_files: list[str],
      budget_tokens: int = 2000,
      languages: list[str] | None = None,  # default: detect from extensions
      include_globs: list[str] | None = None,
      exclude_globs: list[str] | None = None,
  ) -> RepoMapResult
  ```

### Tests + benchmark

- **NEW:** `tests/infra/test_tree_sitter_tags.py` — extract tags from fixture files (Python,
  JS, TS, Go, Rust); assert expected tags found.
- **NEW:** `tests/core/test_repo_map_graph.py` — build reference graph from a small fixture
  repo; assert expected edges.
- **NEW:** `tests/core/test_repo_map_pagerank.py` — personalized PageRank with two
  different seed sets; assert ranking shifts as expected.
- **NEW:** `tests/core/test_repo_map_budget.py` — binary search lands within budget;
  rank order preserved.
- **NEW:** `tests/gateway/test_atelier_repo_map_e2e.py` — MCP smoke test on a fixture repo
  (committed under `tests/fixtures/repo_map/`).

### Docs

- **NEW:** `docs/host-integrations/repo-map.md` — when to use `atelier_repo_map` vs.
  `search` (this is the comparison from the "Why" section, written for users).

## How to execute

1. **Get tree-sitter wired up first.** This is the biggest unknown — multi-language tag
   extraction with the right `.scm` queries. Steal Aider's queries verbatim (MIT). Test
   tag extraction in isolation before touching graph code.

2. **Build the graph.** Use `nx.DiGraph` with edge weights = `(num_references_to_symbol)`.
   Cache aggressively — re-extracting tags on every call would be slow.

3. **Personalized PageRank.** `networkx.pagerank` accepts a `personalization` kwarg that
   biases the random walk toward seed files. Use `alpha=0.85` (standard).

4. **Budget fitting.** Tiktoken count of the rendered outline; binary-search the prefix
   length that fits.

5. **Compare against `search_read`** on a real repo. The test fixture should show that for a
   "what files matter for the auth flow?" task, repo_map produces a more focused outline
   than grep would.

## Boundary check

**No LLM is called.** No Ollama, no internal_llm imports. Pure deterministic graph
algorithm. This is V3.1's only fully-deterministic packet — adoption gate should be
correspondingly low.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv pip install -e ".[repo-map]"

LOCAL=1 uv run pytest tests/infra/test_tree_sitter_tags.py \
                     tests/core/test_repo_map_graph.py \
                     tests/core/test_repo_map_pagerank.py \
                     tests/core/test_repo_map_budget.py \
                     tests/gateway/test_atelier_repo_map_e2e.py -v

# Manual smoke against this repo:
LOCAL=1 uv run python -c "
from atelier.core.capabilities.repo_map import build_repo_map
print(build_repo_map(seed_files=['src/atelier/gateway/mcp_server.py'], budget_tokens=1500))
"

make verify
```

## Definition of done

- [ ] Tree-sitter wired for Python, JS, TS, Go, Rust; tag extraction tested per language.
- [ ] Reference graph + personalized PageRank implemented; tests pass.
- [ ] Budget fit via binary search; never exceeds `budget_tokens` ± tiktoken counting
      tolerance.
- [ ] `atelier_repo_map` MCP tool registered; e2e smoke test green.
- [ ] No LLM client imports anywhere in repo_map/ or tree_sitter/ (boundary check).
- [ ] Doc published comparing repo_map vs. search_read.
- [ ] `make verify` green.
- [ ] V3 INDEX (V3.1 section) updated. Trace recorded.
