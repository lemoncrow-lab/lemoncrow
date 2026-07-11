# LemonCrow Harbor Eval

Run LemonCrow on official [Harbor](https://harborframework.com) benchmark datasets
(including terminal-bench-core) for cost-quality comparison.

## Prerequisites

- **Docker** installed and running
- **Harbor**: `pip install harbor` or `uv add harbor --project benchmarks`
- API credentials in environment (see below)

## Quick start

```bash
# The default arm (Claude Code CLI + LemonCrow plugin) reads OAuth tokens
# from benchmarks/harbor/.env (CLAUDE_CODE_OAUTH_TOKEN_1/_2).

# Quick smoke test (3 tasks, 1 attempt):
lemon benchmark harbor --limit 3 --attempts 1 -y

# A/B comparison (LemonCrow augmentation on vs off):
lemon benchmark harbor -y
lemon benchmark harbor --baseline -y

# With Bedrock credentials:
export AWS_BEARER_TOKEN_BEDROCK=...
export AWS_REGION=us-east-1
lemon benchmark harbor --agent lemoncrow-bedrock --limit 5 -y
```

## Command reference

```
lemon benchmark harbor [OPTIONS]

Options:
  -d, --dataset TEXT        Harbor dataset (default: terminal-bench/terminal-bench-2-1)
  --limit INTEGER           Max tasks to run (default: all)
  --agent TEXT              Agent arm: lemoncrow | lemoncrow-bedrock | lemoncrow-claude-code (default)
  --baseline                Run baseline arm (bench_mode=off, no plugin)
  --model TEXT              Model override (default: LEMONCROW_BENCH_MODEL)
  -n, --attempts INTEGER    Attempts per task for pass@k scoring (default: 5)
  -c, --concurrent INTEGER  Max concurrent trials (default: slots x tokens)
  --resume TEXT             Resume an existing job dir
  -o, --output TEXT         Output directory for results
  -y, --yes                 Skip confirmation prompt
```

## Agent arms

| Arm | Description |
|-----|-------------|
| `lemoncrow-claude-code` | Claude Code CLI + LemonCrow plugin (default) |
| `lemon` | Direct API with LemonCrow augmentation |
| `lemoncrow-bedrock` | LemonCrow via AWS Bedrock |
| `--baseline` flag | `bench_mode=off` — baseline without the LemonCrow plugin |

The two-arm comparison proves LemonCrow's value-add over a clean baseline.

## Custom Harbor agent

The adapter is at `benchmarks/harbor/lemoncrow_agent.py`. Run it directly:

```bash
harbor run -d "terminal-bench/terminal-bench-core@0.1.1" \
    --agent benchmarks.harbor.lemoncrow_agent:LemonCrowHarborAgent \
    --limit 5
```

## Relationship to mini eval

| Command | Purpose |
|---------|---------|
| `lemon benchmark mini --dry-run` | Offline schema validation, no Docker needed |
| `lemon benchmark mini --limit 5` | Live local repo tasks, cheap, no Docker |
| `lemon benchmark harbor --limit 5` | Official Harbor datasets in Docker containers |
| `make proof-cost-quality` | Deterministic proof gate (zero live calls) |

Start with `lemon benchmark mini --dry-run` to verify setup, then escalate to
`lemon benchmark harbor` for credible published results.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN_1/_2` | For default arm | Claude Code OAuth tokens, in `benchmarks/harbor/.env` |
| `ANTHROPIC_API_KEY` | For `lemon` arm | Anthropic API key |
| `AWS_BEARER_TOKEN_BEDROCK` | For `lemoncrow-bedrock` arm | Bedrock bearer token |
| `AWS_REGION` | For `lemoncrow-bedrock` arm | AWS region |
| `LEMONCROW_BENCH_VERSION` | No | LemonCrow version to install (default: latest) |
| `LEMONCROW_BENCH_MODEL` | No | Model override for the run |
| `LEMONCROW_BENCH_RTK_VERSION` | No | Pin the [rtk](https://github.com/rtk-ai/rtk) external-compactor binary version (default: latest) |

## Token compaction (rtk)

Container setup best-effort installs [rtk](https://github.com/rtk-ai/rtk), a
binary LemonCrow's bash tool soft-detects on PATH (see `external_compactors.py`)
and uses to compress git/gh/pytest/lint/etc. output before it reaches the
model, cutting benchmark token cost for free. It is purely additive: a slow or
failed download never fails the trial, it just leaves the run on the plain
shell path (same fallback `lemon doctor` reports for a local dev machine
without rtk installed).
