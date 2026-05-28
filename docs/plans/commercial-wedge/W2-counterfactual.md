# W2 — Per-session counterfactual report

> Parent: [`index.md`](index.md). Driving spec: [`day30/07-counterfactual-report.md`](../../../specs/day30/07-counterfactual-report.md).
> Independent of W0/W1 (does not touch vendor files).

## Goal

Take a completed session — the existing `session_report.py` already knows
its tools, turns, tokens, and actual cost — and produce a **per-vendor
cost-and-mix counterfactual** the user can read in 5 seconds:

> "On this session you spent $7.45 on Sonnet. Gemini Flash would have been
> $0.40. A smart mix would have been $4.10."

The numbers come from a local price + capability table replayed against the
session's turn ledger. **No live API calls to other vendors.** That keeps
the cost of generating a report effectively free and avoids surprise spend.

## Why now

- It's the single most viral demo: one CLI command, no signup, no infra.
- It is the precondition for W3 (cross-vendor live routing) — the user has
  to *want* cross-vendor before they consent to a router making the
  decision in real time.
- Closes the gap the index doc flagged: heuristic weekly opportunities
  exist (`insights.py:118`); per-session, per-vendor breakdown does not.

## Where the code lives

```
src/atelier/core/capabilities/counterfactual/
  __init__.py                NEW package
  pricing.py                 NEW — vendor price tables + version stamping
  capabilities.py            NEW — per-model "can this model do this tool?"
  replay.py                  NEW — applies pricing + capability to a session
  recommender.py             NEW — produces the "smart mix" recommendation
src/atelier/gateway/adapters/cli.py
                             EXTEND — `session counterfactual <id>`
src/atelier/core/service/api.py
                             EXTEND — GET /sessions/<id>/counterfactual
tests/core/capabilities/counterfactual/
  test_pricing_table_versioning.py
  test_replay_against_fixture_session.py
  test_recommender_picks_safe_mix.py
```

## Inputs (already exist — do not duplicate)

- `run_ledger.py` / `session_report.py` — turn ledger with tool, tokens-in,
  tokens-out, latency, actual vendor + model.
- `cost_tracker.py` — actual spend per session.

W2 reads these; it does not store anything that those modules already store.

## Pricing table (`pricing.py`)

```yaml
# bundled YAML, shipped in-repo, version-stamped
version: 2026-05-18
vendors:
  anthropic:
    claude-haiku-4.6:    {input_per_mtok: 0.80,  output_per_mtok: 4.00}
    claude-sonnet-4.6:   {input_per_mtok: 3.00,  output_per_mtok: 15.00}
    claude-opus-4.6:     {input_per_mtok: 15.00, output_per_mtok: 75.00}
  openai:
    gpt-4o-mini:         ...
    gpt-4o:              ...
  google:
    gemini-flash:        ...
    gemini-pro:          ...
```

The pricing file is **versioned and visible** in every counterfactual
output: `pricing_table: 2026-05-18`. Users can pin an older version with
`--pricing-version` for reproducibility.

## Capability table (`capabilities.py`)

A small per-model matrix:
- max context window
- tool-use support (yes/no/partial)
- structured-output support
- preferred phases (read / edit / agent — heuristic, not enforced)

When a turn's tool needs a capability a candidate model lacks, the
counterfactual marks that turn `infeasible` rather than silently dropping
it. The summary shows "Gemini Flash would have been $0.40, but 3 turns
needed tool-use it doesn't support."

## The "smart mix" recommendation (`recommender.py`)

- Per-turn: pick the cheapest model that has every required capability
  and whose risk class fits the turn (edits and agent turns stay on a
  high-tier model unless the user opts into edit-class downgrade).
- Output a mix and its total, side-by-side with the actual.
- Always print the **risk class** of the mix (low / medium / high) so the
  saving is interpretable.

## User-visible CLI

```
$ atelier session counterfactual 7c2f8a
Session 7c2f8a · 4h 12m · 92 turns
Actual: $7.45 on Anthropic Claude Sonnet 4.6
Pricing table: 2026-05-18

Anthropic Claude (haiku)  $1.20   -$6.25 (-84%)
Anthropic Claude (sonnet) $7.45    $0.00
Anthropic Claude (opus)  $24.10  +$16.65
OpenAI GPT-4o-mini        $0.85   -$6.60 (-89%)
OpenAI GPT-4o             $5.60   -$1.85
Google Gemini Flash       $0.40   -$7.05 (3 turns infeasible)
Google Gemini Pro         $4.20   -$3.25

Smart mix (low risk):     $4.10   -$3.35 (-45%)
  Sonnet  for 64 edit/agent turns
  Flash   for 28 read/grep turns
```

## Validation

- `test_pricing_table_versioning` — the in-repo YAML is loaded with its
  version; an unknown version raises a clear error.
- `test_replay_against_fixture_session` — fixture session with a known
  ledger produces the documented per-vendor totals to 4 decimal places.
- `test_infeasible_turns_are_flagged_not_silently_zeroed` — a tool the
  candidate model doesn't support shows in the report as infeasible count.
- `test_recommender_picks_safe_mix_for_edit_heavy_session` — edit turns
  stay on the high-tier model unless `--allow-edit-downgrade`.
- `test_cli_output_includes_pricing_version` — golden output snapshot.
- `test_api_route_returns_counterfactual_json` — `/sessions/<id>/counterfactual`.

## Exit criteria

- Pricing + capability tables shipped, versioned, documented.
- CLI command produces a deterministic report for a fixture session.
- API route returns the same data as JSON.
- No vendor APIs are called during a counterfactual run.
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **True replay against live vendor APIs.** Doubles spend per session and
  changes nothing about the headline number for 99% of users. Revisit only
  if W3 has shipped and a user explicitly opts in.
- **Live routing decisions.** Counterfactual is post-hoc reporting; W3
  ships the in-session router.
- **Pricing auto-refresh from the network.** Bundled YAML only; updates
  ride normal release cycles.

## Open questions

1. **Risk class definition.** Three buckets (low/medium/high) — what's the
   exact rule? Default: **edits + agent turns on the actual session's
   high-tier model = low; allow one tier down per turn = medium; allow any
   model that has the capability = high**. Tunable later.
2. **Pricing freshness.** How often do we cut a new bundled version?
   Default: **every minor release, plus on a vendor price-change advisory**.
3. **Where do third-party / fine-tuned models fit?** Out of scope for v1;
   the table only ships frontier models. Users can `--pricing-file` override
   for custom models without modifying the bundled table.
