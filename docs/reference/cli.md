# LemonCrow CLI Reference

The `lc` command is the local control surface for the runtime, storage,
imports, benchmarks, background processing, and the optional visualization
stack.

Use the built-in help for the exact command tree:

```bash
lc -h
lc help runs
lc help benchmark
lc help background
```

| Flag           | Description                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `--version`    | Show the installed LemonCrow version and exit.                           |
## Managed coding frontends

`lc code` and the permanent `lemoncode` wheel entry point run mature frontends over LemonCrow's owned runtime. Bare `lc` with no subcommand or flags also dispatches to `lc code` directly (`lc -h` still shows the full command tree). LemonCode remains the automatic default during the Pi migration; stock Pi v0.84.2 is available as an explicit managed engine. LemonCrow stays at the expensive boundary: it chooses the provider/model, owns tools and subagents, and applies cache, compaction, verification, and cost controls.

~~~bash
lc code                              # auto: LemonCode, Pi, Codex, Claude, native
lemoncode --engine lemoncode
lemoncode --engine pi
lemoncode --engine pi -p "fix the failing parser test"
lemoncode --engine pi --resume <pi-session-id>
lemoncode --engine codex -p "fix the failing parser test"
lc code --engine native              # original PromptToolkit fallback
~~~

Managed engines receive no outer tool calls: their UI and session store are reused, while a token-authenticated loopback gateway performs the agent loop. The LemonCode fork strips its redundant host prompt/tool schemas. Managed Pi loads one LemonCrow extension and disables Pi's tools, project resources, compaction, retries, telemetry, update checks, and package/model refresh; it stores config and sessions under the LemonCrow root. `lc code host install|update|status|build|remove --engine lemoncode|pi` manages the verified host binaries. Pi is pinned to reviewed upstream v0.84.2; LemonCode keeps its existing release-channel policy. Set `LEMONCROW_CODE_AUTO_ENGINE=pi` only for canary preference and remove it to roll back.
This avoids paying for the host's large system/tool prompt at the real model and
avoids duplicate host/LemonCrow tool execution. The gateway is started for the
CLI process and stopped on exit.

~~~bash
lc code --cache-policy 1h --budget cheap
lc code --model openai/gpt-5.4
lc code --max-cost 2.00
lc code --optimization-mode shadow   # default: measure, preserve provider behavior
lc code --optimization-mode enforce  # apply V2 policies
lc code --optimization-mode off      # one-setting V2 rollback
lc code --local-retrieval auto
lc code --local-retrieval force --local-retrieval-model ollama/qwen2.5-coder:7b
lc optimize decisions --json
lc code --no-mcp
~~~

Local retrieval `auto` skips the broad first-turn primer and exits before
retrieval fingerprinting/corpus scanning when the task names an explicit source
file (existing verified evidence is still validated), and it stays off for
small or non-retrieval tasks. Without
`--local-retrieval-model`, refinement is deterministic and makes zero planner
calls. Planner identifiers must start with `ollama/`, `lm_studio/`, or
`local/`; cloud model identifiers are ignored. `force` overrides only the
eligibility gate—use it with `--optimization-mode enforce` to inject the packet.

Direct Claude/Codex/OpenCode integrations advertise an eager core containing
normal coding tools, so code_search, read, edit, bash, and web_fetch never
require discovery. Rare capabilities use the deterministic tool broker. Set
LEMONCROW_MCP_TOOL_PROFILE=full to advertise every schema eagerly; managed
`lc code` does not expose this outer MCP catalog at all.

| Command                  | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `lc init`           | Initialize the runtime store under `--root`. Fully local; no login or account required. |
| `lc uninstall`      | Remove LemonCrow-managed host integrations and wrappers.         |
| `lc status`         | Show local plugin and runtime status.                          |
| `lc stack ...`      | Start, stop, inspect, or log the optional native UI/API stack. |
| `lc service ...`    | Manage the HTTP/API service surface.                           |
| `lc background ...` | Manage OS-level background services and auto-updates.          |
| `lc worker ...`     | Inspect, enqueue, and run worker jobs.                         |

Common examples:

```bash
lc init
lc background status
lc background restart
lc background logs controller
```

## Background Services & Auto-Update

Manage background components via your OS-native manager (systemd/launchd).

| Subcommand                      | Purpose                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `lc background install`    | Register services with systemd (Linux) or launchd (macOS). |
| `lc background uninstall`  | Unregister and stop background services.                   |
| `lc background status`     | Show service health and auto-update state.                 |
| `lc background restart`    | Trigger a clean restart of the entire environment.         |
| `lc background logs [svc]` | Stream logs for `controller` or `stack`.                   |

### Auto-Update Mechanism

The background controller automatically checks for git updates every hour (default).
If updates are found, it pulls the code, syncs dependencies, and restarts the
managed background services.

To configure the loop manually (not recommended for general use):

```bash
# Start the internal loop with custom auto-update settings
lc servicectl run --auto-update --auto-update-interval-seconds 3600
```

## Traces, Ledgers, and Operational State

LemonCrow persists observable execution state rather than hidden reasoning.

| Command              | Purpose                                             |
| -------------------- | --------------------------------------------------- |
| `lc runs ...`   | Record, list, and inspect run data.                 |
| `lc ledger ...` | Manage run ledgers and session state.               |
| `lc swarm ...`  | Fan out isolated child attempts into git worktrees. |

Examples:

```bash
lc runs list
lc ledger list
```

## Swarm Harness

`lc swarm` is LemonCrow's multi-run harness. It creates one git worktree and
one isolated `LEMONCROW_ROOT` per child, launches the same child agent command in
each sandbox, collects structured result JSON, and merges accepted
improvements onto a coordinator-owned integration base.

```bash
lc swarm start program.md --runs 3 --continuous \
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
lc swarm list
lc swarm status <run_id>
lc swarm logs <run_id> --child-id wave-03-run-01
lc swarm stop <run_id> --cleanup
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
| `lemoncode`     | `lemoncode run -m <provider/model> "<prompt>"`          |
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
`lc tools call <name>`. (LemonGraph — call-graph and reference relations —
callers, callees, usages — fold into one `explore` call.)

| Command         | Purpose                                                               |
| --------------- | --------------------------------------------------------------------- |
| `lc code index` | Build or refresh the code index for a repository.                      |
| `lc optimize`   | Show session cost optimization recommendations.                        |

Examples:

```bash
lc code index --repo-root .
lc dashboard open  # choose Map in the existing dashboard
lc tools call grep --args '{"path":".","content_regex":"TODO"}'
```

### Excluding files from indexing

`lc code index` walks git-visible files (`git ls-files`, so `.gitignore` is
already honored) plus a built-in skip list for caches/build output. To
exclude specific files -- data dumps, fixtures, generated JSON -- that are
tracked in git and therefore not covered by `.gitignore`, add gitignore-syntax
patterns to `.lemoncrow/.ignore`:

```gitignore
# .lemoncrow/.ignore
fixtures/
*.snapshot.json
data/**/*.csv
```

Patterns in `.lemoncrow/.ignore` are a union with `.gitignore`, not a
replacement for it -- both are applied. Re-run `lc code index --reindex`
after changing the file.

## Knowledge, Lessons, and Failure Workflows

These commands manage the reusable knowledge layer and failure review flows.

| Command                      | Purpose                                         |
| ---------------------------- | ----------------------------------------------- |
| `lc lesson ...`         | Review and promote lesson candidates.            |
| `lc eval ...`           | Run eval suites (`mcp`, `retrieval`, `fitness`). |
| `lc report`             | Generate an engineering governance report.       |
| `lc import-style-guide` | Draft lesson candidates from Markdown guidance.  |
| `lc proof ...`          | Run cost-quality proof gate workflows.           |

## Imports and Host Integrations

LemonCrow ships import and integration commands for supported agent hosts.

| Command                | Purpose                                               |
| ---------------------- | ----------------------------------------------------- |
| `lc import` | Import sessions from all supported hosts in one pass. |

Supported session import hosts are defined in the runtime registry, not in the
docs. Use `lc help import` to inspect the exact flags and options
supported by your installed build.

## Benchmarks, Savings, and External Reports

These commands support performance validation and cost-accounting workflows.

| Command                   | Purpose                                        |
| ------------------------- | ---------------------------------------------- |
| `lc benchmark ...`   | Run benchmark suites (`mini`, `harbor`, `codebench`, `swe`, `local`). |
| `lc benchmark local` | BYO-repo A/B: LemonCrow vs vanilla on your repo. |
| `lc savings`         | Aggregate cost and token savings.              |
| `lc session replay`  | Replay a past session; mark what one-shot search would collapse. |
| `lc dashboard`       | Show the spend & savings dashboard.            |

Examples:

```bash
lc benchmark mini --dry-run --json
lc savings --json
lc session replay --last 1
```

`lc session replay` reconstructs a recorded session (Claude Code, Codex, or
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
| `--session-id <id> --host claude\|codex\|opencode\|lemoncode` | Locate a session under a host's store. |
| `--file <path.jsonl>` | Replay a specific transcript directly (any host). |
| `--last N` | Replay the N most recent sessions. |
| `--repo <path>` | Repo root for real `code_search`/`read` (default: cwd). |
| `--no-live` | Structural view only — skip calling real LemonCrow tools. |
| `--no-open` | Do not open the HTML in a browser. |
| `--html <path>` / `--json` / `--no-color` | Output controls. |

```bash
lc session replay --last 1                          # most recent session (+ opens HTML)
lc session replay --session-id <id> --host codex    # a specific session
lc session replay --file ./session.jsonl --repo .   # explicit transcript + repo
lc session replay --last 1 --no-live --no-open      # structural only, no browser
```

`lc benchmark local` is the user-facing BYO benchmark, also surfaced as the
`/benchmark` skill: point it at your own git repo and supply your own coding
prompts to compare LemonCrow against a vanilla Claude Code baseline on the same
model. It prints an up-front cost estimate and asks to confirm before any spend.

```bash
lc benchmark local --repo . --prompt "add a docstring to the entry point"
lc benchmark local --repo . --prompt "x" --estimate-only
```

Wire capture is off by default — cost comes from the CLI receipts, so no
mitmproxy or MITM CA cert is needed. Pass `--capture` to opt into mitmproxy
wire-level cost verification (requires `mitmproxy` and its CA cert).

The internal/dev suites are `lc benchmark {codebench,swe}` and
`lc eval {mcp,retrieval,fitness}`.

## Configuration and Optional Account

The `lc account` commands are an **optional** convenience for linking a hosted
account. They gate nothing — LemonCrow is fully local and every feature works
without them; they are never required and never prompted. Anonymous remote
telemetry is **on by default**; turn it off with `lc telemetry remote off` (see
[Privacy & network behavior](../setup/privacy.md)).

| Command             | Purpose                                                            |
| ------------------- | ----------------------------------------------------------------- |
| `lc settings ...`   | Manage local plugin settings.                                     |
| `lc telemetry ...`  | Inspect or toggle telemetry; remote telemetry is on by default.   |
| `lc account login`  | Optional: link a hosted account. Gates nothing; never required.   |
| `lc account logout` | Remove the optional local account link.                           |
| `lc account status` | Show whether an optional account link is present.                 |
| `lc share`          | Render referral or share text.                                    |
| `lc domain ...`     | Manage internal domain bundles.                                   |
| `lc letta ...`      | Manage the self-hosted Letta sidecar.                             |

Inspect or change any of them with `lc settings`:

```bash
lc settings show --category mcp
```

### Where edits are allowed to write

LemonCrow's read tools accept any absolute path; its **write** tools (`edit`,
batch edit, `bash`) are confined to the workspace root plus `/tmp`. Widen that
boundary in one of three ways:

- `permissions.additionalDirectories` in `~/.claude/settings.json` or
  `~/.claude/settings.local.json` (all workspaces) — **live**, no restart.
- `permissions.additionalDirectories` in `<workspace>/.claude/settings.json` or
  `<workspace>/.claude/settings.local.json` — **live**, no restart.
- `lc settings set mcp.additional_edit_dirs "$HOME/notes:/srv/shared/plans"` (the
  `LEMONCROW_ADDITIONAL_DIRS` env var on the `lc` MCP server entry) — `:`-separated
  (a comma is not a separator) and takes effect only after the MCP connection
  reconnects.

Every entry must be absolute once a leading `~` is expanded; relative entries and
blanket grants (`/` or a bare `~`) are skipped with a warning.

The legacy top-level `additionalDirectories` key is read too. Matching is
component-wise, so allowing `/srv/plans` does not allow `/srv/plans-secret`. See
[setup/installation.md](../setup/installation.md#widening-the-edit-write-boundary)
for the full table.

## JSON Output

Many commands accept `--json` when the output is intended for automation or
other tools. Prefer the built-in help for each command path because JSON support
is command-specific rather than universal.

## Related References

- [README.md](https://github.com/lemoncrow-lab/lemoncrow#readme)
- [setup/installation.md](../setup/installation.md)
- [sdk/mcp.md](../sdk/mcp.md)
