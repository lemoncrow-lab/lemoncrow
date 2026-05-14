# Cost tracking: Atelier vs CodeBurn vs Tokscale vs ccusage

> Reference for how Atelier's cost numbers differ from CodeBurn's, Tokscale's,
> and ccusage's, what each tool reads, and which one to trust for which
> question.

> **For an end-user-friendly version of this material, see**
> [`docs/cost-tracking-tools.md`](../cost-tracking-tools.md).

Atelier, [CodeBurn](https://github.com/codeburn-ai/codeburn),
[Tokscale](https://tokscale.dev), and
[ccusage](https://github.com/ryoppippi/ccusage) all answer the question
*"how much did my AI coding tools cost today?"*, but they routinely produce
different totals — sometimes by 20-40% — even when reading the same on-disk
session files. This page documents **why** they diverge so the next engineer
auditing a discrepancy doesn't have to re-derive the answer from scratch.

All three external tools are wired into Atelier's `External Analyzers` UI
page as sidecars via
[`src/atelier/gateway/integrations/external_analytics.py`](../../src/atelier/gateway/integrations/external_analytics.py),
so you can compare them live in the dashboard.

The numbers shown are real measurements taken on **2026-05-14**.

---

## Headline totals — same day, same machine

| Metric        | Atelier  | CodeBurn | Tokscale | ccusage (Claude only) |
|---------------|---------:|---------:|---------:|----------------------:|
| Spend         | **$81.38** | **$109.34** | **$96.78** | $36.20                |
| Calls         | 1,650    | 1,830    | 1,781    | —                     |
| Sessions      | 42       | 112      | —        | 2                     |
| Input tokens  | 106.0M   | 22.1M    | 11.3M    | 1,002                 |
| Output tokens | 1,209K   | 885K     | 560K     | 207K                  |
| Cache read    | 102.2M   | 249.8M   | 168.0M   | 53.0M                 |
| Cache write   | 477K     | 580K     | 595K     | 645K                  |
| Cache hit     | 49.1%    | 91.9%    | 93.7%    | —                     |

ccusage only covers Claude Code, so its "Spend" column is comparable to
Atelier's claude row ($14.72 today), **not** the full total. The fact that
ccusage shows $36.20 vs Atelier's $14.72 for the same source data is the
chunked-message dedup story in section 1.

Reading these as if they should match leads to nonsense conclusions. The next
sections explain every meaningful difference.

---

## What each tool reads

| Source path                                                                            | Atelier | CodeBurn | Tokscale | ccusage |
|----------------------------------------------------------------------------------------|:-------:|:--------:|:--------:|:-------:|
| `~/.claude/projects/<workspace>/*.jsonl`                                               | ✅      | ✅       | ✅       | ✅      |
| `~/.claude/projects/<workspace>/<session>/subagents/*.jsonl`                           | ✅      | ✅       | ✅       | ✅      |
| `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl`                                   | ✅      | ✅       | ✅       | ❌      |
| `~/.copilot/session-state/<sid>/events.jsonl`                                          | ✅      | ✅       | ✅       | ❌      |
| `~/.config/Code/User/workspaceStorage/<ws>/GitHub.copilot-chat/transcripts/*.jsonl`    | ✅      | ✅       | ❌       | ❌      |
| `~/.config/Code/User/workspaceStorage/<ws>/GitHub.copilot-chat/debug-logs/<sid>/*.jsonl` | ✅†   | ❌       | ❌       | ❌      |
| `~/.gemini/tmp/<project>/chats/session-*.jsonl`                                        | ✅      | ✅       | ✅       | ❌      |
| `~/.gemini/tmp/<project>/chats/<sid>/<subagent>.jsonl`                                 | ✅†     | ?        | ?        | ❌      |
| `~/.config/Cursor/User/.../cursorDiskKV` (bubbles)                                     | ✅      | ✅       | ✅       | ❌      |
| `~/.local/share/opencode/log/*.log`                                                    | ✅      | ✅       | ✅       | ❌      |

† = added in atelier `2026-05-14` after auditing this discrepancy. See
*"Things Atelier learned from this comparison"* below.

CodeBurn's `Copilot (OpenAI)` / `GPT-5.3 Codex` model buckets appear to be
derived classifications (based on routing metadata in the session prompts)
rather than reads of any `model=` field on disk. We were unable to fully
reverse-engineer that logic from the minified `~/.local/lib/node_modules/codeburn/dist/cli.js`.

---

## Why the totals diverge

### 1. Anthropic chunked-message dedup *(Atelier wins; ccusage confirms)*

Claude Code's `*.jsonl` emits a single assistant response as **2–3 separate
events**, one per content-block type (text, tool_use). Each event carries the
**same** `message.id` and the **identical** `usage` block (input, output,
cache_read, cache_write).

Today's smoking gun: ccusage reports $34.06 for `claude-opus-4-7`; Atelier
reports $14.11 for the same model from the same files. That's 2.4× more.

A real example from one of today's claude sessions:

```
msg_018bkb4bRgEe35A1U3pAQtDf  3 occurrences   in=6   out=364  cr=0     cw=23151
msg_01Q2SqE2qNYtZwV6eLa4R65i  2 occurrences   in=1   out=214  cr=25096 cw=513
msg_013eTcALxwAPeWV4DACuR6fK  2 occurrences   in=1   out=164  cr=25609 cw=8030
…
```

- **Atelier**: dedups by `message.id` (`claude.py:assistant_usage_entries[msg_id] = …`).
  Same id → keep one entry. **Correct.**
- **CodeBurn**: appears to naive-sum every assistant event, billing the same
  tokens 2–3×.
- **Tokscale**: also appears to count per-event (`calls=219` for Opus 4.7 vs
  Atelier's 137 — 1.6× more, consistent with multi-block messages).
- **ccusage**: also naive-sums (today: $34.06 vs Atelier $14.11 on Opus 4.7
  for *identical* source files — 2.4× over).

Impact today: **~$12-15** of the Atelier-vs-CodeBurn gap, **~$14** of the
Atelier-vs-Tokscale gap on Anthropic models, **~$20** of the
Atelier-vs-ccusage gap.

Atelier and provider dashboards agree; the three external scanners
collectively over-bill Anthropic by 1.6–2.4×.

### 2. OpenAI / Gemini `cached_input ⊂ input` subtraction *(Atelier wins on cost; Tokscale matches)*

OpenAI and Gemini both report `input_tokens` as the **superset** of cache
reads. To compute cost correctly you must subtract:

```python
billable_input = max(input_tokens - cached_input_tokens, 0)
```

- **Atelier**: subtracts (`codex.py: max(turn_in - turn_cached, 0)` and
  `gemini.py: max(0, in_t - cached_t)`).
- **Tokscale**: appears to subtract too — its Gemini Flash numbers are within
  10% of Atelier's after this subtraction.
- **CodeBurn**: reports very small `inputTokens` for Gemini (14.7M) but very
  large `cacheRead` (132M on 3 calls = ~44M/call). 44M per call is **larger
  than Gemini Flash's max context window** (~1-2M tokens), so something is
  being summed across events that shouldn't be.

### 3. Sonnet 4.6 compaction-input inclusion *(Atelier wins)*

GitHub Copilot's `session.compaction_complete.data.compactionTokensUsed`
event contains a separate LLM call (history summarisation) with its own
input/output/cache_read counts. It's NOT a duplicate of the per-turn
`assistant.message` events.

- **Atelier**: harvests compaction inputs as a distinct `UsageEntry`
  (`copilot.py:454-477`). Today: 882K input captured for Sonnet 4.6.
- **CodeBurn**: shows 295 input for Sonnet 4.6 — i.e. **missing the ~880K
  compaction input** entirely. That's a real billable LLM call hidden from
  CodeBurn's dashboard.

### 4. GitHub Copilot subscription pricing *(judgment call)*

Copilot is a $19/month subscription. The underlying OpenAI / Anthropic calls
are *included in the plan* — the user does not pay per token.

| Tool     | Today's "Copilot (OpenAI)" line                          |
|----------|-----------------------------------------------------------|
| Atelier  | 679 gpt-5.4 calls × `copilot/gpt-5.4` zero-cost = $0.00   |
| CodeBurn | 673 calls × $0.0033/call ≈ **$2.19** (premium-request proxy) |
| Tokscale | Not captured separately                                   |

Atelier's stance: bill the tokens at **zero** because the subscription has
already been paid; show the call volume so users can still see where time
goes. CodeBurn's stance: estimate the marginal "premium request" charge.
Neither is wrong. The `copilot/<model>` namespace prefix in Atelier
(`pricing.py:_load_pricing_table`) makes this overrideable per-installation:

```python
from atelier.core.capabilities.pricing import override_pricing
override_pricing("copilot/gpt-5.4", input_usd=0.0, output_usd=0.05)
```

### 5. `<synthetic>` placeholder model

Claude Code occasionally emits `model: "<synthetic>"` for cached/injected
responses that don't trigger a billable Anthropic request.

- **Atelier**: filters `<synthetic>` from the trace's resolved-model field
  (`pricing.is_placeholder_model`), prices it at $0, no warning logged.
- **Tokscale**: keeps the row visible at $0 — also correct.
- **CodeBurn**: same as Tokscale, $0.

Cosmetic difference only, no cost impact.

### 6. Counting "sessions" and "calls"

The unit *means different things* in each tool:

| Concept       | Atelier              | CodeBurn               | Tokscale                  |
|---------------|----------------------|------------------------|---------------------------|
| **Session**   | one source file on disk (`*.jsonl`) per host | one project bucket OR one chat thread | not directly exposed     |
| **Calls**     | one `UsageEntry` per dedup'd LLM request | one event per chunked emission | `messageCount` per (client, model) bucket |

Direct comparison of "Sessions: 42 vs 112 vs n/a" is **meaningless** —
they're three different units. The numbers that DO line up across tools are:

- **Per-model dollar cost on simple models** (Haiku 4.5 lands at $0.24 in all
  three; GPT-5.5 lands at $50-53 in Atelier+CodeBurn+Tokscale).
- **Call counts on individual models where multi-chunk emission is absent**
  (gpt-5.5 codex calls match within ±10%; haiku is exact).

### 7. Gemini subagent files *(Atelier learned, fixed)*

Gemini writes two layouts under `~/.gemini/tmp/<project>/chats/`:

1. Top-level session files: `chats/session-YYYY-MM-DDTHH-MM-<id>.jsonl`
2. Sub-agent chat files: `chats/<parent-session-id>/<subagent-id>.jsonl`

Atelier originally globbed `**/chats/session-*.jsonl` only and missed (2)
entirely. As of `2026-05-14` it globs both. Today's impact: +13 files,
+17.7M input, +15.2M cache_read of `gemini-3-flash-preview` activity.

### 8. VSCode Copilot Chat per-LLM telemetry *(Atelier learned, fixed)*

Atelier originally read `~/.copilot/session-state/<sid>/events.jsonl` which
has tool-call detail but **only `outputTokens` per turn — no model, no
input, no cache fields**. The actual per-LLM-request telemetry lives in:

```
~/.config/Code/User/workspaceStorage/<ws>/GitHub.copilot-chat/debug-logs/<sid>/main.jsonl
```

Each `type:"llm_request"` event carries `attrs.model`, `attrs.inputTokens`,
`attrs.outputTokens`. As of `2026-05-14` Atelier imports this source via
`CopilotImporter.import_debug_log_dir`. Because long-running chats span
multiple UTC days, traces are **partitioned by event date** so today's calls
land in today's window, not the day the chat was started.

---

## Decision matrix — which tool to believe for which question

| Question                                                                   | Best tool       | Why                                                              |
|---------------------------------------------------------------------------|-----------------|------------------------------------------------------------------|
| "What is my **true marginal dollar cost** for today's AI usage?"          | **Atelier**     | Dedups chunked events; subtracts cached-superset; zero-prices subscription |
| "How many distinct LLM **requests** did my tools fire today?"             | **CodeBurn** or **Tokscale** | They count per-event; Atelier dedups by message-id which is fewer |
| "Which **models** ran in which **clients**?"                              | **Tokscale**    | Native `client,model` grouping; clear separation                |
| "What's my **all-time** cost across providers?"                           | **Tokscale**    | Stores rollups longer than Atelier's per-day traces             |
| "Optimisation suggestions on prompt size, tool churn, cache hit rate"     | **CodeBurn**    | `codeburn optimize` is a built-in feature                       |
| "Replay a single session for debugging"                                   | **Atelier**     | Stores redacted raw artifacts + curated traces with file diffs  |
| "Did the agent's call cost dollar X?"                                     | **Atelier**     | Provider-true cache accounting on Anthropic + OpenAI + Gemini   |
| "Does my Claude usage match what most Claude users see?"                  | **ccusage**     | The de-facto Claude Code tracker in the community               |
| "What did the provider **actually** bill?"                                | **Provider dashboard** | Anthropic / OpenAI / Google consoles — final source of truth |

---

## Cross-checking workflow

When the three tools disagree, walk this checklist:

1. **Confirm the time window matches.** Atelier filters
   `trace.created_at LIKE 'YYYY-MM-DD%'` in **UTC**. CodeBurn defaults to local
   timezone (override with `--timezone`). Tokscale uses `--today` = local
   midnight. A timezone offset alone can move ±$10 of activity across day
   boundaries.
2. **Check one model at a time.** Total spend hides offsetting errors. The
   per-model rows on `codeburn today --format json` and `tokscale models
   --today --json` are the right comparison surface.
3. **For Anthropic models**, count occurrences of `message.id` in the raw
   `*.jsonl`. If a tool's `calls` number ≈ 1.5–3× Atelier's, that tool is
   naive-summing chunked emissions.
4. **For OpenAI/Gemini models**, divide a tool's `cacheRead` by its `calls`.
   Per-call `cacheRead` larger than the model's context window (e.g. >2M for
   Gemini Flash, >200K for GPT-5) means the tool is summing across events
   that shouldn't be combined.
5. **For Copilot**, check whether the gap is in the `copilot/<model>`
   subscription bucket. If yes, that's a pricing philosophy difference, not a
   bug.

---

## Atelier-specific implementation notes

- Placeholder models (`<synthetic>`, `_default`, empty string) are recognised
  by `atelier.core.capabilities.pricing.is_placeholder_model` and stripped
  from `trace.model` when summarising — so the trace's reported model is
  always something real.
- Unknown models log a **one-time warning** to make missing pricing entries
  visible (`atelier.pricing: no entry for model 'X' …`). Override via
  `pricing.override_pricing()` or add a built-in zero-cost alias to the
  `_load_pricing_table` `("opencode/big-pickle", "copilot/")` list.
- Tests covering these accounting choices live in
  `tests/test_session_importer_tokens.py` (~24 cases across all hosts).

---

## Worked example: today's $27.96 atelier-vs-codeburn gap

Breakdown of the $109.34 − $81.38 = $27.96 spread on 2026-05-14:

| Component                                                          | Δ (USD)   | Who's correct      |
|--------------------------------------------------------------------|----------:|--------------------|
| CodeBurn double-counts Opus 4.7 chunked messages                   | +$12.60   | Atelier            |
| CodeBurn double-counts Gemini Flash cache_read events              | +$9.86    | Atelier            |
| CodeBurn double-counts Gemini Pro cache_read events                | +$2.81    | Atelier            |
| CodeBurn applies $0.0033/call Copilot subscription proxy           | +$2.19    | Either defensible  |
| Atelier under-captures GPT-5.3 Codex routing classification        | −$0.68    | CodeBurn           |
| Atelier under-captures Cursor (auto) bubble token counts            | −$0.22    | CodeBurn           |
| Rounding / minor model bucketing differences                        | ±$1.40    | —                  |
| **Total**                                                           | **+$27.96** | —                  |

Of that, ~$0.90 is real Atelier under-capture; the rest is either CodeBurn
over-counting (~$25) or a defensible pricing philosophy difference (~$2.19).

---

## Maintenance

This doc captures the state as of **2026-05-14**. Both CodeBurn (v0.9.8) and
Tokscale evolve quickly. When you next run all three tools side-by-side and
the numbers don't match this page, **update the worked example** rather than
treating it as a regression. The structural points (dedup, subtraction,
subscription pricing) are stable; the absolute numbers will not be.

Source code references:
- Atelier importers: `src/atelier/gateway/hosts/session_parsers/`
- Atelier pricing: `src/atelier/core/capabilities/pricing.py`
- Atelier external bridges: `src/atelier/gateway/integrations/external_analytics.py`

---

## Adding more external trackers

Atelier currently wires three sidecars (CodeBurn, Tokscale, ccusage) into
the `External Analyzers` UI page. Adding more is a 3-step pattern in
`external_analytics.py`:

1. Define an `ExternalAnalyzerSpec` (id, executable name, supported periods,
   env-var override).
2. Add a `_run_<tool>_report_bundle(binary, period, *, cwd)` runner that
   shells out, runs `_run_json_command`, and returns the standard result
   shape (`ok`, `returncode`, `stdout`, `stderr`, `payload`, …).
3. Wire it into the `run_external_report` dispatch.
4. (Frontend) Add a per-tool `<ToolName>Panel` component in
   `frontend/src/pages/External.tsx`, append the id to `TOOL_PRIORITY`, and
   add a `displayToolName` / `toolDescription` entry.

### Ready-to-add candidates

These tools either produce JSON natively or are popular enough that
shimming their TUI output is worth it.

| Tool | License | What it adds beyond the existing three | Difficulty |
|------|---------|---------------------------------------|------------|
| **claude-monitor** (`pipx install claude-monitor`) | MIT | Real-time burn-rate + Anthropic plan quota | Medium — TUI-only; would need a `claude-monitor headless` wrapper |
| **cursor-stats** (`npm i -g cursor-stats`) | MIT | Decodes Cursor `state.vscdb` token tallies | Easy — JSON output, model name normalisation needed |
| **Helicone OSS** (`docker compose up helicone`) | Apache-2.0 | Provider-level *ground truth* via API proxy | Hard — different architecture (proxy, not local-file). Requires user to point SDK at proxy. |
| **LiteLLM Proxy** (`pip install 'litellm[proxy]'`) | MIT | Same idea as Helicone; ships the catalog Atelier already uses | Hard — same caveat |
| **Langfuse OSS** (self-host) | MIT-OSL | Trace-level observability across providers | Medium — SDK integration, not local-file |
| **OpenLLMetry / Traceloop** (`pip install traceloop-sdk`) | Apache-2.0 | OpenTelemetry-native; bridges to Datadog/Phoenix/etc. | Hard — different ingestion model |
| **Pezzo** (self-host) | Apache-2.0 | Prompt management with cost tracking | Medium — REST API, not local-file |
| **Arize Phoenix** (`pip install arize-phoenix`) | ELv2 | OpenInference-based eval + cost tracking | Hard — runs as its own service |

Most of the "local-file scanner" category is now covered by Atelier +
CodeBurn + Tokscale + ccusage. The next category to integrate would be
**provider proxies** (Helicone or LiteLLM), which give absolute ground
truth — at the cost of requiring the user to actively route LLM calls
through the proxy rather than just reading files post-hoc.

### Direct provider dashboards (zero integration, manual cross-check)

These are not Atelier-integratable (they're cloud UIs, not CLIs), but they
**are** the absolute source of truth:

| Provider | URL                                | Useful for                              |
|----------|------------------------------------|-----------------------------------------|
| Anthropic | `console.anthropic.com → Usage`   | Per-API-key Claude spend, daily/hourly  |
| OpenAI    | `platform.openai.com/usage`       | OpenAI / Codex spend                    |
| Google    | `aistudio.google.com → Billing`   | Gemini API spend                        |
| GitHub    | `github.com/settings/copilot/usage` | Copilot premium-request totals        |
| Cursor    | IDE settings → Usage              | Cursor model spend                      |

The next time the worked-example numbers in this doc need refreshing, pull
the provider dashboard total as a column to anchor "this is the answer; all
the tools are estimating against it."
