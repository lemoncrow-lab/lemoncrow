import { describe, expect, it } from "vitest";
import { answerWord, answeredHeadline, backCounter, carriedOver, confirmedAnswer, moreText, noteTarget, pairedChecks, passOnBothChecks, returnedParts } from "./welcomeBackModel";
import type { Annotation, ReviewRevisionComparison, ReviewSnapshotDelta, ReviewTarget, ReviewVerificationCheck, ReviewVerificationProjection } from "../types";

describe("carriedOver: Kept and Removed since the last revision", () => {
  const delta = (extra: Partial<ReviewSnapshotDelta> = {}): ReviewSnapshotDelta => ({
    since_snapshot: 1, added: [], changed: [], removed: 0, stale_evidence: [], reopened_decisions: [], comments: { carried: 0, resolved: 0 }, ...extra,
  });
  const part = (id: string, state: ReviewTarget["state"], on: string) => ({ target_id: id, state, reviewed_revision_id: on } as ReviewTarget);
  it("keeps only Looks good answers from an earlier revision on a part that did not come new", () => {
    const targets = [part("a", "reviewed", "rev1"), part("b", "reviewed", "rev3"), part("c", "reviewed", ""), part("d", "needs_changes", "rev1"), part("e", "reviewed", "rev1")];
    expect(carriedOver(delta({ added: ["e"] }), targets, "rev3")).toEqual({ since: 1, kept: 1, rebased: 0, removed: 0, removedParts: [] });
  });
  it("names at most 25 removed parts and counts them all", () => {
    const names = Array.from({ length: 26 }, (_, index) => `f${index}`);
    const result = carriedOver(delta({ removed: 40, removed_parts: names }), [], "rev3")!;
    expect(result.removed).toBe(40);
    expect(result.removedParts).toEqual(names.slice(0, 25));
  });
  it("is null with no delta or nothing to say, and speaks for a base-only move alone", () => {
    expect(carriedOver(null, [part("a", "reviewed", "rev1")], "rev3")).toBeNull();
    expect(carriedOver(delta(), [], "rev3")).toBeNull();
    expect(carriedOver(delta(), [], "rev3", 2)?.rebased).toBe(2);
  });
  it("words a discarded answer as the reviewer gave it and counts what a list leaves out", () => {
    expect(answerWord("reviewed")).toBe("Looks good");
    expect(answerWord("needs_changes")).toBe("Needs a change");
    expect(answerWord("unreviewed")).toBe("");
    expect(moreText(25, 25)).toBe("");
    expect(moreText(26, 25)).toBe("and 1 more");
  });
});

const target = (id: string, path = "a.py", symbol = id, extra: Partial<ReviewTarget> = {}): ReviewTarget => ({
  target_id: id, unit_key: id, path, symbol, state: "unreviewed", reviewed_revision_id: "", changed_since_mark: false,
  ...extra,
} as ReviewTarget);
const note = (id: string, part: string, extra: Partial<Annotation> = {}): Annotation => ({
  id, unit_key: part, kind: "request_change", parent_id: "", source: "human", path: "a.py", symbol: part, body: "Fix it",
  ...extra,
} as Annotation);
const comparison = (changed: string[]): ReviewRevisionComparison => ({
  units: changed.map((id) => ({ unit_key: id, status: "changed" })),
  code_available: true,
} as ReviewRevisionComparison);
const check = (name: string, stable_key: string, snapshot_id: string, status: "PASS" | "FAIL", state: "current" | "stale" = "current"): ReviewVerificationCheck => ({
  name, stable_key, snapshot_id, status, state,
} as ReviewVerificationCheck);
const projection = (snapshot_id: string, checks: ReviewVerificationCheck[]): ReviewVerificationProjection => ({
  snapshot_id, checks,
} as ReviewVerificationProjection);

describe("Welcome back – revision matching and approval guards", () => {
  it("returns only changed, uniquely matched parts and also flags extra changes", () => {
    const parts = [target("auth"), target("discount"), target("unchanged")];
    const prior = [note("n1", "auth"), note("n2", "unchanged")];
    const result = returnedParts(parts, prior, comparison(["auth", "discount"]));
    expect(result.returned.map((row) => [row.target.target_id, row.kind])).toEqual([
      ["auth", "note"], ["discount", "extra"],
    ]);
    expect(result.returned[0].notes).toEqual([prior[0]]);
    expect(result.untouched).toEqual([prior[1]]);
    expect(result.unmatched).toEqual([]);
  });

  it("does not claim a note matched when multiple current targets share an identity", () => {
    expect(noteTarget(note("n1", "auth"), [target("auth"), target("auth")])).toBeNull();
    expect(returnedParts([target("new")], [note("n1", "gone")], comparison(["new"])).unmatched).toHaveLength(1);
  });

  it("never carries a previous revision judgment into the new revision", () => {
    expect(confirmedAnswer(target("auth", "a.py", "auth", { state: "reviewed", reviewed_revision_id: "old" }), "new")).toBeNull();
    expect(confirmedAnswer(target("auth", "a.py", "auth", { state: "reviewed", reviewed_revision_id: "new" }), "new")).toBe("fixed");
    expect(confirmedAnswer(target("auth", "a.py", "auth", { state: "needs_changes", reviewed_revision_id: "new" }), "new")).toBe("change");
    expect(confirmedAnswer(target("auth", "a.py", "auth", { state: "reviewed", reviewed_revision_id: "new", changed_since_mark: true }), "new")).toBeNull();
  });

  it("claims paired fail then pass only with matching stable check identity on distinct current snapshots", () => {
    const before = projection("rev1", [check("test_auth", "k1", "rev1", "FAIL"), check("test_unrelated", "k2", "rev1", "PASS")]);
    const after = projection("rev2", [check("test_auth", "k1", "rev2", "PASS"), check("test_unrelated", "k2", "rev2", "PASS"), check("test_new", "k3", "rev2", "PASS")]);
    expect(pairedChecks(before, after)).toEqual([{ name: "test_auth", before: "FAIL", after: "PASS" }]);
    expect(pairedChecks(before, projection("rev2", [check("test_auth", "k1", "rev2", "PASS", "stale")]))).toEqual([]);
    expect(pairedChecks(before, projection("rev1", [check("test_auth", "k1", "rev1", "PASS")]))).toEqual([]);
  });
});

describe("P2-6 BACK-5 pass-on-both and the headline", () => {
  const pass = (a: string, b: string) => [
    projection("r1", [check("t::guest", "k", "r1", a as "PASS" | "FAIL")]),
    projection("r2", [check("t::guest", "k", "r2", b as "PASS" | "FAIL")]),
  ] as const;

  it("flags a check that passed on both revisions, and not one that failed then passed", () => {
    expect(passOnBothChecks(...pass("PASS", "PASS"))).toEqual([{ name: "t::guest" }]);
    expect(passOnBothChecks(...pass("FAIL", "PASS"))).toEqual([]);
    expect(pairedChecks(...pass("PASS", "PASS"))).toEqual([]);
    expect(passOnBothChecks(null, pass("PASS", "PASS")[1])).toEqual([]);
  });

  it("words the headline by how many notes there were", () => {
    expect(answeredHeadline("Claude Code", 1)).toBe("Claude Code answered your note.");
    expect(answeredHeadline("Claude Code", 2)).toBe("Claude Code answered both notes.");
    expect(answeredHeadline("Claude Code", 3)).toBe("Claude Code answered all 3 notes.");
  });
});

describe("4.10 bar centre (review-0.3)", () => {
  it("is the decision screens' widget: one dot per decision, returned parts counted by this screen's answers", () => {
    const parts = [
      target("a", "a.py", "a", { state: "reviewed", changed_since_mark: true }),
      target("b", "a.py", "b", { state: "needs_changes", changed_since_mark: true }),
      target("c", "a.py", "c", { state: "reviewed" }),
      target("d", "a.py", "d", { state: "reviewed", changed_since_mark: true }),
    ];
    const counter = backCounter(parts, [parts[0], parts[1], parts[3]], { a: "fixed", d: "change" }, "b", true);
    expect(counter?.text).toBe("3 of 4 decisions");
    expect(counter?.label).toBe("3 of 4 decisions judged");
    expect(counter?.pips?.map((pip) => [pip.id, pip.state, pip.current])).toEqual([
      ["a", "good", false], ["b", "open", true], ["c", "good", false], ["d", "change", false],
    ]);
  });
  it("counts nothing from a startup slice of the parts list", () => {
    expect(backCounter([target("a")], [], {}, "", false)).toBeNull();
  });
});
