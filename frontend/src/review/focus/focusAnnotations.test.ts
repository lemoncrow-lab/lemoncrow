import { describe, expect, it } from "vitest";
import type { Annotation, ReviewTarget } from "../types";
import { aiFindingsForPart, aiHintRanges, inlineNotesForPart, notesForPart, notesOnRow, visibleReviewNotes } from "./focusAnnotations";
import type { PartRow } from "./partDiff";

function note(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "n1", review_id: "review-1", revision_id: "rev1", parent_id: "",
    kind: "request_change", state: "open", source: "human", created_by_actor: "reviewer",
    body: "Keep the permission check", path: "src/auth.py", unit_key: "auth:can_view",
    anchored: true, anchor_exact: true, start_line: 6, end_line: 7, side: "additions",
    file_level: false, relation_to_revision: "originating", ...overrides,
  } as Annotation;
}
const target = { path: "src/auth.py", unit_key: "auth:can_view" } as ReviewTarget;
const add: PartRow = { kind: "add", num: 6, text: "return True", at: 0 };
const deleted: PartRow = { kind: "del", num: 6, text: "return False", at: 6 };

describe("Focus persisted notes, never inferred from paths", () => {
  it("counts only human root change requests on the current or inherited revision", () => {
    const notes = [
      note(), note({ id: "n2", revision_id: "rev0", relation_to_revision: "inherited" }),
      note({ id: "n3", revision_id: "rev0", relation_to_revision: "future" }),
      note({ id: "n4", revision_id: "rev0", relation_to_revision: "originating" }),
      note({ id: "n5", source: "ai_review", created_by_actor: "agent" }),
      note({ id: "n6", parent_id: "n1" }),
      note({ id: "n7", state: "obsolete" }),
      note({ id: "n8", review_id: "another-tenant-review" }),
    ];
    expect(visibleReviewNotes(notes, "review-1", "rev1").map((item) => item.id)).toEqual(["n1", "n2"]);
  });

  it("attaches a note to one exact unit, not a neighboring symbol in the same file", () => {
    expect(notesForPart([note(), note({ id: "next", unit_key: "auth:other" }), note({ id: "elsewhere", path: "src/other.py" })], target).map((item) => item.id)).toEqual(["n1"]);
  });

  it("paints only exact and anchored line notes on the right side", () => {
    expect(notesOnRow([note()], add).map((item) => item.id)).toEqual(["n1"]);
    expect(notesOnRow([note()], deleted)).toEqual([]);
    expect(notesOnRow([note({ side: "deletions" })], deleted)).toHaveLength(1);
    expect(notesOnRow([note({ anchor_exact: false }), note({ anchored: false }), note({ file_level: true })], add)).toEqual([]);
    expect(notesOnRow([note({ start_line: 12, end_line: 14 })], add)).toEqual([]);
  });

  it("shows AI findings only on exact current Snapshot and part when AI is available", () => {
    const ai = note({ source: "ai_review", kind: "suggestion", unit_key: "auth:can_view", start_line: 6, end_line: 7, side: "additions", finding_disposition: "open" });
    expect(aiFindingsForPart([ai], "review-1", "rev1", target, true)).toEqual([ai]);
    expect(aiHintRanges([ai], { ...target, start_line: 5, end_line: 8 })).toEqual([{ start: 6, end: 7 }]);
    expect(aiFindingsForPart([ai], "review-1", "rev1", target, false)).toEqual([]);
    expect(aiFindingsForPart([ai], "wrong-review", "rev1", target, true)).toEqual([]);
    expect(aiFindingsForPart([ai], "review-1", "rev2", target, true)).toEqual([]);
    expect(aiFindingsForPart([note({ ...ai, anchored: false }), note({ ...ai, anchor_exact: false }), note({ ...ai, state: "resolved" }), note({ ...ai, finding_disposition: "dismissed" }), note({ ...ai, unit_key: "other" })], "review-1", "rev1", target, true)).toEqual([]);
  });

  it("does not paint orphaned notes even when their previous line matches", () => {
    expect(notesOnRow([note({ anchored: false, anchor_exact: false, state: "orphaned" })], add)).toEqual([]);
  });
});

describe("inline line comments on a part", () => {
  it("paints human root comments and change requests tied by unit key or an anchored line inside the part", () => {
    const part = { ...target, start_line: 4, end_line: 19 } as ReviewTarget;
    const notes = [
      note({ id: "c1", kind: "comment" }),
      note({ id: "c2", kind: "comment", unit_key: "", start_line: 10, end_line: 10 }),
      note({ id: "c3", kind: "comment", unit_key: "", start_line: 30, end_line: 30 }),
      note({ id: "c4", kind: "comment", path: "src/other.py" }),
      note({ id: "c5", kind: "comment", parent_id: "c1" }),
      note({ id: "c6", kind: "comment", source: "ai_review", created_by_actor: "agent" }),
      note({ id: "c7", kind: "comment", state: "obsolete" }),
      note({ id: "c8" }),
    ];
    expect(inlineNotesForPart(notes, "review-1", "rev1", part).map((item) => item.id)).toEqual(["c1", "c2", "c8"]);
  });
});
