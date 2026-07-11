# `atelier session` trust review — 2026-07-11

Full investigation of the `session` group (list / report / stats / replay / recall): correctness of every displayed number, plumbing across hosts, terminal + HTML UX. Two adversarial audit agents + empirical ground-truthing; three fix agents. All fixes verified.

## Confirmed correctness defects — all FIXED

### Replay / savings-engine math
| # | Defect | Fix |
|---|--------|-----|
| D1 | `read_transcript_stats` kept the FIRST streamed usage row per message id — output tokens undercounted ~6x (110.5k vs 635k ground truth on 28b72bcf), replay cost $82 vs real ~$102. Affected replay, statusline, stop-hook, badges. | keep-last usage per msg id, retract+recount across incremental folds (`savings_summary.py`) |
| D2 | Multi-loop savings overstated: only the globally-first grep-loop round kept as the code_search stand-in; episode ≥2 priced at $0. Live repro: two 1-round loops → fraction 0.13, must be 0. | one stand-in per episode; per-episode round groups threaded through `_round_usage` |
| D3 | Vanilla estimate multiplied main-thread-only fraction by cost incl. subagents → overstated + double-counted savings. | fraction applied to main-transcript cost only |
| D4 | `mcp__atelier__read` classified as wasteful whole-file read → inflated collapse stats on sessions that already ran with Atelier. | atelier tools + `files`/`symbol` args = targeted |
| D5 | codex/opencode replays always showed Cost $0.0000 (Claude-only cost sources). | fallback prices parsed per-turn tokens; codex file now shows real $18.71 |
| D6 | Heuristic time-saved labeled "measured". | labeled "est" (terminal + HTML) |
| D7 | Top-level sessions could show "savings counted on the parent session" (no parent exists). | copy conditioned on `is_subagent` |
| D8 | Leaner-context floor `max(in, cR−removed)` could exceed the round's actual cache_read. | `min(cR, max(0, cR−removed))` |

### report / stats / list
| # | Defect | Fix |
|---|--------|-----|
| B1 | Headline savings % divided LIFETIME saved by 30-day spend — grows without bound as history accumulates. | same-window numerator/denominator |
| B2 | Routing-only sessions showed $0 saved in list/stats (statusline showed the saving). | adoption condition includes `live_saved_usd` |
| B3 | Thinking-token cost inside the total but missing from the 4-bucket breakdown → parts didn't sum; rescale redistributed it wrongly. | thinking folded into output bucket everywhere |
| B4 | `--source store --since` silently capped at 15/host while header claimed the full window. | truncation flagged, footer note |
| R1 | `session report` line items didn't sum to "Total saved" ($6.80 listed vs $50.77 total) — dominant compression component never rendered. | `Context compression: $X (N calls)` + `of which read/output` sub-items; items now sum exactly |
| R2 | run.json with tokens but total_cost 0 → non-zero rows above `Total: $0.00`. | bucket sum becomes total, flagged `cost_estimated` |

## Plumbing — all FIXED
- **opencode replay was completely broken** (globbed `*.jsonl`; opencode moved to `opencode.db`). Now reads the DB via `serialize_opencode_session`; legacy layout fallback kept.
- **`--file` silently mis-parsed foreign transcripts** (codex file as claude → empty replay). Now auto-detects the format, exits 1 with clear error if nothing parses.
- **Replay now supports all 7 import hosts** (was 3): claude/codex/copilot (JSONL), opencode (db), hermes (state.db), cursor/antigravity (normalized artifacts from the Atelier store).
- `session report <prefix>` prefix-matches (ambiguous → candidate list); `--path` to a missing dir warns on stderr.

## UX
- Replay header no longer shows "User ran command: /model" as the task — command/caveat/system noise skipped.
- Duplicate summary lines merged into one: `tool calls X → Y · N collapsed · E search loops · B batches`.
- Money formatting: ≥$1 → 2dp, <$1 → 4dp (terminal + HTML tiles).
- 0-turn replay warns instead of rendering an empty page.
- HTML reviewed statically (Chrome extension not connected): theme-aware, hero tiles, collapsible subagents — solid; no changes needed.

## Verified
- Targeted suites: 115 passed (replay, cli_replay, session_report, cli_coverage, savings render, transcript incremental); `-k "session or savings"`: 405 passed; `mypy src`: clean (533 files).
- Cross-surface consistency for the same session: replay $112.79 vs report $111.53 (1.1% est-vs-ledger drift; was 19% apart) — saved $52.43 IDENTICAL on both; report items sum exactly.
- Live smoke: replay works on claude, codex (`--file` autodetect), opencode (from db); hermes/cursor absent locally → clean "No transcript found".

## Known remaining (out of scope, flagged)
- 6 unrelated test failures from a concurrent merge (`worktree-engine-seam`: workflow_runner ×4, uninstall_cli, code_context) — reproduce without these changes.
- opencode replays show "unknown model" (model id absent from serialized message data) — parser-level.
- PLAUSIBLE items from audit, unverified: opencode reasoning-token double-count if provider folds reasoning into output; resumed-session cross-file msg-id double count; `-0.0%` display when estimate rounds to zero.
- Audit verified correct (no action): cache-write 1h multiplier (1.6), carry prefix-sum math, time-saved formula bounds, div-by-zero guards, per-model bucket pricing partitions.
