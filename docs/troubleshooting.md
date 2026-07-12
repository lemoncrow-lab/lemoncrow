# Troubleshooting

This page starts with installed-product issues first. Source-checkout and contributor issues are lower down.

## `lc` Is Not Found After Install

**Symptom:**

```text
bash: lc: command not found
```

**Cause:** `~/.local/bin` is not on `PATH` yet.

**Fix:** add it to your shell profile and restart the shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## `lc background status` Shows Services Are Not Running

LemonCrow background services should start automatically. If they are stopped or failed:

**Fix:** Restart them using your background manager:

```bash
lc background restart
lc background status
```

If you want to inspect what the controller is doing:

```bash
lc background logs controller
```

## `lc background install` Fails on macOS

**Cause:** `launchd` requires the target directory to exist and might have permission issues.

**Fix:** Ensure `~/Library/LaunchAgents` exists and re-run:

```bash
mkdir -p ~/Library/LaunchAgents
lc background install --with-stack
```

## `lc stack start` Fails

**Common causes:** npm dependencies are not installed, `npm` is missing from `PATH`, or ports are already in use.

Check the frontend toolchain first:

```bash
npm --version
test -d frontend/node_modules || echo "frontend deps missing"
```

If ports `3125` or `8787` are already busy, inspect what owns them first:

```bash
# Check if the managed background service is already running
lc background status

# Check network ports
ss -ltnp | grep -E ':3125|:8787'
```

Then stop or reconfigure the conflicting process, or reset the LemonCrow stack:

```bash
lc background restart
```

## The UI Loads but API Calls Fail With Auth Errors

For local no-auth service usage, start the service explicitly like this:

```bash
LEMONCROW_REQUIRE_AUTH=false lc service start --host 0.0.0.0 --port 8787
```

If you want auth enabled, set `LEMONCROW_API_KEY` and configure the client that is calling the service.

## `lc mcp` Is Not Found

If you used the install script, re-run it and verify both commands:

```bash
lc --version
lc mcp --version
```

If you are on a source checkout instead of an installed setup:

```bash
cd lemoncrow
uv sync --all-extras
```

## `lc tools call context`, `rescue`, or `verify` Returns `noop`

Those tools are developer-mode surfaces.

**Cause:** `LEMONCROW_DEV_MODE=1` is not enabled in the shell or host environment.

**Fix:**

```bash
lc tools call context --dev --args '{"task":"Describe the task","domain":"coding"}' --json
```

For host integrations, set `LEMONCROW_DEV_MODE=1` in the MCP server environment if
you want active context/retrieval behavior instead of passive compatibility stubs.

If the command still behaves unexpectedly, reinitialize the store and verify the
runtime is healthy:

```bash
lc init
lc background status
lc worker list
```

## Antigravity MCP Tool Not Available

**Symptom:** LemonCrow tools do not appear in Antigravity or are unavailable from `agy`.

**Cause:** Antigravity requires absolute paths in its MCP configuration.

Use the install guide here:

- [hosts/antigravity-install.md](hosts/antigravity-install.md)

## pgvector Extension Not Available

**Symptom:**

```text
ERROR: extension "vector" is not available
```

pgvector is optional. LemonCrow works normally without it. Only enable it when you want embedding-based similarity search on Postgres.

## LemonCrow Spend Differs From External Analytics

If LemonCrow and an external analytics tool disagree on spend, check the
trace pricing source before comparing totals.

LemonCrow now prices imported sessions from persisted raw `usage_entries` on each
trace. Mixed-model sessions intentionally leave `trace.model` blank; the source
of truth is `usage_entries` and the derived `model_usages`, not a synthetic
session-level “primary model”.

If you changed importer or pricing logic, rebuild imported traces first:

```bash
lc import --force
```

If the services are already running, restart them to pick up new code or configuration:

```bash
lc background restart
lc background status
```

When comparing totals, keep these rules in mind:

- LemonCrow totals come from backend pricing helpers over `usage_entries`.
- Mixed-model sessions should be inspected via `model_usages`, not `trace.model`.
- Explicit billed tools belong in `usage_entries` with `kind: tool` and `cost_usd`.
- External tools may still disagree if they apply provider-specific synthetic
  pricing instead of raw per-model billing.

## Source Checkout and Contributor Issues

### `make verify` Fails with Ruff or Black

```bash
cd lemoncrow
uv run ruff check --fix src tests
uv run python -m black src tests
make verify
```

### Postgres-Gated Tests Are Skipped

That is expected unless `LEMONCROW_DATABASE_URL` is set.

```bash
LEMONCROW_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/lemoncrow \
cd lemoncrow && uv run pytest
```
