# lemoncrow.com — Landing Page Copy

_Audience: daily coding-agent power users on real repositories. Primary promise: context sharpness. Cost is supporting evidence._

## Nav

\`LemonCrow · Replay · Compare · Docs · Install\`

## Hero

**Badge:** Same model · Cleaner context

# Keep your coding agent sharp on real codebases.

**Proof line:** +12.0pp resolved · 37.7% fewer turns — same model.

LemonCrow gives your existing agent a local code graph, exact-range tools, bounded output, and durable memory. It replaces grep dumps and repeated file reads with the smallest useful working set, so long sessions spend more context solving.

**Primary CTA:** Try it on a hard repo

**Secondary CTA:** See matched proof

\`\`\`bash
curl -fsSL https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download/install.sh | bash
\`\`\`

Hero side panel:

- Find — ranked symbols, callers, and exact ranges.
- Read — only the source needed for the next decision.
- Carry — task state and useful memory across long sessions.
- 92.8% resolved; 37.7% fewer turns; 23.7% faster.

## Context sharpness

# Context gets noisy before it gets full.

More context is not automatically better context. Search results, file reads, tool schemas, and discarded approaches share the same working window.

Three failure modes:

1. Search noise — the investigation becomes baggage.
2. Working-state drift — yesterday's path competes with today's task.
3. Lossy handoffs — compaction keeps what it predicts the next step needs.

Close: LemonCrow treats context as a working set—ranked code first, exact ranges on demand, bounded output, duplicate suppression, and persistent memory.

## How it works

# Install → map → stay sharp.

1. Install the MCP server, agents, skills, and hooks.
2. Build and incrementally refresh the local symbol/call graph.
3. Use \`code_search\`, exact reads, bounded output, and durable state instead of a grep/read loop.

Show one call-graph visual. No second terminal demo.

## Why a runtime

# The runtime controls what enters the conversation.

- Map: find the right code first.
- Bound: keep output proportional.
- Carry: preserve what the task still needs.

Show the compact tool mapping. Move the four-layer architecture table to docs.

## Matched proof

# Cleaner context should finish more work—not just process fewer tokens.

Flagship SWE-bench Verified:

- 92.8% vs 80.8% resolved.
- +12.0 percentage points.
- 37.7% fewer turns.
- 23.7% faster.
- 29.5% lower cost as a secondary note.

Link every result to \`BENCHMARKS.md\`. Keep the Terminal-Bench result visible in the suite strip — now +1.1pp accuracy (80.0% vs 78.9%) on matched 5-rep runs.

## Replay

# See the wandering before you install.

Replay one recorded session without rerunning a model. Show repeated searches, oversized reads, and avoidable calls. Keep the screenshot and local command.

## Local-first

# Improve the agent without shipping your repo to another index.

Say exactly:

- Parsing and indexing run locally.
- Model calls still go to the provider already configured in the host.
- Optional anonymous telemetry excludes source and prompts and can be disabled.
- All of LemonCrow, including the engine, is Apache-2.0 and ships as readable source.

Do not claim every competitor requires uploading code.

## Developer to team

# Do not build another knowledge warehouse. Make existing knowledge executable.

Jira, Confluence, GitHub, and the repository remain sources of truth. LemonCrow selects what a change needs, preserves it during the run, and returns verified learnings for review.

Cards:

- LIVE — one developer, every host.
- DIRECTION — source-linked engineering memory.
- TEAM — shared, governed working set.

## Final CTA

# Try it where your agent starts to wander.

Keep the model, editor, and workflow already chosen. Add the runtime and judge it on a hard repository.

**Install on a hard repo** · **Inspect on GitHub**

## Removed from homepage

- Live savings hero panel → \`/savings\`
- Terminal demo → docs or product walkthrough
- Before/after prose demo → \`/vs\`
- Retrieval comparison and full MRR table → \`/vs\`
- Savings estimator → \`/savings\`
- Standalone honesty section → folded into matched proof
- Newsletter section → footer
- Rotating unshipped \`docs / knowledge\` hero terms → removed

Nothing is deleted from the product or its dedicated page; the homepage no longer asks those sections to compete with the core promise.
