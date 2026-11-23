# Installation

Install LemonCrow once, then use it with an existing coding agent, through LemonCrow's managed coding loop, or for Review.

## Install

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash
```

The installer asks nothing. It installs Local Review: the `lc` client (`lc` and `lcr` are real executables, not shell aliases) and a loopback Review server on port 7420. No account or sign-in is needed. Re-running the bare installer keeps an existing hosted install hosted. Workspaces is fetched once, the first time you run `lc rt enable <host>` (`claude`, `codex`, or `opencode`).

### Installer options

Pass options after `bash -s --`, for example `curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --hosted`.

| Option | What it does |
| --- | --- |
| `--hosted` (alias `--cloud`) | Hosted Review at api.lemoncrow.com instead of Local Review. Needs an invited account; run `lc auth login` next |
| `--product workspaces` or `--product both` | Pre-install Workspaces instead of fetching it on first use (`--product runtime` still works) |
| `--agent <host>` | With `--product`, also enable that agent |
| `--dry-run` | Print the plan and change nothing |
| `--url <url>` | The hosted server to use with `--hosted` |
| `--port <port>` | The port of the local Review server (default 7420) |
| `LEMONCROW_RELEASE_TAG` | Install this release tag |
| `LEMONCROW_RELEASE_BASE_URL` | An alternate asset base, for example `file://` for a local rehearsal |
| `LEMONCROW_NO_LAZY_INSTALL=1` | Disable the first-use fetch |

Every release asset has a Sigstore signature beside it, `<asset>.sigstore.json`, made by the public repository's release workflow at the release's tag; the published `install.sh` also pins the SHA-256 of every helper and archive it fetches. To check the installer yourself with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) before running it:

```bash
base=https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download
curl -fsSLO "$base/install.sh" -fsSLO "$base/install.sh.sigstore.json"
cosign verify-blob --bundle install.sh.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/lemoncrow-lab/lemoncrow/\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com install.sh
bash install.sh
```

Verify the install, then get your first AI review in two more steps (a key or an agent you are signed in to, then `lc review --ai`; see [Your first AI review](./ai-review.md)):

```bash
lc --version
lc status
export ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY; or skip it and use: lc review --ai --agent claude
cd your-project && lc review --ai
```

The loopback server on port 7420 is Local Review's. Workspaces needs no LemonCrow daemon or listening local application server of its own. Repository intelligence is prepared in the repository you are working in.

### On macOS

Linux is the release-qualified platform. On macOS the installer, `lc review` and the Review Reader work, but they are not yet covered by release qualification. Local check runs use the macOS Seatbelt sandbox instead of bubblewrap, and replaying a recorded run (`--reproduce`, see the [Review reference](../reference/review.md)) needs bubblewrap, so it refuses on macOS. The GitHub Action is unaffected: it runs on the CI runner (Linux or macOS). See the [support matrix](../reference/support-matrix.md).

## Install the GitHub Action

To have LemonCrow review your pull requests in CI, on your own model key, add the Action to a workflow instead of installing anything: see [GitHub Action: AI pull request review](./github-action.md).

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

## Use Workspaces Lite locally

Use your existing coding agent with open-source, local Workspaces Lite:

```bash
lc with claude
lc with codex
lc with opencode
```

Cursor uses its integration from a source checkout (above). Persist a supported integration with `lc rt enable <host>`. The repository index is local and can be prepared explicitly with:

```bash
lc runtime index
```

Pi is the public managed coding-agent host:

```bash
lc rt host install --engine pi
lc rt host status --engine pi
```

The installed client manages Pi itself. Installing Pi does not enable hosted retrieval and does not require a Pro plan.

## Use hosted retrieval (Pro)

Point an existing-agent integration explicitly at hosted retrieval. A Pro plan grants access; see [LemonCrow Pro: hosted retrieval](./pro-hosted-runtime.md) for the full sequence:

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product workspaces --hosted
lc auth login
lc rt enable claude --hosted
```

`lc code` is reserved for LemonCrow's hosted coding agent, which is not yet available. The installed client says so and exits; it does not fall back to a local private engine:

```bash
lc code --help
```

| Path | What it adds |
| --- | --- |
| Workspaces Lite | Open-source repository intelligence and LemonCrow tools on the developer machine |
| Public hosts | Pi host binaries managed with `lc rt host` |
| Workspaces Pro | Private deeper retrieval and richer code relationships. Cross-workspace context and LemonCrow-owned managed execution are not yet available |
| Enterprise Workspaces | The private engine in customer-controlled/dedicated infrastructure with identity, policy, managed workspaces, governance, and audit |

Workspaces Pro is a commercial access tier for hosted retrieval. Signing in does not silently change local Workspaces Lite into hosted execution. To set up a Pro organization's machines, follow [Workspaces Pro: hosted retrieval](./pro-hosted-runtime.md).

## Source checkout / contributor setup

For development from this repository:

```bash
git clone https://github.com/lemoncrow-lab/lemoncrow.git
cd lemoncrow
uv sync --all-extras
make oss-dev    # the real public installer on locally built assets, in the sandbox ~/.lemoncrow-oss (port 7428)
make verify-full
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

Review, Workspaces Pro, Enterprise, and integration/API use cases can involve shared HTTP services. They are not required for the normal local Workspaces plugin path.

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

`lc uninstall` lists exactly what it will remove, removes what the installer added after one question, and asks separately before it deletes anything of yours. A product is required:

```bash
lc uninstall all --dry-run   # list what would be removed; changes nothing
lc uninstall all             # asks; --yes skips the question (your data stays), --purge removes your data too
lc uninstall review          # Review only, Workspaces keeps working (or: workspaces)
```

What it removes: the `lc` and `lcr` commands, the Review client and engine, the installer's scripts, the background server, carried review results (a cache) and the lines the installer added to `~/.bashrc`, `~/.profile` and the other shell startup files. What it asks about: your settings and any API key you saved (`~/.lemoncrow/plugin_settings.json`), the reviews you opened in the Reader, the git hooks `lc review hook install` wrote, and the `.lemoncrow` folder of the repository you run it in. It never runs `npm` and never touches coding-agent settings or your code. Without a terminal it removes nothing until you pass `--yes`. Details: [CLI reference](../reference/cli.md#removal-lc-uninstall).

Update with `lc update`, which re-runs the installer for each installed product and leaves host registrations untouched. When `cosign` is installed, it runs the installer only after checking its signature as above, and refuses one that has no signature or does not verify.

Host-specific uninstall scripts remain available for audited or recovery workflows.
