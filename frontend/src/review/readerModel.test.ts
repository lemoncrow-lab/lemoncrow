import { describe, expect, it } from "vitest";

import {
  bestTargetAfterRevision,
  filterTargets,
  firstActionable,
  groupReaderFiles,
  outlineSections,
  progressFromTargets,
  stepFile,
  stepTarget,
  targetDisplayLabel,
  targetScrollLocation,
} from "./readerModel";
import type { RevisionTargetDelta, ReviewOutlineItem, ReviewTarget } from "./types";

function target(
  id: string,
  path: string,
  state: ReviewTarget["state"] = "unreviewed",
  options: Partial<ReviewTarget> = {},
): ReviewTarget {
  return {
    target_id: id,
    unit_key: `unit:${id}`,
    kind: "symbol",
    path,
    label: `${path}::${id}`,
    symbol: id,
    start_line: 10,
    end_line: 20,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: 12, end_line: 12, hunk_ordinal: 0 }],
    state,
    changed_since_mark: state === "changed_since_review",
    reviewed_revision_id: state === "reviewed" ? "rrv-1" : "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: ["normal review order"],
    additions: 1,
    deletions: 0,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    ...options,
  };
}

describe("reader model", () => {
  it("groups target order into one continuous file order", () => {
    const rows = [target("a", "src/a.py"), target("b", "src/a.py"), target("c", "src/b.py")];
    expect(groupReaderFiles(rows).map((file) => [file.path, file.targetCount])).toEqual([
      ["src/a.py", 2],
      ["src/b.py", 1],
    ]);
  });

  it("labels multi-span hunk fallbacks as disjoint uncovered work", () => {
    const fallback = target("fallback", "src/reader.tsx", "unreviewed", {
      kind: "hunk",
      symbol: "",
      start_line: 1,
      end_line: 877,
      spans: [
        { side: "new", start_line: 1, end_line: 67, hunk_ordinal: 0 },
        { side: "new", start_line: 664, end_line: 877, hunk_ordinal: 0 },
      ],
    });
    expect(targetDisplayLabel(fallback)).toBe("Uncovered changes · 2 regions");
  });

  it("j/k stepping is target-based and can skip reviewed work", () => {
    const rows = [target("a", "a.py"), target("b", "a.py", "reviewed"), target("c", "b.py")];
    expect(stepTarget(rows, "a", 1)?.target_id).toBe("b");
    expect(stepTarget(rows, "a", 1, { actionableOnly: true })?.target_id).toBe("c");
    expect(stepTarget(rows, "c", -1, { actionableOnly: true })?.target_id).toBe("a");
  });

  it("stops at the ends of the list unless the caller asks it to wrap", () => {
    // j on the last target and k on the first must do nothing at all: silently
    // jumping to the other end of the review reads as a lost place, not as
    // navigation. Wrapping only happens where a caller has asked for a cycle.
    const rows = [target("a", "a.py"), target("b", "a.py"), target("c", "b.py")];
    expect(stepTarget(rows, "c", 1)).toBeNull();
    expect(stepTarget(rows, "a", -1)).toBeNull();
    expect(stepTarget(rows, "c", 1, { wrap: true })?.target_id).toBe("a");
    expect(stepTarget(rows, "a", -1, { wrap: true })?.target_id).toBe("c");
  });

  it("visits every eligible target exactly once around a wrapped cycle", () => {
    const rows = [target("a", "a.py"), target("b", "a.py", "reviewed"), target("c", "b.py")];
    const seen: string[] = [];
    let current = "a";
    for (let step = 0; step < 2; step += 1) {
      const next = stepTarget(rows, current, 1, { actionableOnly: true, wrap: true });
      current = next?.target_id ?? "";
      seen.push(current);
    }
    expect(seen).toEqual(["c", "a"]);
  });

  it("has nowhere to step when nothing is eligible", () => {
    expect(stepTarget([], "a", 1)).toBeNull();
    expect(stepTarget([], "a", -1, { wrap: true })).toBeNull();
    expect(stepTarget([target("a", "a.py", "reviewed")], "a", 1, { actionableOnly: true, wrap: true })).toBeNull();
  });

  it("high-priority stepping includes changed and needs-changes targets", () => {
    const rows = [
      target("a", "a.py"),
      target("b", "b.py", "changed_since_review"),
      target("c", "c.py", "needs_changes"),
    ];
    expect(stepTarget(rows, "a", 1, { highPriorityOnly: true })?.target_id).toBe("b");
    expect(stepTarget(rows, "b", 1, { highPriorityOnly: true })?.target_id).toBe("c");
  });

  it("J/K moves files and lands on an actionable target", () => {
    const rows = [target("a", "a.py"), target("b", "b.py", "reviewed"), target("c", "b.py")];
    expect(stepFile(rows, "a", 1)?.target_id).toBe("c");
    expect(stepFile(rows, "c", -1)?.target_id).toBe("a");
  });

  it("search ANDs terms across target metadata without mutating review order", () => {
    const rows = [
      target("a", "src/z.py", "needs_changes", { reasons: ["public contract changed"] }),
      target("b", "src/a.py"),
    ];
    expect(filterTargets(rows, "z.py contract").map((row) => row.target_id)).toEqual(["a"]);
    expect(filterTargets(rows, "needs changes").map((row) => row.target_id)).toEqual(["a"]);
    expect(filterTargets(rows, "contract missing")).toEqual([]);
    expect(filterTargets(rows, "review note", new Map([["b", "review note about retry behavior"]])).map((row) => row.target_id)).toEqual(["b"]);
    expect(filterTargets(rows, "").map((row) => row.target_id)).toEqual(["a", "b"]);
  });

  it("computes progress only from targets", () => {
    const rows = [target("a", "a.py", "reviewed"), target("b", "a.py"), target("c", "b.py", "unknown")];
    expect(progressFromTargets(rows)).toMatchObject({ target_count: 3, reviewed: 1, unreviewed: 1, unknown: 1 });
  });

  it("selects the first outstanding judgment before settled work", () => {
    const rows = [target("a", "a.py", "reviewed"), target("b", "b.py")];
    expect(firstActionable(rows)?.target_id).toBe("b");
  });

  it("restores the viewport to the surviving active target after a revision", () => {
    const previous = target("b", "src/a.py", "reviewed", { start_line: 30 });
    const rows = [
      target("a", "src/a.py", "reviewed", { start_line: 10 }),
      target("b", "src/a.py", "changed_since_review", { start_line: 30 }),
      target("c", "src/b.py", "unreviewed", { start_line: 5 }),
    ];
    const delta = {
      preserved: [],
      reopened: [],
      added: [],
      removed: [],
      active: [rows[1], rows[2]].map(({ target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts }) => ({
        target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts,
      })),
    } satisfies RevisionTargetDelta;

    expect(bestTargetAfterRevision(previous, rows, delta)?.target_id).toBe("b");
  });

  it("moves a removed viewport to the nearest active target in the same file", () => {
    const previous = target("gone", "src/a.py", "reviewed", { start_line: 50 });
    // The nearer target is deliberately second in review order, so picking the
    // first same-file row rather than the closest one fails this test.
    const rows = [
      target("far", "src/a.py", "unreviewed", { start_line: 5 }),
      target("near", "src/a.py", "unreviewed", { start_line: 44 }),
      target("other", "src/b.py", "unreviewed", { start_line: 50 }),
    ];
    const active = rows.map(({ target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts }) => ({
      target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts,
    }));
    const delta = { preserved: [], reopened: [], added: [], removed: [], active } satisfies RevisionTargetDelta;

    expect(bestTargetAfterRevision(previous, rows, delta)?.target_id).toBe("near");
  });

  it("builds one highest-priority outline section per file", () => {
    const base = (path: string): ReviewOutlineItem => ({
      path,
      target_count: 2,
      reviewed: 0,
      changed_since_review: 0,
      needs_changes: 0,
      unreviewed: 2,
      unknown: 0,
      mechanical: 0,
      attention_rank: 0,
      reasons: ["+1 -0"],
    });
    const rows = [
      { ...base("src/a.py"), changed_since_review: 1 },
      { ...base("tests/a.test.ts"), reasons: ["+2 -1"] },
      { ...base("vendor.lock"), mechanical: 2 },
      { ...base("src/done.py"), reviewed: 2, unreviewed: 0 },
    ];
    const sections = outlineSections(rows);
    expect(sections.map((section) => [section.key, section.rows.map((row) => row.path)])).toEqual([
      ["changed", ["src/a.py"]],
      ["tests", ["tests/a.test.ts"]],
      ["mechanical", ["vendor.lock"]],
      ["done", ["src/done.py"]],
    ]);
  });

  it("maps a target to a concrete CodeView line scroll location", () => {
    expect(targetScrollLocation(target("a", "a.py"))).toEqual({ lineNumber: 12, side: "additions" });
    expect(
      targetScrollLocation(
        target("b", "b.py", "unreviewed", {
          spans: [{ side: "old", start_line: 7, end_line: 7, hunk_ordinal: 0 }],
        }),
      ),
    ).toEqual({ lineNumber: 7, side: "deletions" });
  });
});
