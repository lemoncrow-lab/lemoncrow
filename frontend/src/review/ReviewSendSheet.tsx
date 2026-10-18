import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { X } from "lucide-react";

import ReviewFeedbackPanel from "./ReviewFeedbackPanel";
import { destinationChipLabel, ReviewDestinationPicker } from "./ReviewDestinationPicker";
import type { FeedbackDelivery, FeedbackExport, ReviewDestinationState } from "./types";

/**
 * The one place to preview and send a Review's feedback mid-review: where it
 * goes (with change…), the exact bundle, and the delivery state afterwards.
 */
export default function ReviewSendSheet({
  reviewId,
  feedback,
  delivery,
  handoffRereview,
  busy,
  canSend,
  onCopy,
  onSend,
  onPublishGitHub,
  onDestinationChanged,
  onReviewOutcome,
  onClose,
}: {
  reviewId: string;
  feedback: FeedbackExport;
  delivery: FeedbackDelivery | null;
  handoffRereview: number | null;
  busy: boolean;
  canSend: boolean;
  onCopy(): Promise<void>;
  onSend(options?: { copied?: boolean }): Promise<void> | void;
  onPublishGitHub?: () => void;
  onDestinationChanged(state: ReviewDestinationState): void;
  onReviewOutcome?(): void;
  onClose(): void;
}) {
  const label = destinationChipLabel(feedback.delivery);
  const handedOff = Boolean(delivery && ["sent", "consumed", "queued", "working"].includes(delivery.state));
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-[min(640px,100%)]">
        <SheetHeader>
          <div className="min-w-0 flex-1">
            <SheetTitle>Send feedback</SheetTitle>
            <SheetDescription>
              Nothing leaves without your click. Sending does not record a verdict or finish the Review.
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close send feedback">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>
        <div className="flex items-center gap-2 border-b border-neutral-900 px-4 py-2 text-caption">
          <span className="text-muted-foreground">To:</span>
          <span className="min-w-0 truncate text-neutral-200" title={feedback.delivery.target_ref}>{label}</span>
          {feedback.delivery.fallback && (
            <span className="truncate text-amber-300/80" title={feedback.delivery.reason}>· no exact author session</span>
          )}
          <span className="flex-1" />
          {canSend && !handedOff && (
            <ReviewDestinationPicker reviewId={reviewId} disabled={busy} onChanged={onDestinationChanged}>
              <UiButton variant="ghost" size="compact" type="button">change…</UiButton>
            </ReviewDestinationPicker>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          <ReviewFeedbackPanel
            feedback={feedback}
            delivery={delivery}
            handoffRereview={handoffRereview}
            busy={busy}
            canDirectSend={canSend}
            onCopy={onCopy}
            onSend={onSend}
            onPublishGitHub={onPublishGitHub}
          />
        </div>
        {canSend && onReviewOutcome && (
          <div className="flex justify-end border-t border-border px-4 py-3">
            <UiButton type="button" onClick={onReviewOutcome}>Finish review…</UiButton>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}