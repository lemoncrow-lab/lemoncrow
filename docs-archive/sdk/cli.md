# CLI Surface for SDK Users

The canonical CLI reference lives at [../cli.md](../cli.md). This page keeps the
SDK-oriented summary small and current.

## Current Rule of Thumb

- Use [../cli.md](../cli.md) for the full command map.
- Use the Python SDK when you want in-process integration.
- Use the CLI when you want initialization, service control, traces, benchmarks,
  or source-checkout operational workflows.

## Runtime Modes

Standard commands are available in normal mode.

Developer-mode commands require `ATELIER_DEV_MODE=1`:

- `atelier context`
- `atelier search`
- `atelier rescue`
- `atelier verify`
- `atelier memory`
- `atelier read`
- `atelier edit`
- `atelier route`

Older docs may refer to `atelier lint`, `atelier check-plan`, `atelier task`, or
`atelier pack`. Those are not the current public CLI surface.

## Common Commands

Initialize a clean store:

```bash
atelier init
```

Fetch task context in developer mode:

```bash
ATELIER_DEV_MODE=1 atelier context \
    --task "Fix generated output that drifts back after refresh" \
    --domain source.truth \
    --file src/content/generate.py
```

Request a rescue suggestion in developer mode:

```bash
ATELIER_DEV_MODE=1 atelier rescue \
    --task "Apply a live config update" \
    --domain state.change \
    --error "known dead end triggered during apply"
```

Run a rubric gate in developer mode:

```bash
echo '{
    "canonical_identifier_used": true,
    "pre_change_state_captured": true,
    "read_after_write_completed": true,
    "observed_state_matches_intent": true
}' | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety
```

Record and inspect traces:

```bash
echo '{
    "agent": "sdk",
    "domain": "state.change",
    "task": "Apply a live state change",
    "status": "success"
}' | atelier trace record

atelier trace list --json
atelier trace show <trace-id>
```

Run service and background operations:

```bash
atelier service start --host 127.0.0.1 --port 8787
atelier service config
atelier servicectl status
atelier worker list
```

Run benchmark and domain inspection flows:

```bash
atelier benchmark full --json
atelier benchmark runtime --json
atelier domain list
atelier domain info <bundle-id>
```

## JSON Output

Many commands accept `--json`, but support is command-specific. Prefer
`atelier help <command path>` for the exact flags on the build you are running.

## Related References

- [../cli.md](../cli.md)
- [mcp.md](mcp.md)
- [python.md](python.md)
- [../engineering/service.md](../engineering/service.md)
