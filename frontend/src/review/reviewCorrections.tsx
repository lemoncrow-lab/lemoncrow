/**
 * Reader wiring for controlled Review actions: one listing fetch, a context the
 * finding cards read (which action addressed which finding, and Fix), palette
 * and More-menu entries from the server catalog, and the preview sheet.
 *
 * A failed listing fails closed: no entries, no Fix routing, no claims.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useFlowPaletteReviewActions } from "./palette/FlowPalette";
import type { ReviewPaletteAction } from "./reviewActionsModel";
import ReviewFixSheet from "./ReviewFixSheet";
import ReviewSplitSheet from "./ReviewSplitSheet";
import {
  cancelReviewAction,
  fetchReviewActions,
  type ReviewActionKind,
  type ReviewActionListing,
  type ReviewActionRecord,
  type ReviewActionRequest,
} from "./reviewActionsApi";
import { correctionPaletteActions, isActionInFlight, latestActionBySubject } from "./reviewActionsModel";

export interface ReviewCorrectionsContextValue {
  /** Newest action per finding id. */
  byFinding: ReadonlyMap<string, ReviewActionRecord>;
  /** Fix is routable here (a current, writable Snapshot with a loaded catalog). */
  canFix: boolean;
  openFix(subjectIds: string[]): void;
  cancel(actionId: string): void;
}

const ReviewCorrectionsContext = createContext<ReviewCorrectionsContextValue | null>(null);

export const ReviewCorrectionsProvider = ReviewCorrectionsContext.Provider;

/**
 * Where `useReviewCorrections` lands in the Reader: the cards' context, the catalog in the ⌘K palette under
 * "In this review", and the preview sheet Fix or a palette entry opens.
 */
export function ReviewCorrectionsHost({ corrections, children }: {
  corrections: Pick<ReturnType<typeof useReviewCorrections>, "context" | "palette" | "sheet">;
  children: ReactNode;
}) {
  useFlowPaletteReviewActions(corrections.palette);
  return (
    <ReviewCorrectionsContext.Provider value={corrections.context}>
      {children}
      {corrections.sheet}
    </ReviewCorrectionsContext.Provider>
  );
}

export function useReviewCorrectionsContext(): ReviewCorrectionsContextValue | null {
  return useContext(ReviewCorrectionsContext);
}

const POLL_MS = 15_000;

export function useReviewCorrections({
  reviewId,
  revisionId,
  enabled,
  readOnly,
}: {
  reviewId: string;
  revisionId: string;
  enabled: boolean;
  readOnly: boolean;
}): {
  context: ReviewCorrectionsContextValue;
  palette: ReviewPaletteAction[];
  sheet: ReactNode;
  listing: ReviewActionListing | null;
} {
  const [listing, setListing] = useState<ReviewActionListing | null>(null);
  const [request, setRequest] = useState<ReviewActionRequest | null>(null);
  const [splitOpen, setSplitOpen] = useState(false);
  const loadSequence = useRef(0);

  const load = useCallback(async () => {
    const current = ++loadSequence.current;
    if (!enabled || !reviewId) {
      setListing(null);
      return;
    }
    try {
      const next = await fetchReviewActions(reviewId);
      if (current === loadSequence.current) setListing(next && Array.isArray(next.actions) ? next : null);
    } catch {
      if (current === loadSequence.current) setListing(null);
    }
  }, [enabled, reviewId]);

  useEffect(() => {
    void load();
  }, [load, revisionId]);

  const inFlight = Boolean(listing?.actions.some(isActionInFlight));
  useEffect(() => {
    if (!inFlight) return undefined;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [inFlight, load]);

  const writable = enabled && !readOnly && Boolean(listing) && listing?.revision_id === revisionId;
  const open = useCallback((kind: ReviewActionKind, subjectIds: string[]) => {
    setRequest({ kind, subject_ids: subjectIds });
  }, []);

  const cancel = useCallback((actionId: string) => {
    void cancelReviewAction(reviewId, actionId).then(() => load(), () => load());
  }, [reviewId, load]);

  const context = useMemo<ReviewCorrectionsContextValue>(() => ({
    byFinding: latestActionBySubject(listing?.actions ?? []),
    canFix: writable,
    openFix: (subjectIds) => open("fix", subjectIds.slice(0, 1)),
    cancel,
  }), [listing, writable, open, cancel]);

  const palette = useMemo(
    () => (listing && enabled
      ? [
          ...correctionPaletteActions(listing.catalog, open, { busy: !writable }),
          // P12-AGENT: same gates and routes as Fix; the server says why when it cannot split.
          {
            id: "correction-split",
            label: "Request split…",
            detail: writable
              ? "Ask the authoring agent for stacked Changes, one per cohort; this Review stays open"
              : "Available on the current, writable revision",
            disabled: !writable,
            managesFocus: true,
            run: () => setSplitOpen(true),
          } satisfies ReviewPaletteAction,
        ]
      : []),
    [listing, enabled, open, writable],
  );

  const splitSheet = splitOpen && revisionId ? (
    <ReviewSplitSheet key={`split:${revisionId}`} reviewId={reviewId} revisionId={revisionId} onClose={() => setSplitOpen(false)} />
  ) : null;

  const fixSheet = request && revisionId ? (
    <ReviewFixSheet
      key={`${request.kind}:${request.subject_ids.join(",")}`}
      reviewId={reviewId}
      revisionId={revisionId}
      request={request}
      onStarted={() => {
        setRequest(null);
        void load();
      }}
      onClose={() => setRequest(null)}
    />
  ) : null;
  const sheet = fixSheet || splitSheet ? <>{fixSheet}{splitSheet}</> : null;

  return { context, palette, sheet, listing };
}
