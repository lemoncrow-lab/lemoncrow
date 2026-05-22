# Learnings Flow

This document describes how Atelier agents extract and preserve institutional
knowledge from completed tasks. The goal: decisions, lessons, patterns, and
surprises discovered during execution should not be lost between sessions.

The storage plumbing already exists. This document is the protocol for using it.

---

## Four categories

Every extracted learning belongs to exactly one category:

| Category | What it captures |
| --- | --- |
| **Decision** | A technical or architectural choice made during the task, with rationale |
| **Lesson** | Something that turned out to be unexpectedly true — complexity, coupling, friction |
| **Pattern** | A reusable approach or technique that worked and should be repeated |
| **Surprise** | An outcome that differed from the plan: time, behavior, dependencies |

Each item requires **source attribution** — a reference to the trace ID, plan
path, or file from which the learning was drawn. Do not fabricate learnings;
extract only what is explicitly evidenced.

---

## When to run

After any task that:
- required a `rescue` call (the failure signal implies a lesson)
- produced a decision that will affect future work (should flow to `docs/decisions/`)
- discovered a pattern worth reusing across tasks
- surprised you — took longer, broke an assumption, or behaved differently than expected

---

## How to run

### Step 1 — Collect sources

Read the artifacts that document what happened:
- The task description (or plan file under `docs/plans/active/<domain>/`)
- The most recent trace (use `memory op="recall"` with `tags=["learning"]` to
  check for prior extractions for the same task)
- Any rescue traces (these are the highest-signal source for lessons)
- Related ADRs or decisions in `docs/decisions/`

### Step 2 — Extract

For each source, pull out items in the four categories. Rules:

- **Do not fabricate.** Extract only what is explicitly documented or
  observable in the artifacts.
- **Source attribution is required.** Every item must include a `Source:` line
  referencing the trace ID or file it came from.
- Running this twice on the same task must produce a replacement, not an append.

### Step 3 — Record

Use `trace` with the `learnings` parameter. The runtime deduplicates by
`agent:text` hash across sessions, so repeat runs are safe:

```
trace(
  agent="atelier:learn",
  domain="learnings",
  task="<original task description>",
  status="success",
  learnings=[
    "Decision: chose X over Y because Z. Source: trace-abc123",
    "Lesson: module M has an undocumented coupling to N. Source: docs/plans/active/foo/PLAN.md",
    "Pattern: always seed DB rubrics before sync_knowledge to allow user overrides. Source: trace-def456",
    "Surprise: sync took 3× longer than estimated due to mtime manifest cold start. Source: trace-def456",
  ],
  capture_sources=["<trace_id_or_plan_path>"],
)
```

Each string in `learnings` is stored as an archival memory passage tagged
`["learning", domain]` and deduped by hash. They are retrievable via:

```
memory(op="recall", query="<relevant topic>", tags=["learning"])
```

### Step 4 — Promote decisions to ADRs (if warranted)

If a Decision learning is durable — it affects future architectural choices —
create a short ADR file in `docs/decisions/`:

```
docs/decisions/<YYYY-MM-DD>-<slug>.md
```

Minimum ADR shape:
```markdown
# <Decision Title>

**Date:** YYYY-MM-DD
**Status:** accepted

## Context
<What situation prompted this decision>

## Decision
<What was decided and why>

## Consequences
<What changes as a result>
```

---

## What not to do

- Do not record learnings as prose in chat — they will be lost between sessions.
- Do not append to existing learning files — overwrite (idempotent by design).
- Do not create an ADR for every lesson — only for durable architectural choices.
- Do not skip source attribution — it makes learnings unverifiable.
