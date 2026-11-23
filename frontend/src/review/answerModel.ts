/**
 * What the Reader may assume about a mark state and a part's answer category.
 *
 * Pure and exact: the server owns the classifier (`answer_category.py`); this module only reads what it
 * sent, and when it cannot read it, it fails towards asking a person.
 */

import type {
  AnswerCategory,
  AnswerCounts,
  MarkState,
  ReviewOutlineItem,
  ReviewProgress,
  ReviewTarget,
  TargetAnswer,
} from "./types";

/** Every mark state, in the server's order. `answerModel.test.ts` pins this against `types.ts` and the server. */
export const MARK_STATES = [
  "unreviewed",
  "reviewed",
  "needs_changes",
  "changed_since_review",
  "unknown",
  "later",
] as const satisfies readonly MarkState[];

/** The states that record a person's decision about the part's content. `later` is not one of them. */
export const JUDGED_MARK_STATES: readonly MarkState[] = ["reviewed", "needs_changes"];

export function isJudgedState(state: MarkState): boolean {
  return JUDGED_MARK_STATES.includes(state);
}

/** The server's `later` count; 0 from a server that predates the state. */
export function laterCount(counts: { later?: number }): number {
  return counts.later ?? 0;
}

/**
 * Parts that still need a person's answer: not judged, or judged on content that has since moved.
 * `needs_changes` is excluded because it waits on the author, not on the reviewer.
 */
export function openPartCount(progress: Pick<ReviewProgress, "unreviewed" | "unknown" | "changed_since_review" | "later">): number {
  return progress.unreviewed + progress.unknown + progress.changed_since_review + laterCount(progress);
}

/** A file row's count for one state; 0 when the server sent none. */
export function outlineCount(row: ReviewOutlineItem, state: MarkState): number {
  return row[state] ?? 0;
}

const CATEGORIES: readonly AnswerCategory[] = ["decision", "quick_check", "accepted_by_rule"];
const RULES = ["generated_files", "lockfiles"] as const;

/**
 * Read a server answer. Anything unrecognised is a decision with no rule: a client that cannot tell how a
 * part is asked must ask a person, and must never show a part as accepted by a rule it does not know.
 */
export function readAnswer(raw: unknown): TargetAnswer {
  const row = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
  const category = CATEGORIES.find((name) => name === row.category);
  const rule = typeof row.rule === "string" ? row.rule : "";
  if (!category) return { category: "decision", rule: "", rule_label: "", reason: "unreadable_answer" };
  if (category === "accepted_by_rule") {
    if (!(RULES as readonly string[]).includes(rule)) {
      return { category: "decision", rule: "", rule_label: "", reason: "unknown_rule" };
    }
    return {
      category,
      rule,
      rule_label: typeof row.rule_label === "string" ? row.rule_label : "",
      reason: typeof row.reason === "string" ? row.reason : "",
    };
  }
  return { category, rule: "", rule_label: "", reason: typeof row.reason === "string" ? row.reason : "" };
}

/** A part's answer; a target from a server that predates answers is a decision. */
export function targetAnswer(target: Pick<ReviewTarget, "answer">): TargetAnswer {
  return readAnswer(target.answer);
}

/** Counts per category, with each accepted-by-rule rule listed by name. Marks never change them. */
export function answerCounts(targets: readonly Pick<ReviewTarget, "answer">[]): AnswerCounts {
  const counts: AnswerCounts = { decision: 0, quick_check: 0, accepted_by_rule: 0, rules: {} };
  for (const target of targets) {
    const answer = targetAnswer(target);
    counts[answer.category] += 1;
    if (answer.category === "accepted_by_rule") counts.rules[answer.rule] = (counts.rules[answer.rule] ?? 0) + 1;
  }
  return counts;
}
