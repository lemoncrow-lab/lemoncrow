/** The one user-facing noun for a reviewable piece of a change is "part"; this is the one place that counts it. */
export function partCount(count: number): string {
  return `${count} ${count === 1 ? "part" : "parts"}`;
}

/**
 * Parts that count as reviewed: marked Reviewed or Needs changes on the current revision (you looked and
 * decided). Needs changes keeps its own counter and still blocks readiness and Finish.
 */
export function judgedParts(progress: {
  reviewed: number;
  needs_changes: number;
}): number {
  return progress.reviewed + progress.needs_changes;
}

/** Parts left to review: no judgment on the current revision, changed-since-review parts included. */
export function partsLeft(progress: {
  target_count: number;
  reviewed: number;
  needs_changes: number;
}): number {
  return Math.max(0, progress.target_count - judgedParts(progress));
}
