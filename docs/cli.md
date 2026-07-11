# LemonCrow CLI Reference

The `lemon` command is the local control surface for the runtime, storage,
imports, benchmarks, background processing, and the optional visualization
stack.

Use the built-in help for the exact command tree:

```bash
lemon -h
lemon help runs
lemon help benchmark
lemon help background
```

## Global Options

| Flag           | Description                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `--version`    | Show the installed LemonCrow version and exit.                           |
| `--root PATH`  | Override the LemonCrow runtime data directory. Defaults to `~/.lemoncrow`. |
| `-h`, `--help` | Show help for the current command path.                                |

## Core Lifecycle Commands

These commands cover installation state, local runtime initialization, and the
optional visualization stack.

| Command                  | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `lemon init`           | Initialize the runtime store under `--root`.                   |
| `lemon uninstall`      | Remove LemonCrow-managed host integrations and wrappers.         |
| `lemon status`         | Show local plugin, auth, and subscription status.              |
| `lemon stack ...`      | Start, stop, inspect, or log the optional native UI/API stack. |
| `lemon service ...`    | Manage the HTTP/API service surface.                           |
| `lemon background ...` | Manage OS-level background services and auto-updates.          |
| `lemon worker ...`     | Inspect, enqueue, and run worker jobs.                         |

Common examples:

```bash
lemon init
lemon background status
lemon background restart
lemon background logs controller
```

## Background Services & Auto-Update

Manage background components via your OS-native manager (systemd/launchd).

| Subcommand                      | Purpose                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `lemon background install`    | Register services with systemd (Linux) or launchd (macOS). |
| `lemon background uninstall`  | Unregister and stop background services.                   |
| `lemon background status`     | Show service health and auto-update state.                 |
| `lemon background restart`    | Trigger a clean restart of the entire environment.         |
| `lemon background logs [svc]` | Stream logs for `controller` or `stack`.                   |

### Auto-Update Mechanism

The background controller automatically checks for git updates every hour (default).
If updates are found, it pulls the code, syncs dependencies, and restarts the
managed background services.

To configure the loop manually (not recommended for general use):

```bash
# Start the internal loop with custom auto-update settings
lemon servicectl run --auto-update --auto-update-interval-seconds 3600
```

## Traces, Ledgers, and Operational State

LemonCrow persists observable execution state rather than hidden reasoning.

| Command              | Purpose                                             |
| -------------------- | --------------------------------------------------- |
| `lemon runs ...`   | Record, list, and inspect run data.                 |
| `lemon ledger ...` | Manage run ledgers and session state.               |
| `lemon swarm ...`  | Fan out isolated child attempts into git worktrees. |

Examples:

```bash
lemon runs list
lemon ledger list
```

## Swarm Harness

`lemon swarm` is LemonCrow's multi-run harness. It creates one git worktree and
one isolated `LEMONCROW_ROOT` per child, launches the same child agent command in
each sandbox, collects structured result JSON, and merges accepted
improvements onto a coordinator-owned integration base.

```bash
lemon swarm start program.md --runs 3 --continuous \
  --runner ollama-claude \
  --runner-model qwen3.6 \
  --validate "make lint" \
  --validate "uv run pytest tests/gateway/test_cli_swarm.py -q"
```

What the harness guarantees today:

- one detached git worktree per child under a deterministic `*-swarm-worktrees/<run_id>/` pool
- one isolated `LEMONCROW_ROOT` plus `LEMONCROW_WORKSPACE_ROOT` / `CLAUDE_WORKSPACE_ROOT` per child
- a copied program spec at `.lemoncrow/swarm/program.md` in each child worktree
- structured child artifacts with summary, files changed, validations, cost/tokens (when available), final status, and live stdout/stderr previews
- persisted coordinator state under `--root/swarm/runs/<run_id>/state.json`
- a dedicated integration worktree whose accepted patches become the base for the next wave
- optional continuous mode that keeps running until a full wave produces no accepted improvements or you stop the job

Useful child environment variables:

| Variable                                          | Meaning                                                                                            |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `LEMONCROW_SWARM_SPEC_PATH`                         | Copied spec path inside the child worktree                                                         |
| `LEMONCROW_SWARM_RESULT_PATH`                       | Final structured result artifact written by the wrapper                                            |
| `LEMONCROW_SWARM_METADATA_PATH`                     | Optional child-authored JSON metadata (`summary`, `token_count`, `cost_usd`, `validation_results`) |
| `LEMONCROW_SWARM_RUN_ID` / `LEMONCROW_SWARM_CHILD_ID` | Stable coordinator and child identifiers                                                           |

Inspection commands:

```bash
lemon swarm list
lemon swarm status <run_id>
lemon swarm logs <run_id> --child-id wave-03-run-01
lemon swarm stop <run_id> --cleanup
```

If you omit the swarm spec path, LemonCrow resolves `program.md` relative to the
selected project root. The command fails clearly if that file is missing or if a
supplied spec path escapes the project root.

Built-in runner profiles:

| Runner          | Command shape                                           |
| --------------- | ------------------------------------------------------- |
| `claude`        | `claude --model <model> -p "<prompt>"`                  |
| `codex`         | `codex exec -m <model> "<prompt>"`                      |
| `copilot`       | `copilot --model <model> -p "<prompt>" --allow-all`     |
| `opencode`      | `opencode run -m <provider/model> "<prompt>"`           |
| `ollama-claude` | `ollama launch claude --model <model> -- -p "<prompt>"` |

You can still bypass profiles entirely and pass any raw child command after `--`
for custom API wrappers or other CLIs.

How patch acceptance works:

- children from the same wave are ranked
- successful, validated children with diffs are tried in score order
- disjoint or cleanly mergeable patches stack onto the integration base
- conflicting patches are rejected once a higher-ranked accepted patch already owns that space

Current limitation: the coordinator owns the isolation/runtime/merge harness,
but the actual child agent command is still supplied after `--` so you can plug
in Claude/Codex/Copilot or another runner that speaks LemonCrow MCP inside that
isolated environment. The current harness does **not** provide first-class
OpenAI or LiteLLM child execution; the dashboard only exposes the real CLI
runner path today.

## Retrieval, Search, and Code-Aware Helpers

Code retrieval, file reads, grep/search, and symbol lookup are exposed as
LemonCrow **MCP tools** (`read`, `grep`, `search`, `explore`, `codemod`)
rather than standalone CLI commands. Invoke them through your agent host or via
`lemon tools call <name>`. (Call-graph and reference relations — callers,
callees, usages — fold into one `explore` call.)

| Command                 | Purpose                                                |
| ----------------------- | ------------------------------------------------------ |
| `lemon code index` | Build or refresh the code index for a repository. |
| `lemon optimize`   | Show session cost optimization recommendations.   |

Examples:

```bash
lemon code index --repo-root .
lemon tools call grep --args '{"path":".","content_regex":"TODO"}'
```

## Knowledge, Lessons, and Failure Workflows

These commands manage the reusable knowledge layer and failure review flows.

| Command                      | Purpose                                         |
| ---------------------------- | ----------------------------------------------- |
| `lemon lesson ...`         | Review and promote lesson candidates.            |
| `lemon eval ...`           | Run eval suites (`mcp`, `retrieval`, `fitness`). |
| `lemon report`             | Generate an engineering governance report.       |
| `lemon import-style-guide` | Draft lesson candidates from Markdown guidance.  |
| `lemon proof ...`          | Run cost-quality proof gate workflows.           |

## Imports and Host Integrations

LemonCrow ships import and integration commands for supported agent hosts.

| Command                | Purpose                                               |
| ---------------------- | ----------------------------------------------------- |
| `lemon import` | Import sessions from all supported hosts in one pass. |

Supported session import hosts are defined in the runtime registry, not in the
docs. Use `lemon help import` to inspect the exact flags and options
supported by your installed build.

## Benchmarks, Savings, and External Reports

These commands support performance validation and cost-accounting workflows.

| Command                   | Purpose                                        |
| ------------------------- | ---------------------------------------------- |
| `lemon benchmark ...`   | Run benchmark suites (`mini`, `harbor`, `codebench`, `swe`, `local`). |
| `lemon benchmark local` | BYO-repo A/B: LemonCrow vs vanilla on your repo. |
| `lemon savings`         | Aggregate cost and token savings.              |
| `lemon session replay`  | Replay a past session; mark what one-shot search would collapse. |
| `lemon dashboard`       | Show the spend & savings dashboard.            |

Examples:

```bash
lemon benchmark mini --dry-run --json
lemon savings --json
lemon session replay --last 1
```

`lemon session replay` reconstructs a recorded session (Claude Code, Codex, or
opencode) from its transcript and replays it turn by turn — assistant text,
thinking, tool calls and outputs. For each native call it then invokes the
**real** LemonCrow tool that would have replaced it and shows the actual output:
grep/read loops collapse into a real `code_search` (whose ranked hit is checked
against the file the loop landed on), whole-file reads show the real `read`
outline, and `edit`/`bash` are shown as **safe previews** — never written or
executed. No model is re-run. By default it prints the terminal timeline, writes
an HTML page, and opens it in the browser.

| Flag | Effect |
| ---- | ------ |
| `--session-id <id> --host claude\|codex\|opencode` | Locate a session under a host's store. |
| `--file <path.jsonl>` | Replay a specific transcript directly (any host). |
| `--last N` | Replay the N most recent sessions. |
| `--repo <path>` | Repo root for real `code_search`/`read` (default: cwd). |
| `--no-live` | Structural view only — skip calling real LemonCrow tools. |
| `--no-open` | Do not open the HTML in a browser. |
| `--html <path>` / `--json` / `--no-color` | Output controls. |

```bash
lemon session replay --last 1                          # most recent session (+ opens HTML)
lemon session replay --session-id <id> --host codex    # a specific session
lemon session replay --file ./session.jsonl --repo .   # explicit transcript + repo
lemon session replay --last 1 --no-live --no-open      # structural only, no browser
```

`lemon benchmark local` is the user-facing BYO benchmark, also surfaced as the
`/benchmark` skill: point it at your own git repo and supply your own coding
prompts to compare LemonCrow against a vanilla Claude Code baseline on the same
model. It prints an up-front cost estimate and asks to confirm before any spend.

```bash
lemon benchmark local --repo . --prompt "add a docstring to the entry point"
lemon benchmark local --repo . --prompt "x" --estimate-only
```

Wire capture is off by default — cost comes from the CLI receipts, so no
mitmproxy or MITM CA cert is needed. Pass `--capture` to opt into mitmproxy
wire-level cost verification (requires `mitmproxy` and its CA cert).

The internal/dev suites are `lemon benchmark {codebench,swe}` and
`lemon eval {mcp,retrieval,fitness}`.

## Configuration and Account State

| Command                 | Purpose                                                 |
| ----------------------- | ------------------------------------------------------- |
| `lemon settings ...`  | Manage local plugin settings.                           |
| `lemon telemetry ...` | Enable, disable, or inspect product telemetry settings. |
| `lemon login`         | Create local auth state for plugin operations.          |
| `lemon logout`        | Remove local auth state.                                |
| `lemon share`         | Render referral or share text.                          |
| `lemon domain ...`    | Manage internal domain bundles.                         |
| `lemon letta ...`     | Manage the self-hosted Letta sidecar.                   |

## JSON Output

Many commands accept `--json` when the output is intended for automation or
other tools. Prefer the built-in help for each command path because JSON support
is command-specific rather than universal.

## Related References

- [README.md](https://github.com/lemoncrowhq/lemoncrow#readme)
- [docs/installation.md](installation.md)
- [docs/sdk/mcp.md](sdk/mcp.md)
