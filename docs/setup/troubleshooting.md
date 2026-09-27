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

## The Local Loopback Server Is Not Running

Local mode uses one server on `127.0.0.1:7420`. Verify the exact client-to-server path first:

```bash
lc mcp --host claude check --json
```

From a source checkout you can inspect or restart it directly:

```bash
bash scripts/local_server.sh status
bash scripts/local_server.sh restart
```

If port `7420` is occupied, inspect the owner before restarting:

```bash
ss -ltnp | grep ':7420'
```

A healthy local install should be running `lemoncrow_server_core`, not the retired `lemoncrow-controller`, `lemoncrow-stack`, or private `lemoncrow_server.ops` local runtime.

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

## `lemoncrow tools call context`, `rescue`, or `verify` Returns `noop`

Those tools are developer-mode surfaces.

**Cause:** `LEMONCROW_DEV_MODE=1` is not enabled in the shell or host environment.

**Fix:**

```bash
lemoncrow tools call context --dev --args '{"task":"Describe the task","domain":"coding"}' --json
```

For host integrations, set `LEMONCROW_DEV_MODE=1` in the MCP server environment if
you want active context/retrieval behavior instead of passive compatibility stubs.

If the command still behaves unexpectedly, reinitialize the store and verify the
runtime is healthy:

```bash
lc init
lc mcp --host claude check --json
lc worker list
```

## Antigravity MCP Tool Not Available

**Symptom:** LemonCrow tools do not appear in Antigravity or are unavailable from `agy`.

**Cause:** Antigravity requires absolute paths in its MCP configuration.

Use the install guide here:

- [Antigravity install guide](../hosts/antigravity-install.md)

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

After changing an installed local runtime, restart the loopback server so it picks up the new code or configuration:

```bash
bash scripts/local_server.sh restart
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
