import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { AiBudgetBanner } from "./AiBudgetBanner";
import { FlowOfflineBanner } from "./FlowOfflineBanner";
import { isColdLoad } from "./reviewBootLocation";
import { readOfflineSend } from "./done/offlineReviewSend";
import { fetchSecondReviewer } from "./second/secondApi";
import { fetchHistoricalAnnotations, fetchReviewFlowSend, fetchReviewOutcome, fetchRevisions } from "./reviewApi";
import {
  buildReviewFlowPath,
  coldLoadRedirect,
  legacyFlowRedirect,
  needsSentRecord,
  parseReviewFlowLocation,
  OVERVIEW_ROUTE,
} from "./reviewRoutes";

/** What a review has recorded, as far as the cold-load rules need it. All false or 0 when it cannot be read. */
export interface ReviewSendState {
  sentRecord: boolean;
  /** A queued/undone server send earns Done even before any verdict is written. */
  flowSendExists?: boolean;
  notesSentToAgent: boolean;
  latestRevision: number;
  /** The revision the review's verdict stands on (not stale); absent when there is none. `sentRecord` counts any verdict. */
  verdictRevisionId?: string;
}

const NOTHING_SENT: ReviewSendState = { sentRecord: false, notesSentToAgent: false, latestRevision: 0 };

/** What the cold-load rules ask the app about a review. */
export interface ReviewFlowFactsSource {
  /** Never rejects; a read that fails answers "nothing sent". `revision` is the n of `/rev/<n>`, 0 for any other screen. */
  readSendState(reviewRef: string, revision: number): Promise<ReviewSendState>;
  /** Whether the viewer is the review's second reviewer (R8). Never rejects: a read that fails is a no. */
  readSecondReviewer(reviewRef: string): Promise<boolean>;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/**
 * A recorded verdict is the closest thing the local store has to "sent" (SPEC
 * 7.1 P1-3 B). Notes reached the agent when the previous revision's feedback has
 * a delivery in state sent: its `published` or `addressed` count. Only the
 * latest revision's counts start again at zero when the next one is captured, so
 * the previous one is read from its own annotation list. Every answer that is
 * not a clear yes is a no.
 */
export async function readLocalSendState(reviewRef: string, revision = 0): Promise<ReviewSendState> {
  const [outcome, revisions, notes, send] = await Promise.allSettled([
    fetchReviewOutcome(reviewRef),
    fetchRevisions(reviewRef),
    // People count revisions from 1, the wire from 0: Revision n-1 is wire n-2.
    revision >= 2 ? fetchHistoricalAnnotations(reviewRef, revision - 2) : Promise.resolve(null),
    revision === 0 ? fetchReviewFlowSend(reviewRef) : Promise.resolve(null),
  ]);
  const feedback = notes.status === "fulfilled" ? notes.value?.feedback : undefined;
  const verdict = outcome.status === "fulfilled" ? outcome.value?.current : null;
  const wire = revisions.status === "fulfilled" ? revisions.value?.latest_revision_number : undefined;
  return {
    sentRecord: outcome.status === "fulfilled" && Boolean(outcome.value?.current),
    ...((send.status === "fulfilled" && send.value && ["pending", "dispatching", "sent", "undone", "failed", "uncertain"].includes(send.value.state)
      && /^fs-[0-9a-f]{32}$/.test(send.value.operation_id)) || (revision === 0 && readOfflineSend()?.reviewRef === reviewRef)
      ? { flowSendExists: true } : {}),
    notesSentToAgent: count(feedback?.published) + count(feedback?.addressed) > 0,
    latestRevision: typeof wire === "number" && Number.isInteger(wire) && wire >= 0 ? wire + 1 : 0,
    ...(verdict && !verdict.stale && verdict.revision_id ? { verdictRevisionId: verdict.revision_id } : {}),
  };
}

/** The server says (hosted only, SPEC 7.1 P0-2): a local install has no such route, so nobody is a second reviewer. */
export async function readServerSecondReviewer(reviewRef: string): Promise<boolean> {
  try {
    return (await fetchSecondReviewer(reviewRef))?.role === "second";
  } catch {
    return false;
  }
}

export const localReviewFlowFacts: ReviewFlowFactsSource = {
  readSendState: readLocalSendState,
  readSecondReviewer: readServerSecondReviewer,
};

type SentCheck = { state: ReviewSendState } | null;

function useSendState(reviewRef: string, revision: number, needed: boolean, facts: ReviewFlowFactsSource): SentCheck | "not needed" {
  const [answer, setAnswer] = useState<{ reviewRef: string; revision: number; state: ReviewSendState } | null>(null);
  useEffect(() => {
    if (!needed) return;
    let live = true;
    facts
      .readSendState(reviewRef, revision)
      .catch(() => NOTHING_SENT)
      .then((state) => {
        if (live) setAnswer({ reviewRef, revision, state });
      });
    return () => {
      live = false;
    };
  }, [facts, needed, revision, reviewRef]);
  if (!needed) return "not needed";
  if (!answer || answer.reviewRef !== reviewRef || answer.revision !== revision) return null;
  return { state: answer.state };
}

/** R8: whether the viewer is the second reviewer, asked only on `/second`; `null` while it is read. */
function useSecondRole(reviewRef: string, needed: boolean, facts: ReviewFlowFactsSource): boolean | "not needed" | null {
  const [answer, setAnswer] = useState<{ reviewRef: string; value: boolean } | null>(null);
  useEffect(() => {
    if (!needed) return;
    let live = true;
    facts.readSecondReviewer(reviewRef).catch(() => false).then((value) => {
      if (live) setAnswer({ reviewRef, value });
    });
    return () => { live = false; };
  }, [facts, needed, reviewRef]);
  if (!needed) return "not needed";
  if (!answer || answer.reviewRef !== reviewRef) return null;
  return answer.value;
}

/**
 * Wraps the Reader under `/r/<review>/…`: applies the
 * route rules (SPEC 3.1) and tells the page which screen the address names.
 *
 * The redesigned screens render at their named addresses. Deep links, reload, Back and Forward
 * follow the same route rules. An unknown path under the review, `/second` for
 * anyone but the second reviewer, and a cold `/done` or `/rev/<n>` with nothing
 * sent are replaced (never pushed) with the Overview. An old walkthrough address
 * is replaced with `/walkthrough`.
 */
export function ReviewFlowRoute({
  children,
  facts = localReviewFlowFacts,
}: {
  children: ReactNode;
  facts?: ReviewFlowFactsSource;
}) {
  const location = useLocation();
  const parsed = parseReviewFlowLocation(location);
  const reviewRef = parsed?.reviewRef ?? "";
  const route = parsed?.route ?? OVERVIEW_ROUTE;
  const cold = isColdLoad();
  const check = useSendState(reviewRef, route.kind === "rev" ? route.revision : 0, Boolean(parsed?.known) && cold && needsSentRecord(route), facts);

  const second = useSecondRole(reviewRef, Boolean(parsed?.known) && route.kind === "second", facts);
  if (!parsed) return <>{children}</>;
  const overview = buildReviewFlowPath(reviewRef, OVERVIEW_ROUTE);
  if (!parsed.known) return <Navigate to={overview} replace />;
  // The walkthrough's old addresses (`/tour`, the Classic Guided review and Change Story) are the Walkthrough's.
  const legacy = legacyFlowRedirect(location);
  if (legacy) return <Navigate to={legacy} replace />;
  const checking = check === null || second === null;
  const state = check && check !== "not needed" ? check.state : NOTHING_SENT;
  const redirect = checking && route.kind === "second" ? null : coldLoadRedirect(
    route,
    { ...state, secondReviewer: second === true },
    { cold: cold && check !== null },
  );
  if (redirect) return <Navigate to={buildReviewFlowPath(reviewRef, redirect)} replace />;
  if (checking) return null;
  return (
    <div
      data-review-flow={route.kind}
      data-review-flow-part={route.kind === "one" || route.kind === "all" ? route.part : undefined}
      style={{ display: "contents" }}
    >
      <FlowOfflineBanner />
      <AiBudgetBanner />
      {children}
    </div>
  );
}
