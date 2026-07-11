# lemoncrow.ws — Landing Page Copy

_Copy blocks for the marketing site, top to bottom. Audience: people paying for Claude Code/Max who are not deep infrastructure buyers. Voice: plain, specific, and skeptical. Every number must link to raw runs or a live endpoint. Live badges support trust; they do not prove the headline by themselves._

---

## Nav

`LemonCrow` · How it saves · Proof · Pricing · Docs · [GitHub ⭐ `<live stars>`] · **[Install]**

---

## Hero

### Honest and benchmark proven -- cut Claude Code costs by 30%, audited head-to-head (up to 67% on some workloads).

LemonCrow is a 30-second install that helps Claude Code waste fewer tokens while you work. It cuts tool calls by up to 90% and input/output tokens by up to 80% -- and gives your agent better search, shorter file reads, cleaner tool output, and reusable memory so more of your subscription goes into fixing code instead of rereading the same noise.

> In our same-model SWE-bench Verified run, LemonCrow was **29.5% cheaper** and solved **+12.0 percentage points more tasks** than the baseline. Same model. Same tasks. Same environment. Raw runs published.

```bash
**[Install in 30 seconds]** · **[Check my savings first →]** · source-available · runs locally · free account to activate

_Small line:_ Keep using Claude Code. LemonCrow sits underneath it and makes the tool loop tighter.
**[Install in 30 seconds]** · **[Check my savings first →]** · Apache-2.0 · runs locally · no account to start

_Small line:_ Keep using Claude Code. LemonCrow sits underneath it and makes the tool loop tighter.

---

## Trust strip

Three proof types, shown close to the headline:

1. **Your own history:** run the read-only savings scan before installing.
2. **Raw benchmark receipts:** every headline number links to committed per-task runs.
3. **Live badges:** aggregate savings, tokens avoided, and routed calls update after real sessions.

Live badge copy:

`$<savings> saved by users` · `<tokens> tokens avoided` · `<calls> agent calls routed`

_Caption:_ Live badges are not the benchmark. They show real usage adding up. The 30% claim comes from reproducible runs and can be checked against your own local history.

---

## The install promise

### Install once. Keep working normally.

```bash
curl -fsSL https://install.lemoncrow.ws | bash
    curl -fsSL https://install.lemoncrow.ws | bash
    cd your-project
    lemon login
    lemon init

Create a free account to activate the official install. Then open Claude Code like usual. LemonCrow adds better tools behind the scenes: smarter code search, exact file reads, compact command output, safer edits, and a running local savings meter. Anonymous remote telemetry is on by default (opt out anytime).
---

## Live savings demo

**Left panel:** Claude asks for context. Baseline dumps broad files and long command output. LemonCrow gives the agent only the useful ranges, relevant symbols, and compact results.

**Right panel:** savings meter accumulates while work continues.

```text
cost           −29.5%  benchmark receipt
turns          −37.7%  fewer back-and-forth loops
output tokens  −27.9%  less paid narration
resolved       +12.0pp more tasks solved
receipts       raw runs published
```

_Caption:_ No fake counters. If a number is live, label it live. If a number is benchmarked, link the raw run. If it is estimated from the visitor's machine, say estimated.

---

## Why it saves

**It reads less.** Claude gets the exact lines and symbols it needs, not whole files pasted into the chat.

**It repeats less.** LemonCrow remembers useful session context so the agent does not pay again for the same discovery.

**It talks less.** Outputs are shorter and more direct while preserving exact code, commands, and errors.

**It makes agents more correct.** In our same-model benchmark, the tighter loop solved more tasks because the agent spent less context on noise and more on the fix.

_The simple claim:_ LemonCrow does not make Claude a different model. It makes the work around Claude less wasteful.

---

## Before / After

**Same fix. Fewer paid words. Same technical meaning.**

| Baseline | With LemonCrow |
| --- | --- |
| "I looked into the failing test and it seems like the flakiness is caused by the retry logic using a real clock… I'd recommend injecting a fake clock so the test becomes deterministic." | "Root cause: retry test uses a real clock. Fix: inject a fake clock; test becomes deterministic." |

_Caption:_ Shorter does not mean vague. Code, commands, filenames, and errors stay exact.

---

## Check your own savings

### Do not take our 30% claim on faith.

```bash
curl -fsSL https://savings.lemoncrow.ws | bash
```

This scans your local Claude/Codex agent history and prints what LemonCrow could have saved. It is read-only, temporary, and does not need an LemonCrow account or provider API keys.

**[Copy command]** · [What it reads →](../cli.md)

---

## Honesty block

### How do you know the numbers are real?

Bad answer: a big live badge with no method.

Better answer:

- Every headline benchmark links to raw per-task runs, costs, turn counts, and reproduction commands.
- The savings scanner runs on your own machine, against your own agent history.
- Live badges are labeled as aggregate usage, not proof of the 30% benchmark.
- We publish rows where LemonCrow does not win. Terminal-Bench 2.1 is flat on accuracy (−0.2pp) and only cheaper on cost.

The trust is the audit trail, not the animation.

**[Read the raw runs →](https://github.com/lemoncrowhq/lemoncrow/tree/main/benchmarks)**

---

## Pricing
## Pricing

**Free — $0.** Create a free account, activate the official local install, and get better search/read/edit/bash tools, agent skills, and local savings estimates.

**Pro beta — $5/mo or $49/yr.** Existing gated capabilities for heavy users: large-repo search, session recall, savings optimization, model routing, and multi-worktree swarm.

**[See the full plan matrix →](../pricing.md)**
---

## Social proof [[PLACEHOLDER — fill only as earned, never fake]]

`[GitHub stars, live]` · `[[Show HN thread — add when posted]]` · `[[Trending / Trendshift — add when ranked]]` · `[[press / creator mentions — add when real]]`

_Until these are real, run only live badges, raw-run links, and the visitor's own savings scan._

---

## Final CTA

### Start saving Claude tokens in the next session.

```bash
curl -fsSL https://install.lemoncrow.ws | bash
```

**[Install in 30 seconds]** · **[Check savings first]** · **[Star on GitHub ⭐]**

_Footer line:_ In our SWE-bench Verified run, LemonCrow cut 2,626 turns and about $69 from the baseline. Same model, same tasks, same environment.
