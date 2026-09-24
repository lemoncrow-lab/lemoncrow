# Ranked tool results — the general-purpose optimizer

Status: draft for review
Date: 2026-09-12
Constrains: all work on `mcp_proxy`, `mcp_output_shrink`, and the spill path
Related: `../../client/README.md` (public thin-client execution contract), `savings-optimization-roadmap.md`

## 1. Problem

LemonCrow's measured savings come from `code_search` and `read`: one call returns
ranked matches inline, everything lower-ranked comes back as `path:Lx-Ly` pointers, and
the model never re-reads what it already has. On SWE-bench Verified that is 37.7% fewer
turns and 27.9% fewer output tokens.

Every *other* tool result gets a categorically worse treatment. When a proxied or
host-wired MCP tool returns a large blob, the shrink path is:

- `integrations/claude/plugin/hooks/mcp_output_shrink.py:132-156` — spill the full text,
  then return `head 70% + tail 30%` of a 16 KiB budget.
- `mcp_server._spill_result_chars:12731-12753` and `_truncate_result_text:12673-12706` —
  the same shape for LemonCrow's own oversized results.
- `mcp_proxy._ProxyRegistry.call:172-184` passes `max_chars=None`, so proxied results
  land in the generic path with no knowledge of what was asked.

Two defects follow, and they are the whole reason LemonCrow does not yet help a
non-coding agent:

1. **The slice is position-based and query-blind.** For 4,812 SQL rows, a log tail, a
   Jira search or a Slack history, the row that matters is rarely in the first 70%.
   `code_search` ranks; this truncates.
2. **The recovery path re-dumps.** `spill_notice` offers `full: <path>`, and the model's
   only move is `read <path>`. The tokens arrive anyway, one turn later, plus a wasted
   turn. Deferral is not reduction.

The corpus is the only thing that differs between the two paths. The discipline that
makes `code_search` cheap — rank, slice, hand back a pointer — is not about code.

## 2. Goals

- Any tool result, from any MCP server, is ranked against the call that produced it
  before it enters context.
- The recovery path from a truncated result is **another bounded slice**, not a full
  read. A large result costs O(1) in context, not O(N) deferred.
- Slicing is shape-aware: whole rows, whole records, whole log lines. Never half a row.
- Nothing is silently dropped. Every truncation states total vs shown and how to reach
  the rest — the existing `spill_notice` contract, extended, not replaced.
- One notice grammar. `tool_output_spill.spill_notice:214-237` is already documented as
  the single source of truth; this design must not add a seventh shape.
- Runs in the thin client. No server round-trip, no embeddings, stdlib only.

## 3. Non-goals

- Semantic caching of tool results. Consecutive agent turns embed at ~0.99 similarity;
  measured 3.3% hit rate at 37.9% accuracy on agent workloads. Exact, state-keyed
  tool-result caching is a separate, later question.
- Transcript compression or summarization to save tokens. Agent turns re-read 93–97% of
  their prefix at 0.1× cache-read pricing; rewriting the prefix converts reads back into
  writes and loses money. Measured: 2× compression cut cost 27.9%, 5× compression *raised*
  it 1.8% because output grew. The external record is harsher still: in the only
  agent-loop evaluation of compressors that exists (arXiv 2608.06503, AppWorld, 168 tasks),
  *every* compressor lost to no compression — LLMLingua-2 68.2% vs 85.7% uncompressed, and
  on a second agent model 35.7% vs **58.3% for naive FIFO truncation**. The mechanism is
  specific to agent loops: compression dilutes the most recent turns, and the agent
  re-explores state it discarded. See §16.
- An offline index of tool-result corpora. Tool results are generated at call time and
  never reused. Ranking is per-call and ephemeral — there is nothing to amortize.
- Replacing per-tool spill thresholds (`_SPILL_RESULT_CHARS_BY_TOOL:635-638`). Those stay;
  this changes what happens *at* the threshold.
- A wire-level proxy. Every stage here needs the call's params, the tool's identity, and
  the moment *before* bytes are serialized. A `base_url` shim sees a finished request: it
  can shrink a payload but not unmake the tool call, cannot substitute "unchanged since
  turn 12" without ground truth, cannot pick which records belong in context, and cannot
  attribute a later failure to its own pruning — which is why every gateway-layer savings
  claim surveyed in §16 is unfalsifiable. The tool boundary is the defensible position.
## 4. Architecture

Four stages, inserted between the tool returning and the result being serialized.

```
proxied / host-wired tool returns
        |
   [1] capture ------> spill store, always, regardless of size -> handle
        |
   [2] shape --------> detect the unit: record | row | line | section | opaque
        |
   [3] rank ---------> BM25 units vs. call context (params + last user turn)
        |
   [4] compose ------> top-N whole units inline + queryable-handle footer
```

**Capture is unconditional.** Today spilling only happens above a threshold, so a result
that fits has no handle and cannot be re-sliced. Always spilling makes `slice` total.
Retention is already handled by `_enforce_retention:78-112`.

**Stage 4 is the only thing the model sees change**, and only in the footer.

## 5. Interception matrix

Two paths reach the model, and both must be covered or the win is partial.

| Path | Where | Hook point | Today |
| --- | --- | --- | --- |
| Servers called through `mcp(op='call')` | in-process | `mcp_proxy._ProxyRegistry.call:172-184` | `max_chars=None`, generic spill downstream |
| Servers wired directly to the host | PostToolUse hook | `mcp_output_shrink._shrink:132-156` | head/tail, no query context |
| LemonCrow's own oversized results | in-process | `mcp_server._spill_result_chars:12731-12753` | head/tail |

The proxied path is the better one — it has the params, the server name and the tool name
in hand. The hook path only has `tool_name` and `tool_response`; it must recover query
context from the PostToolUse payload's `tool_input`. Where it cannot, it degrades to
today's head/tail behaviour and says so in the footer.

## 6. Shape detection

The unit of slicing, resolved in this order, first match wins:

| Detected | Unit | Test |
| --- | --- | --- |
| JSON array of objects | one object | parses, top level is a list of dicts |
| NDJSON | one line | every non-blank line parses as JSON |
| Delimited table | one row | consistent separator count across ≥3 lines, header present |
| Log lines | one line | ≥60% of lines match a timestamp or level prefix |
| Markdown / prose | one section | headings present; else paragraph |
| Anything else | opaque | falls back to today's head/tail, footer says `shape=opaque` |

Shape detection is capped: sniff the first 256 KiB, never the whole blob. A wrong shape
guess must degrade to `opaque`, never to a wrong unit boundary — a half-row is worse than
a truncation, because it reads as complete.

## 7. Ranking

BM25 over units, scored against a query assembled from, in order of weight:

1. the tool call's own params (`params` in `tool_mcp:126-178`, `tool_input` in the hook) —
   this is literally the question the agent just asked;
2. the last user turn, when the host exposes it;
3. nothing else. No conversation history, no embeddings.

`sqlite3` ships FTS5 in the stdlib. For public-client code, any such optimization
must preserve the audited stdlib-only/no-transitive-dependency contract in
`client/README.md`. The index described here is built in-memory per call and
discarded.

Server-side semantic rerank over the same units is a later upgrade, gated on the
measurement in §12 showing lexical ranking leaving turns on the table. It is not v1.

**Ties and empty queries.** With no usable query context, ranking is undefined — do not
invent one. Fall back to head/tail and mark `ranked=false` in the footer, so the
degradation is visible in traces and measurable.

**Retriever recall is the whole risk.** Ranking swaps "the first 70%" for "what BM25
thinks was asked", so recall of the answer-bearing unit becomes the product. The published
spread between a good and a bad retriever over a tool corpus is enormous — Gorilla: 91.26
(oracle) vs 17.03 (BM25) on API selection; MCPProxy: 94% vs 14% on tool retrieval — and
query-awareness is independently the largest lever in the pruning literature (Provence,
ICLR 2025: **+0.24 points at 74.8% token removal**; its released model is CC BY-NC-ND, so
it is a design reference, not a dependency). v1 stays lexical for the stdlib constraint, so
§12 must report **recall@N of the answer unit** alongside cost, and §13's differential
slice test is what makes that measurable before a user sees it.

## 8. The handle protocol

The one real departure from today. `spill_notice` gains a second shape: a handle plus the
shape, so the model knows what it can ask for.

Today:

```
[lc: shrunk 237755→20032; full: /tmp/lemoncrow-spill/bash-bc95d5c7.txt]
```

Proposed, when ranked:

```
[lc: ranked 4812 rows → 12 shown; handle=r7f3;
 more: mcp(op='slice', handle='r7f3', query='timeout 5xx') | units=13-40 | full: <path>]
```

`full: <path>` stays — the escape hatch must remain, and some tasks genuinely need the
whole blob. But it is no longer the *only* move.

New op on the existing `mcp` tool (no new tool definition, see §9):

| Arg | Meaning |
| --- | --- |
| `handle` | id from a prior footer |
| `query` | re-rank the stored units against this instead |
| `units` | explicit range, `13-40`, for sequential walks |
| `limit` | cap on units returned; server-bounded |

`slice` returns the same footer shape, so a second slice is as bounded as the first. This
is what makes the result O(1) rather than deferred-O(N).

Handles are session-scoped and die with the spill retention window. A stale handle returns
a typed error naming the path, never a silent empty result.

## 9. Tool-definition deferral

The other half of the win, and it is already built — just unmeasured and slightly wrong.

Tool *definitions* cost ~55 KTok before a conversation starts (Anthropic reports having
seen 134 KTok; GitHub ~26 KTok, Slack ~21 KTok). Deferred loading measured 77 KTok → 8.7
KTok, an 85% reduction, **with accuracy rising 79.5% → 88.1%** — the schemas were actively
hurting. This is corpus-agnostic and applies to every agent on day one.

`tool_mcp:126-178` already implements exactly this pattern: N configured MCP servers cost
**one** tool schema (`mcp`) instead of N × full schemas, discovered on demand via
`op='list'`. That is the deferred-loading design, shipped, unclaimed.

One defect: `_ProxyRegistry.catalog:154-170` returns every tool on every server. Ten
servers and the catalog *becomes* the 55 KTok it was meant to avoid. Fix is the same
primitive as §7:

```
mcp(op='list', query='jira issue search')
```

Rank the catalog entries and return the matches, with the same footer. `_slim_tool` already
reduces each entry; this ranks and bounds the set.

**Why this is the safe prune, and who else ships it.** Anthropic now has the mechanism
natively (tool search + `defer_loading`), and LiteLLM ships `mcp_tool_search_enabled`,
which collapses a server catalog to two virtual tools. Both work for the same structural
reason: discovered definitions arrive as *inline conversation content*, appended after the
cached prefix, so deferral is the one prune that cannot invalidate the cache. Re-emitting
`tools/list` per turn — what a naive MCP gateway does — produces the opposite: a full miss
on a prefix that is 93–97% of the bill.

**A retrieval surface must beat its own schema tax.** jCodeMunch ships 51 tools ≈ 11,562
tokens into every session; Serena ~17.6–23.9 KTok. Both have reports of models emitting
malformed calls at that surface size and burning turns on retry — the schema tax paid twice.
The same retriever caveat as §7 applies with more force here: the catalog ranker *is* the
tool router, and a bad one costs correctness, not bytes.

## 10. Where it runs

The enterprise thin client no longer exposes the legacy local `mcp` proxy because
spawning another MCP process violates its one-process security contract. Any generalized
result-ranking path that ships in that client must therefore run inside the existing
stdio process or on the configured LemonCrow server; it must not introduce another local
service, daemon, or MCP child process.

## 11. Failure modes

Bounds are semantics. A cap added for cost changes what the agent can see.

| Failure | Consequence | Rule |
| --- | --- | --- |
| Wrong shape guess | half a record read as whole | degrade to `opaque`, never guess a unit boundary |
| Ranker misses the one relevant row | agent re-calls the tool; tokens return as turns | measure turns, not bytes (§12) |
| No query context | ranking is arbitrary | `ranked=false`, head/tail, visible in trace |
| Spill write fails | nothing to slice | fail open — return the full result, as `_shrink` does today |
| Stale handle | silent empty answer | typed error naming the spill path |
| Large unit count | ranking cost | cap sniff at 256 KiB, cap units at 50k, then `opaque` |
| A slice edits anything already sent | cache miss; tokens fall while cost rises | only shape bytes not yet written; §13 cache-safety test |
| Handle assumed to live in an MCP session | breaks on stateless transports (MCP rev 2026-07-28 has no `initialize`/session id) | handles are client-side state keyed to the spill store, never to a session id |
| The mechanism's own standing cost | net loss, and invisible on a savings dashboard | count schema bytes and per-turn injections; report savings net of them (§12) |

The dangerous one is row three combined with row one: a confident-looking slice of the
wrong rows. Every footer states totals so the model can tell "12 of 4,812" from "12 of 12".

## 12. Measurement

The acceptance test is not bytes saved. A ranker that drops the needed row makes the agent
call the tool again, buying the tokens back as turns — the failure mode the project's own
philosophy section names. The public record of savings add-ons (§16) is a catalogue of
exactly this, so the protocol below is deliberately hostile to our own hypothesis.

**Step 0 — the free ceiling, before any paid run.** Replay existing transcripts and compute
(share of calls that route through an intercept) × (share of tool-result bytes those carry)
× (share of billed input that is fresh tool result rather than a cache re-read). rtk's
ceiling was ~3%, which predicted its whole outcome for free. A single-digit ceiling means
the paid suite can only measure noise or harm — and the answer is then to route more
servers through `mcp()` (§15), not to tune the ranker.

**Step 0b — the controls must include the cheap ones.** Head/tail *is* our FIFO control and
stays an arm. Reasoning effort is the other: `--effort medium` matched an add-on's quality
at 23% lower cost in one 900-trial eval, and on non-coding work the effort curve is nearly
flat. Anything that cannot beat a config flag is not a finding.

Fixed non-coding suite, same methodology as the existing benchmark arms (pinned model,
pinned tasks, held-constant containers, 5 reps): agent tasks over a Postgres MCP server, a
GitHub MCP server, and a log store.

- **Baseline:** servers wired directly to the host, no LemonCrow.
- **Arm A:** same servers through `mcp()` with today's head/tail shrink.
- **Arm B:** same servers through `mcp()` with ranked slices.
- **Arm C:** baseline at a lower effort setting — the config-flag control.

Report **$/completed task** and **turns**, per the README's stated standard. Arm A vs
baseline isolates the tool-definition win (§9); Arm B vs Arm A isolates the ranking win.
Publish both, including a regression if one appears.

Rules that decide whether the numbers mean anything:

- **Per-task paired deltas, never arm totals.** One session crossing a long-context pricing
  tier can bill 25× normal and flip a total. Exclusions symmetric: a task that errors in
  either arm leaves both.
- **Cache-aware accounting.** Split input into fresh (uncached + cache-creation) vs cache
  reads and price each at its real rate. Fresh input is the primary endpoint, because it is
  the only class a tool-result shaper can touch. Never book the provider's cache discount as
  our saving, and watch cache-read growth specifically — it is the signal that caught the
  two loudest failures in §16.
- **Turns and tool calls are primary, not diagnostic.** ~92% of the one genuine token effect
  in the public record came from step count, not terser bytes.
- **Recall@N of the answer unit**, per shape, from the §13 corpora — the leading indicator
  of a turn regression before the suite is even run.
- **Provider usage numbers only.** `bytes/4` underestimates terse text by ~48%; never gate
  or report on it.
- **Savings net of standing cost**: the `mcp` schema, the footer, and any per-turn injection.
- **Prove the treatment fired**, per trial, from the trace; exclude non-activated runs. An
  interception layer that silently no-ops while a counter advertises ~98% is a documented
  failure mode, not a hypothetical.
- **Ladder, never k=1.** Replay → 1-trial wiring check → 10-task smoke → same 10 at k=3 →
  full set. Establish within-arm variance first: a published series found a median 22% cost
  spread between identical attempts, and one MCP benchmark swings 18.9 points across 23
  identical runs. Any delta under that is a mirage.
- **State what the suite cannot tell us.** A quality null at this n rules out large
  regressions only; it is not proof of equivalence.

## 13. Testing

- Differential slice test: for a corpus with a known answer row, assert the ranked slice
  contains it across shapes (JSON array, NDJSON, table, logs) at 10×, 100×, 1000× the
  inline budget. This is the highest-value test in the design.
- Recall floor: the same corpora produce a recall@N number per shape; CI fails below a
  declared floor, so a ranker regression is caught by tests rather than by a benchmark run.
- Unit-boundary test: no returned slice ever contains a partial record, for every shape.
- Fail-open test: spill failure returns the unmodified result.
- Footer grammar test: exactly one notice shape reaches the model per event; assert against
  `spill_notice` as the single producer.
- Handle round-trip: slice → footer → `op='slice'` → bounded result, twice, without the
  full blob ever entering the transcript.
- Cache-safety test: a ranked slice must not alter anything earlier in the prompt — assert
  the request prefix is byte-identical before and after.
- Standing-cost test: the mechanism adds the footer and nothing else per turn; the `mcp`
  tool schema stays inside a byte budget asserted in CI.
- Activation test: a trace field proves the pipeline ran on a given call, which is what
  makes §12's exclusion of non-activated runs possible.

## 14. Rollout

0. Run §12 Step 0 on existing transcripts. Free, and it decides whether steps 1–4 are worth
   paying for; a single-digit ceiling means route more servers through `mcp()` first.
1. Land unconditional capture + handles behind a flag; footer unchanged. No behaviour
   change, but everything becomes sliceable.
2. Ship `mcp(op='slice')` and the extended footer. Ranking still off — sequential `units=`
   only. Proves the protocol.
3. Land shape detection + BM25 ranking behind `LEMONCROW_RANK_TOOL_RESULTS`, default off.
4. Run the §12 suite. Default on only if Arm B beats Arm A on turns, not just bytes.
5. Rank `mcp(op='list')` (§9) and publish the tool-definition number separately — it is
   the day-one, no-adapter win and does not depend on steps 0–4.
6. Point `mcp_output_shrink._shrink` at the same pipeline so host-wired servers get it too.
7. Extract stages 1–4 as a host-agnostic library with the four call sites in §17, and prove
   it on one non-Claude stack (LangChain `wrap_tool_call` is the cheapest first adapter).
8. Add loop and budget caps (turn cap, dollar cap, per-tool timeout, retry-the-tool-not-the-
   loop) to that library. Cheapest guard, largest tail saving, and unowned by anyone in
   §16.4 — but it is a separate design doc, not a section here.

## 15. Open questions

- Does the PostToolUse payload reliably carry `tool_input`? If not, the hook path can never
  rank, and only the proxied path gets the full win — which is an argument for routing more
  servers through `mcp()`.
- Handle lifetime vs. spill retention: a handle that outlives its file is a broken promise;
  a file that outlives the session is a disk leak. Retention is `_enforce_retention:78-112`
  today and is time-based, not session-based.
- Whether `read` on a spill path should itself be ranked when the file came from a ranked
  slice — the model currently gets a raw dump.
- Binary and image tool results: out of scope here, but they are a growing share of
  browser-agent traffic and the current path has nothing for them.
- Error excision as a separate lever: the ICLR 2026 self-conditioning result (frontier
  models run 100 turns cleanly on healed history and fail on raw history) says deleting the
  agent's *own failed calls* is worth more than compressing anything. It is deletion of
  known-wrong content, not summarization, so it does not contradict §3 — but it edits the
  prefix, so it can only run at a phase boundary. Separate design.
- Whether to publish the $/task-vs-accuracy curve for ranked slice vs head/tail vs full
  result. No paper or vendor has published that curve for an agent workload; §12 produces it
  as a by-product, and it is the most defensible thing we could put in public.

## 16. What the public record says (2026-09 audit)

The ecosystem this design competes with is loud and almost entirely unmeasured. The audit
behind this section is in `docs-internal/research/` (three files, primary sources cited);
the parts that change decisions here:

**16.1 Measured outcomes, paired A/B, task-gated.** One systematic independent series
exists (JetBrains AI, SkillsBench, claude-sonnet-5, n≈80–86 pairs, sign-tested):

| Tool | Advertised | Measured | Mechanism |
| --- | --- | --- | --- |
| terse-output skill | −65% | **−8.5%** output tokens, quality tied | style |
| command-output compressor | 60–90% | **+7.6% cost** (p=0.004), +13.8% turns | Bash-hook byte compression |
| write-less-code skill | — | **−10.3% cost** (p=0.004) | fewer output tokens, fewer steps |

The only win attacks the **expensive side** (output ≈ 5× input) and the agent's behaviour.
A 900-trial eval of the terse-output skill found **~92% of its token reduction was fewer
steps**, not terser prose — the byte mechanism everyone markets is not the one that pays.

**16.2 The four leaks** that turn "X% smaller payload" into "no saving", in the order they
bite this design:

1. **Scope** — the intercept sees a fraction of the stream (one compressor: ~20% of
   tool-result chars; a symbol server: 20.3% of reads). §12 Step 0 measures ours first.
2. **Pricing** — most input is cached re-reads at 0.1×. Saving a fresh token and a cached
   token are different purchases; this is why §12 splits them.
3. **Counterfactual** — the harness already truncates, so "tokens saved" counted against a
   raw payload were never going to be sent. Our baseline is today's head/tail, not the blob.
4. **Behavioural** — the slice changes what the agent does next: re-reads, retrievals, retry
   loops. One remove-and-retrieve product netted **0.34%** on 3,000 real outputs because the
   agent fetched the content anyway and context then held the marker *and* the blob. This is
   §11 row two, and it is the leak that flips the sign.

**16.3 Standing cost eats the rest.** A hook that re-injects nudges logged 8,348 injections
producing 339 tool calls at ~651 KTok — 3.7× more than every query it enabled. Self-reported
dashboards are not evidence: one advertised 96.2M tokens saved on runs where the bill rose;
another counted binary bytes that would never have entered context; a third silently no-opped
because a native module failed to build while still displaying ~98%. Star counts are
uncorrelated with measured effect (116k stars / 1 watcher, at 3.7× net cost).

**16.4 Adjacent layers.** The wire-level products are weaker than they sound: one "universal
MCP proxy" has no provider-compatible route at all, and the 70%-savings gateway claim has
vanished from its own site. Real gateways (LiteLLM, Portkey — Apache-2.0 since 2026-03-24)
ship commodity levers: exact/semantic cache, budgets, model routing, provider-health
breakers. **Nobody ships agent-loop circuit breaking or in-flight coalescing**, and nobody
can do what §3's last bullet describes. Model routing is the one independently measured
gateway lever (RouterArena, ICLR 2026: ~35% cost at <2% accuracy loss).

**16.5 What survives everything.** Tool-surface deferral (§9), query-aware pruning at the
result boundary (§7), prompt-cache hygiene, loop caps, error excision (§15), and taking
fewer steps. This design is three of the six, which is the argument for building it.

## 17. Reach beyond Claude Code

The pipeline in §4 must be a **library with four call sites**, not a hook. Stages 1–4 take
`(tool_name, params, result_bytes)` and return `(inline_text, footer)` with no knowledge of
the host. That is what makes this the general-purpose optimizer rather than a Claude Code
feature.

| Site | Covers | Query context | Status |
| --- | --- | --- | --- |
| `mcp_proxy._ProxyRegistry.call` | every server routed through `mcp()`, any host | full (params, server, tool) | §5, best path |
| host PostToolUse hook | host-wired servers under Claude Code | `tool_input` if present, else `ranked=false` | §5, degraded |
| framework tool wrapper | non-Claude agents, in-process | full | new, §17.1 |
| MCP gateway (server-side) | agents we never integrate with | tool args only | later; must not churn `tools/list` (§9) |

**17.1 The framework hook per stack**, all of which wrap the tool boundary, not the
transcript — so none of them touch the cached prefix:

| Stack | Hook | Rewrites tool result | Can defer tools |
| --- | --- | --- | --- |
| LangChain / LangGraph | `AgentMiddleware.wrap_tool_call`; `wrap_model_call` + `request.override(tools=…)` | yes | yes |
| Pydantic AI | `Hooks(before_model_request=…)`, `PrepareTools` | yes | yes |
| OpenAI Agents SDK | `RunConfig.call_model_input_filter`; per-tool `is_enabled`; MCP `tool_filter` | yes | yes |
| CrewAI | `@on(InterceptionPoint.PRE_MODEL_CALL)` (in-place `ctx.messages[:]`) | yes | per-agent tool lists |
| AG2 | `BaseMiddleware.on_llm_call`; classic `register_hook("process_all_messages_before_reply")` | yes | via tool middleware |
| AutoGen 0.4+ | subclass `ChatCompletionContext.get_messages()`, pass `model_context=` | yes | no |
| Vercel AI SDK | `prepareStep`, `activeTools`, `stopWhen` | yes | yes |
| Semantic Kernel | `IChatHistoryReducer.ReduceAsync` | yes | via `FunctionChoiceBehavior` |
| Claude Agent SDK | `PostToolUse.updatedToolOutput`, `maxResultSizeChars`, `max_turns`, `max_budget_usd` | yes | `disallowed_tools` |

Two consequences worth stating plainly. First, the ordering rule for any of these sites is
the same: make the prefix byte-stable, cap payloads **at the tool boundary**, defer the tool
surface, cap the loop — and only then touch model or effort. Ranked slices are step two, the
one prune that is cache-safe by construction. Second, `max_turns` / `max_budget_usd` style
loop caps are the cheapest guard with the largest tail saving (retry storms run to ~200×) and
are not in this design at all; they belong in the same library, and nobody in §16.4 ships
them at the agent-loop level.
