# Cost tracking: which tool tells the truth?

> A plain-English guide to comparing Atelier's spend numbers against the other
> popular AI cost trackers. If your dashboards disagree, this page tells you
> who's right and why.

There are several open-source tools that try to answer the question
*"how much did my AI coding assistants cost me today?"*. Atelier is one of
them; CodeBurn, Tokscale, and ccusage are the most-used alternatives. They
read mostly the same files on disk, but they make **different accounting
decisions** — so totals routinely disagree by 15–40%.

This page is for you if you've ever opened two dashboards, seen
`$77` in one and `$109` in the other, and wondered which to trust.

---

## TL;DR

| Tool       | What it's best at                                        | Run it with                              |
|------------|----------------------------------------------------------|------------------------------------------|
| **Atelier**  | Most accurate dollar cost; replay any session for debugging | already running                          |
| **CodeBurn** | "Where does my spend cluster?" (project / activity)      | `codeburn today`                         |
| **Tokscale** | All-time client × model rollups, contribution graph      | `tokscale models --today`                |
| **ccusage**  | Most popular Claude-only checker; great cross-check on dedup | `npx ccusage daily`                  |

**The short version**: Atelier is the most accurate on **how many real
dollars left your wallet**. The others count things slightly differently in
ways that inflate their headline number — sometimes correctly, sometimes
not. None of them are wrong; they answer slightly different questions.

---

## You can see all four in Atelier's UI

Atelier ships an **External Analyzers** page (in the sidebar) that runs
CodeBurn, Tokscale, and ccusage as sidecars and renders their reports next
to Atelier's own numbers. Today's snapshot looks like this:

| Tool      | Today's spend | Disagrees by | Note                                              |
|-----------|---------------:|-------------:|--------------------------------------------------|
| Atelier   | **$81.38**     | baseline     | Provider-accurate, dedup'd                       |
| ccusage   | $36.20 (claude only) | +$22.09 vs Atelier claude row | Doesn't dedup chunked Anthropic messages |
| Tokscale  | $96.78         | +$15.40      | Counts per-event; broader coverage of all hosts  |
| CodeBurn  | $109.34        | +$27.96      | Counts per-event + Copilot subscription proxy    |

Open the **External** page to see live numbers. Each tool's tab walks you
through the per-model breakdown so you can see exactly where the gap is.

---

## Why the totals disagree (the short version)

There are really only **four** reasons cost trackers disagree. Once you
internalise them, every discrepancy you'll see falls into one of these
buckets:

### 1. Counting the same response 2–3 times (Anthropic dedup)

Claude Code writes one assistant response as **multiple JSONL events** —
one event per content block. They all carry the **identical** `usage` block.

- **Atelier** dedups by `message.id` (correct: those events are the same
  response, not three separate ones).
- **ccusage and codeburn** appear to sum every event (incorrect: bills the
  same tokens 2–3 times).

You'll see this as: `ccusage` showing $34 for Opus while Atelier shows $14
for the *same* Opus calls.

### 2. Counting cached input twice (OpenAI / Gemini)

OpenAI and Gemini both report `input_tokens` **including** the cached
portion. To bill correctly you have to subtract `cached_tokens` from
`input_tokens` (the cached portion gets billed separately, at a much lower
rate). Atelier does this. Some other tools don't, which inflates their
input numbers.

### 3. GitHub Copilot is a flat subscription

Copilot Chat doesn't bill per token — it's a flat $19/month subscription.
Atelier respects that and prices Copilot calls at $0 (but still records
the call volume). CodeBurn estimates a per-call rate (~$0.0033/call). Both
are defensible; just know which you're looking at.

### 4. Some tools see more data than others

- Atelier captures Gemini **subagent** chats and VSCode Copilot Chat
  **debug-log** telemetry. CodeBurn and Tokscale don't.
- CodeBurn captures **inline Copilot completions** and a **GPT-5.3 Codex
  routing classification** that Atelier doesn't.
- Tokscale has the cleanest **all-time** rollups (Atelier keeps fewer day-
  level traces).

The deeper engineering writeup —
[Cost tracking: Atelier vs CodeBurn vs Tokscale](engineering/cost-tracking-comparison.md) —
walks through every fix Atelier has made and every test you can run to
confirm an accounting nuance.

---

## How to install the tools to compare side-by-side

Each tool is one shell command. Atelier will detect them on `PATH`
automatically.

```bash
# CodeBurn — best for "where does spend cluster"
npm i -g codeburn

# Tokscale — best for client × model rollups and an annual contribution graph
npm i -g tokscale

# ccusage — most popular Claude Code cost tracker, useful as a dedup cross-check
npm i -g ccusage
```

Then open Atelier's **External Analyzers** page (in the left sidebar) and
you'll see one tab per detected tool. If a tool isn't on `PATH`, set an env
var instead: `ATELIER_CODEBURN_BIN`, `ATELIER_TOKSCALE_BIN`,
`ATELIER_CCUSAGE_BIN`.

If you'd rather run them by hand, here are the commands that produce the
same data Atelier's UI displays:

```bash
codeburn today --format json
tokscale models --today --json
npx ccusage daily --json --since 20260514 --until 20260514
```

---

## Which number should I trust?

It depends on what you're asking:

| Question                                              | Trust       |
|-------------------------------------------------------|-------------|
| "Did my LLM bill really cost $X today?"               | **Atelier** |
| "Where is my spend concentrating (project / activity)?" | **CodeBurn** |
| "How active was I on each client (claude / codex / gemini)?" | **Tokscale** |
| "Does my Claude usage match what most Claude users see?" | **ccusage**  |
| "What's the actual dollar amount my credit card got charged?" | The **provider's own dashboard** — `console.anthropic.com`, `platform.openai.com`, `aistudio.google.com`. |

If two tools disagree wildly, **check the provider dashboard** — that's
the ground truth. In our auditing, Atelier almost always matched the
provider; CodeBurn and ccusage tended to be 1.5–3× higher because of the
double-counting issue described above.

---

## What's missing right now

Atelier doesn't yet capture two niche sources that show up in other
trackers:

1. **GitHub Copilot inline completions** (not chat — the autocomplete you
   get as you type). CodeBurn captures these. Small dollar impact (~$2/day
   for a heavy user); not yet supported in Atelier.
2. **GPT-5.3 Codex routing decisions** that CodeBurn classifies from chat
   metadata. This appears to be a derived label rather than a real
   `model=` field on disk, so Atelier doesn't fabricate it.

Sub-$1/day each on a typical workload. The combined impact on a heavy
user is ~$2–4/day, which is roughly the entire gap between Atelier and
CodeBurn after dedup adjustments.

---

## More tools you can compare against

These are not yet wired into Atelier's UI but **report the same kind of
data** so you can use them as additional cross-checks:

| Tool | Install | What it tells you |
|---|---|---|
| **claude-monitor** | `pipx install claude-monitor` | Real-time TUI showing burn-rate and Claude plan quota. No JSON; eyeball-only. |
| **cursor-stats** | `npm i -g cursor-stats` | Decodes Cursor's `state.vscdb` token counts. Useful if Atelier's Cursor row is showing $0. |
| **Helicone** (self-hosted) | `docker run …helicone…` | Proxies your API calls and logs **exactly what the provider billed**. Ground truth. |
| **LiteLLM proxy** | `pip install 'litellm[proxy]'` | Same architecture as Helicone; ships the pricing catalog Atelier uses. |
| **Langfuse** | self-host or cloud | LLM-observability platform; same data model as Atelier. |
| **Anthropic / OpenAI / Google dashboards** | open in browser | The provider's own usage page. Final word on what you actually owe. |

If you want any of these to show up as tabs in Atelier's External page
the way CodeBurn / Tokscale / ccusage do, the integration is a small lift
— see
[`src/atelier/gateway/integrations/external_analytics.py`](../src/atelier/gateway/integrations/external_analytics.py)
for the pattern (add an `ExternalAnalyzerSpec`, a runner, and a dispatch
branch).

---

## Bottom line

If you're an end user who just wants to know *"what is the right number?"*:

> **Trust Atelier for the dollar amount.** Cross-check ccusage when you
> want a second opinion on Claude specifically. Use CodeBurn for spend-
> clustering insights and Tokscale for client × model rollups. The
> provider's own dashboard is the final tiebreaker.

For the full engineering story — including the actual JSONL examples,
the dedup proof, and the per-file source comparisons — see
[**Cost tracking: Atelier vs CodeBurn vs Tokscale**](engineering/cost-tracking-comparison.md).
