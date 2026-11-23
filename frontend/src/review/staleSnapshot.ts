/**
 * Shared classification of "the displayed Snapshot is stale" failures for human
 * writes. The server answers 409 with a "the Review advanced ..." detail when
 * `expected_revision_id` no longer matches the current revision. Every human
 * mutation (mark, bulk mark, comment, Finish) routes its failure through here so
 * a stale Snapshot is never buried in a generic error string.
 */

export const STALE_SNAPSHOT_NOTICE =
  "The Review advanced to a newer revision, so your change was not saved. Reload the review, then redo it on the current revision.";

export function isStaleSnapshotError(reason: unknown): boolean {
  const status = (reason as { status?: unknown } | null)?.status;
  if (status !== 409) return false;
  const message = reason instanceof Error ? reason.message : "";
  // Only the expected-revision conflicts; other 409s (Ask context, Map
  // selection, proposal preview) are not a stale displayed Snapshot.
  return /\breview advanced\b|expected_revision_id does not match/i.test(message);
}

export interface WriteFailure {
  stale: boolean;
  message: string;
}

/** Classify a failed human write into the message to show and whether the Snapshot is stale. */
export function classifyWriteFailure(reason: unknown): WriteFailure {
  if (isStaleSnapshotError(reason)) return { stale: true, message: STALE_SNAPSHOT_NOTICE };
  return { stale: false, message: String(reason instanceof Error ? reason.message : reason) };
}
