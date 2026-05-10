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

## `atelier servicectl status` Shows Not Running

**Fix:** start it again manually:

```bash
atelier servicectl start
atelier servicectl status
```

If you want to inspect what it is doing:

```bash
atelier servicectl logs
```

## `atelier stack start` Fails

**Common causes:** Docker or Docker Compose is not installed, not running, or the ports are already in use.

Check Docker first:

```bash
docker --version
docker compose version
```

If ports `3125` or `8787` are already busy:

```bash
lsof -ti :3125 :8787 | xargs kill -9 2>/dev/null || true
atelier stack start
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

## `atelier lint` or `atelier reasoning` Looks Empty or Too Permissive

**Cause:** the store was not initialized or the seeded blocks are missing.

**Fix:**

```bash
atelier init
```

Then verify the store exists and background processing is alive:

```bash
atelier servicectl status
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
