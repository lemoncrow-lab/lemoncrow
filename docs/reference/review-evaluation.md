# Review evaluation, Gate I and Gate H

The Review evaluation harness replays a corpus of past Changes through the real Review pipeline and measures what each depth catches, how much noise it adds and what it costs. Gate I uses those measurements to stop a model, prompt, policy-default or preset change from shipping a regression unnoticed.

Everything below runs offline from recorded fixtures. No command spends model money unless you pass `record --live` with a budget cap, and that path refuses to run in CI or under pytest.

## Commands

```bash
uv run lc dev eval review run                          # measure every gate selection on the built-in corpus
uv run lc dev eval review run --selection thorough --out /tmp/review-eval.json   # full report with per-case Review records
uv run lc dev eval review gate                         # Gate I against evals/review/baseline.json
uv run lc dev eval review gate --mode enforce          # preview enforcement (exit 1 unless pass/accepted)
uv run lc dev eval review gate --update-baseline       # record this result for its digest set
uv run lc dev eval review accept --owner NAME --reason TEXT   # accept a blocked trade-off, numbers recorded
uv run lc dev eval review record --synthetic           # re-author fixtures with the scripted reviewer (no model)
uv run lc dev eval review record --live --max-calls 40 [--max-usd 2]   # real provider, budget-capped, never in CI
```

`run` exits 1 when a fixture is stale or missing. `gate` exits 0 in report mode whatever it finds; in enforce mode it exits 1 unless the result is `pass` or `accepted`. On the built-in corpus a full `run` or `gate` takes about 10 to 15 seconds on a developer machine (55 Reviews: 11 cases under 5 selections).

## Layout

```text
evals/review/
  gate.yaml                    # mode, corpus, replay selections, thresholds, authorized owners
  baseline.json                # the recorded gate result (snapshot + digest set)
  acceptances/*.yaml           # owner acceptances of measured trade-offs
  corpus/builtin/
    corpus.yaml                # manifest: id, visibility, license, case ids
    cases/<id>.yaml            # one Change and its expectations
    fixtures/<id>.json         # recorded model outputs for that case
```

## Corpus format

A case embeds its files (so corpus code is never linted, type-checked or imported) and states what a good Review finds:

```yaml
id: auth-env-bypass
title: An environment variable bypasses authorization
kind: defect                      # or clean: a known-clean Change, every surfaced finding is noise
provenance:
  source: synthetic               # synthetic | public_repository | internal_repository | organization_review
  note: Authored for the LemonCrow Review evaluation harness.
  # repository: and commit: are required for repository sources
license: Apache-2.0
base: {src/auth/session.py: "..."}
head: {src/auth/session.py: "..."}  # null deletes a file
review_config: null               # optional trusted-base .lemoncrow/review.yaml text
expected:
  findings:
    - {id: env-bypass, path: src/auth/session.py, lines: [7, 8], category: security, severity: p1}
  behavior:
    - {id: reads-auth-bypass-env, kind: side_effect, subject: "env_var:AUTH_BYPASS", change: added}
synthetic_reviewer:               # used only by record --synthetic
  confirm:
    - {path: src/auth/session.py, category: security, title: ..., explanation: ..., consequence: ...}
```

The built-in corpus (`evals/review/corpus/builtin`, visibility `public`) has 11 cases: 7 defects with expected findings, 5 expected behavior deltas (an environment variable, an outbound host, a destructive migration, a new migration, a skipped test) and 2 known-clean Changes. Every case is synthetic, written for this repository, licensed Apache-2.0, and says so in its own file. A public corpus accepts only `synthetic` cases or `public_repository` cases with a permissive license, a repository URL and a commit.

### Organization corpora

Customer Reviews belong in an organization corpus (`visibility: organization`). Loading one needs both:

- the caller's explicit opt-in naming the organization: `--organization-opt-in acme`; and
- an `opt_in` record in its manifest with `organization`, `approved_by`, `approved_at` and `reference`.

An organization corpus is never bundled. It is refused when it lives inside the repository checkout or the installed package, and `run --out` and `--keep-stores` refuse to write its results there. Gate I only replays the public corpus named in `gate.yaml`. A test fails if any tracked `corpus.yaml` is not public.

## What a replay runs

Each case runs under each selection in its own throwaway workspace. The workspace holds a real git repository (base and head commits with fixed signatures) and a real SQLite Review store, and the replay composes the same components the Review API uses:

1. capture (`build_review_packet_with_blobs`, `snapshot_revision`);
2. targets (frontier, Routine proofs, trusted-base path policy);
3. the effective run plan (`RunPlanService`), with the selection applied as the organization run-settings layer;
4. static side-effect and behavior analysis, when planned;
5. AI review (`run_independent_review` under the plan's AI stage), with model outputs served from fixtures;
6. planned checks, artifact analyses and runtime observation that have recorded execution fixtures, through the product `run_check_entry`, `run_analysis_entry` and `run_observation_entry` paths. Only the final `RunSide` process boundary is replaced by an exact recorded `SurfaceRunResult`.

Replay never starts reviewed code. Static and AI stages run through their normal hermetic evaluation paths; an execution entry runs only when an exact execution fixture exists. A missing or stale execution request is a fixture event and makes the Review incomplete rather than falling back to a local runner. The built-in Gate I corpus records every execution row planned by its configured selections, so there are no execution estimate-only rows. Impact analysis, agent-session provenance and SARIF ingestion remain off because they read state outside the workspace.

The selections come from `gate.yaml`: `quick`, `auto`, `thorough`, `deep`, and the Custom selection `static-only` (Custom from Quick with only `side_effects.static`).

## Matching rules

A surfaced finding catches an expected defect when all of these hold:

- the paths are equal;
- the finding's category is one of the expected categories;
- the finding's source is one of the expected `sources`, if any are named;
- the line ranges overlap once the expected range is widened by the tolerance on both sides (`line_tolerance` per expectation, else the corpus value, default 3).

Folded findings, which fall below the effective finding threshold, neither catch nor count as noise. A behavior delta matches when the kinds are equal, its subject matches the expected `fnmatch` pattern (case-sensitive) and, when given, the change is equal. Assignment is one-to-one and maximum, in a fixed order, so the same inputs always give the same pairs.

## Metrics

Per selection:

| Metric | Definition |
| --- | --- |
| catch rate | matched expected defects / expected defects |
| behavior catch rate | matched expected behavior deltas / expected ones (the rest are missed side effects) |
| noise rate | surfaced findings matching no expectation / surfaced findings |
| severity agreement | matched findings whose severity equals the expected one |
| cost per Review | planned compute minutes split customer (local, worker, Review host) / managed; model calls split provider API / local; tokens and provider dollars where the provider reported them, with unknown calls counted, never estimated. A cost reported in another currency, or with no currency, is unknown, never summed as dollars |
| cost per catch | cost over the corpus divided by weighted catches (defect catches × `defect_catch_weight` + behavior catches × `behavior_catch_weight`), in planned compute minutes, in model calls and, when every provider-API call reported a USD cost, in provider dollars. A plan estimate that is not a finite, non-negative number makes compute unknown, never zero. Gate I compares each unit with the baseline |
| time | measured wall time and in-process compute; reported, never gated |

Each list names the case it came from, and the full report (`--out`) holds every case's Review record (Review and revision ids, plan, findings, behavior deltas, model calls), so every aggregate traces to the record it counts. Per catalog entry, the report gives the Reviews that ran it, what it caught, the false findings it produced, its model calls and its estimated minutes.

Measured on the built-in corpus during the first G12 exact-execution replay (`uv run lc dev eval review gate --mode enforce`, 2026-10-06), against the prior P13-EVAL estimate-only baseline:

| Selection | Catch | Behavior | Noise | Compute minutes / catch | Model calls / catch | Cost / Review (reported) |
| --- | --- | --- | --- | --- | --- | --- |
| quick | 86% (6/7) | 100% (6/6) | 0% (0/6) | 1.13 = 13.6/12 weighted catches (baseline 0.12), +827.3% | 0.50 = 6/12 (baseline 0.67), -25.0% | 0.85 min, 0.375 calls |
| auto | 86% (6/7) | 100% (7/7) | 14% (1/7) | 1.05 = 13.6/13 (baseline 0.12), +755.9% | 0.85 = 11/13 (baseline 1.00), -15.4% | 0.85 min, 0.6875 calls |
| thorough | 100% (7/7) | 100% (9/9) | 12% (1/8) | 9.60 = 153.6/16 (baseline 0.11), +8627.3% | 0.875 = 14/16 (baseline 1.50), -41.7% | 9.60 min, 0.875 calls |
| deep | 100% (7/7) | 100% (11/11) | 12% (1/8) | 11.20 = 201.6/18 (baseline 0.11), +10081.8% | 0.778 = 14/18 (baseline 1.50), -48.1% | 12.60 min, 0.875 calls |
| custom:static-only | 0% (0/7) | 100% (5/5) | 0% (0/0) | 0.32 = 1.6/5 (baseline 0.22), +45.5% | 0 (baseline 0) | 0.10 min, 0 calls |

The built-in fixtures come from the deterministic scripted reviewer plus recorded execution outputs, so these numbers measure the product pipeline rather than provider variance. The five execution-only cases are caught by `checks.test`, `behavior.test_diff`, `side_effects.runtime`, `migrations.execute` and `compat.mixed_version`; the two pre-existing execution-relevant cases also have execution fixtures. Product-side call pruning removes synthesis when one deterministic candidate already gives a complete hypothesis, and Quick defers model investigation rather than arbitrarily covering only one of several independent candidate targets. Replay also honors trusted-base `ai_content_access`, so the SQL execution-only cases can prove deterministic execution value while model access is denied. No selection loses a defect or behavior catch, and every AI depth stays within the +10% model-calls-per-catch rule.

Those first exact-execution compute-minutes-per-catch values exceeded the prior estimate-only baseline, so G12-COST initially blocked exactly as intended. On 2026-10-06 the owner explicitly accepted that one-time measurement transition and recorded these values as the new baseline. Gate I now **PASSes** in enforce mode; the threshold, weights and owner approval remain unchanged, so any future cost-per-weighted-catch rise above 10% still blocks.

## Fixtures (CI mode)

Model calls are keyed by stage, driver, model, and the digests of the system prompt, output schema and full prompt. Execution calls live beside them in `<case>.execution.json` and are keyed by entry id, side, recipe/analysis digest, request variant and the case-input digest. Replay answers only exact keys. Anything else is reported and never reused:

- `missing`: no fixture exists for the case/stage/execution request, or its exact execution key was not recorded;
- `stale`: case inputs, model/prompt/schema identity, or an execution fixture's case-input identity changed.

A stale or missing model or execution fixture leaves its Review incomplete, so `run` exits 1 and Gate I reports the fixture event. Replay never falls back to executing reviewed code.

`record --synthetic` re-authors model fixtures with the scripted reviewer and deterministic execution fixtures from each case's `execution:` block; recording the same inputs again produces byte-identical files. `record --synthetic --execute` (or `record --live --execute`) replaces only the execution authoring source with the real local product runner so Bubblewrap can record exact side outputs once. `--execute` is opt-in and is refused in CI and under pytest. `record --live` calls the provider configured as the gate reviewer, requires `--max-calls` (and optionally `--max-usd`), and stops at the first failure or budget exhaustion. A case whose recording fails keeps its old fixtures, which then replay as stale.

## Gate I

`gate` replays the corpus, then compares each selection's numbers with `baseline.json`:

| Status | Meaning |
| --- | --- |
| `pass` | identical to the recorded baseline |
| `pass_unrecorded` | within thresholds, but the numbers or the digest set changed; `--update-baseline` records a gate result for it |
| `blocked` | a threshold is breached and no valid acceptance covers it |
| `accepted` | breached, and a valid acceptance record covers exactly this result |
| `stale` | a fixture is stale or missing, so nothing could be measured |
| `no_baseline` | nothing recorded yet |

The digest set names what a measurement was taken against, and the gate reports which parts changed since the baseline:

- `model`: the replay reviewer and every identity the fixtures were recorded with;
- `prompt`: prompt and schema versions, system prompts and output schemas;
- `policy_default`: default run settings, budget defaults, minute estimates, the finding threshold and severity mapping;
- `preset`: the Quick, Auto, Thorough and Deep selections, the capability catalog and AI effort profiles;
- `corpus`, `fixtures`, `replay`: the cases, the recorded outputs, the selections and matching rules.

A regressed result never becomes the baseline without an acceptance, in either mode.

### Thresholds (approved by the product owner, 2026-10-06)

| Threshold | Approved value |
| --- | --- |
| `catch_rate_drop_max` | 0.0: any drop in a selection's catch rate blocks |
| `behavior_catch_rate_drop_max` | 0.0: any drop in its behavior catch rate blocks |
| `lost_catches_max` | 0: any previously caught expectation that is now missed blocks |
| `noise_rate_rise_max` | 0.05: noise may rise by at most 5 points |
| `cost_per_catch_rise_max` | 0.10: cost per weighted catch may rise by at most 10%, separately in compute minutes, model calls and known provider dollars |
| `defect_catch_weight` | 1.0: weight of a caught defect in weighted catches (must be positive) |
| `behavior_catch_weight` | 1.0: weight of a caught behavior delta in weighted catches (must be positive) |

A selection passes only when its catch rate and behavior catch rate do not drop, no catch is lost, noise rises by at most 0.05 and cost per catch rises by at most `cost_per_catch_rise_max`. A selection removed from the replay also blocks. `gate.yaml` may select `mode: enforce` only while the thresholds are approved. It stays in `mode: report`, where the gate is visible and never blocks, so CI does not block on it; `--mode enforce` on the command line enforces for one run (the release gate uses it). `authorized_owners` names who may accept a trade-off.

#### Approval is bound to the values

The thresholds count as approved only when `thresholds.approval` has `status: approved`, `approved_by`, `approved_at`, `thresholds` (every threshold and weight as approved) and `thresholds_digest` (the digest of those values), and the digest matches the thresholds now in `gate.yaml`. Values are compared as numbers, so `0.1` and `0.10` are the same. Editing any threshold or weight after approval makes them unapproved: the gate notes it, and `mode: enforce` in `gate.yaml` is refused with a message that names each changed key with its approved and current value and prints the values and digest to re-approve. An approval with no `thresholds_digest` counts as unapproved too.

The product owner approved the proposed values on 2026-10-06. The checked-in approval block records them with their digest.

#### Cost per catch

Cost per catch is checked per selection in three units, each with the rules below: planned compute minutes and model calls, which the harness always counts, and provider dollars. Absolute cost per Review (compute minutes, model calls, provider dollars) is printed beside the result as `cost/review ... (reported, not gated)` and is in the JSON as `cost_per_review_changes`; it never blocks by itself.

| Case | Rule |
| --- | --- |
| both sides caught something and spent something | the relative rise in cost per catch must be at most `cost_per_catch_rise_max` (exactly 10% passes) |
| nothing spent now | passes |
| cost now with no catch | cost per catch is infinite and blocks, unless the baseline also caught nothing and the cost did not rise |
| the baseline caught nothing but spent something | its cost per catch is infinite, so any catch now is an improvement |
| the baseline spent nothing (with or without catches) | any cost now blocks as a rise from zero |
| compute minutes or model calls unknown on either side | blocks: a plan estimate was not a finite, non-negative number, or the total is missing. Unknown cost is never counted as zero |
| provider dollars unknown on either side | blocks: a provider-API call reported no cost, or a cost in another currency or with no currency. Unknown dollars are never counted as zero |
| provider dollars reported on neither side | dollars are not compared (the gate says so in a note); compute minutes and model calls carry the rule |

Local model calls (CLI subscriptions, Ollama) have no provider bill: they count as model calls, never as unknown dollars.

On the built-in corpus, G12's first honest execution measurement exceeded the previous estimate-only compute baseline while every AI selection passed catch, lost-catch, noise and model-calls-per-catch thresholds. On 2026-10-06 the owner accepted that one-time measurement transition and recorded it as the new baseline. Gate I now passes with the same approved 10% future cost-per-weighted-catch regression limit; the acceptance does not loosen future thresholds.

The flat `cost_per_review_rise_max` threshold was removed on 2026-10-05. A `gate.yaml` that still sets it is refused with a message naming the replacement, so an approved flat rule is never reinterpreted as a cost-per-catch threshold; the product owner approves the new thresholds again.

The checked-in `baseline.json` predates cost per catch. It was migrated once by replaying its own baseline path (commit `24b219fb6`: code, corpus and fixtures) with the cost-per-catch metrics. The digest set and every recorded number were unchanged, `behavior_caught` and `cost_total` were added, and its `migration` field records this. A baseline without those fields fails closed (`cost_per_catch`, "predates cost per catch") until a new baseline is recorded.

### Accepting a trade-off

`accept --owner NAME --reason TEXT` records `acceptances/<time>-<snapshot>.yaml`. The record holds the owner, who recorded it, the reason, the thresholds, the baseline and current snapshot digests, the changed and full digest sets, the breaches, and the measured baseline and current numbers. The gate accepts it only when the owner is in `authorized_owners`, a reason is present, it targets this exact baseline and snapshot, and its numbers and breaches equal the measured ones. An edited record is reported invalid. Commit the acceptance, then run `gate --update-baseline` to record the accepted result (`decision: accepted`, with a link to the acceptance).

## Gate H: behavior evidence quality

Gate H decides whether a result LemonCrow executed (a check recipe verdict, a kind entry such as `checks.test`, a browser or visual result) may count for readiness. Such a result can satisfy or block a readiness condition, or feed auto-approval, only after its recipe or surface has been qualified from measured runs:

- **flake rate**: repeat runs on fixed inputs must agree;
- **classification accuracy**: the R5 base/head classifier must agree with labeled ground truth;
- **visual determinism**: two renders of one input must be identical.

Qualification is per repository and per subject. A recipe subject is its `recipe_digest` plus the environment class (`<executor_class>@<image_digest>`) its result ran in, and the classifier for its report format. A surface subject is its R7 render digest plus environment class. Every LemonCrow-executed result reads as one of:

| State | Meaning |
| --- | --- |
| `qualified` | measured within the thresholds for this repository, digest and environment class |
| `advisory` | measured and outside them (`flaky`, `classification_inaccurate`, `visual_nondeterministic`) or measured too little (`insufficient_measurement`) |
| `unmeasured` | no measurement for this subject (`unmeasured`), the measurement could not run (`measurement_unavailable`), or the result names no digest or environment (`unidentified`) |

CI, provider, reported and human evidence are not Gate H subjects and are never affected.

### Modes

`evals/review/gate-h.yaml` is the Gate H contract: the mode, the thresholds and their approval, mirroring Gate I. Readiness reads it from a byte-identical packaged copy, `src/lemoncrow/pro/capabilities/review/gate_h_contract.yaml`, which ships as package data in the wheel, the Review engine (`server/review-engine-files.txt`; the engine build refuses a wheel without it and resolves it in a clean venv) and the hosted server. An installed package therefore evaluates the same contract as a checkout, and a Gate H diagnostic is `unknown` only when that contract is missing or invalid. Edit the checked-in file, then `cp evals/review/gate-h.yaml src/lemoncrow/pro/capabilities/review/gate_h_contract.yaml`; `uv run python -m scripts.release_gate --validate` fails while they differ.

The modes:

- `report` (the default): readiness, its conditions and its digest are unchanged. Every condition a LemonCrow-executed result feeds carries `evidence_qualification` with what enforcement would do (`would_be_advisory`, `would_be`). That field is outside the readiness digest. The Checks rail shows "Would be advisory under Gate H enforcement".
- `enforce`: an unqualified or unmeasured result is shown but neither satisfies nor blocks. When only advisory results exist, `verification` is `not_applicable` with reason `advisory_only`. A required browser scenario with only an advisory result stays pending. An organization-required check met only by advisory evidence is `pending` with reason `advisory_evidence`. Behavior targets from advisory producers are not counted as open. Auto-approval keys on readiness, so advisory results never feed it. The Checks rail shows "Advisory, not counted for readiness" and the reason. There is no new control.

`enforce` takes effect only when the thresholds are approved: `thresholds.approval.status` is `approved` (with `approved_by` and `approved_at`) and `thresholds_digest` equals the digest of the current threshold values. An unreadable or invalid contract, or `enforce` without a valid approval, falls back to `report` mode with the built-in defaults, so a broken switch never enforces.

### Commands

```bash
uv run lc dev eval review gate-h run                     # measure every corpus repository (all parts)
uv run lc dev eval review gate-h run --repository synthetic-calc --part visual
uv run lc dev eval review gate-h report                  # qualification per repository vs baseline.json
uv run lc dev eval review gate-h run --results-dir DIR   # measure into DIR (release qualification); report --results-dir DIR reads it
uv run lc dev eval review gate-h record                  # write baseline.json from the results
uv run lc dev eval review gate-h import --review REVIEW  # store results for that Review's repository
```

`run` writes one results document per repository to `evals/review/gate-h/results/<id>.json`. Each document holds:

- the corpus digest, the executor and the recipe digests;
- every measurement, keyed by metric, digest and environment class, with its fingerprints;
- per-input run statuses;
- per-case labeled units with the predicted class and a confusion matrix;
- per-pair render differences;
- runs and seconds spent.

`run --part X` rewrites that repository's document with only that part. `import` merges the chosen results documents (counts add up per key) and stores them as `review_derived` rows (kind `evidence_qualification`) on the Review's latest Snapshot, scoped to its repository. Readiness reads those rows. One import is capped at 256 measurements, and the store keeps the newest 512 measurement rows (least recently used are evicted first). Rows are deleted with their Snapshot (foreign-key cascade). They qualify every Review of that repository. Hosted repositories are keyed `hosted:<org>:<repo>`. `import` writes only to a local store.

### Corpus

`evals/review/gate-h/corpus/<id>/repository.yaml` describes one repository:

- **Source.** `kind: synthetic` (files and seeded commits embedded in the file) or `kind: checkout` (named revisions of this source checkout, a path subset, plus seeded edits).
- **Executor.** `executor: sandbox` (the bubblewrap command sandbox, the same environment class as a local product result) or `executor: host` (unsandboxed, class `host_unsandboxed@...`, which never matches, and so never qualifies, a sandboxed product result).
- **Runs.** `runs` and `reruns` (the R5 attempt policy).
- **What it measures.** `recipes`, `flake` (inputs per recipe), labeled `cases` (`base`, `head`, `labels` per unit, `@recipe` for the recipe verdict) and `surfaces` (an R7 render config and `pairs`).
- **What each subject is for.** `expectations` types every recipe (`checks.recipe:<id>`) and surface (`surface:<id>`): `qualify` (meant to qualify, counts as qualifying evidence), `advisory` (cannot honestly qualify yet; `reason` and `why` say so) or `negative_control` (flaky or nondeterministic on purpose; `reason` names what must disqualify it). A negative control may name only `flaky`, `visual_nondeterministic` or `classification_inaccurate`: `insufficient_measurement` is refused, because a control "met" by never being measured proves nothing. `report` checks the union of the corpus and the results against these types: a negative control that qualifies, or is disqualified for another reason, a `qualify` subject that does not qualify, an `advisory` one that starts to, a measured subject nobody typed, a typed subject with no measurement (`no_measurement`), a measured repository the corpus no longer holds (`no_corpus`) and a corpus repository with no results (`no_results`) are `expectation_violations`. The text report prints `expectations checked: yes|no`, each violation, and exits 1 when there are any; `--json` exits 1 on violations only with `--strict`, because `release_gate` reads the JSON and does not take violations into account. A manifest with no `expectations` loads untyped and every subject is reported as a violation. `expectations` is not part of the corpus digest (`corpus_digest` hashes the manifest without that block): typing a subject does not change what was measured, so it never makes recorded results look stale; a test holds every committed results and baseline digest equal to its manifest's.

Two corpora ship:

- `synthetic-calc` (sandbox): a stdlib test runner that writes JUnit or jest-JSON, a recipe-level lint check, a random `coin` recipe, a page whose clock R7 freezes (`Date`, `Math.random`), and a page it does not (`performance.now`, `crypto.randomUUID`). `py-tests`, `py-tests-jest` and `frozen-clock-page` are typed `qualify`; `coin` (`flaky`) and `performance-clock-page` (`visual_nondeterministic`) are negative controls, never targets to turn green; `lint` is typed `advisory` (`insufficient_measurement`): a recipe-level classifier yields one labeled unit per base/head pair, so 60 units need 60 distinct seeded pairs, and padding with relabeled copies would qualify lint without measuring anything new.
- `lemoncrow-dogfood` (host): two pytest files and one vitest file of this repository at two pinned revisions, plus seeded edits that break one test of each.

### Thresholds (approved by the product owner, 2026-10-06)

The approval names `approved_by` and `approved_at` and, like Gate I's, is bound to the values: `thresholds.approval.thresholds` holds the approved values and `thresholds_digest` their digest (`QualificationThresholds.digest`, the `thresholds_digest` a baseline records). Editing any threshold, tighter or looser, makes the thresholds unapproved until the owner re-approves: `report` and the release gate then say which value changed, and `mode: enforce` is refused with the values and digest to re-approve. If `thresholds.approval.thresholds` is present it must also equal the live values, so a tampered map under a matching digest reads unapproved. The digest is integrity, not authorization, exactly as in Gate I: `approved_by` and `approved_at` are free text and whoever can edit the file can write a digest; what it guarantees is that an edit made without rewriting the approval is visible, and the review of the commit that rewrites it is the authorization. A repository's tightening of the floor does not unapprove it. The approval recorded on 2026-10-07 carries the digest of the values approved on 2026-10-06 without changing any of them. Enforcement is a separate switch: `gate-h.yaml` stays in `mode: report`.

A repository may tighten these in its trusted-base `.lemoncrow/review.yaml` under `check_policy.qualification`. A `*_max` key can only go lower and a `*_min` or `min_*` key can only go higher. A looser value is ignored and named in the qualification notes, and the mode cannot be overridden.

| Threshold | Approved | Why |
| --- | --- | --- |
| `flake_rate_max` | 0.0 | Every measured deterministic recipe agreed on every run. Any disagreement is a real defect in the evidence, and a tolerance would let a known-flaky recipe gate. |
| `min_runs_per_input` | 10 | With 2 inputs, 10 runs each catch a 10%-per-run flake with about 88% probability (5 runs: about 65%). |
| `min_flake_inputs` | 2 | A base and a head input, at least. |
| `classification_accuracy_min` | 0.95 | The R5 classifier made 0 errors in 296 labeled units. 95% is room for report-format edge cases, not for systematic misclassification. |
| `min_labeled_units` | 60 | 0 errors in 60 units bound the true error rate below 5% at 95% confidence (one-sided bound `1 - 0.05^(1/n)`; 20 units would bound it only below 14%). |
| `visual_nondeterminism_max` | 0.0 | A surface that renders the same input differently cannot attribute a difference to the change. |
| `min_render_pairs` | 5 | Catches a surface that differs on 30% of renders with about 83% probability (3 pairs: about 66%). |

### Measured (this host, `evals/review/gate-h/results/`: `lemoncrow-dogfood` 2026-10-04, `synthetic-calc` 2026-10-07)

| Repository | Measurement | Result |
| --- | --- | --- |
| `lemoncrow-dogfood` (host, 56 executions, 127.1 s) | flake `pytest-review-subset` (51 tests) | 0/2 inputs disagreed, 20 runs |
| | flake `vitest-check-recipes-panel` (8 tests) | 0/2 inputs disagreed, 20 runs |
| | classifier `junit`, 4 cases | 0 wrong of 174 units (168 unchanged, 4 introduced, 2 fixed) |
| `synthetic-calc` (sandbox, 240 executions, 161.0 s) | flake `py-tests`, `py-tests-jest`, `lint` | 0/4, 0/2, 0/2 inputs disagreed, 10 runs each |
| | flake `coin` (random by design) | 2/2 inputs disagreed |
| | classifiers `junit` and `jest-json`, 13 cases | 1 wrong of 75 units each (the `flaky-test` case's coin-flip label) |
| | classifier `recipe` (recipe-level verdicts) | 0 wrong of 4 units |
| | visual `frozen-clock-page` | 0/5 render pairs differed |
| | visual `performance-clock-page` | 5/5 pairs differed (DOM and screenshot) |

Under these floors the baseline qualifies both dogfood recipes, `py-tests`, `py-tests-jest` and the frozen-clock page (two seeded `sub` cases, `introduced-failure-sub` and `fixed-sub`, took each classifier from 59 to 75 units). It marks the following as `advisory`:

- `coin` (`flaky`, a negative control);
- `performance-clock-page` (`visual_nondeterministic`, a negative control);
- `lint` (`insufficient_measurement`, 4 of 60 labeled units, typed `advisory`).

The `flaky-test` case labels `test_coin` as `flaky`, but a coin that passes every attempt of R5's three is classified `new` or `introduced_failure`, so that one unit is right about 75% of the time. The 2026-10-04 run happened to get it right (0 of 59); 2026-10-07 did not (1 of 75, 98.7% accuracy). One wrong unit cannot cross the 95% floor at 75 units (3 are allowed), so qualification does not depend on the draw, but the committed confusion matrix does.

The statistical rationale for `min_labeled_units: 60` ("0 errors in 60 units bound the true error rate below 5% at 95% confidence") holds for a clean run only. With one error the one-sided 95% Clopper-Pearson upper bound is 6.17% at 75 units; one error stays below 5% only from 93 units. The qualifying synthetic classifiers (1 of 75) therefore meet the approved accuracy floor of 95%, but not the 5% confidence bound the 60-unit figure was chosen for. No threshold was changed; the owner may want the floor, or the corpus, revisited before enforcement.

Importing both results documents into one repository merges the `junit` classifier to 233 units.

The flake measurement runs each input `runs` times as independent single attempts, with no reruns. It compares the recipe status and every per-test status, and an input is flaky when any two runs differ. A flake rarer than about 1 in 10 runs per input can go undetected at the proposed floor. Lockfile and toolchain are recorded but are not part of the environment class.

### Moving readiness to enforce

Recommendation (RC1, 2026-10-07): keep `mode: report`. Do not switch to `enforce` yet.

Evidence:

- The only subjects with measurements are the corpus recipes, keyed by their digests and the sandbox or host environment class: the two dogfood recipes, the three qualifying synthetic subjects and the frozen-clock page. No recipe or surface of a user's repository is measured, because the only writer of measurement rows is the development command `lc dev eval review gate-h import`, which writes a local store from a corpus under `evals/review/gate-h/corpus/`. There is no hosted import path.
- Under `enforce` an unmeasured result is `advisory`: shown, neither satisfying nor blocking, and never feeding auto-approval. Every LemonCrow-executed check, kind entry and visual result of every repository other than this one would therefore stop counting toward `verification`, behavior targets and organization-required checks (`advisory_only`, `advisory_evidence`). Flipping the switch today is a product-wide loss of executed-evidence readiness, not a hardening.
- The release rule is unaffected: it accepts an approved, complete, unchanged baseline in either mode.

What RC1 settled, so the switch is safe once it is wanted: approval is bound to the threshold digest (a changed threshold is unapproved and `enforce` is refused); the contract readiness evaluates is package data in every install, so a hosted server or wheel can no longer be in a different mode from the release that measured it; corpus subjects are typed and `report` names every violation; the qualifying synthetic subjects carry the 60 labeled units.

Switch to `enforce` when all of these hold:

1. A supported way to measure a repository's own recipes and surfaces exists locally and hosted, writing measurement rows keyed by that repository's digests and environment class. Without it `enforce` qualifies nothing.
2. A report-mode soak on real reviews shows what enforcement would do: the count of conditions carrying `would_be_advisory` (`evidence_qualification.would_be_advisory` and the summary's `would_be_advisory`) against the conditions they feed. Proposed bar for the owner to set: at least 50 reviews across 3 repositories over 14 days, and no condition that is satisfied only by evidence that would be advisory in a repository that has opted to be measured.
3. `lc dev eval review gate-h report` shows `expectation_violations: []`, no change against the baseline, and `thresholds_changed_since_baseline: false`.
4. The owner re-approves if any threshold was edited meanwhile (the digest makes this visible), and decides whether `lint`-style recipe-level classification needs a real corpus before lint results can count.
5. The switch is one reviewed commit that changes `mode` in `evals/review/gate-h.yaml` and its packaged copy together (`release_gate --validate` holds them equal), changes `scripts/build_review_engine.sh`, whose clean-venv smoke test asserts `gate_h.mode == "report"` (the engine build fails otherwise), and moves `docs/status/capabilities.toml` `gate-h` out of `disabled_by_default`.

Open design choice for the owner: a global switch (today) or a repository opt-in through the trusted-base `check_policy.qualification` section. Opt-in would let measured repositories enforce while the rest stay in report mode; it needs its own review because a repository could otherwise opt out of enforcement.
