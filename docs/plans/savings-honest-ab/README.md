# Honest savings — A/B harness + sequence-level metric

Status: **active** · Owner: code agent · Initiated 2026-05-21

## Why

We ship token-savings numbers (statusline, dashboards) computed from constants nobody can defend:

- `LIVE_INPUT_TOKENS_PER_CALL = 20_000`, `OUTPUT = 50_000`, `CACHE_READ = 1_000`, `CONTEXT_MULTIPLIER = 1.3` in `src/atelier/core/capabilities/plugin_runtime.py:35-40` — **zero source, zero ADR, zero PR justification**.
- `equivalent_calls` returns `2 + content_regex` for search, `1.0` for read, `5.0` for sql — all guessed.
- `docs/agent-os/tool-substitution.md` had "saves 80-95%" / "saves ~280k tokens per search" — **deleted in this commit, never had a benchmark**.
- The one measured number is **13.27% on 50 synthetic prompts** (`docs-archive/benchmarks/v3-honest-savings.md`).

Fix: replace constants with **measured per-tool deltas from real A/B runs**, and add a new metric the LLM can't game — *calls per LLM turn*.

## Two metrics, same harness

### M1. Per-tool A/B (replaces the multipliers)

For each Atelier tool with a native equivalent, run **both** on the same input, log both:

| Atelier tool | Native equivalent | Inputs |
|---|---|---|
| `mcp__atelier__read` outline mode | `cat FILE` | every changed file in last 100 sessions |
| `mcp__atelier__read` range mode | `sed -n 'a,bp' FILE` | every range-read tool use |
| `mcp__atelier__search` (chunks) | `rg -n PAT PATH` | every search args replayed |
| `mcp__atelier__search` (full) | `cat $(rg -l PAT PATH)` | same args |
| `mcp__atelier__code op=search` | `rg -nw SYMBOL` | symbol queries from traces |
| `mcp__atelier__edit` (atomic multi) | per-file `Edit` chain | replayed edit batches |

Measure: `chars_in`, `tokens_estimate`, `wall_ms`, `match_count`. Persist to `~/.atelier/savings_calibration.jsonl` with `{tool, native_chars, atelier_chars, ratio, model_input_usd_delta, ts}`.

Rolling median of `ratio` per tool replaces the magic constants.

### M2. Calls-per-turn (new — LLM's reduction in tool calls before responding)

The LLM saves money two ways:
1. Each individual call returns less context (M1).
2. The LLM needs **fewer calls** to find the answer (M2).

Definition: a *turn* is the sequence of tool calls between two assistant messages. A naive baseline turn for the same goal is: "how many native calls would the LLM have needed to surface the same answer span?"

Approximation (works without a counterfactual LLM run):

```
actual_calls_per_turn  = count(tool_uses where ts in [t_user, t_assistant])
naive_calls_per_turn   = sum over actual_calls: equivalent_calls(tool, args)
turn_calls_saved       = max(0, naive_calls_per_turn - actual_calls_per_turn)
```

But `equivalent_calls` itself is the thing we're trying to validate. So we measure naive_calls_per_turn empirically:

- Replay the same task against a fixed baseline agent with ONLY native tools (`Read`, `Bash`, `Grep`, `Glob`, `Edit`).
- The baseline agent's calls-per-turn for the same goal is the naive baseline.
- Persist `{turn_id, goal_hash, actual_calls, baseline_calls, delta}` to `~/.atelier/turn_calibration.jsonl`.

We need a corpus of (goal, ground-truth-answer) pairs. Bootstrap from `~/.atelier/session_stats/*.json` — every completed session is a (task, answer) pair.

## Action items (sequenced)

1. **Hard-remove `savings_bench.py` V2** — it explicitly says "deprecated for measurement". Touches `cli.py:3199`, `run_swe_bench.py:205`, `tests/infra/test_context_savings_smoke.py`, Makefile target (already removed in this commit). Replace `cli.py:3199` import with an explicit error.
2. **Wire bash interception** in `pre_tool_use.py`/`tool_redirect.py` for stable mode behind `ATELIER_BASH_REWRITE=shadow|rewrite|off`:
   - `shadow` (default during rollout): run both, log delta, no rewrite
   - `rewrite`: replace `Bash(grep ...)` with `mcp__atelier__search`
   - `off`: today's behavior
3. **Bash dedup cache pre-hook** — `_cache_bash_invocation` exists post-hoc (`post_tool_use_bash.py:62-88`); add pre-hook lookup. Cache hits = unambiguous "call saved".
4. **Replace magic constants** in `plugin_runtime.py:35-40` with `load_calibration()` that reads `~/.atelier/savings_calibration.jsonl`. Falls back to current numbers only when the calibration file has fewer than 30 samples per tool.
5. **Persist the cross-host bench aggregate** — `tool_bench/runner.py` already computes `saving_pct`; persist the aggregate JSON+MD into `reports/YYYY-WW/per-tool-bench.{json,md}`.
6. **Calls-per-turn baseline corpus** — `make bench-turns` that picks N completed sessions, replays the task against a native-only baseline, logs delta.

## Open bug: `atelier:explore` subagent unreachable

Claude Code's harness advertises both built-in `Explore` and our plugin `atelier:explore` but both reject invocation with `Agent type 'explore' not found`. Subagents fall back to `general-purpose`, which has unrestricted Bash — they then use grep/find instead of our tools and **none of our savings instrumentation fires**. This invalidates A/B numbers any subagent produces.

Workaround: rename plugin agent `explore` → `atelier_explore` (frontmatter `name:`). Will land with action item #2.

## Test files

- `tests/benchmarks/test_read_ab_real.py` — measures `mcp__atelier__read` vs native on 4 real repo Python files + 4 multi-language fixtures + 3 inflated synthetic fixtures (Go/Rust/Java). Uses **tiktoken cl100k_base** for honest token counts (was `len(text) // 4`). Persists rows to `~/.atelier/savings_calibration.jsonl`.
- Future: `test_search_ab_real.py`, `test_code_ab_real.py`, `test_edit_ab_real.py`, `test_turn_calls_ab_real.py`.

## Real numbers (as of 2026-05-22, post tree-sitter integration)

From `make bench-ab`, surfaced via `build_savings_report()['ab_calibration']`. Saved % is **real tiktoken cl100k_base** counts on `outline` vs `full`.

### Languages with tree-sitter outlines (this turn)

Measured on 3×-inflated synthetic fixtures (small enough to keep in `tests/benchmarks/fixtures/`, big enough to cross the 200-LOC threshold):

| Language | Saved % (tiktoken) | Source of outline |
|---|---|---|
| Go | **76.9%** | tree-sitter (`function_declaration`, `type_declaration`, `import_declaration`, ...) |
| Java | **76.5%** | tree-sitter (class/interface/enum/record containers + method signatures) |
| Rust | **65.4%** | tree-sitter (`fn`, `struct`, `enum`, `trait`, `impl` containers) |
| C# | ~78% on smoke test | tree-sitter (namespace/class containers + members) |
| Bash | ~60% on smoke test | tree-sitter (function definitions) |
| Ruby | _config in place, no fixture yet_ | tree-sitter |
| C / C++ | _config in place, no fixture yet_ | tree-sitter |
| Kotlin / PHP / Swift / Scala | _config in place; smoke tests show 22-27% on tiny samples — needs realistic-size fixture to confirm production savings_ | tree-sitter |

### Languages with non-tree-sitter outlines

| Language | Saved % (tiktoken) | Source of outline |
|---|---|---|
| Python | **85.3%** | stdlib `ast` (existing) |
| TypeScript / JS | _no AB fixture yet_ | regex-based (existing) |
| Markdown | **84.5%** | heading-extract |

### Languages with generic regex fallback only

_Used when tree-sitter outline saves < 25% (safety gate) or no per-language config exists_:

yaml, toml, json (all currently route through `_language_for` but typically too small to trigger outline). When they do trigger, the generic outline extracts column-0 lines and signature-shaped patterns.

### Production behavior at non-inflated sizes

Go/Rust/Java at the original ~150 LOC fixture sizes correctly return `full` mode because they fall under the production outline_threshold of 200 LOC. The numbers above are for files that cross the threshold.

## Done in this thread

- [x] Hard-removed `rewrite_agent` (caller in `tool_redirect.py` + tests). `atelier:explore` resolves cleanly via namespace.
- [x] Hard-removed `free_plan` (4 read sites + JSON file write + 3 tests).
- [x] Replaced `len(text) // 4` token heuristic with **tiktoken cl100k_base** (`semantic_file_memory/capability.py`), with chars/4 fallback if tiktoken unavailable.
- [x] Added **generic regex-based outline** for non-Python/TS languages as a fallback.
- [x] Added **tree-sitter outlines** (`semantic_file_memory/treesitter_ast.py`) for **Go, Rust, Java, Ruby, C, C++, C#, Kotlin, PHP, Swift, Scala, Bash** via `tree-sitter-language-pack`. Smart_read pipeline: AST (py/ts) → tree-sitter (per-grammar) → generic regex → full. Each stage gated at 25%-saved minimum so we never ship fake savings.
- [x] Extended `_language_for` to cover 22 file types.
- [x] Expanded `tests/benchmarks/test_read_ab_real.py` from 4 to 12 cases, tracking per-language ratios.
- [x] `_summarize_ab_calibration` returns `by_language` breakdown so dashboards can't show one inflated median that hides per-language outline weakness.

### Known: test isolation issue

`tests/gateway/test_statusline_script.py::_run_statusline` does `env = os.environ.copy()` then overrides `ATELIER_STORE_ROOT` — but `statusline.sh` prefers `ATELIER_ROOT` over `ATELIER_STORE_ROOT`. When pytest is run with `ATELIER_ROOT=...` (as our benchmark runs do), the variable leaks into the subprocess and the statusline reads the wrong root.

Fix: `_run_statusline` should `env.pop("ATELIER_ROOT", None)` before adding `ATELIER_STORE_ROOT`. ~1 line.

### Measured improvement from tree-sitter (vs generic regex)

| Lang | Generic | Tree-sitter | Gain |
|---|---|---|---|
| Go | 73.3% | 76.9% | +3.6pp |
| Java | 59.9% | 76.5% | **+16.6pp** |
| Rust | 52.2% | 65.4% | **+13.2pp** |

Java and Rust saw the biggest jump because tree-sitter knows class/impl/trait containers and extracts only member signatures, where the regex approach was keeping every column-0 line in those grammars.

## Non-goals

- We are NOT trying to prove a specific savings %. We are trying to **make the displayed number truthful**, whatever it turns out to be.
- If real A/B shows read outline saves 30% not 80%, the docs and statusline will say 30%. The point is to stop lying.
