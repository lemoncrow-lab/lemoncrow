# ReasonBlock Authoring

ReasonBlocks are the reusable procedure documents Atelier retrieves for risky or
repetitive work. The current runtime validates them as YAML against the
`ReasonBlock` model in `src/atelier/core/foundation/models.py`.

## Current File Format

Atelier's built-in seed blocks live under `src/atelier/infra/seed_blocks/` and
look like this:

```yaml
id: canonical-identifier-over-display-name
title: Canonical Identifier Over Display Name
domain: state.change
task_types: [integration_change, data_write, rollback]
triggers:
  - slug
  - handle
  - title
  - url
tool_patterns:
  - api.write
  - db.write
situation: >
  Human-readable labels such as titles, URLs, paths, and display names can
  drift. Mutations and rollbacks should target a stable canonical identifier.
dead_ends:
  - resolve target from url slug alone
  - use display name as stable identity
procedure:
  - Resolve the target through its canonical stable identifier.
  - Record that identifier before the write.
  - Use the same identifier for the mutation and the readback.
verification:
  - A canonical identifier was recorded before the write.
  - The same identifier was used for readback.
failure_signals:
  - wrong target updated
  - ambiguous match set
required_rubrics:
  - rubric_state_change_safety
when_not_to_apply: >
  Pure read-only exploration where no state mutation or rollback will happen.
```

## Required and Optional Fields

- Required: `id`, `title`, `domain`, `situation`, `procedure`
- Optional selectors: `task_types`, `triggers`, `file_patterns`, `tool_patterns`
- Optional safeguards: `dead_ends`, `verification`, `failure_signals`, `required_rubrics`, `when_not_to_apply`
- Runtime-managed fields such as `status`, counts, and timestamps are persisted by the store rather than hand-authored in normal source content

`procedure` must contain at least one step.

## Current Contributor Workflow

1. Add or edit a YAML block under `src/atelier/infra/seed_blocks/`.
1. Validate the bundle with a fresh store import:

```bash
ATELIER_ROOT=/tmp/atelier-docs-check uv run atelier init
```

1. Inspect the loaded content with the runtime:

```bash
uv run atelier list-blocks
ATELIER_DEV_MODE=1 uv run atelier context --task "Your task" --domain coding
```

1. Run targeted tests or benchmarks if the block changes retrieval, rescue, or
   savings behavior.

## Important Note

Older docs referred to a public `atelier pack create/install` workflow and
Markdown-frontmatter blocks. That is not the current public CLI surface.
