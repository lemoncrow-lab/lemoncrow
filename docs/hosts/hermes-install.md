# Hermes Agent integration

Hermes Agent can use LemonCrow through its YAML-configured external-tool integration.

Hermes only supports user-wide configuration; there is no project-local Hermes config mode.

## Source-checkout install

```bash
bash scripts/install_hermes.sh
```

The installer updates `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml`) with the LemonCrow tool connection and adds the LemonCrow toolset to Hermes' CLI toolset list.

## Verify

```bash
make verify-full
lc rt status
```

You can also inspect the Hermes config directly:

```bash
cat ~/.hermes/config.yaml
```

## Expected behavior

- LemonCrow tools appear in Hermes' external toolset.
- Hermes can use LemonCrow repository/context tools according to the capabilities exposed by the installed Workspaces client.
- Evidence depth is lower than hosts with richer native lifecycle hooks.

## Implementation/debug details

Hermes stores external tools under an `mcp_servers` configuration key. Current LemonCrow installer entries invoke:

```text
lc runtime mcp --host hermes
```

The `mcp-lemoncrow` entry in `platform_toolsets.cli` must remain enabled or Hermes can filter the tools out of normal sessions.

## Uninstall

```bash
lc uninstall runtime
```

Or remove the LemonCrow entries from the Hermes YAML configuration if you only want to detach that host.
