# opencode Integration

Support level: **MCP config** — MCP server registration in `opencode.json`.

## What gets installed

| Component  | Location after install                    | Description                             |
| ---------- | ----------------------------------------- | --------------------------------------- |
| MCP server | Merged into global or workspace `opencode.json` | Wired to `scripts/atelier_mcp_stdio.sh` |

By default the installer writes `~/.config/opencode/opencode.json`. With
`--workspace DIR`, it writes `<workspace>/opencode.json` and installs the agent
profile in `<workspace>/.opencode/agents/`. The MCP wrapper defaults to the
local Atelier HTTP service at `http://127.0.0.1:8787`.

`read` and `search` are default-on when the Atelier
agent profile is installed. They augment repeated reads/searches but do not
replace opencode-native file reads, shell search, `rg`, or `grep`. Set
`ATELIER_CACHE_DISABLED=1` to bypass Atelier caching.

## Install

```bash
make install
```

## Verify

```bash
make verify
```

## Source

Config source: `atelier/opencode/`
Full guide: `docs/hosts/opencode-install.md`
