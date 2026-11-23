import { describe, expect, it } from "vitest";

import { note, target } from "./notesFixtures";
import { inNotesFilter, listedNotes, noteLocation, notesButtonText, notesCounts, noteTarget } from "./notesModel";

const NOTES = [
  note("open"),
  note("reply", { parent_id: "open" }),
  note("done", { state: "resolved" }),
  note("gone", { state: "obsolete" }),
  note("agent-said", { author_response: "addressed", author_response_source_id: "sess-1" }),
  note("moved", { state: "orphaned", anchored: false }),
  note("moved-done", { state: "resolved", anchored: false }),
  note("finding", { source: "ai_review", finding_disposition: "open" }),
  note("dismissed", { source: "ai_review", finding_disposition: "dismissed" }),
  note("fixed", { source: "ai_review", finding_disposition: "fixed" }),
  note("lemon", { source: "lemoncrow" }),
  note("author", { source: "author" }),
];

describe("the Notes list", () => {
  it("lists root comments and AI findings; never replies, obsolete notes, or author and LemonCrow notes", () => {
    expect(listedNotes(NOTES).map((item) => item.id)).toEqual(["open", "done", "agent-said", "moved", "moved-done", "finding", "dismissed", "fixed"]);
  });

  it("counts Open as everything not settled, with re-review and moved lines as parts of it", () => {
    expect(notesCounts(NOTES)).toEqual({ open: 4, rereview: 1, resolved: 4, orphaned: 1 });
    const ids = (filter: Parameters<typeof inNotesFilter>[1]) => listedNotes(NOTES).filter((item) => inNotesFilter(item, filter)).map((item) => item.id);
    expect(ids("open")).toEqual(["open", "agent-said", "moved", "finding"]);
    expect(ids("rereview")).toEqual(["agent-said"]);
    expect(ids("orphaned")).toEqual(["moved"]);
    // A dismissed or fixed finding is settled like a resolved comment.
    expect(ids("resolved")).toEqual(["done", "moved-done", "dismissed", "fixed"]);
  });

  it("names the top bar control by what is open", () => {
    expect(notesButtonText(3)).toBe("Notes · 3 open");
    expect(notesButtonText(0)).toBe("Notes");
  });

  it("finds a note's part by its unit, else by the line it sits on; never for moved lines or a change-wide note", () => {
    const targets = [target("a", { unit_key: "u-a", path: "src/a.py" }), target("b", { unit_key: "u-b", path: "src/b.py" })];
    expect(noteTarget(note("x"), targets)?.target_id).toBe("a");
    expect(noteTarget(note("x", { unit_key: "", path: "src/b.py", start_line: 5 }), targets)?.target_id).toBe("b");
    expect(noteTarget(note("x", { unit_key: "", path: "src/b.py", start_line: 50 }), targets)).toBeNull();
    expect(noteTarget(note("x", { anchored: false }), targets)).toBeNull();
    expect(noteTarget(note("x", { comment_scope: "change" }), targets)).toBeNull();
  });

  it("says where a note is", () => {
    expect(noteLocation(note("x"))).toBe("src/a.py:10-12");
    expect(noteLocation(note("x", { end_line: 10 }))).toBe("src/a.py:10");
    expect(noteLocation(note("x", { file_level: true }))).toBe("src/a.py · file");
    expect(noteLocation(note("x", { comment_scope: "change" }))).toBe("Change discussion");
  });
});
