import { describe, expect, it } from "vitest";

import type { ReviewMarkEvent, ReviewProjection, ReviewTarget } from "../types";
import {
  answerBadge,
  confirmationText,
  counterFor,
  decisionQueue,
  interstitialHeading,
  momentumText,
  movedToEndOnce,
  confirmationTone,
  mergeRanges,
  nextNeeding,
  notableSites,
  onlyCheckThereLine,
  partPosition,
  pauseHeading,
  pauseReason,
  planDecideLater,
  planLooksGood,
  resolvePart,
  stepPart,
  testOutcome,
  unhintedRanges,
} from "./focusModel";

function target(id: string, position: number, overrides: Partial<ReviewTarget> & { category?: "decision" | "quick_check" | "accepted_by_rule" } = {}): ReviewTarget {
  const { category = "decision", ...rest } = overrides;
  return {
    target_id: id,
    unit_key: `sym:${id}`,
    kind: "symbol",
    path: "app/orders.py",
    label: id,
    symbol: id,
    start_line: 4,
    end_line: 6,
    hunk_ordinals: [0],
    spans: [],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: position,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 1,
    fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "review_first", outstanding: true, position, reasons: [] },
    ...rest,
  } as ReviewTarget;
}

const A = target("can_view_order", 0);
const B = target("apply_discount", 1);
const C = target("order_total", 2, { category: "quick_check" });
const ALL = [C, B, A];

describe("order", () => {
  it("asks decisions in the server's order and counts a part's position within its own kind", () => {
    expect(decisionQueue(ALL).map((t) => t.symbol)).toEqual(["can_view_order", "apply_discount"]);
    expect(partPosition(ALL, "apply_discount")).toEqual({ n: 2, total: 2 });
    expect(partPosition(ALL, "order_total")).toEqual({ n: 1, total: 1 });
  });

  it("moves parts left for later to the end of the decisions, without reordering either group", () => {
    const later = target("can_view_order", 0, { state: "later" });
    expect(decisionQueue([later, B]).map((t) => t.symbol)).toEqual(["apply_discount", "can_view_order"]);
    const two = target("x", 3, { state: "later" });
    const three = target("y", 4, { state: "later" });
    expect(decisionQueue([three, two, A]).map((t) => t.symbol)).toEqual(["can_view_order", "x", "y"]);
  });

  it("resolves a part by symbol or target id", () => {
    expect(resolvePart(ALL, "apply_discount")?.target_id).toBe("apply_discount");
    expect(resolvePart(ALL, "nope")).toBeNull();
    expect(resolvePart(ALL, "")).toBeNull();
  });

  it("next part: the next one that needs an answer after this one, else one before it, else none", () => {
    const queue = [A, B];
    expect(nextNeeding(queue, "can_view_order")?.symbol).toBe("apply_discount");
    // Answered parts are skipped; the one before is the fallback.
    expect(nextNeeding([{ ...A, state: "unreviewed" }, { ...B, state: "reviewed" }], "apply_discount")?.symbol).toBe("can_view_order");
    expect(nextNeeding([{ ...A, state: "reviewed" }, B], "apply_discount")).toBeNull();
    // A part left for later is open: it comes back.
    expect(nextNeeding([A, { ...B, state: "later" }], "can_view_order")?.symbol).toBe("apply_discount");
    // The part just answered does not count even if its state has not arrived yet.
    expect(nextNeeding([A, B], "can_view_order", new Set(["apply_discount"]))).toBeNull();
  });

  it("hands over from a part just moved to the end to the one that followed it", () => {
    const before = [A, B, target("third", 5)];
    expect(nextNeeding(before, "can_view_order")?.symbol).toBe("apply_discount");
  });

  it("J and K step through the queue and stop at its ends", () => {
    const queue = [A, B];
    expect(stepPart(queue, "can_view_order", 1)?.symbol).toBe("apply_discount");
    expect(stepPart(queue, "apply_discount", 1)).toBeNull();
    expect(stepPart(queue, "can_view_order", -1)).toBeNull();
    expect(stepPart(queue, "missing", 1)).toBeNull();
  });
});

describe("pips and counter", () => {
  it("one pip per decision with the current one marked; quick checks keep their own small progress", () => {
    const parts = [{ ...A, state: "reviewed" as const }, { ...B, state: "later" as const }, C];
    const counter = counterFor(parts, "apply_discount", true)!;
    expect(counter.pips?.map((p) => [p.state, p.quick, p.current])).toEqual([
      ["good", false, false],
      ["later", false, true],
    ]);
    expect(counter.text).toBe("1 of 2 decisions");
    expect(counter.label).toBe("1 of 2 decisions judged · 1 left for later");
    expect(counter.quick).toEqual({ judged: 0, total: 1, text: "quick checks 0/1" });
  });

  it("Needs a change is judged; changed since review is not", () => {
    const counter = counterFor([{ ...A, state: "needs_changes" }, { ...B, state: "changed_since_review" }], "", true)!;
    expect(counter.judged).toBe(1);
    expect(counter.pips?.map((p) => p.state)).toEqual(["change", "open"]);
    expect(counter.label).toBe("1 of 2 decisions judged");
    expect(counter.quick).toBeNull();
  });

  it("a large review counts its decisions, not every part: 27 decisions among 3048 parts read 0 of 27 decisions", () => {
    const rule = (i: number) => target(`r${i}`, 5000 + i, { answer: { category: "accepted_by_rule", rule: "generated_files", rule_label: "generated files", reason: "" } });
    const parts = [
      ...Array.from({ length: 27 }, (_, i) => target(`d${i}`, i)),
      ...Array.from({ length: 40 }, (_, i) => target(`q${i}`, 100 + i, { category: "quick_check", state: i < 3 ? "reviewed" : "unreviewed" })),
      ...Array.from({ length: 2981 }, (_, i) => rule(i)),
    ];
    expect(parts).toHaveLength(3048);
    const counter = counterFor(parts, "", true)!;
    expect(counter.text).toBe("0 of 27 decisions");
    expect(counter.total).toBe(27);
    expect(counter.quick?.text).toBe("quick checks 3/40");
    // A judged quick check never moves the decisions' count.
    expect(counter.judged).toBe(0);
  });

  it("one decision is singular; no decisions says so instead of 0 of 0", () => {
    expect(counterFor([A, C], "", true)!.text).toBe("0 of 1 decision");
    const none = counterFor([C], "", true)!;
    expect(none.text).toBe("No decisions");
    expect(none.pips).toBeNull();
    expect(none.quick?.text).toBe("quick checks 0/1");
  });

  it("counts only a complete list, and draws no pips above twelve decisions", () => {
    expect(counterFor(ALL, "", false)).toBeNull();
    const many = Array.from({ length: 13 }, (_, i) => target(`p${i}`, i));
    const counter = counterFor(many, "", true)!;
    expect(counter.pips).toBeNull();
    expect(counter.text).toBe("0 of 13 decisions");
    expect(counterFor(many.slice(0, 12), "", true)!.pips).toHaveLength(12);
    // Many quick checks beside a few decisions still draw the decisions' pips.
    const quick = Array.from({ length: 30 }, (_, i) => target(`q${i}`, 50 + i, { category: "quick_check" }));
    expect(counterFor([A, B, ...quick], "", true)!.pips).toHaveLength(2);
  });

  it("above twelve decisions the bar carries the server's three counts and its accessible text (SCALE-1)", () => {
    const rule = (i: number) => target(`r${i}`, 100 + i, { answer: { category: "accepted_by_rule", rule: "lockfiles", rule_label: "Lockfiles", reason: "" } });
    const parts = [
      ...Array.from({ length: 13 }, (_, i) => target(`d${i}`, i)),
      ...Array.from({ length: 8 }, (_, i) => target(`q${i}`, 20 + i, { category: "quick_check" })),
      rule(1), rule(2), rule(3),
    ];
    const counter = counterFor(parts, "", true)!;
    expect(counter.pips).toBeNull();
    expect(counter.text).toBe("0 of 13 decisions");
    expect(counter.bar?.label).toBe("13 decisions · 8 quick checks · 3 accepted by rule");
    expect(counter.bar?.segments.map((s) => [s.kind, s.count])).toEqual([["decision", 13], ["quick_check", 8], ["accepted_by_rule", 3]]);
  });

  it("up to twelve decisions there is no bar, at thirteen there is", () => {
    const many = Array.from({ length: 13 }, (_, i) => target(`p${i}`, i));
    expect(counterFor(many.slice(0, 12), "", true)!.bar).toBeNull();
    expect(counterFor(many, "", true)!.bar).not.toBeNull();
  });

  it("accepted-by-rule parts are not counted", () => {
    const rule = target("lock", 9, { answer: { category: "accepted_by_rule", rule: "lockfiles", rule_label: "Lockfiles", reason: "" } });
    const counter = counterFor([A, rule], "", true)!;
    expect(counter.total).toBe(1);
    expect(counter.quick).toBeNull();
  });
});

describe("answers", () => {
  it("Looks good records reviewed and advances; a part already Looks good only moves on", () => {
    expect(planLooksGood(A, false)).toEqual({ mark: "reviewed", advance: true, pause: false, blocked: false });
    expect(planLooksGood({ ...A, state: "reviewed" }, true)).toEqual({ mark: null, advance: true, pause: false, blocked: false });
  });

  it("the pause strip rule: a notable structure fact records the answer and stops instead of advancing", () => {
    expect(planLooksGood(B, true)).toEqual({ mark: "reviewed", advance: false, pause: true, blocked: false });
  });

  it("Decide later records later once, then refuses with its reason", () => {
    expect(planDecideLater(false)).toEqual({ mark: "later", advance: true, pause: false, blocked: false });
    expect(planDecideLater(true).blocked).toBe(true);
    expect(planDecideLater(true).mark).toBeNull();
  });

  it("a part moved to the end once is one that is later now or whose history shows it on this revision", () => {
    const event = (to: ReviewMarkEvent["to_state"], revision = "r1", kind: ReviewMarkEvent["event_kind"] = "judgment") => ({ to_state: to, revision_id: revision, event_kind: kind }) as ReviewMarkEvent;
    expect(movedToEndOnce({ state: "later" }, null, "r1")).toBe(true);
    expect(movedToEndOnce({ state: "unreviewed" }, [event("later")], "r1")).toBe(true);
    expect(movedToEndOnce({ state: "unreviewed" }, [event("later", "r0")], "r1")).toBe(false);
    expect(movedToEndOnce({ state: "unreviewed" }, [event("reviewed")], "r1")).toBe(false);
    expect(movedToEndOnce({ state: "unreviewed" }, [event("later", "r1", "reconciled")], "r1")).toBe(false);
    expect(movedToEndOnce({ state: "unreviewed" }, null, "r1")).toBe(false);
  });

  it("the confirmation lines", () => {
    expect(confirmationText({ kind: "good", part: "a", next: "apply_discount" })).toBe("Looks good · next: apply_discount");
    expect(confirmationText({ kind: "change", part: "a", next: "apply_discount" })).toBe("Note saved · next: apply_discount");
    expect(confirmationText({ kind: "later", part: "a", next: "apply_discount" })).toBe("Moved to the end · next: apply_discount");
  });

  it("momentum names the next decision, one left for later, or the quick check", () => {
    expect(momentumText(B, C)).toBe("Next: apply_discount · risky");
    expect(momentumText({ ...B, state: "later" }, C)).toBe("Next: apply_discount · you left this for later");
    expect(momentumText(null, C)).toBe("Next: order_total · quick check");
    expect(momentumText(null, null)).toBe("");
  });

  it("interstitial heading and badges", () => {
    expect(interstitialHeading(2)).toBe("Both risky parts judged.");
    expect(interstitialHeading(3)).toBe("All risky parts judged.");
    expect(answerBadge({ state: "reviewed" })).toBe("Looks good");
    expect(answerBadge({ state: "needs_changes" })).toBe("Needs a change");
  });
});

describe("pause strip trigger", () => {
  const site = (over: Record<string, unknown> = {}) => ({
    path: "app/views.py",
    line: 12,
    symbol: "show",
    snippet: "apply_discount(total)",
    kind: "consumer",
    impact_kind: "signature_change",
    in_patch: false,
    exactness: "snapshot",
    uncertainty: "",
    ...over,
  });
  const plan = (sites: unknown[], join: "exact" | "file" = "exact", ids = ["apply_discount"]): ReviewProjection =>
    ({
      context: {
        readiness: { impact_pass: "ran" },
        consumers: [{ key: "g", path: "app/orders.py", symbol: "apply_discount", target_ids: ids, join, known_callers: 1, sites, omitted: 0, unlisted_callers: 0 }],
        consumers_omitted: 0,
      },
    }) as unknown as ReviewProjection;

  it("fires on a checked call site of a changed signature in a file the change did not touch", () => {
    const sites = notableSites(B, plan([site()]));
    expect(sites).toHaveLength(1);
    expect(pauseHeading(1)).toBe("Before you move on · 1 thing the structure shows");
    expect(pauseHeading(2)).toBe("Before you move on · 2 things the structure shows");
    expect(pauseReason("apply_discount", sites)).toBe("We stopped you because 1 call site of apply_discount in app/views.py may not pass its new required parameter.");
  });

  it("does not fire for a site matched by name only, one inside the change, another kind of site, or another part", () => {
    expect(notableSites(B, plan([site({ uncertainty: "another def of this name exists" })]))).toEqual([]);
    expect(notableSites(B, plan([site({ exactness: "current_index" })]))).toEqual([]);
    expect(notableSites(B, plan([site({ exactness: "unknown" })]))).toEqual([]);
    expect(notableSites(B, plan([site({ in_patch: true })]))).toEqual([]);
    expect(notableSites(B, plan([site({ impact_kind: "untouched_caller" })]))).toEqual([]);
    expect(notableSites(A, plan([site()]))).toEqual([]);
    expect(notableSites(B, null)).toEqual([]);
  });

  it("a file-level group (the server records no owning symbol) stops only the part its call line names", () => {
    expect(notableSites(B, plan([site()], "file", []))).toHaveLength(1);
    expect(notableSites(A, plan([site()], "file", []))).toEqual([]);
    expect(notableSites(B, plan([site({ snippet: "" })], "file", []))).toEqual([]);
    expect(notableSites(B, plan([site({ snippet: "apply_discount_all(total)" })], "file", []))).toEqual([]);
  });
});

describe("the only-check-there line", () => {
  it("names the hinted lines and the ones nobody hinted at", () => {
    expect(onlyCheckThereLine({ start: 1, end: 8 }, [{ start: 6, end: 7 }], true)).toBe(
      "AI hinted at lines 6–7. Nothing hinted at lines 1–5 and 8 — you are the only check there.",
    );
    expect(onlyCheckThereLine({ start: 8, end: 12 }, [{ start: 9, end: 11 }], true)).toBe(
      "AI hinted at lines 9–11. Nothing hinted at lines 8 and 12 — you are the only check there.",
    );
  });

  it("is absent with AI off, with no hint inside the part, and drops the second sentence when every line is hinted", () => {
    expect(onlyCheckThereLine({ start: 1, end: 8 }, [{ start: 6, end: 7 }], false)).toBeNull();
    expect(onlyCheckThereLine({ start: 1, end: 8 }, [], true)).toBeNull();
    expect(onlyCheckThereLine({ start: 1, end: 8 }, [{ start: 20, end: 30 }], true)).toBeNull();
    expect(onlyCheckThereLine({ start: 1, end: 3 }, [{ start: 1, end: 5 }], true)).toBe("AI hinted at lines 1–3.");
  });

  it("names overlapping and touching hinted ranges once, as one range", () => {
    expect(mergeRanges([{ start: 8, end: 9 }, { start: 6, end: 7 }, { start: 7, end: 7 }, { start: 12, end: 12 }])).toEqual([
      { start: 6, end: 9 },
      { start: 12, end: 12 },
    ]);
    expect(onlyCheckThereLine({ start: 1, end: 12 }, [{ start: 6, end: 8 }, { start: 7, end: 9 }], true)).toBe(
      "AI hinted at lines 6–9. Nothing hinted at lines 1–5 and 10–12 — you are the only check there.",
    );
  });

  it("overlapping hints merge and a hint is clipped to the part", () => {
    expect(unhintedRanges({ start: 1, end: 10 }, [{ start: 2, end: 4 }, { start: 3, end: 6 }, { start: 9, end: 20 }])).toEqual([
      { start: 1, end: 1 },
      { start: 7, end: 8 },
    ]);
  });
});

describe("the confirmation line tone", () => {
  it("is green for Looks good, amber for a saved note and neutral for a move to the end", () => {
    expect(confirmationTone({ kind: "good", part: "a", next: "b" })).toBe("success");
    expect(confirmationTone({ kind: "change", part: "a", next: "b" })).toBe("warning");
    expect(confirmationTone({ kind: "later", part: "a", next: "b" })).toBe("neutral");
  });
});

describe("the Run test outcome", () => {
  it("takes the verdict from this run's own report", () => {
    expect(testOutcome({ verdicts: { "checks.recipe:auth": "PASS" }, reused: 0 }, "checks.recipe:auth", "test_orders.py")).toEqual({ verdict: "PASS", reused: false, detail: "test_orders.py · passed" });
    expect(testOutcome({ verdicts: { "checks.recipe:auth": "FAIL" }, reused: 0 }, "checks.recipe:auth", "t").detail).toBe("t · failed");
  });

  it("is no result when the report holds no verdict for the entry", () => {
    expect(testOutcome({ verdicts: { other: "PASS" }, reused: 0 }, "checks.recipe:auth", "t")).toEqual({ verdict: "UNKNOWN", reused: false, detail: "t · no result" });
  });

  it("says so when the run was replayed, carried forward or shared", () => {
    for (const reuse of [{ reused: 1 }, { reused: 0, carried: 1 }, { reused: 0, shared_base: 2 }]) {
      const outcome = testOutcome({ verdicts: { e: "PASS" }, ...reuse }, "e", "t");
      expect(outcome.reused).toBe(true);
      expect(outcome.detail).toBe("t · passed · reused from an earlier run, not run again for this click");
    }
  });
});
