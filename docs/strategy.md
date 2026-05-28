# Atelier Strategy

> Status: living doc. Last revised 2026-05-28.

## One-line positioning

**The honest, vendor-neutral layer for AI-assisted coding — your memory, your costs, your audit trail, across Claude, Codex, and Gemini.**

Atelier is not a smarter AI. Developers have already picked their AI. Atelier makes sure they aren't getting fleeced, aren't losing context between vendors and machines, and can see what's actually happening with their sessions.

## The market reality (May 2026)

The native CLIs all shipped persistent memory in early-to-mid 2026:

- **Claude Code**: Auto memory (v2.1.59+, on by default, 200-line cap), Session Memory, Claude Dreaming (April 2026), enterprise Memory in Agents
- **Codex (OpenAI)**: `~/.codex/memories/` markdown files, 6-hour background consolidation, 30-day pruning, **no cross-machine sync**
- **Gemini CLI**: Hierarchical `GEMINI.md`, `SaveMemory` tool, Auto Memory Inbox, Context Compression Service

**"Persistent memory" is no longer a moat.** Building a better single-vendor memory than Anthropic's Dreaming (Harvey reports 6× task-completion improvement) is a losing fight.

## Where the gaps remain

The natives all shipped vendor-locked memory. The pain points they cannot or will not solve:

1. **Cross-vendor memory.** Claude's memory feeds only Claude. Gemini's feeds only Gemini. A developer using all three has three disconnected knowledge bases.
2. **Cross-machine sync.** Codex specifically can't (local-only). Claude's auto-memory is per-project filesystem state.
3. **Auditability.** Anthropic's Dreaming is a black box. Users can't see what was learned, why, or roll it back.
4. **Honest cross-vendor cost comparison.** No native will tell users "Gemini would have been cheaper here."
5. **Federated outcome learning across users.** Single-vendor optimisations only; no shared knowledge.

These are **structural** gaps. Anthropic, OpenAI, and Google will not ship cross-vendor anything because each would undermine their own pricing.

## Three pillars — what we deliver to developers

### Pillar 1 — Honest cost & quality dashboard
The thing every developer paying $100-500/mo for AI tools wants: a real-time, brutally honest view of where the money goes.

- Per-session cost breakdown by tool, model, vendor
- Counterfactual: "this session cost $4.20 on Claude. With Gemini for read turns + Claude for edits: $1.80."
- Weekly summary in terminal and web

### Pillar 2 — Cross-vendor memory router
Atelier becomes the layer above the native single-vendor memories.

- Read Claude `Session Memory`, Codex `~/.codex/memories/`, Gemini `GEMINI.md`
- Surface all three in one inspectable view
- Edit, rollback, attribution ("this fact came from session X on machine Y")
- Sync across machines (encrypted, opt-in, self-hostable)

### Pillar 3 — Outcome telemetry that learns
Every decision (route, compact) gets an observable outcome score 5–10 turns later. Multipliers self-tune. With opt-in federated signal, routing improves across users.

Not a feature you sell — the engine that makes pillars 1 and 2 keep getting better.

## Defensible moats — in order of strength

1. **Cross-vendor routing & memory.** Structural. Natives cannot ship without partnering with competitors.
2. **Cross-machine sync.** Codex explicitly lacks this. Even if Anthropic ships it later, it's Claude-only.
3. **Auditable / inspectable memory.** Atelier's ledger is already inspectable. Lean into it.
4. **Federated outcome learning.** Opt-in cross-user signal. Single vendors can't aggregate across competitors' usage.
5. **Honest published benchmarks.** Atelier can publish "Claude is bad at X" — natives can't.

## What we explicitly do not do

- Beat native compact on compact alone. Same axis, smaller team.
- Build a "better than Anthropic memory" algorithm. Dreaming is genuinely good.
- IDE plugin before CLI is sticky. Heavy users live in the CLI.
- Custom models, fine-tuning, our own embeddings. We don't have the data yet.
- Enterprise sales motion before Team tier is repeating.

## Strategic timing

The window is open **now** — probably 6 to 12 months before one native partners with another or builds a vendor-bridge. Sprint while the gap exists. Use the lead to build federated learning, which is the harder-to-replicate moat.
