# LemonCrow — Marketing & Positioning Playbook

_Source of truth for public positioning. Revised 2026-07-16._

## 1. Position

> **LemonCrow keeps coding agents sharp on real codebases: the right code, less tool noise, and useful state that survives long work.**

Five seconds: **Same model. Cleaner context.**

Twelve seconds: **LemonCrow gives Claude Code, Codex, and opencode one local code graph, exact-range tools, bounded output, durable memory, and a verified runtime.**

Internal category: **host-neutral context and execution runtime for coding agents**.

Do not lead with “context layer.” It is broad, crowded, and says nothing about the developer's immediate problem. Do not lead with cost. Power users buy a sharper run; lower cost is evidence that less waste occurred.

## 2. Audience

Primary reader: an AI-native senior/staff engineer or technical lead using coding agents daily on a mature codebase.

Champion: the same developer standardizing agent workflows for a team.

Economic buyer: Developer Productivity, Platform Engineering, or an engineering leader paying for shared policy, memory, evaluation, and governance.

Write the main landing page for the primary reader.

## 3. Pain

Recognizable scene:

> A long agent session accumulates broad searches, whole-file reads, logs, stale approaches, and decisions that may not survive compaction. The model is capable; its working set is noisy.

Safe claims:

- Context quality degrades as irrelevant material accumulates.
- Broad repository exploration can dominate an agent's initial work.
- Compaction is useful and lossy.
- Million-token windows provide headroom, not immunity from context rot.

Do not claim:

- every agent spends 60% of total runtime searching;
- the agent degrades after a fixed number of hours;
- manually compacting at 60% is official vendor guidance;
- 44.9% fewer cumulative tokens means the live context window fills 44.9% more slowly;
- the paused June 15 Agent SDK credit change took effect.

## 4. Product hierarchy

1. **Outcome:** stay sharp and finish more work.
2. **Mechanism:** ranked code graph, exact ranges, bounded output, durable state.
3. **Runtime:** tools, agents, skills, hooks, and verification—not one optional MCP call.
4. **Proof:** matched success, turns, and wall-clock.
5. **Economics:** cost reduction as a measured consequence.
6. **Team direction:** reviewed, source-linked engineering memory with provenance, staleness, permissions, and outcome evaluation.

## 5. Proof stack

1. SWE-bench Verified: **92.8% vs 80.8% resolved (+12.0pp)**, 50 tasks × 5 reps, same model and matched environment.
2. **37.7% fewer turns** and **23.7% less wall-clock**.
3. Retrieval evaluation: **0.727 semantic MRR**, with every named provider scored on the same corpus.
4. Cost: **29.5% lower** in the flagship run—supporting evidence, not the headline.
5. Terminal-Bench: +1.1pp accuracy on a matched 5-rep run (80.0% vs 78.9%), 91.8% fewer fresh input tokens; publish it.
6. Raw runs and reproduction commands linked wherever a number appears.

## 6. Homepage arc

1. Keep your coding agent sharp.
2. Context gets noisy before it gets full.
3. Install → map → stay sharp.
4. Runtime controls what enters the conversation.
5. Matched proof.
6. One replay.
7. Local-first trust.
8. Developer-to-team direction.
9. Install CTA.

Comparison tables belong on \`/vs\`. Cost calculators belong on \`/savings\`. Deep architecture belongs in docs.

## 7. Voice

Concrete nouns: symbols, callers, line ranges, logs, decisions, tests.

Preferred lines:

- “Same model. Cleaner context.”
- “Keep your coding agent sharp on real codebases.”
- “A ranked answer, not a search transcript.”
- “Cleaner context should finish more work—not just process fewer tokens.”
- “Try it where your agent starts to wander.”
- “Every number reproducible. Every run published.”

Avoid generic words without mechanism: intelligent, AI-powered, seamless, context layer, organizational brain.

## 8. Team direction

Do not build or market a replacement for Jira, Confluence, GitHub, Glean, or enterprise search. Those systems remain sources of truth.

LemonCrow owns the last mile:

\`code + ticket + docs → task working set → agent run → tests/review → source-linked memory\`

Team memory must be reviewable, permission-aware, provenance-rich, and staleable. An unreviewed transcript is not organizational knowledge.

## 9. Guardrails

- Every headline number links to a raw run.
- Benchmark results are not universal guarantees.
- Cumulative token throughput is not context occupancy.
- Roadmap capabilities are labeled.
- All of LemonCrow, including the engine, is Apache-2.0 and ships as readable source.
- Local-first means parsing/indexing stay local; model calls still go to the user's configured provider.
