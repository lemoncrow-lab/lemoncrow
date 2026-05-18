# Agent OS Principles

## Goals

1. Keep host entrypoints short. They should be a table of contents, not a manual.
2. Keep durable engineering knowledge in versioned repository artifacts.
3. Update rules once, then propagate them to every host surface.
4. Make quality, architecture, and rollout decisions discoverable without chat history.
5. Prefer repo-local, inspectable tooling over hidden or off-repo conventions.

## Repository rules

- Root instruction files point to deeper docs instead of duplicating whole manuals.
- Live docs under `docs/` are the current system of record.
- Historical and maintainer-only material stays in `docs-archive/`.
- Plans, decisions, and technical debt are committed so agents can find them later.
- When a prompt-level rule becomes durable, promote it into this tree or into code.

## Definition of done for repo rules

- The rule is documented in `docs/agent-os/` or a linked live doc.
- The rule is enforced by tests, CI, generated artifacts, or all three.
- The rule is visible from the host entrypoints without bloating them.
