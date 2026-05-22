# M5 — `code op="pattern"` (ast-grep structural search & rewrite)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0. Independent of M1 (SCIP).

## Goal

Add a structural-pattern primitive that beats regex on precision and beats
SCIP on flexibility for refactor-shaped queries. *"Find every call to
`requests.get` without a timeout"* is impossible to express cleanly as a name
lookup; trivial as an ast-grep pattern.

No new MCP tool. We extend `tool_code` (mcp_server.py:1914) with `op="pattern"`,
delegating to a new internal adapter under `infra/code_intel/astgrep/`.

## Background — why ast-grep

ast-grep (`sg`) is a tree-sitter–native structural search/rewrite tool with
metavariables: `$AUTH.verify($USER)`, `requests.get($URL)`, etc. Single
static binary, cross-language, millisecond queries across huge repos, ships a
rewrite engine that respects AST boundaries. Reference: <https://ast-grep.github.io>.

## What changes

### `tool_code` — extend signature for the new op

```python
op: Literal[..., "pattern"],
# new for pattern op:
pattern: str | None = None,
rewrite: str | None = None,
dry_run: bool = True,                    # never write without explicit opt-out
# already-present params reused: language, file_glob (add if missing), limit, budget_tokens
```

### New adapter module

```
src/atelier/infra/code_intel/astgrep/
  __init__.py
  AGENT_README.md
  binary.py            Resolves the sg binary; lazy download to .atelier/bin/
  adapter.py           Subprocess wrapper; parses JSON output
  rewrite.py           Wraps the rewrite path with diff + reindex hook
```

### Dispatch inside `tool_code`

```python
if op == "pattern":
    from atelier.infra.code_intel.astgrep import run_pattern
    result = run_pattern(
        pattern=pattern, rewrite=rewrite, language=language,
        file_glob=file_glob, dry_run=dry_run, limit=limit,
        budget_tokens=budget_tokens, repo_root=repo_root,
    )
    # On non-dry-run rewrite: trigger engine reindex of changed files,
    # tag memory blocks pattern/<hash>, record trace via the existing path
    return result
```

The adapter shells out:

```python
def find_matches(pattern, lang, glob):
    cmd = ["sg", "run", "--pattern", pattern, "--json"]
    if lang: cmd += ["--lang", lang]
    if glob: cmd += ["--globs", glob]
    out = subprocess.run(cmd, capture_output=True, ...).stdout
    return [PatternMatch.parse(m) for m in json.loads(out)]

def apply_rewrite(pattern, rewrite, glob, dry_run):
    cmd = ["sg", "run", "--pattern", pattern, "--rewrite", rewrite, "--json"]
    if not dry_run: cmd.append("--update-all")
    # parse diff, call engine._reindex_file on each changed file
```

### Response shape

```json
{
  "matches": [
    { "file_path": "...", "line": 42, "captures": {"AUTH": "auth_service", "USER": "u"},
      "snippet": "auth_service.verify(u)" }
  ],
  "diff": "--- a/...\n+++ b/...\n...",     // only when rewrite was applied or dry_run
  "files_changed": ["..."],
  "truncated": false,
  "cache_hit": false
}
```

## When agents should use `pattern` vs `search`

| Use `code op="search"` (M2) when | Use `code op="pattern"` (M5) when |
|---|---|
| You know the name | You know the shape |
| "Find AuthService" | "Find every call to AuthService.verify" |
| Navigation, lookup | Refactor, audit, lint |
| Returns SymbolHits | Returns code matches with captures |

Add to `docs/agent-os/taste-invariants.md` in M13.

## Validation

Tests under `tests/infra/code_intel/astgrep/`:

- `test_pattern_finds_known_callsites` — fixture with 3 known `requests.get` calls → all 3 found.
- `test_pattern_captures_metavariables` — pattern with `$URL` → match includes captured URL string.
- `test_rewrite_dry_run_no_writes` — `dry_run=True` → diff returned, file unchanged.
- `test_rewrite_applied_with_reindex` — `dry_run=False` → file changed, engine reindex called.
- `test_budget_truncation` — 500 matches, budget 1000 tokens → truncated, top matches preserve captures.
- `test_binary_not_found_fallback` — missing binary → tool returns clear `tool_unavailable` error with install hint, does not crash.

Token-cost benchmark `tests/benchmarks/code_intel/bench_pattern_vs_grep.py`:

- Refactor task: "add a timeout kwarg to every `requests.get` call".
- Baseline: `tool_smart_search` + `tool_smart_read` + `tool_smart_edit` (line-based).
- New: `tool_code(op="pattern", pattern=..., rewrite=...)`.
- Pass: pattern path uses ≤ 30% of baseline tokens.

## Exit criteria

- ast-grep binary auto-fetched on first use, recorded in `.atelier/bin/MANIFEST` with checksum.
- `tool_code(op="pattern")` works; routes through M0 cache for search-only calls.
- Refactor benchmark shows ≥ 70% token reduction vs baseline.
- Validation matrix row added.

## Open questions

- **Binary distribution.** Lean fetch-on-first-use; release artifacts are ~5 MB and stable.
- **Multi-language patterns.** Auto-detect language from `file_glob` extension; require explicit `language` if mixed.
- **YAML rule files.** ast-grep also supports rule files for complex queries. Defer; pattern-string mode covers the common path.
- **Should rewrites go through `tool_smart_edit` instead?** Lean no — ast-grep does the AST-aware rewrite itself; routing through `tool_smart_edit` would double the work. But we *do* call the same diff-recording helper (`_compute_and_record_diffs`) so the ledger stays consistent.
