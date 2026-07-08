# atelier.ws — Landing Page Copy

_Copy blocks for the marketing site, top to bottom. Voice: witty but serious (see `strategy.md`). Every number here must link to raw runs — if it's not reproducible, it doesn't ship. Placeholder blocks are marked `[[PLACEHOLDER]]` — do not publish faked proof._

---

## Nav

`Atelier`  ·  Proof  ·  Why  ·  Docs  ·  Pricing  ·  [GitHub ⭐ <live stars>]  ·  **[Install]**

---

## Hero

### Same model. Sharper agent.

**Atelier is the runtime that turns a coding agent into a craftsman.**
It reads the right line instead of the whole file, routes work through the right shape, keeps the transcript clean, and never pays twice for context it already found.

> **+12.0pp resolved · 29.5% cheaper** on SWE-bench Verified — same model, same tasks, same environment. Every raw run published.

```bash
curl -fsSL https://install.atelier.ws | bash
```

**[Install]**  ·  **[Estimate your savings →]**  ·  Apache-2.0 · local-first · no account to start

_Subhead, small:_ Everyone's teaching agents to talk less. That's the easy 5%. Atelier compresses the other 95% — what the agent reads, calls, and re-reads.

---

## Live proof strip (real, wire to the badge API)

Three live counters, updated on every session end across all Atelier installs:

`💰 $<savings> saved`   `🧮 <tokens> tokens avoided`   `⚡ <calls> calls routed`

_Caption:_ Live and aggregate. Same honesty we hold our own metering to — [see the method](docs/architecture.md).

---

## The demo (the money shot)

**Left panel — the loop, animated:** a coding agent asks "why is the retry test flaky?" → baseline greps 6 files, dumps 3 whole files, narrates for 71 tokens. → Atelier: one `code_search`, one ranged `read`, answers in 38.

**Right panel — the meter:**
```
cost           ███████████████░░░░░   −29.5%
turns          ██████████████░░░░░░   −37.7%
output tokens  ███████████████░░░░░   −27.9%
resolved       ████████████████░░░░   +12.0 pp
receipts       ████████████████████   PUBLISHED ✔
```
_Caption:_ Not a mockup number. Reproduce it: `benchmarks/codebench/results/swe50_2026_06_30/`.

---

## Before / After

**Same fix. A third of the words. Nothing technical lost.**

| Baseline — 71 tokens | Atelier — 38 tokens |
| --- | --- |
| "I looked into the failing test and it seems like the flakiness is caused by the retry logic using a real clock… I'd recommend injecting a fake clock so the test becomes deterministic." | "Root cause: retry test uses a real clock — 100ms sleep + exact 3-retry assert drifts under CI load. Fix: inject a fake clock; test becomes deterministic." |

_Caption:_ Atelier shrinks the loop, not the brain. Code, commands, and errors stay byte-for-byte exact.

---

## Why (the four verbs)

**Read smarter.** `code_search` replaces grep loops — relevant symbols, source, callers, callees, and blast radius in one call. `read` returns exact ranges, not whole files.

**Think sharper.** Narrow modes route work through the right shape — explore, plan, execute, solve — instead of one vague do-everything agent.

**Talk less.** Compact tool results, batched edits, telegraphic personas. The transcript holds decisions, not narration.

**Never forget.** Recall, session memory, and deduped reads stop the agent from paying twice for context it already found.

_The claim, in one line:_ less noise gives agents more room to land correct patches.

---

## Estimate your savings (interactive CTA — the conversion driver)

### Don't take our benchmark's word for it. Point it at your own history.

```bash
curl -fsSL https://savings.atelier.ws | bash
```

Scans your local coding-agent sessions and prints what routable calls would have cost less. **Read-only. Temporary store. No login, no API keys.** ~60 seconds.

**[Copy command]**  ·  [What it does →](docs/cli.md)

---

## Honesty block (trust > hype)

**We publish the benchmark where we don't win.**

Terminal-Bench 2.1: Atelier is flat on accuracy (−0.2pp) and wins only on cost (28.1% cheaper). It's in the table, not the footnotes. Every headline number links to committed raw runs — per-task outcomes, costs, turn counts, reproduction commands. No cherry-picking. The credibility is the product.

**[Read the raw runs →](https://github.com/atelier-ws/atelier/tree/main/benchmarks)**

---

## Pricing (open-core)

**Free — $0.** The full local runtime. Grounded tools, host packaging, agents, skills, the context engine, and the headline savings number. No account, no key, no network call.

**Pro — $19/mo.** The leverage: Zoekt fast search + large-repo indexing, cross-session recall + cross-vendor memory, the savings engine, model routing, and multi-worktree swarm.

**Enterprise — contact us.** Very large repos, shared team context, governance, SSO, audit.

_Line:_ Free is genuinely useful — the moat is the maintainer and a server-anchored billing backend, not DRM on your local code.

**[See full plan matrix →](docs/pricing.md)**

---

## Social proof [[PLACEHOLDER — fill only as earned, never fake]]

`[GitHub stars, live]`  `[[Show HN thread — add when posted]]`  `[[Trending / Trendshift — add when ranked]]`  `[[press / creator mentions — add when real]]`

_Until these are real, run only the live badges + star count. Empty is honest; faked is fatal._

---

## Final CTA

### Give your agent a workshop.

```bash
curl -fsSL https://install.atelier.ws | bash
```

**[Install]**  ·  **[Star on GitHub ⭐]**  ·  **[Book a call]**

_Footer line:_ In our SWE-bench run, Atelier cut 2,626 turns and ~$69 off the baseline. A star costs zero tokens. Fair trade.
