import { useEffect, useRef } from "react";

import { putReviewQueueState } from "../reviewApi";
import type { ReviewResume } from "../types";

const SAVE_AFTER_MS = 400;

/**
 * Keeps the viewer's place in this review on the server so Home can offer "Continue review" (SPEC 4.1, HOME-4).
 * `resume` null forgets the place (the review was sent). A failed write is dropped: the place is a convenience, and
 * the next move writes it again. `enabled` is false for a read-only view (an older Snapshot).
 */
export function useSaveResume(reviewId: string, resume: ReviewResume | null, enabled = true): void {
  const last = useRef("");
  const key = resume ? `${resume.screen}|${resume.part}|${resume.position}|${resume.total}` : "none";
  useEffect(() => {
    if (!enabled || !reviewId) return;
    const marker = `${reviewId}|${key}`;
    if (last.current === marker) return;
    const timer = window.setTimeout(() => {
      last.current = marker;
      try {
        void putReviewQueueState(reviewId, { resume }).catch(() => { last.current = ""; });
      } catch {
        last.current = "";
      }
    }, SAVE_AFTER_MS);
    return () => window.clearTimeout(timer);
    // `resume` is covered by `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId, key, enabled]);
}
