# Gemini CLI Integration

Atelier integrates with Gemini CLI through a formal extension that bundles MCP wiring, Atelier commands, skills, and GEMINI context. Installs are global by default; pass `--workspace DIR` to switch activation to a single workspace.

## Setup

```bash
cd atelier
uv sync --all-extras
make install
make verify
```

## Installed Artifacts

- Extension bundle: `integrations/gemini/extension/`
- Global install: `gemini extensions link integrations/gemini/extension`
- Workspace install: same linked extension, but enabled with workspace scope for the requested directory

## Notes

- Gemini loads the extension from `integrations/gemini/extension/` and invokes the installed `atelier-mcp` console script.
- Re-run `make install` if the Atelier repo path changes.

## MCP Tool Names

Canonical MCP names: `task`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`, `search`, `compact`, `atelier_repo_map`.

CLI-only workflows include `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, and `atelier proof show`.
