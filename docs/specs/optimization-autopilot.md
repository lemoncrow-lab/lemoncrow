# Spec — Optimization Autopilot

> Standalone spec. Cross-phase: a v0 ships in Phase 2; continuous personalization in Phase 3.
> Originated from product brainstorm 2026-05-15. Maintained on its own branch.

## The killer line

> "Based on your last 7 days, Atelier can save about 34% with no statistically visible quality loss on your golden tasks."

That sentence — not the slider, not the graph — is the product. Everything in this spec exists to make Atelier able to say that sentence honestly.

## Why

Today Atelier exposes two levers (routing, compaction) but the user has to trust our defaults blindly. Power users want tunability; non-experts want guidance; everyone wants proof. The Optimization Autopilot makes both work:

- **Non-experts** pick a preset (Conservative / Balanced / Economy / Custom)
- **Power users** see the Pareto frontier and tune
- **Both** get a recommendation backed by real data from their last 7 days

This is also where the data we already capture (Spec 01 outcomes, Spec 02 costs, Spec 07 counterfactuals) becomes a user-facing differentiator instead of just plumbing.

## What — user-visible

### Terminal

```bash
$ atelier optimize
Optimization Autopilot
─────────────────────────────────────────────────
Analysed your last 7 days: 184 sessions, 91 replayable tasks

Current setting: Balanced (default)
  Cost / week:      $42.10
  Estimated quality: 96.8%
  Latency mult:      1.0x
  Escalation rate:   8%

Recommended: Custom (auto-tuned from your sessions)
  Cost / week:      $27.30  (-35%)
  Estimated quality: 96.1%  (-0.7%)
  Latency mult:      0.82x
  Escalation rate:   13%

Confidence: Medium
  143 sessions cleanly classified
  Only 12 high-complexity coding tasks in window — quality estimate is noisy here

Try the recommendation?
  [a] Apply now
  [s] Shadow-run for 7 days (no behavior change yet)
  [d] Show details (Pareto curve, per-task breakdown)
  [n] No thanks
>
```

```bash
$ atelier optimize details
Pareto frontier — cost vs estimated correctness on your tasks
─────────────────────────────────────────────────
Correctness
   100% ●A
        │
   97%  ●─●B   ●C ◯ (current)
        │
   95%  │       ●D ★ (recommended)
        │
   93%  │             ●E
   91%  │                   ●F
        └───────────────────────── Cost / week
        $20    $30    $40    $50

  A (Strong-only)    $52.30   100.0%    [opus everywhere, no compact]
  B (Conservative)   $43.10    97.2%    [sonnet, safe compact only]
  C (current)        $42.10    96.8%    [current default]
  D (recommended) ★  $27.30    96.1%    [smart routing + safe + dedup compact]
  E (Economy)        $24.40    94.0%    [haiku-first, aggressive compact]
  F (Maximum saving) $20.10    91.8%    [haiku everywhere, aggressive compact]

Compaction breakdown for [D recommended]:
  Prompt-cache reorder:  +$3.80/wk saved   (very low risk)
  Dedup compaction:      +$4.10/wk saved   (very low risk)
  Retrieval filter:      +$2.40/wk saved   (medium risk)
  Lossy summary:         +$0.00/wk saved   (off — high risk)

Routing breakdown for [D recommended]:
  Cheap-tier for 41% of turns (read/grep/list, low complexity)
  Medium-tier for 53% of turns (edit/test/refactor)
  Expensive-tier for 6% of turns (architect/migration/security)
  Escalation rate: 13% (cheap → medium when uncertainty rises)
```

```bash
$ atelier optimize apply --preset balanced
$ atelier optimize apply --recommended
$ atelier optimize apply --custom ~/.atelier/my-policy.yaml
$ atelier optimize shadow --policy recommended --days 7
$ atelier optimize compare    # current vs shadow
```

### Settings file

`~/.atelier/optimization.yaml`:

```yaml
optimization:
  preset: custom               # conservative | balanced | economy | custom
  quality_floor: 0.96          # minimum acceptable correctness
  confidence_required: medium  # low | medium | high — autopilot only changes if confidence met

routing:
  policy: complexity_escalate
  simple:    cheap
  medium:    medium
  hard:      expensive
  escalate_on:
    - low_confidence
    - failed_tests
    - repeated_tool_error
    - high_diff_risk
    - user_marks_wrong

compaction:
  prompt_cache_reorder: true     # very low risk, always on
  dedup: true                    # very low risk, always on
  retrieval_filter: true         # medium risk, can disable
  lossy_summary: false           # high risk, off by default
  trigger_at_context_fraction: 0.72
  preserve:
    - user_requirements
    - repo_facts
    - active_plan
    - open_files
    - failing_tests
    - tool_results
```

## Compaction split — the 4-type taxonomy

This is the most important conceptual change. Compaction is **not one slider**.

| Type | Correctness risk | Mechanism | Default |
|------|-----------------|-----------|---------|
| **Prompt-cache reorder** | Very low | Put stable instructions, tool schemas, repo map at the front to maximise provider cache hits | On |
| **Dedup** | Very low | Remove repeated tool outputs, duplicate file reads, repeated logs | On |
| **Retrieval filter** | Medium | Include only retrieval-relevant files/spans (can miss hidden deps) | On with safe-mode |
| **Lossy summary** | High | Summarise old discussion / resolved branches / repeated reasoning | Off by default |

**Implication:** in the UI we always show four separate savings numbers. Users see "you can save $X with zero risk by enabling dedup" — that's a free win.

## Complexity scoring — for routing

```
complexity_score =
  0.20 × task_type_weight      # bug fix=0.6, refactor=0.7, migration=0.95, explain=0.2
+ 0.15 × repo_context_size     # # files touched / 50
+ 0.10 × user_intent_risk      # explicit user keywords: production, security, migration, urgent
+ 0.15 × test_failure_signal   # prior_errors > 0 → bump score
+ 0.10 × dependency_graph_depth # multi-module change
+ 0.10 × ambiguity_score       # user_message length × question marks
+ 0.10 × prior_failure_count   # retries in this session
+ 0.10 × required_tool_count   # # distinct tools needed for the task
```

Each component clamped to [0, 1]. Output clamped to [0, 1].

**Important:** ship this scored alongside the actual decision so users can audit it.

## Escalation triggers (the most important routing logic)

When the cheap model runs and any of these fire mid-task, escalate to medium for the rest of the task:

- `model_error` (per Spec 01 definition: wrong old_string, schema fail, etc.)
- `low_confidence` (model outputs "I'm not sure", "let me check", "this might be")
- `tool_repeat` (same tool called 3× without progress)
- `high_diff_risk` (edit touches >10 files or migration paths)
- `user_marks_wrong` (user follow-up contains "no", "wrong", "broken", "fix this")
- `test_failure` (cmd output contains test failures)

Each trigger increments an escalation counter. Threshold = 2 → escalate.

## Evaluation sources — five tiers

In strict order of trust:

1. **Golden tests** (release gate) — curated, deterministic, hand-graded
2. **Historical replay** (personalization signal) — past 7/14/30 days, real user data
3. **Shadow runs** (zero-risk validation) — alternative policies run in parallel, comparison only
4. **Online canary** (limited rollout) — 5–10% of safe tasks through new settings
5. **Human feedback** (continuous calibration) — user accepted / rejected / edited / reverted

Each tier feeds the optimizer with different weights:
```
combined_quality = 0.40 × golden  +  0.30 × historical  +  0.20 × shadow  +  0.05 × canary  +  0.05 × human
```

## Golden test suite

A versioned set of ~50–100 tasks committed to `tests/golden/optimization/`:

- 15 explain / summarize tasks
- 15 single-file edit tasks
- 15 multi-file refactor tasks
- 10 bug fix with provided failing test
- 10 architecture/planning tasks
- 5 migration / schema tasks
- 5 security-sensitive tasks
- 5 ambiguous / underspecified tasks

Each task is a JSONL run trace:
```json
{
  "task_id": "edit-rename-fn-001",
  "complexity_label": "simple",
  "messages": [...],
  "tool_use_expected": ["Grep", "Edit"],
  "success_criteria": {
    "files_modified": ["src/foo.py"],
    "test_must_pass": "tests/test_foo.py::test_rename",
    "no_extra_files": true
  }
}
```

Scoring per task:
- `tool_match`: did the run call the expected tools?
- `criteria_pass`: did success criteria evaluate true?
- `cost_ratio`: cost vs baseline policy
- `latency_ratio`: latency vs baseline

Golden suite runs nightly in CI plus on every `atelier optimize` invocation.

## Presets

```yaml
# preset: conservative
quality_floor: 0.98
routing:
  policy: prefer_strongest
  simple:    medium    # don't even drop to cheap for simple
compaction:
  prompt_cache_reorder: true
  dedup: true
  retrieval_filter: false  # safe-mode, no retrieval risk
  lossy_summary: false

# preset: balanced (default)
quality_floor: 0.96
routing:
  policy: complexity_escalate
  simple:    cheap
  medium:    medium
  hard:      expensive
compaction:
  prompt_cache_reorder: true
  dedup: true
  retrieval_filter: true
  lossy_summary: false

# preset: economy
quality_floor: 0.93
routing:
  policy: cheap_first
  simple:    cheap
  medium:    cheap   # try cheap, escalate on failure
  hard:      medium
compaction:
  prompt_cache_reorder: true
  dedup: true
  retrieval_filter: true
  lossy_summary: true  # accept some lossy summarization
  trigger_at_context_fraction: 0.65  # compact earlier
```

`custom` reads the user's full YAML; `recommended` is autopilot-generated.

## Optimizer algorithm

```
1. Load last 7 days of sessions from ledger
2. Classify each session's tasks by (task_type, complexity_score)
3. Compute baseline cost and quality (from outcome capture)
4. Generate candidate policies:
   - 4 presets (Conservative / Balanced / Economy / Maximum-saving)
   - 5 auto-generated points along the cost-quality frontier
5. For each candidate, replay sampled tasks (5–15 per complexity bucket):
   - Use Spec 09's cross-vendor router to simulate
   - For lossy operations (model swap, compaction), use replay infra (Claude CLI -p) to get real haiku output where possible
6. Score each candidate:
   - quality = combined_quality formula above
   - cost = sum of weekly projected cost
   - latency = avg from replay
   - escalation_rate = % of tasks that triggered escalation
7. Filter candidates: quality >= quality_floor AND escalation_rate <= threshold
8. Pick lowest-cost survivor
9. Compute confidence:
   - high: 50+ sessions per major bucket
   - medium: 15–49 sessions per major bucket
   - low: < 15 sessions
10. Output recommendation + Pareto data
```

## UI — terminal (Phase 2)

- `atelier optimize` — show summary + recommendation
- `atelier optimize details` — show Pareto + per-component savings
- `atelier optimize apply [--preset X | --recommended | --custom path]`
- `atelier optimize shadow [--policy X --days N]` — run policy in parallel, no behavior change
- `atelier optimize compare` — current vs shadow
- `atelier optimize history` — show past recommendations + outcomes

## UI — web (Phase 3, via Spec 10 dashboard)

Pages:
- **Pareto frontier** — interactive chart, click a point to see policy details
- **Compaction breakdown** — toggle each of the 4 types, see savings update
- **Routing breakdown** — distribution of complexity scores, escalation timeline
- **Confidence** — explain why confidence is high/med/low
- **Golden tests** — pass/fail per policy
- **Shadow run results** — when applicable

## Where — files

### v0 (terminal-only, Phase 2)

| File | What |
|------|------|
| `src/atelier/core/capabilities/optimization/__init__.py` | new package |
| `src/atelier/core/capabilities/optimization/policy.py` | `Policy` dataclass, presets, YAML I/O |
| `src/atelier/core/capabilities/optimization/complexity.py` | `score_complexity()` |
| `src/atelier/core/capabilities/optimization/compaction_types.py` | the 4-type taxonomy |
| `src/atelier/core/capabilities/optimization/optimizer.py` | the algorithm |
| `src/atelier/core/capabilities/optimization/golden_runner.py` | runs golden tests against a policy |
| `src/atelier/core/capabilities/optimization/replay_simulator.py` | simulates a policy against historical sessions |
| `tests/golden/optimization/*.jsonl` | the golden test corpus |
| `src/atelier/gateway/adapters/cli.py` | add `optimize` command group |

### v1 (continuous, Phase 3)

| File | What |
|------|------|
| `src/atelier/core/capabilities/optimization/autopilot.py` | nightly optimizer with hysteresis |
| `src/atelier/core/capabilities/optimization/shadow.py` | shadow-run engine |
| `src/atelier/gateway/web/routes.py` | new `/api/v1/optimization/*` endpoints |

## Data model

```python
@dataclass(frozen=True)
class Policy:
    name: str
    preset: str  # conservative | balanced | economy | custom | recommended
    quality_floor: float
    routing: RoutingPolicy
    compaction: CompactionPolicy

@dataclass(frozen=True)
class CompactionPolicy:
    prompt_cache_reorder: bool
    dedup: bool
    retrieval_filter: bool
    lossy_summary: bool
    trigger_at_context_fraction: float
    preserve: list[str]

@dataclass(frozen=True)
class RoutingPolicy:
    policy: str  # complexity_escalate | prefer_strongest | cheap_first
    simple: ModelTier
    medium: ModelTier
    hard: ModelTier
    escalate_on: list[str]

@dataclass
class OptimizationResult:
    current_policy: Policy
    recommended_policy: Policy
    candidates: list[Candidate]     # Pareto points
    confidence: str                 # low | medium | high
    confidence_reason: str
    sessions_analysed: int
    weekly_savings_usd: float
    quality_delta: float

@dataclass
class Candidate:
    policy: Policy
    weekly_cost_usd: float
    estimated_quality: float
    latency_mult: float
    escalation_rate: float
    compaction_breakdown: dict[str, float]  # per-type weekly savings
```

## Dependencies

- **Hard dependency on Spec 01** (outcome capture) — without outcomes we have no quality signal
- **Hard dependency on Spec 02** (cost report) — for baseline
- **Soft dependency on Spec 07** (counterfactual) — uses the same pricing engine
- **Soft dependency on Spec 09** (cross-vendor routing) — enables broader Pareto frontier
- **v1 depends on Spec 10** (web dashboard) — for the Pareto UI

## Out of scope

- **Auto-applying changes without user consent.** Always recommend, never silently switch.
- **Multi-user shared policies.** Spec 12 (Team).
- **Per-project policies.** v2 — for now, one user-global policy.
- **Custom golden test authoring UI.** v2 — for now, golden tests live in repo.
- **A/B testing across users for policy quality.** Federated learning (Spec 11) handles this differently.
- **Cost optimization for non-AI components** (test runners, CI). Out of scope.

## Acceptance criteria

### v0 (terminal, Phase 2)
- [ ] `atelier optimize` runs in <5s on 7-day window with 200 sessions
- [ ] Recommendation always includes confidence + reason
- [ ] All 4 compaction types report separate savings
- [ ] Golden test suite of ≥50 tasks committed and runnable
- [ ] `apply --preset conservative` and `--preset economy` work
- [ ] `shadow --days 7` runs in parallel without affecting live sessions
- [ ] Output never shows "save 40%" without confidence + caveat
- [ ] Tests cover: empty history (no recommendation, polite message), low-confidence case (recommend cautiously), high-confidence case (full recommendation)
- [ ] If outcome capture data is insufficient (<10 outcomes), output: "Need more session history before recommending — try again after 50+ sessions."

### Shadow-run cost guardrails (mandatory, ship-blocking)

Shadow runs spend real money in parallel with normal usage. Every one of these is a hard requirement before `optimize shadow` ships to users:

- [ ] **Explicit opt-in flag.** First-ever invocation of `optimize shadow` requires `--i-understand-this-costs-money`. After explicit acceptance, the consent is recorded in `~/.atelier/optimization.yaml` (`shadow_consent_at: <iso-timestamp>`) and the flag is no longer required for that user.
- [ ] **Spend cap.** Shadow daily spend hard-capped at 10% of the user's trailing 7-day average. Cap is enforced inside the shadow runner (early-terminate the shadow if cap is hit; never let it exceed). Cap is configurable via `--max-daily-spend-usd` but cannot exceed 25% of baseline by default.
- [ ] **Pre-run estimate.** Before starting a shadow, print an estimate: `"Shadow will spend approximately $X this week against your $Y baseline. Continue? (y/n)"`. Default answer is `n`.
- [ ] **Live cost tracking.** `atelier optimize shadow status` shows live shadow spend vs cap. Updated every shadow task completion.
- [ ] **Separate cost line.** `atelier optimize compare` and `atelier session report` show shadow cost as a distinct line item, never bundled into baseline or total. Phrase: `"Shadow spend (this run only): $X"`.
- [ ] **Auto-stop on anomaly.** If a single shadow task costs >5× the median shadow task cost, the runner pauses and asks for confirmation before continuing.
- [ ] **Revocable.** `atelier optimize shadow stop` halts immediately; `atelier optimize shadow forget-consent` revokes the persistent consent flag.
- [ ] **Tests cover:** opt-in not granted (refuses to run), cap hit mid-run (clean early-termination), anomaly trigger, consent revocation.

### v1 (continuous, Phase 3)
- [ ] Nightly autopilot runs as background hook (no daemon)
- [ ] Hysteresis: policy only changes if delta > 5% over a stable 14-day window
- [ ] Web dashboard Pareto chart interactive
- [ ] User can revert any auto-applied change within 7 days

## Honest risks

| Risk | Why it bites | Mitigation |
|------|--------------|------------|
| Quality measurement is noisy (22% tool-divergence from replay) | We promise "no quality loss" and a power user catches a regression | Show confidence intervals always; never claim "0% quality loss" — claim "≤X% on N tasks" |
| Continuous opt becomes flapping | Settings change every night; users lose trust | 14-day hysteresis + delta threshold + explicit "stable for N days" requirement |
| Users with thin history get bad recommendations | Cold-start problem | Confidence=low always shown; recommend "use Balanced preset" if <20 sessions |
| Golden tests don't reflect user's actual work | Optimizer over-fits to golden, regresses on user's real tasks | Weight historical replay heavily (30%); show per-bucket quality, not just average |
| User confusion: "but I didn't change anything, why did my AI behavior shift?" | Trust collapse | NEVER silently change. Always recommend. Even autopilot needs explicit one-time consent + notification on every change |

## Open questions for the executor

1. **Initial complexity_score weights** — the formula above is a starting point. Tune against golden test results. The right answer is "weights that minimise prediction error vs labelled complexity in golden suite."
2. **Should `optimize` block in CI?** I.e., should `atelier optimize --strict` exit nonzero if golden tests regress on the current policy? **Default: no for v0; yes as opt-in for v1.**
3. ~~**Where does shadow-run cost go?**~~ **Resolved.** See "Shadow-run cost guardrails" in Acceptance criteria — explicit opt-in flag, hard 10% spend cap with config override capped at 25%, pre-run estimate with `n` default, live tracking, separate cost line, auto-stop on anomaly, revocable consent.
4. **Cross-vendor routing in Pareto** — when Spec 09 is live, candidates can use Gemini/GPT. Should we show vendor-specific Pareto points or one combined frontier? **Default: combined frontier with vendor mix annotated per point.**
5. **Naming.** "Autopilot" implies autonomy; "Advisor" implies recommendation. Pick one and stick to it. Current spec uses "Autopilot" but defaults to advisory behaviour — this is confusing. **Recommendation: rename to "Optimization Advisor" unless we genuinely ship autonomous behaviour.**

## Phased delivery

### Phase A — Foundations (2 weeks after Spec 01 + 02 ship)
1. 4-type compaction taxonomy in code (refactor existing compactor)
2. Complexity scorer
3. Policy YAML + presets
4. `atelier optimize apply --preset X` works

### Phase B — Recommendation engine (1 week)
5. Golden test suite (≥50 tasks)
6. Historical replay simulator (uses Spec 09 if available)
7. Optimizer algorithm
8. `atelier optimize` + `details` + `apply --recommended`

### Phase C — Validation tools (1 week)
9. Shadow runner
10. `optimize shadow` + `compare` + `history`

### Phase D — Continuous (Phase 3 of main roadmap)
11. Nightly autopilot (with hysteresis)
12. Web dashboard Pareto UI
13. Cross-vendor frontier

## Branch and PR strategy

This spec lives on its own branch (`feat/optimization-autopilot`). Land in slices:

1. PR-1: 4-type compaction taxonomy (small, low-risk, useful even without the autopilot)
2. PR-2: Complexity scorer + golden tests scaffold
3. PR-3: Policy YAML + presets
4. PR-4: `optimize` command (advisory only, no apply)
5. PR-5: `optimize apply` + presets
6. PR-6: Shadow runner
7. PR-7: Web dashboard Pareto (depends on Spec 10)

Each PR ships behind a feature flag until PR-7 is green.

## Status

- [ ] Pending — full review needed before starting PR-1
- [ ] PR-1 (4-type compaction)
- [ ] PR-2 (complexity + golden)
- [ ] PR-3 (policy YAML)
- [ ] PR-4 (optimize advisor)
- [ ] PR-5 (apply)
- [ ] PR-6 (shadow)
- [ ] PR-7 (web Pareto)
