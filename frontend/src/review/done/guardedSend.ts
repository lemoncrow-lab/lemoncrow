/**
 * The guarded, undoable send used by Wrap up and Welcome back: re-check the review is still open, re-export the exact
 * feedback and require the same digest, save the intent for recovery, then hand it to the server-held queue.
 * Nothing leaves the browser except through `onQueue`. A thrown error means nothing was sent.
 */
import { exportFeedback, fetchFinishReview } from "../reviewApi";
import type { FeedbackExport, FeedbackPresentation, ReviewFlowSend, ReviewOutcomeKind } from "../types";
import { clearOfflineSend, createOfflineSend, saveOfflineSend } from "./offlineReviewSend";
import { savePendingSendToken } from "./pendingSendToken";

export interface GuardedSendArgs {
  reviewRef: string;
  apiReviewId: string;
  revisionId: string;
  outcome: ReviewOutcomeKind;
  summary: string;
  presentation: FeedbackPresentation;
  /** The exact preview the person saw. It is what gets sent; the server refuses it if its hash no longer matches. */
  shown: FeedbackExport;
  destinationLabel: string;
  onQueue(outcome: ReviewOutcomeKind, summary: string, feedback: FeedbackExport | null, operationId: string): Promise<ReviewFlowSend>;
}

function sameIds(left: Record<string, number>, right: Record<string, number>): boolean {
  const a = Object.keys(left).sort();
  const b = Object.keys(right).sort();
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

export async function guardedQueueSend(args: GuardedSendArgs): Promise<ReviewFlowSend | null> {
  const fresh = await fetchFinishReview(args.apiReviewId);
  if (fresh.status !== "open") throw new Error("The review changed. Reload and check the answers before sending.");
  // Re-render now; anything other than exactly what was shown is refused before it reaches the server.
  const current = await exportFeedback(args.apiReviewId, { expectedRevisionId: args.revisionId, presentation: args.presentation });
  if (current.revision_id !== args.revisionId || current.feedback_hash !== args.shown.feedback_hash
    || current.operation_id !== args.shown.operation_id || !sameIds(current.annotation_versions, args.shown.annotation_versions)) {
    throw new Error("The notes changed after the preview. Check the updated text before sending.");
  }
  const noteCount = Object.keys(args.shown.annotation_versions).length;
  const intent = createOfflineSend(args.reviewRef, args.apiReviewId, args.revisionId, args.outcome, args.summary, args.shown);
  if (!saveOfflineSend({ ...intent, attempted: true })) throw new Error("This send could not be saved for recovery. Nothing was sent.");
  try {
    const queued = await args.onQueue(args.outcome, args.summary, args.shown, intent.operationId);
    clearOfflineSend(intent.operationId);
    savePendingSendToken(args.reviewRef, args.apiReviewId, queued, noteCount > 0 ? args.destinationLabel : "");
    return queued;
  } catch (reason) {
    const status = (reason as { status?: number })?.status;
    if (typeof status === "number" && status !== 502 && status !== 503 && status !== 504) {
      clearOfflineSend(intent.operationId);
      throw reason;
    }
    // No response: acceptance is not asserted. Done reconciles this same operation ID.
    return null;
  }
}
