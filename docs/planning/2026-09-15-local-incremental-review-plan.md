# Local incremental review — implementation plan

Status: COMPLETE — local milestone
Date: 2026-09-15
Branch: `feat/review-incremental-local`
Worktree: `/home/pankaj/Projects/leanchain/lemoncrow-review-incremental`

## Goal

Make the existing local `lc review` revision-driven and incremental before moving the Review Reader to the enterprise server. The local and hosted products should eventually share the same review engine semantics; hosted mode should change source/storage/transport, not reimplement review behavior.

The user-visible outcome for the local reader is:

- a settled working-tree edit can advance the review without rebuilding the human workspace from scratch;
- unchanged loaded file patches remain mounted and cached;
- only changed/removed paths are invalidated and refetched;
- human marks are still content-bound and automatically reopen when their bytes change;
- revision delta remains explicit, so hot reload never means implicit approval;
- an in-progress comment draft is never silently re-anchored onto new source.

## Current grounded state

The code already has useful incremental primitives:

- `gitdiff.source_state()` computes a cheap source identity for working-tree/staged review detection;
- the browser probes source state and requires the same changed fingerprint twice before surfacing it, avoiding half-written agent edits;
- `record_revision()` reuses an existing revision when exact source/tree identity matches;
- `reconcile()` carries/reopens/drops human marks by content fingerprint rather than line position;
- `_incremental_impact_reuse()` reuses detector sites for unchanged changed-files when the diff base is stable;
- the API already returns a target-level `revision_target_delta` after refresh;
- the reader lazy-loads file patches and protects against stale responses with `loadGenerationRef`.

The remaining restart-from-scratch behavior is concentrated in two seams:

1. backend refresh captures the complete diff/blob packet and only reuses a narrow subset of semantic detector work;
2. frontend refresh clears the entire file-detail cache and reloads every initially visible path, even when only one file changed.

## Invariants

These are release-blocking:

1. **Frozen judgment.** A mark is valid only for the exact content fingerprint it was made against. Any changed target reopens visibly.
2. **No stale response win.** A file request started on revision N can never overwrite revision N+1 state.
3. **No implicit draft migration.** An unsaved human composer is either preserved only when its source path is proven unchanged, or explicitly discarded with a message.
4. **Revision atomicity.** Browser mutation controls stay blocked while the server has advanced but dependent revision reads have not landed.
5. **No analysis claim from cache alone.** Reuse must have an exact content/base precondition; otherwise recompute.
6. **Same semantics for manual and automatic refresh.** Hot reload calls the same refresh path and produces the same ReviewRevision/target delta as an explicit refresh.

## Architecture

### 1. Source delta

Introduce a small review-source delta projection derived from the previous and current frozen packets/source identities:

```text
ReviewSourceDelta
  changed_paths
  added_paths
  removed_paths
  renamed_paths
  preserved_paths
```

For this local milestone the persisted ReviewRevision stays dense and immutable. We optimize construction and consumption first rather than changing the storage format at the same time.

### 2. Incremental packet construction

Refresh continues to collect the authoritative current Git diff once. Reuse is allowed only for file-local analysis whose source bytes and diff base are unchanged. Changed files are recomputed; cross-file graph/ordering facts may still be recomputed until the later shared link-index invalidation seam lands.

This keeps correctness simple while removing repeated expensive detector work and establishes the `SourceDelta` contract needed by the future enterprise `ViewReviewSource`.

### 3. Delta-bearing refresh API

Extend the refresh response with file-level revision information in addition to the existing target delta:

```json
{
  "refreshed": {
    "changed_paths": ["src/a.ts"],
    "added_paths": [],
    "removed_paths": [],
    "renamed_paths": [{"old_path":"src/old.ts","path":"src/new.ts"}],
    "preserved_paths": ["src/b.ts"]
  }
}
```

The browser should not infer changed paths from target state: a file can change without preserving the same target shape.

### 4. Incremental Reader cache

On revision N -> N+1:

- bump the request generation before accepting any N+1 reads;
- retain the immutable patch-local portion of `details[path]` for `preserved_paths`;
- never carry revision-scoped symbols/provenance/evidence/rich preview metadata as current;
- invalidate changed/added/removed/rename patches completely;
- load changed/new visible patches before judgments unlock;
- refresh preserved revision-scoped metadata in the background or lazily on access;
- keep the active target/scroll position when its target survives;
- keep already loaded preserved diffs mounted instead of blanking the stream.

### 5. Local hot reload

The existing source probe already implements a settled-change detector. After two identical changed fingerprints:

- if the reader is idle and has no unsaved draft, automatically run the same revision refresh;
- if a draft exists or another mutation owns the busy lock, keep the `New revision available` banner and defer;
- manual `Review update` remains available;
- `Later` suppresses that exact source fingerprint only, as today.

Automatic refresh must not use a separate code path.

## Stages

### Stage A — contracts and tests

Add file-level delta calculation + response fields. Add backend tests for modified/added/removed/renamed/preserved classification and no-op/revert behavior.

### Stage B — browser incremental cache

Change `refreshReview()` to preserve only exact patch-local data for preserved paths, while invalidating revision-scoped context and refreshing that context in the background/lazily. Changed/new patches are blocking; preserved patches never blank. Add stale-request and preservation tests.

### Stage C — settled hot reload

Automatically call refresh after the existing two-probe stability gate when safe. Defer while composing/busy/read-only. Add deterministic tests with controlled probe promises.

### Stage D — backend analysis reuse expansion

Generalize the existing narrow impact reuse around the file-level delta so unchanged changed-files do not rerun file-local detector work. Keep graph/ordering recomputation until a dependency-closure cache is proven.

### Stage E — qualification

Run focused Python + frontend review suites, typecheck/lint for touched code, then the broader review test group. Record any remaining non-incremental work explicitly rather than declaring it solved.

## Deferred to hosted/server work

Not part of this branch:

- moving ReviewStore/API/frontend serving into `enterprise/server`;
- enterprise `ReviewSource` over `view_revision`/overlay events;
- server push/WebSocket/SSE transport;
- remote preview/workspace execution;
- structural-sharing persistence format for ReviewRevision artifacts;
- dependency-closure reuse backed by the enterprise three-layer link index.

The local contracts in this branch should make those follow-up changes substitutions underneath the engine rather than a redesign.

## Qualification result

Local milestone qualification on 2026-09-15:

- focused backend review/API/source-state/target/annotation suite: **207 passed**;
- broader review subsystem sweep: **334 passed, 1 skipped**, with **2 pre-existing provenance failures** (`test_a_tie_is_reported_in_degraded`, `test_a_matched_host_that_records_no_reads_is_named_in_degraded`) reproduced unchanged on `main`;
- Review Reader suite: **46 passed**;
- frontend TypeScript typecheck: passed;
- Ruff on touched Python modules/tests: passed;
- `git diff --check`: passed.

The intentionally deferred performance step is cross-file graph/ordering dependency-closure reuse. File-local detector reuse is incremental now; graph facts remain fresh on each revision until the shared link-index invalidation layer is available.
