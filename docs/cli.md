# Atelier CLI Reference

The `atelier` command is the local control surface for the runtime, storage,
imports, benchmarks, background processing, and the optional visualization
stack.

Use the built-in help for the exact command tree:

```bash
atelier -h
atelier help runs
atelier help benchmark
atelier help background
```

## Global Options

| Flag           | Description                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `--version`    | Show the installed Atelier version and exit.                           |
| `--root PATH`  | Override the Atelier runtime data directory. Defaults to `~/.atelier`. |
| `-h`, `--help` | Show help for the current command path.                                |

## Core Lifecycle Commands

These commands cover installation state, local runtime initialization, and the
optional visualization stack.

| Command                  | Purpose                                                 |
| ------------------------ | ------------------------------------------------------- |
| `atelier init`           | Initialize the runtime store under `--root`.            |
| `atelier uninstall`      | Remove Atelier-managed host integrations and wrappers.  |
| `atelier status`         | Show local plugin, auth, and subscription status.       |
| `atelier stack ...`      | Start, stop, inspect, or log the optional native UI/API stack. |
| `atelier service ...`    | Manage the HTTP/API service surface.                    |
| `atelier background ...` | Manage OS-level background services and auto-updates.   |
| `atelier worker ...`     | Inspect, enqueue, and run worker jobs.                  |

Common examples:

```bash
atelier init
atelier background status
atelier background restart
atelier background logs controller
```

## Background Services & Auto-Update

Manage background components via your OS-native manager (systemd/launchd).

| Subcommand                      | Purpose                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `atelier background install`    | Register services with systemd (Linux) or launchd (macOS). |
| `atelier background uninstall`  | Unregister and stop background services.                   |
| `atelier background status`     | Show service health and auto-update state.                 |
| `atelier background restart`    | Trigger a clean restart of the entire environment.         |
| `atelier background logs [svc]` | Stream logs for `controller` or `stack`.                   |

### Auto-Update Mechanism

The background controller automatically checks for git updates every hour (default).
If updates are found, it pulls the code, syncs dependencies, and restarts the
managed background services.

To configure the loop manually (not recommended for general use):

```bash
# Start the internal loop with custom auto-update settings
atelier servicectl run --auto-update --auto-update-interval-seconds 3600
```
## Traces, Ledgers, and Operational State

Atelier persists observable execution state rather than hidden reasoning.

| Command                    | Purpose                                               |
| -------------------------- | ----------------------------------------------------- |
| `atelier runs ...`         | Record, list, and inspect run data.                   |
| `atelier ledger ...`       | Manage run ledgers and session state.                 |
| `atelier swarm ...`        | Fan out isolated child attempts into git worktrees.   |

Examples:

```bash
atelier runs list
atelier ledger list
```

## Swarm Harness (MVP)

`atelier swarm` is the first Atelier-owned multi-run harness. It creates one git
worktree and one isolated `ATELIER_ROOT` per child, launches the same child
agent command in each sandbox, collects structured result JSON, and ranks the
candidates explicitly.

```bash
atelier swarm start program.md --runs 3 \
  --validate "make lint" \
  --validate "uv run pytest tests/gateway/test_cli_swarm.py -q" \
  -- claude-code run --spec {spec}
```

What the harness guarantees today:

- one detached git worktree per child under a deterministic `*-swarm-worktrees/<run_id>/` pool
- one isolated `ATELIER_ROOT` plus `ATELIER_WORKSPACE_ROOT` / `CLAUDE_WORKSPACE_ROOT` per child
- a copied program spec at `.atelier-swarm/program.md` in each child worktree
- structured child artifacts with summary, files changed, validations, cost/tokens (when available), and final status
- persisted coordinator state under `--root/swarm/runs/<run_id>/state.json`

Useful child environment variables:

| Variable | Meaning |
| --- | --- |
| `ATELIER_SWARM_SPEC_PATH` | Copied spec path inside the child worktree |
| `ATELIER_SWARM_RESULT_PATH` | Final structured result artifact written by the wrapper |
| `ATELIER_SWARM_METADATA_PATH` | Optional child-authored JSON metadata (`summary`, `token_count`, `cost_usd`, `validation_results`) |
| `ATELIER_SWARM_RUN_ID` / `ATELIER_SWARM_CHILD_ID` | Stable coordinator and child identifiers |

Inspection commands:

```bash
atelier swarm status <run_id>
atelier swarm stop <run_id> --cleanup
```

Current limitation: the coordinator provides the isolation/runtime harness, but
the actual child agent command is still supplied after `--` so you can plug in
Claude/Codex/Copilot or another runner that speaks Atelier MCP inside that
isolated environment.

## Retrieval, Search, and Code-Aware Helpers

Code retrieval, file reads, grep/search, and symbol lookup are exposed as
Atelier **MCP tools** (`read`, `grep`, `search`, `symbols`, `node`, `callers`,
`callees`, `usages`, `impact`, `explore`, `pattern`) rather than standalone CLI
commands. Invoke them through your agent host or via `atelier tools call <name>`.

| Command                  | Purpose                                                 |
| ------------------------ | ------------------------------------------------------- |
| `atelier code index`     | Build or refresh the SCIP code index for a repository.  |
| `atelier tool-mode ...`  | Configure smart tool replacement/shadow/suggest modes.  |
| `atelier tool-report`    | Report tool usage, savings, and redundancy patterns.    |
| `atelier optimize`       | Show session cost optimization recommendations.         |

Examples:

```bash
atelier code index --repo-root .
atelier tools call grep --args '{"path":".","content_regex":"TODO"}'
```

## Knowledge, Lessons, and Failure Workflows

These commands manage the reusable knowledge layer and failure review flows.

| Command                      | Purpose                                         |
| ---------------------------- | ----------------------------------------------- |
| `atelier lesson ...`         | Review and promote lesson candidates.           |
| `atelier failure ...`        | Inspect and manage failure clusters.            |
| `atelier eval ...`           | Manage and run evaluation cases.                |
| `atelier eval-from-cluster`  | Draft an eval from an accepted failure cluster. |
| `atelier report`             | Generate an engineering governance report.      |
| `atelier import-style-guide` | Draft lesson candidates from Markdown guidance. |
| `atelier deprecate`          | Mark a block as deprecated.                     |
| `atelier quarantine`         | Quarantine a block from retrieval.              |
| `atelier consolidate`        | Run manual consolidation.                       |
| `atelier consolidation ...`  | Review consolidation candidates.                |
| `atelier proof ...`          | Run cost-quality proof gate workflows.          |

## Imports and Host Integrations

Atelier ships import and integration commands for supported agent hosts.

| Command                | Purpose                                               |
| ---------------------- | ----------------------------------------------------- |
| `atelier import`       | Import sessions from all supported hosts in one pass. |
| `atelier claude ...`   | Claude Code import and session workflows.             |
| `atelier codex ...`    | Codex session workflows.                              |
| `atelier copilot ...`  | Copilot session workflows.                            |
| `atelier gemini ...`   | Gemini CLI session workflows.                         |
| `atelier opencode ...` | OpenCode session workflows.                           |
| `atelier bash ...`     | Shell interception helpers.                           |

Supported session import hosts are defined in the runtime registry, not in the
docs. Use `atelier help import` or the host-specific help output to inspect the
exact flags and options supported by your installed build.

## Benchmarks, Savings, and External Reports

These commands support performance validation and cost-accounting workflows.

| Command                   | Purpose                                        |
| ------------------------- | ---------------------------------------------- |
| `atelier benchmark ...`   | Run benchmark suites and benchmark reports.    |
| `atelier savings`         | Aggregate cost and token savings.              |
| `atelier savings-detail`  | Show per-operation savings breakdowns.         |
| `atelier savings-reset`   | Reset persisted savings state.                 |
| `atelier loop-report`     | Generate a full loop/pathology report.         |
| `atelier external-status` | Check optional upstream analyzer availability. |
| `atelier external-report` | Run supported upstream JSON reports.           |

Examples:

```bash
atelier benchmark full --json
atelier savings --json
atelier loop-report --help
```

## Configuration and Account State

| Command                 | Purpose                                                 |
| ----------------------- | ------------------------------------------------------- |
| `atelier settings ...`  | Manage local plugin settings.                           |
| `atelier telemetry ...` | Enable, disable, or inspect product telemetry settings. |
| `atelier login`         | Create local auth state for plugin operations.          |
| `atelier logout`        | Remove local auth state.                                |
| `atelier share`         | Render referral or share text.                          |
| `atelier domain ...`    | Manage internal domain bundles.                         |
| `atelier letta ...`     | Manage the self-hosted Letta sidecar.                   |

## JSON Output

Many commands accept `--json` when the output is intended for automation or
other tools. Prefer the built-in help for each command path because JSON support
is command-specific rather than universal.

## Related References

- [README.md](../README.md)
- [docs/quickstart.md](quickstart.md)
- [docs/installation.md](installation.md)
- [docs/sdk/mcp.md](sdk/mcp.md)
