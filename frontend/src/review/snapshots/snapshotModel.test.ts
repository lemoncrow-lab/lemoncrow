import { describe, expect, it } from "vitest";

import { revision } from "../notes/notesFixtures";
import type { Annotation } from "../types";
import {
  baseMovedText, commentCountText, commentCounts, commitText, defaultCompareFrom, newestFirst, newestSaved, partsText, runningDraft,
  snapshotName, snapshotSpec, snapshotTags,
} from "./snapshotModel";

function comment(id: string, revisionId: string, extra: Partial<Annotation> = {}): Annotation {
  return { id, revision_id: revisionId, parent_id: "", state: "open", source: "human", ...extra } as Annotation;
}

describe("the revision menu", () => {
  const rows = newestFirst([revision(0), revision(2, { capture_kind: "draft", is_published_snapshot: false }), revision(1, { is_last_reviewed: true, is_last_commented: true })]);

  it("lists newest first and tells the newest saved revision from the running draft", () => {
    expect(rows.map((row) => row.revision_number)).toEqual([2, 1, 0]);
    expect(newestSaved(rows)?.revision_number).toBe(1);
    expect(runningDraft(rows)?.revision_number).toBe(2);
    expect(snapshotName(rows[0])).toBe("Draft revision 3");
    expect(snapshotName(rows[1])).toBe("Revision 2");
    expect(runningDraft([revision(1), revision(0)])).toBeNull();
  });

  it("tags each row: viewing, newest saved, running draft, last reviewed, feedback, rewritten", () => {
    expect(snapshotTags(rows[0], rows, 1).map((tag) => tag.text)).toEqual(["Running draft"]);
    expect(snapshotTags(rows[1], rows, 1).map((tag) => tag.text)).toEqual(["Viewing", "Newest saved", "Last reviewed", "Latest feedback"]);
    const rewritten = revision(3, { capture_kind: "commit", commit_state: "superseded", has_review_feedback: true });
    expect(snapshotTags(rewritten, [rewritten], 0).map((tag) => tag.text)).toEqual(["Newest saved", "Has feedback", "Rewritten"]);
    expect(snapshotTags(rewritten, [rewritten], 0).find((tag) => tag.text === "Rewritten")?.tone).toBe("warning");
  });

  it("says how the parts changed, whether the base moved, and which commit", () => {
    expect(partsText(revision(0, { review_area_count: 5, previous_revision_number: null }))).toBe("5 initial parts");
    expect(partsText(revision(1, { review_area_count: 6, previous_revision_number: 0, review_area_delta: { added: 1, changed: 2, removed: 0 } }))).toBe("Δ +1 ~2 −0 parts");
    expect(partsText(revision(1))).toBe("");
    expect(baseMovedText(revision(1, { base_moved: true, semantic_changed: false }))).toBe("Base moved · parts unchanged");
    expect(baseMovedText(revision(1, { base_moved: true, semantic_changed: true }))).toBe("Base moved · parts also changed");
    expect(baseMovedText(revision(1))).toBe("");
    expect(commitText(revision(1, { capture_kind: "commit", commit_number: 3, commit_sha: "1a2b3c4d5e6f" }))).toBe("Commit 3 · 1a2b3c4d");
  });

  it("counts reviewer comments by the revision they were written on, open and resolved, and nothing else", () => {
    const counts = commentCounts([
      comment("a", "s0"),
      comment("b", "s0", { state: "resolved" }),
      comment("c", "s9", { origin_revision_number: 1 }),
      comment("reply", "s0", { parent_id: "a" }),
      comment("gone", "s0", { state: "obsolete" }),
      comment("ai", "s0", { source: "ai_review" }),
      comment("unknown", "s7"),
    ], rows);
    expect(counts.get(0)).toEqual({ open: 1, resolved: 1 });
    expect(counts.get(1)).toEqual({ open: 1, resolved: 0 });
    expect(counts.has(7)).toBe(false);
    expect(commentCountText(counts.get(0))).toBe("1 open · 1 resolved");
    expect(commentCountText(undefined)).toBe("No comments");
  });

  it("compares from the newest earlier revision with feedback, else the one before the latest", () => {
    expect(defaultCompareFrom(rows)).toBe(1);
    expect(defaultCompareFrom(newestFirst([revision(0), revision(1), revision(2)]))).toBe(1);
    expect(defaultCompareFrom([revision(0)])).toBe(0);
    expect(defaultCompareFrom([])).toBeNull();
    expect(snapshotSpec(revision(1, { ref: "" }))).toBe("rr/s1");
  });
});
