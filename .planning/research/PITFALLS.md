# Domain Pitfalls: Reproducible AI Agent Benchmarking

**Domain:** End-to-end A/B agent benchmarking (Atelier-on vs Atelier-off)
**Researched:** 2026-05-28
**Confidence:** HIGH (based on direct codebase inspection + verified API docs)

---

## Critical Pitfalls

Mistakes that cause rewrites, destroy credibility, or make results irreproducible.

---

### Pitfall 1: `ATELIER_BENCH_MODE=off` — Shared ATELIER_ROOT Contaminates Both Arms

**What goes wrong:**
Both bench arms (`mode=on` and `mode=off`) accidentally share state through the filesystem because they use the same `ATELIER_ROOT`.

**Why it happens — confirmed by code inspection:**
`RealtimeContextManager` writes `{ATELIER_ROOT}/runtime/realtime_context.json` and reloads it on next instantiation (`self._state = self._load()` in `__init__`). `CostTracker` writes `{ATELIER_ROOT}/cost_history.json` and appends every call immediately. `RunLedger` writes `{ATELIER_ROOT}/runs/{session_id}.json`. `_append_savings()` writes to `{ATELIER_ROOT}/session_stats/<host>/<session_id>.jsonl`.

If both arms run sequentially in the same process with the same `ATELIER_ROOT`:
- The on-arm's compressed context state persists to disk and is loaded by the off-arm on next instantiation.
- The cost history accumulates interleaved entries from both arms, making savings calculations wrong.
- Session stats from the on-arm can be picked up by the off-arm's session report.

**Consequences:** The off-arm is not a clean baseline — it starts with Atelier's memory artifacts pre-loaded. Cost and quality deltas are contaminated.

**Prevention:**
- Each bench arm must use a distinct, freshly created ATELIER_ROOT (e.g., a temp directory scoped to the run):
  ```python
  on_root = bench_run_dir / "atelier_root_on"
  off_root = bench_run_dir / "atelier_root_off"
  ```
- Pass the root explicitly via `ATELIER_ROOT=<path>` env var when launching each subprocess.
- After each replication, delete or snapshot the arm's root to prevent cross-replication contamination.

**Detection:** Run `mode=off` twice; check that `cost_history.json` is empty on the second run (no on-arm entries). Run `diff` of the two arm root directories after a sweep.

---

### Pitfall 2: Module-Level Singletons Survive Between Arms (In-Process Runs)

**What goes wrong:**
If the bench runner imports `mcp_server` and calls tool handlers directly (rather than launching subprocesses), the following module-level singletons persist for the lifetime of the Python process and contaminate subsequent arms:

- `_current_ledger: RunLedger | None = None` (line 153)
- `_realtime_ctx: RealtimeContextManager | None = None` (line 154)
- `_runtime_cache: ContextRuntime | None = None` (line 528)
- `_cached_claude_session_id: str = ""` (line 167)
- `_cached_mcp_model: str = ""` (line 168)
- `_product_session_started_at: float | None = None` (line 156)
- `_last_plan_hash_by_session: dict[str, str] = {}` (line 157)

**Why it happens:**
These are lazy singletons initialized on first access. `_get_realtime_context()` returns the same `RealtimeContextManager` object across all tool calls until the process restarts. `_get_ledger()` reuses the same `RunLedger` which accumulates events from both arms into the same session.

`_reset_runtime_cache_for_testing()` exists (line 539) and clears these, but it must be called explicitly between arm runs. It is not called automatically between bench replications.

**Consequences:** arm A's ledger events appear in arm B's session report. Arm B's startup metrics include arm A's timing data.

**Prevention:**
- Always run each bench arm as a **subprocess**, never in-process. The subprocess boundary guarantees module-level state is fresh.
- If in-process testing is required (unit tests), call `_reset_runtime_cache_for_testing()` before each arm.

**Detection:** Check that `len(_get_ledger().events)` is 0 at arm start.

---

### Pitfall 3: `ATELIER_BENCH_MODE=off` — Subtle Hooks That Survive

**What goes wrong:**
Even with `ATELIER_BENCH_MODE=off` properly set, Atelier behavior can leak in through side channels:

**(a) Tool registration at import time:**
All `@mcp_tool`-decorated functions are registered into the module-level `TOOLS` dict at import time, before `ATELIER_BENCH_MODE` is read. The dict is populated unconditionally. `mcp_tool_visible_to_llm()` controls whether tools appear in the MCP tool-list response, but the handlers are still present and callable if the agent happens to call them by name.

**(b) `ATELIER_DEV_MODE` interplay:**
`mcp_tool_visible_to_llm()` returns `True` for all tools when `ATELIER_DEV_MODE=1`, overriding any bench-mode visibility filtering. If the benchmark runner has `ATELIER_DEV_MODE=1` set in its environment (common in dev/CI), dev tools will be exposed to the agent even when bench mode claims to be off.

**(c) `CLAUDE_CODE` env var agent detection:**
The agent detection code (line ~188 in `mcp_server.py`) reads `os.environ.get("CLAUDE_CODE")`. If the benchmark is being run _by_ Claude Code (the common case for a coding agent benchmark), this env var is set, and `_detect_agent()` returns `"claude"`. This means Atelier's Claude-specific session bridging, telemetry, and session-start hooks fire even under `off` mode unless explicitly guarded.

**(d) Auto-update can mutate code mid-benchmark:**
`_check_auto_update()` in `mcp_server.py` runs `git pull` if it hasn't run in the last hour. If the benchmark sweep takes more than an hour, Atelier's code can change mid-run, making early and late replications incomparable.

**(e) Existing memory store on disk:**
If a developer has been using Atelier and has memory blocks in `~/.atelier/` (the default ATELIER_ROOT), and the off-arm runs with the default root, it will still load those memory blocks when `create_store(root)` is called. The off-arm is not naive — it has accumulated memory.

**Prevention:**
- The `bench/mode.py` module (D1) must read `ATELIER_BENCH_MODE` **at startup**, before any other module is imported, and install shims that make ALL pathways (store creation, memory read, compaction, routing) no-ops.
- Use a fresh empty temp directory as ATELIER_ROOT for the off-arm.
- Set `ATELIER_NO_AUTO_UPDATE=1` for the entire benchmark sweep.
- In CI, clear `ATELIER_DEV_MODE` before running off-arm.
- Verify with a smoke test: `ATELIER_BENCH_MODE=off atelier --version` should produce zero ledger events in `runs/` (check the empty directory).

**Detection:** After an off-arm run: `ls {off_root}/runs/ | wc -l` should be 0. `ls {off_root}/session_stats/ | wc -l` should be 0.

---

### Pitfall 4: tiktoken `cl100k_base` Is the Wrong Tokenizer for Claude

**What goes wrong:**
`benchmarks/mcp_tools/harness.py` (line 14-18) uses `tiktoken.get_encoding("cl100k_base")` — GPT-4's BPE tokenizer — to count tokens. Claude uses Anthropic's own tokenizer (based on different BPE rules). The counts diverge by 10-30% depending on content type (code, prose, JSON).

**Why it happens:**
tiktoken is easily installable and familiar. The cl100k_base encoding is a widely-cited approximation. But Claude's tokenizer is not cl100k_base.

**Consequences:**
- Token counts in all existing `mcp_tools/` benchmarks are systematically wrong for Claude.
- Cost estimates derived from these counts are wrong by the same 10-30%.
- If the on-arm and off-arm have different content types (on-arm returns compact JSON summaries; off-arm returns verbose prose), the error is asymmetric and exaggerates or understates the savings.

**The correct approach:**
Read token counts from the Anthropic API response `usage` object — it is authoritative and free:
```python
# From the API response (claude -p with --output-format json, or direct API call)
usage = response.usage
input_tokens = usage.input_tokens          # actual tokens consumed
output_tokens = usage.output_tokens        # actual tokens generated
cache_write_tokens = (
    usage.cache_creation_input_tokens      # billed at 125% of input rate
    # or in newer API: usage.cache_creation.ephemeral_1h_input_tokens
    #                + usage.cache_creation.ephemeral_5m_input_tokens
)
cache_read_tokens = usage.cache_read_input_tokens  # billed at ~10% of input rate
```

Anthropic also exposes `/v1/messages/count_tokens` for pre-flight estimation without making an actual call — useful for cost caps.

**Prevention:**
- Never use tiktoken for Claude token counting in published benchmarks.
- Parse `usage` from every API response; record all four token categories.
- If using `claude -p` subprocess with `--output-format stream-json`, the final streamed event contains usage data.

---

### Pitfall 5: Simulated Token Counts in `benchmarking.py`

**What goes wrong:**
`benchmarks/swe/../infra/runtime/benchmarking.py`'s `run_runtime_benchmark()` (lines 49-54) uses **hardcoded constants** to simulate token savings:
```python
saved_per_lesson_in = 350   # FICTION
saved_per_lesson_out = 100  # FICTION
base_in, base_out = 4000, 1500  # FICTION
```
This is not measurement — it is theatre. The "savings" it reports are algebraically computed from made-up starting values, not from actual API calls.

**Why it happens:**
This function was designed for offline cost-model validation, not as a source of publishable benchmark data. It exists to estimate _potential_ savings given a model. It was never intended to be the foundation of external benchmark claims.

**Consequences:** Publishing savings numbers derived from this function would be fraudulent. The function runs entirely without API calls.

**Prevention:**
- D2/D3 must capture token counts from actual `claude -p` subprocess runs via the `usage` field in the API response.
- The existing `run_runtime_benchmark()` should be clearly marked as a simulation/smoke test, not a benchmark source.
- Any publication pipeline must verify that token counts come from `source: "api_usage"`, not `source: "estimated"`.

---

### Pitfall 6: Benchmark P-Hacking via Post-Hoc Task Selection

**What goes wrong:**
Run the benchmark against 30 TerminalBench tasks. Pick the 10 where Atelier-on wins most convincingly. Publish only those 10. The headline "Atelier saves 22% on cost, maintains quality" is technically true for those 10 tasks but false for the population.

**Why it happens:**
Incentive to publish wins. Easy to rationalize ("we selected representative tasks"). With 30 tasks and 3 metrics, you have 90 opportunities to find a "significant" result by chance.

**Consequences:** When independent researchers try to reproduce on the other 20 tasks, results look nothing like the claim. Credibility destroyed.

**Prevention:**
- **Pre-register the task subset before running.** Commit `benchmarks/terminalbench/tasks.yaml` with the 10 task IDs before the first benchmark run. The selection criterion must be stated in the methodology section (e.g., "first 10 alphabetically", "random sample seeded with SHA of today's date").
- Never re-run the benchmark and "select the better seed".
- Report aggregate results across all tasks in the task list, including tasks where Atelier-on lost.
- If you iterate on the system and re-run, the old results must be preserved and the new run must use the same pre-registered task list.

**Detection warning sign:** "We ran additional experiments to validate" after seeing the first results, without pre-committing the selection criteria.

---

### Pitfall 7: N=1 Appears to Have CIs But Is Actually Noise

**What goes wrong:**
Run 1 replication per cell. Report results with a table but no confidence intervals. Readers assume stability. In reality, LLM agent pass-rate has ~10-30% variance run-to-run at temperature > 0, and even at temperature=0, multi-GPU Anthropic inference is not guaranteed deterministic.

**Consequences:** A result that looks like "Atelier saves $0.42 per run" based on N=1 could easily be "Atelier saves $0.10 or costs $0.15 more" on the next run due to variance.

**Prevention:** N ≥ 5 per cell, as already specified in the plan. This is the minimum. For tasks with binary pass/fail, N=5 gives a Wilson 95% CI width of ~40 percentage points for a 60% pass-rate — wide, but honest.

---

## Critical Pitfall (Statistics)

### Pitfall 8: Wrong CI Formula for Pass-Rate (Binary Metric)

**What goes wrong:**
Using the normal approximation confidence interval for a binary pass-rate:
```python
# WRONG — Wald/normal approximation
p_hat = k / n
ci_half = 1.96 * sqrt(p_hat * (1 - p_hat) / n)
ci = (p_hat - ci_half, p_hat + ci_half)
```
This fails at boundaries (k=0: CI=[0,0]; k=n: CI=[1,1]) and is unreliable at small N. With N=5 and k=5, this gives CI=[1.0, 1.0] — mathematically impossible (confidence interval that contains only one point).

**The correct formula — Wilson score interval:**
```python
from scipy.stats import proportion_confint
lo, hi = proportion_confint(k, n, alpha=0.05, method='wilson')
# Or manually:
z = 1.96  # 95% CI
n_eff = n + z**2
p_tilde = (k + z**2 / 2) / n_eff
margin = z * sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) / (1 + z**2 / n)
ci = (p_tilde - margin, p_tilde + margin)
```
With N=5, k=5: Wilson gives CI≈[0.478, 1.0]. With N=5, k=0: Wilson gives CI≈[0.0, 0.522]. These are honest bounds that acknowledge small-N uncertainty.

**Why it matters for this project:**
The D4 aggregate report must use Wilson intervals for pass-rate. The D3 `summary.json` must store raw (k, n) counts, not just p_hat, so Wilson intervals can be computed at report time.

**Prevention:**
- Store `{"passed": k, "total": n}` for every (task, mode) cell in `summary.json`.
- Use `scipy.stats.proportion_confint(k, n, method='wilson')` for all pass-rate CIs in D4.
- Never use `mean ± 1.96 * std / sqrt(n)` for a binary metric.

---

## Moderate Pitfalls

### Pitfall 9: "Losses Section: No Losses" is Not Credible Without Transparency

**What goes wrong:**
The losses section says "No losses recorded in this run" with N=5 × 10 tasks = 50 cells × 3 metrics = 150 comparison points. This is statistically suspicious and signals either cherry-picking or that the loss definition is too narrow.

**What the losses section must actually contain to be credible:**

1. **Explicit loss definition:** "A loss is any cell where Atelier-on had a worse mean value than Atelier-off, regardless of CI overlap." (This is the strict definition. Looser: "where CI for Atelier-on is entirely above Atelier-off for cost or latency, or entirely below for quality".)

2. **When there genuinely are no losses:** State the full counts:
   > "Across 50 cells (10 tasks × 2 metrics with objective ordering: cost and latency), Atelier-on had a lower mean cost in 10/10 tasks and lower mean latency in 8/10 tasks. In 2 tasks (IDs: `task_07`, `task_09`), Atelier-on was slower (mean +1.4s, +0.9s). Quality pass-rate was equal or better in all 10 tasks. No cell showed quality regression."
   This is credible. The empty "No losses" alone is not.

3. **Every loss row must link to the raw transcript** so readers can verify.

4. **Hypothesis for each loss:** Even a sentence ("likely because task_07 is a single-file read where compaction overhead dominates") adds credibility.

**The anti-pattern that destroys trust:**
- Losses section that says "0 losses" but the task list was selected after seeing results.
- Losses section that counts only "losses on all three metrics simultaneously" (near-impossible to trigger).
- Losses section hidden at the bottom of the page after five paragraphs of wins.

**Prevention:**
The losses section should be the second section in the report (right after the headline table), not the last. Readers who jump to losses first are exactly the skeptical audience you need to persuade.

---

### Pitfall 10: PR-Replay — Non-Deterministic Checkout and Git State Contamination

**What goes wrong:**
The `--pr <url>` benchmark checks out the PR's base commit, runs the agent, then compares the output diff to the actual merge. Several git-level failures can silently corrupt this:

**(a) Shared worktree:** Both arms (on/off) run in the same git worktree. The first arm's tool calls (`git checkout`, file edits) leave the tree in a modified state when the second arm starts. The second arm sees uncommitted changes that weren't part of the PR.

**(b) Non-pinned checkout:** `git fetch origin pull/<N>/head && git checkout FETCH_HEAD` fetches the _current_ state of the PR branch. If the author force-pushed since the PR was opened, the benchmark uses a different diff than the one you think you're testing against.

**(c) Closed PR inaccessibility:** Some git hosts expire pull/<N>/head refs for closed/merged PRs. Fetching a specific merge SHA directly is more reliable.

**(d) Ground truth ambiguity:** The "real merge" may be a squash commit that contains more changes than any individual PR commit, or it may have been modified by a reviewer's `--amend`. The quality score ("diff matches merge") will penalize the agent for not guessing the reviewer's edits.

**Prevention:**
- Each arm must clone the repo to a fresh temp directory with `git clone --no-local`.
- Pin the base commit SHA in `config.json` at benchmark start, not at run time.
- Store the base SHA and merge SHA in `summary.json` so reproduction is exact.
- For quality scoring: compare against the PR's first-parent merge commit SHA (not the branch tip), and note in the methodology that reviewer amendments are excluded from the ground truth.
- Add a `--pr-sha` override flag that takes a specific commit SHA rather than a PR URL.

---

### Pitfall 11: Non-Determinism from Date/Time Injection in Context

**What goes wrong:**
Multiple paths in Atelier inject wall-clock timestamps into content that the model sees:
- `_append_savings()` in `mcp_server.py` (line 751): `ts = datetime.utcnow().isoformat()` written to JSONL files.
- Every `RunLedger` event includes `at = datetime.now(UTC).isoformat()`.
- `RealtimeContextManager._append()` records `_now_iso()`.
- Claude Code's own system prompt includes today's date.

If any of this date-stamped content is injected back into the agent's context during an `on`-arm run (via `tool_context`, `tool_compact`, or `tool_memory`), then:
- Two on-arm replications run on different days will see different dates in their context.
- An on-arm run at 11:59 PM and an off-arm run at 12:01 AM will have different date contexts.
- Seeded determinism (`--seed 42`) cannot suppress wall-clock time.

**Prevention:**
- Pin a `ATELIER_BENCH_DATE` env var (a fake fixed date) for the entire benchmark sweep, and make Atelier read from it instead of `datetime.now()` when bench mode is active.
- Alternatively, scrub timestamps from injected context before benchmarking (the `compact` output should not include `at` fields if they'll vary run-to-run).
- Record the wall-clock timestamp of each run in `config.json` so post-hoc analysis can identify time-of-day effects.

---

### Pitfall 12: Prompt Caching Asymmetry Between Arms

**What goes wrong:**
Anthropic's prompt caching (5-minute and 1-hour TTLs) means that if the on-arm and off-arm run against the same system prompt, the second arm to run may get a cache hit (billed at ~10% of normal input rate) while the first arm paid full price. This makes cost comparison arm-order-dependent.

The off-arm typically uses a shorter system prompt (no Atelier context injected), so even if both arms hit the cache, they'll have different cached prefixes of different lengths, leading to different cache read token counts.

**Consequences:**
- If on-arm runs first, off-arm may inherit cached tokens from the same system prompt prefix.
- If you run all 5 on-arm reps then 5 off-arm reps, the off-arm reps benefit from warm cache on the base model system prompt — the on-arm reps didn't.
- The cost difference is not just "Atelier saves tokens via compression" but also "we measured the arms at different cache temperatures".

**Prevention:**
- Interleave arm executions: rep 1 (on), rep 1 (off), rep 2 (on), rep 2 (off)... This ensures both arms face the same cache temperature at each replication index.
- Wait at least 5 minutes between reps to let the 5-minute cache expire, OR accept that cache effects are present and note this in the methodology.
- Report `cache_read_input_tokens` separately in the summary table so readers can see how much of the cost difference is from cache hits vs. compression.

---

### Pitfall 13: Leakage via Shared Embedding Index / Zoekt Index

**What goes wrong:**
Atelier's search capability uses embeddings stored in `{ATELIER_ROOT}/embeddings/` and potentially a Zoekt code index. If both arms share a root and the on-arm triggers an embedding indexing operation (common on first `tool_search` call), the off-arm's subsequent search will benefit from the already-built index — which was built during the on-arm run.

**Prevention:**
- Separate ATELIER_ROOT per arm (already covers this).
- Also separate the Zoekt index directory (check `ATELIER_ZOEKT_ROOT` env var or the configured path in `src/atelier/infra/code_intel/`).

---

## Minor Pitfalls

### Pitfall 14: Docusaurus Blog Rendering — `blog: false` Must Be Enabled

**What goes wrong:**
The current `docs-site/docusaurus.config.ts` has `blog: false` in the preset options. Attempting to publish to `docs-site/blog/` will produce no output — the blog feature is disabled.

**Prevention:**
Enable the blog in the config and configure it:
```typescript
blog: {
  showReadingTime: true,
  blogSidebarTitle: 'All benchmarks',
  blogSidebarCount: 'ALL',
},
```
Also add a blog link to the navbar, or the post will be unreachable from the nav.

---

### Pitfall 15: MDX Parsing Breaks on Embedded JSON/Curly Braces

**What goes wrong:**
Docusaurus uses MDX (by default) for blog posts. MDX treats `{...}` and `<tag>` as JSX syntax. A benchmark report that embeds raw JSON (e.g., a summary statistics block) or uses angle brackets will fail the build with a cryptic parse error.

Examples that break MDX:
```markdown
{"task": "fix_bug", "mode": "on", "cost": 0.042}   ← broken: JSX expression
<task_07>transcript</task_07>                        ← broken: JSX tag
```

**Prevention:**
- Name blog post files with `.md` extension, not `.mdx`. Configure the blog plugin with `format: 'detect'` (Docusaurus v3.x default handles this automatically by extension).
- Or: wrap all JSON blocks in fenced code blocks with a language tag.
- Or: use an HTML entity for `<` → `&lt;` in non-code sections.

**`onBrokenLinks: "throw"` note:**
The config has `onBrokenLinks: "throw"`. Any transcript link that references a file not committed alongside the blog post will hard-fail the build. All `transcripts/` files must be committed in the same PR as `index.md`.

---

### Pitfall 16: Image Paths Break for Non-Folder Blog Posts

**What goes wrong:**
If the blog post is a flat file `blog/2026-06-04-terminalbench.md` (not a folder), relative image paths like `./plots/cost_delta.png` won't resolve — the PNG is not co-located.

The D5 spec correctly uses the folder approach (`docs-site/blog/<slug>/index.md`), which means relative image paths work. But the flat-file mistake is easy to make if a developer manually creates the post.

**Prevention:**
Always use the folder structure for blog posts containing images:
```
docs-site/blog/2026-06-04-terminalbench-claude-sonnet/
├── index.md
├── plots/
│   ├── cost_delta.png
│   ├── latency_delta.png
│   └── quality_delta.png
└── transcripts/
    └── task_01__on__rep1.json
```

---

### Pitfall 17: Missing `<!-- truncate -->` Produces Useless Post Previews

**What goes wrong:**
Docusaurus uses the content before the first `<!-- truncate -->` marker as the blog list preview. Without it, Docusaurus uses the entire post content (or a hard-truncated 300-character snippet). A benchmark post that starts with a methodology section will show 300 characters of boilerplate ("This report covers TerminalBench × Claude Sonnet × 10 tasks...") instead of the headline finding.

**Prevention:**
The report template should have a compelling 2-3 sentence summary _before_ the methodology, followed by `<!-- truncate -->`:
```markdown
Atelier reduced token costs by 18% (95% CI: 11–25%) while maintaining equivalent
pass-rate (Δ = +2%, CI: -8% to +12%) across 10 TerminalBench tasks × 5 replications.
**2 of 10 tasks showed higher latency under Atelier-on** (see losses section).

<!-- truncate -->

## Methodology
...
```

---

### Pitfall 18: Rate Limits Silently Truncate Long Benchmark Sweeps

**What goes wrong:**
A full sweep (10 tasks × 2 modes × 5 reps = 100 agent runs) hitting the Anthropic API will likely encounter rate limits (tokens-per-minute or requests-per-minute). The default behavior of `claude -p` when rate-limited is to retry with exponential backoff — which can make individual runs take 10× longer or time out silently.

**Consequences:**
- Some reps take 30 seconds; others take 8 minutes (waiting out rate limit). The latency distribution for those reps is not comparable.
- If a rep times out and returns an error, the harness may record it as a failure, artificially deflating pass-rate for one arm.

**Prevention:**
- Add a `--max-cost-per-run $5` guard that aborts individual runs that are clearly runaway.
- Parse `claude -p` exit codes and distinguish between "model refused" (failure), "rate limited / timed out" (retry), and "tool error" (should be recorded separately).
- Log the actual wall-clock start and end time for every run, and flag runs that took >3× the median as "suspect".
- Stagger rep starts with a minimum 30-second gap between runs to smooth rate limit pressure.

---

## Phase-Specific Warnings

| Deliverable | Likely Pitfall | Mitigation |
|-------------|----------------|------------|
| D1 — bench-mode toggle | Off-mode passthrough not covering `RealtimeContextManager` startup (it loads from disk in `__init__`, before any tool is called) | Guard `_load()` in `RealtimeContextManager` with bench-mode check; or use separate ATELIER_ROOT |
| D1 — bench-mode toggle | `_emit_mcp_session_start()` fires telemetry even in off-mode, coupling both arms to same product session | Suppress telemetry when `ATELIER_BENCH_MODE` is set (either `on` or `off`) |
| D2 — TerminalBench adapter | `agent_adapter.py` inherits parent env vars when spawning `claude -p`; ATELIER_ROOT, ATELIER_DEV_MODE, CLAUDE_CODE, etc. bleed through | Explicitly construct the subprocess env dict, whitelisting only needed vars; block Atelier env vars for off-arm |
| D3 — A/B runner | Prompt cache asymmetry when arms are batched (all on-arm reps, then all off-arm reps) | Interleave reps: on/rep1, off/rep1, on/rep2, off/rep2 |
| D3 — A/B runner | Resumability: if a sweep is killed and resumed, re-run IDs may collide with cached partial results | Hash the (task_id, mode, rep_index, seed, commit_sha) as the idempotency key |
| D4 — report | Wilson CI not used for pass-rate; Wald CI used instead | `summary.json` stores raw `{passed, total}` not `p_hat`; report.py uses `scipy.stats.proportion_confint(..., method='wilson')` |
| D4 — report | Losses section empty but no explanation | Losses section always shows counts: "N cells compared; X showed Atelier-on worse on cost; Y on latency; Z on quality" |
| D5 — publication | `blog: false` in docusaurus.config.ts — publish command creates files, site build silently omits them | Enable blog in config as part of D5 |
| D5 — publication | Embedded JSON in report.md breaks MDX parser | Use `.md` extension for blog post; wrap stats in fenced code blocks |
| D6 — long-session | Recall-rubric grader for 200-turn sessions will itself be a long-context LLM call — token costs for the grader must be tracked separately and not attributed to the bench run | Separate API key / cost bucket for the grader; do not sum grader tokens into bench cost |
| D7 — `atelier bench` CLI | `--quick` mode (N=2) produces Wilson CIs that span nearly [0, 1] — uninformative; user may interpret them as precise | Print a clear warning: "N=2 produces wide uncertainty bounds; use --full for publishable results" |
| PR-replay | Ground truth diff scored by semantic similarity may reward verbose agents (more lines = more overlap) even if quality is lower | Use a rubric-based grader that rewards correctness, not line-count overlap |

---

## Anti-Patterns Checklist

Before publishing any benchmark result, verify:

- [ ] Task subset was committed to `tasks.yaml` **before** the first run (not cherry-picked post-hoc)
- [ ] Each arm used a separate `ATELIER_ROOT` (no shared filesystem state)
- [ ] Each arm ran as a subprocess (no shared module-level singleton state)
- [ ] Token counts came from `usage` field of API response, not `tiktoken`
- [ ] Pass-rate CI uses Wilson score interval (`method='wilson'`), not Wald/normal approximation
- [ ] N ≥ 5 replications per cell
- [ ] Arms were interleaved (rep-by-rep, not batch-by-batch) to equalize cache temperature
- [ ] Losses section shows counts even when zero, with per-task breakdown
- [ ] `ATELIER_NO_AUTO_UPDATE=1` was set for the entire sweep
- [ ] `reproduce.sh` was tested on a fresh clone (by a second developer, not the benchmark author)
- [ ] Blog post uses folder structure with relative image paths
- [ ] `<!-- truncate -->` placed after the key finding summary
- [ ] `blog: false` changed to `blog: {...}` in docusaurus config

---

## Sources

- Direct code inspection: `src/atelier/gateway/adapters/mcp_server.py` (lines 85-167, 153-157, 220-260, 528-549, 700-800, 1006-1080, 4585-4593) — HIGH confidence
- Direct code inspection: `src/atelier/infra/runtime/benchmarking.py` (lines 49-54, simulated tokens) — HIGH confidence
- Direct code inspection: `benchmarks/mcp_tools/harness.py` (lines 14-18, tiktoken cl100k_base) — HIGH confidence
- Direct code inspection: `docs-site/docusaurus.config.ts` (`blog: false`) — HIGH confidence
- Anthropic SDK Python docs via Context7 (`/anthropics/anthropic-sdk-python`): `usage` object fields including `cache_creation_input_tokens`, `cache_read_input_tokens`, `CacheCreation` model with `ephemeral_1h_input_tokens` + `ephemeral_5m_input_tokens` — HIGH confidence
- Docusaurus docs via Context7 (`/facebook/docusaurus`): blog front matter, `<!-- truncate -->`, folder-based blog posts, MDX parsing — HIGH confidence
- Wilson score interval: `scipy.stats.proportion_confint(k, n, alpha=0.05, method='wilson')` — HIGH confidence (standard statistics, widely documented)
- Anthropic API non-determinism at temperature=0: known property of multi-GPU inference — MEDIUM confidence (widely reported, not formally documented)
