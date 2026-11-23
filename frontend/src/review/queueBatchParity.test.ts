/**
 * P11-2 M2: the Overview's "to confirm in one step" is the Quick check batch, at any size.
 *
 * The Overview counts with `reviewQueue`; Quick check groups with `groupedQuick`. Both ask `guardOfPart` over the same
 * inputs, and what code has loaded decides nothing (a large change reads only the files around the part in hand).
 */
import { beforeEach, describe, expect, it } from "vitest";

import { forgetPatchFailures, patchEvidence, recordPatchResults, resetPatchEvidence } from "./quickCheck/patchEvidence";
import { groupedQuick, guardContext, guardOfPart, quickParts, quickPatchPaths, resetGuardLatch, type QuickGuard } from "./quickCheck/quickCheckModel";
import { reviewQueue } from "./queue";
import type { Annotation, ReviewTarget } from "./types";

const REV = "rev2";
const REVIEW = "review-1";
const PROVED = { routine: true, rule: "formatting_only", detail: "Only formatting changed." } as const;
const CLEAR = { guarded: false, code: "", reason: "" } as const;
const LOOPY = { guarded: true, code: "loop_bound", reason: "The loop bound changed." } as const;

function part(index: number, rest: Partial<ReviewTarget> = {}): ReviewTarget {
  const id = `p${index}`;
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: `app/${id}.py`, label: id, symbol: id,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed",
    changed_since_mark: false, reviewed_revision_id: "", attention_rank: index, attention_level: "normal",
    reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "quick_check", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: index, reasons: [] },
    batch_guard: CLEAR,
    ...rest,
  } as ReviewTarget;
}

/** A change of `count` parts, one file each: a mix of new clear parts, proved-routine reopened parts, and guarded ones. */
function change(count: number): ReviewTarget[] {
  return Array.from({ length: count }, (_, index) => {
    if (index % 7 === 3) return part(index, { batch_guard: LOOPY });
    if (index % 5 === 2) return part(index, { state: "changed_since_review", changed_since_mark: true, reviewed_revision_id: "rev1", since_review: PROVED, batch_guard: LOOPY });
    if (index % 11 === 4) return part(index, { state: "changed_since_review", changed_since_mark: true, reviewed_revision_id: "rev1" });
    return part(index);
  });
}

function hint(target: ReviewTarget, body: string): Annotation {
  return {
    id: `hint-${target.target_id}`, review_id: REVIEW, revision_id: REV, relation_to_revision: "current", source: "ai_review", state: "open",
    finding_disposition: "open", unit_key: target.unit_key, path: target.path, anchored: true, anchor_exact: true, file_level: false,
    start_line: 1, end_line: 1, side: "additions", body,
  } as unknown as Annotation;
}

/** What Quick check offers in one step: the open parts in its groups, with the guards it computes. */
function quickBatch(targets: readonly ReviewTarget[], annotations: readonly Annotation[]): string[] {
  const quick = quickParts(targets);
  const failed = patchEvidence(REVIEW, REV).failed;
  const context = guardContext({ reviewId: REVIEW, annotationReviewId: REVIEW, revisionId: REV, parts: quick, annotations, failedPaths: failed });
  const guards = new Map<string, QuickGuard>(quick.map((one) => [one.target_id, guardOfPart(one, context)]));
  const byDirectory = false;
  return groupedQuick(quick, guards, byDirectory, context.routineIds).flatMap((group) => group.parts).map((one) => one.target_id).sort();
}

/** What the Overview headline counts. */
function overviewConfirm(targets: readonly ReviewTarget[], annotations: readonly Annotation[]): string[] {
  const parts = targets.filter((one) => one.answer?.category === "quick_check");
  const context = guardContext({ reviewId: REVIEW, annotationReviewId: REVIEW, revisionId: REV, parts, annotations, failedPaths: patchEvidence(REVIEW, REV).failed });
  return reviewQueue(targets, REV, undefined, context).confirm.map((one) => one.target_id).sort();
}

beforeEach(() => { resetGuardLatch(); resetPatchEvidence(); });

describe.each([199, 200, 201, 1000])("a change of %i files", (count) => {
  const targets = change(count);

  it("the headline count is the Quick check batch, whatever code has loaded", () => {
    const expected = quickBatch(targets, []);
    expect(expected.length).toBeGreaterThan(0);
    expect(overviewConfirm(targets, [])).toEqual(expected);
    // Only the files around the part in hand have been read (a large change reads about 30), or all, or none.
    const window = quickPatchPaths(quickParts(targets), targets[0].path);
    recordPatchResults(REVIEW, REV, window.map((path) => [path, true] as const));
    expect(overviewConfirm(targets, [])).toEqual(expected);
    recordPatchResults(REVIEW, REV, targets.map((one) => [one.path, true] as const));
    expect(overviewConfirm(targets, [])).toEqual(expected);
    expect(reviewQueue(targets, REV, undefined, guardContext({ reviewId: REVIEW, annotationReviewId: REVIEW, revisionId: REV, parts: targets, annotations: [], failedPaths: patchEvidence(REVIEW, REV).failed })).confirm).toHaveLength(expected.length);
  });

  it("an AI hint that raises a result narrows both the same way", () => {
    const base = quickBatch(targets, []);
    const hinted = base.filter((_, position) => position % 4 === 0).map((id) => targets.find((one) => one.target_id === id)!);
    const notes = hinted.map((one) => hint(one, "This changes the result."));
    const batch = quickBatch(targets, notes);
    expect(batch).toEqual(base.filter((id) => !hinted.some((one) => one.target_id === id)));
    expect(overviewConfirm(targets, notes)).toEqual(batch);
    // A hint that says nothing about the result, a note from a person, and another revision's hint leave the batch alone.
    const quiet = [hint(hinted[0], "Consider a clearer name."), { ...hint(hinted[1], "This changes the result."), source: "human" }, { ...hint(hinted[2], "This changes the result."), revision_id: "rev1" }] as unknown as Annotation[];
    expect(quickBatch(targets, quiet)).toEqual(base);
    expect(overviewConfirm(targets, quiet)).toEqual(base);
  });

  it("a patch that fails to load takes its part out of both, and a retry puts it back", () => {
    const base = quickBatch(targets, []);
    const gone = targets.find((one) => one.target_id === base[0])!;
    recordPatchResults(REVIEW, REV, [[gone.path, false]]);
    const after = quickBatch(targets, []);
    expect(after).toEqual(base.filter((id) => id !== gone.target_id));
    expect(overviewConfirm(targets, [])).toEqual(after);
    forgetPatchFailures(REVIEW, REV);
    expect(overviewConfirm(targets, [])).toEqual(base);
    expect(quickBatch(targets, [])).toEqual(base);
  });
});

describe("the server's first answer for a part holds for the session", () => {
  it("a guard that changes after the first request does not move a part in or out of the batch, on either screen", () => {
    const first = [part(0, { batch_guard: CLEAR }), part(1, { batch_guard: LOOPY })];
    expect(quickBatch(first, [])).toEqual(["p0"]);
    expect(overviewConfirm(first, [])).toEqual(["p0"]);
    // A later response (a mark replaced the target, the revision was refreshed) swaps the two guards.
    const later = [part(0, { batch_guard: LOOPY }), part(1, { batch_guard: CLEAR })];
    expect(quickBatch(later, [])).toEqual(["p0"]);
    expect(overviewConfirm(later, [])).toEqual(["p0"]);
    const context = guardContext({ reviewId: REVIEW, annotationReviewId: REVIEW, revisionId: REV, parts: later, annotations: [], failedPaths: new Set() });
    expect(guardOfPart(later[0], context).guarded).toBe(false);
    expect(guardOfPart(later[1], context).guarded).toBe(true);
  });

  it("a part first seen guarded stays guarded for the session even when the server later clears it", () => {
    expect(quickBatch([part(0, { batch_guard: LOOPY })], [])).toEqual([]);
    expect(quickBatch([part(0, { batch_guard: CLEAR })], [])).toEqual([]);
    expect(overviewConfirm([part(0, { batch_guard: CLEAR })], [])).toEqual([]);
  });
});
