import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, CheckCircle2, Clock3, LoaderCircle, ShieldAlert, Undo2 } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/button";
import type { ReviewOutcomeProjection } from "../types";
import { fetchAnnotations, fetchReviewFlowSend, fetchReviewOutcome, fetchRevisions, fetchTargets, listReviews, reviewHref, undoReviewFlowSend } from "../reviewApi";
import { buildReviewFlowPath } from "../reviewRoutes";
import { clearPendingSendToken } from "./pendingSendToken";
import { clearOfflineSend, OFFLINE_SEND_EVENT, readOfflineSend, serverAcceptedSend } from "./offlineReviewSend";
import { flowAction, classifyFocus, doneEnterTarget, screenLegend } from "../flowKeymap";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { reviewPlace } from "../palette/partEntries";
import { openFlowSurface } from "../openSurface";
import { partsOf, plural } from "../overview/overviewModel";
import { tallyLine, type PartTally } from "../home/homeModel";
import { rangeText, timeRange } from "../home/timeEstimate";
import { revisionName } from "../revisionLabel";
import { useOnline } from "../useOnline";
import { isSnoozedReview, pickNextUp, type NextUp, type NextUpCandidate } from "./nextUp";
import { useSaveResume } from "../home/useSaveResume";
import { Skeleton } from "../overview/OverviewParts";
import { SignedRecordLine } from "../record/SignedRecordLine";
import { SecondDoneBody } from "../second/SecondDoneBody";
import { doneFor } from "../second/secondCopy";
import { useSecondReviewer } from "../second/useSecondReviewer";
import type { ReviewFlowSend, ReviewOverview, ReviewTarget } from "../types";

const CHECK_EVERY_MS = 500;
const NEXT_UP_CANDIDATES = 5;
type DoneState = ReviewFlowSend | null;

/** The parts still to answer, split the way Home counts them, so Next up's reason and its range line agree. */
export function openTally(targets: readonly ReviewTarget[]): PartTally {
  const open = targets.filter((target) => target.state !== "reviewed" && target.state !== "needs_changes");
  return { decisions: partsOf(open, "decision").length, quickChecks: partsOf(open, "quick_check").length };
}
export function remainingUndoMs(send: ReviewFlowSend, now: number): number {
  return send.state === "pending" ? Math.max(0, Date.parse(send.send_after) - now) : 0;
}
export function canUndoSend(send: ReviewFlowSend | null, now: number): boolean {
  return Boolean(send && send.state === "pending" && remainingUndoMs(send, now) > 0);
}
export function doneTitle(send: ReviewFlowSend, destination: string): string {
  if (send.state === "pending") return send.has_feedback ? `Sending to ${destination}…`
    : send.outcome === "lgtm" ? "Recording your approval…" : "Recording your verdict…";
  if (send.state === "dispatching") return send.has_feedback ? `Handing off to ${destination}…` : "Recording your verdict…";
  if (send.state === "sent") {
    if (!send.has_feedback) return send.outcome === "lgtm" ? "Approved. Nothing was merged." : "Recorded.";
    return send.outcome === "lgtm" ? `Approved. ${destination} has your notes.` : `Sent. ${destination} is on it.`;
  }
  if (send.state === "undone") return "Send cancelled.";
  if (send.state === "failed") return "Could not send.";
  if (send.state === "uncertain") return "Delivery needs checking.";
  return "Review status";
}

/** The hold the server set for this send, in whole seconds (10 in a real install). */
export function holdSeconds(send: ReviewFlowSend): number {
  const seconds = Math.round((Date.parse(send.send_after) - Date.parse(send.created_at)) / 1000);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : 10;
}

function verdictWord(outcome: ReviewFlowSend["outcome"]): string {
  return outcome === "lgtm" ? "Approve" : outcome === "changes_requested" ? "Request changes" : "Comment";
}

function hostedVerdictWord(outcome: string | null): string {
  return outcome === "lgtm" || outcome === "lgtm_with_suggestions" ? "Approve" : outcome === "changes_requested" ? "Request changes" : "Comment";
}

/**
 * Hosted Done (SPEC 7.1 P9-4 D): a hosted server holds nothing to send later, so there is no hold, no Undo and nothing
 * pending. The verdict is recorded when it is sent, and this says so.
 */
export function hostedReceipt(outcome: string | null, revision: number): string {
  // No verdict read back (the read failed, or none exists): no receipt, rather than one that claims a verdict.
  if (!outcome) return "";
  return `Your verdict on Revision ${revision} (${hostedVerdictWord(outcome)}) is recorded. Nothing was merged.`;
}

/** What the author can read of each verdict on a hosted server (not a promise of a notification: that follows their preferences). */
export function hostedTold(outcome: string | null): string {
  if (outcome === "changes_requested") return "Your request for changes is on the review for the author to read.";
  if (outcome === "comment") return "Your comment is on the review for the author to read.";
  if (outcome === "lgtm" || outcome === "lgtm_with_suggestions") return "An approval is sent to nobody. The author sees it on the review.";
  return "";
}

/** The sub line under the headline (SPEC 4.9). `revision` is the human number. */
export function doneSub(send: ReviewFlowSend | null, revision: number, destination: string): string {
  if (!send || send.state === "none") return "Nothing was sent from this review.";
  const verdict = `Your verdict on Revision ${revision}`;
  const notes = send.note_count ?? 0;
  if (send.state === "pending") {
    const seconds = `in ${holdSeconds(send)} seconds.`;
    if (send.has_feedback) return `${verdict} (${verdictWord(send.outcome)}) will be recorded and ${plural(notes, "note")} sent ${seconds}`;
    return send.outcome === "lgtm"
      ? `${verdict} (Approve) will be recorded ${seconds} LemonCrow never commits or merges.`
      : `${verdict} (${verdictWord(send.outcome)}) will be recorded ${seconds} No notes will be sent.`;
  }
  if (send.state === "dispatching") return "Recording now. Undo is no longer available.";
  if (send.state === "sent") {
    return `${verdict} was recorded${send.has_feedback ? ` and ${plural(notes, "note")} went to ${destination}` : ""}. Nothing was committed or merged.`;
  }
  if (send.state === "undone") return "Nothing was sent. Your answers and notes remain in Wrap up.";
  if (send.state === "failed") return "Nothing was delivered. Your answers and notes are still in Wrap up, so you can send again.";
  return `We could not confirm whether ${destination} received it. LemonCrow will not send it again on its own. Check with ${destination}, or send again from Wrap up.`;
}

/**
 * "Sent just now" for the first minute, then "Sent N min ago". Nothing about undoing: "Undo" names only the hold
 * before the send, and once it ends the one way back is the record's "Retract this verdict" (review-20.5).
 */
export function sentAgo(send: ReviewFlowSend, now: number): string {
  const at = Date.parse(send.updated_at ?? send.send_after);
  const minutes = Number.isFinite(at) ? Math.floor((now - at) / 60_000) : 0;
  return minutes >= 1 ? `Sent ${minutes} min ago` : "Sent just now";
}

/**
 * The bar's centre text on Done (SPEC 3.3): "Sending · Revision 1", "Sent · Revision 1", "Approved · Revision 1",
 * "Recorded · Revision 1", "Waiting to send · Revision 1". "Waiting for 2nd signature" needs a co-signature state this
 * screen does not receive. The ends that have no SPEC text (cancelled, failed, not confirmed) are SPEC 7.1 W2 E.
 */
export function doneStatus(send: ReviewFlowSend | null, revision: string, local: "waiting" | "checking" | "needs_review" | null): { text: string; tone: "neutral" | "success" | "info" } | undefined {
  // The words follow the page body: "Waiting to send", "Checking send status…", "Review changed. Send not confirmed."
  if (local === "waiting") return { text: `Waiting to send · ${revision}`, tone: "neutral" };
  if (local === "checking") return { text: `Checking send status · ${revision}`, tone: "neutral" };
  if (local === "needs_review") return { text: `Send not confirmed · ${revision}`, tone: "neutral" };
  if (!send) return undefined;
  if (send.state === "pending" || send.state === "dispatching") return { text: `Sending · ${revision}`, tone: "neutral" };
  if (send.state === "sent") {
    if (send.has_feedback) return { text: `Sent · ${revision}`, tone: "neutral" };
    return send.outcome === "lgtm" ? { text: `Approved · ${revision}`, tone: "success" } : { text: `Recorded · ${revision}`, tone: "neutral" };
  }
  if (send.state === "undone") return { text: `Cancelled · ${revision}`, tone: "neutral" };
  if (send.state === "failed") return { text: `Not sent · ${revision}`, tone: "neutral" };
  if (send.state === "uncertain") return { text: `Not confirmed · ${revision}`, tone: "neutral" };
  return undefined;
}

export interface ReviewDoneScreenProps {
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  destinationLabel: string;
  onDownloadRecord?(): Promise<string>;
  /** "Ask about the change" (the top bar); absent when AI Ask is off. */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}

export function ReviewDoneScreen({ reviewRef, apiReviewId, overview, targets, destinationLabel, onDownloadRecord, onAskChange, askPanel }: ReviewDoneScreenProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const humanRevision = overview.revision.revision_number + 1;
  // A sent review has no place to continue from (Home would otherwise offer Revision 1's last part).
  useSaveResume(apiReviewId, null);
  const [send, setSend] = useState<DoneState>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [undoing, setUndoing] = useState(false);
  const [now, setNow] = useState(Date.now);
  const [next, setNext] = useState<NextUp | null>(null);
  /** The next review's parts as Home counts them, for the same "1 decision · 4–7 min" line (HOME-9). */
  const [nextTally, setNextTally] = useState<PartTally | null>(null);
  const [nextLoadError, setNextLoadError] = useState("");
  // Until the list is read, Next up shows a placeholder, never "All caught up" (it may not be).
  const [nextLoading, setNextLoading] = useState(true);
  const [revisionReady, setRevisionReady] = useState<number | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [localIntent, setLocalIntent] = useState(readOfflineSend);
  // Hosted only: once a second signature is in play, Done says where it stands (SPEC 4.16, R3, R13). Local: null.
  const second = useSecondReviewer(apiReviewId, { watchMs: 3000 });
  const secondDone = second.status ? doneFor(second.status, revisionName(overview.revision.revision_number)) : null;
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusedRef = useRef(false);
  const online = useOnline();
  useEffect(() => {
    const sync = () => setLocalIntent(readOfflineSend());
    window.addEventListener(OFFLINE_SEND_EVENT, sync);
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    return () => {
      window.removeEventListener(OFFLINE_SEND_EVENT, sync);
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);

  const fetchStatus = useCallback(async () => {
    try {
      const status = await fetchReviewFlowSend(apiReviewId);
      setSend(status);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setLoading(false); }
  }, [apiReviewId]);

  // Hosted holds no send (SPEC 7.1 P9-4 D): the verdict is recorded at once and nothing is pending, so there is no
  // hold to watch, no Undo and no polling. A server that answers the second-reviewer route is a hosted one.
  const hosted = second.status !== null;
  const [recorded, setRecorded] = useState<ReviewOutcomeProjection | null>(null);
  useEffect(() => {
    if (!second.settled) return;
    if (hosted) {
      setLoading(false);
      let active = true;
      void fetchReviewOutcome(apiReviewId).then((outcome) => { if (active) setRecorded(outcome); }).catch(() => { /* Done then says only what it knows */ });
      return () => { active = false; };
    }
    setSend(null); setLoading(true); setError("");
    void fetchStatus();
    const interval = window.setInterval(() => {
      setNow(Date.now());
      void fetchStatus();
    }, CHECK_EVERY_MS);
    return () => window.clearInterval(interval);
  }, [fetchStatus, hosted, second.settled, apiReviewId]);

  // lc-debt: compares at most NEXT_UP_CANDIDATES open reviews, two reads each; a ranked server queue would replace it.
  useEffect(() => {
    let active = true;
    setNextLoading(true);
    listReviews("open").then(async ({ reviews }) => {
      const all = reviews.filter((review) => review.id !== apiReviewId && review.id !== overview.session.id && review.ref !== reviewRef && review.ref !== overview.session.ref && !isSnoozedReview(review, Date.now()));
      const others = all.slice(0, NEXT_UP_CANDIDATES);
      const read = await Promise.all(others.map(async (review): Promise<NextUpCandidate | null> => {
        try {
          const [list, notes] = await Promise.all([fetchTargets(review.id), fetchAnnotations(review.id).catch(() => null)]);
          return { review, targets: list.targets, feedback: notes?.feedback };
        } catch { return null; }
      }));
      if (!active) return;
      const known = read.filter((item): item is NextUpCandidate => item !== null);
      // A review whose parts could not be read still opens; it just carries no reason.
      let picked: NextUp | null = null;
      try { picked = pickNextUp(known, all.length <= NEXT_UP_CANDIDATES && known.length === others.length); } catch { /* the review still opens, with no reason claimed */ }
      const pickedCandidate = picked ? known.find((item) => item.review.id === picked?.review.id) : undefined;
      setNextTally(pickedCandidate ? openTally(pickedCandidate.targets) : null);
      setNext(picked ?? (others[0] ? { review: others[0], action: null, yourTurn: false, decisions: 0, reason: "" } : null));
      setNextLoadError("");
      setNextLoading(false);
    }).catch(() => {
      if (!active) return;
      setNextLoadError("Next review unavailable. Open your review list.");
      setNextLoading(false);
    });
    return () => { active = false; };
  }, [apiReviewId, overview.session.id, overview.session.ref, reviewRef]);

  useEffect(() => {
    if (send?.state !== "sent") return;
    let active = true;
    const fetch = () => {
      void fetchRevisions(apiReviewId).then((revisions) => {
        if (!active) return;
        const newest = revisions.latest_revision_number;
        // Revision history uses zero-based wire numbers. /rev/<n> names the
        // one-based human revision, so the new wire revision 1 is /rev/2.
        if (typeof newest === "number" && newest > overview.revision.revision_number)
          setRevisionReady(newest + 1);
      }).catch(() => { /* no claimed revision if the current revision cannot be checked */ });
    };
    fetch();
    const interval = window.setInterval(fetch, 10_000);
    return () => { active = false; window.clearInterval(interval); };
  }, [apiReviewId, send?.state, overview.revision.revision_number]);

  const undo = useCallback(async () => {
    if (!send || !canUndoSend(send, Date.now()) || undoing) return;
    setUndoing(true); setError("");
    try {
      const result = await undoReviewFlowSend(apiReviewId, send.operation_id);
      setSend(result);
      clearPendingSendToken(send.operation_id);
      navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The send can no longer be undone.");
      await fetchStatus();
    } finally { setUndoing(false); }
  }, [send, undoing, apiReviewId, reviewRef, navigate, fetchStatus]);

  const unsentLocal = localIntent?.reviewId === apiReviewId && (!send || !serverAcceptedSend(send, localIntent)) ? localIntent : null;
  const canUndo = canUndoSend(send, now);
  const countdown = send ? Math.ceil(remainingUndoMs(send, now) / 1000) : 0;
  const judgedCount = useMemo(() => targets.filter((target) => (target.state === "reviewed" || target.state === "needs_changes") && !target.changed_since_mark).length, [targets]);
  const isWaiting = send?.state === "pending";
  const pending = isWaiting || send?.state === "dispatching";
  const sent = send?.state === "sent";
  const noteCount = send?.note_count ?? 0;
  const hostedVerdict = hosted ? recorded?.current?.outcome ?? null : null;

  // Arriving from another screen puts focus on the heading; a reload or a cold link leaves it where the browser puts it.
  const inApp = location.key !== "default";
  useEffect(() => {
    if (!inApp || loading || focusedRef.current) return;
    focusedRef.current = true;
    headingRef.current?.focus();
  }, [inApp, loading]);

  // The two things Enter does on Done; the palette's rows call the same two functions.
  const openRevision = useCallback(() => {
    if (revisionReady !== null) navigate(buildReviewFlowPath(reviewRef, { kind: "rev", revision: revisionReady }));
  }, [navigate, reviewRef, revisionReady]);
  const openNext = useCallback(() => {
    if (next) navigate(reviewHref(next.review.ref || next.review.id));
  }, [navigate, next]);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const node = document.activeElement as HTMLElement | null;
      const focus = node ? classifyFocus({ tagName: node.tagName, isContentEditable: node.isContentEditable, role: node.getAttribute("role") }) : "other";
      const action = flowAction(event, { screen: "done", surface: openFlowSurface(), focus, undoLive: canUndo, revision2Ready: revisionReady !== null, hasNextReview: next !== null });
      if (action?.intent === "help") {
        event.preventDefault(); setShortcutsOpen(true);
      } else if (action?.intent === "undo" && canUndo) {
        event.preventDefault(); void undo();
      } else if ((action?.intent === "startNextReview" || action?.intent === "reviewRevision2")) {
        event.preventDefault();
        if (action.intent === "reviewRevision2") openRevision(); else openNext();
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [canUndo, undo, openRevision, openNext, next, revisionReady]);

  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    place: reviewPlace(repoNameOf(overview), overview.title, "Done"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "done",
      revision2Ready: revisionReady !== null,
      revision2Label: revisionReady !== null ? `Review ${revisionName(revisionReady)}` : undefined,
      undoLive: canUndo,
      hasNextReview: next !== null,
      actions: { reviewRevision2: openRevision, startNextReview: openNext, undo: () => void undo() },
    },
  });

  const bar = (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Done"
        title={overview.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: "Reviews", onClick: () => navigate("/reviews") }}
        status={secondDone ? { text: secondDone.bar, tone: secondDone.tone === "success" ? "success" : "neutral" } : hosted ? { text: `Recorded · ${revisionName(overview.revision.revision_number)}`, tone: "success" } : doneStatus(send, revisionName(overview.revision.revision_number), unsentLocal ? (unsentLocal.state === "needs_review" ? "needs_review" : unsentLocal.attempted ? "checking" : "waiting") : null)}
        onAsk={onAskChange}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("done", { aiAsk: false, undoLive: canUndo, revision2Ready: revisionReady !== null, hasNext: next !== null })} />
    </>
  );

  if (secondDone && second.status) {
    return <>
      {bar}
      <SecondDoneBody ref={headingRef} copy={secondDone} status={second.status} revisionLabel={revisionName(overview.revision.revision_number)} reviewId={apiReviewId} revisionId={overview.revision.id} revisionNumber={overview.revision.revision_number} />
    </>;
  }

  if (unsentLocal) {
    const neverAttempted = !unsentLocal.attempted;
    const needsReview = unsentLocal.state === "needs_review";
    const savedNotes = unsentLocal.noteCount ?? 0;
    const saved = savedNotes > 0 ? `Your verdict and ${plural(savedNotes, "note")} are` : "Your verdict is";
    const counts = [judgedCount > 0 ? `${plural(judgedCount, "part")} judged` : "", savedNotes > 0 ? `${plural(savedNotes, "note")} waiting to send` : ""].filter(Boolean).join(" · ");
    return <>
      {bar}
      <div role="region" aria-label="Done" data-testid="done-offline" className="min-w-0 flex-1">
        <section className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
          <Clock3 size={20} aria-hidden="true" />
          <p className="font-mono text-tag text-muted-foreground">Review · {revisionName(overview.revision.revision_number)}</p>
          <h1 ref={headingRef} tabIndex={-1} className="text-title font-semibold focus:outline-none" data-testid="done-title">{needsReview ? "Review changed. Send not confirmed." : neverAttempted ? "Waiting to send" : "Checking send status…"}</h1>
          <p className="text-label text-muted-foreground" data-testid="done-offline-receipt">
            {needsReview ? "The server did not accept this saved send. Check the latest review before trying again."
              : neverAttempted ? `${online ? "The send has not gone out yet." : "You're offline."} ${saved} saved here and will be sent when you're back online. They have not been sent. Nothing was lost.`
                : "The network connection interrupted confirmation. The server may already have accepted the request. LemonCrow will reconcile the same operation ID, without sending a duplicate. Do not assume it was cancelled."}
          </p>
          {counts && <p className="text-meta text-muted-foreground">{counts}</p>}
          <div className="flex flex-wrap gap-2">
            {neverAttempted || needsReview ? <Button variant="outline" onClick={() => {
              clearOfflineSend(unsentLocal.operationId);
              navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }));
            }}>{neverAttempted ? "Cancel and edit" : "Review and edit"}</Button> : null}
            <Button variant="outline" onClick={() => navigate("/reviews")}>Done for now</Button>
          </div>
          <p className="text-meta text-muted-foreground">Offline recovery is limited to this browser tab. Keep it open until the send is confirmed. No server acknowledgement or Undo window is claimed until the server accepts it.</p>
        </section>
      </div>
      <FlowAskOverlay>{askPanel}</FlowAskOverlay>
    </>;
  }

  const destination = destinationLabel;
  // One rule for Enter (flowKeymap `doneEnterTarget`): the chips and the buttons' look follow it.
  const enterTarget = doneEnterTarget({ revisionReady: revisionReady !== null, undoLive: canUndo, hasNext: next !== null });
  const reviewRevisionPrimary = enterTarget === "revision";
  const enterOnRevision = enterTarget === "revision";
  const sessionFacts = [
    "This session",
    judgedCount > 0 ? `${plural(judgedCount, "part")} judged` : "",
    send?.has_feedback && noteCount > 0 ? `${plural(noteCount, "note")} ${sent || send.state === "dispatching" ? "sent" : "to send"}` : "",
  ].filter(Boolean).join(" · ");

  return (
    <>
      {bar}
      <div role="region" data-review-scroll-root="done" aria-label="Done" className="min-w-0 flex-1">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-5 pb-24" data-testid="review-done">
          {/* The heading names the send; the bar only holds the undo window (review-20.1). */}
          {isWaiting && <div role="status" data-testid="done-undo-bar" className="flex flex-wrap items-center gap-3 rounded-lg bg-foreground p-3 text-background">
            <span className="min-w-0 flex-1 font-semibold text-label">{canUndo ? "Still time to undo" : "Undo window closed · waiting for the server"}</span>
            {canUndo && <><span className="rounded-full border border-current px-2 py-1 font-mono text-meta" aria-hidden="true">{countdown}s</span>
              <Button variant="outline" className="min-w-11 border-current text-background" disabled={undoing} onClick={() => void undo()} aria-label="Undo send"><Undo2 size={16} aria-hidden="true" /> Undo <span className="font-mono text-meta pointer-coarse:hidden">Z</span></Button></>}
          </div>}
          {send && !pending && send.state !== "none" && <p role="status" className="text-meta text-muted-foreground" data-testid="done-dispatched-status">{sent ? sentAgo(send, now) : send.state === "undone" ? "Cancelled before sending" : send.state === "failed" ? "Nothing delivered" : send.state === "uncertain" ? "Delivery not confirmed" : "Send status updated"}</p>}

          <section className="space-y-3" aria-labelledby="done-heading">
            <div className="flex h-14 w-14 items-center justify-center rounded-full border border-border bg-card">
              {hosted ? <CheckCircle2 size={20} className="text-ok" aria-hidden="true" /> : loading || pending ? <LoaderCircle size={20} className="animate-spin" aria-hidden="true" /> : sent ? <CheckCircle2 size={20} className="text-ok" aria-hidden="true" /> : send?.state === "failed" || send?.state === "uncertain" ? <ShieldAlert size={20} aria-hidden="true" /> : <Clock3 size={20} aria-hidden="true" />}
            </div>
            <p className="font-mono text-tag text-muted-foreground">Review · {revisionName(overview.revision.revision_number)}</p>
            <h1 id="done-heading" ref={headingRef} tabIndex={-1} className="text-title font-semibold focus:outline-none" data-testid="done-title">{hosted ? "Recorded." : loading ? "Checking send status…" : send ? doneTitle(send, destination) : "No send recorded."}</h1>
            {(hosted ? hostedReceipt(hostedVerdict, humanRevision) : doneSub(send, humanRevision, destination)) && <p className="text-label text-muted-foreground" data-testid="done-receipt">{hosted ? hostedReceipt(hostedVerdict, humanRevision) : doneSub(send, humanRevision, destination)}</p>}
            {hosted && hostedTold(hostedVerdict) && <p className="text-label text-muted-foreground" data-testid="done-told">{hostedTold(hostedVerdict)}</p>}
          </section>
          {error && <p role="alert" className="rounded-md border border-p1-line bg-p1-bg p-3 text-label">{error}</p>}
          <p className="text-meta text-muted-foreground">{sessionFacts}</p>

          {send?.has_feedback && <section aria-label="Author progress" className="rounded-lg border border-border bg-card p-4">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-violet-500" aria-hidden="true" />
              <h2 className="text-heading font-semibold">{destination} · {isWaiting ? "not started yet" : sent ? "handoff accepted" : send.state === "dispatching" ? "sending" : "needs attention"}</h2>
            </div>
            <ol className="mt-3 space-y-2 text-label">
              <li>{isWaiting ? "Starts when the undo window ends" : sent ? "Notes handed to the coding session" : "Delivery not confirmed"}</li>
              {sent && revisionReady === null && <li>Waiting for a new captured revision. No fix or test result has been assumed.</li>}
              {revisionReady !== null && <li><Check size={16} className="inline text-ok" aria-hidden="true" /> Revision {revisionReady} is ready.</li>}
            </ol>
            {revisionReady !== null && <Button variant="brand" className="mt-3" onClick={openRevision}>Review Revision {revisionReady} {enterOnRevision && <span className="font-mono text-meta pointer-coarse:hidden">Enter</span>}</Button>}
          </section>}

          <section className="rounded-lg border border-border bg-card p-4" aria-labelledby="next-up-title" aria-busy={nextLoading || undefined} data-testid="done-next-up">
            <p className="font-mono text-tag text-muted-foreground">{next?.reason ? `Next up · ${next.reason}` : "Next up"}</p>
            {nextLoading ? <>
              <h2 id="next-up-title" className="mt-1 text-heading font-semibold" data-testid="done-next-up-loading"><span className="sr-only">Looking for your next review…</span><Skeleton className="h-5 w-2/3" /></h2>
              <Skeleton className="mt-2 w-1/2" />
            </> : <>
              <h2 id="next-up-title" className="mt-1 text-heading font-semibold">{next?.review.title || (nextLoadError ? "Review list" : "All caught up here")}</h2>
              <p className="mt-2 text-label text-muted-foreground">{next ? (next.action?.detail || "Another open review.") : nextLoadError || "No other open reviews found. You can return to the list."}</p>
            </>}
            {next && nextTally && <p data-testid="done-next-up-range" className="mt-1 font-mono text-meta text-muted-foreground">{tallyLine(nextTally)} · {rangeText(timeRange(nextTally))}</p>}
            <div className="mt-3 flex flex-wrap gap-2">
              {next && <Button variant={reviewRevisionPrimary ? "outline" : "brand"} onClick={openNext}>Start next review {!enterOnRevision && <span className="font-mono text-meta pointer-coarse:hidden">Enter</span>}</Button>}
              <Button variant="outline" onClick={() => navigate("/reviews")}>Done for now</Button>
            </div>
          </section>

          {(sent ? send?.outcome !== "comment" : hosted && hostedVerdict !== null && hostedVerdict !== "comment") && <SignedRecordLine reviewId={apiReviewId} revisionId={overview.revision.id} revisionNumber={overview.revision.revision_number} onDownloadUnsigned={onDownloadRecord} onOpenWrap={() => navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }))} />}

          {(send?.state === "failed" || send?.state === "uncertain") && <p className="rounded-lg border border-p1-line bg-p1-bg p-4 text-label" role="alert">
            LemonCrow won't send this again on its own.
          </p>}
          {send?.state === "none" || send?.state === "undone" || send?.state === "failed" || send?.state === "uncertain" ? <Button variant="outline" className="self-start" onClick={() => navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }))}>Return to Wrap up</Button> : null}
          <Button variant="ghost" className="self-start" onClick={() => navigate("/reviews")}>Back to your reviews</Button>
        </div>
      </div>
      <FlowAskOverlay>{askPanel}</FlowAskOverlay>
    </>
  );
}
