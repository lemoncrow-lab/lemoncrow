---
name: perf-review
argument-hint: <the code change or surface to verify, e.g. an endpoint or page>
description: "Verify a code change against measured performance gates — latency & tail-latency regression, profiler-confirmed hot paths, memory/leak soak, I/O & wire budgets, and scaling — by running it, not reading it. Use for 'perf review', 'did this slow down', 'check for a regression', or /perf-review. Enforces performance quality; does not review general code (use /code-review)."
---

> **Active** — do not call `Skill("lemon:perf-review")` again.

# Performance review

Gates a **code change against its performance bar** by running it — five measured signals: **latency & throughput** (tail incl.), **hot-path truth**, **memory & resource leaks**, **I/O & wire budgets**, **measured scaling**. Any language/project: discover repo's own benchmark/profiler/load/query-log/browser-perf tooling; never assume stack. Weighs every cost in context (Guardrails). Not general code review (use `/code-review`); no inline auto-fixes — engineer owns fix. Opt-in remediation on request: one solver per blocker, re-measured before merge (step 11).

On invoke: state measurement plan; fixes opt-in — per-blocker solvers, re-measured before merge. Then gather inputs.
Whatever the target's phrasing — a change to check, or a question about perf-review itself —
it's the surface to measure; never substitute a hand-written explanation for actually running
the gates below.

## Operating loop

1. **Ground the target and baseline.** First discover project's own perf tooling + invocation (`CLAUDE.md`/`AGENTS.md`, README, CI config, dependency manifest): bench runner, profiler, load generator, query log, (UI) browser-perf. Examples — bench: `pytest-benchmark`, `go test -bench`, `cargo bench`/`criterion`, JMH, `hyperfine`, own suite; profiler: `cProfile`/`py-spy`, `pprof`, `perf`, `async-profiler`, Instruments, browser devtools; load/soak: `wrk`, `k6`, `locust`, `ab`; memory/leak: `tracemalloc`, `memray`, `valgrind`, `heaptrack`, `/proc` + fd counts. Page/frontend: Core Web Vitals (LCP/INP/CLS/TBT) via project browser tooling (Lighthouse, Chrome DevTools, Playwright traces); host web-performance skill present → delegate page-load measurement. Surface defaults to the invocation argument (`/perf-review <target>`) if given — never re-ask it. Remaining gaps → one `AskUserQuestion` call, minimum: surface (only if not given via argument); baseline (default: pre-change code via VCS — working-tree stash or parent commit); thresholds (defaults: **5% wall-time on p50 _and_ p99**, **0 new allocations on the hot path**, **0 net heap/handle growth across the soak**, **0 new queries/round-trips**); representative workload; **input sizes** for scaling curve; **soak iteration count**. Confirm exact commands per side-effect guardrail before any run.
2. **Establish the baseline.** Measure unchanged code first (stash diff or checkout baseline via VCS). Capture full latency distribution (p50/p95/p99), not mean only.
3. **Measure the change.** Same bench + workload on changed code — same machine, inputs, iterations beating noise. Record variance + distribution, not one number.
4. **Gate — latency & throughput.** Change-vs-baseline wall-time/throughput delta beyond threshold _and_ outside noise band = **Blocker**. Gate **tail (p95/p99)**, not p50 only — median flat, p99 blown = fail. Include **cold-start/warmup/first-call** cost, **time-to-first-byte** (streaming), **latency under representative concurrency** (profiler-surfaced lock contention, queueing count). Page surfaces: regressed Core Web Vital (LCP/INP/CLS) = **Blocker**.
5. **Gate — hot path.** Profile change under workload (stack's profiler); confirm real top cost centers incl. **off-CPU/lock-wait** on concurrent loads. Uncorroborated hot-path claim, or fix aimed at non-hot path = **Blocker**. Usual CPU-burn shapes: **busy-wait/hot-poll loops**; **retry storms** (no backoff/attempt cap) burning idle cores; **per-call re-creation of expensive resources** (client/connection/pool/compiled artifact rebuilt per call → connect/TLS/setup churn under sustained load); **redundant recompute**, **O(n²) inner loops**; **lock contention from unbounded concurrency**. Each = **Blocker** only if profiler-material on critical path. Before labeling waste, name **feature served** — feature-paid work ≠ waste even ranked high (Guardrails: product guarantee).
6. **Gate — memory & resource leaks.** Stack's memory/allocation profiler under workload: peak memory/allocations + **GC pressure** (allocation rate, pause time/frequency). Then **soak** many iterations: heap/RSS **and** open fds, sockets, DB connections, threads must return flat. **Monotonic growth = leak = Blocker** — single peak reading can't catch. RAM shapes to target: **unbounded caches/maps/sets, no eviction/TTL**; **per-session/per-turn accumulators, history, buffers never trimmed** (worse if deep-copied each call); **large payloads pulled wholesale into memory** (whole file, base64 blob, full embedding matrix/result set, no size cap) — **spike** on big input, **leak** if never released on refresh/reindex. Soak must **exceed any eviction cap** — below-cap run proves nothing above it. Growth beyond budget = **Blocker**.
7. **Gate — I/O & wire.** Under workload measure: **query count and N+1**; **query plan/index usage** (`EXPLAIN` — silent full-scan = regression even at unchanged count); **cache hit-rate** (caching changes); **syscall/disk-IO volume**; **network round-trips**; **response payload size/bytes over wire** (agent/LLM surfaces: tokens per call/response); shipped **artifact/bundle/binary size**. Growth beyond budget = **Blocker**.
8. **Gate — scaling & worst-case.** Never infer complexity from one point: measure **growing input sizes**, fit curve. Superlinear where linear claimed (or worse than stated) = **Blocker**. Probe **worst-case/adversarial inputs** — data skew, pathological regex/catastrophic backtracking, degenerate/deeply-nested structures; collapse on realistic worst case = **Blocker**. Probe **fan-out ceilings**: work scaling with caller-controllable count (threads, processes, subprocesses, tasks, connections, worktrees) — drive count up, watch process/thread/fd/memory totals. **Unbounded fan-out, no ceiling = resource-exhaustion Blocker** even with cheap units — one call → thousands of units → OS process/fd/memory exhaustion.
9. **Critique (advisory only).** Theoretical complexity, micro-optimizations, unmeasured "could be faster" = **Warnings**, never blockers — speculation not gate-able. Measured but immaterial on critical path = also **Warning**; **Blocker** = material costs only (Guardrails: out of budget).
10. **Verdict.** End with exactly one fenced JSON block (final element of review), caller-parseable:

```json
{
  "verdict": "NEEDS_FIX",
  "gates": {
    "latency": "fail",
    "hot_path": "pass",
    "memory": "pass",
    "io_wire": "pass",
    "scaling": "pass"
  },
  "baseline": "parent commit (HEAD~1) vs working tree",
  "measurements": {
    "latency": "search() p50 4.1ms -> 6.8ms (+66%), p99 9.0ms -> 31ms (+244%); noise band +/-3%",
    "soak": "1000 iters: RSS flat, fds flat (no leak)",
    "scaling": "fit exponent 1.0 over 4 input sizes (linear, as claimed)",
    "iterations": 200
  },
  "blockers": [
    "search() +66% p50 / +244% p99 wall-time vs baseline (threshold 5%) — the index is re-walked in full on every call instead of reused"
  ],
  "warnings": [
    "a hot-loop allocation in the parser is wasteful but off the measured hot path (0.4% of samples)"
  ],
  "not_checked": [
    "production-scale dataset",
    "sustained concurrent load",
    "cold OS page cache",
    "GC behavior under memory pressure"
  ]
}
```

11. **Remediate (optional, user-gated — never automatic).** `NEEDS_FIX` → engineer owns fix by default. User opt-in only (confirm via `AskUserQuestion` after verdict); reviewer **never hand-edits product code**. **You stay the orchestrator**: spawn solvers via host sub-agent capability — create worktrees, dispatch, re-measure, open PRs. Never hand remediation to a workflow/swarm engine running end-to-end without you — you own the loop. Per blocker, own pipeline, **independently**:
    1. **Isolate.** One **git worktree per blocker** (host worktree/swarm/sub-agent capability; else `git worktree add`). One finding, one worktree — no collisions, masking, or all-or-nothing bundle merges.
    2. **Spawn one sub-agent per blocker, yourself.** Host sub-agent tool, one solver per finding/worktree, orchestrated directly. Hand each only its finding: measured evidence (numbers, profiler/soak output, exact cost center) + minimal-fix hint. No solver takes two findings; no refactor scope-creep.
    3. **Re-measure _and_ re-verify the feature — don't trust the diff.** Solver done → (a) **re-run finding's failed gate(s)** in its worktree — identical harness, workload, inputs, warm/cold state, iteration count; gate must **pass**, no previously-passing gate regressed. Measure the right thing — change-blind metric (net-growth on file **rewrite**) or fix-missing workload (soak below eviction cap) proves nothing; (b) **re-verify product guarantee holds** — clearing number via degraded freshness, durability, ordering, or accuracy = **false solution**. Gate not `pass`, or feature broken → not done — send back or report unresolved; never merge.
    4. **Review.** Show user per-finding **before → after numbers**, not just "fixed".
    5. **Merge gate.** Merge to `main` (repo convention — PR or direct) **only** when (a) re-run proves gate cleared on same workload, (b) fix **preserves product guarantee** (no false solution), (c) user approves finding's numbers. Rejected → discard worktree. Merge per-finding — each fix judged on own evidence.

## Guardrails

- **Measure, don't eyeball.** Blocker cites number from executed run, never code-reading guess. No number → not a blocker.
- **Discover the stack; don't assume it.** Use project's own benchmark/profiler/load/query-log/browser tooling + conventions. Never hardcode one language's commands — infer from repo or ask. **Never reference another project's internal benchmarks** (runs against user's repo, not skill's home); rediscover repo tooling every time.
- **Averages lie — gate the tail.** p50 flat, p99 blown = possible; capture + gate full latency distribution, not mean only.
- **A leak needs a soak, not a snapshot.** Leaks = growth across many iterations; single peak reading detects none. Run soak (heap **and** fds/sockets/connections/threads) or set `memory` gate `skipped` and say so.
- **Verify Big-O by measuring, not reading.** Complexity claim (linear, quadratic→linear) = ≥3 growing input sizes + fitted curve; one point proves nothing.
- **A green microbenchmark is not a green verdict.** Synthetic benches miss cold cache, real data shapes, concurrency, tail latency, GC pressure. Unmeasured → `not_checked`, hand to human.
- **A number out of budget is not automatically a Blocker — weigh it against the real critical path.** Microbenchmark breach or self-chosen absolute bar without baseline = hypothesis, not verdict. Contextualize end-to-end: µs–ms per-call framing before seconds of model inference, network I/O, or user think-time = **Warning**. **Blocker** = material only — dominant on critical path, **unbounded/compounding** (leak, O(n²), per-session growth), user-perceptible, or explicit SLA breach. No blockers from synthetic bars real workload never feels.
- **Absolute resource risk counts, not only regression vs baseline.** Already-unsafe code shows **no delta**: unbounded cache, no eviction; missing fan-out ceiling on caller-controlled N; whole-file/base64/full-matrix load, no size cap; per-call rebuild of expensive resource. Hazard the changed surface **owns, feeds, or newly exercises** = in scope even if pre-existing. Name it; **confirm by driving the workload** — soak past eviction cap, push count up, feed large input — never by reading alone. Materiality still applies: bounded, provably-small structure = **Warning**, not Blocker.
- **Speed that costs a product guarantee is a regression, not a fix.** Before flagging hot path or accepting remediation, name what code is for — much 'overhead' pays its way: near-realtime sidecar for UI/statusline, routing/recommendation decision, durability `fsync`/flush, ordering guarantee, audit log, accuracy computation. Clearing a gate by **batching, deferring, dropping, sampling, or coarsening** feature-critical work — staling near-realtime signal, weakening durability/ordering, lowering accuracy — = **false solution**: reject despite improved number. State guarantee (freshness/visibility-latency, durability, ordering, accuracy, correctness); prove fix **preserves** it. Cost = feature's genuine price → verdict = no change, or off-critical-path move **only if** guarantee holds end-to-end.
- **Compare like for like.** Same machine, inputs, iteration count, warm/cold state both runs. Report variance; ignore deltas inside noise band.
- **No baseline, no regression claim.** Unchanged code unmeasurable → latency, memory, I/O, scaling gates `skipped`; verdict defaults `NEEDS_FIX`.
- **Remediation is opt-in, orchestrated by you, never inline (see step 11).** One finding → one worktree → one solver, minimal fix; re-run failed gate, identical harness, before merge — diff-reading ≠ proof, green re-measure is. No merge without proof, guarantee intact, user approval.
- **Running benches, profilers, load, and soaks is a side-effect.** Confirm command via `AskUserQuestion` before running unless repo already authorizes.
- **Default to `NEEDS_FIX`.** `DONE` requires positive proof every gate passed; skipped gate ≠ pass.
