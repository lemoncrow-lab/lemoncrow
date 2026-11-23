import { describe, expect, it } from "vitest";
import type { ReviewTarget } from "../types";
import { DEFAULT_FLOW_TIMINGS } from "../flowTimings";
import {
  blocksForTarget, coverageRecordRows, emptyLedger, fractionVisible, lineRangesLabel, meterLabel, missingRisky, parseLedger,
  partsFullySeen, seenCount, seenStorageKey, unmeasuredKey, withSeen,
} from "./seenModel";

const target = (unit = "unit/a", side: "new" | "old" = "new", size = 10): ReviewTarget => ({
  target_id: unit, unit_key: unit, path: "src/checkout.py", label: "checkout", symbol: "checkout",
  spans: [{ side, start_line: 10, end_line: 10 + size - 1, hunk_ordinal: 1 }],
  attention_level: "high", priority: { attention: "review_first" },
} as ReviewTarget);

describe("P3-2 D7 deterministic seen coverage", () => {
  it("measures changed blocks with the 600ms floor, 40ms/line, and 3s ceiling", () => {
    expect(blocksForTarget(target("a", "new", 10))[0].thresholdMs).toBe(600);
    expect(blocksForTarget(target("b", "new", 40))[0].thresholdMs).toBe(1600);
    expect(blocksForTarget(target("c", "old", 200))[0].thresholdMs).toBe(3000);
    expect(blocksForTarget(target("d", "new", 10), { ...DEFAULT_FLOW_TIMINGS, seenMinMs: 30 })[0].thresholdMs).toBe(400);
  });
  it("never marks an incompletely exposed block as seen and never marks an approval", () => {
    const t = target();
    const block = blocksForTarget(t)[0];
    const empty = emptyLedger("r", "viewer", "revision");
    expect(withSeen(empty, block, 599, "dwell")).toEqual(empty);
    const complete = withSeen(empty, block, 600, "dwell");
    expect(seenCount(t, complete)).toMatchObject({ seen: 1, total: 1, linesSeen: 10 });
    expect(missingRisky([t], complete)).toHaveLength(0);
    expect((t as ReviewTarget).state).toBeUndefined();
  });
  it("requires 60% of the actual lines to be visible, not 60% of the available viewport", () => {
    const six = Array.from({ length: 10 }, (_, i) => ({ top: i * 10, bottom: i * 10 + 10, height: 10 }));
    expect(fractionVisible(six, 0, 60)).toBe(0.6);
    expect(fractionVisible(six, 0, 50)).toBe(0.5);
    expect(fractionVisible(six, 0, 59)).toBe(0.6);
    expect(fractionVisible(six, 0, 0)).toBe(0);
  });
  it("isolates reviewer and revision and drops invalid or mismatched browser data", () => {
    const empty = emptyLedger("r", "viewer", "revision-1");
    const row = blocksForTarget(target())[0];
    const saved = withSeen(empty, row, 600, "dwell");
    expect(parseLedger(JSON.stringify(saved), "r", "viewer", "revision-1")).toEqual(saved);
    expect(parseLedger(JSON.stringify(saved), "r", "other-viewer", "revision-1").rows).toEqual({});
    expect(parseLedger(JSON.stringify(saved), "r", "viewer", "revision-2").rows).toEqual({});
    expect(parseLedger("{broken", "r", "viewer", "revision-1")).toEqual(empty);
    expect(seenStorageKey("r", "viewer", "revision-1")).not.toBe(seenStorageKey("r", "viewer", "revision-2"));
  });
  it("binds exposure to the captured content fingerprint, even when the unit and line span stay identical", () => {
    const original = { ...target(), content_fingerprint: "sha256:before" } as ReviewTarget;
    const changed = { ...target(), content_fingerprint: "sha256:after" } as ReviewTarget;
    const ledger = withSeen(emptyLedger("r", "viewer", "rev"), blocksForTarget(original)[0], 600, "dwell");
    expect(seenCount(original, ledger).seen).toBe(1);
    expect(seenCount(changed, ledger).seen).toBe(0);
    expect(missingRisky([changed], ledger)).toHaveLength(1);
  });
  it("separates old/new sides and cannot re-use coverage from a different unit or line span", () => {
    const original = target("a", "old", 5);
    const changed = target("a", "new", 5);
    const ledger = withSeen(emptyLedger("r", "viewer", "rev"), blocksForTarget(original)[0], 600, "focus_answer");
    expect(seenCount(original, ledger).seen).toBe(1);
    expect(seenCount(changed, ledger).seen).toBe(0);
    expect(seenCount(target("b", "old", 5), ledger).seen).toBe(0);
    expect(missingRisky([changed], ledger)).toHaveLength(1);
  });
  it("does not count unmeasurable risky parts as complete and keeps them in disclosure", () => {
    const bad = { ...target(), spans: [{ side: "new", start_line: 0, end_line: 0, hunk_ordinal: 1 }] } as ReviewTarget;
    expect(blocksForTarget(bad)).toEqual([]);
    const ledger = emptyLedger("r", "viewer", "rev");
    expect(seenCount(bad, ledger)).toMatchObject({ seen: 0, total: 0 });
    expect(missingRisky([bad], ledger).map(({ target: row }) => row.unit_key)).toEqual([bad.unit_key]);
  });
});

describe("P3-2 risky means the decision answer category", () => {
  const asked = (id: string, category: "decision" | "quick_check" | "accepted_by_rule", attention: string): ReviewTarget =>
    ({ ...target(id), answer: { category, rule: category === "accepted_by_rule" ? "lockfiles" : "" }, attention_level: attention, priority: { attention } }) as ReviewTarget;
  it("lists a decision with ordinary attention and skips a high-attention quick check", () => {
    // A submodule pointer, symlink, executable bit or lockfile decision carries no "high" attention.
    const submodule = asked("submodule", "decision", "normal");
    const noisy = asked("noisy", "quick_check", "high");
    const rule = asked("rule", "accepted_by_rule", "high");
    const ledger = emptyLedger("r", "viewer", "rev");
    expect(missingRisky([submodule, noisy, rule], ledger).map(({ target: row }) => row.unit_key)).toEqual(["submodule"]);
    const seenAll = withSeen(ledger, blocksForTarget(submodule)[0], 600, "dwell");
    expect(missingRisky([submodule, noisy], seenAll)).toHaveLength(0);
  });
  it("treats a part from a server without answers as a decision", () => {
    const old = { ...target("legacy"), answer: undefined, attention_level: "normal", priority: undefined } as unknown as ReviewTarget;
    expect(missingRisky([old], emptyLedger("r", "viewer", "rev"))).toHaveLength(1);
  });
});

describe("P3-2 coverage wording", () => {
  it("writes line ranges, meters and the Wrapped up lines in SPEC's words", () => {
    const disc = { ...target("disc", "new", 3), symbol: "apply_discount", spans: [{ side: "new", start_line: 9, end_line: 11, hunk_ordinal: 1 }] } as ReviewTarget;
    const bare = { ...target("bare"), symbol: "bare_part", spans: [] } as unknown as ReviewTarget;
    const blocks = blocksForTarget(disc);
    expect(lineRangesLabel(blocks)).toBe("lines 9–11");
    expect(lineRangesLabel([{ side: "new", start: 7, end: 7 }])).toBe("line 7");
    expect(lineRangesLabel([{ side: "old", start: 4, end: 8 }, { side: "new", start: 20, end: 22 }])).toBe("lines 4–8 (removed), 20–22");
    expect(meterLabel(0, 3)).toBe("seen 0 of 3 lines");
    expect(meterLabel(0, 1)).toBe("seen 0 of 1 line");
    const ledger = { ...emptyLedger("r", "viewer", "rev"), omission: { blockKeys: [blocks[0].key, unmeasuredKey(bare)], serverRecorded: true, recordedAt: "" } };
    const rows = coverageRecordRows([disc, bare], ledger);
    expect(rows.coverage).toEqual(["apply_discount: seen 0 of 3 changed lines", "bare_part: changed lines unavailable"]);
    expect(rows.skipped).toEqual(["Wrapped up without seeing apply_discount lines 9–11", "Wrapped up without seeing bare_part (changed lines unavailable)"]);
    expect(partsFullySeen([disc, bare], emptyLedger("r", "viewer", "rev"))).toEqual({ seen: 0, total: 1 });
    expect(partsFullySeen([disc], withSeen(emptyLedger("r", "viewer", "rev"), blocks[0], 600, "dwell"))).toEqual({ seen: 1, total: 1 });
  });
});
