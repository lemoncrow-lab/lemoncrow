# Savings Optimization Status and Roadmap

> Last implementation audit: 2026-08-02.
>
> **Implementation state: all six runtime levers implemented.** Redacted
> decision tracing plus Routing V2, Output Governor V2, adaptive cache economics,
> verified evidence reuse, bounded local retrieval, and hybrid MCP exposure are
> locally verified. Controlled benchmark gates remain pending; no paid run has
> begun.
>
> This document is the source of truth for LemonCrow's model-token and dollar-cost
> optimization work. A feature moves to **complete** only when its default runtime
> path, tests, rollback control, and measured acceptance evidence all exist.

## Executive status

The LemonCode host/control plane is shipped. All six savings levers now have
runtime implementations and rollback controls. Five remain **V2 implemented /
measurement pending**; MCP exposure is **adapted complete** because the safe
design intentionally rejects a mandatory search-first call.

| # | Optimization lever | Current status | Short answer |
| -: | ------------------ | -------------- | ------------ |
| 1 | Closed-loop phase routing | **V2 implemented / A/B pending** | Expected-total-cost calibration learns from outcomes, shadows below the sample floor, and safely escalates repair work. |
| 2 | Output governor | **V2 implemented / A/B pending** | Enforce mode strips model-history narration, uses tool-only mutation phases, extends only on truncation, and stops on a verified receipt. |
| 3 | Provider-aware cache economics | **V2 implemented / A/B pending** | Auto observes reuse gaps, shadows expected-value TTL choices, and enforces them only in enforce mode; explicit policies always win. |
| 4 | Local frontier-token firewall | **V2 implemented / retrieval A/B pending** | A gated, bounded local loop returns exact source-hashed spans; obvious tasks add zero planner calls. |
| 5 | Lazy MCP/tool exposure | **Adapted complete** | The safe hybrid policy avoids a compulsory search-first call. |
| 6 | Verified cross-session reuse | **V2 implemented / recurring benchmark pending** | Only source-hashed deterministic evidence with matching workspace/dependency/tool fingerprints and a receipt is reused. |
“Implemented” is not the same as “complete” in this ledger. Unit and regression
tests close the implementation gate; only a controlled quality/cost comparison
can close the product acceptance gate.

## Status vocabulary

| Status | Meaning |
| ------ | ------- |
| **Complete** | Active on the supported default path, controllable, tested, and accepted against a measured quality/cost gate. |
| **Adapted complete** | The original idea was deliberately changed to avoid a known regression; the safer intended behavior is active and tested. |
| **V1 / partial** | Useful runtime behavior ships, but the full decision loop or acceptance evidence is incomplete. |
| **Foundation only** | Supporting deterministic infrastructure exists, but the proposed optimization itself is not implemented. |
| **Not started** | No meaningful runtime implementation exists. |

## What is already fully delivered

### LemonCode control plane

LemonCrow does not need to build another terminal UI from scratch. It owns the
expensive agent boundary while reusing a mature frontend:

```text
LemonCode fork / Codex / Claude / native frontend
                      |
                      v
       token-authenticated loopback gateway
                      |
                      v
  LemonCrow-owned route, tools, subagents, cache,
  compaction, verification, stopping, and cost cap
                      |
                      v
                 model provider
```

The delivered control plane includes:

- `lc code` as the permanent LemonCrow command and `lemoncode` as a permanent
  console entry point for the same command.
- The controlled
  [`lemoncrow-lab/lemoncode`](https://github.com/lemoncrow-lab/lemoncode)
  fork, derived from OpenCode, as the preferred frontend.
- Managed LemonCode, Codex, and Claude frontend modes plus the native fallback.
- OpenAI Chat Completions, OpenAI Responses/Codex, and Anthropic Messages
  gateway paths.
- Removal of the frontend's redundant model-facing system prompt and tool
  schemas before the provider request.
- An authenticated loopback gateway that owns the actual agent loop.
- Source build, install, update, status, and removal commands for the managed
  LemonCode host.
- An isolated LemonCode data directory and disabled duplicate frontend
  compaction, update, and model-fetch loops.
- Automated upstream synchronization, tests, and multi-platform release
  verification for the fork.

See [the CLI reference](../reference/cli.md#lemoncode) for current commands and controls.

### Why a fork is the right boundary

A fork keeps the mature OpenCode interaction model, renderer, session handling,
and upstream fixes while allowing LemonCrow to change the model-facing
internals. The fork is intentionally a thin controlled host: optimizations
belong in the LemonCrow gateway/runtime unless the frontend must change to
remove duplicate prompts, schemas, model calls, or lifecycle behavior. This
keeps upstream merging practical while preserving full control over spend.

## Evidence ledger

### Current measured baseline

The public matched Terminal-Bench 2.1 comparison is the baseline for claims:

| Metric | LemonCrow | Claude Code baseline | Result |
| ------ | ---------: | -------------------: | ------ |
| Resolved | 351 / 445 | 351 / 445 | Tied at 78.9% |
| Fresh input tokens | 182K | 12.87M | 98.6% lower |
| Output tokens | 5.36M | 8.09M | 33.8% lower |
| Cache tokens | 122.0M | 161.9M | 24.6% lower |
| Total tokens | 127.6M | 182.9M | 30.2% lower |
| Normalized cost, both at 1-hour cache-write rate | $61.98 | $73.75 | 16.0% lower |

The detailed methodology and raw-run links are in
[Terminal-Bench](../../BENCHMARKS.md#terminal-bench).

### Design estimates are not product claims

The original exploration produced the following upside ranges. They are
engineering targets, overlap with each other, and **must not be added together**.

| Lever | Original planning estimate | Evidence class |
| ----- | -------------------------- | -------------- |
| Closed-loop routing | 20–45% incremental savings | Target; not yet measured end to end |
| Output governor | 20% fewer output tokens, roughly 9.8% total-cost reduction in the historical cost mix | Arithmetic counterfactual |
| Cache TTL repricing | $61.98 to about $55.28, or 10.8%, when repricing that historical trajectory from 1-hour to 5-minute writes | Direct counterfactual, not a runtime A/B |
| Selective cache writes | 3–8% | Target |
| Local retrieval firewall | 5–15% | Target |
| MCP exposure reduction | 2–8% | Target; protected by a no-extra-call constraint |
| Verified cross-session reuse | 10–30% on recurring work | Target; recurring workloads only |
| LemonCode with the same model/provider | 15–30% beyond MCP-only operation | Architecture target |
| LemonCode with verified model routing | 35–60% dollar-cost reduction | Architecture target |

Only the public benchmark numbers are current product measurements. The 10.8%
cache number is a pricing counterfactual. Every other percentage above remains
a hypothesis until a controlled run says otherwise.

## Lever 1 — Closed-loop phase routing

**Implementation status: V2 done; controlled A/B evidence pending.**

**Original goal:** choose the tuple
`{provider, model, reasoning effort, output limit, toolset, cache lane}` for
each safe phase boundary. The desired loop is:

```text
cheap/local discovery
        -> frontier reasoning and edit
        -> deterministic verification
        -> cheap or template finalization
```

The decision minimizes:

```text
direct cost + P(failure) * escalation cost + cache-break cost
```

### Active now

- Provider/model candidates still honor capability, health, pricing, configured
  vendors, execution contracts, and warm-cache affinity.
- A local SQLite outcome store records model, phase, success, and actual cost;
  prompts, arguments, code, and commands are not stored.
- Bayesian-smoothed failure probability uses conservative tier/phase priors.
- A calibrated route is shadow-only until its bucket reaches the configurable
  20-sample floor (`LEMONCROW_ROUTING_MIN_SAMPLES`).
- Repair or prior failure immediately restricts selection to a high-tier route;
  it does not wait for the learning floor.
- Route score includes direct cost, expected escalation cost, and estimated
  cache-break cost.
- A configurable 10% expected-cost hysteresis
  (`LEMONCROW_ROUTING_HYSTERESIS_PCT`) retains the warm model/cache lane when
  marginal savings do not justify a switch; repair safety bypasses hysteresis.
- Initial and phase-boundary choices honor `off`, `shadow`, and `enforce`.
- Every provider request records the coordinated model, effort, output cap,
  tool choice/profile, cache tier, and stable lane as one joint phase policy.
- Verified receipt finalization removes the unsafe finish-phase model switch.
- `lc optimize decisions --json` reports aggregate route outcomes and runtime
  proposals; a pinned `--model` or `--optimization-mode off` is the rollback.

Implementation:
[`runtime.py`](../../src/lemoncrow/gateway/cli/runtime.py),
[`runtime_policy.py`](../../src/lemoncrow/pro/capabilities/owned_agent_session/runtime_policy.py), and
[`routing_calibration.py`](../../src/lemoncrow/pro/capabilities/optimization/routing_calibration.py).

### Still missing

- Enough live shadow outcomes to make ordinary learned buckets enforceable.
- A controlled workload A/B showing lower cost per accepted change without a
  quality or latency regression.

### Completion gate

- Predeclare a correctness non-inferiority margin on a frozen task corpus.
- Log proposed and actual decisions, route switches, escalation outcomes,
  cache-break cost, latency, and verification result.
- Show lower cost per accepted change on eligible tasks.
- Keep an explicit single-model/pinned-route rollback control.
- Do not switch routes merely because a cheaper model exists; switch only when
  expected total cost, including failure and cache loss, improves.

## Lever 2 — Output governor

**Implementation status: V2 done; controlled A/B evidence pending.**

**Original goal:** prevent the expensive model from producing text that does not
advance execution or verification.

### Active now

- Phase-specific maximum output tokens and reasoning effort.
- Per-run maximum dollar cost.
- `off`, `shadow`, and `enforce` modes through `lc code --optimization-mode`.
- Tool-only execution on supported providers for explicit mutation work.
- Progress narration remains visible in the native UI but is excluded from
  subsequent model-visible history and managed-host final output in enforce mode.
- Output budgets extend only after an explicit provider truncation signal, with
  a hard bounded retry.
- Successful edit generations require a later successful verification receipt;
  mutation requests that only claim completion are not counted as accepted.
- Enforce mode stops without another provider call and renders a bounded final
  response from edited paths plus the verification command.
- Shadow mode records every proposed change while preserving legacy behavior.
- Loop detection, deterministic history compaction, retry/fallback handling, and
  verification events remain active.

### Still missing

- A controlled A/B isolating output policy from routing and cache changes.
- Provider-compatibility evidence beyond the mocked Chat/Responses/Anthropic
  gateway and owned-runtime regression suites.

### Completion gate

- Reduce output tokens by at least 15–20% on the eligible corpus.
- Preserve the predeclared verified-success bound.
- Do not increase incomplete, truncated, or “tests were never run” outcomes.
- Report output dollars, calls, latency, and accepted-change cost separately.
- Retain a configuration switch for observe-only and enforcement modes.

## Lever 3 — Provider-aware cache economics

**Implementation status: V2 done; controlled replay/A/B evidence pending.**

**Original goal:** cache stable prefixes only when expected future reads repay
the write premium, using the provider's actual cache semantics.

### Active now

- `auto` records actual stable-prefix reuse gaps in a local SQLite store.
- A conservative expected-value rule promotes Anthropic/Gemini prefixes to one
  hour only after at least three observations show that 5–60 minute reuse repays
  the additional write premium.
- Shadow mode records the adaptive proposal but keeps the 5-minute behavior;
  enforce mode applies it. Explicit `5m`, `1h`, and `off` always win.
- Anthropic system and moving breakpoints receive the selected TTL.
- Selective moving breakpoints accept bounded user/read evidence and reject
  assistant narration, build/test logs, diffs, and volatile payloads.
- OpenAI requests receive a stable `prompt_cache_key` lane.
- Gemini cached-content handles are reused across processes; creation is
  best-effort, size-gated, TTL-aware, and never retried repeatedly in one run.
- End-of-turn compaction is wired to the rewrite-economics decision in enforce
  mode. Cache-disabled sessions compact normally, cached sessions rewrite only
  when expected reads repay the write, and the provider rate-card context window
  supplies a hard 80% safety ceiling that always wins.
- Every decision is included in the redacted optimization trace.

### Still missing

- A mixed-gap replay against fixed off/5-minute/1-hour policies.
- Real provider receipts proving lower cache dollars and no write amplification.
- Controlled confirmation that route hysteresis and compaction economics reduce
  cache breaks without retaining excessive context.

### Completion gate

- Compare adaptive policy against always-5-minute, always-1-hour, and off on a
  mixed reuse-gap replay.
- Lower real or faithfully repriced cache cost without write amplification.
- No correctness change and no unexplained prefix-hit regression.
- Log write tokens, read tokens, TTL, observed reuse gap, invalidation reason,
  and provider pricing used by the decision.

## Lever 4 — Local frontier-token firewall

**Implementation status: V2 done; retrieval A/B evidence pending.**

**Original goal:** let deterministic retrieval plus a small local/cheap
micro-agent perform ambiguous, iterative exploration. The frontier model should
receive one bounded evidence packet rather than the entire search transcript.

### Active now

- An explicit source path is rejected by a cheap task-text gate before the
  broad primer, retrieval fingerprint, or corpus scan (unless an existing
  verified packet must be validated). Auto mode runs only for retrieval-heavy
  or ambiguous work in a non-trivial workspace.
- The local corpus is deterministic and bounded to 500 source files, 250 KB per
  file, 10 MB total, and configured source suffixes while skipping generated,
  VCS, dependency, and cache directories.
- Multi-query retrieval is limited to 1–5 turns, 1–16 returned spans, 1–20 KB,
  and 0.1–30 seconds; CLI defaults are deliberately smaller.
- With no planner configured, refinement is entirely deterministic and makes
  zero model calls.
- An optional planner accepts only `ollama/`, `lm_studio/`, or `local/`
  identifiers. A cloud/provider model identifier is ignored without a call.
- The planner can emit only a bounded next-query/finish JSON decision. It cannot
  answer the task, generate a patch, or bypass deterministic source reads.
- Packets contain exact `path:Lx-Ly` citations, current whole-file SHA-256
  hashes, scores, queries, confidence, and a fallback instruction.
- Low confidence returns no packet, causing the frontier agent to use its normal
  deterministic retrieval rather than guess.
- Packet caching is local SQLite, mode `0600`, workspace-fingerprint keyed,
  budget/model/mode keyed, capped at 256 packets, and expires after 14 days.
- Verified cross-session evidence takes precedence, avoiding a redundant local
  loop when a valid packet already exists.
- `shadow` records packet availability but leaves the frontier prompt
  unchanged; `enforce` applies it; `off` skips both verified reuse and local
  retrieval. The trace records counts and decisions, never source contents.
- Controls are `--local-retrieval off|auto|force` and
  `--local-retrieval-model <local model>`.

Implementation:
[`task_primer.py`](../../src/lemoncrow/pro/capabilities/owned_agent_session/task_primer.py),
[`primer_cache.py`](../../src/lemoncrow/pro/capabilities/owned_agent_session/primer_cache.py),
and
[`local_retrieval.py`](../../src/lemoncrow/pro/capabilities/optimization/local_retrieval.py).
Tests:
[`test_local_retrieval.py`](../../tests/core/test_local_retrieval.py) and
[`test_primer_cache.py`](../../tests/gateway/cli/test_primer_cache.py).

### Still missing

- A frozen retrieval corpus measuring exact-target recall, returned frontier
  tokens, local latency, local planner calls, and end-to-end accepted cost.
- A controlled `shadow` versus `enforce` comparison with the optional local
  planner tested separately from deterministic refinement.

### Completion gate

- Invoke the local agent only for retrieval-heavy or ambiguous tasks.
- Bound it to a small fixed budget, initially 2–5 local turns.
- Return exact source evidence, never an unverified generated answer or patch.
- Fall back to normal deterministic retrieval when confidence is low.
- Demonstrate savings on eligible tasks without a material exact-target recall
  regression or unacceptable latency.

## Lever 5 — MCP and tool exposure

**Original concern:** a purely lazy catalog can backfire. If every small task
must first call “search tools,” call count and latency can approach twice the
necessary work, and fuzzy selection may choose an unrelated tool.

The implementation therefore uses a hybrid policy rather than mandatory lazy
discovery.

### Active now

- Managed `lc code` exposes zero redundant outer-host tool schemas; the
  LemonCrow runtime owns its internal tools.
- Direct host integrations eagerly expose the core coding tools, so
  `code_search`, `read`, `edit`, `bash`, and `web_fetch` do not require
  discovery.
- Small or ambiguous external catalogs are exposed eagerly.
- An explicit server or tool name can focus a larger catalog deterministically.
- A broker is available only as a bounded fallback.
- `LEMONCROW_MCP_TOOL_PROFILE=full` is an escape hatch.
- There is no unconditional search-first call for every operation.

### Remaining measurement, not redesign

- Count discovery calls and incorrect tool selections on real traces.
- Confirm that small/core tasks add zero broker calls.
- Keep the current eager fallback if a lazy variant cannot prove lower total
  calls and equal selection accuracy.

This lever is **adapted complete**. It should not be replaced with pure lazy
exposure without trace evidence.

## Lever 6 — Verified cross-session reuse

**Implementation status: V2 done; recurring-work benchmark pending.**

**Original goal:** reuse verified investigation evidence and deterministic tool
results across recurring work, without replaying stale model prose or blindly
reapplying a patch.

### Active now

- Deterministic task primers persist across sessions and load verified evidence
  even when the base primer itself was already cached.
- Only `read`, `grep`, `code_search`, `explore`, and `symbols` results are
  eligible; edit, shell, model, patch, diff, failure-log, and oversized output is
  rejected before storage.
- Every packet contains normalized-task/tool/argument fingerprints, whole-source
  SHA-256 hashes, exact line spans when available, result hash, workspace Git and
  dirty-state fingerprint, dependency/lockfile fingerprint, Python/tool version,
  provenance, TTL, invalidation reason, and a hashed—not raw—verification command.
- Packets are staged during retrieval and become reusable only after a successful
  project-command receipt or a read-only workspace revalidation receipt.
- Source hashes are checked both when finalizing and when loading.
- SQLite WAL storage is bounded, process-safe, local, and mode `0600`.
- Shadow mode can discover and validate reusable packets without putting them in
  the provider prompt; enforce applies them, and off skips lookup/staging.
- Primer reuse never replays a patch or model answer.

### Still missing

- A repeated-work benchmark measuring hit rate, stale-hit rate, latency, and cost.
- Live evidence that recurring-task savings reach the planning range.

### Completion gate

- Never replay a patch or unverified model answer.
- Require source hash/fingerprint matches for every reused evidence span.
- Invalidate on relevant commit, dirty-file, dependency, or tool-version change.
- Record the verification receipt and provenance.
- Show lower cost on a recurring-task corpus with zero accepted stale evidence.

## Proposed next work

### Priority 0 — Measurement contract and shadow mode

**Implementation: done; live evidence accumulation pending.** Redacted local
runtime records now capture proposed-versus-actual decisions, provider/tool/broker
calls, phase/model/output controls, tokens, cost, verification, and accepted
outcomes. Use `lc optimize decisions --json`; `lc code --optimization-mode off`
is the rollback.

This was the immediate first implementation slice. Optimization policy should not
become more aggressive until LemonCrow can prove whether each decision helped.

The local optimization-decision trace records:

- task/run identity and runtime phase;
- proposed and actual provider/model/effort/output/cache/tool decisions;
- reason and eligibility rule;
- provider calls, tool calls, broker/discovery calls, and route transitions;
- fresh input, cache write, cache read, and output tokens;
- estimated and actual dollar cost;
- latency, truncation, escalation, verifier result, and accepted outcome;
- cache reuse gap, invalidation, and cache-break/rewrite cost.

The first mode must be **shadow**: compute the proposed action, record it, and
leave behavior unchanged. Use replay traces for cost modeling and a smaller paid
A/B for quality; replay alone cannot prove correctness after a model change.

### Priority 1 — Output Governor V2

**Implementation: done; controlled A/B pending.** User-visible progress is now
separate from retained model history, extensions require real truncation,
mutation generations carry verification receipts, and enforce mode finalizes
without another provider call. `off` is the one-setting rollback and `shadow`
is the behavior-preserving evidence mode.

The remaining work for this priority is evidence: run the predeclared eligible
corpus and promote the status only if the completion gate passes.
### Priority 2 — Adaptive Cache Economics V2

**Implementation: done; controlled replay/A/B pending.** Observed reuse gaps,
expected-value TTL selection, semantic breakpoint eligibility, Anthropic TTLs,
OpenAI stable keys, and size-gated Gemini cached-content handles are wired into
the owned runtime. Explicit policies and `--optimization-mode off` remain the
rollback controls.
### Priority 3 — Routing V2 calibration

**Implementation: done; live calibration/A/B pending.** Initial and phase routes
now compare direct cost plus calibrated failure/escalation and cache-break cost.
Below 20 matching outcomes the proposal remains shadow-only; repair safely
escalates immediately. Each request records the full model/effort/output/tool/
cache tuple, and verified receipt finalization avoids a late prefix-breaking
model call.

### Priority 4 — Verified evidence reuse

**Implementation: done; recurring-work benchmark pending.** Immutable,
source-hashed deterministic evidence packets now include workspace, dependency,
Python/tool-version, TTL, provenance, invalidation, and verification receipts.
Mutation, shell, patch, and model output can never enter this store.
### Priority 5 — Local retrieval micro-agent

**Implementation: done; retrieval A/B pending.** The gated bounded loop, exact
source-hashed packet, deterministic fallback, local-only optional planner,
workspace-aware cache, mode controls, and redacted decision metadata are wired
through native and managed LemonCode paths. Explicit-file tasks exit before a
workspace scan and make zero planner calls.

### MCP policy — hold steady

Do not add a mandatory lazy broker. Treat the current hybrid exposure as a
guardrail and change it only if trace data proves fewer total calls, equal tool
selection, and no quality regression.

## Implementation milestone

**Status: implementation closed; controlled acceptance evidence open.**

Delivered in this milestone:

- redacted decision traces and `off|shadow|enforce` controls;
- Output Governor V2 and verifier-gated deterministic receipts;
- adaptive provider cache TTLs, selective breakpoints, stable cache keys/handles,
  route hysteresis, and compaction rewrite economics;
- calibrated expected-total-cost phase routing;
- source-hashed, receipt-gated cross-session evidence reuse;
- the bounded local retrieval micro-agent and local-only optional planner;
- hybrid MCP exposure with zero compulsory discovery calls.

The default remains `shadow`: it records proposals while preserving the legacy
provider-visible behavior. `enforce` is opt-in until each lever passes its
predeclared quality/cost gate. `off` is the one-setting rollback for V2 policy
and writes no decision, routing-outcome, adaptive-cache, or evidence state. Patch/model-answer reuse and a
pure-lazy MCP broker remain intentionally excluded for correctness and call-count
reasons.

Remaining work is measurement, not another feature placeholder: frozen replay
corpora, controlled provider A/B runs, accepted-change quality gates, and linked
reports.

## Validation snapshot — 2026-08-02

**Implementation gate: done. Product acceptance gate: open.**

- The consolidated savings/host release gate passes: **115 passed, 7
  deselected**, covering the six levers, LemonCode/native/managed paths, OpenAI
  and Anthropic gateway protocols, both MCP profiles, daemon IPC, and the SDK
  boundary. The executable tests live in
  [`tests/core`](../../tests/core), [`tests/gateway`](../../tests/gateway), and
  [`tests/infra`](../../tests/infra). A follow-up MCP HTTP/profile gate passes
  **23 tests**, including sorted discovery manifests in both profiles.
- `uv build` produces both the source distribution and wheel;
  `npm --prefix frontend run typecheck` passes; `git diff --check` passes.
- Both permanent entry points are executable: `lc code --help` and
  `lemoncode --help`. `lc optimize decisions --json` returns the redacted
  seven-day decision/routing summary without requiring an existing trace.
- The initial repository-wide run completed with **4,870 passed, 9 failed, 1
  skipped**. Six savings/host-attributable failures were corrected (core-profile
  schema ordering, profile-isolated surface assertions, local IPC/client
  boundaries, and relative-import SDK detection); their exact reruns now pass.
  The three remaining failures reproduce independently in
  [`test_telegraphic_retry_invalid.py`](../../tests/benchmarks/test_telegraphic_retry_invalid.py)
  (scratch-repository setup counted as benchmark subprocess work) and
  [`test_minify_projection.py`](../../tests/core/test_minify_projection.py)
  (a pre-existing fuzzy-ambiguity expectation). They are recorded here rather
  than being silently changed as part of the savings milestone.

This snapshot is local regression evidence, not a savings claim. No paid model
A/B has run, and no lever advances to **Complete** until the acceptance gates
above have a linked controlled report.

## Update protocol

When changing a status in this document:

1. Link the implementation and tests.
2. Link a controlled benchmark or label the number as a counterfactual/target.
3. Record the quality gate as well as savings.
4. State whether the feature is default, opt-in, or shadow-only.
5. Keep estimates non-additive.
6. Never mark a feature complete from unit tests alone.

## Decision log

| Date | Decision | Reason |
| ---- | -------- | ------ |
| 2026-08-02 | Keep the OpenCode-derived LemonCode fork and own the model boundary in LemonCrow. | Preserves upstream UI work while giving LemonCrow full control of prompts, tools, routing, cache, compaction, stopping, and spend. |
| 2026-08-02 | Use hybrid MCP exposure; reject compulsory lazy discovery. | A search-first broker can add a call to every small task and can select a fuzzy match incorrectly. |
| 2026-08-02 | Implement measurement/shadow mode before aggressive routing or a local micro-agent. | Savings without verified outcome data can silently trade away correctness. |
| 2026-08-02 | Build Output Governor V2 before the local micro-agent. | It targets a broad, already-paid cost surface with lower dependency and retrieval-miss risk. |
| 2026-08-02 | Gate local retrieval before scanning and allow only explicitly local planner identifiers. | Obvious file tasks must add zero retrieval/model calls, and a cost firewall must never silently invoke a paid cloud planner. |
| 2026-08-02 | Apply verified/local evidence only in enforce mode. | Default shadow mode must measure proposals without changing provider-visible behavior; off must be a true rollback. |
| 2026-08-02 | Add route hysteresis and retain mandatory eager core tools. | Small theoretical savings do not justify breaking a warm cache lane or adding a discovery call to ordinary coding work. |
