# Installation

Install LemonCrow once, then use it with an existing coding agent, through LemonCrow's managed coding loop, or for Review.

## Install

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash
```

The installer asks nothing. It installs Local Review: the `lc` client (`lc` and `lcr` are real executables, not shell aliases) and a loopback Review server on port 7420. No account or sign-in is needed. Pass `--hosted` (alias `--cloud`) for Hosted Review at api.lemoncrow.com instead, which needs an invited account; run `lc auth login` next. Re-running the bare installer keeps an existing hosted install hosted. Runtime is fetched once, the first time you run `lc rt enable <host>` (`claude`, `codex`, or `opencode`). To pre-install it, pass `--product runtime` or `--product both`, adding `--agent <host>` to enable an agent. `--dry-run` prints the plan; `--url` and `--port` are the other flags. Environment overrides: `LEMONCROW_RELEASE_TAG`, `LEMONCROW_RELEASE_BASE_URL` (alternate asset base, for example `file://` for a local rehearsal), and `LEMONCROW_NO_LAZY_INSTALL=1` to disable the first-use fetch.

Verify the install:

```bash
lc --version
lc status
```

The normal Runtime path does not require a LemonCrow daemon or a listening local application server. Repository intelligence is prepared in the repository you are working in.

## Use LemonCrow with your existing coding agent

Claude Code, Codex, and OpenCode support a temporary launch that adds the LemonCrow plugin only to that run:

```bash
lc with claude
lc with codex
lc with opencode
```

The agent keeps its existing model, provider account, credentials, and saved settings. Run these commands from a Git worktree so LemonCrow can prepare the repository index.

The local index lives under:

```text
<repo>/.lemoncrow/workspace/
```

It is incremental and reused the next time you work in that repository.

### Keep the integration enabled

For Claude Code, Codex, and OpenCode you can persist the integration:

```bash
lc rt enable claude
lc rt enable codex
lc rt enable opencode
```

Check it with:

```bash
lc rt status
```

Remove it with:

```bash
lc rt disable claude
```

### Cursor

Cursor is also supported, but it uses an installed Cursor integration rather than the `lc with` launcher. The integration covers both the Cursor IDE and `cursor-agent` CLI.

The public installer and `lc rt enable` do not configure Cursor (`lc rt enable cursor` reports `unsupported agent 'cursor'`). Today the integration is installed from a source checkout:

```bash
bash scripts/install_cursor.sh
```

For workspace-specific rules and hooks:

```bash
bash scripts/install_cursor.sh --workspace /path/to/repo
```

See [Cursor integration](../hosts/cursor-install.md) for the exact artifacts and verification steps.

## Use Runtime Lite locally

Use your existing coding agent with the open-source local Runtime:

```bash
lc with claude
lc with codex
lc with opencode
```

Cursor uses its integration from a source checkout (above). Persist a supported integration with `lc rt enable <host>`. The repository index is local and can be prepared explicitly with:

```bash
lc runtime index
```

Pi and LemonCode are public managed coding-agent hosts:

```bash
lc rt host install --engine pi
lc rt host status --engine pi
lc rt host install --engine lemoncode   # full CLI only; the installed client says so
```

The installed client manages Pi itself. LemonCode host lifecycle needs the full LemonCrow CLI from a [source checkout](#source-checkout--contributor-setup). Installing either host does not enable Hosted Runtime and does not require a Pro plan.

## Use Hosted Runtime

Point an existing-agent integration explicitly at Hosted Runtime. A Pro plan grants access; see [LemonCrow Pro: Hosted Runtime](./pro-hosted-runtime.md) for the full sequence:

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product runtime --hosted
lc auth login
lc rt enable claude --hosted
```

`lc code` is reserved for LemonCrow's Hosted owned coding agent, which is not yet available. The installed client says so and exits; it does not fall back to a local private engine:

```bash
lc code --help
```

| Path | What it adds |
| --- | --- |
| Runtime Lite | Open-source repository intelligence and LemonCrow tools on the developer machine |
| Public hosts | Pi and LemonCode host binaries managed with `lc rt host` |
| Hosted Runtime | Private deeper retrieval and richer code relationships. Cross-workspace context and LemonCrow-owned managed execution are not yet available |
| Enterprise Runtime | The private Runtime in customer-controlled/dedicated infrastructure with identity, policy, managed workspaces, governance, and audit |

A Pro plan is a commercial access tier for Hosted Runtime. Signing in does not silently change local Runtime Lite into hosted execution. To set up a Pro organization's machines, follow [LemonCrow Pro: Hosted Runtime](./pro-hosted-runtime.md).

## Source checkout / contributor setup

For development from this repository:

```bash
git clone https://github.com/lemoncrow-lab/lemoncrow.git
cd lemoncrow
uv sync --all-extras
make oss-dev    # the real public installer on locally built assets, in the sandbox ~/.lemoncrow-oss (port 7428)
make verify
```

Developer targets come in two sets with the same verbs. `oss-*` uses only this
repository; `hosted-*` also needs the private `enterprise/` checkout. Only
`-prod` touches your live `~/.lemoncrow`; every other verb stays in that side's
sandbox, so both sandboxes can run side by side.

| Verb | `oss-*` | `hosted-*` |
| --- | --- | --- |
| `-dev` | `install.sh --local` on staged assets into `~/.lemoncrow-oss` (server on 7428) | `hosted-up`, then `install.sh --cloud` into `~/.lemoncrow-hosted` |
| `-up` / `-down` | the sandbox Review server | enterprise API 7432, UI 3130, Authward |
| `-status` | servers and the sandbox client | servers and the sandbox client |
| `-reset` | `-down`, then delete the sandbox | `-down`, then delete the sandbox |
| `-ui` | Vite HMR against `API_PORT` (7428) | disposable preview with sample data (3131) |
| `-ui-build` | `frontend/dist` | `enterprise/frontend/dist` |
| `-test` | both products in a temp home (7429) | Review client qualification on a real server |
| `-prod` | full rebuild into the live home (7420) | `-test`, then the live home (`HOSTED_URL`) |

`oss-prod` and `hosted-prod` replace each other, since the live home holds one
deployment. Sandboxes never configure agent hosts; `-dev` and `-status` print
the environment that points `lc` at a sandbox. `make release-stage` stages the
release assets every installer target uses.

Useful host-specific installers live under `scripts/install_<host>.sh` and are documented under [`docs/hosts/`](../hosts/all-agent-clis.md).

## Shared services and self-hosting

Review, hosted Runtime, Enterprise, and integration/API use cases can involve shared HTTP services. They are not required for the normal local Runtime plugin path.

For production or self-hosted service topology, storage backends, ports, and operational controls, use [Production readiness](../operations/production-readiness.md) and the [architecture reference](../reference/architecture.md).

## Upgrading: behaviour changes

Security hardening changed how the local product behaves. The full list is in the [changelog](https://github.com/lemoncrow-lab/lemoncrow/blob/main/CHANGELOG.md#behaviour-changes-for-local-installs); the ones you will notice first:

- A bare install is Local Review; Hosted Review needs `--hosted`.
- Run `lc review --open` once to connect the browser to the Local Review server. The browser session then renews itself; run `lc review --open` again if the Reader says it is not connected.
- The Local Review server answers only `127.0.0.1`, `localhost` or `[::1]` on its own port and returns `403` otherwise. Tunnel it with the same port (`ssh -L 7420:127.0.0.1:7420 host`), not a different one.
- Hosted mode ignores `~/.lemoncrow/token`. Sign in with `lc auth login`, or set `LEMONCROW_TOKEN_FILE` or `LEMONCROW_TOKEN`.
- Review's **Send to agent** keeps each agent's own approvals: Codex runs in `workspace-write` with network off, and Copilot and OpenCode get no auto-approve.
- Remote MCP pairing codes (`lc rt mcp serve`) are single use and valid for 15 minutes, and `--no-auth` is loopback only.

## Uninstall

Remove one product without disturbing the other. A product is required and there is no prompt:

```bash
lc uninstall runtime
lc uninstall review
lc uninstall all --dry-run   # preview
lc uninstall all
```

Update with `lc update`, which re-runs the installer for each installed product and leaves host registrations untouched.

Host-specific uninstall scripts remain available for audited or recovery workflows.
