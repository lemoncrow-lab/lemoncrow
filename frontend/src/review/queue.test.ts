import { describe, expect, it } from "vitest";

import { decisionQueue, judgedParts } from "./focus/focusModel";
import { firstOpenPart } from "./overview/overviewModel";
import { guardOfPart, latchRoutine, plainGuardContext, quickGroup, quickGuard, quickParts, groupedQuick, SINCE_REVIEW_GROUP, type QuickGuard } from "./quickCheck/quickCheckModel";
import { queueLine, reviewQueue } from "./queue";
import { hasProofFrom, readOrder, readTier, routineSinceReview } from "./sinceReview";
import type { ReviewTarget } from "./types";

const REV = "rev2";
const structuralGuard = (target: ReviewTarget) => guardOfPart(target, plainGuardContext(REV));
const CLEAR = { guarded: false, code: "", reason: "" } as const;
const PROVED = { routine: true, rule: "formatting_only", detail: "Only formatting changed." } as const;

function part(id: string, position: number, rest: Partial<ReviewTarget> & { category?: "decision" | "quick_check" | "accepted_by_rule" } = {}): ReviewTarget {
  const { category = "quick_check", ...more } = rest;
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: `app/${id}.py`, label: id, symbol: id,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed",
    changed_since_mark: false, reviewed_revision_id: "", attention_rank: position, attention_level: "normal",
    reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position, reasons: [] },
    batch_guard: CLEAR,
    ...more,
  } as ReviewTarget;
}

/** A part the reviewer judged on rev1 whose content has since changed. */
const reopened = (id: string, position: number, rest: Partial<ReviewTarget> & { category?: "decision" | "quick_check" } = {}) =>
  part(id, position, { state: "changed_since_review", changed_since_mark: true, reviewed_revision_id: "rev1", ...rest });
/** A part the reviewer judged on rev1 that did not change. */
const kept = (id: string, position: number, rest: Partial<ReviewTarget> & { category?: "decision" | "quick_check" } = {}) =>
  part(id, position, { state: "reviewed", reviewed_revision_id: "rev1", ...rest });

describe("read order (P11-2)", () => {
  const targets = [
    part("new_a", 0, { category: "decision" }),
    reopened("proved", 1, { category: "decision", path: "app/proved.py" }),
    reopened("bare", 2, { category: "decision", path: "app/bare.py" }),
    part("new_b", 3, { category: "decision" }),
  ];
  const hasProof = hasProofFrom({ revision_id: REV, default: { o: "not_tested" }, files: { "app/proved.py": { o: "proven" } } } as never);

  it("asks changed parts without proof, then changed parts with proof, then new parts", () => {
    expect(decisionQueue(targets, hasProof).map((p) => p.target_id)).toEqual(["bare", "proved", "new_a", "new_b"]);
  });

  it("keeps the server's order inside a tier and when there is no record yet", () => {
    expect(decisionQueue(targets).map((p) => p.target_id)).toEqual(["proved", "bare", "new_a", "new_b"]);
    expect(readTier(targets[1], hasProof)).toBe(1);
    expect(readTier(targets[2], hasProof)).toBe(0);
    expect(readTier(targets[0], hasProof)).toBe(2);
  });

  it("counts only a proven or exercised outcome as proof; a failing or unrun test is not", () => {
    const proof = { revision_id: REV, default: { o: "not_run_yet" }, files: {
      "a.py": { o: "proven" }, "b.py": { o: "exercised" }, "c.py": { o: "fails" }, "d.py": { o: "not_tested" }, "e.py": { o: "couldnt_run" },
    } } as never;
    const has = hasProofFrom(proof);
    expect(["a.py", "b.py", "c.py", "d.py", "e.py", "z.py"].map(has)).toEqual([true, true, false, false, false, false]);
    expect(hasProofFrom({ ...(proof as object), load_failed: true } as never)("a.py")).toBe(false);
    expect(hasProofFrom(null)("a.py")).toBe(false);
  });

  it("puts the changed-since-review part first in the first open part, the Focus counter order and the quick order", () => {
    expect(firstOpenPart(targets, hasProof)).toBe("bare");
    expect(judgedParts(targets, hasProof).map((p) => p.target_id).slice(0, 2)).toEqual(["bare", "proved"]);
    const quick = [part("q_new", 0), reopened("q_changed", 1)];
    expect(quickParts(quick).map((p) => p.target_id)).toEqual(["q_changed", "q_new"]);
  });

  it("does not reorder parts that were answered on this revision or never answered", () => {
    const rest = [part("x", 0), kept("y", 1, { reviewed_revision_id: REV }), part("z", 2)];
    expect(readOrder(rest).map((p) => p.target_id)).toEqual(["x", "y", "z"]);
  });
});

describe("the guard for a part reopened since the review (P11-2)", () => {
  const routine = reopened("fmt", 0, { since_review: PROVED });

  it("clears a part the server proved routine against what the reviewer reviewed", () => {
    expect(routineSinceReview(routine)).toBe(true);
    expect(structuralGuard(routine).guarded).toBe(false);
    expect(quickGroup(routine)).toBe(SINCE_REVIEW_GROUP);
  });

  it("ignores the base diff's batch_guard for that part: it was proved against the reviewed content", () => {
    const loopy = reopened("fmt", 0, { since_review: PROVED, batch_guard: { guarded: true, code: "loop_bound", reason: "The loop bound changed." } });
    expect(structuralGuard(loopy).guarded).toBe(false);
  });

  it("guards a reopened part with no answer, a refused answer, an unknown rule, or a non-reopened state", () => {
    const cases: ReviewTarget[] = [
      reopened("none", 0),
      reopened("refused", 0, { since_review: { routine: false, rule: "", detail: "The behavior changed." } }),
      reopened("unknown_rule", 0, { since_review: { routine: true, rule: "trust_me", detail: "" } }),
      part("claimed_but_unreviewed", 0, { state: "unreviewed", changed_since_mark: true, since_review: PROVED }),
      reopened("claimed_without_flag", 0, { changed_since_mark: false, since_review: PROVED }),
    ];
    for (const row of cases) {
      expect(routineSinceReview(row), row.target_id).toBe(false);
      expect(structuralGuard(row).guarded, row.target_id).toBe(true);
    }
  });

  it("a since_review on a part that was never reopened proves nothing", () => {
    expect(routineSinceReview(part("fresh", 0, { since_review: PROVED }))).toBe(false);
    expect(quickGroup(part("fresh", 0, { since_review: PROVED }))).not.toBe(SINCE_REVIEW_GROUP);
  });

  it("never trusts a rename hint alone: renamed_from without a proof stays guarded", () => {
    const renamed = reopened("renamed", 0, { renamed_from: { path: "app/old.py", symbol: "old" } } as Partial<ReviewTarget>);
    expect(structuralGuard(renamed).guarded).toBe(true);
    const wrong = reopened("renamed_wrong", 0, { renamed_from: { path: "app/old.py", symbol: "old" }, since_review: { routine: false, rule: "rename_only", detail: "" } } as Partial<ReviewTarget>);
    expect(structuralGuard(wrong).guarded).toBe(true);
  });

  it("hosted: no since_review and no batch_guard on a reopened part stays guarded", () => {
    const hosted = reopened("hosted", 0, { batch_guard: undefined });
    expect(structuralGuard(hosted).guarded).toBe(true);
    expect(quickGuard(hosted, REV, undefined).guarded).toBe(true);
  });

  it("does not wait on the patch: a proved-routine part is in the batch before its code is read, and out once the read fails", () => {
    expect(quickGuard(routine, REV).guarded).toBe(false);
    expect(quickGuard(routine, REV, null).guarded).toBe(true);
    expect(quickGuard(routine, REV, { path: routine.path, revision_id: "rev1", renderable: true, patch: "@@" }).guarded).toBe(true);
  });

  it("still needs the frozen patch, and still refuses an AI hint that says the result changes", () => {
    const patch = { path: routine.path, revision_id: REV, renderable: true, patch: "@@ -1 +1 @@\n-a\n+b\n" };
    expect(quickGuard(routine, REV, patch).guarded).toBe(false);
    expect(quickGuard(routine, REV, null).guarded).toBe(true);
    expect(quickGuard(routine, REV, patch, [{ body: "This changes the result." } as never]).guarded).toBe(true);
  });

  it("keeps a confirmed routine part in its group: the latch survives the mark that drops since_review", () => {
    const latch = latchRoutine({ revision: "", ids: new Set() }, [routine, reopened("bare", 1)], REV);
    expect([...latch.ids]).toEqual(["fmt"]);
    const confirmed = kept("fmt", 0, { reviewed_revision_id: REV, batch_guard: { guarded: true, code: "loop_bound", reason: "x" } });
    const guards = new Map<string, QuickGuard>([["fmt", quickGuard(confirmed, REV, { path: confirmed.path, revision_id: REV, renderable: true, patch: "@@" }, [], confirmed.batch_guard, latch.ids.has("fmt"))]]);
    const groups = groupedQuick([confirmed], guards, false, latch.ids);
    expect(groups.map((g) => g.name)).toEqual([SINCE_REVIEW_GROUP]);
  });

  it("the latch starts over on a new revision: target_id is stable, so a part proved routine on revision 2 is not batched on revision 3 after a behavior change", () => {
    const first = latchRoutine({ revision: "", ids: new Set() }, [routine], "rev2");
    expect([...first.ids]).toEqual(["fmt"]);
    // Revision 3: the same part (same target_id) changed behavior, so the server's answer is not routine.
    const behavior = reopened("fmt", 0, { since_review: { routine: false, rule: "", detail: "The behavior changed." } });
    const second = latchRoutine(first, [behavior], "rev3");
    expect(second.revision).toBe("rev3");
    expect(second.ids.has("fmt")).toBe(false);
    expect(guardOfPart(behavior, { ...plainGuardContext("rev3"), routineIds: second.ids }).guarded).toBe(true);
    // The same revision keeps what it first saw (the mark that drops since_review does not move the part).
    expect(latchRoutine(first, [behavior], "rev2").ids.has("fmt")).toBe(true);
  });

  it("a part never proved routine is not admitted by the latch", () => {
    const latch = latchRoutine({ revision: "", ids: new Set() }, [reopened("bare", 0), part("new", 1)], REV);
    expect(latch.ids.size).toBe(0);
  });
});

describe("a hunk that was only renumbered by a re-hunking (P11-2)", () => {
  const SAME = { routine: true, rule: "rehunked_identical", detail: "The same hunk you reviewed; only its position in the file moved." } as const;
  const hunk = (id: string, rest: Partial<ReviewTarget> = {}) => part(id, 0, { kind: "hunk", state: "unreviewed", changed_since_mark: true, path: "notes/n.txt", ...rest });

  it("is in the batch when the server proved it the reviewed hunk, and a person still confirms it there", () => {
    const row = hunk("h", { since_review: SAME });
    expect(routineSinceReview(row)).toBe(true);
    expect(structuralGuard(row).guarded).toBe(false);
    expect(quickGroup(row)).toBe(SINCE_REVIEW_GROUP);
    const queue = reviewQueue([row], REV);
    expect(queue.confirm.map((p) => p.target_id)).toEqual(["h"]);
    expect(queue.read).toEqual([]);
    expect(row.state).toBe("unreviewed"); // nothing is carried as reviewed
  });

  it("stays in the read queue with no proof, with a refused proof, or on any other rule or kind", () => {
    for (const row of [
      hunk("none"),
      hunk("no", { since_review: { routine: false, rule: "", detail: "The hunk is not byte for byte the one you reviewed." } }),
      hunk("other_rule", { since_review: { routine: true, rule: "comment_only", detail: "" } }),
      hunk("flag_off", { since_review: { ...SAME, routine: false } }),
      hunk("a_symbol", { kind: "symbol", since_review: SAME }),
      hunk("answered", { state: "reviewed", since_review: SAME }),
      hunk("needs", { state: "needs_changes", since_review: SAME }),
    ]) {
      expect(routineSinceReview(row), row.target_id).toBe(false);
    }
    expect(reviewQueue([hunk("none"), hunk("no", { since_review: { routine: false, rule: "", detail: "x" } })], REV).confirm).toEqual([]);
  });

  it("is judged by its proof, not by the base diff's guard, and a hosted server that never sends one reads it", () => {
    const row = hunk("h", { since_review: SAME, batch_guard: { guarded: true, code: "loop_bound", reason: "The loop bound changed." } });
    expect(structuralGuard(row).guarded).toBe(false);
    expect(structuralGuard(hunk("hosted", { batch_guard: undefined })).guarded).toBe(true);
  });
});

describe("the queue and its counts (P11-2)", () => {
  const targets = [
    reopened("behavior", 0, { category: "decision", since_review: { routine: false, rule: "", detail: "The behavior changed." } }),
    part("new_decision", 1, { category: "decision" }),
    reopened("fmt", 2, { since_review: PROVED }),
    reopened("changed_quick", 3),
    part("new_quick", 4),
    part("new_quick_guarded", 5, { batch_guard: { guarded: true, code: "loop_bound", reason: "The loop bound changed." } }),
    kept("same_a", 6),
    kept("same_b", 7, { category: "decision" }),
    kept("answered_now", 8, { reviewed_revision_id: REV }),
    part("rule", 9, { answer: { category: "accepted_by_rule", rule: "lockfiles", rule_label: "Lockfile", reason: "" } }),
    kept("rule_kept", 10, { answer: { category: "accepted_by_rule", rule: "generated_files", rule_label: "Generated", reason: "" } }),
    part("hold", 11, { state: "later", category: "decision" }),
  ];

  it("splits parts into read, confirm in one step and unchanged since the review", () => {
    const queue = reviewQueue(targets, REV);
    expect(queue.read.map((p) => p.target_id)).toEqual(["behavior", "changed_quick", "new_decision", "hold", "new_quick_guarded"]);
    expect(queue.confirm.map((p) => p.target_id)).toEqual(["fmt", "new_quick"]);
    expect(queue.unchanged.map((p) => p.target_id)).toEqual(["same_a", "same_b"]);
    expect(queue.followsReview).toBe(true);
  });

  it("the headline is exactly the three list lengths, in the fixed words", () => {
    const queue = reviewQueue(targets, REV);
    expect(queueLine(queue)).toBe(`${queue.read.length} to read · ${queue.confirm.length} to confirm in one step · ${queue.unchanged.length} unchanged since your review`);
    expect(queueLine(queue)).toBe("5 to read · 2 to confirm in one step · 2 unchanged since your review");
  });

  it("no part is in two lists, parts accepted by rule are in none, and a part answered on this revision is in none", () => {
    const queue = reviewQueue(targets, REV);
    const all = [...queue.read, ...queue.confirm, ...queue.unchanged].map((p) => p.target_id);
    expect(new Set(all).size).toBe(all.length);
    expect(all).not.toContain("rule");
    expect(all).not.toContain("rule_kept");
    expect(all).not.toContain("answered_now");
  });

  it("every reopened part with no proof of routine change is in read, never in confirm", () => {
    const queue = reviewQueue(targets, REV);
    const confirmIds = new Set(queue.confirm.map((p) => p.target_id));
    for (const row of targets.filter((p) => p.changed_since_mark && !routineSinceReview(p))) expect(confirmIds.has(row.target_id)).toBe(false);
  });

  it("orders the read list across kinds: tier first, decisions before quick checks inside a tier", () => {
    const hasProof = hasProofFrom({ revision_id: REV, default: { o: "not_tested" }, files: { "app/c_proof.py": { o: "proven" }, "app/q_proof.py": { o: "proven" } } } as never);
    const mixed = [
      part("n_quick", 0, { batch_guard: { guarded: true, code: "loop_bound", reason: "x" } }),
      part("n_decision", 1, { category: "decision" }),
      reopened("q_proof", 2, { batch_guard: { guarded: true, code: "loop_bound", reason: "x" } }),
      reopened("c_proof", 3, { category: "decision" }),
      reopened("q_bare", 4),
      reopened("c_bare", 5, { category: "decision" }),
    ];
    expect(reviewQueue(mixed, REV, hasProof).read.map((p) => p.target_id)).toEqual(["c_bare", "q_bare", "c_proof", "q_proof", "n_decision", "n_quick"]);
  });

  it("orders the read list: changed without proof, changed with proof, then the rest (decisions before quick checks)", () => {
    const hasProof = hasProofFrom({ revision_id: REV, default: { o: "not_tested" }, files: { "app/changed_quick.py": { o: "proven" } } } as never);
    const two = [part("n", 0, { category: "decision" }), reopened("c1", 1, { category: "decision" }), reopened("c2", 2, { category: "decision", path: "app/changed_quick.py" })];
    expect(reviewQueue(two, REV, hasProof).read.map((p) => p.target_id)).toEqual(["c1", "c2", "n"]);
  });

  it("a first review has no 'since your review' line to show", () => {
    expect(reviewQueue([part("a", 0), part("b", 1, { category: "decision" })], REV).followsReview).toBe(false);
  });

  it("hosted: with no since_review and no batch_guard, nothing reaches the confirm step", () => {
    const hosted = [reopened("h1", 0, { batch_guard: undefined }), part("h2", 1, { batch_guard: undefined })];
    const queue = reviewQueue(hosted, REV);
    expect(queue.confirm).toHaveLength(0);
    expect(queue.read).toHaveLength(2);
  });
});
