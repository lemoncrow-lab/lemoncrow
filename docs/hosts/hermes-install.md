# Installing Atelier into Hermes Agent

**Support level**: MCP server (stdio JSON-RPM)

Hermes Agent by Nous Research is a general-purpose agent framework that uses a
YAML-based configuration file at `$HERMES_HOME/config.yaml` (defaults to
`~/.hermes/config.yaml`).

---

## Quick Install

```bash
make install
```

Hermes Agent only supports global (user-wide) configuration — there is no
project-local config concept.

```bash
bash scripts/install_hermes.sh
```

---

## What Gets Installed

| Artifact          | Global install                         |
| ----------------- | -------------------------------------- |
| MCP server config | `$HERMES_HOME/config.yaml` or `~/.hermes/config.yaml` |
| Toolset entry     | Added `mcp-atelier` to `platform_toolsets.cli` |

The installer merges an `atelier` entry into the `mcp_servers` key of
`config.yaml`:

```yaml
mcp_servers:
  atelier:
    command: atelier-mcp
    args:
      - --host
      - hermes
    timeout: 120
    connect_timeout: 60
    enabled: true

platform_toolsets:
  cli:
    - mcp-atelier
    - hermes-cli
```

The `platform_toolsets.cli` entry is essential — without it, Hermes CLI profiles
may filter out MCP-discovered tools from normal sessions even though the server
connects successfully. Keeping `mcp-atelier` first also makes the Atelier toolset
the default preference when Hermes composes CLI capabilities.

---

## Verify

```bash
make verify
```

Or manually:

```bash
atelier-mcp --host hermes --version
```

Check that the config file contains the atelier entry:

```bash
cat ~/.hermes/config.yaml
```

---

## Expected Behavior

- Hermes Agent connects to the Atelier MCP server on session start
- Atelier tools appear in Hermes' dynamic toolset under `mcp-atelier`
- Hermes agents can use `context` for pre-task reasoning, `trace` for recording,
  and `rescue` for failure recovery
- With `ATELIER_DEV_MODE=1`, all tools are fully active
- `trace` remains the stable observable recording surface

---

## Troubleshooting

| Problem                          | Fix                                                                                |
| -------------------------------- | ---------------------------------------------------------------------------------- |
| "atelier-mcp: command not found" | Run `pip install atelier-runtime` or reinstall via `make install`                  |
| Tools not showing up in Hermes   | Verify `mcp-atelier` is in `platform_toolsets.cli` — start a new Hermes session    |
| `$HERMES_HOME` not set           | Installer defaults to `~/.hermes/config.yaml`. Set `HERMES_HOME` to customize.     |
| Tools fail with "hermes" label   | Check `config.yaml` has `--host hermes` in the atelier args                        |
| MCP connection timeout           | Increase `connect_timeout` and `timeout` values in `config.yaml`                   |

---

## Uninstall

```bash
atelier uninstall
```

Or manually remove the `atelier` block from `mcp_servers` and remove
`mcp-atelier` from `platform_toolsets.cli` in `$HERMES_HOME/config.yaml`.
