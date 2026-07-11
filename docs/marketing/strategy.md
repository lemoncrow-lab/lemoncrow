# LemonCrow — Marketing & Positioning Playbook

_The source of truth for how LemonCrow talks about itself. Voice, motif, taglines, launch angles, growth loop. Written telegraphic; ship prose to users, keep this tight._

---

## 1. The one-sentence positioning

> **LemonCrow is the workshop that turns a coding agent into a craftsman — same model, sharper agent, with every efficiency claim backed by published raw runs.**

When you have 5 seconds: **"Same model. Sharper agent."**
When you have 12: **"A smaller mouth saves output tokens. A smaller loop saves the run. LemonCrow does the loop — and hands you the receipts."**

## 2. The brand motif: the crow vs the cave

The name is the strategy. A **crow** is the cleverest of birds — it uses tools, remembers precisely, and brings back exactly the right object and nothing more. That is the exact opposite pole from the "shrink the mouth, ignore the loop" trend, and we should own that contrast **without ever naming a competitor.**

| Them (the trend) | LemonCrow |
| --- | --- |
| Shrink the **mouth** (output only) | Shrink the **loop** (input + output + runtime + context) |
| Terse by **costume** — a persona prompt | Terse by **precision** — an enforced runtime |
| "Honest number warning: often net-negative" | Net-positive end-to-end, with published raw runs |
| Meme energy, vibes | Craft energy, receipts |

**Rule:** never punch down, never name them. We win by being the grown-up version of the same instinct. The jab is structural, not personal: _"Everyone's teaching agents to talk less. That's the easy 5%."_

## 3. Voice: witty but serious

The register is a **senior engineer with a dry sense of humor**, not a mascot. Personality lives in headers, connective tissue, and CTAs — never in the numbers, commands, or claims.

**Do**
- Lead with a concrete, reproducible number.
- Let honesty be the flex: publish the row where we lose (Terminal-Bench is flat on accuracy — say so).
- One good line per section, then get out of the way.
- Keep code, commands, errors byte-exact. Always.

**Don't**
- No grunt-speak, no fake mascot voice, no cosplay personas. LemonCrow's whole differentiator is *precision* — sounding sloppy would undercut it.
- No cherry-picked benchmarks. The credibility IS the product.
- No invented social proof (no fake HN/press/star counts). Earn it, then cite it.

### Signature lines (reuse across surfaces)
- "Same model. Sharper agent."
- "Read smarter. Think sharper. Talk less. Never forget." (the four-verb spine — keep everywhere)
- "A smaller mouth saves output tokens. A smaller loop saves the run."
- "Terse by precision, not by costume."
- "Measured, not vibed."
- "Every number reproducible. Every run published."
- "Your agent already has a big brain. What it lacks is a workshop."

## 4. The proof stack (our actual moat)

Order every pitch by strength of proof — this is what the trend can't copy:

1. **SWE-bench Verified: +12.0pp resolved, 29.5% cheaper**, 50 tasks × 5 reps, same model/env. Raw runs committed.
2. **37.7% fewer turns, 27.9% fewer output tokens, 23.7% faster wall-clock.**
3. **Estimate-your-savings one-liner** (`curl -fsSL https://savings.lemoncrow.com | bash`) — the interactive hook: proof on the reader's *own* history, read-only, no signup.
4. **Live badges** (cost saved / tokens less / calls avoided) — real-time aggregate, updates every session end.
5. **The losing row** (Terminal-Bench flat on accuracy) — counterintuitively our strongest trust signal.

## 5. Growth mechanics

| Mechanic | LemonCrow version | Where |
| --- | --- | --- |
| Meme hook | The lemoncrow-vs-cave motif; "the easy 5%" line | README hero, landing |
| Before/After drama | Telegraphic table, 2 rows, byte-exact | README, landing |
| Playful metric chart | ASCII bar chart ending in `receipts ███ PUBLISHED ✔` (our "vibes OOG") | README |
| One-command install | `curl -fsSL https://install.lemoncrow.com \| bash` | README, landing |
| Interactive proof | `curl \| bash` savings estimator on user's own logs | README, landing |
| Star CTA | "Cut 2,626 turns and ~$69. A star costs zero tokens. Fair trade. ⭐" | README footer |
| Star-history chart | `api.star-history.com` embed | README footer |
| Tweetable receipts | `lemon savings --json` / a `--share` one-liner (see §7) | CLI + statusline |
| Social proof strip | Live badges now; HN/Trending/press logos as earned (never faked) | landing |
| Ecosystem gravity | One coherent "workshop" family, not five separate tools | landing, docs |

## 6. Ecosystem naming

LemonCrow already ships the pieces — they just aren't *branded as a family* yet. Propose one coherent **workshop** vocabulary so the surfaces feel like one place, not a feature list. These are **optional naming skins over existing features** — do NOT rename code/CLI, just the marketing labels:

| Existing feature | Workshop label (marketing only) | Metaphor |
| --- | --- | --- |
| Core MCP tools | **The Bench** | the tools on the workbench |
| Savings engine + dashboard | **The Ledger** | the craftsman's books |
| Recall + cross-session memory | **The Archive** | the workshop's records |
| Swarm (worktrees) | **The Swarm** (keep) | many hands |
| Workflows | **The Blueprint** | the plan pinned to the wall |
| Model routing | **The Router** (keep) | — |
| Pro/Cloud | **The Guild** | membership, shared craft |

Keep it light. The metaphor should reward attention, never block comprehension — always pair the label with the plain feature name on first use.

## 7. Concrete next actions (ranked)

1. **Ship the rewritten `README.md`** (done in this branch) — the hero, before/after, receipts chart, star CTA.
2. **Landing page** — see `docs/marketing/landing-copy.md`; build the hero + live-metering demo + interactive-savings CTA first.
3. **`lemon savings --share`** — emit a copy-paste tweet line: _"LemonCrow saved my last week of agent work $X across N tool calls. Same model. → lemoncrow.com"_. This is the free viral loop — users become the ad.
4. **The losing-row blog post** — "The benchmark where LemonCrow doesn't win (and why we published it)." Honesty content outperforms hype content in dev communities.
5. **Launch sequence** — Show HN ("LemonCrow — same model, +12pp on SWE-bench, all raw runs published"), then r/LocalLLaMA / r/ChatGPTCoding, then submit to Trendshift/GitHub-trending-adjacent lists once stars build. Lead every post with the reproducible number, not the tagline.
6. **Statusline receipt** — show `[LemonCrow] ◈ saved $X` in supported hosts. Passive, per-session, tweetable.

## 8. Guardrails (don't torch the credibility)

- **Never fabricate proof.** No fake stars, HN threads, or press logos. Placeholders in the landing copy are marked as such.
- **Every headline number links to raw runs.** If it's not reproducible, it doesn't go in the hero.
- **Keep Free genuinely useful.** The funnel is "Free earns trust → savings number is the hook → Pro unlocks leverage," not a crippled trial.
- **Motif serves clarity.** The moment "workshop" language makes a feature harder to understand, drop it for that surface.
