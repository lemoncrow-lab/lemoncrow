# Prefer Direct Implementation Over Subagent Delegation

- **id:** `prefer-direct-over-delegation`
- **domain:** `coding`
- **status:** `active`
- **task_types:** implementation, refactor

## Situation
When the parent agent already has concrete anchors — exact file paths, design decisions, and what to write — spawning a subagent causes the subagent to re-explore what the parent already knows, burning tokens on redundant discovery before it can implement. Subagents also frequently return mid-loop when used one-shot for straightforward tasks.

## Triggers
- spawn subagent
- delegate implementation
- atelier:code agent
- already know the files
- greenfield with known design

## Dead ends
- spawning a subagent to implement when the parent already has the file paths and full design
- expecting a one-shot subagent to complete a multi-step loop without SendMessage iteration
- treating a subagent's final text as task completion without verifying files were written

## Procedure
1. Check whether the current context already contains the target file paths, the exact content to write, and the test command.
2. If yes — implement directly. Do not spawn a subagent.
3. If no — do the minimal exploration to establish concrete anchors, then implement directly.
4. Only spawn atelier:code when the task is genuinely opaque (unknown code paths, complex debugging) and anchors cannot be established without significant exploration.
5. If a spawned subagent returns early with future-tense final text ("I will...", "Let me call..."), use SendMessage to continue it rather than re-implementing from scratch.

## Verification
- No subagent was spawned when file paths and content were already known.
- If a subagent was used, its return was verified (files exist on disk) before treating the task as done.

## Failure signals
- Subagent returns with only text and no files written
- Parent re-implements what the subagent was supposed to do
- Subagent final message is future-tense ("Let me...", "I'll now...")

## When not to apply
Genuinely opaque debugging tasks where the controlling code path is unknown and requires multi-file exploration to identify.
