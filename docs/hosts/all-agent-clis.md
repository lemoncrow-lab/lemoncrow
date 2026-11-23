# Coding-agent integrations

LemonCrow plugs Workspaces Lite into the coding agent you already use locally. LemonCrow-owned managed execution belongs to Hosted/Enterprise Workspaces.

These are different integration models and should not be described as one generic server setup.

## Main integrations

| Host | User model | Normal entry point |
| --- | --- | --- |
| **Claude Code** | Existing-agent plugin | `lc with claude` or `lc rt enable claude` |
| **Codex** | Existing-agent plugin | `lc with codex` or `lc rt enable codex` |
| **OpenCode** | Existing-agent plugin | `lc with opencode` or `lc rt enable opencode` |
| **Cursor** | Installed IDE + `cursor-agent` integration | Cursor installer; see below |
| **Pi** | Public LemonCrow-managed coding-agent host | `lc rt host ... --engine pi`; `lc with pi` |

Additional integrations exist for Copilot, Hermes Agent, Antigravity, and other integration surfaces. Their capabilities depend on what each host exposes.

## Existing-agent plugins

### Claude Code

```bash
lc with claude
```

For persistent integration:

```bash
lc rt enable claude
```

See [Claude Code integration](claude-code-install.md).

### Codex

```bash
lc with codex
```

For persistent integration:

```bash
lc rt enable codex
```

See [Codex integration](codex-install.md).

### OpenCode

```bash
lc with opencode
```

For persistent integration:

```bash
lc rt enable opencode
```

See [OpenCode integration](opencode-install.md).

### Cursor

Cursor uses an installed integration because its IDE and CLI share configuration and lifecycle hooks. It is not an `lc with` host.

From a source checkout:

```bash
bash scripts/install_cursor.sh
bash scripts/verify_cursor.sh
```

See [Cursor integration](cursor-install.md).

## LemonCrow-managed frontends

### Pi

Pi is a public coding-agent host. LemonCrow manages its pinned binary with `lc rt host install|status|update|build|remove --engine pi`, and `lc with pi` launches Pi with local Workspaces Lite tools for that process. Hosted/Enterprise may also use Pi as an implementation/frontend choice, but that does not make Pi itself private.

See [Pi in LemonCrow Workspaces](pi-install.md).

## Source-checkout install

To verify a source checkout, run `make verify-full`. Install a host integration directly:

```bash
bash scripts/install_claude.sh
bash scripts/install_codex.sh
bash scripts/install_opencode.sh
bash scripts/install_cursor.sh
```

The host installers are designed to be repeatable, preserve user configuration, support dry-run/verification paths, and skip missing hosts where appropriate.

## Other supported integrations

| Host | Integration |
| --- | --- |
| Copilot | instructions, agent/task integration, and LemonCrow tools |
| Hermes Agent | host configuration and LemonCrow tool integration |
| Antigravity | generated host guidance and LemonCrow tool integration |

See the [host capability matrix](host-capability-matrix.md) for the technical differences.

## Protocol details

Most users should think of these as LemonCrow plugins/integrations. Integration authors who need the underlying protocol, tool registration, or transport details can use the [protocol / MCP reference](../sdk/mcp.md).
