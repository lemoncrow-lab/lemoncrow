# ADR-001: V2 Stateful Memory and Context Savings

## Status

Accepted.

The project owner has approved the V2 direction. This ADR records the architectural contract for
the work packets in [Atelier V2 - Implementation Plan](../../../architecture/IMPLEMENTATION_PLAN_V2.md)
and the companion [V2 data model](../../../architecture/IMPLEMENTATION_PLAN_V2_DATA_MODEL.md).

## Context

Atelier V1 is a reasoning runtime for coding and operational agents. It already stores
ReasonBlocks, rubrics, traces, environments, failure clusters, and run ledgers. That is enough to
prevent known dead ends, run plan checks, and extract reusable procedures from observable work.

The gap is continuity. ReasonBlocks describe what to do, while traces describe what happened. They
do not provide editable long-term project facts, archival recall, or a measured context-budget
story for premium single-model workflows. Agents can still waste tokens rereading the same files,
reconstructing stale task context, or retrying failures that should have been promoted into
procedural guidance.

V2 addresses that gap without turning Atelier into a model router or a full agent framework. The
runtime stays between agent hosts and their environments. It adds durable memory, sharper retrieval,
context compression, and lesson promotion while preserving the existing contract: no hidden
chain-of-thought storage, no secret storage, and no host-specific capability logic.

## Decision

Atelier V2 is built around three pillars.

**Stateful memory** adds editable core-memory blocks, archival passages, recall records, and
run-level memory frames. These records hold facts and state: what is true about a project, user, or
agent session. They remain distinct from ReasonBlocks and are stored in new V2 models rather than
new fields on existing V1 models.

**ReasonBlocks evolution** keeps `ReasonBlock` as the canonical procedure schema and adds a lesson
pipeline around it. Traces and failure clusters can produce lesson candidates, but promotion into a
live ReasonBlock requires review. This prevents raw execution history from becoming unreviewed
runtime policy.

**Context savings** becomes a measured runtime capability rather than a marketing claim. V2 tracks
naive input tokens, actual input tokens, cache-read/write tokens, tool-call counts, and
per-lever savings. The benchmark target is a median context-token reduction of at least 50 percent
on the SWE-bench-style harness.

Letta is adopted as an optional sidecar and client dependency, not forked. Atelier will vendor the
`letta-client` package through an optional extra and talk to a Letta server out of process when
configured. Letta is Apache-2.0 licensed, which makes client-level integration compatible with this
posture, but Atelier will not copy or subclass Letta internals. If Letta is absent, the in-process
memory store remains authoritative and functional.

The context-savings plan is adopted as implementation guidance, not as vendored
code. V2 implements the relevant concepts inside Atelier: combined search-read, batched edits,
AST-aware truncation, SQL inspection as a deterministic tool, fuzzy edit matching, cached reads,
scoped recall, and compact lifecycle support. Each lever must publish measurable savings data where
the runtime can observe it.

## Alternatives Considered

### Fork Letta

Rejected. Forking Letta would give fast access to an existing memory stack, but it creates a
forever-divergence problem. Atelier would inherit a large surface area it does not need to own:
agent orchestration, server behavior, model integrations, and a fast-moving memory implementation.
The V2 direction keeps Letta optional and external so Atelier can benefit from it without becoming a
Letta distribution.

### Build Memory Ground Up

Rejected as the only path. Atelier needs a first-class local memory store because it must work
without a sidecar, but building the entire memory system in isolation would recreate lessons that
Letta and similar systems have already learned. V2 therefore defines small, Atelier-owned models
and adapters while leaving room for Letta to provide scale-oriented memory and summarization when
available.

### Replace ReasonBlocks With Letta Blocks

Rejected. ReasonBlocks and memory blocks have different semantics. A ReasonBlock is a reviewed
procedure, dead end, or validation rule: what the agent should do. A memory block is editable state:
what is currently true. Mixing them would make retrieval harder to audit and could let transient
facts masquerade as durable procedure. V2 keeps two stores with explicit links instead of one
ambiguous store.

## Consequences

V2 introduces an optional memory dependency path. Runtime imports must stay guarded, and core code
must keep working with no Letta package, no Letta server, and no network.

The codebase will contain two durable stores with separate authority boundaries. The ReasonBlock
store remains the source of truth for procedures, rubrics, and known dead ends. The memory store
becomes the source of truth for editable facts, pinned state, archival passages, and recall events.

The embedder interface becomes a stable boundary. Local, OpenAI, Letta, and null embedders must
share one protocol so storage and recall code do not depend on a single vector provider.

Lesson promotion needs human review. The runtime can draft lesson candidates from traces and
failure clusters, but it must not silently convert operational history into active policy. Accepted
lessons should remain reviewable in the same way ReasonBlocks are reviewable today.

Context-savings claims require instrumentation. The runtime must record enough budget data to prove
what was avoided, not just that a smaller prompt was sent.

## References

- [Atelier V2 - Implementation Plan](../../../architecture/IMPLEMENTATION_PLAN_V2.md)
- [Atelier V2 - Data Model](../../../architecture/IMPLEMENTATION_PLAN_V2_DATA_MODEL.md)
- [Work-packets index](../../../architecture/work-packets/INDEX.md)
- [Letta GitHub repository](https://github.com/letta-ai/letta)
