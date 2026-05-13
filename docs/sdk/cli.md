# CLI Reference

Atelier ships a full CLI for runtime operations, packs, benchmarking, and service deployment.

## Global Usage

```bash
atelier [--root PATH] COMMAND [OPTIONS]
atelier help [COMMAND...]
```

| Option        | Default                         | Description                             |
| ------------- | ------------------------------- | --------------------------------------- |
| `--root PATH` | `$ATELIER_ROOT` or `~/.atelier` | Path to the Atelier trace/history store |

All commands that return data support `--json` to emit a machine-readable JSON envelope instead of human-readable text.
Use `atelier help` for top-level help and `atelier help benchmark run` for a specific command path.

## Platform Surfaces

- `atelier pack list|create|validate|install|uninstall|search|info|benchmark`
- `atelier benchmark [run|core|runtime|savings|hosts|packs|full|compare|report|export|swe]`
- `atelier service start|config`
- `atelier worker start|run-once`

---

## Store Commands

### `init`

```bash
atelier init [--no-seed]
```

Create the store directory, run schema migrations, and seed 10 ReasonBlocks + 7 rubrics.

| Option      | Description                       |
| ----------- | --------------------------------- |
| `--no-seed` | Skip seeding (create empty store) |

---

## Reasoning Commands

### `context`

```bash
atelier context \
    [--task TEXT] \
    [--domain TEXT] \
    [--file PATH]... \
    [--tool TEXT]... \
    [--error TEXT]... \
    [--limit N] \
    [--json]
```

Retrieve a structured reasoning context prompt for an agent about to start a task. Reads the most relevant ReasonBlocks from the store (FTS5 search + domain filter), plus any known dead ends and environment constraints.

**Exit codes:** 0 = success

### `task`

```bash
atelier task TASK_DESCRIPTION... \
    [--domain TEXT] \
    [--file PATH]... \
    [--tool TEXT]... \
    [--error TEXT]... \
    [--limit N] \
    [--json]
```

Like `context` but accepts the task as positional arguments. Convenient for shell usage without `--task`.

### `check-plan`

```bash
atelier check-plan \
    --task TEXT \
    --step TEXT... \
    [--domain TEXT] \
    [--file PATH]... \
    [--tool TEXT]... \
    [--error TEXT]... \
    [--json]
```

Validate a proposed agent plan against known dead ends and required checks. Each `--step` is one step of the plan.

**Exit codes:**

- `0` = plan passes
- `2` = plan blocked (contains a known dead end or violates a constraint)

**Example (blocked):**

```bash
atelier check-plan \
    --task "Apply a live state change" \
    --domain state.change \
    --step "Resolve target from URL slug alone" \
    --step "Apply the update"
# → status: blocked, exit 2
```

### `rescue`

```bash
atelier rescue \
    --task TEXT \
    --error TEXT \
    [--domain TEXT] \
    [--file PATH]... \
    [--action TEXT]... \
    [--json]
```

Given a task and an error message, suggest a rescue procedure from the stored failure history and ReasonBlocks.

## Pack Commands

```bash
atelier pack create my-pack --type reasonblocks --path ./examples
atelier pack validate ./examples/my-pack --json
atelier pack install ./examples/my-pack
atelier pack search coding-general
atelier pack info atelier-pack-coding-general --json
```

Packs are production-scoped for internal use. External git/http sources are disabled by default.
The local catalog writes installed pack checksums and compatibility metadata to `packs/catalog/index.json`.

## Benchmark Commands

```bash
atelier benchmark run --prompt "Fix live state drift" --rounds 2 --json
atelier benchmark runtime --json
atelier benchmark hosts --json
atelier benchmark packs --json
atelier benchmark full --json
atelier benchmark swe show-modes
atelier benchmark compare --input .atelier/benchmarks/runtime/latest.json --input other.json
atelier benchmark report --input .atelier/benchmarks/runtime/latest.json
atelier benchmark export --input .atelier/benchmarks/runtime/latest.json --output report.csv --format csv
```

Benchmark commands are exposed only as explicit subcommands under `atelier benchmark`.

---

## Trace Commands

### `record-trace`

```bash
atelier record-trace [--input PATH]
# or via stdin:
echo '&#123;...trace json...&#125;' | atelier record-trace
```

Record an execution trace. Accepts JSON from stdin or a file. Required fields:

```json
&#123;
  "agent": "claude-code",
    "domain": "state.change",
    "task": "Apply a live config change",
  "status": "success",
    "commands_run": ["resolve-target", "api.write", "api.read"],
  "errors_seen": [],
    "diff_summary": "Applied change using canonical identifier",
    "output_summary": "Read-after-write verification passed"
&#125;
```

Full trace schema:

| Field                | Type         | Required            | Description                       |
| -------------------- | ------------ | ------------------- | --------------------------------- |
| `id`                 | string       | No (auto-generated) | Trace ID                          |
| `agent`              | string       | Yes                 | Agent identifier                  |
| `domain`             | string       | Yes                 | Domain (e.g. `state.change`)      |
| `task`               | string       | Yes                 | Task description                  |
| `status`             | enum         | Yes                 | `success`, `failed`, or `partial` |
| `files_touched`      | string[]     | No                  | Files modified                    |
| `tools_called`       | string[]     | No                  | Tools invoked                     |
| `commands_run`       | string[]     | No                  | Commands executed                 |
| `errors_seen`        | string[]     | No                  | Errors encountered                |
| `repeated_failures`  | string[]     | No                  | Patterns that recurred            |
| `diff_summary`       | string       | No                  | What changed                      |
| `output_summary`     | string       | No                  | Outcome summary                   |
| `validation_results` | object       | No                  | Rubric results                    |
| `created_at`         | ISO datetime | No                  | Timestamp (auto)                  |

All string fields are redacted before persistence (secrets removed).

### `extract-block`

```bash
atelier extract-block TRACE_ID [--save] [--json]
```

Analyze a trace and extract a candidate ReasonBlock. Shows confidence score and reasoning.

| Option   | Description                                                       |
| -------- | ----------------------------------------------------------------- |
| `--save` | Save the extracted block to the store and write a markdown mirror |
| `--json` | Emit JSON instead of human text                                   |

---

## ReasonBlock Commands

### `list-blocks`

```bash
atelier list-blocks [--domain TEXT] [--query TEXT] [--json]
```

List all ReasonBlocks, optionally filtered by domain or full-text query.

### `add-block`

```bash
atelier add-block --title TEXT --domain TEXT --procedure TEXT [--json]
```

Add a new ReasonBlock to the store.

### `block` (subcommand group — alias)

There are also individual subcommands for block management. Use `list-blocks` and `add-block` for most operations.

---

## Rubric Commands

### `run-rubric`

```bash
echo '&#123;"check_name": true, ...&#125;' | atelier run-rubric RUBRIC_ID [--json]
```

Run a rubric gate against a set of check results (JSON from stdin). Returns pass/blocked + which checks failed.

**Exit codes:**

- `0` = rubric passes
- `2` = rubric blocked (one or more required checks missing or false)

**Example with the state-change safety rubric:**

```bash
echo '&#123;
    "canonical_identifier_used": true,
    "pre_change_state_captured": true,
    "read_after_write_completed": true,
    "observed_state_matches_intent": true,
    "rollback_plan_available": true,
    "user_visible_surface_checked": true
&#125;' | atelier run-rubric rubric_state_change_safety
```

---

## Ledger Commands

The run ledger tracks per-run state for long-running agent sessions.

```bash
atelier ledger show [--session-id ID] [--json]
atelier ledger update --session-id ID --key TEXT --value TEXT
atelier ledger summarize [--session-id ID] [--json]
atelier ledger reset [--session-id ID]
```

---

## Failure Commands

```bash
atelier failure list [--json]
atelier failure show CLUSTER_ID [--json]
atelier failure accept CLUSTER_ID
atelier failure reject CLUSTER_ID

atelier analyze-failures [--domain TEXT] [--limit N] [--json]
atelier eval-from-cluster CLUSTER_ID [--save] [--json]
```

---

## Eval Commands

```bash
atelier eval list [--json]
atelier eval show EVAL_ID [--json]
atelier eval promote EVAL_ID
atelier eval deprecate EVAL_ID
atelier eval run [--eval-id ID] [--domain TEXT] [--json]
```

---

## Tool-Mode Commands

```bash
atelier tool-mode show [--json]
atelier tool-mode set MODE   # e.g. "smart" or "standard"
```

---

## Smart-Tool Commands (V2 MCP counterparts)

```bash
atelier smart-read PATH [--json]
atelier smart-search QUERY [--json]
atelier cached-grep PATTERN PATH... [--json]
atelier compress-context [--json]
atelier monitor-event --event-type TEXT [--data TEXT] [--json]
```

---

## Savings Commands

```bash
atelier savings [--json]
atelier savings-reset
atelier benchmark savings --baseline-command CMD --atelier-command CMD [--json]
```

---

## Service Commands

```bash
atelier service start [--host HOST] [--port PORT] [--reload]
atelier service config
```

Or via Makefile:

```bash
cd atelier && make service
```

---

## Worker Commands

```bash
atelier worker start [OPTIONS]
```

---

## OpenMemory Commands

OpenMemory bridge commands (require `ATELIER_OPENMEMORY_ENABLED=true`):

```bash
atelier openmemory status
atelier openmemory link-trace TRACE_ID [--context-id ID]
atelier openmemory fetch-context TASK_DESCRIPTION [--project-id ID]
```

By default these are stubs that print instructions for enabling the integration.
