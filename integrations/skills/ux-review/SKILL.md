---
name: ux-review
argument-hint: <the UI to verify — a route, page, or component story>
description: "UI review."
---

# UX review

Checks **shipped UI implementation** against design bar — real-browser render, five objective gates (steps 4–8): a11y, tokens, responsive/render, states, regression vs baseline. Repo's _own_ tooling discovered, never assumed. Not code review (`/review`); not authoring/restyling (designer's scope). On request **orchestrates** remediation — one solver per blocker, re-render before merge (step 11).

On invoke: brief user — render + check plan, fixes opt-in (per-blocker solvers, re-render before merge); gather inputs.
Whatever the target's phrasing — a UI to check, or a question about ux-review itself — it's
the surface to render; never substitute a hand-written explanation for actually rendering and
gating below.

## Operating loop

1. **Ground the target and baseline.** Discover repo's _own_ tooling first (`CLAUDE.md`/`AGENTS.md`, README, CI config, dependency manifest): design-token source of truth, stories, a11y auditor, browser driver, visual-regression harness + invocation. By stack — tokens: design-token JSON/YAML, Style Dictionary, Tailwind/theme config, CSS custom properties, Figma MCP / Dev Mode server; stories: Storybook, Ladle, Histoire; a11y: axe-core, `pa11y`, Lighthouse; drivers: Playwright MCP tools, Cypress, devtools; visual regression: Playwright `toHaveScreenshot`, Chromatic, Percy, BackstopJS, reg-suit. Web-perf skill on host → page-load/Core-Web-Vitals go there, not here. Render target defaults to the invocation argument (`/ux-review <target>`) if given — never re-ask it. Remaining gaps → one `AskUserQuestion` call, minimum: render target (only if not given via argument); baseline (default: pre-change UI via VCS — stash or parent commit); WCAG level (default **AA**); breakpoints (default **360/768/1280**); token source; state matrix (interaction, dark/high-contrast theming, reduced-motion, RTL/i18n, content stress) + scope. Confirm exact commands per rendering-side-effect guardrail first.
2. **Establish the baseline.** Render _unchanged_ UI first (stash diff or checkout baseline). Capture a11y tree + screenshots at every breakpoint.
3. **Render the change.** Same target, changed code — routes, breakpoints, viewport, data, theme unchanged. Re-capture tree + screenshots.
4. **Gate — accessibility (WCAG).** Repo's auditor (axe-core/pa11y/Lighthouse) at chosen level; a11y-tree diff vs baseline. Gates: contrast below WCAG ratio (AA = **4.5:1** text, **3:1** large text & UI); missing alt text/form labels; broken ARIA; keyboard operability + focus order (tab through); visible focus indicators. New/unresolved violation at level = **Blocker**.
5. **Gate — design-token fidelity.** Changed source (+ computed styles where feasible) vs tokens. Hardcoded colors/spacing/radii/type bypassing tokens = **Blocker**; name exact conforming token each.
6. **Gate — responsive & render integrity.** Breakpoint screenshots: overflow, clipping, overlap, vanished/misreflowed content, broken empty/loading/error states. Broken layout at any in-scope breakpoint = **Blocker**.
7. **Gate — interaction & state coverage.** Drive: hover, focus, active, disabled, loading, error/validation; theming (dark/high-contrast); motion (`prefers-reduced-motion` honored); direction (RTL if supported); content stress (long strings, 200% text zoom [WCAG criterion], missing images/data, deeply nested/empty content). Breaks layout, drops below WCAG contrast bar, or hides essential content = **Blocker**.
8. **Gate — visual regression.** Screenshots + a11y tree vs baseline. Intended → report before→after; unintended drift outside changed surface = regression → **Blocker**. Repo harness if present, else direct screenshot compare.
9. **Critique (advisory only).** Hierarchy, spacing rhythm, alignment, typographic taste, "feels off" = **Warnings**, never blockers.
10. **Verdict.** Review ends with exactly one fenced JSON block, caller-parseable:

```json
{
  "verdict": "NEEDS_FIX",
  "gates": {
    "a11y": "fail",
    "tokens": "pass",
    "responsive": "pass",
    "states": "fail",
    "regression": "pass"
  },
  "baseline": "parent commit (HEAD~1) vs working tree",
  "observations": {
    "a11y": "contrast 2.9:1 on .cta fg/bg (WCAG AA needs 4.5:1); axe: 1 critical, 0 serious",
    "states": "card body clips at 200% text zoom; dark-mode focus ring invisible (1.8:1)",
    "regression": "header diff is the intended nav change; no off-target drift",
    "breakpoints": [360, 768, 1280]
  },
  "blockers": [
    "contrast 2.9:1 on .cta (WCAG AA needs 4.5:1) — use token color/cta-fg (#0b5cad, 4.7:1)",
    "200% text zoom clips .card body — fixed container height; switch to min-height"
  ],
  "warnings": [
    "card spacing 14px is off the 4px scale; nearest token space-3 (12px)"
  ],
  "not_checked": [
    "screen-reader semantics (NVDA / VoiceOver)",
    "assistive-tech focus traps",
    "motion / animation timing",
    "production data shapes"
  ]
}
```

11. **Remediate (optional, user-gated — never automatic).** `NEEDS_FIX` → designer/engineer by default. Opt-in via `AskUserQuestion` after verdict; reviewer **never hand-edits the UI**. You = orchestrator: spawn solvers via host sub-agent capability — create worktrees, dispatch, re-render, open PRs; no end-to-end hand-off to workflow/swarm engine — you own the loop. Per blocker: own pipeline, independent:
    1. **Isolate.** One **git worktree per blocker** (host worktree/swarm/sub-agent capability, else `git worktree add`) — no collision, masking, unrejectable bundle merge.
    2. **Spawn one sub-agent per blocker, yourself.** Host sub-agent tool → one solver per finding/worktree. Solver input = _only_ its finding: evidence (screenshot, failing ratio or axe rule, exact element/selector) + minimal conforming fix from verdict (exact token, contrast-passing value). No two findings per solver; no restyle/refactor widening.
    3. **Re-render, don't trust the diff.** Solver done → re-render failed gate(s) in worktree — identical target, breakpoints, viewport, data, theme, state matrix — re-run a11y audit. Gate must pass; no prior-passing gate regressed. Gate not `pass` = not done → send back or report unresolved; never merge.
    4. **Review.** Per finding: before→after to user (screenshots + numbers — contrast ratio, token, axe count).
    5. **Merge gate.** Merge to `main` (repo convention — PR or direct) only if (a) re-render proves gate cleared, same target, and (b) user approves before/after evidence. Rejected → discard worktree. Per-finding merges — each judged on own evidence.

## Guardrails

- **Gate only on the measurable** — the five gates above. Aesthetics = `Warning`-only, never `Blocker` for "feels off." No fabricated "design score" — real measurables: WCAG ratios, exact token equality, binary render/layout integrity.
- **Reason about the accessibility tree first, pixels second.** ARIA snapshot deterministic, styling-churn-resilient, doubles as a11y check; screenshots for pixels-only — overflow, clipping, contrast, layout. Never gate on raw markup.
- **A clean axe run is not a clean a11y verdict.** Automation catches ~half of WCAG; non-machine-checkable (screen-reader semantics, focus traps, cognitive load, motion timing) → `not_checked`, hand to human.
- **Consume the design system; don't invent it.** Tokens, breakpoints, states from repo or Figma MCP = source of truth; never guess brand values.
- **A state you didn't render is not a pass.** Unexercised state = `not_checked`, never `pass`.
- **No baseline, no regression claim.** Unchanged UI unrenderable → `regression` gate `skipped`, verdict defaults `NEEDS_FIX`.
- **Compare like for like.** Same routes, breakpoints, viewport, data, theme, state matrix both sides, else noise.
- **Verify, don't redesign.** Report drift + minimal conforming fix (exact token, contrast-passing value); no restyle/refactor/"improve" — authoring = designer's call.
- **Remediation is opt-in, orchestrated by you, never inline (see step 11).** One finding → one worktree → one solver, minimal conforming fix; re-render failed gate (identical target + state matrix) before merge — diff ≠ proof, clean re-render + re-audit = proof. No merge without proof + user approval.
- **Rendering is a side-effect.** Dev-server start, URL hit, audit/visual-regression run — confirm via `AskUserQuestion` unless repo already authorizes.
- **Default to `NEEDS_FIX`.** `DONE` = positive proof every gate passed; skipped gate (`status: skipped`) ≠ pass.
