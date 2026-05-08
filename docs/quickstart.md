# Quickstart — Atelier in 5 Minutes

This guide gets Atelier running in a fresh project in under 5 minutes.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) installed

## Step 1 — Install

```bash
cd atelier
uv sync --all-extras
```

## Step 2 — Initialize the store

```bash
uv run atelier init
```

This creates `.atelier/` with:

- `atelier.db` — SQLite store with FTS5 search
- `blocks/` — 10 pre-seeded ReasonBlocks for debugging, code changes, live state changes, source-of-truth fixes, and gate discipline
- `rubrics/` — 7 pre-seeded rubrics including `rubric_code_change` and `rubric_state_change_safety`
- `traces/` — empty, will fill as agents run

## Step 3 — Check a plan

The core feature: block dangerous agent plans _before_ execution.

```bash
uv run atelier lint \
  --task "Apply a live config update" \
  --domain state.change \
  --step "Resolve target from URL slug alone" \
  --step "Apply the change"
```

Expected output:

```text
status: blocked
exit: 2
warnings:
  - dead end: resolve target from url slug alone
  - suggested plan uses a canonical identifier plus read-after-write verification
```

Now try a safe plan:

```bash
uv run atelier lint \
  --task "Apply a live config update" \
  --domain state.change \
  --step "Resolve and record the canonical identifier" \
  --step "Capture pre-change state" \
  --step "Apply the change" \
  --step "Read back the state and diff against intent"
```

Expected: `status: pass` (exit 0)

## Step 4 — Get reasoning context

Before an agent starts a task, inject relevant procedures into its context:

```bash
uv run atelier reasoning \
  --task "Fix generated output that drifts back after refresh" \
  --domain source.truth \
  --file src/content/generate.py
```

This returns a structured prompt block with relevant ReasonBlocks, known dead ends, and environment constraints.

## Step 5 — Run a rubric gate

After an agent completes a task, verify it met all required checks:

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": false
&#125;' | uv run atelier verify rubric_state_change_safety
```

Expected: `status: blocked` (because a required verification check failed).

## Step 6 — Record a trace

After an agent run (success or failure), record what happened:

```bash
echo '&#123;
  "agent": "claude-code",
  "domain": "state.change",
  "task": "Apply a live config change",
  "status": "success",
  "commands_run": ["resolve-target", "api.write", "api.read"],
  "errors_seen": [],
  "diff_summary": "Applied the config change using a canonical identifier",
  "output_summary": "Read-after-write verification matched intent"
&#125;' | uv run atelier trace record
```

## Step 7 — Extract a ReasonBlock from a trace

When an agent solves something non-obviously, capture the pattern for future runs:

```bash
uv run atelier trace list
# → find the trace ID

uv run atelier block extract <trace-id>
# → shows candidate block with confidence score

uv run atelier block extract <trace-id> --save
# → saves to store and markdown mirror
```

## Step 8 — Use smart runtime commands

```bash
# Smart retrieval across ReasonBlocks
uv run atelier search "read after write verification"

# AST-aware file read with symbol summary
uv run atelier read src/atelier/gateway/adapters/runtime.py --max-lines 120

# Batch edit input format: [{"path": "...", "find": "...", "replace": "..."}]
uv run atelier edit --input edits.json
```

## Next Steps

- **Connect to your AI agent host**: [docs/hosts/](hosts/)
- **Full CLI reference**: [docs/cli.md](cli.md)
- **Core architecture docs**: [docs/core/](core/)
- **Storage and configuration**: [docs/installation.md](installation.md)
- **Engineering details**: [docs/engineering/](engineering/)
