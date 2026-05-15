# Troubleshooting

This page starts with installed-product issues first. Source-checkout and contributor issues are lower down.

## `atelier` Is Not Found After Install

**Symptom:**

```text
bash: atelier: command not found
```

**Cause:** `~/.local/bin` is not on `PATH` yet.

**Fix:** add it to your shell profile and restart the shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## `atelier background status` Shows Services Are Not Running

Atelier background services should start automatically. If they are stopped or failed:

**Fix:** Restart them using your background manager:

```bash
atelier background restart
atelier background status
```

If you want to inspect what the controller is doing:

```bash
atelier background logs controller
```

## `atelier background install` Fails on macOS

**Cause:** `launchd` requires the target directory to exist and might have permission issues.

**Fix:** Ensure `~/Library/LaunchAgents` exists and re-run:

```bash
mkdir -p ~/Library/LaunchAgents
atelier background install --with-stack
```

## `atelier stack start` Fails

**Common causes:** Docker or Docker Compose is not installed, not running, or the ports are already in use.

Check Docker first:

```bash
docker --version
docker compose version
```

If ports `3125` or `8787` are already busy, inspect what owns them first:

```bash
# Check if the managed background service is already running
atelier background status

# Check for manual docker processes
docker ps

# Check network ports
ss -ltnp | grep -E ':3125|:8787'
```

Then stop or reconfigure the conflicting process, or reset the Atelier stack:

```bash
atelier background restart
```

## The UI Loads but API Calls Fail With Auth Errors

For local no-auth service usage, start the service explicitly like this:

```bash
ATELIER_REQUIRE_AUTH=false atelier service start --host 0.0.0.0 --port 8787
```

If you want auth enabled, set `ATELIER_API_KEY` and configure the client that is calling the service.

## `atelier-mcp` Is Not Found

If you used the install script, re-run it and verify both commands:

```bash
atelier --version
atelier-mcp --version
```

If you are on a source checkout instead of an installed setup:

```bash
cd atelier
uv sync --all-extras
```

## `atelier context`, `atelier rescue`, or `atelier verify` Returns `noop`

Those commands are developer-mode surfaces.

**Cause:** `ATELIER_DEV_MODE=1` is not enabled in the shell or host environment.

**Fix:**

```bash
ATELIER_DEV_MODE=1 atelier context --task "Describe the task" --domain coding
```

For host integrations, set `ATELIER_DEV_MODE=1` in the MCP server environment if
you want active context/retrieval behavior instead of passive compatibility stubs.

If the command still behaves unexpectedly, reinitialize the store and verify the
runtime is healthy:

```bash
atelier init
atelier background status
atelier worker list
```

## Gemini CLI MCP Tool Not Available

**Symptom:** Atelier tools do not appear in Gemini CLI.

**Cause:** Gemini requires absolute paths in its MCP configuration.

Use the install guide here:

- [hosts/gemini-cli-install.md](hosts/gemini-cli-install.md)

## pgvector Extension Not Available

**Symptom:**

```text
ERROR: extension "vector" is not available
```

pgvector is optional. Atelier works normally without it. Only enable it when you want embedding-based similarity search on Postgres.

## Atelier Spend Differs From External Analytics

If Atelier and an external tool such as CodeBurn disagree on spend, check the
trace pricing source before comparing totals.

Atelier now prices imported sessions from persisted raw `usage_entries` on each
trace. Mixed-model sessions intentionally leave `trace.model` blank; the source
of truth is `usage_entries` and the derived `model_usages`, not a synthetic
session-level “primary model”.

If you changed importer or pricing logic, rebuild imported traces first:

```bash
atelier import --force
```

If the services are already running, restart them to pick up new code or configuration:

```bash
atelier background restart
atelier background status
```

When comparing totals, keep these rules in mind:

- Atelier totals come from backend pricing helpers over `usage_entries`.
- Mixed-model sessions should be inspected via `model_usages`, not `trace.model`.
- Explicit billed tools belong in `usage_entries` with `kind: tool` and `cost_usd`.
- External tools may still disagree if they apply provider-specific synthetic
  pricing instead of raw per-model billing.

## Source Checkout and Contributor Issues

### `make verify` Fails with Ruff or Black

```bash
cd atelier
uv run ruff check --fix src tests
uv run python -m black src tests
make verify
```

### Postgres-Gated Tests Are Skipped

That is expected unless `ATELIER_DATABASE_URL` is set.

```bash
ATELIER_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/atelier \
cd atelier && uv run pytest
```
