# Gemini CLI Example

## Install

```bash
cd atelier
uv sync --all-extras
make install
```

## Config

Configure Gemini CLI to launch `atelier-mcp` and pass `ATELIER_ROOT=.atelier`.

## Commands

```bash
ATELIER_DEV_MODE=1 atelier context --task "Repair failed crawl" --domain Agent.crawl
atelier trace record --input trace.json
```

## Benchmark

```bash
atelier benchmark export --input .atelier/benchmarks/runtime/latest.json --output benchmark.md --format markdown
```

## Troubleshooting

- If Gemini reads stale data, clear or re-seed `.atelier`.
- If trace ingestion fails, validate the JSON payload shape first.
