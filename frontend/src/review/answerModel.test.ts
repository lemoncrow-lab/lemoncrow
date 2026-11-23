import { describe, expect, it } from "vitest";

import {
  JUDGED_MARK_STATES,
  MARK_STATES,
  answerCounts,
  isJudgedState,
  laterCount,
  openPartCount,
  outlineCount,
  readAnswer,
  targetAnswer,
} from "./answerModel";
import { progressFromTargets } from "./readerModel";
import { shiftOutlineState } from "./reviewStore";
import type { MarkState, ReviewOutlineItem, ReviewProgress, ReviewTarget, TargetAnswer } from "./types";

const progress = (counts: Partial<ReviewProgress> = {}): ReviewProgress => ({
  target_count: 4,
  reviewed: 0,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
  ...counts,
});

const target = (state: MarkState, answer?: TargetAnswer): ReviewTarget =>
  ({ target_id: `t-${state}`, path: "a.py", state, attention_level: "normal", answer }) as ReviewTarget;

const outlineRow = (counts: Partial<ReviewOutlineItem> = {}): ReviewOutlineItem => ({
  path: "a.py",
  target_count: 2,
  reviewed: 0,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 2,
  unknown: 0,
  mechanical: 0,
  attention_rank: 0,
  reasons: [],
  ...counts,
});

describe("mark states", () => {
  it("judges only reviewed and needs_changes; later and every other state is not a judgment", () => {
    expect([...JUDGED_MARK_STATES].sort()).toEqual(["needs_changes", "reviewed"]);
    for (const state of MARK_STATES) {
      expect(isJudgedState(state), state).toBe(state === "reviewed" || state === "needs_changes");
    }
    expect(isJudgedState("later")).toBe(false);
  });

  it("counts later as a part that still needs an answer, and reads an absent count as 0", () => {
    expect(openPartCount(progress({ unreviewed: 1, unknown: 1, changed_since_review: 1, later: 2 }))).toBe(5);
    expect(openPartCount(progress({ later: 1 }))).toBe(1);
    // needs_changes waits on the author, not the reviewer.
    expect(openPartCount(progress({ needs_changes: 3 }))).toBe(0);
    expect(laterCount({})).toBe(0);
    expect(laterCount({ later: 2 })).toBe(2);
  });

  it("moves a part into and out of later in a file row without producing NaN", () => {
    const outline = [outlineRow({ unreviewed: 2 })];
    const aside = shiftOutlineState(outline, "a.py", "unreviewed", "later");
    expect(aside[0]).toMatchObject({ unreviewed: 1, later: 1 });
    const judged = shiftOutlineState(aside, "a.py", "later", "reviewed");
    expect(judged[0]).toMatchObject({ unreviewed: 1, later: 0, reviewed: 1 });
    // A server that predates `later` sent no field; leaving it never goes below zero.
    const old = shiftOutlineState(outline, "a.py", "later", "reviewed");
    expect(old[0]).toMatchObject({ later: 0, reviewed: 1 });
    expect(Object.values(old[0] ?? {}).some((value) => Number.isNaN(value))).toBe(false);
    expect(outlineCount(outlineRow(), "later")).toBe(0);
  });

  it("partitions the targets: every state lands in exactly one counter", () => {
    const counters = ["reviewed", "changed_since_review", "needs_changes", "unreviewed", "unknown", "later"] as const;
    for (const state of MARK_STATES) {
      const counted = progressFromTargets([target(state)]);
      expect(counters.filter((name) => counted[name]), state).toEqual([state]);
    }
    const mixed = progressFromTargets([target("reviewed"), target("later"), target("later"), target("unreviewed")]);
    expect(mixed).toMatchObject({ target_count: 4, reviewed: 1, later: 2, unreviewed: 1 });
  });
});

describe("reading the server's answer category", () => {
  const accepted: TargetAnswer = {
    category: "accepted_by_rule",
    rule: "generated_files",
    rule_label: "generated files",
    reason: "at_capture",
  };

  it("keeps a well-formed answer exactly", () => {
    expect(readAnswer(accepted)).toEqual(accepted);
    expect(readAnswer({ category: "decision", rule: "", rule_label: "", reason: "review_first" })).toEqual({
      category: "decision",
      rule: "",
      rule_label: "",
      reason: "review_first",
    });
    expect(readAnswer({ category: "quick_check", rule: "lockfiles" }).rule).toBe("");
  });

  it("fails towards asking a person: anything unreadable is a decision, never an accepted rule", () => {
    for (const bad of [undefined, null, 7, "decision", {}, { category: "skim" }, { category: "accepted_by_rule" }]) {
      const answer = readAnswer(bad);
      expect(answer.category, JSON.stringify(bad)).toBe("decision");
      expect(answer.rule).toBe("");
    }
    expect(readAnswer({ category: "accepted_by_rule", rule: "made_up" }).category).toBe("decision");
    expect(targetAnswer({})).toMatchObject({ category: "decision" });
  });

  it("counts categories and each rule, and a mark never changes them", () => {
    const quick: TargetAnswer = { category: "quick_check", rule: "", rule_label: "", reason: "" };
    const lock: TargetAnswer = { ...accepted, rule: "lockfiles", rule_label: "lockfiles" };
    const targets = [
      target("later", accepted),
      target("reviewed", accepted),
      target("unreviewed", lock),
      target("later", quick),
      target("needs_changes"),
    ];
    expect(answerCounts(targets)).toEqual({
      decision: 1,
      quick_check: 1,
      accepted_by_rule: 3,
      rules: { generated_files: 2, lockfiles: 1 },
    });
    expect(answerCounts(targets)).toEqual(answerCounts(targets.map((item) => ({ ...item, state: "unreviewed" }))));
  });
});
