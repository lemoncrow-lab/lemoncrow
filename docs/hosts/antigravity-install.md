# Antigravity integration

LemonCrow integrates with Antigravity through its external-tool configuration plus generated host guidance. The companion `agy` CLI can use the same workspace integration.

## Source-checkout install

```bash
bash scripts/install_antigravity.sh
```

For one workspace:

```bash
bash scripts/install_antigravity.sh --workspace /path/to/repo
```

The installer can write:

- Antigravity's external-tool configuration;
- workspace `AGENTS.md` guidance;
- LemonCrow host/plugin metadata and shared skills used by `agy`.

## Verify

```bash
make verify-full
lc rt status
```

Open the workspace in Antigravity or launch `agy` from the repository and confirm LemonCrow tools are available.

## Evidence and limits

Antigravity does not expose the same deep lifecycle hook surface as Claude Code or Codex, so LemonCrow relies more heavily on tool traffic, generated guidance, and explicit recorded evidence.

## Implementation/debug details

Antigravity stores its external-tool connection in an MCP-shaped configuration file. Current installer entries invoke the Workspaces protocol adapter under:

```text
lc runtime mcp --host antigravity
```

That protocol layer is an implementation detail; normal users should treat LemonCrow as an Antigravity integration rather than a service they operate.

## Uninstall

```bash
bash scripts/uninstall_antigravity.sh
bash scripts/uninstall_antigravity.sh --workspace /path/to/repo
```
