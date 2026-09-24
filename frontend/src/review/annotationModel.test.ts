/**
 * The annotation UI's decisions, tested without a DOM.
 *
 * Two of these assertions are the browser half of the project's central rule:
 * an orphaned comment is **never** drawn on a line, and a marker carries our
 * annotation key and nothing else. A UI that painted an orphan at its last
 * known position would be performing exactly the silent relocation the
 * relocation ladder refuses to perform on the server.
 */

import { describe, expect, it } from "vitest";

import {
  DRAFT_ID,
  KIND_LABELS,
  KIND_ORDER,
  anchorAccent,
  commentsAt,
  fileComments,
  isAnchored,
  locationLabel,
  markerLines,
  markerSide,
  markerSignature,
  partition,
  repliesFor,
} from "./annotationModel";
import type { Annotation, AnnotationKind } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "look at this",
    created_by: "local",
    created_by_actor: "human",
    created_at: "2026-09-08T00:00:00Z",
    updated_at: "2026-09-08T00:00:00Z",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "file unchanged in this revision",
    anchored: true,
    path: "src/app.py",
    side: "new",
    start_line: 10,
    end_line: 10,
    unit_key: "sym:abc",
    symbol: "refresh",
    origin_symbol: "",
    ...overrides,
  };
}

describe("sides", () => {
  it("translates both vocabularies in one direction only", () => {
    expect(markerSide("new")).toBe("additions");
    expect(markerSide("additions")).toBe("additions");
    expect(markerSide("old")).toBe("deletions");
    expect(markerSide("deletions")).toBe("deletions");
  });

  it("treats an unrecognised side as the new side rather than throwing", () => {
    expect(markerSide("document")).toBe("additions");
  });
});

describe("partition", () => {
  it("keeps orphans out of the drawable set", () => {
    const live = annotation({ id: "ann-live" });
    const lost = annotation({
      id: "ann-lost",
      state: "orphaned",
      anchored: false,
      anchor_method: "orphaned",
      anchor_detail: "3 candidate locations",
    });

    const { anchored, orphaned } = partition([live, lost], "src/app.py");
    expect(anchored.map((item) => item.id)).toEqual(["ann-live"]);
    expect(orphaned.map((item) => item.id)).toEqual(["ann-lost"]);
  });

  it("never draws an orphan on a line, even though it still has one", () => {
    const lost = annotation({ state: "orphaned", anchored: false, start_line: 10 });
    expect(lost.start_line).toBe(10);
    expect(markerLines([lost], "src/app.py", null)).toEqual([]);
  });

  it("keeps a file-level comment out of line zero and in the file thread", () => {
    const wholeFile = annotation({
      file_level: true,
      start_line: 0,
      end_line: 0,
      anchor_method: "file",
      anchor_method_label: "file-level comment",
    });
    expect(markerLines([wholeFile], "src/app.py", null)).toEqual([]);
    expect(fileComments([wholeFile], "src/app.py")).toEqual([wholeFile]);
    expect(locationLabel(0, 0)).toBe("file");
  });

  it("trusts the server's `anchored` rather than re-deriving it from the state", () => {
    // A comment that was never anchorable is `open` and carries a line number.
    // Inferring "open, therefore drawable" would put it on a line nobody chose.
    const never = annotation({ anchored: false, anchor_method: "unresolved", state: "open" });
    expect(isAnchored(never)).toBe(false);
    expect(markerLines([never], "src/app.py", null)).toEqual([]);
  });

  it("ignores comments on other files", () => {
    const elsewhere = annotation({ id: "ann-2", path: "src/other.py" });
    expect(partition([annotation(), elsewhere], "src/app.py").anchored).toHaveLength(1);
  });

  it("leaves replies out of the top level and finds them under their parent", () => {
    const parent = annotation({ id: "ann-parent" });
    const reply = annotation({ id: "ann-reply", parent_id: "ann-parent", body: "because" });

    expect(partition([parent, reply], "src/app.py").anchored.map((item) => item.id)).toEqual(["ann-parent"]);
    expect(repliesFor([parent, reply], "ann-parent").map((item) => item.body)).toEqual(["because"]);
  });
});

describe("markers", () => {
  it("carries only our own key across the viewer boundary", () => {
    const [marker] = markerLines([annotation()], "src/app.py", null);
    expect(Object.keys(marker.metadata)).toEqual(["key"]);
    expect(marker).toEqual({ side: "additions", lineNumber: 10, metadata: { key: "additions:10" } });
  });

  it("collapses two comments on one line into a single marker", () => {
    const markers = markerLines([annotation({ id: "a" }), annotation({ id: "b" })], "src/app.py", null);
    expect(markers).toHaveLength(1);
    expect(commentsAt([annotation({ id: "a" }), annotation({ id: "b" })], "src/app.py", "additions:10")).toHaveLength(
      2,
    );
  });

  it("separates the two sides of one line number", () => {
    const markers = markerLines(
      [annotation({ id: "a" }), annotation({ id: "b", side: "old" })],
      "src/app.py",
      null,
    );
    expect(markers.map((item) => item.metadata.key).sort()).toEqual(["additions:10", "deletions:10"]);
  });

  it("adds a marker for a draft below the last selected line", () => {
    const markers = markerLines([], "src/app.py", {
      path: "src/app.py",
      startLine: 4,
      endLine: 6,
      side: "additions",
    });
    expect(markers).toEqual([{ side: "additions", lineNumber: 6, metadata: { key: "additions:6" } }]);
  });

  it("draws a range comment under its last line so it never splits the range", () => {
    const ranged = annotation({ start_line: 3, end_line: 5 });
    expect(markerLines([ranged], "src/app.py", null)).toEqual([
      { side: "additions", lineNumber: 5, metadata: { key: "additions:5" } },
    ]);
    expect(commentsAt([ranged], "src/app.py", "additions:5")).toHaveLength(1);
    expect(commentsAt([ranged], "src/app.py", "additions:3")).toHaveLength(0);
  });

  it("does not add a second marker when the draft lands on an existing comment", () => {
    const markers = markerLines([annotation()], "src/app.py", {
      path: "src/app.py",
      startLine: 10,
      endLine: 10,
      side: "additions",
    });
    expect(markers).toHaveLength(1);
  });

  it("ignores a draft aimed at another file", () => {
    const markers = markerLines([], "src/app.py", {
      path: "src/other.py",
      startLine: 1,
      endLine: 1,
      side: "additions",
    });
    expect(markers).toEqual([]);
  });

  it("orders markers by line so the viewer never receives them backwards", () => {
    const markers = markerLines(
      [annotation({ id: "a", start_line: 40, end_line: 40 }), annotation({ id: "b", start_line: 4, end_line: 4 })],
      "src/app.py",
      null,
    );
    expect(markers.map((item) => item.lineNumber)).toEqual([4, 40]);
  });
});

describe("markerSignature", () => {
  it("is stable when nothing about the markers changed", () => {
    const before = markerSignature([annotation()], "src/app.py", null);
    const after = markerSignature([annotation({ body: "reworded" })], "src/app.py", null);
    expect(after).toBe(before);
  });

  it("moves when a comment is added, relocated, resolved or re-dispositioned", () => {
    const base = markerSignature([annotation()], "src/app.py", null);
    expect(markerSignature([annotation(), annotation({ id: "ann-2", start_line: 3 })], "src/app.py", null)).not.toBe(
      base,
    );
    expect(markerSignature([annotation({ start_line: 11 })], "src/app.py", null)).not.toBe(base);
    expect(markerSignature([annotation({ state: "resolved" })], "src/app.py", null)).not.toBe(base);
    expect(markerSignature([annotation({ kind: "request_change" })], "src/app.py", null)).not.toBe(base);
  });

  it("moves when a reply lands, so the card re-renders with the thread", () => {
    const base = markerSignature([annotation()], "src/app.py", null);
    const reply = annotation({ id: "ann-reply", parent_id: "ann-1" });
    expect(markerSignature([annotation(), reply], "src/app.py", null)).not.toBe(base);
  });

  it("moves when a comment orphans, because its marker has to disappear", () => {
    const base = markerSignature([annotation()], "src/app.py", null);
    const lost = annotation({ state: "orphaned", anchored: false });
    expect(markerSignature([lost], "src/app.py", null)).not.toBe(base);
  });

  it("moves when a draft opens and closes", () => {
    const closed = markerSignature([], "src/app.py", null);
    const open = markerSignature([], "src/app.py", {
      path: "src/app.py",
      startLine: 2,
      endLine: 2,
      side: "additions",
    });
    expect(open).not.toBe(closed);
  });
});

describe("the disposition set", () => {
  it("offers three new-comment dispositions while keeping legacy looks-good renderable", () => {
    const offered: AnnotationKind[] = ["comment", "request_change", "suggestion"];
    expect(KIND_ORDER).toEqual(offered);
    expect(KIND_ORDER).not.toContain("looks_good");
    expect(KIND_LABELS.looks_good).toBe("Looks good");
  });
});

describe("labels", () => {
  it("spells a range the way the CLI spells it", () => {
    expect(locationLabel(10, 10)).toBe("L10");
    expect(locationLabel(10, 20)).toBe("L10-L20");
  });

  it("reserves a draft id that no server id can collide with", () => {
    expect(DRAFT_ID.startsWith("__")).toBe(true);
  });
});

describe("how a comment says it got to its line", () => {
  it("draws a heuristic re-find differently from an untouched file", () => {
    // The judge's case: the function was renamed and its docstring rewritten,
    // and the comment survived only because its text happened to be unique.
    // Given the same weight as `identical_blob`, the reader cannot tell the
    // two apart -- and the ladder's whole value is that they are different.
    const exact = anchorAccent(annotation());
    const heuristic = anchorAccent(
      annotation({ anchor_method: "unique_text", anchor_method_label: "re-found by its text alone", anchor_exact: false }),
    );
    expect(exact).not.toBe(heuristic);
  });

  it("draws an orphan differently again", () => {
    const orphan = anchorAccent(
      annotation({ anchored: false, state: "orphaned", anchor_method: "orphaned", anchor_exact: false }),
    );
    expect(orphan).not.toBe(anchorAccent(annotation()));
    expect(orphan).not.toBe(
      anchorAccent(annotation({ anchor_method: "unique_text", anchor_exact: false })),
    );
  });
});
