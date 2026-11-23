import { counterFor, type Counter } from "../focus/focusModel";
import type { Annotation, ReviewRevisionComparison, ReviewTarget, ReviewVerificationProjection } from "../types";

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
