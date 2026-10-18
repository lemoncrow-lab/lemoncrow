import { useCallback, useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";

import { HOSTED_CONFLICT_TEXT, hostedActionSummary, unresolvedHostedActions } from "./hostedQueue";
import { detachHostedAttachment, fetchHostedAttachment, resolveHostedAction } from "./reviewApi";
import type { HostedAttachmentInfo, HostedAttachmentStatus } from "./types";

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/** The attachment's queue, re-read whenever `version` moves (a queued write, a resolution). */
export function useHostedQueue(reviewId: string, attached: boolean, version: number) {
  const [status, setStatus] = useState<HostedAttachmentStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    if (!attached || !reviewId) {
      setStatus(null);
      setError("");
      return () => { live = false; };
    }
    fetchHostedAttachment(reviewId)
      .then((next) => {
        if (!live) return;
        setStatus(next);
        setError("");
      })
      .catch((reason) => {
        if (live) setError(`The hosted queue is unavailable: ${message(reason)}`);
      });
    return () => { live = false; };
  }, [reviewId, attached, version]);
  return { status, error, setStatus };
}

export interface HostedQueuePanelProps {
  reviewId: string;
  hosted: HostedAttachmentInfo;
  status: HostedAttachmentStatus | null;
  error?: string;
  /** Called with the server's status after every resolution. */
  onStatus(status: HostedAttachmentStatus): void;
  /** Called once the attachment is gone; the Reader returns to the local Review. */
  onDetached(localReviewId: string): void;
}

/**
 * What this attached Review has queued for hosted, and the reviewer's explicit
 * resolutions: discard, re-apply a conflict on hosted's current Snapshot, or
 * detach. Hosted decides every conflict; nothing here merges or guesses.
 */
export function HostedQueuePanel({ reviewId, hosted, status, error = "", onStatus, onDetached }: HostedQueuePanelProps) {
  const [busy, setBusy] = useState("");
  const [failure, setFailure] = useState("");
  const [confirmingDetach, setConfirmingDetach] = useState(false);
  const unresolved = unresolvedHostedActions(status);
  const conflicts = unresolved.filter((action) => action.state === "conflict").length;

  const resolve = useCallback(async (actionId: string, op: "discard" | "requeue") => {
    setBusy(actionId);
    setFailure("");
    try {
      onStatus(await resolveHostedAction(reviewId, actionId, op));
    } catch (reason) {
      setFailure(message(reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, onStatus]);

  const detach = useCallback(async () => {
    if (unresolved.length > 0 && !confirmingDetach) {
      setConfirmingDetach(true);
      return;
    }
    setBusy("detach");
    setFailure("");
    try {
      const result = await detachHostedAttachment(reviewId, unresolved.length > 0);
      onDetached(result.review_id || hosted.local_review_id);
    } catch (reason) {
      setFailure(message(reason));
      setConfirmingDetach(false);
    } finally {
      setBusy("");
    }
  }, [reviewId, unresolved.length, confirmingDetach, onDetached, hosted.local_review_id]);

  return (
    <div className="space-y-2 text-label leading-5" data-testid="hosted-queue-panel">
      <p className="text-neutral-300">
        Hosted <a className="text-sky-300 hover:underline" href={hosted.url}>{hosted.reference}</a> owns comments,
        judgments and outcomes. What you do here queues for it, bound to the hosted Snapshot you saw, and applies
        when you run <code>lc review hosted sync</code>.
      </p>
      {error && <p role="alert" className="text-amber-300">{error}</p>}
      {unresolved.length === 0 ? (
        <p className="text-muted-foreground">Nothing is waiting for hosted.</p>
      ) : (
        <ul className="space-y-1.5" aria-label="Queued for hosted">
          {unresolved.map((action) => (
            <li key={action.id} className="rounded-md border border-border/70 px-2 py-1.5" data-state={action.state}>
              <div className="flex items-start gap-2">
                <span className={action.state === "conflict" ? "text-amber-300" : "text-sky-300"}>
                  {action.state === "conflict" ? "Conflict" : "Queued"}
                </span>
                <span className="min-w-0 flex-1 break-words text-neutral-200">{hostedActionSummary(action)}</span>
              </div>
              {action.state === "conflict" && (
                <p className="text-muted-foreground">
                  {HOSTED_CONFLICT_TEXT[action.reason] ?? action.reason} Re-apply it on hosted's current Snapshot, or discard it.
                </p>
              )}
              <div className="mt-1 flex justify-end gap-1.5">
                {action.state === "conflict" && (
                  <UiButton size="compact" disabled={Boolean(busy)} onClick={() => void resolve(action.id, "requeue")}>
                    Re-apply
                  </UiButton>
                )}
                <UiButton
                  size="compact"
                  variant="ghost"
                  disabled={Boolean(busy)}
                  onClick={() => void resolve(action.id, "discard")}
                >
                  Discard
                </UiButton>
              </div>
            </li>
          ))}
        </ul>
      )}
      {failure && <p role="alert" className="text-rose-300">{failure}</p>}
      <div className="flex items-center justify-between gap-2 border-t border-border/60 pt-2">
        <span className="text-muted-foreground">
          {conflicts > 0 ? `${conflicts} conflict${conflicts === 1 ? "" : "s"} need you` : "Detach to work locally again"}
        </span>
        {confirmingDetach ? (
          <span className="flex items-center gap-1.5">
            <UiButton size="compact" variant="ghost" disabled={Boolean(busy)} onClick={() => setConfirmingDetach(false)}>
              Keep attached
            </UiButton>
            <UiButton size="compact" variant="destructive" disabled={Boolean(busy)} onClick={() => void detach()}>
              {`Detach and discard ${unresolved.length}`}
            </UiButton>
          </span>
        ) : (
          <UiButton size="compact" variant="warning" disabled={Boolean(busy)} onClick={() => void detach()}>
            Detach
          </UiButton>
        )}
      </div>
      {confirmingDetach && (
        <p role="alert" className="text-amber-300">
          {`${unresolved.length} unresolved action${unresolved.length === 1 ? " has" : "s have"} not reached hosted. Detaching discards ${unresolved.length === 1 ? "it" : "them"}; everything hosted already applied stays on hosted.`}
        </p>
      )}
    </div>
  );
}
