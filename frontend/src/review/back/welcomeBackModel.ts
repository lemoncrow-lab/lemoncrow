import { counterFor, type Counter } from "../focus/focusModel";
import type { Annotation, MarkState, ReviewRevisionComparison, ReviewSnapshotDelta, ReviewTarget, ReviewVerificationProjection } from "../types";

/** The most rows a Welcome back list names; the rest are counted ("and N more"). */
export const BACK_LIST_LIMIT = 25;

export interface CarriedOver {
  /** Zero-based stored revision the delta runs from. */
  since: number;
  /** Parts answered Looks good on an earlier revision whose answer still stands here. */
  kept: number;
  /** Answers kept because only the base moved under them (the refresh's `rebased`). */
  rebased: number;
  /** Parts that left the change; `removedParts` names at most 25 of them. */
  removed: number;
  removedParts: string[];
}

/**
 * What the new revision did to this reviewer's earlier answers, from the projection's Snapshot delta (the Classic
 * re-review ledger's Kept and Removed rows). Null when there is no delta or nothing was kept or removed.
 */
export function carriedOver(
  delta: ReviewSnapshotDelta | null | undefined,
  targets: readonly ReviewTarget[],
  revisionId: string,
  rebased = 0,
): CarriedOver | null {
  if (!delta) return null;
  const added = new Set(delta.added);
  const kept = targets.filter((target) => target.state === "reviewed" && !added.has(target.target_id)
    && target.reviewed_revision_id !== "" && target.reviewed_revision_id !== revisionId).length;
  const removed = Math.max(0, delta.removed);
  const removedParts = (delta.removed_parts ?? []).slice(0, BACK_LIST_LIMIT);
  if (kept + removed + rebased === 0) return null;
  return { since: delta.since_snapshot, kept, rebased: Math.max(0, rebased), removed, removedParts };
}

/** A discarded answer as the reviewer gave it; "" for a state that is not an answer. */
export function answerWord(state: MarkState): string {
  if (state === "reviewed") return "Looks good";
  if (state === "needs_changes") return "Needs a change";
  return "";
}

/** "and N more" past the list limit, else "". */
export function moreText(total: number, shown: number): string {
  return total > shown ? `and ${total - shown} more` : "";
}

export interface ReturnedPart {
  target: ReviewTarget;
  notes: Annotation[];
  kind: "note" | "extra";
}

/** Match a previous note only to an unambiguous current part. Never guess from a file alone. */
export function noteTarget(note: Annotation, targets: readonly ReviewTarget[]): ReviewTarget | null {
  const exact = targets.filter((target) => target.unit_key === note.unit_key && target.path === note.path);
  if (exact.length === 1) return exact[0];
  const named = targets.filter((target) =>
    target.path === note.path && Boolean(note.symbol) && target.symbol === note.symbol);
  return named.length === 1 ? named[0] : null;
}

/** Only changed current units return. A code file changing does not prove any given note was fixed. */
export function returnedParts(
  targets: readonly ReviewTarget[],
  previous: readonly Annotation[],
  comparison: ReviewRevisionComparison,
): { returned: ReturnedPart[]; untouched: Annotation[]; unmatched: Annotation[] } {
  const notes = previous.filter((annotation) =>
    annotation.kind === "request_change" && (annotation.source === "human" || !annotation.source) && !annotation.parent_id);
  const changed = new Set(comparison.units.filter((unit) =>
    unit.status !== "unchanged" && unit.status !== "base_only" && unit.status !== "removed").map((unit) => unit.unit_key));
  const byId = new Map<string, ReturnedPart>();
  const untouched: Annotation[] = [];
  const unmatched: Annotation[] = [];
  const orderedTargets = [...targets];
  for (const note of notes) {
    const target = noteTarget(note, orderedTargets);
    if (!target) { unmatched.push(note); continue; }
    const changedPart = changed.has(target.unit_key)
      || (target.movement === "authored" || target.movement === "added");
    if (!changedPart) { untouched.push(note); continue; }
    const row = byId.get(target.target_id) ?? { target, notes: [], kind: "note" as const };
    row.notes.push(note);
    byId.set(target.target_id, row);
  }
  for (const target of targets) {
    if (byId.has(target.target_id)) continue;
    if (changed.has(target.unit_key) || target.movement === "authored" || target.movement === "added") {
      byId.set(target.target_id, { target, notes: [], kind: "extra" });
    }
  }
  return { returned: [...byId.values()], untouched, unmatched };
}

/** A previous-revision judgment is not a confirmation on the new revision. */
export function confirmedAnswer(target: ReviewTarget, revisionId: string): "fixed" | "change" | null {
  if (target.reviewed_revision_id !== revisionId || target.changed_since_mark) return null;
  if (target.state === "reviewed") return "fixed";
  if (target.state === "needs_changes") return "change";
  return null;
}

/** Only a recorded, current check on BOTH exact revisions qualifies as a paired observation. */
export function pairedChecks(
  before: ReviewVerificationProjection | null,
  after: ReviewVerificationProjection | null,
): Array<{ name: string; before: string; after: string }> {
  if (!before || !after || before.snapshot_id === after.snapshot_id) return [];
  const old = new Map(before.checks.filter((c) =>
    c.state === "current" && c.snapshot_id === before.snapshot_id && Boolean(c.stable_key)).map((c) => [c.stable_key, c]));
  return after.checks.flatMap((check) => {
    const prior = check.stable_key ? old.get(check.stable_key) : undefined;
    if (!prior || check.state !== "current" || check.snapshot_id !== after.snapshot_id ||
      prior.status !== "FAIL" || check.status !== "PASS") return [];
    return [{ name: check.name, before: prior.status, after: check.status }];
  });
}

/** A check that passed on BOTH exact revisions: it cannot show the change fixed anything (BACK-5). */
export function passOnBothChecks(
  before: ReviewVerificationProjection | null,
  after: ReviewVerificationProjection | null,
): Array<{ name: string }> {
  if (!before || !after || before.snapshot_id === after.snapshot_id) return [];
  const old = new Map(before.checks.filter((c) =>
    c.state === "current" && c.snapshot_id === before.snapshot_id && Boolean(c.stable_key)).map((c) => [c.stable_key, c]));
  return after.checks.flatMap((check) => {
    const prior = check.stable_key ? old.get(check.stable_key) : undefined;
    if (!prior || check.state !== "current" || check.snapshot_id !== after.snapshot_id ||
      prior.status !== "PASS" || check.status !== "PASS") return [];
    return [{ name: check.name }];
  });
}

/** "answered your note" / "answered both notes" / "answered all 3 notes" (SPEC 4.10). */
export function answeredHeadline(agent: string, notes: number): string {
  if (notes <= 0) return `${agent} sent a new revision.`;
  if (notes === 1) return `${agent} answered your note.`;
  return `${agent} answered ${notes === 2 ? "both" : `all ${notes}`} notes.`;
}

/**
 * The Welcome back and Approved bar centre (SPEC 4.10, review-0.3): the same widget as every decision screen, one dot per
 * decision with "n of N decisions" (`counterFor`). A part that came back counts by this screen's answer to it
 * ("Looks fixed" is looks good, "Still needs a change" needs a change) and is open until answered, whatever its earlier
 * mark said. Null while the returned parts are unknown, or while the parts list is a startup slice.
 */
export function backCounter(
  targets: readonly ReviewTarget[],
  returned: readonly ReviewTarget[],
  answers: Readonly<Record<string, "fixed" | "change" | null | undefined>>,
  currentId: string,
  complete: boolean,
): Counter | null {
  const back = new Set(returned.map((target) => target.target_id));
  const overlaid = targets.map((target) => {
    if (!back.has(target.target_id)) return target;
    const answer = answers[target.target_id];
    return { ...target, state: answer === "fixed" ? "reviewed" : answer === "change" ? "needs_changes" : "unreviewed" } as ReviewTarget;
  });
  return counterFor(overlaid, currentId, complete);
}
