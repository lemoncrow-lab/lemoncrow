# Local incremental review — tasklist

Status: COMPLETE — local milestone
Date: 2026-09-15
Plan: `docs/planning/2026-09-15-local-incremental-review-plan.md`

## A. File-level revision delta

- [x] Add an immutable file-delta model for one review revision transition.
- [x] Classify preserved / changed / added / removed / renamed paths from frozen review units.
- [x] Derive file delta once at the refresh API boundary, relative to the browser's previous frozen revision.
- [x] Add `changed_paths`, `added_paths`, `removed_paths`, `renamed_paths`, `preserved_paths` to refresh payload.
- [x] Refuse patch preservation when the Git diff base moved, even if new-side file bytes are identical.
- [x] Test modification, addition, deletion, rename, unknown identity, no-op/revert semantics, and base movement.

## B. Incremental browser refresh

- [x] Extend frontend refresh types with file delta.
- [x] Stop blanking the whole file-detail cache on every refresh.
- [x] Keep exact patch-local data mounted for proven `preserved_paths`.
- [x] Never carry old revision-scoped symbols/provenance/evidence/preview metadata as current.
- [x] Fully invalidate changed/added/removed and rename endpoints.
- [x] Bump request generation before N+1 reads so old responses cannot win.
- [x] Block refresh completion only on changed/new patches needed in the initial/active N+1 window.
- [x] Refresh preserved revision-scoped metadata in the background/lazily without blanking its diff.
- [x] Keep active target and preserved rendered diffs stable across refresh.
- [x] Test preserved patch remains mounted while fresh metadata is still in flight.
- [x] Test changed loaded patch is replaced with N+1 content.
- [x] Retain existing stale-response safety test.

## C. Hot local revision advance

- [x] Reuse the existing two-identical-probe stability rule.
- [x] Include the Git base identity in source-state fingerprints.
- [x] Auto-refresh a settled source change when reader is idle, mutable and has no draft.
- [x] Defer automatic refresh while a comment draft exists.
- [x] Defer automatic refresh while another mutation is busy.
- [x] Keep explicit `Review update` and `Later` controls as fallback.
- [x] Test automatic refresh occurs exactly once per settled fingerprint.
- [x] Test draft/busy deferral.

## D. Backend incremental analysis

- [x] Make the same file-level exact-content delta available to incremental analysis reuse.
- [x] Preserve exact range/base/content safety checks.
- [x] Reuse file-local detector results only for preserved paths.
- [x] Recompute changed/added/renamed file-local detector work.
- [x] Keep cross-file graph/ordering recomputation explicit for now.
- [x] Retain regression instrumentation proving preserved files avoid detector reruns while matching a full rebuild semantically.

## E. Qualification

- [x] Focused local-source/revision/source-state tests.
- [x] Review API / targets / annotations / incremental-impact tests.
- [x] `frontend/src/review/ReviewReader.test.tsx` — 46 passed.
- [x] Frontend typecheck.
- [x] Ruff on touched Python modules/tests.
- [x] Broader review test subset — 334 passed, 1 skipped.
- [x] Confirm the two broader provenance failures are pre-existing by reproducing them on `main`.
- [x] `git diff --check`.
- [x] Record remaining/deferred work in the plan.
- [x] Commit clean branch.

## Deferred

- Cross-file graph/ordering dependency-closure caching. Current revisions deliberately recompute graph facts because another changed file can alter caller counts/centrality even when a source file is byte-identical. This is the next incremental-analysis layer and should share the future server/link-index invalidation contract.
- Hosted `ReviewSource`, server-side ReviewStore/API/frontend serving, push transport, and remote preview execution remain in the enterprise track.
