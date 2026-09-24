import { CheckCircle2, Copy, Send, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { FeedbackDelivery, FeedbackExport, FeedbackStatus } from "./types";

interface ReviewFeedbackPanelProps {
  feedback: FeedbackExport;
  delivery: FeedbackDelivery | null;
  busy: boolean;
  canDirectSend: boolean;
  onCopy(): Promise<void> | void;
  onSend(): void;
  onClose(): void;
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

export default function ReviewFeedbackPanel({
  feedback,
  delivery,
  busy,
  canDirectSend,
  onCopy,
  onSend,
  onClose,
}: ReviewFeedbackPanelProps) {
  const status = feedbackStatus(feedback);
  const [copyState, setCopyState] = useState<"idle" | "copying" | "copied" | "failed">("idle");
  useEffect(() => setCopyState("idle"), [feedback.operation_id]);
  const sent = delivery?.state === "sent";
  const queued = delivery?.state === "queued";
  const deliveryNeedsInspection = Boolean(
    delivery
    && (
      delivery.state === "uncertain"
      || (delivery.state === "blocked" && Boolean(delivery.remote_ref))
      || (delivery.state === "failed" && Boolean(delivery.remote_ref))
    )
  );
  const publishable = status.unpublished > 0;
  const destination = feedback.delivery.label || feedback.delivery.host || "coding agent";
  const sentCount = delivery?.annotation_count ?? status.published;
  const title = sent
    ? `Sent to ${destination}`
    : queued
      ? `Queued to ${destination}`
      : deliveryNeedsInspection
        ? "Delivery needs inspection"
        : publishable
          ? feedback.delivery.supported ? `Send feedback to ${destination}` : "Export feedback"
          : status.addressed > 0 ? "Re-review addressed feedback" : status.in_flight > 0 ? "Delivery pending" : "No feedback to send";
  const detail = sent
    ? `${sentCount} comment${sentCount === 1 ? "" : "s"} sent to the exact ${destination} coding session. The agent was told to fix, verify, and mark addressed items; you still resolve them after re-review.`
    : queued
      ? `${sentCount} comment${sentCount === 1 ? "" : "s"} queued to the exact ${destination} coding session. LemonCrow will not create a duplicate session; the handoff is waiting for that exact session to consume it.`
      : deliveryNeedsInspection
        ? `${delivery?.message || "LemonCrow cannot prove whether the exact coding-agent session received this handoff."} Do not resend this prepared operation until its delivery state is understood.`
        : publishable
          ? feedback.delivery.supported
            ? `Nothing is sent automatically. Review these ${status.unpublished} comment${status.unpublished === 1 ? "" : "s"}, then click Send to hand them off together to the exact ${destination} coding session.`
            : `Nothing is sent automatically. ${status.unpublished} comment${status.unpublished === 1 ? "" : "s"} ready to export. ${feedback.delivery.reason || "Direct coding-agent delivery is unavailable."}`
          : status.addressed > 0
            ? `${status.addressed} comment${status.addressed === 1 ? "" : "s"} were marked addressed by the ${feedback.delivery.supported ? "coding agent" : "author"} and need your re-review.`
            : status.in_flight > 0
              ? "This handoff has a delivery attempt that is still active or is not safe to resend automatically."
              : status.published > 0
                ? "Current feedback has already been sent to the coding agent."
                : "There is no open human feedback to send.";

  return (
    <section className="review-feedback-panel shrink-0 border-b border-neutral-800 bg-neutral-950" aria-label="Review feedback">
      <span className="sr-only">Feedback · {status.unpublished} unsent · {status.open_total} open</span>
      <div className="review-feedback-toolbar">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className={sent || queued || !publishable
            ? "flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-emerald-950/40 text-emerald-300"
            : "flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-amber-950/45 text-amber-300"}
          >
            {sent || queued || !publishable ? <CheckCircle2 size={14} /> : <Send size={13} />}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-[13px] font-semibold text-neutral-100">
              <span>{title}</span>
              {!sent && !queued && status.unpublished > 0 && <span className="review-pill">{status.unpublished} to send</span>}
              {status.addressed > 0 && <span className="review-pill border-sky-900/50 text-sky-300">{status.addressed} re-review</span>}
              {feedback.orphaned > 0 && <span className="review-pill border-amber-900/50 text-amber-300">{feedback.orphaned} orphaned</span>}
            </div>
            <p className="mt-1 max-w-[75ch] text-[12px] leading-5 text-neutral-400" title={detail}>{detail}</p>
          </div>
        </div>
        {publishable && !sent && !queued && (
          <button
            type="button"
            disabled={busy || copyState === "copying"}
            onClick={() => {
              setCopyState("copying");
              void Promise.resolve().then(onCopy).then(
                () => setCopyState("copied"),
                () => setCopyState("failed"),
              );
            }}
            className="review-toolbar-button"
            aria-label="Copy review feedback bundle"
          >
            <Copy size={12} />
            {copyState === "copied" ? "Copied" : copyState === "copying" ? "Copying…" : "Copy feedback"}
          </button>
        )}
        {canDirectSend && publishable && !sent && !queued && (
          <button type="button" disabled={busy} onClick={onSend} className="review-toolbar-button-primary">
            <Send size={12} />
            Send {status.unpublished} to {destination}
          </button>
        )}
        <button type="button" onClick={onClose} className="review-icon-button" aria-label="Close feedback preview">
          <X size={14} />
        </button>
      </div>

      {publishable && !sent && !queued && (
        <div className="flex items-center gap-2 border-t border-neutral-900 px-3 py-1.5 text-[10px] text-neutral-500">
          {feedback.delivery.supported ? (
            <span title={feedback.delivery.target_ref}>Exact {destination} coding session · manual send after preview</span>
          ) : (
            <span>{feedback.delivery.reason || "Direct delivery unavailable"} · copy/export remains available</span>
          )}
          <span className="flex-1" />
        </div>
      )}

      {copyState === "failed" && (
        <div className="border-t border-amber-900/30 bg-amber-950/15 px-3 py-1.5 text-[10px] text-amber-300" role="status">
          Clipboard copy failed. Open the preview below and copy the feedback manually.
        </div>
      )}

      {delivery && delivery.state !== "sent" && delivery.state !== "queued" && (
        <div className="border-t border-amber-900/30 bg-amber-950/15 px-3 py-1.5 text-[10px] text-amber-300">
          Delivery {delivery.state}: {delivery.message}
        </div>
      )}

      {publishable && !sent && !queued && (
        <details className="border-t border-neutral-900">
          <summary className="cursor-pointer list-none px-3 py-1.5 text-[10px] font-medium text-neutral-500 hover:bg-neutral-900/40 hover:text-neutral-50 [&::-webkit-details-marker]:hidden">
            Preview exact handoff
          </summary>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap border-0 border-t border-neutral-900 bg-neutral-950 px-4 py-3 text-[10px] leading-5 text-neutral-400">
            {feedback.markdown}
          </pre>
        </details>
      )}
    </section>
  );
}
