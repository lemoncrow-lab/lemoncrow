<!-- cspell:ignore Alamofire Excalidraw ast-grep codegraph ctags django jcodemunch nohit okhttp scip serena tokio vscode zoekt beasm Trendshift telegraphese -->

<div align="center">

# Atelier Runtime

### Honestly, get 30% more out of your Claude subscription.

Atelier is a 30-second install that helps Claude Code waste fewer tokens while you work. Keep using Claude Code normally; Atelier sits underneath it and gives the agent better search, shorter file reads, compact command output, reusable memory, and a live savings meter.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue?style=flat-square)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/atelier-ws/atelier?style=flat-square)](https://github.com/atelier-ws/atelier/releases)
[![Stars](https://img.shields.io/github/stars/atelier-ws/atelier?style=flat-square)](https://github.com/atelier-ws/atelier)

**Live savings across Atelier sessions**

[![Cost saved](https://img.shields.io/endpoint?url=https%3A%2F%2Fatelier.ws%2Fapi%2Fbadge%3Fmetric%3Dsavings&style=for-the-badge&color=04ba0d)](https://atelier.ws)
[![Tokens less](https://img.shields.io/endpoint?url=https%3A%2F%2Fatelier.ws%2Fapi%2Fbadge%3Fmetric%3Dtokens&style=for-the-badge&color=7904b8)](https://atelier.ws)
[![Calls avoided](https://img.shields.io/endpoint?url=https%3A%2F%2Fatelier.ws%2Fapi%2Fbadge%3Fmetric%3Dcalls&style=for-the-badge&color=eae4ed)](https://atelier.ws)

[Install](#install-in-30-seconds) · [Check your savings first](#check-your-own-savings) · [Why trust the numbers?](#why-trust-the-numbers) · [Results](#results) · [Pricing](#pricing)

</div>

---

## Install in 30 seconds

Run this once:

```bash
curl -fsSL https://install.atelier.ws | bash
```

Then turn it on inside the project where you use Claude Code:

```bash
cd your-project
atelier init
```

Open Claude Code like you normally do. Atelier wires in better tools behind the scenes and starts tracking savings as sessions finish.

Already installed?

```bash
atelier update
```

Check that everything is connected:

```bash
atelier doctor
```

## What changes for you

Atelier does not ask you to learn a new coding app. It improves the work Claude Code already does:

| Before                                                       | With Atelier                                                                   |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Claude reads broad files and long terminal output.           | Claude gets the exact code ranges and compact results it needs.                |
| The same context gets rediscovered again and again.          | Useful session context can be reused.                                          |
| You pay for long explanations inside the working transcript. | Outputs stay shorter while code, commands, filenames, and errors remain exact. |
| Savings are hard to see.                                     | A local meter shows tokens, cost, and savings adding up.                       |

The point is simple: more of your Claude subscription should go into useful work, not repeated setup and paid noise.

## Check your own savings

Do not take our 30% claim on faith. Before installing, you can scan your own local agent history:

```bash
curl -fsSL https://savings.atelier.ws | bash
```

What it does:

- Reads local Claude/Codex agent session files.
- Estimates where Atelier would have used fewer tokens or cheaper tool calls.
- Prints savings from your own history.
- Uses a temporary local store.
- Needs no Atelier account and no provider API keys.

Useful variants:

```bash
curl -fsSL https://savings.atelier.ws | bash -s -- --since 30d --top 10
curl -fsSL https://savings.atelier.ws | bash -s -- --host codex --limit 20
```

## Why trust the numbers?

You are right to be skeptical. A live badge alone proves very little because anyone can fake a counter.

Atelier uses four checks instead:

1. **Raw benchmark receipts:** headline numbers link to committed per-task runs, costs, turn counts, setup notes, and reproduction commands.
2. **Your own scan:** the savings command checks your machine, not our marketing page.
3. **Labeled live badges:** live counters show aggregate usage adding up; they are not used as the source of the 30% benchmark claim.
4. **Rows where Atelier does not win:** Terminal-Bench 2.1 is flat on accuracy (-0.2pp) and only cheaper on cost. It stays in the results table.

The trust is the audit trail, not the animation.

## Results

Measured on the same model, same tasks, and same environment:

| Benchmark                                           | Baseline correct | Atelier correct | Correct delta |        Baseline cost |        Atelier cost | Cost delta |
| ----------------------------------------------------- | -----------------: | ----------------: | --------------: | ---------------------: | --------------------: | -----------: |
| SWE-bench Verified, 50 tasks x 5 reps | 80.8% | **92.8%** | **+12.0 pp** | $234.84 | **$165.45** | **29.5% cheaper** |
| SWE-bench Lite, 10 tasks x 3 reps | 93.3% | **100%** | **+6.7 pp** | $12.38 | **$10.79** | **12.9% cheaper** |
| SWE-bench Pro, 10 tasks x 5 reps | 88.0% | **90.0%** | **+2.0 pp** | $39.01 | **$30.61** | **21.5% cheaper** |
| Exploration tasks across 7 large repos x 5 reps | - | - | - | $19.11 | **$6.29** | **67% cheaper** |
| Telegraphic Q&A, 20 prompts x 5 reps | - | - | - | $8.93 | **$6.81** | **23.7% cheaper** |
| Terminal-Bench 2.1, 89 tasks vs public leaderboard* | 78.9% expected | 78.7% | -0.2 pp | $96.76 | **$69.52**† | **28.1% cheaper**† |

<sub>* Atelier: 1 rep/task. Baseline: public tbench.ai leaderboard, 5-rep average per task. † Other 5 tasks in Atelier timeout and cannot capture cost; see [BENCHMARKS.md](BENCHMARKS.md).</sub>

SWE-bench Verified detail:

| Metric        | Baseline | Atelier |            Delta |
| --------------- | ---------: | --------: | -----------------: |
| Turns         |    6,962 |   4,336 |  **37.7% fewer** |
| Output tokens |    3.04M |   2.19M |  **27.9% fewer** |
| Wall-clock    |    14.3h |   10.9h | **23.7% faster** |

Raw runs, setup notes, and reproduction commands live in [BENCHMARKS.md](BENCHMARKS.md) and [`benchmarks/codebench/results/`](benchmarks/codebench/results/).

## Code search vs 10 named tools

Retrieval quality (MRR) across ~7,200 query/gold pairs on 14 repos -- ripgrep, ast-grep, universal-ctags, Serena, CodeGraph, cocoindex-code, codebase-memory-mcp, fff-mcp, code-index-mcp, and jCodeMunch all scored on the identical corpus:

| Provider | MRR | rec@1 | p95 |
| --- | ---: | ---: | ---: |
| **Atelier +semantic (BGE)** | **0.727** | **0.650** | 390ms |
| Atelier lexical (default) | 0.676 | 0.582 | 134ms |
| cocoindex-code (best rival) | 0.557 | 0.457 | 595ms |
| serena | 0.401 | 0.359 | 3834ms |
| ripgrep | 0.376 | 0.320 | 66ms |
| universal-ctags (worst rival) | 0.237 | 0.226 | 1ms |

None of these 10 tools had ever been scored against each other, on a shared query set, before this table existed -- most publish a number about themselves, on their own terms, against their own baseline (grep, file-by-file reading), never against a rival. Full 13-row table and a per-tool "what they claim vs. what it scored" breakdown, with sources: [atelier.ws/vs](https://atelier.ws/vs) and [docs.atelier.ws/benchmarks/results](https://docs.atelier.ws/benchmarks/results).

## Why it works

Claude is strong, but the work around Claude is often wasteful. Atelier reduces that waste.

- **Better inputs:** the agent gets relevant symbols and exact file ranges instead of whole files.
- **Better outputs:** command output and replies stay compact without losing exact technical facts.
- **Better memory:** useful context can be reused instead of rediscovered.
- **Better guardrails:** tools and hooks reduce risky edits, oversized reads, and unverified "done" states.

Atelier does not make Claude a different model. It makes the loop around Claude cleaner, which is why the same model solved more tasks in the benchmark.

## What you get

- Works with Claude Code, Codex, Cursor, opencode, and other coding agents that use MCP-style tools.
- Runs locally by default.
- Apache-2.0 open source runtime.
- No account needed to start.
- Live local stats for cost, tokens, and savings.
- Optional paid features for heavy users and teams.

## Pricing

| Plan | Price | Best for |
| --- | ---: | --- |
| Free | $0 | Local runtime, Claude Code setup, better tools, skills, and headline savings estimates. |
| Pro | $19/mo | Heavy users who want large-repo search, session recall, full savings breakdown, model routing, and swarm workflows. |
| Enterprise | Contact us | Teams that need shared context, governance, SSO, retention, audit, and very large repos. |

Full breakdown: [docs/pricing.md](docs/pricing.md).

## Learn more

- [Installation](docs/installation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Benchmarks](BENCHMARKS.md) · [full results, backed by docs](docs/benchmarks/results.md) · [every "vs" comparison, with sources](https://atelier.ws/vs)
- [CLI reference](docs/cli.md)
- [Architecture](docs/architecture.md)

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
