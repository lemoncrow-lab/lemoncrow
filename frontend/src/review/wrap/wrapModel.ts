/** P2-4 Wrap up: derive only from persisted human answers and current-revision annotations. */
import { isJudgedState, targetAnswer } from "../answerModel";
import { partName, partsOf, plural } from "../overview/overviewModel";
import type { Annotation, ReviewOutcomeKind, ReviewTarget } from "../types";

export type NoteImportance = "must_fix" | "suggestion" | "question";
export type WrapChoice = ReviewOutcomeKind | "lgtm_with_suggestions";

export interface WrapAnswers {
  decisions: ReviewTarget[];
  quick: ReviewTarget[];
  accepted: ReviewTarget[];
  unanswered: ReviewTarget[];
  unconfirmedQuick: ReviewTarget[];
  /** Every human "Needs a change", on a decision or a quick check. */
  needsChange: number;
  /** Decisions only. */
  decisionNeeds: number;
  good: number;
  quickGood: number;
  quickNeeds: number;
  /** Absent if a decision is open: a reviewer cannot inherit a verdict from AI or an unfinished part. */
  suggested: "lgtm" | "changes_requested" | null;
  reason: string;
}

export function wrapAnswers(targets: readonly ReviewTarget[]): WrapAnswers {
  const decisions = partsOf(targets, "decision");
  const quick = partsOf(targets, "quick_check");
  const accepted = partsOf(targets, "accepted_by_rule");
  const unanswered = decisions.filter((part) => !isJudgedState(part.state) || part.changed_since_mark);
  const unconfirmedQuick = quick.filter((part) => !isJudgedState(part.state) || part.changed_since_mark);
  const answeredAs = (parts: readonly ReviewTarget[], state: ReviewTarget["state"]) =>
    parts.filter((part) => part.state === state && !part.changed_since_mark).length;
  const decisionNeeds = answeredAs(decisions, "needs_changes");
  const quickNeeds = answeredAs(quick, "needs_changes");
  const quickGood = answeredAs(quick, "reviewed");
  const needsChange = decisionNeeds + quickNeeds;
  const good = answeredAs(decisions, "reviewed");
  const suggested = unanswered.length
    ? null : needsChange ? "changes_requested" : decisions.length || (quick.length > 0 && unconfirmedQuick.length === 0) ? "lgtm" : null;
  const reason = unanswered.length
    ? "Answer every decision before a verdict can be suggested from your answers"
    : needsChange ? `Based on your answers · ${plural(needsChange, "part")} ${needsChange === 1 ? "needs" : "need"} a change`
      : decisions.length ? "Based on your answers · every decision looks good"
        : suggested ? "Based on your answers · every quick check is confirmed"
          : "No decision has been answered. Choose a verdict yourself.";
  return { decisions, quick, accepted, unanswered, unconfirmedQuick, needsChange, decisionNeeds, good, quickGood, quickNeeds, suggested, reason };
}

/** The notes the server would send now, by the annotation IDs of its own preview; the client never builds this set itself. */
export function wrapNotesFor(annotations: readonly Annotation[], serverIds: readonly string[]): Annotation[] {
  const wanted = new Set(serverIds);
  return annotations.filter((note) => wanted.has(note.id))
    .sort((left, right) => left.path.localeCompare(right.path) || left.start_line - right.start_line || left.id.localeCompare(right.id));
}

export function wrapPartStatus(target: ReviewTarget): string {
  if (target.changed_since_mark) return "Not answered";
  if (target.state === "reviewed") return "Looks good";
  if (target.state === "needs_changes") return "Needs a change";
  return targetAnswer(target).category === "quick_check" ? "Not checked yet" : "Not answered";
}

export function wrapChoiceLabel(choice: WrapChoice): string {
  return { lgtm: "Approve", changes_requested: "Request changes", comment: "Comment only", lgtm_with_suggestions: "Approve with suggestions" }[choice];
}

export function wrapBackendOutcome(choice: WrapChoice): ReviewOutcomeKind {
  // The current record API has only LGTM, Request changes and Comment.
  // Approval with suggestions is LGTM accompanied by selected notes, not a fabricated fourth server verdict.
  return choice === "lgtm_with_suggestions" ? "lgtm" : choice;
}

export interface WrapReadyInput {
  fullTargets: boolean;
  readOnly: boolean;
  answers: WrapAnswers;
  notes: readonly Annotation[];
  importance: Readonly<Record<string, NoteImportance>>;
  choice: WrapChoice | null;
  reachable: boolean;
  destinationKind: "agent" | "new_session" | "export" | "github";
  previewCurrent: boolean;
}

/** Human-safe blockers. Never silently finish risky, unjudged parts. */
export function wrapBlockers(input: WrapReadyInput): string[] {
  const issues: string[] = [];
  if (!input.fullTargets) issues.push("Still loading all parts");
  if (input.readOnly) issues.push("This revision is read-only");
  if (input.answers.unanswered.length) issues.push(`${plural(input.answers.unanswered.length, "decision")} still need your answer`);
  const missing = input.notes.filter((note) => !input.importance[note.id]).length;
  if (missing) issues.push(`Set the importance of ${plural(missing, "note")}`);
  if (!input.choice) issues.push("Choose a verdict");
  if (input.choice === "changes_requested" && !input.notes.length) {
    // The underlying Finish API also permits an explicit summary; the Wrap screen supplies an input for it separately.
    // That validation is performed by the screen, which has the actual summary text.
  }
  if (input.notes.length && !input.reachable) issues.push("The feedback destination cannot receive these notes");
  if (input.notes.length && !input.previewCurrent) issues.push("Check the exact feedback preview before sending");
  return issues;
}

export function wrapMismatch(choice: WrapChoice | null, importance: Readonly<Record<string, NoteImportance>>, notes: readonly Annotation[]): string {
  if (!choice) return "";
  const must = notes.filter((note) => importance[note.id] === "must_fix").length;
  if (must && (choice === "lgtm" || choice === "lgtm_with_suggestions"))
    return `You have ${plural(must, "must-fix note")}. ${wrapChoiceLabel(choice)} anyway?`;
  if (!must && choice === "changes_requested") return "You have no must-fix notes. Request changes anyway?";
  return "";
}

export function wrapNoteAnchor(note: Annotation): string {
  if (!note.anchored || note.state === "orphaned" || note.file_level || !note.anchor_exact)
    return `${note.path} · anchor not verified`;
  return `${note.path}:${note.start_line}${note.end_line > note.start_line ? `–${note.end_line}` : ""}${note.symbol ? ` · ${note.symbol}` : ""}`;
}

/**
 * "3 decisions: 2 look good, 1 not answered · 1 quick check: 1 confirmed": each kind of part the review has, and
 * only the counts that are not zero. "" for a review with no parts.
 */
export function wrapSummaryLine(answers: WrapAnswers): string {
  const needs = (count: number) => (count === 1 ? "needs a change" : "need a change");
  const group = (total: number, noun: string, counts: ReadonlyArray<readonly [number, string]>) => {
    const shown = counts.filter(([count]) => count > 0).map(([count, words]) => `${count} ${words}`).join(", ");
    return shown ? `${plural(total, noun)}: ${shown}` : plural(total, noun);
  };
  const parts: string[] = [];
  if (answers.decisions.length) parts.push(group(answers.decisions.length, "decision", [
    [answers.good, answers.good === 1 ? "looks good" : "look good"], [answers.decisionNeeds, needs(answers.decisionNeeds)], [answers.unanswered.length, "not answered"],
  ]));
  if (answers.quick.length) parts.push(group(answers.quick.length, "quick check", [
    [answers.quickGood, "confirmed"], [answers.quickNeeds, needs(answers.quickNeeds)], [answers.unconfirmedQuick.length, "not checked yet"],
  ]));
  if (answers.accepted.length) parts.push(`${answers.accepted.length} accepted by rule, not by a person`);
  return parts.join(" · ");
}

export function wrapNoteLabel(note: Annotation): string {
  return `${partName({ path: note.path, symbol: note.symbol, label: "" } as ReviewTarget)} · ${wrapNoteAnchor(note)}`;
}

/** Browser-local preference, scoped to one review, frozen revision and note identity. */
export function wrapImportanceKey(reviewId: string, revisionId: string): string {
  return `lc.review.wrap-importance.${encodeURIComponent(reviewId)}.${encodeURIComponent(revisionId)}`;
}
export function readWrapImportance(reviewId: string, revisionId: string): Record<string, NoteImportance> {
  try {
    const raw = window.localStorage.getItem(wrapImportanceKey(reviewId, revisionId));
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter(([id, kind]) =>
      id.length > 0 && (kind === "must_fix" || kind === "suggestion" || kind === "question"),
    ));
  } catch { return {}; }
}
export function writeWrapImportance(reviewId: string, revisionId: string, importance: Readonly<Record<string, NoteImportance>>): void {
  try { window.localStorage.setItem(wrapImportanceKey(reviewId, revisionId), JSON.stringify(importance)); } catch { /* Ephemeral choice remains on screen. */ }
}

/** The GitHub review event a verdict asks for; the server may post something else (see the plan's notices). */
export function githubEventFor(choice: WrapChoice): "APPROVE" | "REQUEST_CHANGES" | "COMMENT" {
  const outcome = wrapBackendOutcome(choice);
  return outcome === "lgtm" ? "APPROVE" : outcome === "changes_requested" ? "REQUEST_CHANGES" : "COMMENT";
}
