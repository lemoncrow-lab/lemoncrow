# LemonCrow — sales one-pager

> **Internal framing note.** This sheet leads with **cost**. The public homepage
> leads with sharpness per `docs/marketing/strategy.md` §1 ("do not lead with
> cost"). Deliberate split: the landing page sells the developer, this sheet
> sells the person who signs the invoice. Correctness is the guardrail here
> (§9), not the headline.
>
> **Claim discipline.** Every number below is either **MEASURED** (matched
> benchmark, raw data public) or labeled **TARGET** (design estimate, not yet
> measured end to end). Never present a TARGET as a result. Technical buyers
> check, and we have already published our own regression to prove we don't
> round in our favor.

---

# Cut your coding-agent bill ~30%. Same model. Same host. No worse output.

**$234.84 → $165.45** on SWE-bench Verified · **44.9% fewer tokens** ·
**67% cheaper** on repo exploration — and it resolved **more** tasks, not fewer
(92.8% vs 80.8%).

LemonCrow is a local runtime that installs under Claude Code, Codex, Copilot,
opencode, and Cursor. You keep your model, your provider contract, your editor,
your workflow. It changes one thing: **what is allowed to enter the context
window** — and, if you let it own the loop, **whether an expensive turn happens
at all.**

---

## 1. Where the money actually goes

You are not paying for the fix. You are paying for the agent to find out where
the fix goes. Anatomy of one ordinary task on a real repository:

| Waste | What it costs you |
| --- | --- |
| **The grep loop** | Agent greps a term, gets 40 hits, reads 4 whole files to see which one matters. ~8,000 tokens spent to learn one function name. |
| **Whole-file reads** | It needs lines 120–160. It gets all 2,400 lines — and then carries them for the rest of the session, on every turn. |
| **Log dumps** | One failing `npm test` or `docker build` pastes 3,000 lines into context. The fix is one line. |
| **Re-reading** | 40 turns later the same file is read again, because the first copy was compacted away. You pay twice for the same fact. |
| **The schema tax** | Tool definitions are re-sent on *every single request*. A verbose tool surface bills on every turn of every session, forever. |
| **Verbose replies** | The model narrates what it is about to do. Output tokens are the expensive ones — and they return as input on the next turn. |
| **Turns that shouldn't exist** | "What's the signature of `parse_config`?" does not need a frontier model. It gets one anyway, at frontier prices. |
| **One tier for everything** | A whole session runs on the most expensive model because that is what the host was configured with, including the boilerplate turns. |
| **Runaway sessions** | Nothing stops a looping agent. The meter runs until a human notices. |

The first six are fixed by the **runtime** (§3). The last three can only be
fixed by **owning the loop** (§5) — and that is the part no plugin can do.

## 2. The receipts — MEASURED

Matched runs: same model, same Docker images, same turn caps, same disabled
tools, same verification harness on both sides.

| Benchmark | Baseline | LemonCrow | Cost delta | Quality |
| --- | ---: | ---: | --- | --- |
| SWE-bench Verified, 50 tasks × 5 reps | $234.84 | **$165.45** | **−29.5%** | **+12.0 pp** (92.8% vs 80.8%) |
| SWE-bench Pro, 10 × 5 | $39.01 | **$30.61** | **−21.5%** | +2.0 pp |
| SWE-bench Lite, 10 × 5 | $19.83 | **$17.51** | **−11.7%** | −2.0 pp _(our published miss)_ |
| Terminal-Bench 2.1, 445 trials | $73.75 | **$61.98** | **−16.0%** | tied exactly (351/445 both sides) |
| Repo exploration, 7 large repos | $19.11 | **$6.29** | **−67%** | — |
| Telegraphic Q&A, 20 prompts × 5 | $8.40 | **$4.48** | **−46.7%** | — |
| Cursor CLI, SWE-bench Lite | — | — | **−41.2%** | tokens −39.8% |

Second-order savings that arrive as time rather than tokens: **37.7% fewer
turns**, **37.8% fewer tool calls**, **23.7% less wall-clock** (14.3h → 10.9h on
the flagship run). If you bill engineering time, that column is worth more than
the token column.

The Terminal-Bench run is the cleanest read on pure context efficiency, because
correctness is tied exactly:

| Metric | Baseline | LemonCrow | Delta |
| --- | ---: | ---: | ---: |
| Fresh input tokens | 12.87M | **182K** | **−98.6%** |
| Output tokens | 8.09M | **5.36M** | **−33.8%** |
| Cache tokens | 161.9M | **122.0M** | **−24.6%** |
| Total tokens | 182.9M | **127.6M** | **−30.2%** |
| Cost (both normalized to 1h cache-write rate) | $73.75 | **$61.98** | **−16.0%** |
| Resolved | 351/445 | 351/445 | tied |

Raw runs, reproduction commands, and per-task data: `BENCHMARKS.md` and
`github.com/lemoncrow-lab/benchmarks`.

### Say the honest version of the math — it closes technical buyers

Tokens dropped **44.9%** but the bill dropped **29.5%**. Why: cached context is
billed at a steep discount, so cutting cache reads saves fewer dollars than the
token count implies. We publish it that way on purpose — same reason SWE-bench
Lite stays in the table at −2.0 pp. **A vendor who shows you their worst number
is a vendor whose best number you can trust.** Lead with this on an engineering
call; it is the single highest-converting paragraph in this document.

## 3. How we cut it — six runtime mechanisms

These work everywhere LemonCrow installs, including as a plain MCP plugin inside
someone else's agent.

| # | Mechanism | What it removes from the bill |
| --- | --- | --- |
| 1 | **Ranked code graph** replaces the grep loop | One call returns the symbol, its callers and callees, and exact line ranges — ranked by importance. Kills the read-four-files-to-find-one pattern. |
| 2 | **Exact-range reads** replace whole-file reads | Six projections: `summary` / `outline` / `range` / `exact` / `compact` / `minified`. 40 lines instead of 2,400. Conflicting requests resolve to the most detailed one, because an agent can gist down for free but recovering detail costs a turn. |
| 3 | **Output bounding + spill** caps every result | ANSI strip → dedup-with-count → test-failure extraction → suppress-on-success for noisy commands → head/tail budgets. Oversized output spills to a recoverable file and leaves one standard footer, so recovery is always the same `read` call. |
| 4 | **Run-and-dedup** kills repeat commands | A byte-identical re-run collapses to a one-line "unchanged" marker. The command still executes — nothing goes stale, and there is no cache to invalidate. |
| 5 | **Telegraphic instruction surface** cuts the per-turn tax | Every schema, persona, and skill body is written telegraphically — ~31% smaller schemas shipped on every request, forever. Model replies drop ~46% of prose tokens, which then don't come back as input next turn. |
| 6 | **Durable memory + verified edits** stop rework | State survives compaction and moves across hosts; post-edit contract checks catch the half-finished refactor before it becomes a second paid session. |

**Why it compounds:** fewer turns × smaller turns. That is why tokens, turns,
tool calls, and wall-clock all fall together instead of trading against each
other — and why correctness went **up** while cost went down.

**What gets replaced.** On Claude Code, `lc init` installs five tools and hides
the host's equivalents — one way to do each job, not two:
`code_search` → Grep/Glob · `read` → Read · `edit` → Edit/Write · `bash` → Bash ·
`web_fetch` → WebFetch. A smaller advertised tool surface also means fewer
decisions per turn, so the agent leads with the right primitive instead of
exploring. Third-party MCP servers get the same output bounding through hooks,
so the savings are not confined to our own lane.

## 4. The product line

Four named pieces. Use the names — they make an abstract runtime concrete, and
they give the buyer a map of what they are getting now versus later.

| Product | What it is | Status |
| --- | --- | --- |
| **LemonGraph** | The local code graph. tree-sitter symbol table + call graph of definitions, callers, callees, usages, ranked by centrality. Everything else stands on it. | **LIVE** on every supported host |
| **LemonScout** | The frontier-token firewall. A bounded local pass over LemonGraph's index that reads real source and returns exact, **hash-verified** spans — zero calls to the expensive planning model when it is confident, one small local-model call when it isn't. | **Shipped, A/B pending.** Needs `lc code` |
| **LemonRoute** | Per-turn model routing. Scores the tool, the task text, and session state each turn, then answers with a cheap tier, a mid tier, or a local Ollama / LM Studio model instead of paying frontier price for a whole session. | **Shipped, default = shadow (measures only); `enforce` is opt-in.** Needs `lc code` |
| **LemonCode** | Our own fork of opencode, and the frontend we control end to end. It strips its own system prompt and tool schemas before the gateway call, so you don't pay for a host prompt at the real model and host tools don't run alongside ours. | **LIVE** |

All of it runs through one command — `lc code` — against LemonCode, Codex, or
Claude as the frontend. Nothing forces a frontend switch.

```bash
lc code                                  # auto-pick frontend
lc code --local-retrieval force \        # LemonScout, forced local
  --local-retrieval-model ollama/qwen2.5-coder:7b
lc code --budget cheap --model openai/gpt-5.4   # LemonRoute
lc code --optimization-mode enforce      # apply, don't just measure
lc code --max-cost 2.00                  # hard spend ceiling
lc code --cache-policy 1h                # provider cache economics
```

**Talk track:** _"LemonGraph is the map. LemonScout uses the map to answer
cheap questions without waking the expensive model. LemonRoute decides which
model each turn actually deserves. LemonCode is the frontend where we control
every token on the wire. You can adopt them in that order."_

## 5. The real differentiator: a plugin can't stop a turn from happening

This is the answer to "what makes you different," and it is structural, not a
feature list.

As an MCP plugin inside Claude Code, Copilot, or stock opencode, LemonCrow
improves **what a turn sees**. That is worth ~30% — measured, §2. But the host's
own model still answers every turn, at whatever tier the host chose, and the
host's own loop decides when to stop.

`lc code` reverses that. A token-authenticated loopback gateway owned by
LemonCrow runs the agent loop itself. That unlocks four things a plugin
architecturally cannot do:

| Capability | Why a plugin can't |
| --- | --- |
| **Skip the turn entirely** (LemonScout) | A plugin is called *by* a turn. It cannot decide the turn was unnecessary. |
| **Choose the model per turn** (LemonRoute) | Model selection belongs to the host. A plugin never sees that decision. |
| **Stop on a verified receipt** | The output governor holds the loop open until a successful check has run after the last change. Only the loop owner can hold the loop. |
| **Hard cost ceiling** (`--max-cost`) | Once projected spend would cross the limit, the loop stops before the next turn. A plugin cannot refuse to be called. |

_Be precise about `--max-cost` on calls:_ it is a **backstop, not a target**. It
stops before the next turn with no rollback and no cleanup, so the task may be
left unfinished. Sell it as a runaway-session breaker.

_Be precise about `enforce`:_ the default is **shadow mode**, which measures what
would have been saved and changes nothing. Customers opt into `enforce`. That is
an honesty feature and a de-risking feature at once — **they can run it in
shadow for two weeks and read their own savings number before paying.** That is
the strongest pilot offer we have.

**Owned-loop savings, labeled honestly:**

| Claim | Class |
| --- | --- |
| 16.0% cheaper, 98.6% fewer fresh input tokens, correctness tied (Terminal-Bench, 445 trials) | **MEASURED** |
| LemonCode with the same model/provider: 15–30% beyond MCP-only operation | **TARGET** — architecture estimate |
| LemonCode with verified model routing: 35–60% dollar-cost reduction | **TARGET** — architecture estimate |
| Output governor: ~20% fewer output tokens, ~9.8% total-cost reduction | Arithmetic counterfactual, not a live A/B |
| Cache TTL repricing: $61.98 → ~$55.28 (−10.8%) on that trajectory | Direct counterfactual |
| Verified cross-session reuse: 10–30% on recurring work | **TARGET** — recurring workloads only |

Say "target" out loud. It costs nothing and buys everything.

## 6. Underlying technology

Every dependency earns its place against one rule: **fewer tokens to a more
correct answer.**

| Layer | Technology | Why it's there / what it saves |
| --- | --- | --- |
| Parsing | **tree-sitter** (40+ languages, error-tolerant) | Builds the symbol table and file outlines with no per-language LSP server to install or run. Works on a repo that doesn't currently compile. |
| Code graph | **LemonGraph** (in-house) | Definitions, callers, callees, usages, ranked by **degree-normalized, macro-aware PageRank** so a promiscuous caller can't outrank a real hub. This is the core of the savings. |
| Structural search | **ast-grep** (Rust) | Matches code *structure*, not text — so rename verification and the SAST rule pack don't fire on comments and strings. |
| Lexical search | **BM25 + SQLite FTS** | Exact-match relevance with zero servers to operate. |
| Semantic search | **BGE-Code-v1** embeddings (falls back to SFR-Embedding-Code-400M on low VRAM) | Intent-level matching — "where do we handle retries" finds code that contains none of those words. Runs locally. |
| Vector scan | **sqlite-vector (TurboQuant, 4-bit)** | The Linux kernel's 1.24M × 1536 vector store scans from ~960 MB quantized instead of loading a 7.5 GB matrix into memory. This is why million-symbol repos work on a laptop. |
| Incremental index | **blake3** content addressing | A commit re-indexes only what changed — seconds, not a rebuild. |
| Large repos | **Zoekt** trigram backend | Monorepo-scale search; Linux core indexes on this path in 13.7s. |
| Storage | **SQLite** local · **PostgreSQL + pgvector** at team scale | No server for one developer; a real database when a team shares context. |
| Edits | **diff-match-patch** · **rapidfuzz** · **rope** | Deterministic multi-hunk cross-file edits with fuzzy anchors and safe symbol rename — one call, not patch-per-file guessing. |
| Git | **pygit2 / GitPython** | History archaeology, blame, renames, worktree and swarm management. |
| Web | **trafilatura + BeautifulSoup** | `web_fetch` returns clean Markdown — nav, scripts, and page chrome never reach context. |
| Local models | **Ollama / LM Studio / OpenAI-compatible** | LemonScout's fallback call and LemonRoute's cheapest tier run on-device at zero API cost. |
| Token economics | **tiktoken** · **OR-Tools** · **XGBoost** | Exact token accounting for the savings ledger; a constraint solver allocates budget across steps; learned ranking signals for retrieval and risk. |
| Reliability | **tenacity** · **pybreaker** | Repeated tool failure trips a circuit breaker instead of looping — a looping agent is a running meter. |
| Runtime | **Python 3.12/3.13**, hot paths **mypyc**-compiled to C · **Pydantic v2** | Native-speed parsing, ranking, indexing; every tool payload is a validated model, so schema errors fail fast and cheap. |
| Service | **FastAPI + Uvicorn** (`lcd` daemon) + token-authenticated loopback gateway | The local API, the savings endpoints, and the owned agent loop. |
| Wire protocol | **MCP SDK** | How every host attaches. We modify no agent and fork no editor — except the one we maintain ourselves. |
| Observability | **OpenTelemetry → PostHog/GCP** · **Prometheus** · optional **Langfuse** | Local-first, strict allowlist, one-command opt-out. |
| Distribution | **uv** · **hatchling** · **PyInstaller** | One checksummed binary install. No login, no account, no network dependency at runtime. |

## 7. What we do differently — named

### 7a. Against other code-search and index tools — MEASURED

Retrieval quality on the **same 14 repositories and 7,213 query/gold pairs**,
every provider scored on all five gold kinds so the numbers are directly
comparable. MRR = does the right answer come back first (higher is better):

| Provider | MRR | rec@1 | p95 |
| --- | ---: | ---: | ---: |
| **LemonCrow + semantic** | **0.727** | **0.650** | 390ms |
| **LemonCrow lexical (default)** | **0.676** | 0.582 | 134ms |
| cocoindex-code | 0.557 | 0.457 | 595ms |
| Graft 0.8.2 | 0.514 | 0.433 | 1,770ms |
| codebase-memory-mcp | 0.502 | 0.437 | 541ms |
| fff-mcp | 0.430 | 0.388 | 46ms |
| **serena** | 0.401 | 0.359 | 3,834ms |
| **ripgrep** | 0.376 | 0.320 | 66ms |
| code-index-mcp | 0.343 | 0.296 | 377ms |
| ast-grep | 0.312 | 0.271 | 1,255ms |
| jcodemunch-mcp | 0.299 | 0.226 | 214ms |
| codegraph | 0.296 | 0.267 | 17ms |
| universal-ctags | 0.237 | 0.226 | 1ms |

**~1.9x more accurate than ripgrep**, and ahead of every MCP code-search tool we
could install — at an interactive p95. ripgrep and ctags win raw latency; they
lose on what they find, and a wrong first answer costs another whole turn.

### 7b. Against the other approaches — ARCHITECTURAL, not measured

No matched benchmark exists for these. State them as design differences. Do not
imply we measured them.

| Approach | Examples | The structural difference |
| --- | --- | --- |
| **Cloud RAG index** | Sourcegraph Cody, Augment, hosted "codebase context" MCPs | Your source is uploaded to their index. It goes stale on every commit, and chunk retrieval returns paragraphs without the callers that explain them. **We index locally and incrementally and return a call-graph neighborhood — nothing is uploaded.** |
| **Repo-map in the prompt** | aider's repomap and similar | A static map is re-sent every turn whether the task needs it or not — you pay for the whole map to use one corner. **We retrieve on demand and pay only for the neighborhood.** |
| **Grep-class MCP tools** | serena, code-index-mcp, codegraph (scored above) | They improve *finding text*. The agent still reads whole files to learn which hit mattered — the expensive part. **We return ranked symbols with exact ranges, and we bound the read.** |
| **IDE assistants** | Cursor, in-editor Copilot | Their built-in tools can't be displaced, so any runtime is additive there. Measured: Cursor **CLI** + LemonCrow ~40% cheaper; Cursor **IDE** showed no such saving. We say this before the customer finds it. |
| **Prompt compressors** | LLMLingua-class | They compress noise *after* it is already in the window. Cheaper never to let it in. |
| **Model-routing gateways** | LLM proxies that route by prompt heuristics | They route without knowing the codebase or the session state. LemonRoute scores tool, task text, and session state inside the loop it owns — and LemonScout can remove the call entirely, which no proxy can do. |
| **"Just use a bigger window"** | 1M-token models | A bigger window is more room for noise, and you pay to carry every token on every turn. Headroom is not immunity from context rot. |
| **Bare host agent** | Claude Code / Codex with no runtime | The measured baseline in every table above. |

### 7c. The four-sentence version

1. Everyone else improves *search*. We replace the
   **grep-then-read-whole-file loop** with one ranked call plus an exact range —
   that loop is where the money is.
2. Everyone else works on input. We also bound **output** — logs, tool results,
   and the model's own prose — which is the expensive direction.
3. Everyone else lives inside someone else's loop. With `lc code` we **own the
   loop**, so we can skip a turn, downgrade a turn, or stop the session on a
   verified receipt.
4. Everyone else asks you to upload your repo or switch your editor. We are
   **Apache-2.0, fully local, and host-neutral** — install it and keep
   everything else.

## 8. Scale

Linux kernel core — **1.24M symbols, 4.5M lines** — cold index in **179 seconds**
(13.7s on the trigram backend), with a 4-bit-quantized ANN scan so a
million-symbol semantic index doesn't need gigabytes of RAM. Incremental
re-index on commit is content-addressed, so it touches only what changed. No
index or symbol caps on Enterprise.

## 9. The guardrail: cheaper is worthless if it's dumber

This is the first question a good engineering buyer asks. The answer is on the
record: on the flagship 250-run benchmark LemonCrow resolved **92.8% vs 80.8%**
— **+12.0 percentage points** — while costing 29.5% less. On Terminal-Bench,
correctness tied exactly while cost fell 16%. Cleaner context is not a trade
against quality; the noise was never helping.

Where the correctness comes from:

- **Post-edit contract verification** scans *untouched* files for leftover
  references after a rename or signature change, structurally via ast-grep, so
  comments and strings don't create false hits.
- **Edit-verify gate** runs lint / type / test checks before an edit is accepted.
- **Verified stopping** — in owned-loop mode the session doesn't end on the
  model's say-so; it ends after a successful check has run since the last change.
- **Bundled SAST rule pack** (OWASP/CWE) flags eval/exec, shell injection, SQL
  concatenation, and hardcoded secrets in agent-written code, plus bounded taint
  analysis.

## 10. Security posture — what procurement will ask

- **Does our code leave?** No. Parsing and indexing run on the developer's
  machine. No upload, no hosted index of your source.
- **The model?** Calls go to the provider you already approved and already pay
  for. We do not proxy or resell inference.
- **Auditable?** **Apache-2.0**, engine included — readable source, no black box
  in the security review.
- **Telemetry?** Anonymous, excludes source and prompts, one command to disable
  (`lc telemetry remote off`).
- **Deployment:** self-hosted service and gateway, PostgreSQL backend,
  air-gapped install available.
- **Local models:** LemonScout and LemonRoute's cheapest tier can run entirely
  on-device via Ollama or LM Studio — useful for regulated teams that want fewer
  external calls, not just cheaper ones.

## 11. Tiers

| | |
| --- | --- |
| **Free** | Full local workflow for one developer: ranked code graph, exact-range reads, bounded output, agents and hooks, session recall, multi-worktree swarm. |
| **Pro** | Larger repos and heavier use: Zoekt + semantic index, cross-vendor memory, savings dashboard, optimizer, budget optimization. |
| **Enterprise** | Shared team context across repositories, role-based permissions, governance policy, audit export, retention controls, SSO, no index or symbol caps, priority support. Custom pricing. |

Full enterprise benefit list: `docs/marketing/enterprise-benefits.md`.

## 12. Who to sell to

Mid-size engineering teams (~20–500 developers) already spending real money on
coding agents against large, long-lived codebases. Skip solo founders (cloud
credits, no price sensitivity) and mega-enterprises (compliance cycle longer than
the sales cycle). The buyer is a VP Eng or platform lead who has seen the agent
bill and can sign without convening a committee.

**Qualifying questions:**

1. What are you spending per developer per month on coding agents right now?
2. How big is the repository the agents work in — files, and does anyone
   understand all of it?
3. Do your agent sessions run long enough to hit compaction?
4. Who sees the bill, and do they know which team generated it?
5. Is anyone allowed to upload source to a third-party index? (If no — that
   disqualifies most of our competition on the spot.)

## 13. Objection handling

| They say | You say |
| --- | --- |
| "We have cloud credits, cost isn't an issue." | Then buy the other half: +12.0 pp resolved, 37.7% fewer turns, 23.7% faster. Credits run out; wall-clock is your engineers' day. |
| "Sounds like it makes the model dumber." | It resolved **more** tasks in the same harness. §9. |
| "We already use \<Cody / Augment / serena\>." | Show §7a — same corpus, same 7,213 pairs, published. Then §7b: theirs is an index, ours is a runtime that also bounds output and can skip a turn. |
| "Our code can't leave the building." | It doesn't. Indexing is local, source is Apache-2.0 and readable, model calls go where they already go. |
| "How do I know the savings are real for *us*?" | Run `--optimization-mode shadow` for two weeks. It measures what it *would* have saved and changes nothing. You read your own number before paying. |
| "What if it runs away and burns budget?" | `--max-cost` stops the loop before the next turn. Backstop, not a target — no rollback, task may be unfinished. |
| "Can we stop developers bypassing it?" | Honestly: a laptop-side runtime can't force adoption. Ship it in the dev image, gate it in CI, or route agent traffic through the gateway — that last one is real enforcement. |
| "Is this a wrapper around Claude Code?" | No. It's a runtime with its own index, graph, tool surface, and — in `lc code` — its own agent loop. We also maintain our own frontend, LemonCode. |

## 14. Demo script (10 minutes)

1. **`lc init` on their repo** — show the index build and symbol count. Real
   repo, live, on their machine.
2. **One `code_search`** — symbol, callers, callees, exact ranges in one call.
   Contrast with the grep-and-read transcript they recognize.
3. **Session replay** — replay a recorded session and point at the repeated
   searches and oversized reads. No model reruns; nothing to fake.
4. **`lc savings`** — the ledger with actual vs counterfactual cost.
5. **Shadow mode** — set it running and tell them to check the number in two
   weeks. That is the close.

## 15. Next step

30-minute call, then a pilot: we run a **matched before/after benchmark on your
repository**, or you run shadow mode yourself. You keep the numbers either way.

```bash
curl -fsSL https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download/install.sh | bash
cd your-project && lc init
```

[lemoncrow.com](https://lemoncrow.com) · [github.com/lemoncrow-lab/lemoncrow](https://github.com/lemoncrow-lab/lemoncrow)

---

## Appendix — jargon decoder

| Term | Say it like this |
| --- | --- |
| **Context window** | The agent's short-term memory for one session. Fixed size. Everything competes for it. |
| **Token** | The billing unit, roughly ¾ of a word. You pay for what goes in and what comes out. |
| **Cache read / cache write** | Reused context, billed at a discount. Why a 45% token cut is a 30% bill cut, not 45%. |
| **Turn** | One round trip: agent thinks, calls a tool, reads the result. Fewer turns = less time and less money. |
| **Code graph / LemonGraph** | A map of which function calls which. Lets us hand over the relevant *neighborhood* of code instead of a pile of search hits. |
| **tree-sitter** | The parser that reads 40+ languages to build that map — locally, in seconds, without needing the code to compile. |
| **Centrality / PageRank** | Importance scoring. The function everything else calls ranks above one nobody calls. |
| **BM25 vs embeddings** | Exact keyword match vs meaning-based match. We run both and merge the results. |
| **MCP** | The standard plug that lets any coding agent talk to an outside tool. How we attach without modifying the agent. |
| **Shadow vs enforce mode** | Shadow measures what would have been saved and changes nothing. Enforce applies it. Default is shadow. |
| **MRR / rec@1** | Retrieval accuracy. Higher = the right answer shows up first more often. |
| **SWE-bench Verified** | The industry-standard exam: real bugs from real open-source projects, graded by whether the project's own hidden tests pass. Not a demo. |
| **pp (percentage points)** | 80.8% → 92.8% is +12 **points**. Never say "12% better" in front of an engineer. |
