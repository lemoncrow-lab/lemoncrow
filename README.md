<!-- cspell:ignore Alamofire Excalidraw ast-grep codegraph ctags django jcodemunch nohit okhttp scip serena tokio vscode zoekt beasm Trendshift telegraphese -->

<div align="center">

<img src="docs-site/favicon.png" width="36" height="36" alt="" style="vertical-align: middle;">

# LemonCrow Runtime

### Keep your coding agent sharp on real codebases

LemonCrow runs underneath Claude Code, Codex, and other supported hosts with a local code graph, exact-range reads, bounded output, durable memory, and verified runtime controls. On matched SWE-bench Verified runs: **+12.0pp resolved**, **37.7% fewer turns**, and **23.7% faster** — same model.

[![License](https://img.shields.io/badge/License-Apache--2.0%20%2B%20proprietary%20engine-blue?style=flat-square)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/lemoncrow-lab/lemoncrow?style=flat-square)](https://github.com/lemoncrow-lab/lemoncrow/releases)
[![Stars](https://img.shields.io/github/stars/lemoncrow-lab/lemoncrow?style=flat-square)](https://github.com/lemoncrow-lab/lemoncrow)

[![Claude Code](https://img.shields.io/badge/Claude_Code-supported-blue?style=flat-square)](integrations/claude)
[![Codex](https://img.shields.io/badge/Codex-supported-blue?style=flat-square)](integrations/codex)
[![opencode](https://img.shields.io/badge/opencode-supported-blue?style=flat-square)](integrations/opencode)
[![Copilot](https://img.shields.io/badge/Copilot-supported-blue?style=flat-square)](integrations/copilot)
[![Copilot CLI](https://img.shields.io/badge/Copilot_CLI-supported-blue?style=flat-square)](integrations/copilot-cli)
[![Cursor](https://img.shields.io/badge/Cursor-coming_soon-lightgray?style=flat-square)](integrations/cursor)
[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-coming_soon-lightgray?style=flat-square)](scripts/install_hermes.sh)
[![Antigravity](https://img.shields.io/badge/Antigravity-coming_soon-lightgray?style=flat-square)](integrations/antigravity)

**Matched proof, same model:** 92.8% vs 80.8% resolved · 37.7% fewer turns · 23.7% faster · [raw runs published](BENCHMARKS.md) · [see live savings](https://lemoncrow.com/savings)

[Install](#install-in-30-seconds) · [Working set](#the-clean-working-set) · [Results](#results) · [Code-search benchmark](#one-shot-code-search-vs-10-named-tools) · [Pricing](https://lemoncrow.com/pricing)

[![LemonCrow running inside Claude Code -- statusline tracking cost, context, and savings live](docs/assets/terminal-demo.gif)](https://lemoncrow.com/#terminal)

_Click for the full walkthrough on [lemoncrow.com](https://lemoncrow.com/#terminal)._

</div>

---

## Install in 30 seconds

Run this once:

    curl -fsSL https://install.lemoncrow.com | bash

Then create or sign in to a free LemonCrow account and activate it inside the project where you use your coding agent:

    cd your-project
    lc account login
    lc init

LemonCrow wires in better tools behind the scenes and starts tracking local savings as sessions finish. Remote telemetry is on by default; turn it off anytime with `lc telemetry remote off`.

```bash
lc update
```

Check that everything is connected:

```bash
lc doctor
```

> **Account boundary:** anonymous evaluation includes up to **$50 in measured
> savings** over a rolling 30-day window. Creating or signing into a Free
> account is required for uncapped core usage. Recall, handovers, verification,
> and local multi-worktree swarm are included in Free.

## The clean working set

LemonCrow keeps your existing coding agent and changes the working set around
it:

| Stage      | Runtime behavior                                                                                                                          |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Find**   | Rank symbols, definitions, callers, callees, usages, and exact source ranges before broad file exploration.                           |
| **Read**   | Return an outline or only the requested lines; cap noisy command and web output with recoverable spill files.                         |
| **Carry**  | Preserve useful task state through memory, deduplication, compaction manifests, and handover packets—not the raw transcript forever. |
| **Verify** | Notice code changes without tests or checks, then nudge the agent before it declares completion.                                        |

Open the existing dashboard with `lc dashboard open`, then choose **Map**. The
browser opens the full tracked source map: files, indexed symbols, and only
uniquely resolved call edges. Groups stay spatially grouped; filters split
source, tests, Markdown/docs, config, data, assets, and languages. Search any
symbol, click it for its exact source range, or double-click to focus callers
and callees. Live mode adds glow, edge flow, and camera-follow from the local
run ledger; switching it off leaves the same zoomable, clickable graph in
place. Raw source, diffs, stdout, and stderr never enter the map feed.

![LemonCrow source map: a full repository call graph with 28,462 indexed symbols, 10,349 tracked files, 38,811 map nodes, and 23,894 resolved calls, one function focused to show its callers and callees.](docs/assets/screenshots/map.jpg)

### What actually gets replaced

On Claude Code, `lc init` gives the agent five grounded tools and hides the
equivalent built-ins—one way to do each job, not two. Other hosts use the
strongest equivalent controls they expose.

**Find things in one shot** -- no wandering the codebase call after call.

| LemonCrow tool | Replaces (hidden from the model) | Why                                                                                                                                                                       |
| -------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code_search`  | Grep, Glob                       | One call returns the symbol, its callers/callees, and ranked source -- no grep-loop-then-read-whole-file. Ranked by call-graph centrality over a tree-sitter symbol table |
| `read`         | Read                             | Returns an outline or the exact `:L10-L40` range, budgeted, instead of the full file                                                                                      |
| `edit`         | Edit, Write                      | Verified, cross-file edits in one call instead of per-file patch-or-create guessing                                                                                       |
| `bash`         | Bash                             | Output is capped and structured so a noisy build log can't blow the context window                                                                                        |
| `web_fetch`    | WebFetch                         | Strips a page to clean Markdown instead of a raw HTML dump                                                                                                                |

What's unchanged: Claude Code itself, the model, your workflow. Full internals: [Architecture](docs/architecture.md).

### Why a runtime, not just tools

A bare MCP server is a library the model can call _if_ it remembers to. A runtime decides what's callable at all — four jobs, four layers:

| Layer                           | Without it                                        | With it                                                                                                                        |
| ------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **Agents** — process isolation  | a "read-only" agent can still edit                | `explore`/`plan`/`research`/`review` hard-deny `edit`/`write` at the host-config level                                         |
| **Skills** — standard library   | multi-step procedures re-improvised every session | encoded once, invoked the same way every time                                                                                  |
| **Hooks** — interrupts          | wasteful re-reads; "done" without a check         | bad calls denied before they run; on Claude Code, session close blocked until verification ran (advisory on other hosts today) |
| **MCP tools** — syscall surface | agents fall back to grep-and-read under pressure  | natives hidden — LemonCrow tools are the only surface for those jobs                                                           |

### The engine underneath — not just for code

Under the coding runtime is a domain-neutral context engine with one contract: **one-shot retrieval** — a single call returns what the model needs, packed to a token budget; follow-up queries are allowed, retry loops are never required. Retrieval, compression, memory, and replay do not assume code — code is the first conforming retriever (`Retriever` protocol, `lemoncrow.core.capabilities.retrieval`), chosen because the pain is mature and the results are measurable. The same engine is being opened to teams building any LLM agent: [lemoncrow.com/engine](https://lemoncrow.com/engine).

### Agents

Packaged in [integrations/agents/](integrations/agents/) — each a distinct capability grant (subagent name `lemoncrow:<mode>`), not a persona:

| Agent      | Writes? | Use                                              |
| ---------- | :-----: | ------------------------------------------------ |
| `code`     |   Yes   | default interactive — edits, refactors, features |
| `auto`     |   Yes   | fully autonomous — CI/headless runs              |
| `solve`    |   Yes   | end-to-end solving of a well-defined task        |
| `execute`  |   Yes   | one verified pass of an accepted plan            |
| `general`  |   Yes   | catch-all for mixed work                         |
| `bare`     |   Yes   | minimal toolset, same discipline                 |
| `explore`  |   No    | read-only exploration — locate and cite          |
| `plan`     |   No    | read-only planning, stops for human checkpoint   |
| `review`   |   No    | adversarial read-only review                     |
| `research` |   No    | external web research — cited memo               |

### Skills

Packaged in [integrations/skills/](integrations/skills/):

| Skill         | What it does                                                 |
| ------------- | ------------------------------------------------------------ |
| `lc`          | manage LemonCrow itself via the CLI                          |
| `benchmark`   | measure savings on _your_ repo — offline scan or live A/B    |
| `orchestrate` | one structured multi-step task, routed to the right surface  |
| `swarm`       | N parallel attempts in isolated worktrees — best result wins |
| `perf-review` | gate a change on measured performance, not read code         |
| `ux-review`   | gate shipped UI on objective checks in a real browser        |
| `recall`      | retrieve what past sessions learned                          |

## Check your own savings

After LemonCrow is installed, inspect all savings and missed opportunities in
your local session history:

```bash
lemoncrow session stats
```

The report is read-only. It counts wasted tool calls and round-trips in your
Claude Code, Codex, and opencode sessions, then shows the associated token,
cost, and time opportunity.

### Replay a past session

`lemoncrow session replay` plays back a recorded session and, for each native call, **runs the real LemonCrow tool that would have replaced it** — grep/read loops collapse into one `code_search`, whole-file reads become budgeted outlines, `bash` logs get capped. It then estimates — from that session alone — the **cost, savings opportunity, and time** LemonCrow would have saved. No model re-run, nothing written; opens a shareable HTML page.

```bash
lemoncrow session replay
```

Works on Claude Code, Codex, and opencode sessions. The saving is an estimate; the live re-measured A/B is `lc benchmark local`.

## Results

These are fixed results from pinned benchmark runs—not a live counter. Every
headline number links back to committed raw runs and methodology in
[BENCHMARKS.md](BENCHMARKS.md). The model, tasks, containers, turn limits, and
verification harness were held constant. Community telemetry remains separate:
[see live savings](https://lemoncrow.com/savings).

| Benchmark                                            | Baseline correct | LemonCrow correct | Correct delta | Baseline cost | LemonCrow cost |         Cost delta |
| ---------------------------------------------------- | ---------------: | ----------------: | ------------: | ------------: | -------------: | -----------------: |
| SWE-bench Verified, 50 tasks x 5 reps                |            80.8% |         **92.8%** |  **+12.0 pp** |       $234.84 |    **$165.45** |  **29.5% cheaper** |
| SWE-bench Lite, 10 tasks x 5 reps                    |            98.0% |              96.0% |       -2.0 pp |        $19.83 |     **$17.51** |  **11.7% cheaper** |
| SWE-bench Pro, 10 tasks x 5 reps                     |            88.0% |         **90.0%** |   **+2.0 pp** |        $39.01 |     **$30.61** |  **21.5% cheaper** |
| Exploration tasks across 7 large repos x 5 reps      |                - |                 - |             - |        $19.11 |      **$6.29** |    **67% cheaper** |
| Telegraphic Q&A, 20 prompts x 5 reps                 |                - |                 - |             - |         $8.68 |      **$6.18** |  **28.8% cheaper**‡ |
| Terminal-Bench 2.1, 89 tasks vs public leaderboard\* |   78.9% expected |             78.7% |       -0.2 pp |        $96.76 |    **$69.52**† | **28.1% cheaper**† |

<sub>\* LemonCrow 1 rep/task vs public leaderboard 5-rep average. † 5 timed-out tasks excluded from cost. ‡ Caveman (free terse-persona system prompt, no install) also ran in the same invocation: $8.57, 1.2% cheaper than baseline -- LemonCrow's edge is durable cost, not just shorter replies; see [`/vs/caveman`](https://lemoncrow.com/vs/caveman).</sub>

<p align="center">
  <img src="benchmarks/cost_vs_savings_scatter.svg" alt="LemonCrow vs baseline: dollars saved per run against baseline task cost, across SWE-bench Verified/Lite/Pro, exploration, Telegraphic Q&A, and Terminal-Bench" width="720">
</p>

SWE-bench Verified detail (250 runs a side) — one-shot search collapses the grep-and-read loop, so turns, wall-clock, and tool calls drop together:

| Metric               | Baseline | LemonCrow |            Delta |
| -------------------- | -------: | --------: | ---------------: |
| Turns                |    6,962 |     4,336 |  **37.7% fewer** |
| Wall-clock           |    14.3h |     10.9h | **23.7% faster** |
| Total tool calls     |    6,700 |     4,167 |       **-37.8%** |
| Output tokens        |    3.04M |     2.19M |  **27.9% fewer** |
| Bash                 |    3,327 |     1,785 |       **-46.3%** |
| Read                 |    1,733 |     1,050 |       **-39.4%** |
| Edit + Write         |    1,628 |       759 |       **-53.4%** |
| Search (code_search) |        - |       568 |   lemoncrow-only |

Exploration detail (7 large repos × 5 reps, read-only Q&A, no edits):

| Tool                          | Baseline calls | LemonCrow calls |          Delta |
| ----------------------------- | -------------: | --------------: | -------------: |
| Read                          |            672 |              23 |     **-96.6%** |
| Bash                          |            508 |              71 |     **-86.0%** |
| Search (code_search)          |              - |              23 | lemoncrow-only |
| Agent + orchestration calls\* |             79 |               1 |     **-98.7%** |
| Total tool calls              |          1,259 |             118 |     **-90.6%** |
| input                         |        286,191 |         205,967 |     **-28.0%** |
| cache read                    |     35,862,919 |       2,753,393 |     **-92.3%** |
| cache write                   |      2,811,356 |         233,381 |     **-91.7%** |
| output                        |        426,367 |          68,893 |     **-83.8%** |
| input + cache write           |      3,097,547 |         439,348 |     **-85.8%** |

Source: [`exploration_2026_06_29`](benchmarks/codebench/results/exploration_2026_06_29/) · [`telegraphic_2026_07_16`](benchmarks/codebench/results/telegraphic_2026_07_16/).

## One-shot code search vs 10 named tools

The search is the engine: right code in front of the agent on the first try. MRR and first-hit rate (rec@1) across ~7,200 query/gold pairs on 14 repos — 10 tools scored on the identical corpus. LemonCrow semantic search returns the right code first on 65.0% of queries at 390ms p95; the lexical path returns it first on 58.2% at 134ms p95:

| Provider                      |       MRR |     rec@1 |    p95 |
| ----------------------------- | --------: | --------: | -----: |
| **LemonCrow +semantic (BGE)** | **0.727** | **0.650** |  390ms |
| LemonCrow lexical (default)   |     0.676 |     0.582 |  134ms |
| cocoindex-code (best rival)   |     0.557 |     0.457 |  595ms |
| serena                        |     0.401 |     0.359 | 3834ms |
| ripgrep                       |     0.376 |     0.320 |   66ms |
| universal-ctags (worst rival) |     0.237 |     0.226 |    1ms |

No one had scored these 10 tools against each other on a shared query set before -- each publishes its own number, on its own terms, against its own baseline.

## What you get

- Works with Claude Code, Codex, Copilot, Copilot CLI, and opencode today; Cursor, Hermes Agent, and Antigravity integrations are in progress. Any MCP-compatible agent (LangChain, the OpenAI SDK, Gemini ADK, ...) can connect to the same tools.
- Runs locally by default.
- Open-source runtime (Apache-2.0); the compiled engine (`lemoncrow.pro`) is proprietary and required at runtime.
- Anonymous evaluation is capped at $50 in measured savings; a free account unlocks uncapped core usage.
- Live local stats for cost, tokens, and savings; anonymous remote telemetry is on by default (opt out anytime).
- Free: grounded local tools, recall, handovers, verification, and swarm.
- Pro: larger-repo indexing, cross-vendor memory, compression, optimization, and reusable knowledge for $20/month or $200/year.

## Learn more

- [Installation](docs/installation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Benchmarks](BENCHMARKS.md) · [full results, backed by docs](docs/benchmarks/results.md) · [every "vs" comparison, with sources](https://lemoncrow.com/vs)
- [CLI reference](docs/cli.md)
- [Architecture](docs/architecture.md)

---

## Star History

<a href="https://www.star-history.com/?repos=lemoncrow-lab%2Flemoncrow&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=lemoncrow-lab/lemoncrow&type=date&theme=dark&legend=top-left&sealed_token=NdXY4u9dzzr-g_ahRPzkwBVWWX4kTV8uKk0z1K8bGEhBe8-O6CzxUvJNT3Gt-CnxpX5sLlgBnzjyCaSZp6tnTqtJg0dBKOi3QJ3QGd9JHBrUsu7ZxA5-mcoC5vS16ryI6UKqLwYMada9aB9aPVRK2AjDCdt-2c--Sg3Zybv5_NgZWwggYuuV0gZceADH" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=lemoncrow-lab/lemoncrow&type=date&legend=top-left&sealed_token=NdXY4u9dzzr-g_ahRPzkwBVWWX4kTV8uKk0z1K8bGEhBe8-O6CzxUvJNT3Gt-CnxpX5sLlgBnzjyCaSZp6tnTqtJg0dBKOi3QJ3QGd9JHBrUsu7ZxA5-mcoC5vS16ryI6UKqLwYMada9aB9aPVRK2AjDCdt-2c--Sg3Zybv5_NgZWwggYuuV0gZceADH" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=lemoncrow-lab/lemoncrow&type=date&legend=top-left&sealed_token=NdXY4u9dzzr-g_ahRPzkwBVWWX4kTV8uKk0z1K8bGEhBe8-O6CzxUvJNT3Gt-CnxpX5sLlgBnzjyCaSZp6tnTqtJg0dBKOi3QJ3QGd9JHBrUsu7ZxA5-mcoC5vS16ryI6UKqLwYMada9aB9aPVRK2AjDCdt-2c--Sg3Zybv5_NgZWwggYuuV0gZceADH" />
 </picture>
</a>

---

## License

**Open-core.** All source published in this repository is [Apache-2.0](LICENSE-APACHE) — free to read, use, modify, and redistribute.

The **engine** — the `lemoncrow.pro` package (the retrieval-quality and token-savings core) — is **proprietary**: it ships only as compiled binaries, its source is not published, and it is **required at runtime** (without it, LemonCrow falls back to a degraded built-in-only mode). See [LICENSE](LICENSE) for the exact terms.
