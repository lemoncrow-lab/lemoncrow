# LemonCrow Review Reader — Execution Tracker

> Canonical product spec: `docs/planning/2026-09-12-review-reader-spec.md`
>
> Rule: implement and qualify one phase before depending on it in the next. Existing durable review state remains authoritative throughout the migration.

## R20 — Target model — COMPLETE

### Plan
1. Add a pure reader projection over raw `ReviewUnit`s; do not add target persistence.
2. Assign captured changed spans exactly once: deepest trustworthy symbol first, otherwise hunk, otherwise file fallback.
3. Derive target state from the current frontier and expose target progress/outline.
4. Add an authenticated `GET /api/reviews/{id}/targets` endpoint while leaving legacy file groups intact.
5. Qualify nested definitions, mixed symbol/hunk ownership, multi-hunk symbols, missing patch bodies, binaries, ambiguity, and progress denominator behavior.

### Implementation
- `src/lemoncrow/pro/capabilities/review/targets.py`
- additive target/progress/outline fields in the review overview
- `GET /api/reviews/{id}/targets?order=recommended|file`
- `tests/gateway/test_review_targets.py`
- API contract tests

### Decisions
- Raw file/hunk/symbol units remain the durable identity/fingerprint substrate.
- `ReviewTarget` is deterministic and never stored.
- Target spans are side-specific (`old`/`new`) and may cover several hunks for one symbol.
- Missing/ambiguous semantic precision falls back rather than guessing.

### Gate result
PASS. Changed text in the R20 fixtures is covered once, while overlapping units are not double-counted as human progress.

## R21 — Target-based marking — COMPLETE

### Plan
1. Mark a target by its underlying unit key.
2. Return the changed target, target progress, and affected outline row from mark calls.
3. Generalize bulk close-out to current eligible targets and reject arbitrary raw non-target units.
4. Expose target counts in the CLI.
5. Bridge the legacy browser so `r` acts on an explicitly visible symbol/hunk target instead of silently marking the file.

### Implementation
- target-aware mark response in `api.py`
- target-aware bulk marking with current-revision eligibility checks
- frontend `ReviewTarget`, `ReviewProgress`, `ReviewTargetList`, and `fetchTargets`
- transitional active-target strip in `ReviewWorkspace`
- CLI now distinguishes `files`, `targets`, and `raw units`

### Compatibility
Legacy file groups and the old file close-out path remain only as a migration bridge. They are not the reader progress denominator and are scheduled for deletion in R26.

### Gate result
PASS. Browser `r` posts the active symbol/hunk unit key when target metadata is present; target progress updates independently of file approval.

### Qualification
- backend/CLI focused suite: 156 passed
- review frontend focused suite: 45 passed
- TypeScript: pass
- production frontend build: pass
- Ruff: pass
- mypyc compile-safety: pass

## R22 — Continuous reader shell — COMPLETE

### Implementation
- `/review` now renders `ReviewReader`, not selected-file `ReviewWorkspace`.
- one virtualized multi-file `@pierre/diffs` stream;
- compact outline and header;
- target/file keyboard navigation;
- target-level actions embedded at review boundaries;
- progressive file loading and focus mode.

### Gate result
PASS. The synthetic 30-target review can be traversed and completed without replacing the selected file page because there is no selected-file page anymore.

## R23 — Context drawer — COMPLETE

### Implementation
- Context closed by default;
- overlay mode by default, explicit pinning with tab-scoped persistence;
- task tabs: Impact / Checks / Evidence / Author / Discussion;
- exact signal → exact tab navigation;
- related-source peek preserves reader position.

### Gate result
PASS. Context opens over the reader and closing it immediately restores full diff width.

## R24 — Comments + feedback semantics — COMPLETE

### Implementation
- new-comment dispositions are Comment / Request change / Suggestion;
- legacy `looks_good` remains render-only compatibility;
- root Request change can atomically mark the current target `needs_changes`;
- folded raw-symbol comments are attributed back to their owning human target by disjoint target spans;
- author-addressed comments remain human-owned re-review work;
- exact-Claude delivery remains an explicit action after previewing feedback.

### Gate result
PASS. Request-change state and comment persistence stay consistent across reload/revision projection and author responses cannot resolve human judgment.

## R25 — Revision-delta experience — COMPLETE

### Implementation
- refresh returns target-level preserved / reopened / added / removed / active queues;
- rename aliases preserve target identity;
- preserved reviewed targets are excluded from the active re-review queue;
- best-target viewport restoration;
- discarded verdicts and hunk-reset explanations remain explicit;
- compact frozen-revision notification and delta bar.

### Gate result
PASS. Revision refresh preserves unchanged human judgments, reopens changed targets, and explains the complete active queue without forcing a full reread.

## R26 — Finish review + cleanup — COMPLETE

### Implementation
- completion is derived from `ReviewTarget`, not file units or overlapping raw units;
- `GET /api/reviews/{id}/finish` previews the exact finish tally without mutating status;
- finish sheet explains the full target partition and current risk separately;
- POST finish records only the human's status choice and never emits an approval verdict;
- removed dead selected-file browser stack: `ReviewWorkspace`, Attention/Chapter lens panes, permanent `ContextPane`, legacy `DiffPane`, and their obsolete tests;
- shared drawer primitives moved into `ContextSections` and active diff theme primitives into `diffModel`.

### Gate result
PASS. On the real 540-target review, the finish sheet reported an exact 540-target partition, remained non-mutating while open, changed status only after explicit confirmation, and reopening restored the review to `open`.
## R27 — Performance + accessibility qualification — COMPLETE

### Implementation
- added a 500-file / 1,500-target browser fixture;
- large reviews load only five file patches initially and navigation prefetches a bounded three-file window around the destination;
- `ReviewStream` only hands loaded files to the virtualized `CodeView`;
- memoized ordered file paths for target navigation rather than rebuilding them on each keypress;
- secondary `ContextDrawer` and `FinishSheet` are lazy chunks;
- reduced-motion preference disables smooth programmatic diff scrolling;
- outline sections expose expansion state and active-file semantics;
- target actions have explicit accessible names;
- Context uses tablist/tab/tabpanel semantics and restores focus on close;
- Finish sheet takes focus on open and restores it to Finish review on close;
- source/revision status changes use polite live regions.

### Gate result
PASS. The 500-file / 1,500-target fixture fetches 5 patches initially; navigating to file 6 fetches only files 6–8. The real 540-target workspace also emitted exactly 5 initial patch requests. Keyboard `e` moves focus into Context and Escape returns it to the active target; Finish Review behaves the same and returns focus to its primary button.

### Qualification through R27
- review frontend suite: 102 passed;
- TypeScript: pass;
- production frontend build: pass;
- `ReviewReader`: 668.86 kB raw / 185.55 kB gzip;
- lazy `ContextDrawer`: 14.29 kB raw / 4.05 kB gzip;
- lazy `FinishSheet`: 5.90 kB raw / 2.03 kB gzip;
- real-browser large-review fetch/focus dogfood: pass.

## R28 — Dogfood release qualification — COMPLETE

### Dogfood setup
- created an isolated durable Review session for `HEAD..WORKDIR`, so qualification marks/comments could not contaminate the normal persistent review;
- initial self-review snapshot contained 102 files / 561 human review targets / 1,982 raw units;
- used only the new Review Reader for representative high-risk navigation, marking, Request change, revision refresh, Context, feedback, and Finish flows.

### Release blockers found and fixed
1. **Stale product truth in CLI/reference docs.** `lc review --help` and `docs/reference/cli.md` still described the deleted three-pane/file-review UI, obsolete shortcuts, and the old fragment behavior. Updated both surfaces and pinned the current continuous-reader contract in tests.
2. **Disjoint fallback work looked like one giant contiguous range.** A real `ReviewReader.tsx` fallback rendered as `Lines 1–877` even though it owned only uncovered regions between symbol targets. The reader now renders genuinely disjoint fallback ownership as `Uncovered changes · N regions`; ordinary old/new replacement spans remain normal hunk/line labels.
3. **Revision delta had an unexplained remainder.** A real refresh showed `553 need your eyes` but only named reopened/new/preserved counts. The bar now explicitly names `carried pending`, so the active queue is arithmetically explainable.
4. **Reconciliation trust concern investigated, not papered over.** Dogfood initially appeared to show a reopened target becoming reviewed without human action. Stored fingerprints proved revision 3 had exactly reverted to the original human-reviewed fingerprint, which is the intended contract. Added the missing three-revision regression proving `A reviewed → B changed → B unchanged` remains `changed_since_review`; exact `B → A` reversion remains validly reviewed.

### Real-reader acceptance evidence
- marked four representative targets through the browser, then changed one of them and refreshed;
- revision 2 produced exactly `3 preserved + 1 reopened` for those human judgments;
- created a real Request change in `RevisionDeltaBar.tsx`; Save atomically set the target to `needs_changes`;
- after another revision, the comment remained visible and reported `anchor: re-found by its text and both neighbours`;
- revision 3 explained its active queue as `561 need your eyes = 10 new + 551 carried pending`, with 3 preserved separately;
- feedback preview contained the live Request change, relocated-anchor method, impacted callers, and explicit unknowns;
- direct `Send to Claude` was correctly unavailable in the isolated session because exact author provenance was absent; the automated exact-Claude delivery path remains qualified;
- Finish preview on the live 565-target review reconciled exactly: 4 reviewed + 560 unreviewed + 1 needs changes, plus one open human comment; preview remained non-mutating and Escape left the review open;
- no representative review step required leaving LemonCrow for another diff viewer.

### Final qualification
- backend/CLI review qualification: 695 tests covered across two non-overlapping controlled chunks; the single presentation-expectation failure in chunk 1 was corrected and its focused test passes;
- remaining backend review chunk: 354/354 passed;
- review frontend suite: 105/105 passed;
- TypeScript: pass;
- production frontend build: pass;
- Ruff: pass;
- mypyc compile-safety: 3/3 passed;
- final real-browser Finish/feedback/comment-relocation dogfood: pass.

### Gate result
PASS. The self-review could explain every outstanding count and carried judgment, preserved unaffected human work across revisions, reopened genuinely changed work, retained and relocated human feedback, and completed the Finish/feedback flows without switching to another diff viewer.

## R29 — Real-world large-review performance hardening — COMPLETE

### Measurement fixture
- profiled LemonCrow itself over `HEAD~100..HEAD`: 444 changed files, 89,596 insertions, 1,359 deletions, and 90,951 changed lines;
- added `benchmarks/review/bench_reader.py` to separate capture/persistence cost from the deterministic reader projection;
- added `benchmarks/review/bench_reader_browser.py`, a dependency-free Chrome DevTools Protocol probe for real initial render, patch fetches, heap/DOM size, and distant navigation;
- kept the existing 500-file / 1,500-target synthetic browser fixture as the deterministic regression gate rather than turning machine-specific timing into a flaky unit test.

### Reader projection fixes
- index annotations by path once instead of scanning every annotation for every target;
- index packet symbol metadata by path once instead of rescanning the complete symbol list for every changed file;
- track each file's target-list start offset instead of repeatedly scanning/filtering the growing global target list for fallback detection;
- preserve folded micro-symbol annotation ownership and all existing target/fallback semantics.

### Impact-analysis capture fix
- `_astgrep_detect` now sends all candidate/language/pattern rules through one `AstGrepAdapter.scan()` process in the normal path instead of spawning a process per combination;
- rule-mode output is normalized back to repo-relative paths so touched-file exclusion remains identical to the legacy search path;
- malformed/unsupported rule batches fall back to the old tolerant per-pattern search, preserving detector coverage;
- batching behavior is pinned by a regression test, while the existing real-ast-grep impact suite verifies end-to-end semantics.

### Real measurements
No-impact durable review, 444 files / 1,412 raw units / 973 targets:
- capture + persist: ~2.3s;
- target projection: ~60–65ms median;
- outline: ~1ms; progress: ~0.1ms; unchanged revision delta: ~0.3ms.

Annotation stress, same 973-target review with 5,000 comments:
- before path indexing: ~455ms median target projection;
- after path indexing: ~60ms median, about 7.6x faster.

Full-intelligence durable review, 444 files / 6,884 raw units / ~4,172 targets:
- target projection before symbol indexing: ~431ms median;
- after symbol indexing: ~187–190ms median, about 2.3x faster;
- capture + persist after ast-grep batching: ~25.0s in the measured run, down from ~27.8–36.9s in the pre-batching runs;
- peak child RSS improved from roughly 3.69GiB to 3.45GiB, but full impact/code-intelligence capture remains the dominant large-review cost.

Real headless-Chrome reader, 444-file / 973-target review:
- reader ready: ~1.0–2.0s across warm/cold probe runs;
- exactly 5 initial patch requests (~113KB encoded patch payload);
- initial JS heap: ~8.6–9.3MB; ~5.1K DOM nodes;
- distant file navigation: ~55ms and only 1 additional patch request.

Pure reader-model stress at 50,000 synthetic targets:
- next target: ~4.1ms median;
- next file: ~2.5ms;
- grouping: ~7.4ms;
- search hit: ~6.7ms median.

### Remaining measured bottleneck
A profiled full-intelligence capture still spends most of its time upstream of the reader: changed-symbol parsing/index reconciliation plus roughly 200 individual call-graph name resolutions. There is currently no batch caller-resolution API in the code-intelligence engine. That should be treated as a separate engine-performance task rather than adding reader complexity or weakening impact evidence.

### Gate result
PASS. The continuous reader stays fetch-, DOM-, and navigation-bounded on a real 444-file review; deterministic target projection scales acceptably after removing accidental corpus rescans; and the remaining large-review latency is isolated to the impact/code-intelligence acquisition path rather than the browser review model.

## R30 — Large-review search and navigation — COMPLETE

### Implementation
- `/` now exits focus mode, reopens the outline, and focuses search reliably;
- search tokenizes whitespace and ANDs terms across path, symbol, label, target kind/state, and attention reasons without changing review order;
- already-loaded annotation title/body text participates in search without issuing another request, including folded line comments that map back to their owning human target;
- search shows target/file result counts plus per-file match counts;
- Enter / Shift+Enter and explicit next/previous controls move through matching targets with wraparound;
- Escape or the clear control returns to the unfiltered reader and restores outline focus;
- searching itself remains network-free: a distant patch is fetched only when the reviewer navigates to that result;
- closed outline sections expand transiently while searching so a hit cannot be hidden by prior section state.

### Large-review acceptance
- deterministic 500-file / 1,500-target fixture still starts with exactly five loaded patches;
- searching directly for target 1,499 narrows to one target / one file without fetching intervening files;
- Enter fetches only `src/large-499.py` and jumps to `large-target-1499`;
- comment-text search resolves to the target using the annotations already fetched with the review.

### Qualification
- focused search/navigation suite: 30/30 passed;
- complete review frontend suite: 108/108 passed;
- TypeScript: pass;
- production frontend build: pass;
- `ReviewReader` chunk: 672.65 kB raw / 186.79 kB gzip.

### Gate result
PASS. Large reviews can be searched and traversed directly without changing the canonical review order or violating the bounded patch-fetch contract.

## R31 — Batch caller qualification for large reviews — COMPLETE

### Implementation
- added `CodeContextEngine.tool_callers_batch()` for exact bare-name, depth-1 caller qualification against the existing local symbol/call-edge index;
- one batch resolves exact definitions, ambiguity, and caller nodes for many names without changing the scalar `tool_callers` surface;
- batch payloads keep the same compact target/related/ambiguity shape the review impact layer already consumes;
- same-named definitions remain explicitly ambiguous and therefore cannot lend their caller/usage counts to the wrong changed symbol;
- the review impact layer feature-detects the batch seam and falls back name-by-name for older engines, test doubles, malformed batch answers, or exact names the batch could not resolve;
- caller resolutions are still returned to the untouched-caller pass, so qualification and call-site expansion continue to share one answer.

### Real measurements
Full-intelligence durable review after R31, `HEAD~100..HEAD`:
- 446 changed files / 92,041 changed lines / 6,958 raw units / 4,205 review targets;
- capture + persist: ~18.2s in the measured run;
- previous R29 post-ast-grep-batching capture was ~25.0s, so caller batching removes roughly another 27% from large-review capture time;
- target projection remains ~193ms median, confirming the improvement is upstream rather than browser-side;
- peak child RSS stayed roughly flat (~3.44GiB), so this pass is a latency optimization, not a memory claim.

Current cProfile after batching:
- `_enrich`: ~1.7s cumulative, down from the earlier ~9s profile where serial name resolution dominated;
- the next dominant cost is `_changed_symbols` (~13.5s), primarily repeated definition/tree parsing rather than caller qualification.

### Qualification
- scalar-vs-batch ambiguity/caller equivalence regression: pass;
- batch-path enrichment regression: pass;
- full `test_review_impact.py` + `test_code_context.py`: 119/119 passed;
- mypyc compile-safety: 3/3 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass.

### Gate result
PASS. Large-review caller/usage qualification no longer performs hundreds of serial graph-tool resolutions, while the existing ambiguity guards and scalar fallback preserve review correctness.

## R32 — Parse-once changed-symbol analysis — COMPLETE

### Implementation
- `_deleted_symbols` now determines whether a modified file contains any pure-deletion work before parsing the old blob; ordinary replacements/additions no longer pay for an old-side symbol walk that cannot produce a deleted symbol;
- added one review-local Python definition analysis that derives module-level assignments, nested functions/classes, and every AST `end_lineno` body span from a single `ast.parse` / `ast.walk`;
- the Python definition set is pinned against the legacy tag extractor, including module constants, annotated assignments, nested definitions, and the deliberate exclusion of local assignments as standalone review symbols;
- `symbol_windows`, indexed-symbol mapping, and tree-sitter fallback mapping now reuse that same analysis instead of parsing the new blob once for tags and again for body ends;
- non-Python languages keep the existing tree-sitter/regex definition extraction and indentation/bracket body-window fallback unchanged;
- direct helper callers retain their previous contracts, so this is a hot-path reuse change rather than a new review model.

### Real measurements
Exact 447-file / 92,316-changed-line / 6,969-raw-unit / 4,213-target range:
- R31-era path with old-side parsing: ~18.2s on the preceding nearly-identical 446-file range;
- moving the pure-deletion gate ahead of old-side parsing: ~16.36s on the exact current range;
- parse-once Python definition/body analysis: ~13.07s on that same current range;
- parse-once itself removes about 20% from the exact-range 16.36s measurement, while the combined R32 path is roughly 28% below the R31-era large-review capture despite the range growing slightly;
- target count and raw-unit count stayed identical before/after parse-once, and peak child RSS remained roughly flat (~3.44GiB).

Post-change cProfile:
- `_changed_symbols`: ~7.5s cumulative, down from ~11.0s immediately before parse-once;
- Python `ast.walk` visits fell from roughly 2.45M to 1.23M in the profiled capture;
- `_index_symbols`: ~2.8s, down from ~6.2s;
- the next major costs are contract-literal analysis / parser work and the remaining base-side body comparison, rather than duplicate new-side parsing.

### Qualification
- Python definition-set/body-window parity regression: pass;
- review unit/fingerprint suite plus parse parity: 42/42 passed;
- complete review impact suite: 52/52 passed;
- mypyc compile-safety: 3/3 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass.

### Gate result
PASS. Review impact no longer parses the same changed Python blob twice just to derive definition identity and body geometry, and deletion-only analysis is skipped when the diff cannot contain a deleted symbol.

## R33 — Concurrent deterministic detector execution — COMPLETE

### Implementation
- the four independent deterministic impact detectors now execute in a four-worker `ThreadPoolExecutor` over the same immutable edit snapshot;
- results are still consumed in the original declaration order, so site ordering, source attribution order, and degradation naming are unchanged;
- each detector keeps its existing fail-open boundary: an exception marks only that detector degraded and cannot suppress the other three results;
- no detector logic, query shape, ast-grep pattern, text-search fallback, rarity gate, or evidence cap changed;
- broader detector-vs-symbol phase concurrency was deliberately not adopted because its measured gain was smaller and it would widen the shared-state surface unnecessarily.

### Rejected experiments before this implementation
- reusing the R32 Python AST for contract-literal extraction was rejected: a parity prototype reached exact behavior only by tokenizing AST source slices, which was slightly slower than the existing full tokenizer on the real corpus and introduced unnecessary complexity;
- batch text search was also rejected: on the real 38-query detector workload it was slower than scalar `search_text` and changed result ordering/coverage because the scalar API intentionally stops once the warmed index returns a result;
- both experiments were fully reverted before R33 qualification.

### Real measurements
Exact current `HEAD~100..HEAD` range, 446 changed files / 92,491 changed lines:
- paired full packet-capture A/B with fresh stores and alternating order;
- serial detector execution: 9.98s median (`9.76s`, `9.98s`, `10.23s`);
- concurrent detector execution: 8.46s median (`8.46s`, `8.90s`, `8.42s`);
- improvement: roughly 15% end-to-end on the exact same review;
- file count, changed-symbol count, impact-site count, raw review semantics, and final packet contents remained identical;
- an absolute `bench_reader.py` run remained noisier (~14.97s capture/persist on this machine), so the paired same-process A/B is the qualification measurement for this change rather than comparing unrelated absolute runs.

### Qualification
- concurrency/failure-semantics regression: pass;
- complete impact + incremental-impact + detector unit suites: 104/104 passed;
- mypyc compile-safety: 3/3 passed;
- forced serial-vs-concurrent packet equivalence: exact match for files, symbols, impact sites, and degraded signals;
- Ruff: pass;
- mypy: pass;
- Black: pass.

### Gate result
PASS. Independent detector acquisition overlaps safely and cuts large-review capture latency without changing any review evidence or degradation semantics.

## R34 — Uncovered-change fallback semantics — COMPLETE

### Implementation
- hunk fallback spans now use only exact `old_ranges/new_ranges` preserved by the diff producer; context-inclusive hunk header geometry is never promoted into changed-line progress spans;
- a missing/unparseable patch body may still produce a hunk target when exact changed ranges survive independently;
- if any hunk lacks both a trustworthy patch body and exact changed ranges, the entire file is promoted to one file target rather than exposing a partial target denominator;
- deletion-only blocks no longer borrow the surviving symbol at their new-side join point: without surviving changed lines there is no proof that the removed bytes belonged to that symbol, so the removed span stays on its hunk target;
- the canonical Review Reader spec now states both rules explicitly.

### Why this matters
- unified-diff `old_start/new_start + *_lines` spans include context, so using them as fallback progress geometry can charge unchanged code to the reviewer;
- a deletion join point identifies where removed text sat, but can point directly at the following function/class and falsely make that surviving symbol own unrelated removed content;
- falling back the whole file when one hunk is untrustworthy is intentionally conservative: a broad judgment is visible, while a partial denominator can incorrectly claim the file is fully reviewed.

### Real-repo projection
Current `HEAD~100..HEAD`, no-impact reader benchmark:
- 443 changed files / 90,932 changed lines / 1,391 raw units;
- 952 derived review targets;
- target derivation: ~60.7ms median across five runs;
- no target-count explosion or projection-performance regression from the stricter fallback policy.

### Qualification
- focused target derivation suite: 18/18 passed;
- target/API/annotation/revision/store backend suite: 203/203 passed;
- mypyc compile-safety: 3/3 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass.

### Gate result
PASS. Every review denominator now prefers conservative aggregation over invented line ownership: exact changed spans are partitioned once, pure deletions stay unclaimed by surviving symbols, and incomplete hunk geometry cannot silently disappear from progress.

## R35 — Reader order control — COMPLETE

### Implementation
- surfaced the backend's existing target-order projection in the reader's compact `⋯` menu;
- supported modes are `Recommended` and `File order`, matching the only two deterministic backend `TargetOrder` values currently available;
- switching order refetches only the target projection, keeps the same `target_id` active when it still exists, preserves marks/comments/search/delta state, and retains already-loaded patch details;
- the newly ordered stream preloads only its normal bounded initial file window plus the surviving active target when needed;
- revision refresh now requests targets using the current selected order instead of silently returning to Recommended;
- Commit order and Dependency order remain intentionally unavailable until target-level commit/dependency provenance exists; the canonical spec now says not to fabricate those client-side.

### UX semantics
- order is navigation only: it never mutates review marks, target identity, progress, or evidence;
- the control lives with other secondary reader actions rather than becoming another permanent toolbar;
- selecting the already-active order is a no-op;
- already-loaded files are reused, so a 30-target fully-loaded fixture performs zero additional patch fetches when changing order.

### Qualification
- order-switch regression preserves active target identity and loaded patches: pass;
- refresh-preserves-order regression: pass;
- focused `ReviewReader` suite: 18/18 passed;
- complete review frontend suite: 110/110 passed;
- TypeScript: pass;
- production frontend build: pass;
- `ReviewReader` chunk: 674.00 kB raw / 187.11 kB gzip.

### Gate result
PASS. The reader now exposes every ordering mode the backend can honestly prove, preserves that navigation choice across refreshes, and does not fabricate unsupported commit/dependency ordering.

## R36 — Safe file remainder completion — COMPLETE

### Implementation
- added the file-header `Review eligible remainder` action to the continuous stream, backed by the existing bulk-mark route rather than client-side state mutation;
- the reader always derives the submitted unit keys from the full current target list for that file, even when search or revision-delta focus narrows the visible stream;
- the client applies only server-confirmed `marked` keys locally and leaves every skipped target outstanding, with the server's refusal reasons surfaced when completion is partial;
- the server now accepts only keys that are current derived `ReviewTarget`s; the obsolete pre-R26 broad file-unit compatibility path was removed, so stale/raw overlapping units cannot become broad review judgments;
- bulk eligibility is re-evaluated against current state at request time and refuses changed/reviewed/needs-changes/unknown targets, high-attention targets, unknown fingerprints, and unresolved request-change threads;
- ordinary open comments remain compatible with bulk completion; only unresolved `request_change` threads block it;
- request-change ownership uses the same exact-unit-then-disjoint-span mapping as target annotation counts, so comments on folded micro-symbols still protect the enclosing human target.

### Verification boundary
- `ReviewTarget.verification` already has pass/fail/unknown fields and the bulk route refuses fail/unknown when those fields are populated;
- current `ReviewPacket.evidence` is review-wide and carries no target/path attribution, so R36 deliberately does not smear global check results across every target or claim target-level verification that cannot be proved;
- target/file verification attribution remains a separate follow-up capability before the verification refusal can fire from ordinary packet evidence.

### Qualification
- target-only bulk contract: stale/non-target ids refused, duplicate ids deduplicated, risky current targets preserved: pass;
- unresolved request-change vs ordinary-comment eligibility regressions: pass;
- folded micro-symbol request-change ownership regression: pass;
- search-narrowed file completion still submits the full file target remainder: pass;
- focused bulk/target backend regressions: pass;
- complete review frontend suite: 111/111 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass;
- mypyc compile-safety: 3/3 passed;
- TypeScript: pass;
- production frontend build: pass.

### Gate result
PASS. File-level convenience now remains target-authoritative end to end: the browser cannot hide work through search filtering, the server cannot sweep stale/raw units, and unresolved human change requests stay outside bulk completion.

## R37 — File-scoped target verification attribution — COMPLETE

### Implementation
- `ReviewTarget.verification` is now populated from persisted `ReviewEvidence` when evidence is explicitly scoped to the target's file and carries `PASS`, `FAIL`, `UNKNOWN`, or `NOT_RUN`;
- review-wide packet evidence and global artifacts remain review context and never become per-target verification claims;
- evidence freshness follows reviewed-code identity rather than strict revision-row identity, so evidence remains current across analysis-only revision rows that describe the same source;
- repeated file checks are deduplicated by title with the newest current artifact winning, matching the evidence store's newest-first ordering;
- `PASS` contributes to target pass count, `FAIL` to fail count, and `UNKNOWN`/`NOT_RUN` to unresolved count; artifacts without a verification status do not imply a pass;
- the review API now centralizes target projection through `_derive_targets(...)`, so overview, `/targets`, marks, comments, bulk completion, and revision delta all consume the same current evidence set;
- the file Context drawer's verification rows now use the same newest-result-wins rule instead of allowing an older current artifact with the same title to overwrite a newer one.

### Bulk-completion semantics
- a current file-scoped failed or unresolved check blocks bulk completion for targets in that file;
- a newer passing result for the same check title supersedes the prior failure and clears that verification blocker;
- review-wide failures do not block an otherwise eligible target because they do not prove that target failed verification.

### Qualification
- pure target verification projection: newest-per-title, file scope, global/no-status exclusion: pass;
- file FAIL → target fail count + bulk refusal; newer same-title PASS → target pass count + bulk acceptance: pass;
- file patch/context verification renders the same newest PASS used by target eligibility: pass;
- code-equivalent analysis-only revision keeps file evidence current for the target: pass;
- focused verification/bulk integration: 8/8 passed;
- complete `tests/gateway/test_review_*.py` suite: 618/618 passed;
- mypyc compile-safety: 3/3 passed;
- complete review frontend suite: 111/111 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass;
- TypeScript: pass;
- production frontend build: pass;
- `ReviewReader` chunk: 675.34 kB raw / 187.47 kB gzip.

### Gate result
PASS. Verification now has an honest target-level provenance boundary: only current evidence tied to the file can change target eligibility, newer reruns supersede older results deterministically, and global checks remain visibly review-wide rather than becoming fabricated per-target failures.

## R38 — Release qualification and acceptance closure — COMPLETE

### Current large-review dogfood
Current `HEAD~100..HEAD` on LemonCrow itself:
- 433 changed files;
- 91,042 changed lines (`89,787` insertions / `1,259` deletions);
- 6,805 raw review units;
- 4,089 human review targets;
- full capture/persist with impact: ~13.10s;
- target derivation: ~181.2ms median / ~186.0ms p95.

Headless browser probe on the same durable review:
- reader ready: ~1.77s;
- initial patch requests: exactly 5;
- initial DOM nodes: 5,085;
- initial JS heap used: ~10.2MiB;
- distant navigation to `tests/gateway/test_review_end_to_end.py`: ~79ms;
- distant navigation added exactly one patch request (5 → 6), not an eager load of intervening files;
- post-navigation DOM nodes: 6,439;
- post-navigation JS heap used: ~20.7MiB.

### Regression qualification carried into this release gate
- complete review backend suite: 618/618 passed;
- mypyc compile-safety: 3/3 passed;
- complete review frontend suite: 111/111 passed;
- TypeScript: pass;
- production frontend build: pass;
- Ruff / mypy / Black: pass.

### Acceptance closure
- every objective product acceptance criterion in canonical spec §38 is now checked against implemented behavior/tests;
- the product still refuses to invent unsupported Commit order / Dependency order until trustworthy provenance exists;
- the subjective senior-engineer dogfood questions in §39 remain the actual human release bar; they are not auto-certified by tests or benchmarks.

### Gate result
PASS for objective product acceptance and engineering qualification. The remaining release decision is the explicit human dogfood question: whether a senior engineer would choose this reader over GitHub/IDE diff for the next agent-generated change.

## R39 — Senior-engineer dogfood ergonomics — COMPLETE

### Dogfood findings
The §39 pass found two concrete spec-to-product mismatches that would create avoidable operating friction for an experienced reviewer:
- the canonical keyboard model required `p` for actions and `?` for shortcut discovery, but the reader implemented neither;
- the canonical orientation model required the one-line change story to open a compact overview, but the story was static text and no overview surface existed.

Manual-scroll ownership was also re-audited during this pass: `ReviewStream` already tracks the active target from the CodeView scroll position against a stable viewport threshold, so judgment shortcuts continue to apply to the code actually being read. No change was needed there.

### Implementation — actions and shortcut discovery
- `p` now opens a searchable Review actions palette backed by the reader's existing callbacks rather than a parallel action model;
- palette actions include search, reviewed/reopen/needs-changes/comment, Context, focus mode, diff style, refresh, feedback preview, finish/reopen, ordering, change overview, and shortcut help;
- `↑/↓` changes the selected action and `Enter` executes it;
- `?` opens the complete shortcut reference required by canonical §16.1;
- the existing `⋯` menu now exposes Keyboard shortcuts so discovery does not depend on already knowing `?`;
- Esc closes both transient surfaces and restores reviewer focus;
- action/shortcut dialogs are lazy-loaded and do not become permanent reader chrome;
- modal stacking is prevented: command shortcuts do not open over Finish Review or Change Overview.

### Implementation — expandable change overview
- the header change story is now a clickable `Open change overview` control;
- the lazy overview sheet shows major conceptual changes, verification summary, author provenance, and current/stale evidence counts;
- each major change now carries an honest `target_count` projected from the already-derived non-overlapping `ReviewTarget` list rather than relabeling `attention_count` as targets;
- major-change rows show file count, target count, elevated-file count, and first review path;
- selecting a major change closes the sheet and jumps directly into its first file in the continuous stream;
- the overview remains useful when no commit chapters exist: verification/provenance/evidence still render without inventing major-change rows.

### Qualification
- deterministic review-brief target-count regression: pass;
- focused backend evidence/brief qualification: 5/5 passed;
- complete gateway review backend suite: 618/618 passed;
- Ruff: pass;
- mypy: pass;
- Black: pass;
- focused palette/reader interaction suite: 25/25 passed;
- complete review frontend suite: 117/117 passed across 13 files;
- TypeScript: pass;
- production frontend build: pass;
- command palette chunk: 4.23 kB raw / 1.73 kB gzip;
- change overview chunk: 5.22 kB raw / 1.59 kB gzip;
- main `ReviewReader` chunk: 679.35 kB raw / 188.57 kB gzip;
- current headless browser probe on the 433-file durable review: ~1.78s reader-ready, exactly 5 initial patch requests, ~79ms distant navigation, exactly one additional patch request.

### Gate result
PASS. The reader now satisfies the full canonical keyboard-discovery contract and gives a senior reviewer one-click orientation before diving into code, without increasing permanent chrome or weakening the bounded-loading model. The final §39 preference question remains a human product judgment, but the engineering dogfood pass no longer has a known spec-level usability gap.

## R40 — Modal keyboard focus containment — COMPLETE

### Dogfood finding
The three transient review dialogs already set initial focus, support Escape, and restore focus on close, but their `aria-modal` contract was incomplete: Tab could move focus out of the dialog and back into the reader underneath.

### Implementation
- added one shared `trapModalTab` helper for review modals;
- Review actions / Keyboard shortcuts now wrap Tab and Shift+Tab inside the command dialog;
- Change Overview now contains keyboard focus while open;
- Finish Review now contains keyboard focus while open;
- no new permanent chrome or dialog state model was introduced.

### Qualification
- focused modal interaction suite: 11/11 passed;
- complete review frontend suite: 121/121 passed across 14 files;
- TypeScript: pass;
- production frontend build: pass;
- main `ReviewReader` chunk remains ~679.40 kB raw / 188.59 kB gzip.

### Gate result
PASS. Every modal review surface now matches its `aria-modal` behavior: focus enters deliberately, cannot leak into the obscured reader through Tab navigation, and returns to the invoking control when the surface closes.

## R41 — Manual-scroll target ownership regression — COMPLETE

### Dogfood finding
§39 depends on judgment shortcuts applying to the code actually being read after free scrolling, not merely to the last target selected through keyboard navigation. The stream already implemented this with a stable viewport threshold, but the behavior had no regression test.

### Qualification guard
- the CodeView scroll callback is now exercised against three mounted target headers with deterministic viewport positions;
- the target nearest the stable `115px` reading threshold is promoted to active ownership;
- the test proves manual scrolling changes the target used by subsequent reader actions without requiring `j/k` navigation;
- focused `ReviewStream` suite: 5/5 passed;
- complete review frontend suite: 122/122 passed across 14 files;
- TypeScript: pass.

### Gate result
PASS. Manual reading and keyboard judgment remain coupled: scrolling to another target updates active ownership before the next review action, and that invariant is now protected by a direct regression test.

## R42 — Virtualized diff render-race hardening — COMPLETE

### Dogfood finding
A real Review Reader click could throw `VirtualizedFileDiff.render: rendered a different diff than its prepared layout`. The reader violated two `@pierre/diffs` controlled-item contracts at once: it reparsed an unchanged patch into a new diff object on React updates, and it derived item `version` from only the length of a marker signature. Active-target changes also rebuilt `CodeView.options`, forcing unnecessary virtualized renders. Separately, an in-flight patch request from an older revision could land after refresh and overwrite the newer file detail.

### Implementation
- parsed diff objects are cached by `(path, patch)` and reused while patch bytes are unchanged;
- CodeView item revisions now move only when the actual controlled payload changes: patch/fallback content, exact marker signature, target-marker placement, or collapsed state;
- the existing `annotationModel.markerSignature` contract is now used directly instead of reducing it to string length;
- CodeView gutter options are stable across active-target/scroll changes by reading current target/draft state through refs;
- file fetches are generation-scoped, so responses started before revision refresh cannot write into the refreshed reader;
- an old request can no longer delete a newer in-flight request from the per-path request map.

### Qualification
- focused `ReviewReader` + `ReviewStream` regressions: 30/30 passed;
- complete review frontend suite from an isolated staged tree: 125/125 passed across 14 files;
- TypeScript: pass;
- production frontend build: pass;
- real headless-Chrome stress on the durable 433-file review: one actual `Reviewed` click, 6 collapse/expand clicks, 10 split/unified toggles, and 20 target navigations;
- zero `VirtualizedFileDiff` / prepared-layout exceptions and zero non-benign page errors during the real-browser stress pass.

### Gate result
PASS. Review-state clicks and navigation no longer replace an unchanged diff or churn CodeView options underneath an in-flight virtualized layout, and stale revision patch responses are rejected before they can mutate the current reader.
