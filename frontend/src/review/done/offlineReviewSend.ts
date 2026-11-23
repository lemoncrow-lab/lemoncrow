/** Ephemeral, same-tab recovery for a Review send attempted without connectivity.
 *
 * This only holds the user's intent and the server-issued preview digest,
 * never an invented server receipt, delivery success or Undo deadline. The
 * server rechecks every revision, note and mark before accepting the send.
 * sessionStorage survives refresh but not a closed browser/tab.
 */
import type { FeedbackExport, FeedbackPresentation, ReviewFlowSend, ReviewOutcomeKind } from "../types";

const KEY = "lc.review.offline-send.v1";
export const OFFLINE_SEND_EVENT = "lc-review-offline-send";
export type FlowFeedbackProof = Pick<FeedbackExport, "revision_id" | "feedback_hash" | "operation_id"> &
  Partial<Pick<FeedbackExport, "presentation">>;
export interface OfflineReviewSend {
  reviewRef: string;
  reviewId: string;
  revisionId: string;
  operationId: string;
  outcome: ReviewOutcomeKind;
  summary: string;
  feedback: FlowFeedbackProof | null;
  createdAt: string;
  /** Notes in the saved preview, for the "will be sent" line. */
  noteCount?: number;
  state: "waiting" | "needs_review";
  /** False only before the first network attempt; safe to cancel locally. */
  attempted: boolean;
  error: string;
}
export function createOfflineSend(
  reviewRef: string, reviewId: string, revisionId: string, outcome: ReviewOutcomeKind,
  summary: string, preview: FeedbackExport | null,
): OfflineReviewSend {
  return {
    reviewRef, reviewId, revisionId, outcome, summary,
    feedback: preview ? {
      revision_id: preview.revision_id, operation_id: preview.operation_id,
      feedback_hash: preview.feedback_hash,
      ...(preview.presentation ? { presentation: preview.presentation } : {}),
    } : null,
    operationId: `fs-${crypto.randomUUID().replace(/-/g, "")}`,
    createdAt: new Date().toISOString(), state: "waiting", attempted: false, error: "",
    ...(preview ? { noteCount: preview.open + preview.orphaned } : {}),
  };
}
function validPresentation(p: unknown): p is FeedbackPresentation {
  if (!p || typeof p !== "object" || Array.isArray(p)) return false;
  const v = p as Record<string, unknown>;
  const imp = v.importance;
  return ["lgtm", "changes_requested", "comment", "lgtm_with_suggestions"].includes(v.verdict as string)
    && typeof v.test_with_fix === "boolean" && typeof v.summary === "string"
    && !!imp && typeof imp === "object" && !Array.isArray(imp)
    && Object.values(imp).every((x) => x === "must_fix" || x === "suggestion" || x === "question");
}
export function readOfflineSend(): OfflineReviewSend | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const data = JSON.parse(raw) as Partial<OfflineReviewSend>;
    if (!data || !["waiting", "needs_review"].includes(data.state ?? "")
      || typeof data.reviewRef !== "string" || typeof data.reviewId !== "string"
      || typeof data.revisionId !== "string" || typeof data.summary !== "string"
      || !/^fs-[0-9a-f]{32}$/.test(data.operationId ?? "")
      || typeof data.attempted !== "boolean"
      || !["comment", "changes_requested", "lgtm"].includes(data.outcome ?? "")) return null;
    // Field by field (ST-4): a field of the wrong type falls back to its empty value instead of reaching the page.
    const fb = data.feedback as Partial<FlowFeedbackProof> | null | undefined;
    const proof = fb && typeof fb === "object" && typeof fb.revision_id === "string" && typeof fb.feedback_hash === "string" && typeof fb.operation_id === "string"
      ? { revision_id: fb.revision_id, feedback_hash: fb.feedback_hash, operation_id: fb.operation_id, ...(validPresentation(fb.presentation) ? { presentation: fb.presentation } : {}) }
      : null;
    return {
      reviewRef: data.reviewRef, reviewId: data.reviewId, revisionId: data.revisionId, operationId: data.operationId as string,
      outcome: data.outcome as ReviewOutcomeKind, summary: data.summary, feedback: proof,
      createdAt: typeof data.createdAt === "string" ? data.createdAt : "",
      ...(typeof data.noteCount === "number" && Number.isFinite(data.noteCount) && data.noteCount >= 0 ? { noteCount: data.noteCount } : {}),
      state: data.state as "waiting" | "needs_review", attempted: data.attempted,
      error: typeof data.error === "string" ? data.error : "",
    };
  } catch { return null; }
}
export function saveOfflineSend(draft: OfflineReviewSend): boolean {
  try {
    const content = JSON.stringify(draft);
    if (content.length > 32_768) return false;
    window.sessionStorage.setItem(KEY, content);
    window.dispatchEvent(new Event(OFFLINE_SEND_EVENT));
    return true;
  } catch { return false; }
}
export function clearOfflineSend(operationId: string): void {
  try {
    if (readOfflineSend()?.operationId === operationId) window.sessionStorage.removeItem(KEY);
    window.dispatchEvent(new Event(OFFLINE_SEND_EVENT));
  } catch { /* No persistent storage. */ }
}
export function serverAcceptedSend(send: ReviewFlowSend, draft: OfflineReviewSend): boolean {
  return send.operation_id === draft.operationId
    && ["pending", "dispatching", "sent", "failed", "undone", "uncertain"].includes(send.state);
}
