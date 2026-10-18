import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { CheckCircle2, Copy, GitPullRequest, Send, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { FeedbackDelivery, FeedbackExport, FeedbackRouteKind, FeedbackStatus } from "./types";

interface ReviewFeedbackPanelProps {
  feedback: FeedbackExport;
  delivery: FeedbackDelivery | null;
  /**
   * Set once every comment of this handoff carries the agent's addressed claim
   * (or was resolved): how many still await the reviewer's re-review. The
   * handoff is then over, whatever the delivery record last reported.
   */
  handoffRereview?: number | null;
  busy: boolean;
  /** This Snapshot can still hand feedback off (not historical, archived or superseded). */
  canDirectSend: boolean;
  onCopy(): Promise<void> | void;
  /** Hand the prepared feedback off; `copied` confirms a copy for the author. */
  onSend(options?: { copied?: boolean }): Promise<void> | void;
  /** A pull-request route publishes through the GitHub preview. */
  onPublishGitHub?: () => void;
  /** Omitted when a surrounding sheet owns closing. */
  onClose?: () => void;
}

function feedbackStatus(feedback: FeedbackExport): FeedbackStatus {
  return feedback.status ?? {
    open_total: feedback.open + feedback.orphaned,
    unpublished: feedback.open + feedback.orphaned,
    published: 0,
    in_flight: 0,
    addressed: 0,
  };
}

/** How this prepared feedback leaves: older servers only say whether an agent session is exact. */
export function feedbackRoute(feedback: FeedbackExport): FeedbackRouteKind {
  return feedback.delivery.kind ?? (feedback.delivery.supported ? "agent" : "export");
}

/**
 * Single derived action state for a prepared feedback operation. Send is only
 * offered when no delivery attempt exists or the attempt provably never left
 * (blocked/failed without a remote reference). Anything active, delivered, or
 * in doubt is non-resendable regardless of unpublished-annotation counts.
 */
export function deriveFeedbackAction(delivery: FeedbackDelivery | null): {
  sendBlocked: boolean;
  inDoubt: boolean;
} {
  if (!delivery) return { sendBlocked: false, inDoubt: false };
  const inDoubt =
    delivery.state === "uncertain"
    || delivery.state === "leased"
    || delivery.state === "dispatching"
    || delivery.state === "partial"
    || ((delivery.state === "blocked" || delivery.state === "failed") && Boolean(delivery.remote_ref));
  // Allowlist: an unknown future state is never resendable.
  const provablyUnsent = delivery.state === "prepared"
    || ((delivery.state === "blocked" || delivery.state === "failed") && !delivery.remote_ref);
  return { sendBlocked: !provablyUnsent, inDoubt };
}

function plural(count: number, word = "comment"): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export default function ReviewFeedbackPanel({
  feedback,
  delivery,
  handoffRereview = null,
  busy,
  canDirectSend,
  onCopy,
  onSend,
  onPublishGitHub,
  onClose,
}: ReviewFeedbackPanelProps) {
  const settled = handoffRereview !== null;
  const status = settled
    ? { ...feedbackStatus(feedback), addressed: handoffRereview, in_flight: 0 }
    : feedbackStatus(feedback);
  const [copyState, setCopyState] = useState<"idle" | "copying" | "copied" | "failed">("idle");
  useEffect(() => setCopyState("idle"), [feedback.operation_id]);
  // A settled handoff is no longer queued, working or in doubt. Resending stays
  // governed by the delivery record itself.
  const live = settled ? null : delivery;
  const consumed = live?.state === "consumed";
  const sent = live?.state === "sent" || consumed;
  const queued = live?.state === "queued";
  const working = live?.state === "working";
  const { sendBlocked } = deriveFeedbackAction(delivery);
  const { inDoubt: deliveryNeedsInspection } = deriveFeedbackAction(live);
  const route = feedbackRoute(feedback);
  const toAuthor = route === "export";
  // Only a server that routes copies records them; an older one just renders.
  const recordsCopy = feedback.delivery.kind === "export";
  const toPullRequest = route === "github";
  const unsent = toPullRequest ? (status.github?.unpublished ?? status.unpublished) : status.unpublished;
  const publishable = unsent > 0;
  const reachable = feedback.delivery.supported;
  const destination = toAuthor ? "the author" : feedback.delivery.label || feedback.delivery.host || "coding agent";
  const sentCount = delivery?.annotation_count ?? status.published;
  const lifecycle = status.addressed > 0
    ? "Addressed"
    : sent
      ? (toPullRequest ? "Published" : "Sent")
      : publishable
        ? "Pending feedback"
        : status.open_total === 0
          ? "Resolved"
          : "Pending feedback";
  const fallbackNote = feedback.delivery.fallback && feedback.delivery.reason
    ? ` No exact agent session: ${feedback.delivery.reason}.`
    : "";
  const title = consumed
    ? `Consumed by ${destination}`
    : sent
    ? toAuthor ? "Copied for the author" : `Sent to ${destination}`
    : queued
      ? `Queued to ${destination}`
      : working
        ? `Working in ${destination}`
        : deliveryNeedsInspection
        ? "Delivery needs inspection"
        : publishable
          ? toAuthor
            ? "Copy feedback for the author"
            : toPullRequest
              ? `Publish feedback to ${destination}`
              : reachable ? `Send feedback to ${destination}` : `${destination} is unavailable`
          : status.addressed > 0 ? "Re-review addressed feedback" : status.in_flight > 0 ? "Delivery pending" : "No feedback to send";
  const detail = consumed
    ? `${plural(sentCount)} were consumed by the exact ${destination} coding session. The agent was told to fix, verify, and mark addressed items; you still resolve them after re-review.`
    : sent
    ? toAuthor
      ? `${plural(sentCount)} were copied for the author. The next Snapshot returns this Review to you for re-review.`
      : `${plural(sentCount)} were sent to the exact ${destination} coding session. Consumption was not separately acknowledged; you still resolve them after re-review.`
    : queued
      ? `${plural(sentCount)} queued to the exact ${destination} coding session. LemonCrow will not create a duplicate session; the handoff is waiting for that exact session to consume it.`
      : working
        ? `${plural(sentCount)} are being handled by the exact ${destination} coding session. Consumption is not yet separately acknowledged.`
      : deliveryNeedsInspection
        ? `${delivery?.message || "LemonCrow cannot prove whether the exact coding-agent session received this handoff."} Do not resend this prepared operation until its delivery state is understood.`
        : publishable
          ? toAuthor
            ? recordsCopy
              ? `Nothing is recorded until you copy. Copying ${plural(unsent)} records the handoff, and this Review waits on the author until the next Snapshot.${fallbackNote}`
              : `Nothing is sent automatically. ${plural(unsent)} ready to copy. ${feedback.delivery.reason || "Direct coding-agent delivery is unavailable."}`
            : toPullRequest
              ? `Nothing is posted until you confirm the GitHub preview of ${plural(unsent)}.${fallbackNote}`
              : reachable
                ? route === "new_session"
                  ? `Nothing is sent automatically. Send starts a ${destination} in this worktree with these ${plural(unsent)}.`
                  : `Nothing is sent automatically. Review these ${plural(unsent)}, then click Send to hand them off together to the exact ${destination} coding session.`
                : `${feedback.delivery.reason || "This destination cannot receive feedback."} Change where fixes go, or copy the feedback.`
          : status.addressed > 0
            ? `${plural(status.addressed)} were marked addressed by the ${toAuthor ? "author" : "coding agent"} and need your re-review.`
            : status.in_flight > 0
              ? "This handoff has a delivery attempt that is still active or is not safe to resend automatically."
              : status.published > 0
                ? toAuthor ? "Current feedback was already copied for the author." : "Current feedback has already been sent to the coding agent."
                : "There is no open human feedback to send.";
  const open = publishable && !sent && !queued;
  const copy = (then?: () => Promise<void> | void) => {
    setCopyState("copying");
    void Promise.resolve()
      .then(onCopy)
      .then(() => setCopyState("copied"), (reason: unknown) => {
        setCopyState("failed");
        throw reason;
      })
      .then(then)
      .catch(() => undefined);
  };

  return (
    <section className="review-feedback-panel shrink-0 border-b border-border bg-background" aria-label="Review feedback">
      <span className="sr-only">Feedback · {unsent} unsent · {status.open_total} open</span>
      <div className="review-feedback-toolbar">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className={sent || queued || working || !publishable
            ? "flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-emerald-950/40 text-emerald-300"
            : "flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-amber-950/45 text-amber-300"}
          >
            {sent || queued || working || !publishable ? <CheckCircle2 size={14} /> : <Send size={13} />}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-heading font-semibold text-foreground">
              <span>{title}</span>
              <Badge tone="secondary" data-testid="feedback-lifecycle">{lifecycle}</Badge>
              {!sent && !queued && unsent > 0 && <Badge tone="secondary">{unsent} {toAuthor ? "to copy" : toPullRequest ? "to publish" : "to send"}</Badge>}
              {status.addressed > 0 && <Badge tone="secondary" className="border-sky-900/50 text-sky-300">{status.addressed} re-review</Badge>}
              {feedback.orphaned > 0 && <Badge tone="secondary" className="border-amber-900/50 text-amber-300">{feedback.orphaned} orphaned</Badge>}
            </div>
            <p className="mt-1 max-w-[75ch] text-body leading-5 text-neutral-400" title={detail}>{detail}</p>
          </div>
        </div>
        {open && !recordsCopy && (
          <UiButton
            type="button"
            disabled={busy || copyState === "copying"}
            onClick={() => copy()}
            aria-label="Copy review feedback bundle"
          >
            <Copy size={12} />
            {copyState === "copied" ? "Copied" : copyState === "copying" ? "Copying…" : "Copy feedback"}
          </UiButton>
        )}
        {canDirectSend && open && recordsCopy && !sendBlocked && (
          <UiButton
            variant="default"
            className="px-3"
            type="button"
            disabled={busy || copyState === "copying"}
            onClick={() => copy(() => onSend({ copied: true }))}
          >
            <Copy size={12} />
            {copyState === "copying" ? "Copying…" : `Copy ${unsent} for the author`}
          </UiButton>
        )}
        {canDirectSend && open && toPullRequest && reachable && onPublishGitHub && (
          <UiButton variant="default" className="px-3" type="button" disabled={busy} onClick={onPublishGitHub}>
            <GitPullRequest size={12} />
            Publish {unsent} to {destination}…
          </UiButton>
        )}
        {canDirectSend && open && !toAuthor && !toPullRequest && reachable && !sendBlocked && (
          <UiButton variant="default" className="px-3" type="button" disabled={busy} onClick={() => void onSend()}>
            <Send size={12} />
            Send {unsent} to {destination}
          </UiButton>
        )}
        {onClose && (
          <UiButton variant="ghost" size="icon" type="button" onClick={onClose} aria-label="Close feedback preview">
            <X size={14} />
          </UiButton>
        )}
      </div>

      {open && (
        <div className="flex items-center gap-2 border-t border-neutral-900 px-3 py-1.5 text-caption text-muted-foreground">
          {recordsCopy ? (
            <span>Copy for the author · recorded only once copied</span>
          ) : toPullRequest ? (
            <span>{destination} · posted only after the GitHub preview</span>
          ) : reachable ? (
            <span title={feedback.delivery.target_ref}>
              {route === "new_session" ? `Starts a ${destination}` : `Exact ${destination} coding session`} · manual send after preview
            </span>
          ) : (
            <span>{feedback.delivery.reason || "Direct delivery unavailable"} · copy/export remains available</span>
          )}
          <span className="flex-1" />
        </div>
      )}

      {copyState === "failed" && (
        <div className="flex items-center gap-2 border-t border-amber-900/30 bg-amber-950/15 px-3 py-1.5 text-caption text-amber-300" role="status">
          <span className="min-w-0 flex-1">
            {recordsCopy
              ? "Clipboard copy failed. Copy the preview below yourself, then confirm it reached the author."
              : "Clipboard copy failed. Open the preview below and copy the feedback manually."}
          </span>
          {/* Without clipboard access (a non-secure origin, a refused permission) the
              reviewer's own copy is the handoff; only their confirmation records it. */}
          {canDirectSend && open && recordsCopy && !sendBlocked && (
            <UiButton size="compact" type="button" disabled={busy} onClick={() => void onSend({ copied: true })}>
              I copied it
            </UiButton>
          )}
        </div>
      )}

      {live && !sent && live.state !== "queued" && (
        <div className="border-t border-amber-900/30 bg-amber-950/15 px-3 py-1.5 text-caption text-amber-300">
          Delivery {live.state}: {live.message}
        </div>
      )}

      {open && (
        <details open className="border-t border-neutral-900">
          <summary className="cursor-pointer list-none px-3 py-1.5 text-caption font-medium text-muted-foreground hover:bg-muted/40 hover:text-neutral-50 [&::-webkit-details-marker]:hidden">
            {reachable && !toAuthor ? "Preview exact handoff" : "Preview feedback"}
          </summary>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap border-0 border-t border-neutral-900 bg-background px-4 py-3 text-caption leading-5 text-neutral-400">
            {feedback.markdown}
          </pre>
        </details>
      )}
    </section>
  );
}
