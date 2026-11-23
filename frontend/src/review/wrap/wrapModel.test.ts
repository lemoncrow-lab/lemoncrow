import { describe, expect, it } from "vitest";
import type { Annotation, ReviewTarget } from "../types";
import {
  githubEventFor,
  readWrapImportance, writeWrapImportance, wrapAnswers, wrapBackendOutcome, wrapBlockers, wrapChoiceLabel, wrapMismatch,
  wrapNotesFor, wrapPartStatus, wrapSummaryLine, type NoteImportance,
} from "./wrapModel";

function target(id: string, category: "decision" | "quick_check" | "accepted_by_rule" = "decision", state: ReviewTarget["state"] = "unreviewed"): ReviewTarget {
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: "src/orders.py", label: id, symbol: id,
    start_line: 1, end_line: 2, hunk_ordinals: [0], spans: [], state, changed_since_mark: false,
    reviewed_revision_id: "rev1", attention_rank: 1, attention_level: "normal", reasons: [],
    additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 0 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: category === "accepted_by_rule" ? "generated_files" : "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: 1, reasons: [] },
  };
}

function note(id: string, rest: Partial<Annotation> = {}): Annotation {
  return {
    id, review_id: "review", revision_id: "rev1", parent_id: "", kind: "request_change", state: "open",
    body: "Please check this", source: "human", created_by_actor: "human", path: "src/orders.py",
    anchored: true, anchor_exact: true, file_level: false, start_line: 2, end_line: 2, symbol: "total", side: "new",
    ...rest,
  } as Annotation;
}

describe("P2-4 Wrap model", () => {
  it("WRAP-4: no verdict suggested until every decision has a human judgment", () => {
    const answers = wrapAnswers([target("auth"), target("checkout", "decision", "later"), target("trivia", "quick_check", "reviewed")]);
    expect(answers.suggested).toBe(null);
    expect(answers.unanswered.map((part) => part.target_id)).toEqual(["auth", "checkout"]);
    expect(answers.reason).toContain("Answer every decision");
    expect(wrapBlockers({ fullTargets: true, readOnly: false, answers, notes: [], importance: {}, choice: null, reachable: true, destinationKind: "export", previewCurrent: true })).toEqual([
      "2 decisions still need your answer", "Choose a verdict",
    ]);
  });

  it("WRAP-2/3: approval is derived from completed human decisions only, never from an AI finding", () => {
    const answers = wrapAnswers([target("auth", "decision", "reviewed"), target("checkout", "decision", "reviewed")]);
    expect(answers.suggested).toBe("lgtm");
    expect(answers.reason).toBe("Based on your answers · every decision looks good");
    expect(wrapBlockers({ fullTargets: true, readOnly: false, answers, notes: [], importance: {}, choice: "lgtm", reachable: true, destinationKind: "export", previewCurrent: true })).toEqual([]);
  });

  it("WRAP-3: any needs-change decision suggests Request changes but does not force it", () => {
    const answers = wrapAnswers([target("auth", "decision", "reviewed"), target("checkout", "decision", "needs_changes")]);
    expect(answers.suggested).toBe("changes_requested");
    expect(answers.reason).toContain("1 part needs a change");
    expect(wrapChoiceLabel("lgtm_with_suggestions")).toBe("Approve with suggestions");
    expect(wrapBackendOutcome("lgtm_with_suggestions")).toBe("lgtm");
  });

  it("WRAP-6: quick checks left unconfirmed remain visible, without blocking a fully answered decision", () => {
    const rows = [target("auth", "decision", "reviewed"), target("trivia", "quick_check", "later"), target("generated", "accepted_by_rule", "unreviewed")];
    const answers = wrapAnswers(rows);
    expect(answers.suggested).toBe("lgtm");
    expect(answers.unconfirmedQuick).toHaveLength(1);
    expect(wrapPartStatus(rows[1])).toBe("Not checked yet");
    expect(wrapSummaryLine(answers)).toContain("accepted by rule, not by a person");
    expect(wrapBlockers({ fullTargets: true, readOnly: false, answers, notes: [], importance: {}, choice: "lgtm", reachable: true, destinationKind: "agent", previewCurrent: true })).toEqual([]);
  });

  it("never uses stale judgments and doesn't imply an approval for zero answered decisions", () => {
    const stale = { ...target("auth", "decision", "reviewed"), changed_since_mark: true };
    const answers = wrapAnswers([stale]);
    expect(answers.unanswered).toHaveLength(1);
    expect(answers.suggested).toBeNull();
    expect(wrapPartStatus(stale)).toBe("Not answered");
    expect(wrapAnswers([]).suggested).toBeNull();
  });

  it("takes the note set from the server's annotation ids, whatever their kind", () => {
    const rows = [note("looks", { kind: "looks_good" as Annotation["kind"] }), note("b"), note("sent"), note("a", { path: "a.py" })];
    expect(wrapNotesFor(rows, ["looks", "b", "a"]).map((row) => row.id)).toEqual(["a", "b", "looks"]);
    expect(wrapNotesFor(rows, [])).toEqual([]);
    expect(wrapNotesFor(rows, ["missing"])).toEqual([]);
  });

  it("a Needs a change on a quick check alone suggests Request changes and the summary stays true", () => {
    const answers = wrapAnswers([target("auth", "decision", "reviewed"), target("trivia", "quick_check", "needs_changes")]);
    expect(answers.suggested).toBe("changes_requested");
    expect(answers.needsChange).toBe(1);
    expect(answers.reason).toBe("Based on your answers · 1 part needs a change");
    expect(wrapSummaryLine(answers)).toBe("1 decision: 1 looks good · 1 quick check: 1 needs a change");
  });

  it("zero decisions: confirmed quick checks suggest approval, a quick-check change suggests Request changes", () => {
    const good = wrapAnswers([target("a", "quick_check", "reviewed"), target("b", "quick_check", "reviewed")]);
    expect(good.suggested).toBe("lgtm");
    expect(wrapSummaryLine(good)).toBe("2 quick checks: 2 confirmed");
    const bad = wrapAnswers([target("a", "quick_check", "reviewed"), target("b", "quick_check", "needs_changes")]);
    expect(bad.suggested).toBe("changes_requested");
    expect(bad.reason).toContain("1 part needs a change");
  });

  it("the summary line never shows a zero count, and names what is still open", () => {
    const open = wrapAnswers([target("a", "decision", "unreviewed"), target("b", "decision", "unreviewed"), target("c", "quick_check", "unreviewed")]);
    expect(wrapSummaryLine(open)).toBe("2 decisions: 2 not answered · 1 quick check: 1 not checked yet");
    const mixed = wrapAnswers([target("a", "decision", "reviewed"), target("b", "decision", "reviewed"), target("c", "decision", "needs_changes"), target("d", "decision", "unreviewed")]);
    expect(wrapSummaryLine(mixed)).toBe("4 decisions: 2 look good, 1 needs a change, 1 not answered");
    expect(wrapSummaryLine(wrapAnswers([]))).toBe("");
  });

  it("WRAP-1: importance starts unset; all notes need a human pick, with mismatch warnings", () => {
    const a = note("a");
    const b = note("b");
    const answers = wrapAnswers([target("auth", "decision", "needs_changes")]);
    const importance: Record<string, NoteImportance> = { a: "must_fix" };
    expect(wrapBlockers({ fullTargets: true, readOnly: false, answers, notes: [a, b], importance, choice: "changes_requested", reachable: true, destinationKind: "agent", previewCurrent: false })).toEqual([
      "Set the importance of 1 note", "Check the exact feedback preview before sending",
    ]);
    expect(wrapMismatch("lgtm", importance, [a, b])).toContain("Approve anyway?");
    expect(wrapMismatch("changes_requested", { a: "question", b: "suggestion" }, [a, b])).toBe("You have no must-fix notes. Request changes anyway?");
    expect(wrapMismatch("changes_requested", { a: "must_fix", b: "suggestion" }, [a, b])).toBe("");
  });

  it("WRAP-4: unavailable destination, partial target list and read-only mode block sending", () => {
    const answers = wrapAnswers([target("auth", "decision", "reviewed")]);
    expect(wrapBlockers({ fullTargets: false, readOnly: true, answers, notes: [note("a")], importance: { a: "must_fix" }, choice: "lgtm", reachable: false, destinationKind: "agent", previewCurrent: false })).toEqual([
      "Still loading all parts", "This revision is read-only", "The feedback destination cannot receive these notes", "Check the exact feedback preview before sending",
    ]);
  });

  it("importance survives a revisit only within its review/revision; corrupt browser values are discarded", () => {
    const first = { n: "suggestion" as NoteImportance };
    writeWrapImportance("shop", "rev1", first);
    expect(readWrapImportance("shop", "rev1")).toEqual(first);
    expect(readWrapImportance("shop", "rev2")).toEqual({});
    window.localStorage.setItem("lc.review.wrap-importance.shop.rev2", JSON.stringify({ n: "evil", okay: "must_fix" }));
    expect(readWrapImportance("shop", "rev2")).toEqual({ okay: "must_fix" });
  });
});

describe("githubEventFor", () => {
  it("is the GitHub event a verdict asks for; approval with suggestions is an approval", () => {
    expect(githubEventFor("lgtm")).toBe("APPROVE");
    expect(githubEventFor("lgtm_with_suggestions")).toBe("APPROVE");
    expect(githubEventFor("changes_requested")).toBe("REQUEST_CHANGES");
    expect(githubEventFor("comment")).toBe("COMMENT");
  });
});
