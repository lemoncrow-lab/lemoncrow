# CLI Reference

Atelier ships a full CLI for runtime operations, packs, benchmarking, and service deployment.

## Global Usage

```bash
uv run atelier [--root PATH] COMMAND [OPTIONS]
```

| Option        | Default                         | Description                             |
| ------------- | ------------------------------- | --------------------------------------- |
| `--root PATH` | `$ATELIER_ROOT` or `~/.atelier` | Path to the Atelier trace/history store |

All commands that return data support `--json` to emit a machine-readable JSON envelope instead of human-readable text.

## Platform Surfaces

- `atelier pack list|create|validate|install|uninstall|search|info|benchmark`
- `atelier benchmark [run|compare|report|export]`
- `atelier service start|config`
- `atelier worker start|run-once`

---

## Store Commands

### `init`

```bash
uv run atelier init [--no-seed]
```

Create the store directory, run schema migrations, and seed 10 ReasonBlocks + 5 rubrics.

| Option      | Description                       |
| ----------- | --------------------------------- |
| `--no-seed` | Skip seeding (create empty store) |

---

## Reasoning Commands

### `context`

```bash
uv run atelier context \
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
uv run atelier task TASK_DESCRIPTION... \
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
uv run atelier check-plan \
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
uv run atelier check-plan \
    --task "Publish Shopify product" \
    --domain beseam.shopify.publish \
    --step "Parse product handle from PDP URL" \
    --step "Use handle to update metafields"
# → status: blocked, exit 2
```

### `rescue`

```bash
uv run atelier rescue \
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
uv run atelier pack create my-pack --type reasonblocks --path ./examples
uv run atelier pack validate ./examples/my-pack --json
uv run atelier pack install ./examples/my-pack
uv run atelier pack search coding-general
uv run atelier pack info atelier-pack-coding-general --json
```

Packs are production-scoped for internal use. External git/http sources are disabled by default.
The local catalog writes installed pack checksums and compatibility metadata to `packs/catalog/index.json`.

## Benchmark Commands

```bash
uv run atelier benchmark --prompt "Fix Shopify publish" --json
uv run atelier benchmark run --prompt "Fix Shopify publish" --rounds 2 --json
uv run atelier benchmark compare --input .atelier/benchmarks/runtime/latest.json --input other.json
uv run atelier benchmark report --input .atelier/benchmarks/runtime/latest.json
uv run atelier benchmark export --input .atelier/benchmarks/runtime/latest.json --output report.csv --format csv
```

The legacy no-action invocation remains valid; `run` is the new explicit action.

---

## Trace Commands

### `record-trace`

```bash
uv run atelier record-trace [--input PATH]
# or via stdin:
echo '&#123;...trace json...&#125;' | uv run atelier record-trace
```

Record an execution trace. Accepts JSON from stdin or a file. Required fields:

```json
&#123;
  "agent": "claude-code",
  "domain": "beseam.shopify.publish",
  "task": "Publish product ID 123",
  "status": "success",
  "commands_run": ["shopify.get_product", "shopify.update_metafield"],
  "errors_seen": [],
  "diff_summary": "Updated metafields for gid://shopify/Product/123",
  "output_summary": "Product published, audit passed"
&#125;
```

Full trace schema:

| Field                | Type         | Required            | Description                            |
| -------------------- | ------------ | ------------------- | -------------------------------------- |
| `id`                 | string       | No (auto-generated) | Trace ID                               |
| `agent`              | string       | Yes                 | Agent identifier                       |
| `domain`             | string       | Yes                 | Domain (e.g. `beseam.shopify.publish`) |
| `task`               | string       | Yes                 | Task description                       |
| `status`             | enum         | Yes                 | `success`, `failed`, or `partial`      |
| `files_touched`      | string[]     | No                  | Files modified                         |
| `tools_called`       | string[]     | No                  | Tools invoked                          |
| `commands_run`       | string[]     | No                  | Commands executed                      |
| `errors_seen`        | string[]     | No                  | Errors encountered                     |
| `repeated_failures`  | string[]     | No                  | Patterns that recurred                 |
| `diff_summary`       | string       | No                  | What changed                           |
| `output_summary`     | string       | No                  | Outcome summary                        |
| `validation_results` | object       | No                  | Rubric results                         |
| `created_at`         | ISO datetime | No                  | Timestamp (auto)                       |

All string fields are redacted before persistence (secrets removed).

### `extract-block`

```bash
uv run atelier extract-block TRACE_ID [--save] [--json]
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
uv run atelier list-blocks [--domain TEXT] [--query TEXT] [--json]
```

List all ReasonBlocks, optionally filtered by domain or full-text query.

### `add-block`

```bash
uv run atelier add-block --title TEXT --domain TEXT --procedure TEXT [--json]
```

Add a new ReasonBlock to the store.

### `block` (subcommand group — alias)

There are also individual subcommands for block management. Use `list-blocks` and `add-block` for most operations.

---

## Rubric Commands

### `run-rubric`

```bash
echo '&#123;"check_name": true, ...&#125;' | uv run atelier run-rubric RUBRIC_ID [--json]
```

Run a rubric gate against a set of check results (JSON from stdin). Returns pass/blocked + which checks failed.

**Exit codes:**

- `0` = rubric passes
- `2` = rubric blocked (one or more required checks missing or false)

**Example with the Shopify publish rubric:**

```bash
echo '&#123;
  "product_identity_uses_gid": true,
  "pre_publish_snapshot_exists": true,
  "write_result_checked": true,
  "post_publish_refetch_done": true,
  "post_publish_audit_passed": true,
  "rollback_available": true,
  "localized_url_test_passed": true,
  "changed_handle_test_passed": true
&#125;' | uv run atelier run-rubric rubric_shopify_publish
```

---

## Ledger Commands

The run ledger tracks per-run state for long-running agent sessions.

```bash
uv run atelier ledger show [--run-id ID] [--json]
uv run atelier ledger update --run-id ID --key TEXT --value TEXT
uv run atelier ledger summarize [--run-id ID] [--json]
uv run atelier ledger reset [--run-id ID]
```

---

## Environment Commands

Reasoning environments define domain-specific constraints and known tool patterns.

```bash
uv run atelier env list [--json]
uv run atelier env show ENV_ID [--json]
uv run atelier env context ENV_ID [--json]
uv run atelier env validate ENV_ID
```

---

## Failure Commands

```bash
uv run atelier failure list [--json]
uv run atelier failure show CLUSTER_ID [--json]
uv run atelier failure accept CLUSTER_ID
uv run atelier failure reject CLUSTER_ID

uv run atelier analyze-failures [--domain TEXT] [--limit N] [--json]
uv run atelier eval-from-cluster CLUSTER_ID [--save] [--json]
```

---

## Eval Commands

```bash
uv run atelier eval list [--json]
uv run atelier eval show EVAL_ID [--json]
uv run atelier eval promote EVAL_ID
uv run atelier eval deprecate EVAL_ID
uv run atelier eval run [--eval-id ID] [--domain TEXT] [--json]
```

---

## Tool-Mode Commands

```bash
uv run atelier tool-mode show [--json]
uv run atelier tool-mode set MODE   # e.g. "smart" or "standard"
```

---

## Smart-Tool Commands (V2 MCP counterparts)

```bash
uv run atelier smart-read PATH [--json]
uv run atelier smart-search QUERY [--json]
uv run atelier cached-grep PATTERN PATH... [--json]
uv run atelier compress-context [--json]
uv run atelier monitor-event --event-type TEXT [--data TEXT] [--json]
```

---

## Savings Commands

```bash
uv run atelier savings [--json]
uv run atelier savings-reset
uv run atelier benchmark [--json]
```

---

## Service Commands

```bash
uv run atelier service start [--host HOST] [--port PORT] [--reload]
uv run atelier service config
```

Or via Makefile:

```bash
cd atelier && make service
```

---

## Worker Commands

```bash
uv run atelier worker start [OPTIONS]
```

---

## OpenMemory Commands

OpenMemory bridge commands (require `ATELIER_OPENMEMORY_ENABLED=true`):

```bash
uv run atelier openmemory status
uv run atelier openmemory link-trace TRACE_ID [--context-id ID]
uv run atelier openmemory fetch-context TASK_DESCRIPTION [--project-id ID]
```

By default these are stubs that print instructions for enabling the integration.
