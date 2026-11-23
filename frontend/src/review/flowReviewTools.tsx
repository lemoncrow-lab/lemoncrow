import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { isQueuedWrite, queuedWriteNotice } from "./hostedQueue";
import { patchAnnotation } from "./reviewApi";
import { activeReviewFriction } from "./reviewFriction";
import type {
  Annotation, AnnotationDraft, FindingDismissalReason, FindingDisposition, HostedAttachmentInfo, HostedAttachmentStatus,
  ReviewRevisionHistory, ReviewTarget,
} from "./types";

/** The hosted attachment's queue, for the top bar's chip. */
export interface FlowHostedQueue {
  info: HostedAttachmentInfo;
  status: HostedAttachmentStatus | null;
  error: string;
  onStatus(status: HostedAttachmentStatus): void;
  onDetached(localReviewId: string): void;
}

/**
 * What the Reader hands every flow screen's top bar: the review's notes and revisions and the writes on them. The
 * top bar draws the revision menu, the Notes list and the hosted chip from it; outside the Reader (a unit test, the
 * Compare screen) there is none and the bar draws the plain revision chip.
 */
export interface FlowReviewTools {
  /** The API id. */
  reviewId: string;
  /** The `/r/<reviewRef>` route name. */
  reviewRef: string;
  revisionId: string;
  revisionNumber: number;
  historical: boolean;
  readOnly: boolean;
  busy: boolean;
  /** Every annotation on screen, below-threshold findings marked. */
  annotations: readonly Annotation[];
  targets: readonly ReviewTarget[];
  revisionHistory: ReviewRevisionHistory | null;
  setState(id: string, state: "open" | "resolved"): void;
  setDisposition(id: string, disposition: FindingDisposition, reason?: FindingDismissalReason): void;
  createNote(draft: AnnotationDraft): Promise<unknown>;
  hosted: FlowHostedQueue | null;
}

interface FlowReviewToolsContextValue extends FlowReviewTools {
  notesOpen: boolean;
  setNotesOpen(open: boolean): void;
}

const FlowReviewToolsContext = createContext<FlowReviewToolsContextValue | null>(null);

/** Holds whether the Notes list is open, so the top bar's button and ⌘K open the same one across screens. */
export function FlowReviewToolsProvider({ value, children }: { value: FlowReviewTools | null; children: ReactNode }) {
  const [notesOpen, setNotesOpen] = useState(false);
  const reviewId = value?.reviewId ?? "";
  useEffect(() => { setNotesOpen(false); }, [reviewId]);
  const merged = useMemo(() => (value ? { ...value, notesOpen, setNotesOpen } : null), [value, notesOpen]);
  return <FlowReviewToolsContext.Provider value={merged}>{children}</FlowReviewToolsContext.Provider>;
}

export function useFlowReviewTools(): FlowReviewToolsContextValue | null {
  return useContext(FlowReviewToolsContext);
}

/**
 * Dismiss, confirm or reopen an AI finding. Disposition is a fact about the finding, never a mark on the part. A
 * hosted review queues it instead (HTTP 202).
 */
export function useFindingDisposition({ reviewId, revisionId, readOnly, onReplaced, onQueued, onFailure }: {
  reviewId: string;
  revisionId: string;
  readOnly: boolean;
  onReplaced(annotation: Annotation): void;
  onQueued(notice: string): void;
  onFailure(reason: unknown): void;
}) {
  const [pending, setPending] = useState(false);
  const live = useRef({ onReplaced, onQueued, onFailure });
  live.current = { onReplaced, onQueued, onFailure };
  const setDisposition = useCallback(async (id: string, disposition: FindingDisposition, reason: FindingDismissalReason = "") => {
    if (!reviewId || readOnly) return;
    setPending(true);
    try {
      const result: unknown = await patchAnnotation(id, { finding_disposition: disposition, finding_disposition_reason: reason }, revisionId);
      activeReviewFriction()?.judgment();
      if (isQueuedWrite(result)) live.current.onQueued(queuedWriteNotice(result));
      else if (result && typeof result === "object" && "annotation" in result) live.current.onReplaced((result as { annotation: Annotation }).annotation);
    } catch (failure) {
      live.current.onFailure(failure);
    } finally {
      setPending(false);
    }
  }, [reviewId, revisionId, readOnly]);
  return { setDisposition, pending };
}
