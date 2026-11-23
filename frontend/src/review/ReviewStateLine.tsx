import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";

import { Button } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetLede, SheetTitle } from "../components/ui/sheet";
import { AgentApprovalPanel } from "./AgentActions";
import { fetchAgentApprovals, type AgentApproval } from "./agentApprovalsApi";
import { finishReview, publishReviewSnapshot, reviewCompareHref, reviewHref } from "./reviewApi";
import { revisionName } from "./revisionLabel";
import { stateLine, type StateLineFacts, type StateLineTone } from "./reviewStateLineModel";

const TONE_CLASS: Record<StateLineTone, string> = {
  p0: "border-p0-line bg-p0-bg",
  p1: "border-p1-line bg-p1-bg",
  p2: "border-p2-line bg-p2-bg",
  neutral: "border-border bg-muted",
};

const APPROVAL_POLL_MS = 5_000;
/** No approval policy and nothing waiting: an agent is unlikely to ask, so look less often. */
const APPROVAL_IDLE_POLL_MS = 60_000;

/**
 * Approval requests the agent host's hook filed for this Review. A failed read keeps what is shown; nothing is
 * read while `enabled` is false.
 */
export function usePendingAgentApprovals(reviewId: string, enabled: boolean, initial: readonly AgentApproval[] | undefined) {
  const [approvals, setApprovals] = useState<AgentApproval[]>(() => [...(initial ?? [])]);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!enabled || !reviewId) {
      setApprovals([]);
      return undefined;
    }
    let live = true;
    let timer = 0;
    // lc-debt: polls /agent-approvals (5s while a policy exists or a request waits; 60s with no policy, after a failed
    // read, or on a server without the route) because a filed request moves no /live-state cursor; upgrade path: an
    // approvals cursor in /live-state so the Reader reads only on change.
    const tick = async () => {
      // A hidden page reads nothing, and looks again soon so a returning reviewer sees a waiting agent.
      let interval = APPROVAL_POLL_MS;
      if (document.visibilityState !== "hidden") {
        interval = APPROVAL_IDLE_POLL_MS;
        try {
          const listing = await fetchAgentApprovals(reviewId);
          if (!live) return;
          if (Array.isArray(listing?.approvals)) {
            setApprovals(listing.approvals);
            if (listing.policy || listing.approvals.some((item) => item.status === "pending")) interval = APPROVAL_POLL_MS;
          }
        } catch {
          // Keep what is shown; a failed read never invents or drops a request.
        }
      }
      if (live) timer = window.setTimeout(() => void tick(), interval);
    };
    void tick();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [reviewId, enabled, nonce]);
  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { approvals, pending: approvals.filter((item) => item.status === "pending").length, reload };
}

export interface ReviewStateLineProps extends Omit<StateLineFacts, "failure" | "pendingApprovals"> {
  reviewId: string;
  reviewRef: string;
  /** A host made this Review read-only and says why itself: no write is offered here. */
  forcedReadOnly: boolean;
  /** Another Reader write is running; the line's own writes wait for it. */
  busy: boolean;
  /** Approval requests the Reader already holds (Development), shown before the first read. */
  approvals?: readonly AgentApproval[];
  /** Runs one of the line's writes as the Reader's one write at a time, then reads the Review again; rejects when it failed. */
  onWrite(kind: "snapshot" | "reopen", write: () => Promise<unknown>): Promise<void>;
  onOpenNewer(): Promise<void> | void;
  onRetryContext(): Promise<void> | void;
  /** Captures and publishes the working tree; resolves to why it did not, or "" once it did. */
  onSnapshotNow(): Promise<string>;
  onDismissSource(): void;
}

interface Failure { label: string; message: string; retry: () => void }

/**
 * One row under the flow's banners: why this Review is read-only or degraded, and the one action that changes it
 * (reviewStateLineModel.ts decides which). The text is static (A11Y-3); only a failure of the line's own action is an alert.
 */
export function ReviewStateLine(props: ReviewStateLineProps) {
  const { reviewId, reviewRef, forcedReadOnly, busy } = props;
  const [running, setRunning] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const approvalsReadable = !props.historical && !props.accessLost && props.status !== "archived";
  const approvals = usePendingAgentApprovals(reviewId, Boolean(reviewId) && approvalsReadable, props.approvals);

  const run = (label: string, action: () => Promise<unknown>) => {
    if (running || busy) return;
    setRunning(label);
    setFailure(null);
    void action().then(
      (result) => {
        if (typeof result === "string" && result) setFailure({ label, message: result, retry: () => run(label, action) });
      },
      (reason: unknown) => setFailure({
        label,
        message: reason instanceof Error ? reason.message : String(reason),
        retry: () => run(label, action),
      }),
    ).finally(() => setRunning(""));
  };

  const signal = stateLine({ ...props, failure, pendingApprovals: approvals.pending });
  const disabled = Boolean(running) || busy;
  const writable = !forcedReadOnly && !props.accessLost;
  let actions = null;
  switch (signal?.id) {
    case "failed":
      actions = (
        <>
          <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => failure?.retry()}>Try again</Button>
          <Button variant="ghost" size="compact" type="button" onClick={() => setFailure(null)}>Dismiss</Button>
        </>
      );
      break;
    case "access-lost":
      actions = <Button asChild variant="outline" size="compact"><a href="/reviews">All reviews</a></Button>;
      break;
    case "agent-approvals":
      actions = <Button variant="outline" size="compact" type="button" aria-haspopup="dialog" onClick={() => setApprovalsOpen(true)}>Approve or deny</Button>;
      break;
    case "revision-advanced":
      actions = (
        <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => run("open the newer Snapshot", async () => { await props.onOpenNewer(); })}>
          {props.newerRevisionNumber != null && props.newerRevisionNumber > props.revisionNumber ? `Open ${revisionName(props.newerRevisionNumber)}` : "Open the newer Snapshot"}
        </Button>
      );
      break;
    case "running-draft":
      actions = writable && props.status === "open" ? (
        <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => run("make it a Snapshot", () => props.onWrite("snapshot", () => publishReviewSnapshot(reviewId, props.revisionNumber)))}>
          {running === "make it a Snapshot" ? "Making it a Snapshot…" : "Make it a Snapshot"}
        </Button>
      ) : null;
      break;
    case "historical":
      actions = (
        <>
          <Button asChild variant="outline" size="compact"><a href={reviewHref(reviewRef)}>Back to latest</a></Button>
          {props.latestRevisionNumber !== props.revisionNumber ? (
            <Button asChild variant="ghost" size="compact">
              <a href={reviewCompareHref(reviewRef, props.revisionNumber, props.latestRevisionNumber)}>Compare with latest</a>
            </Button>
          ) : null}
        </>
      );
      break;
    case "finished":
    case "archived": {
      const label = signal.id === "finished" ? "reopen this review" : "restore this review";
      actions = writable ? (
        <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => run(label, () => props.onWrite("reopen", () => finishReview(reviewId, "open")))}>
          {signal.id === "finished" ? "Reopen" : "Restore"}
        </Button>
      ) : null;
      break;
    }
    case "context-failures":
      actions = <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => void props.onRetryContext()}>Retry</Button>;
      break;
    case "source-moved":
      actions = (
        <>
          <Button variant="outline" size="compact" type="button" disabled={disabled} onClick={() => run("take a Snapshot", props.onSnapshotNow)}>
            {running === "take a Snapshot" ? "Taking a Snapshot…" : "Snapshot now"}
          </Button>
          <Button variant="ghost" size="compact" type="button" onClick={props.onDismissSource}>Dismiss</Button>
        </>
      );
      break;
    default:
      break;
  }

  return (
    <>
      {signal ? (
        <div
          role={signal.id === "failed" ? "alert" : undefined}
          data-testid="review-state-line"
          data-state-line={signal.id}
          className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1.5 text-label ${TONE_CLASS[signal.tone]}`}
        >
          <p className="min-w-0 flex-1">
            <span className="text-foreground">{signal.text}</span>
            {signal.detail ? <span className="text-muted-foreground">{` · ${signal.detail}`}</span> : null}
          </p>
          {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
        </div>
      ) : null}
      {approvalsOpen ? (
        <Sheet open onOpenChange={(open) => { if (!open) { setApprovalsOpen(false); approvals.reload(); } }}>
          <SheetContent aria-label="Agent requests">
            <SheetHeader>
              <SheetTitle>Agent requests</SheetTitle>
              <SheetClose asChild>
                <Button variant="ghost" size="icon" type="button" aria-label="Close agent requests"><X size={14} /></Button>
              </SheetClose>
            </SheetHeader>
            <SheetLede>
              The agent asked before a risky action and waits. Approve exactly that action once, or deny it; it is denied when its wait runs out.
            </SheetLede>
            <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
              <AgentApprovalPanel reviewId={reviewId} initial={approvals.approvals} readOnly={!writable} />
            </div>
          </SheetContent>
        </Sheet>
      ) : null}
    </>
  );
}
