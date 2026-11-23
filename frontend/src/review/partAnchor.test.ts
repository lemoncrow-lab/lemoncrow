import { describe, expect, it } from "vitest";

import { partHeaderLocation, partIsTall, revealExpansions, signatureReveals, SIGNATURE_REVEAL_MAX_LINES, TALL_PART_LINES } from "./partAnchor";
import type { ReviewTarget } from "./types";

function part(id: string, start: number, end: number, changeStart: number, changeEnd = changeStart): ReviewTarget {
  return {
    target_id: `target:${id}`,
    unit_key: id,
    kind: "symbol",
    path: "app/auth.py",
    label: `app/auth.py::${id}`,
    symbol: id,
    start_line: start,
    end_line: end,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: changeStart, end_line: changeEnd, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 1,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 1,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

describe("part signature reveal", () => {
  it("reveals a signature the patch folds just above its hunk (the walkthrough's can_view_order)", () => {
    // def can_view_order is line 1; git's hunk starts at line 2.
    const canView = part("can_view_order", 1, 8, 4, 5);
    expect(signatureReveals([canView], [{ additionStart: 2, additionCount: 7 }])).toEqual([
      { targetId: "target:can_view_order", hunkIndex: 0, line: 1, mode: "expand", lines: 1 },
    ]);
  });

  it("leaves a signature the hunk already shows alone", () => {
    const orderTotal = part("order_total", 1, 5, 3);
    expect(signatureReveals([orderTotal], [{ additionStart: 1, additionCount: 6 }])).toEqual([]);
    // Without hunk data there is nothing to reason about.
    expect(signatureReveals([orderTotal], undefined)).toEqual([]);
  });

  it("reveals down to the line above the signature between two hunks, so the header has a row to sit on", () => {
    const method = part("apply_discount", 20, 40, 34);
    const hunks = [{ additionStart: 5, additionCount: 6 }, { additionStart: 31, additionCount: 8 }];
    expect(signatureReveals([method], hunks)).toEqual([
      { targetId: "target:apply_discount", hunkIndex: 1, line: 20, mode: "expand", lines: 12 },
    ]);
  });

  it("pins a signature that sits too far above its change instead of unfolding the whole body", () => {
    const start = 10;
    const hunkTop = start + SIGNATURE_REVEAL_MAX_LINES + 1;
    const long = part("long_function", start, hunkTop + 10, hunkTop + 3);
    expect(signatureReveals([long], [{ additionStart: hunkTop, additionCount: 7 }])).toEqual([
      { targetId: "target:long_function", hunkIndex: 0, line: start, mode: "pin", lines: 0 },
    ]);
    // Exactly at the bound it still unfolds.
    const atBound = part("at_bound", start, hunkTop + 10, hunkTop + 3);
    expect(signatureReveals([atBound], [{ additionStart: start + SIGNATURE_REVEAL_MAX_LINES, additionCount: 7 }])[0]?.mode).toBe("expand");
  });

  it("asks each hunk once, for the most lines any of its parts needs", () => {
    const expansions = revealExpansions([
      { targetId: "a", hunkIndex: 1, line: 20, mode: "expand", lines: 4 },
      { targetId: "b", hunkIndex: 1, line: 18, mode: "expand", lines: 6 },
      { targetId: "c", hunkIndex: 2, line: 90, mode: "pin", lines: 0 },
    ]);
    expect([...expansions]).toEqual([[1, 6], [2, 0]]);
  });
});

describe("part header location", () => {
  it("puts a part's header above its first line, not between its code lines", () => {
    const hunks = [{ additionStart: 1, additionCount: 9 }];
    // order_total starts at line 1: the file-top slot.
    expect(partHeaderLocation(part("order_total", 1, 5, 3), hunks)).toEqual({ side: "additions", lineNumber: 0 });
    // apply_discount starts at line 6 with the change on line 8: after line 5.
    expect(partHeaderLocation(part("apply_discount", 6, 9, 8), hunks)).toEqual({ side: "additions", lineNumber: 5 });
  });

  it("moves above a revealed signature only once the file's contents are on screen", () => {
    const hunks = [{ additionStart: 12, additionCount: 7 }];
    const target = part("can_view_order", 10, 18, 15);
    // Before: the line above the first change, which the patch renders.
    expect(partHeaderLocation(target, hunks, false)).toEqual({ side: "additions", lineNumber: 14 });
    // After the reveal of lines 9-11: right above the signature.
    expect(partHeaderLocation(target, hunks, true)).toEqual({ side: "additions", lineNumber: 9 });
  });

  it("keeps the slot above the first change without hunk data or for a pinned signature", () => {
    const target = part("can_view_order", 10, 18, 15);
    expect(partHeaderLocation(target, undefined, true)).toEqual({ side: "additions", lineNumber: 14 });
    const far = part("far", 1, 200, 150);
    // Pinned at the file's first line: the header stays by the change, not at the file top.
    expect(partHeaderLocation(far, [{ additionStart: 147, additionCount: 7 }], true)).toEqual({ side: "additions", lineNumber: 149 });
    // A folded line-1 signature next to its hunk moves to the file top once revealed.
    const top = part("can_view_order", 1, 8, 4);
    expect(partHeaderLocation(top, [{ additionStart: 2, additionCount: 7 }], false)).toEqual({ side: "additions", lineNumber: 3 });
    expect(partHeaderLocation(top, [{ additionStart: 2, additionCount: 7 }], true)).toEqual({ side: "additions", lineNumber: 0 });
    const farInside = part("far_inside", 60, 200, 150);
    expect(partHeaderLocation(farInside, [{ additionStart: 147, additionCount: 7 }], true)).toEqual({ side: "additions", lineNumber: 149 });
  });
});

describe("tall parts", () => {
  it("counts from the part's first line to its last", () => {
    expect(partIsTall(part("short", 1, 5, 3))).toBe(false);
    expect(partIsTall(part("screen", 1, TALL_PART_LINES, 3))).toBe(true);
  });
});
