# wozcode.com teardown → landing/ actions

What they do better, and the concrete port for `landing/`.

## 1. Hero leads with money, not mechanism

- Them: "Cut your Claude Code costs in half." One promise, denominated in the buyer's currency.
- Us: "Keep your coding agent sharp on real codebases." — mechanism-first, abstract; the 92.8% proof line is a subhead.
- Action: promote a dollar/time outcome to `Hero.tsx` H1 ("Get twice as much done before you hit your limit" / "29.5% cheaper per task, same model"). Keep the resolve-rate line as the credibility subhead. `HeroSavings` already computes the number — say it in the headline.

## 2. Two-audience CTA split above the fold

- Them: "For individuals → Start for free" and "For engineering teams → Book a pilot" side by side. The team path is a sales pipeline, not a pricing tier.
- Us: single `Install free` + `See matched proof`.
- Action: add a second CTA column in `Hero.tsx` → `/enterprise#book` (Cal/Calendly embed). Highest-leverage single change for revenue per visitor.

## 3. Personalized proof before install (their strongest device)

- Them: `curl -fsSL https://wozcode.com/savings-check.js | node -` scans the visitor's *own* Claude Code history and prints what they would have saved, last 30 days + lifetime. Runs locally, source linked on GitHub.
- Why it converts: turns a generic claim into the visitor's own number, with zero signup and zero trust cost.
- Action: ship `lemoncrow-savings-check` as a standalone, dependency-free script served from the landing origin, source link to the repo. Feed the result into `EstimateSavings.tsx` instead of slider-estimated inputs.

## 4. Live aggregate counters + leaderboard

- Them: cumulative $ saved, hours saved, session count — all-time running totals; plus a sortable leaderboard (cost/time/tokens, any window) that requires sign-in to appear on.
- Effect: social proof that grows on its own, plus a signup incentive that is not a paywall, plus shareable vanity.
- Action: extend `ScaleProof.tsx` / `MetricsProof.tsx` into a live rollup fed by existing telemetry (`core/service/telemetry/public_rollup.py`). Leaderboard is a v2, but the counters are cheap.

## 5. Head-to-head benchmarks as narrative, not a table

- Them: three named scenarios with real repos and live data (art portfolio from scratch; 68-table AACT clinical-trials DB; `GeekyAnts/express-typescript-postgres`), stated as "same prompts, run head-to-head", each with prompt counts and a visible final result.
- Us: `Benchmarks.tsx` / `RetrievalBench.tsx` are accurate but read as instrumentation.
- Action: reframe two benchmarks as story cards — named public repo, N sequential prompts, side-by-side final artifact/answer. Keep the rigorous table below for the skeptics.

## 6. Pricing anchored on savings, not features

- Them: Free = "$100/mo in free Claude Code savings, then capped" ($200 with a corporate email — a free B2B email-capture); Pro $20/**week** = uncapped savings; Enterprise custom.
- The frame: you are buying back savings, so the price is trivially justified.
- Action: in `Pricing.tsx`, lead each tier with the savings ceiling it unlocks, not the feature list. The corporate-email bonus is a clean lead-gen mechanism worth copying.

## 7. FAQ that answers objections in the buyer's words

- Them: explicit "does it proxy/exfiltrate" answer, naming what telemetry *is* collected (PostHog, token counts, tool invocations — no code, no prompts). Precision buys trust.
- Us: `TrustLocal.tsx` states the posture but there is no FAQ block on `index.astro`.
- Action: add an FAQ section (also earns SEO/AI-answer surface): what is it, does it work with my subscription, does my code leave, is it really free, do I need an account.

## 8. Voice

- Their closer: "If you prefer paying more for slower, less capable performance, WOZCODE probably isn't for you."
- One sharp line does more for memorability than a paragraph of positioning. `FinalCta.tsx` is where it goes.

## Section-order change for `index.astro`

Current: Hero → Install → DelegationGap → HowItWorks → WhyRuntime → CodeHygiene → Benchmarks → …14 sections… → FinalCta.

Problem: the proof a buyer needs (Benchmarks, ScaleProof) sits behind four explanation sections, and there is no testimonial anywhere.

Proposed: Hero (money headline + dual CTA) → live counters → run-it-on-your-machine script → Benchmarks (story cards) → testimonials → HowItWorks → WhyRuntime/DelegationGap → TrustLocal → Pricing → FAQ → FinalCta. Move `LemonCodeTeaser` / `GraphRoadmap` / `SearchCompilerTeaser` off the home page — three roadmap teasers dilute the single action.

## What not to copy

- Placeholder zeros rendering live on their page ("0–0%", "0%", "0 sessions") — their counters fail open and read as broken. Ours must render a real number server-side or not render.
- Claiming a competitor's benchmark number without a reproducible harness; our matched-pair methodology is a differentiator, keep it visible.
