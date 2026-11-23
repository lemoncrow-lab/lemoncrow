import type { ReviewFlowSend } from "../types";

const KEY = "lc.review.pending-send.v1";
export const PENDING_SEND_EVENT = "lc-review-pending-send";
export interface PendingSendToken {
  reviewRef: string;
  reviewId: string;
  operationId: string;
  sendAfter: string;
  /** Who the notes go to ("Claude Code"); the toast names it. Empty when the send has no notes. */
  destinationLabel?: string;
}

export function readPendingSendToken(): PendingSendToken | null {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(KEY) || "null");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const value = parsed as Record<string, unknown>;
    if (["reviewRef", "reviewId", "operationId", "sendAfter"].some((name) =>
      typeof value[name] !== "string" || !value[name] || (value[name] as string).length > 255)) return null;
    if (!/^fs-[0-9a-f]{32}$/.test(value.operationId as string) || !Number.isFinite(Date.parse(value.sendAfter as string))) return null;
    const label = value.destinationLabel;
    return {
      reviewRef: value.reviewRef as string, reviewId: value.reviewId as string,
      operationId: value.operationId as string, sendAfter: value.sendAfter as string,
      ...(typeof label === "string" && label ? { destinationLabel: label.slice(0, 80) } : {}),
    };
  } catch { return null; }
}
export function savePendingSendToken(reviewRef: string, reviewId: string, operation: ReviewFlowSend, destinationLabel = ""): void {
  if (operation.state !== "pending" || !operation.operation_id || !operation.send_after) return;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({
      reviewRef, reviewId, operationId: operation.operation_id, sendAfter: operation.send_after,
      ...(destinationLabel ? { destinationLabel: destinationLabel.slice(0, 80) } : {}),
    } satisfies PendingSendToken));
    window.dispatchEvent(new Event(PENDING_SEND_EVENT));
  } catch { /* The Done page still reads the server-held deadline. */ }
}
export function clearPendingSendToken(operationId?: string): void {
  try {
    if (!operationId || readPendingSendToken()?.operationId === operationId)
      window.sessionStorage.removeItem(KEY);
    window.dispatchEvent(new Event(PENDING_SEND_EVENT));
  } catch { /* Storage disabled. */ }
}
