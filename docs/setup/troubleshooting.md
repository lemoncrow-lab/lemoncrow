# Troubleshooting

Start with the product surface you are actually using. Normal local Runtime use is a coding-agent plugin/integration and does not depend on a LemonCrow Runtime daemon or port `7420`.

## `lc` is not found after install

```text
bash: lc: command not found
```

Check where LemonCrow was installed and make sure its bin directory is on `PATH`:

```bash
which lc
lc --version
```

The installer adds the bin directory to `~/.bashrc` or `~/.zshrc` for new terminals. In the terminal you installed from, run the `Activate in this terminal:` line the installer printed (`source ~/.lemoncrow/shell-env.sh` for the default home), or open a new terminal.

## LemonCrow is installed but my agent does not see it

First check product and integration state:

```bash
lc status
lc rt status
```

### Claude Code / Codex / OpenCode

Try the temporary plugin path from inside a Git repository:

```bash
lc with claude
lc with codex
lc with opencode
```

If that works and you want the integration always available, persist it with `lc rt enable <host>`.

If `lc with` says it needs a Git repository, `cd` into the repository first. Runtime Lite prepares repository intelligence under `.lemoncrow/workspace/`.

### Cursor

Cursor uses an integration installed from a source checkout rather than `lc with`; the public installer and `lc rt enable` do not configure it (`lc rt enable cursor` reports `unsupported agent 'cursor'`). Install it with `bash scripts/install_cursor.sh` from the checkout (see [Installation](installation.md#cursor)).

From a source checkout:

```bash
bash scripts/verify_cursor.sh
```

For workspace-specific installs:

```bash
bash scripts/verify_cursor.sh --workspace /path/to/repo
```

See [Cursor integration](../hosts/cursor-install.md).

## `lc code --pro` is rejected

The old local Pro execution mode is removed; `--pro` is not a compatibility flag. `lc code` now means the Hosted LemonCrow-owned coding agent, which is not yet available.

For local Runtime Lite, use `lc with claude|codex|opencode|pi`, Cursor's integration from a source checkout, or a persistent local integration. Pi is public, managed with `lc rt host ... --engine pi`, and launchable for one Lite session with `lc with pi`; LemonCode host lifecycle needs the full CLI from a source checkout.

If `lc code` reports `This OSS Runtime client does not contain the Hosted owned-agent execution loop`, that is expected: use a local Runtime Lite integration, or Hosted Runtime through `lc rt enable <host> --hosted` (see [LemonCrow Pro: Hosted Runtime](pro-hosted-runtime.md)).

## A command says it is not part of this client

The installed `lc` client carries Review capture, the AI review in the terminal (`lc review --ai`, `hook`, `init-action`, `config`), Runtime Lite and product management. Some documented commands belong to the full LemonCrow CLI, which a source checkout runs as `uv run lc ...` (see [CLI reference](../reference/cli.md)). The installed client says so instead of running them:

| You ran | What it means |
| --- | --- |
| `unknown command: host` | Use `lc rt host`. Other Runtime verbs also live under `lc rt` |
| `unknown command: usage`, `doctor`, `settings` | Full CLI only. Check this install with `lc status` and `lc rt status` |
| `lc session ... is not part of serverless Runtime Lite` (also `run`, `context`, `rt model`, `rt savings`, `rt work`, `rt map`, `rt dashboard`) | Full CLI only |
| `lc rt telemetry ...` | The installed client sends no product telemetry (`lc audit` shows `"product_telemetry": false`), so there is nothing to turn off |
| `unknown review option: --since-my-review` (also `--mark`, `--comment`, `--feedback`, ...) | Full Review CLI only. Use the Review Reader (`lc review --open`) to mark, comment, see changes since your review and copy feedback |
| `unknown review option: --agent` (also `--pr`, `--fail-on`, `--strict`) without `--ai` | These belong to the AI review: add `--ai`, for example `lc review --pr 123 --ai`. Nothing calls a model until `--ai` is on the command line |
| `lc review --ai needs the local Review engine` | This install has no local engine (a Hosted install does not). Run the installer again: `curl -fsSL https://lemoncrow.com/install.sh \| bash` |
| `LemonCode host lifecycle requires the managed/full CLI` | Full CLI only. Pi works in the installed client: `lc rt host install --engine pi` |

## `lc review --ai` does not review

| It says | Do this |
| --- | --- |
| `no model route is configured` | `export ANTHROPIC_API_KEY=sk-ant-...` (or `OPENAI_API_KEY`) and run it again, or `lc review --ai --agent claude` (also `codex`, `opencode`) to use an agent CLI you are signed in to |
| `Anthropic rejected the API key (HTTP 401): check ANTHROPIC_API_KEY` | The key is wrong or revoked. Nothing was reviewed and nothing is billed |
| `could not reach Anthropic` | A network or proxy problem; `ANTHROPIC_BASE_URL` points a gateway or a local server |
| `AI review is turned off` | The intelligence policy's kill switch is off in `.lemoncrow` or your settings |

The review exits `0` when it could not run, so a hook or script never fails because of it; add `--strict` to make that exit `2`. A pre-push hook never stops a push because LemonCrow broke: it stops only for an open finding at or above `--fail-on`, or, with `--strict`, an incomplete review.

## Runtime Lite index problems

The local repository index lives under:

```text
<repo>/.lemoncrow/workspace/
```

Useful checks:

```bash
lc runtime index --help
```

If a repository was moved, partially deleted, or left with corrupted generated state, remove only the generated `.lemoncrow/workspace/` directory and let LemonCrow rebuild it. Do not delete committed project files.

## Review does not open

Check Review/product state first:

```bash
lc status
lc review --no-open
```

`lc status` shows the Review `Deployment`: `local` or `hosted`.

**Local Review** (the default install) needs no account. If `lc review` cannot reach the server, check and restart it with the installed lifecycle helper:

```bash
bash ~/.lemoncrow/install/scripts/local_server.sh status
bash ~/.lemoncrow/install/scripts/local_server.sh diagnose
bash ~/.lemoncrow/install/scripts/local_server.sh restart
```

If the Reader page says *this browser is not connected to LemonCrow Review* or *connection expired*, run `lc review --open` in your terminal: it connects the browser once and the session then renews itself.

**Hosted Review** (installed with `--hosted`) needs an invited account:

```text
unauthenticated: Hosted Review requires sign-in; run `lc auth login` first
```

Run `lc auth login`, then `lc review --open`. Check sign-in with `lc auth status`. In hosted mode the client ignores `~/.lemoncrow/token`; for an operator-provided credential set `LEMONCROW_TOKEN_FILE` or `LEMONCROW_TOKEN`. If you meant to review locally without an account, reinstall with `curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --local`.

Review and Runtime are separate product surfaces; a healthy Runtime plugin does not imply that a hosted/local Review service is configured, and vice versa.

## `403 forbidden` from the Local Review server

The Local Review server answers only a literal loopback `Host` on its own port (`127.0.0.1:7420`, `localhost:7420` or `[::1]:7420`) and refuses anything else with `403`, which also blocks DNS-rebinding pages. This affects SSH port forwarding: keep the same local port.

```bash
ssh -L 7420:127.0.0.1:7420 devbox   # works: open http://127.0.0.1:7420
ssh -L 9000:127.0.0.1:7420 devbox   # refused: the Host is 127.0.0.1:9000
```

A browser session is bound to its exact origin, so after changing how you reach the server, run `lc review --open` again to reconnect.

## A deliberately configured local/shared service is down

This section applies only when you intentionally run a Review/shared HTTP service or contributor deployment. It is **not** a prerequisite for normal local Runtime plugin use.

From a source checkout, use the service/operations commands documented by your deployment and see [Production readiness](../operations/production-readiness.md).

If your deployment uses loopback port `7420`, inspect it with:

```bash
ss -ltnp | grep ':7420'
```

Do not expose a loopback-only local Review/service port to other machines as though it were a hardened remote service.

## Spend differs from external analytics

LemonCrow prices imported sessions from recorded provider usage where available. Mixed-model sessions can legitimately have no single session-level model.

When comparing totals:

- prefer recorded per-model usage over a guessed primary model;
- include explicitly billed tool usage where the provider exposes it;
- expect differences when another analytics product applies synthetic pricing instead of provider-reported usage.

## Optional Postgres / pgvector problems

Postgres is not required for the normal local Runtime plugin path.

If a self-hosted/enterprise setup reports:

```text
ERROR: extension "vector" is not available
```

pgvector is optional unless that deployment explicitly enables vector-backed features. Follow the deployment's storage configuration rather than enabling it for Local Lite.

## Source checkout / contributor issues

### `make verify` fails with Ruff or Black

```bash
cd lemoncrow
uv run ruff check --fix src tests
uv run python -m black src tests
make verify
```

### Postgres-gated tests are skipped

That is expected unless `LEMONCROW_DATABASE_URL` is configured for the test run.
