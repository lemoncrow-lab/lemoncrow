import { describe, expect, it } from "vitest";

import {
  bestTargetAfterRevision,
  fileAttention,
  filterTargets,
  firstActionable,
  groupReaderFiles,
  isActionable,
  nextReviewGuideTarget,
  outlineSections,
  progressFromTargets,
  reviewAttentionPath,
  reviewQueue,
  reviewScopeTone,
  reviewStorySteps,
  stepFile,
  stepQueueTarget,
  stepTarget,
  targetAttention,
  targetDisplayLabel,
  targetScrollLocation,
} from "./readerModel";
import type { RevisionTargetDelta, ReviewOutlineItem, ReviewOverview, ReviewTarget, ReviewTargetPriority } from "./types";

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

function priority(options: Partial<ReviewTargetPriority> = {}): ReviewTargetPriority {
  return {
    version: 1,
    tier: 4,
    attention: "understand",
    outstanding: true,
    position: 0,
    reasons: [{ code: "unreviewed", detail: "Not reviewed yet" }],
    ...options,
  };
}

function outlineRow(path: string, options: Partial<ReviewOutlineItem> = {}): ReviewOutlineItem {
  return {
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
    ...options,
  };
}

describe("reader model", () => {
  it("groups part order into one continuous file order", () => {
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
    expect(targetDisplayLabel(fallback)).toBe("Other changed lines · 2 places");
  });

  it("names a lone changed line and a plain module-level part", () => {
    const single = target("line", "src/reader.tsx", "unreviewed", {
      kind: "hunk",
      symbol: "",
      start_line: 2,
      end_line: 2,
      spans: [{ side: "new", start_line: 2, end_line: 2, hunk_ordinal: 0 }],
    });
    expect(targetDisplayLabel(single)).toBe("Changed line 2");
    const section = target("top", "src/reader.tsx", "unreviewed", { kind: "document_section", symbol: "" });
    expect(targetDisplayLabel(section)).toBe("Imports and top-level code");
  });

  it("uses diff semantics for scope tone instead of review state", () => {
    expect(reviewScopeTone(target("added", "a.ts"))).toBe("added");
    expect(reviewScopeTone(target("deleted", "d.ts", "needs_changes", {
      spans: [{ side: "old", start_line: 12, end_line: 14, hunk_ordinal: 0 }],
      additions: 0,
      deletions: 3,
    }))).toBe("deleted");
    expect(reviewScopeTone(target("modified", "m.ts", "reviewed", {
      spans: [
        { side: "old", start_line: 12, end_line: 13, hunk_ordinal: 0 },
        { side: "new", start_line: 12, end_line: 14, hunk_ordinal: 0 },
      ],
      additions: 3,
      deletions: 2,
    }))).toBe("modified");
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

  it("visits every eligible part exactly once around a wrapped cycle", () => {
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

  it("J/K moves files and lands on an actionable part", () => {
    const rows = [target("a", "a.py"), target("b", "b.py", "reviewed"), target("c", "b.py")];
    expect(stepFile(rows, "a", 1)?.target_id).toBe("c");
    expect(stepFile(rows, "c", -1)?.target_id).toBe("a");
  });

  it("search ANDs terms across part metadata without mutating review order", () => {
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

  it("computes progress only from parts", () => {
    const rows = [target("a", "a.py", "reviewed"), target("b", "a.py"), target("c", "b.py", "unknown")];
    expect(progressFromTargets(rows)).toMatchObject({ target_count: 3, reviewed: 1, unreviewed: 1, unknown: 1 });
  });

  it("selects the first outstanding judgment before settled work", () => {
    const rows = [target("a", "a.py", "reviewed"), target("b", "b.py")];
    expect(firstActionable(rows)?.target_id).toBe("b");
  });

  it("follows server positions and never re-ranks by reason text", () => {
    const loud = target("t:loud", "a.py", "unreviewed", {
      reasons: ["public contract changed"],
      attention_level: "high",
      priority: priority({ position: 2, tier: 4, attention: "understand" }),
    });
    const asked = target("t:asked", "z.py", "needs_changes", {
      symbol: "authorize",
      priority: priority({
        position: 0,
        tier: 1,
        attention: "review_first",
        reasons: [{ code: "request_change_open", detail: "Requested change is still open" }],
      }),
    });
    const plain = target("t:plain", "m.py", "unreviewed", {
      priority: priority({ position: 1, tier: 4, attention: "understand" }),
    });
    const done = target("t:done", "b.py", "reviewed", {
      priority: priority({ position: 3, tier: 6, attention: "done", outstanding: false }),
    });
    const input = [loud, done, plain, asked];

    expect(reviewQueue(input).map((row) => row.target_id)).toEqual(["t:asked", "t:plain", "t:loud"]);
    expect(nextReviewGuideTarget(input)).toEqual({
      targetId: "t:asked",
      path: "z.py",
      label: "authorize",
      reason: "Requested change is still open",
      reasonCode: "request_change_open",
      attention: "review_first",
    });
    expect(reviewAttentionPath(input).map((row) => row.targetId)).toEqual(["t:asked", "t:plain", "t:loud"]);
    expect(stepQueueTarget(input, "t:asked", 1)?.target_id).toBe("t:plain");
    expect(stepQueueTarget(input, "t:loud", 1)?.target_id).toBe("t:asked");
    expect(stepQueueTarget(input, "t:asked", -1)?.target_id).toBe("t:loud");
    expect(stepQueueTarget(input, "t:done", 1)?.target_id).toBe("t:asked");
    expect(firstActionable(input)?.target_id).toBe("t:asked");
    expect(isActionable(done)).toBe(false);
  });

  it("keeps the server's pinned queue whatever the list order and legacy ranks say (stable slots)", () => {
    // The server pinned an in-progress reviewer's order (positions 0..2) after an
    // impact re-rank; the kept list order and the legacy scalar ranks disagree.
    const first = target("t:first", "c.py", "unreviewed", {
      attention_rank: 3,
      priority: priority({ position: 0, tier: 4, attention: "understand" }),
    });
    const second = target("t:second", "b.py", "unreviewed", {
      attention_rank: 2,
      priority: priority({ position: 1, tier: 4, attention: "understand" }),
    });
    const third = target("t:third", "a.py", "unreviewed", {
      attention_rank: 1,
      priority: priority({ position: 2, tier: 4, attention: "understand" }),
    });
    const input = [third, second, first];

    expect(reviewQueue(input).map((row) => row.target_id)).toEqual(["t:first", "t:second", "t:third"]);
    expect(firstActionable(input)?.target_id).toBe("t:first");
    expect(nextReviewGuideTarget(input)?.targetId).toBe("t:first");
    expect(stepQueueTarget(input, "t:first", 1)?.target_id).toBe("t:second");
    expect(stepQueueTarget(input, "t:third", -1)?.target_id).toBe("t:second");
    expect([...fileAttention(input).entries()].map(([path, attention]) => [path, attention.attention_rank])).toEqual([
      ["a.py", 2],
      ["b.py", 1],
      ["c.py", 0],
    ]);
  });

  describe("one canonical attention classification (plan §8.8)", () => {
    // A mixed Review as the server places it: one target per tier, plus a
    // plan question that promotes an unreviewed target to Review first. The
    // server lists the reason that placed a target first.
    const mixed = [
      target("t:plain", "src/store.py", "unreviewed", {
        start_line: 40,
        priority: priority({ position: 3 }),
      }),
      target("t:done", "src/done.py", "reviewed", {
        reasons: ["public contract changed"],
        priority: priority({
          position: 5,
          tier: 6,
          attention: "done",
          outstanding: false,
          reasons: [{ code: "reviewed", detail: "Reviewed; content unchanged" }],
        }),
      }),
      target("t:routine", "src/fmt.py", "unreviewed", {
        priority: priority({
          position: 4,
          tier: 5,
          attention: "routine",
          reasons: [{ code: "routine_comment_only", detail: "Comment-only change" }],
        }),
      }),
      target("t:conflict", "src/store.py", "unreviewed", {
        start_line: 5,
        priority: priority({
          position: 2,
          tier: 3,
          attention: "review_first",
          reasons: [
            { code: "author_guidance_conflict", detail: "Authors disagree about retention" },
            { code: "unreviewed", detail: "Not reviewed yet" },
          ],
        }),
      }),
      target("t:changed", "src/auth.py", "changed_since_review", {
        priority: priority({
          position: 1,
          tier: 2,
          attention: "review_first",
          reasons: [{ code: "changed_since_judgment", detail: "Changed since your review" }],
        }),
      }),
      target("t:asked", "src/api.py", "needs_changes", {
        priority: priority({
          position: 0,
          tier: 1,
          attention: "review_first",
          reasons: [{ code: "request_change_open", detail: "Requested change is still open" }],
        }),
      }),
    ];

    it("orders the queue and names each reason from the same result", () => {
      const queue = reviewQueue(mixed);
      expect(queue.map((row) => row.target_id)).toEqual(["t:asked", "t:changed", "t:conflict", "t:plain", "t:routine"]);
      const classified = queue.map((row) => targetAttention(row));
      // Order is the classification's rank; tiers never go backwards along it.
      expect(classified.map((item) => item.attention_rank)).toEqual([0, 1, 2, 3, 4]);
      expect(classified.map((item) => item.tier)).toEqual([1, 2, 3, 4, 5]);
      expect(classified.map((item) => [item.reason_code, item.attention])).toEqual([
        ["request_change_open", "review_first"],
        ["changed_since_judgment", "review_first"],
        ["author_guidance_conflict", "review_first"],
        ["unreviewed", "understand"],
        ["routine_comment_only", "routine"],
      ]);
      // The Next card and `]` path say exactly what the classification says.
      expect(reviewAttentionPath(mixed).map((item) => [item.targetId, item.reason, item.reasonCode, item.attention])).toEqual(
        queue.map((row, index) => [row.target_id, classified[index].reason_label, classified[index].reason_code, classified[index].attention]),
      );
      expect(reviewAttentionPath(mixed)[2].reason).toBe("Authors disagree about retention");
      expect(targetAttention(mixed[1])).toMatchObject({ actionable: false, attention: "done", tier: 6 });
    });

    it("gives each file its first queue entry's section and reason, whatever the server row says", () => {
      const byPath = fileAttention(mixed);
      // store.py holds a Review-first question (rank 2) and ordinary work
      // (rank 3); the file reads as its first queue entry.
      expect(byPath.get("src/store.py")).toMatchObject({
        attention: "review_first",
        reason_code: "author_guidance_conflict",
        reason_label: "Authors disagree about retention",
        attention_rank: 2,
      });
      expect(byPath.get("src/done.py")).toMatchObject({ actionable: false, attention: "done" });
      for (const [path, attention] of byPath) {
        const first = reviewQueue(mixed).find((row) => row.path === path);
        if (first) expect(attention).toEqual(targetAttention(first));
      }

      // Server rows as they read mid-flight: stale sections and reasons. The
      // canonical classification decides the section instead.
      const rows = [
        outlineRow("src/api.py", { section: "unreviewed", position: 0 }),
        outlineRow("src/auth.py", { section: "review_first", position: 1 }),
        outlineRow("src/store.py", { section: "unreviewed", position: 2, next_reason: "Not reviewed yet" }),
        outlineRow("src/fmt.py", { section: "unreviewed", position: 4 }),
        outlineRow("src/done.py", { section: "unreviewed", position: 5 }),
      ];
      expect(outlineSections(rows, "", byPath).map((section) => [section.key, section.rows.map((row) => row.path)])).toEqual([
        ["review_first", ["src/api.py", "src/auth.py", "src/store.py"]],
        ["mechanical", ["src/fmt.py"]],
        ["done", ["src/done.py"]],
      ]);
    });

    it("classifies a synthetic compare row by its own state, in list order", () => {
      const compare = [
        target("c:b", "b.py", "unknown", { reasons: ["Changed between Revision 3 and Revision 4"] }),
        target("c:a", "a.py", "unknown", { reasons: ["+3 -1"] }),
      ];
      expect(compare.map((row, index) => targetAttention(row, index))).toEqual([
        { tier: 4, attention: "understand", reason_code: "", reason_label: "Changed between Revision 3 and Revision 4", actionable: true, attention_rank: 0 },
        { tier: 4, attention: "understand", reason_code: "", reason_label: "Needs human judgment", actionable: true, attention_rank: 1 },
      ]);
      expect(reviewQueue(compare).map((row) => row.target_id)).toEqual(["c:b", "c:a"]);
      expect(nextReviewGuideTarget(compare)).toMatchObject({ reason: "Changed between Revision 3 and Revision 4", reasonCode: "", attention: "" });
    });
  });

  it("maps the deterministic change story to navigable chapter paths", () => {
    const overview = {
      change_story: ["Identity", "API"],
      chapters: {
        intent: [
          { key: "intent:identity", label: "Identity", reason: "", rows: [{ path: "src/auth.py" }], file_count: 1, attention_count: 1, reviewed_count: 0, changed_count: 0, min_attention_rank: 1, depends_on: [] },
          { key: "intent:api", label: "API", reason: "", rows: [{ path: "src/api.py" }], file_count: 1, attention_count: 0, reviewed_count: 0, changed_count: 0, min_attention_rank: 2, depends_on: [] },
        ],
        dependency: [],
        commits: [],
      },
    } as unknown as ReviewOverview;
    expect(reviewStorySteps(overview)).toEqual([
      { label: "Identity", path: "src/auth.py" },
      { label: "API", path: "src/api.py" },
    ]);
  });

  it("restores the viewport to the surviving active part after a revision", () => {
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

  it("moves a removed viewport to the nearest active part in the same file", () => {
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

  it("buckets outline rows by server section and keeps canonical order under a filter", () => {
    const rows = [
      outlineRow("src/z.py", { section: "review_first", position: 0 }),
      outlineRow("lib/tests/test_a.py", { section: "unreviewed", position: 1 }),
      outlineRow("src/a.py", { section: "unreviewed", position: 2, reasons: ["public contract changed"] }),
      outlineRow("src/done.py", { section: "done", position: 3, reviewed: 2, unreviewed: 0 }),
    ];
    expect(outlineSections(rows).map((section) => [section.key, section.label, section.rows.map((row) => row.path)])).toEqual([
      ["review_first", "Review first", ["src/z.py"]],
      ["unreviewed", "Unreviewed", ["lib/tests/test_a.py", "src/a.py"]],
      ["done", "Done", ["src/done.py"]],
    ]);
    expect(outlineSections([outlineRow("src/fmt.go", { section: "mechanical", position: 0 })]).map((section) => section.label))
      .toEqual(["Routine"]);
    expect(outlineSections(rows, "src/").flatMap((section) => section.rows.map((row) => row.path))).toEqual([
      "src/z.py",
      "src/a.py",
      "src/done.py",
    ]);
  });

  it("falls back to counts, never reasons or paths, for a row without a server section", () => {
    const rows = [
      outlineRow("tests/a.test.ts", { reasons: ["public contract changed"] }),
      outlineRow("src/done.py", { reviewed: 2, unreviewed: 0 }),
    ];
    expect(outlineSections(rows).map((section) => [section.key, section.rows.map((row) => row.path)])).toEqual([
      ["unreviewed", ["tests/a.test.ts"]],
      ["done", ["src/done.py"]],
    ]);
  });

  it("maps a part to a concrete CodeView line scroll location", () => {
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
