# Host capability matrix

This page explains the technical differences between LemonCrow's coding-agent integrations. User-facing docs call these **plugins/integrations**; the underlying protocol details only matter when implementing or debugging a host adapter.

## Main product integrations

| Host | User-facing model | Temporary launch | Persistent integration | Lifecycle/evidence | Owned-loop frontend |
| --- | --- | --- | --- | --- | --- |
| **Claude Code** | LemonCrow plugin inside existing Claude Code | `lc with claude` | `lc rt enable claude` | Rich plugin hooks + importable session artifacts | Local Workspaces Lite frontend |
| **Codex** | LemonCrow plugin inside existing Codex | `lc with codex` | `lc rt enable codex` | Native lifecycle hooks + session imports | Local Workspaces Lite frontend |
| **OpenCode** | LemonCrow plugin inside existing OpenCode | `lc with opencode` | `lc rt enable opencode` | Local plugin events + session DB import | No dedicated owned-loop engine |
| **Cursor** | Installed IDE + `cursor-agent` integration | — | Cursor installer | `sessionStart` + `stop` hooks; Cursor IDE/CLI share config | No |
| **Pi** | Public managed coding-agent host | managed binary | `lc rt host ... --engine pi`; `lc with pi` | Workspaces Lite tools are added locally for the Pi process | May also be used by Hosted/Enterprise |

## Additional integrations

| Host | Integration depth | Notes |
| --- | --- | --- |
| Copilot | Instructions, tools, agent/tasks, session evidence where available | Host lifecycle APIs are more limited than Claude/Codex |
| Hermes Agent | Configured LemonCrow tool integration | No deep native hook bus |
| Antigravity | Generated guidance + LemonCrow integration | Host-specific lifecycle support is limited |

## Evidence confidence

Recorded evidence depends on what the host exposes. Internal trace records may use confidence/source labels such as:

- `full_live` — strong live lifecycle + tool evidence;
- `wrapper_live` — LemonCrow owns the loop and can record its execution boundary;
- `mcp_live` — protocol/tool traffic is visible but native host lifecycle may be incomplete;
- `imported` — reconstructed from host session artifacts;
- `manual` — only explicitly recorded evidence is available.

These are evidence-quality labels, not product tiers.

The recorded trace schema uses the fields `host`, `trace_confidence`, `capture_sources`, and `missing_surfaces`. Host integrations should only claim a stronger `trace_confidence` when the corresponding `capture_sources` actually exist; unavailable host surfaces belong in `missing_surfaces` rather than being inferred.

## Important host differences

### Claude Code

Claude exposes a rich plugin surface. LemonCrow can combine plugin hooks, repository tools, and imported session artifacts.

### Codex

Codex exposes lifecycle hooks and session artifacts. Hook trust/configuration can affect how much live evidence LemonCrow sees.

### OpenCode

OpenCode supplies a local plugin/event surface and a local session database. Its lifecycle API differs from Codex, so not every hook-oriented feature has a direct equivalent.

### Cursor

Cursor's IDE and `cursor-agent` CLI share the same integration files. Cursor retains its own built-in tools, and privacy/ghost-mode behavior can limit provider-usage attribution. Do not describe Cursor savings as identical to the Claude/Codex benchmark path.

### Pi

Pi itself is public. `lc rt host` manages the reviewed/pinned binary. When LemonCrow uses Pi inside the private Hosted owned loop, a separate managed mode can disable Pi features that would create a second competing tool/model loop; that private use does not change Pi host ownership.

## Protocol details

Integration authors can inspect the underlying protocol and tool surface in [Protocol / MCP reference](../sdk/mcp.md). Normal product setup should use the host-specific plugin/integration docs instead.
