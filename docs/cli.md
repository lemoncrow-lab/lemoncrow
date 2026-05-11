# Atelier CLI Reference

Full command reference for the `atelier` command-line interface.

## Global Options

| Flag         | Description                     |
| ------------ | ------------------------------- |
| `--help`     | Show help message and exit      |
| `--version`  | Show version and exit           |
| `--verbose`  | Enable verbose/debug logging    |
| `--root DIR` | Override Atelier root directory |

## Commands

### `atelier status`

Show Atelier runtime status including storage, active agents, and pack inventory.

```bash
atelier status
atelier status --json
```

### `atelier context`

Retrieve and display cached reasoning context for the current workspace.

```bash
atelier context
atelier context --workspace /path/to/project
```

### `atelier pack`

Manage Atelier packs.

```bash
atelier pack list                        # List installed packs
atelier pack install <pack-id>           # Install a pack
atelier pack uninstall <pack-id>         # Remove a pack
atelier pack info <pack-id>              # Show pack details
atelier pack search <query>              # Search available packs
atelier pack benchmark                   # Run pack benchmark suite
```

### `atelier run`

Run an atelier workflow or benchmark.

```bash
atelier run <workflow-id>
atelier run --pack <pack-id> <workflow>
```

### `atelier evals`

Execute eval scenarios from installed packs.

```bash
atelier evals                            # Run all evals
atelier evals <pack-id>                  # Run evals for a pack
atelier evals --scenario <name>          # Run a named scenario
```

### `atelier settings`

View or update Atelier configuration.

```bash
atelier settings                         # Show current settings
atelier settings set <key> <value>       # Update a setting
```

### `atelier savings`

Report token and cost savings from Atelier's context caching.

```bash
atelier savings
atelier savings --since 7d
atelier savings --json
```

## Exit Codes

| Code | Meaning             |
| ---- | ------------------- |
| `0`  | Success             |
| `1`  | General error       |
| `2`  | Configuration error |
| `3`  | Storage error       |
| `4`  | Pack not found      |

## Trace Schema

Atelier traces are stored as JSONL files in `<atelier-root>/traces/`. Each line is a JSON object with the following fields:

| Field        | Type   | Description              |
| ------------ | ------ | ------------------------ |
| `ts`         | float  | Unix timestamp           |
| `session_id` | string | Unique run identifier    |
| `agent`      | string | Agent name               |
| `action`     | string | Action type              |
| `args_sig`   | string | SHA1 prefix of arguments |
| `tokens_in`  | int    | Input tokens             |
| `tokens_out` | int    | Output tokens            |
| `latency_ms` | float  | Latency in milliseconds  |

## Environment Variables

| Variable                  | Default      | Description                                              |
| ------------------------- | ------------ | -------------------------------------------------------- |
| `ATELIER_ROOT`            | `~/.atelier` | Root directory for all Atelier data                      |
| `ATELIER_STORAGE_BACKEND` | `sqlite`     | Storage backend (`sqlite` or `postgres`)                 |
| `ATELIER_DB_URL`          | —            | Postgres connection string (when using postgres backend) |
| `ATELIER_API_KEY`         | —            | API key for the Atelier service (optional)               |
| `ATELIER_LOG_LEVEL`       | `INFO`       | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)          |
