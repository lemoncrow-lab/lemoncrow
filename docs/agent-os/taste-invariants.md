# Agent OS Taste Invariants

These invariants exist to keep the repository legible to both humans and future
agent runs.

## Source of truth

- Do not guess API shapes. Read the defining schema or API surface first.
- Prefer typed boundaries over probing data structures "YOLO style".
- Keep architectural rules in live docs and back them with tests or checks.

## Repository shape

- Root host entrypoints stay short and point to the deeper docs tree.
- Plans, scorecards, decisions, and technical debt stay committed in-repo.
- Do not point live docs at nonexistent internal paths.

## Adversarial stance

- **Task completion ≠ goal achievement.** A file can exist without being
  functional, wired, or data-connected. Apply the verification ladder before
  claiming a task is done.
- **Be adversarial until the codebase proves otherwise.** Assume every change
  contains a defect until the evidence refutes it.
- **Never trust a claim — verify the code.** Prose summaries and task status
  flags are not evidence of correctness. Read the actual implementation.
- **Do not downgrade findings to seem less harsh.** If it is a correctness or
  security issue, it is a Blocker.

## Validation

- Match validation to the surface you changed.
- Frontend changes must run `cd frontend && npm run build` and `cd frontend && npm run test`.
- Python changes must run lint, type check, and tests through the existing project commands.
- Host instruction changes must regenerate the derived artifacts.

## Logging and evidence

- Prefer structured, queryable evidence over prose-only reassurance.
- Capture runtime evidence with scripts or tasks when validating service behavior.
- Keep verification steps reproducible by another agent without chat context.

## Learnings discipline

- Record decisions, lessons, patterns, and surprises in `trace(learnings=[...])`
  — not in chat where they will be lost.
- Every learning must have a source attribution to a trace ID or artifact path.
- Do not fabricate learnings; extract only what is explicitly evidenced.

## Code intelligence

- *"If the caller already knows the symbol name, do not run a text search."*
- *"Default to outline-first responses. Expand only on intent."*
- *"Never edit at line numbers when the target is a named symbol."*

## Python string style

- **Never use implicit string concatenation across source lines.** Adjacent
  string literals are easy to misread (missing trailing spaces silently join
  words, layout `\n` characters smuggle structure into prose) and diff badly
  when bullets get reordered.
- For a single sentence or paragraph, use **one string literal**. Project
  line-length is 120 — let the literal run long rather than concatenating.
- For content with layout (bullets, paragraphs, embedded newlines), use
  **`textwrap.dedent("""…""")`** so the source layout matches the output.
  Hoist to a module-level `_CONSTANT` if reused or longer than ~5 lines.
- This applies to MCP tool descriptions, schema field descriptions, log
  messages, and any other multi-line prose stored in Python source.

Avoid:

```python
description=(
    "Operation to perform. "
    "\n• `search` — Find symbols by name. "
    "\n• `node` — Full definition for one symbol."
)
```

Prefer (layout content):

```python
from textwrap import dedent

_OP_DESCRIPTION = dedent("""\
    Operation to perform.

    • `search` — Find symbols by name.
    • `node` — Full definition for one symbol.
""")
```

Prefer (single sentence, no layout):

```python
description = "Number of context lines to include before each content match."
```