# ADR 003: Memory Single-Primary Backend

## Status

Accepted for V3.

## Context

Earlier memory bridge experiments mirrored records between SQLite and optional external systems. That made recall behavior hard to reason about and created hidden fallback paths where data could appear in one backend but not another.

## Decision

Atelier chooses exactly one primary memory backend per runtime:

- `sqlite` is the default.
- `letta` is selected only by `ATELIER_MEMORY_BACKEND=letta`, explicit factory preference, or `[memory].backend = "letta"`.
- When Letta is primary, blocks and archival passages are written to Letta. SQLite may retain trace, run-frame, and recall audit records only.
- Explicit Letta selection must not silently fall back to SQLite on adapter failure.

## Consequences

Operators can inspect one source of truth for memory. Tests can assert backend behavior without accounting for dual-write races. Migration and re-embed jobs run against the selected backend and must state their scope.
