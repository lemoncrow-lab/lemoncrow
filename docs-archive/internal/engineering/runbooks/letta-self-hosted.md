# Letta Self-Hosted Runbook

Atelier V3 supports a single-primary memory backend. SQLite remains the default. Letta can be selected explicitly for deployments that want a self-hosted memory sidecar.

## Start

```bash
atelier letta up
atelier letta status
```

The compose stack is defined in `deploy/letta/docker-compose.yml` and exposes Letta at `http://localhost:8283` by default.

## Configure Atelier

Use one of these selectors:

```bash
export ATELIER_MEMORY_BACKEND=letta
export ATELIER_LETTA_URL=http://localhost:8283
```

or `.atelier/config.toml`:

```toml
[memory]
backend = "letta"

[letta]
url = "http://localhost:8283"
```

Atelier must not dual-write memory blocks/passages into SQLite while Letta is primary. The local SQLite store may still retain trace and recall audit rows.

## Operate

```bash
atelier letta logs
atelier letta down
```

`down` preserves the named Docker volume. To remove the sidecar data volume:

```bash
atelier letta reset --yes
```

Use reset only for local development or disposable test environments.

## Troubleshooting

- `atelier letta status` checks `/v1/health` using `ATELIER_LETTA_URL` when set.
- If Docker Compose is unavailable, keep `ATELIER_MEMORY_BACKEND=sqlite`.
- If the Letta service is unhealthy, Atelier should fail visibly rather than silently falling back to SQLite when the backend is explicitly set to `letta`.
