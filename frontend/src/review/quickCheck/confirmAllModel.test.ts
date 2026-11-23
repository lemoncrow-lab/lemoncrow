import { describe, expect, it } from "vitest";

import type { ReviewTarget } from "../types";
import { BULK_MARK_LIMIT, confirmAllLabel, confirmedLine, quickConfirmSplit, rounds } from "./confirmAllModel";
import { plainGuardContext } from "./quickCheckModel";

function quick(id: string, overrides: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: `src/${id}.py`, label: id, symbol: id,
    start_line: 1, end_line: 2, hunk_ordinals: [0], spans: [], state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "",
    attention_rank: 0, attention_level: "normal", reasons: [], additions: 1, deletions: 0, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 0 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "quick_check", rule: "", rule_label: "", reason: "" },
    batch_guard: { guarded: false, code: "", reason: "" },
    ...overrides,
  } as ReviewTarget;
}

describe("confirm all quick checks", () => {
  it("offers the open quick checks Quick check would batch; the rest need their own answer", () => {
    const parts = [
      quick("a"),
      quick("b", { state: "reviewed" }),
      quick("c", { batch_guard: { guarded: true, code: "loop_bound", reason: "A loop bound changed" } }),
      quick("d", { state: "later" }),
      quick("e", { state: "changed_since_review" }),
      quick("f", { fingerprint_method: "unknown" }),
      quick("g"),
    ];
    const split = quickConfirmSplit(parts, plainGuardContext("rev1"));
    expect(split.confirm.map((part) => part.target_id)).toEqual(["a", "d", "g"]);
    // b is already answered, so it is neither offered nor counted; e changed after an answer and was not proved routine.
    expect(split.ownAnswer).toBe(3);
  });

  it("a quick check with no server guard fails closed", () => {
    expect(quickConfirmSplit([quick("a", { batch_guard: undefined })], plainGuardContext("rev1"))).toEqual({ confirm: [], ownAnswer: 1 });
  });

  it("splits a click into the server's rounds without dropping or repeating any part", () => {
    const keys = Array.from({ length: 1201 }, (_, i) => `k${i}`);
    const out = rounds(keys);
    expect(out.map((round) => round.length)).toEqual([BULK_MARK_LIMIT, BULK_MARK_LIMIT, 201]);
    expect(out.flat()).toEqual(keys);
    expect(rounds([])).toEqual([]);
    expect(rounds(keys.slice(0, BULK_MARK_LIMIT)).length).toBe(1);
  });

  it("names the count on the button and in the result line", () => {
    expect(confirmAllLabel(40)).toBe("Confirm all 40 quick checks");
    expect(confirmAllLabel(1)).toBe("Confirm 1 quick check");
    expect(confirmedLine({ marked: [{ unit_key: "a", event_id: "e" }], single: [], skipped: [] })).toBe("1 quick check confirmed as Looks good");
    expect(confirmedLine({ marked: [{ unit_key: "a", event_id: "e" }], single: [quick("z")], skipped: [] })).toBe("2 quick checks confirmed as Looks good");
    expect(confirmedLine({ marked: [], single: [], skipped: [{ unit_key: "b", path: "b.py", reason: "verification is not settled" }], failed: true }))
      .toBe("0 quick checks confirmed as Looks good · 1 left for their own answer (verification is not settled) · the rest could not be saved");
  });
});
