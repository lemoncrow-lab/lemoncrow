# Shopify Domain Example

## Install

```bash
cd atelier
uv sync --all-extras
atelier init
```

## Config

Use the Shopify publish reasonblocks, rubrics, and traces under the `Agent.shopify.publish` domain.

## Commands

```bash
ATELIER_DEV_MODE=1 atelier context --task "Publish Shopify product" --domain Agent.shopify.publish
ATELIER_DEV_MODE=1 atelier rescue --task "Publish Shopify product" --domain Agent.shopify.publish --error "Parsed product handle from PDP URL only"
```

## Benchmark

```bash
atelier benchmark run --prompt "Publish Shopify product safely" --json
```

## Troubleshooting

- If active context commands return `noop`, enable `ATELIER_DEV_MODE=1` in the shell or MCP environment.
- Use trace recording after every failed publish flow so rescue procedures can stabilize.
