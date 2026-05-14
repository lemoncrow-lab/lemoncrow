# Atelier V3 — Implementation Plan

**Status:** Draft v2 · 2026-05-04
**Owner:** Pankaj (root coordinator)
**Audience:** Small subagents (`atelier:code` / `atelier:explore` / `atelier:review`) executing one
atomic work-packet at a time. Read the [V3 work-packets index](work-packets-v3/INDEX.md) for the
dispatch graph.

**Companion docs:**
[IMPLEMENTATION_PLAN_V3_DATA_MODEL.md](IMPLEMENTATION_PLAN_V3_DATA_MODEL.md),
[V2 plan](IMPLEMENTATION_PLAN_V2.md) (for context on what V3 supersedes),
[V2 work-packets](work-packets/INDEX.md).

---

## 0. What V3 is (and is not)

> **Atelier is a tool and data provider that host CLIs call over MCP. It does not run an agent
> loop, does not call LLMs, does not spawn subagents, and does not hold API keys.**

The host CLI (Claude Code, Codex, opencode, Gemini, or a user's own agent) owns:

- the conversational loop;
- model invocation and billing;
- tool dispatch;
- subagent spawning;
- compaction and checkpoint;
- the user's API keys.

Atelier owns:

- ReasonBlocks (procedure store) and rubric gates — surfaced as MCP tools;
- the `memory_*` MCP tools (upsert/get/list/recall/archive);
- the deterministic context-savings tools: `search`, `edit`,
  `atelier sql inspect`, AST-outline-first reads;
- the `lint` advisory and the `verify` verifier;
- the trace store (`trace`);
- the lesson pipeline that processes traces in the background and surfaces candidates for human
  review.

V3 keeps that boundary unchanged from V2. The MCP tool surface is preserved. What V3 changes is:

1. **Truth.** V2 shipped three things that aren't what they look like — `stub_embedding` is a
   SHA-256 hash dressed as an embedding; the "81 % savings" headline is a hand-written YAML
   constant; the `LettaMemoryStore` is a dual-write proxy, not a Letta-backed store. V3 fixes
   each of these.
2. **Memory backend.** V3 lets a user pick `sqlite` or `letta` as the memory backend behind the
   existing `memory_*` MCP tools, and makes Letta a real primary (not a side-write proxy).
3. **Lesson promoter quality.** The V2 promoter clusters by SHA-hash collisions. V3 rebuilds it
   on real embeddings and hits the precision target that was filed but never met.
4. **Honest benchmark.** V3 replaces the hand-written 81 % with a measured replay benchmark of
   how Atelier's tools change a host's token budget.

V3 introduces no new runtime, no new executor, no new agent framework dependencies, and no new
LLM clients. It is, in a sense, "V2 minus the lies, plus a real Letta backend, plus a real
lesson signal."

---

## 1. Non-goals (hard exclusions)

These are exclusions that V3 enforces in code review and CI. Any packet that violates them is
rejected.

- ❌ **No agent executor inside Atelier.** No `AgentExecutor`, no LangGraph, no Deep Agents,
  no state-machine runtime, no `atelier run <task>` CLI. The host owns the loop.
- ❌ **No LLM calls from Atelier.** The MCP server, the capabilities, the lesson pipeline,
  and any background worker may not import `anthropic`, `openai`, `litellm`, `google.generativeai`,
  `cohere`, `mistralai`, or any other model client. There is one exception, narrowly scoped:
  **embeddings**. The `Embedder` protocol may use `sentence-transformers` (local model, no
  API call) or, optionally, `openai` for `text-embedding-3-small` if the user explicitly enables
  it. Embeddings are not LLM completions; they are vector lookups. Other than embeddings, the
  rule is absolute.
- ❌ **No subagent spawning.** The host's `Agent` tool spawns subagents. Atelier never does.
- ❌ **No checkpoint/resume.** That is the host's responsibility. Atelier's traces are records,
  not resumable state.
- ❌ **No marketing language without a measurement.** "AI-native", "self-improving",
  "intelligent", "autonomous" are banned in code, packet titles, README, and PR descriptions
  until a CI-asserted measurement exists.

---

## 2. The audit findings V3 fixes

The 2026-05-04 internal audit ([summary in this commit's PR description]) found:

| Finding | Affected modules | V3 packet |
|---|---|---|
| `stub_embedding` (SHA-256 feature hash) is wired into `LessonPromoter`, archival "hybrid" ranking, and at least 6 other call sites. Any "semantic" claim downstream is broken. | `infra/storage/vector.py`, `core/capabilities/lesson_promotion/`, `core/capabilities/archival_recall/` | **WP-33** |
| The "81 % savings" headline is computed from hand-written YAML constants; nothing is measured. | `benchmarks/swe/savings_bench.py`, `benchmarks/swe/prompts_11.yaml`, `README.md`, `docs/benchmarks/v2-context-savings.md` | **WP-34** (retract) + **WP-50** (replace with real measurement) |
| `LettaMemoryStore` dual-writes to Letta and SQLite; reads prefer Letta with SQLite fallback; `insert_passage` and `record_recall` go to SQLite only. The store is not actually Letta-backed. | `infra/memory_bridges/letta_adapter.py` | **WP-35** (decide single primary) + **WP-39** (real Letta-primary path) |
| Sleeptime "summarizer" is template `groupby` + truncation; counts as a savings lever in V2 docs but compresses no meaning. | `core/capabilities/context_compression/sleeptime.py` | **WP-36** |
| `LessonPromoter` clusters via SHA-hash fingerprint; precision target `≥ 0.7` was filed but never met. | `core/capabilities/lesson_promotion/capability.py` | **WP-47** |

Everything else V2 shipped — ReasonBlocks, rubric gates, plan-check, rescue, trace recording,
`search_read`, `batch_edit`, `sql_inspect`, AST outline, MCP gateway, frontend pages — is real,
in-scope, and **kept verbatim** by V3. The audit was a quality check on specific subsystems, not
a wholesale verdict.

---

## 3. Architecture (unchanged from V2)

```
                    ┌─────────────────────────────────────────────────────┐
                    │              Agent Host (Claude Code, Codex…)       │
                    │                                                     │
                    │   - Owns the conversational loop                    │
                    │   - Calls LLMs (host's API key)                     │
                    │   - Dispatches tool calls                           │
                    │   - Spawns subagents                                │
                    │   - Owns checkpoint / compaction                    │
                    └───────────────┬─────────────────────────────────────┘
                                    │ MCP stdio  (tool calls + responses)
                    ┌───────────────▼─────────────────────────────────────┐
                    │                  atelier-mcp gateway                │
                    │           (tool surface, identical to V2)           │
                    └──┬──────────────────┬─────────────────────┬─────────┘
                       │                  │                     │
        ┌──────────────▼──────┐   ┌───────▼─────────┐   ┌───────▼─────────┐
        │ ReasonBlock store   │   │ Memory tools    │   │ Lesson pipeline │
        │ + rubric gates      │   │ (memory_*)      │   │ (background)    │
        │ + plan-check        │   │                 │   │                 │
        │ + rescue + trace    │   │ Backend, picked │   │ Reads recorded  │
        │                     │   │ by config:      │   │ traces; embeds; │
        │ + search_read       │   │   sqlite (default)  │ clusters;       │
        │ + batch_edit        │   │   letta (opt-in)│   │ surfaces lesson │
        │ + sql_inspect       │   │                 │   │ candidates for  │
        │ + AST outline       │   │ Embeddings via  │   │ human review.   │
        │                     │   │ sentence-       │   │ No LLM calls.   │
        │                     │   │ transformers    │   │                 │
        └─────────────────────┘   └────────┬────────┘   └─────────────────┘
                                           │
                                  ┌────────▼─────────────────────────┐
                                  │  Storage:                        │
                                  │   SQLite (default, dev)          │
                                  │   Letta sidecar (optional)       │
                                  └──────────────────────────────────┘
```

**No box says "executor". No arrow goes from Atelier to an LLM API.** This is the same
architecture as V2; what V3 fixes is what's *inside* the boxes.

### 3.1 Backend choice

A single config knob picks the memory backend:

```toml
[memory]
backend = "sqlite"   # default, dev, unit tests
# backend = "letta"  # opt-in: requires Letta server reachable at ATELIER_LETTA_URL
```

The MCP tool surface is identical in both modes. The host doesn't know or care which backend is
in use. (This is what WP-35 commits to and WP-39 implements.)

### 3.2 Embeddings

`Embedder` is the V2 protocol. V3 enforces that **every** embedding call goes through it. The
default backend is `LocalEmbedder` (sentence-transformers, MiniLM-L6-v2, ~80 MB, no API call).
Optional backends:

- `OpenAIEmbedder` (`text-embedding-3-small`) — only when the user explicitly sets
  `OPENAI_API_KEY` and `[embeddings].backend = "openai"` in config.
- `LettaEmbedder` — only when Letta is the memory backend and exposes an embed endpoint.
- `NullEmbedder` — returns `[]`; recall falls back to BM25 only. Used in CI when no embedder
  is available.

`stub_embedding` (the SHA-hash trick) is **deleted** in WP-33. Any reference is a CI failure.

### 3.3 Routing — V2 advisory, kept as-is

Atelier's V2 routing capability (`quality_router/policy.py`, WP-25..28) is an *advisory* MCP
response inside `lint`. The host reads `routing_advice` and decides what to do —
or ignores it if the host has no per-step model switching. V3 does not change this surface.

If, later, you decide to improve the routing algorithm, that is a separate decision and a
separate packet — not part of V3.

---

## 4. Phasing

V3 is small. 8 packets, 3 phases, ~10 dev-days sequential.

```
Phase Z — Truth & cleanup       WP-33 … WP-36   (4 packets, blocking)
Phase I — Differentiation fix   WP-39, WP-47    (2 packets)
Phase J — Migration & honesty   WP-49, WP-50    (2 packets)
```

### Why Phase Z first

Same reason as in V2: the cleanups unblock honest measurement. Without WP-33, lesson promoter
"precision" is meaningless. Without WP-34, the README is making a claim the code can't back up.
Without WP-35, WP-39 has to deal with data divergence on top of integration. Without WP-36, the
"savings levers" list contains a fake.

### Phase I

Two packets, both honesty-driven:

- **WP-39** implements Letta-as-real-primary for the `memory_*` MCP tools (WP-35 only resolves
  the contradiction; WP-39 makes the chosen path actually work).
- **WP-47** rebuilds `LessonPromoter` clustering on real embeddings and hits the V2 precision
  target.

### Phase J

- **WP-49** documents the V2→V3 transition. The MCP tool surface is unchanged, so most users
  do nothing. The migration doc covers config knobs and deprecation flags.
- **WP-50** publishes the first measured savings benchmark, replacing the retracted 81 % story.

---

## 5. Success metrics

V3's metrics are deliberately fewer than V2's because the runtime is unchanged. We assert only
the things V3 actually fixes.

| Metric | V2 baseline | V3 target | Test |
|---|---:|---:|---|
| `stub_embedding` references in `src/atelier/` | 8+ | **0** | grep gate (`tests/infra/test_no_stub_embedding.py`) |
| Bare unmeasured percentages in README and benchmark docs | several | **0** | doc gate (`tests/docs/test_readme_no_unmeasured_claims.py`) |
| Letta dual-write code paths | present | **0** | grep gate + behavioral test |
| Sleeptime "savings" recorded in telemetry without measurement | present | **0** | grep / behavioral gate (per WP-36 path chosen) |
| `LessonPromoter` precision on 200-trace fixture | unmeasured | **≥ 0.7** | `tests/infra/test_lesson_promotion_precision.py` |
| Atelier modules importing `anthropic` / `openai` / `litellm` / model clients (excluding the embeddings module) | n/a (V2 didn't have such imports either) | **0** | CI grep gate (new in V3 — defends the boundary) |
| Honest savings benchmark published with real numbers | no | yes | `make bench-savings-honest` produces a `BenchmarkRun` row; doc lists it |

The single hard release gate for V3 is: **every percentage in the README and docs links to a
measurement, and no module under `src/atelier/` (outside the embeddings package) imports a
model client.**

---

## 6. Dependency strategy

V3 adds **zero new runtime dependencies.**

- Letta — already a V2 optional extra (`letta-client>=1.7.12` in the `memory` extra).
- Embeddings — already a V2 optional extra (`sentence-transformers` in the `vector` extra).

Phase Z packets remove code; they do not add deps. Phase I packets reuse the existing extras.
Phase J packets are documentation and benchmark scripts.

---

## 7. Migration strategy

### V2 users

Most users do nothing. V3 keeps every V2 MCP tool name, signature, and return shape.

What changes:

| Concern | V2 default | V3 default | Action required |
|---|---|---|---|
| Memory backend | implicit dual-write to Letta+SQLite when `ATELIER_LETTA_URL` set | explicit single-primary; `[memory].backend = "sqlite"` if not set | nothing for SQLite users; Letta users add one config line |
| `stub_embedding` | silently used in lesson promoter and ranking | deleted; legacy rows flagged `legacy_stub`; back-fill via `atelier reembed` (WP-47) | run `atelier reembed` once after upgrade |
| Sleeptime lever | counted toward "savings" via templates | either real (WP-36 path A) or removed (path B) | none; reflected in telemetry only |
| 81 % savings claim | in README | retracted, footnoted as "design target" until WP-50 | none |

### Trace continuity

V3 traces are a strict superset of V2 traces. No trace fields are removed; no fields are
required that V2 didn't have.

---

## 8. How a subagent should consume this plan

1. Open [work-packets-v3/INDEX.md](work-packets-v3/INDEX.md). Pick the lowest-numbered packet
   whose dependencies are all satisfied.
2. **Phase Z is blocking.** No Phase I or J packet may start while a Phase Z packet is `ready`
   or `in_progress`.
3. Read the packet file. Read the V3 plan section it links to. Read any V2 packet it
   supersedes (linked in `supersedes:` front-matter).
4. Run the standing Atelier loop: `reasoning` → draft plan →
   `lint` → implement → run packet acceptance tests → `trace`.
5. Mark `status: done` in front-matter and update the V3 INDEX.

Subagents must **not** invent new files outside what a packet specifies, and must **not** add
runtime dependencies. If a packet seems under-specified, raise a comment and stop — do not
improvise scope.

---

## 9. References

### Internal

- [V2 plan](IMPLEMENTATION_PLAN_V2.md) — V3 supersedes this for new work; V2 packets stay
  `done`.
- [V2 INDEX](work-packets/INDEX.md) — the 32-packet history.
- [cost-performance-runtime.md](cost-performance-runtime.md) — V2 routing context, still
  authoritative; V3 makes no changes here.

### External

- [Letta](https://github.com/letta-ai/letta) — Apache-2.0; used as the optional Letta sidecar
  for the `letta` memory backend. Atelier never embeds Letta source.
- sentence-transformers — used by `LocalEmbedder` for the default embedding backend.

V3 does not adopt LangGraph, Deep Agents, LiteLLM, or any other agent-framework dependency.
Those were considered in an earlier draft of this plan and rejected because they would have
required Atelier to call LLMs, which violates the boundary in § 0.
