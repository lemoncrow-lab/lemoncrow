# Preserve Unrelated User Work

- **id:** `preserve-unrelated-user-work`
- **domain:** `coding`
- **status:** `active`
- **task_types:** debugging, implementation, refactor

## Situation

Agents often work in a dirty repository. Unknown changes are not safe to discard just because they are inconvenient.

## Triggers

- dirty tree
- existing changes
- uncommitted edits
- merge context

## Dead ends

- revert unknown changes to get a clean state
- overwrite neighboring user edits without understanding them
- use destructive git commands to simplify the task

## Procedure

1. Inspect touched files for pre-existing edits before changing them.
2. Work with unrelated changes in place unless they directly conflict.
3. Avoid destructive resets, checkouts, and cleans.
4. If unrelated changes create a direct conflict, stop and surface that conflict explicitly.

## Verification

- Only intended lines or files changed.
- Unrelated worktree changes were preserved.
- No destructive git operation was used.

## Failure signals

- git diff includes unrelated reversions
- user changes disappear during the task

## When not to apply

Repositories or worktrees that were explicitly created as disposable sandboxes.
