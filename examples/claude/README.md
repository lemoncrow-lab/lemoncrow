# Claude Code Example

## Install

```bash
cd atelier
uv sync --all-extras
atelier init
```

## Config

Point Claude Code at `atelier-mcp` and set `ATELIER_ROOT=.atelier`.

## Commands

```bash
ATELIER_DEV_MODE=1 atelier context --task "Fix Shopify publish" --domain Agent.shopify.publish
ATELIER_DEV_MODE=1 atelier rescue --task "Fix Shopify publish" --domain Agent.shopify.publish --error "Parsed product handle from PDP URL only"
```

## Benchmark

```bash
atelier benchmark run --prompt "Fix Shopify publish" --json
```

## Troubleshooting

- If the server is not visible, verify the MCP command uses `atelier-mcp` from the repo root.
- If active context commands return `noop`, enable `ATELIER_DEV_MODE=1` in the shell or MCP environment.
