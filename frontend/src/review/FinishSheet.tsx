import { useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { ToggleGroup } from "../components/ui/toggle-group";
import type {
  DegradedNote,
  FeedbackRouteKind,
  ReviewClosure,
  ReviewOutcome,
  ReviewOutcomeKind,
} from "./types";

import "./reviewUi.css";

/** Where pending feedback goes if Finish also delivers it. */
export interface FinishHandoff {
  unsent: number;
  label: string;
  kind: FeedbackRouteKind;
  available: boolean;
  reason: string;
}

export interface FinishOptions {
  /** Deliver pending feedback before the outcome/finish write. */
  send: boolean;
}

/** Phase 8 contract: Finish is a decision surface; Review Status owns diagnostic detail. */
interface FinishSheetProps {
  reviewId?: string;
  summary: ReviewClosure;
  /** Kept for call-site compatibility; detailed limitations belong in Review Status. */
  degraded?: DegradedNote[];
  /** Kept for call-site compatibility; detailed evidence belongs in Review Status. */
  evidenceFacts?: readonly unknown[];
  busy: boolean;
  currentOutcome?: ReviewOutcome | null;
  handoff?: FinishHandoff | null;
  destinationControl?: ReactNode;
  sendError?: string;
  finishError?: string;
  onClose(): void;
  onFinish(outcome: ReviewOutcomeKind, summary: string, options: FinishOptions): void;
  /** Open the authoritative status/readiness projection without duplicating it here. */
  onOpenStatus?(): void;
  onCloseAutoFocus?: (event: Event) => void;
}

const OUTCOMES: { id: ReviewOutcomeKind; label: string; hint: string }[] = [
  { id: "comment", label: "Comment", hint: "Finish without an overall approval verdict." },
  { id: "lgtm", label: "LGTM", hint: "This Snapshot looks good to you." },
  { id: "changes_requested", label: "Request changes", hint: "Record requested changes and keep the Review open for re-review." },
];

function plural(count: number): string {
  return `${count} comment${count === 1 ? "" : "s"}`;
}

function requestChangesLabel(handoff: FinishHandoff | null): string {
  if (handoff?.kind === "github") {
    return handoff.unsent > 0 ? `Request changes & publish ${handoff.unsent}…` : "Request changes & publish…";
  }
  if (!handoff || handoff.unsent === 0) return "Request changes";
  return handoff.kind === "export"
    ? `Request changes & copy ${handoff.unsent}`
    : `Request changes & send ${handoff.unsent}`;
}

function deliveryVerb(handoff: FinishHandoff): string {
  if (handoff.kind === "export") return "copy";
  if (handoff.kind === "github") return "publish";
  return "send";
}

export default function FinishSheet({
  summary,
  busy,
  currentOutcome = null,
  handoff = null,
  destinationControl,
  sendError = "",
  finishError = "",
  onClose,
  onFinish,
  onOpenStatus,
  onCloseAutoFocus,
}: FinishSheetProps) {
  const current = currentOutcome && !currentOutcome.stale ? currentOutcome : null;
  const [outcome, setOutcome] = useState<ReviewOutcomeKind>(current?.outcome ?? "comment");
  const [outcomeSummary, setOutcomeSummary] = useState(current?.summary ?? "");
  const [alsoSend, setAlsoSend] = useState(true);
  const requestChanges = outcome === "changes_requested";
  const unsent = handoff?.unsent ?? 0;
  const reachable = Boolean(handoff?.available);
  const toPullRequest = handoff?.kind === "github";
  const offerSend = Boolean(handoff) && reachable && (unsent > 0 || toPullRequest);
  const sending = offerSend && (requestChanges || alsoSend);

  const outstandingTargets =
    summary.unreviewed_targets
    + summary.changed_since_review
    + summary.needs_changes
    + summary.unknown_targets;
  const outstandingSignals =
    summary.open_comments
    + summary.orphaned_comments
    + summary.failed_verification
    + summary.unresolved_verification;
  const blockers = outstandingTargets + outstandingSignals;
  const complete = blockers === 0;
  const blockedRequest = requestChanges && (
    (unsent > 0 && !reachable)
    || (unsent === 0 && summary.open_comments === 0 && !outcomeSummary.trim() && handoff?.kind !== "github")
  );
  const finishLabel = requestChanges
    ? requestChangesLabel(handoff)
    : outcome === "lgtm"
      ? complete ? "LGTM & finish" : "LGTM & finish anyway"
      : complete ? "Finish" : "Finish anyway";

  // Keep the explicit choice stable when an older verdict becomes stale.
  useEffect(() => {
    if (currentOutcome?.stale) {
      setOutcome("comment");
      setOutcomeSummary("");
    }
  }, [currentOutcome?.stale]);

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent onCloseAutoFocus={onCloseAutoFocus}>
        <SheetHeader>
          <div className="min-w-0 flex-1">
            <SheetTitle>Finish review</SheetTitle>
            <SheetDescription>
              Record your verdict for this Snapshot. Sending pending feedback is a separate explicit choice.
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close review outcome">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
          <section>
            <div className="review-kicker mb-2">Your verdict</div>
            {currentOutcome?.stale && (
              <div className="mb-2 text-caption leading-4 text-amber-400/80">
                Your previous {currentOutcome.outcome === "lgtm" ? "LGTM" : currentOutcome.outcome.replaceAll("_", " ")} applies to an older Snapshot. Choose a verdict for this Snapshot.
              </div>
            )}
            <ToggleGroup
              variant="segment"
              aria-label="Overall review outcome"
              className="grid grid-cols-3"
              itemClassName="justify-center text-caption"
              options={OUTCOMES.map((item) => ({ value: item.id, label: item.label, title: item.hint }))}
              value={outcome}
              onChange={(value) => setOutcome(value as ReviewOutcomeKind)}
            />
            <UiInput
              uiSize="xs"
              type="text"
              value={outcomeSummary}
              onChange={(event) => setOutcomeSummary(event.target.value)}
              aria-label="Review outcome summary"
              placeholder={requestChanges ? "What still needs to change? (optional)" : "Review summary (optional)"}
              className="mt-2"
              maxLength={16_384}
            />
            {requestChanges && (
              <p className="mt-2 text-caption leading-4 text-muted-foreground">
                The Review stays open; the next Snapshot returns here for re-review.
              </p>
            )}
          </section>

          <section className="mt-4 rounded-md border border-border/80 px-3 py-2" aria-label="Review status summary">
            <div className="flex items-center gap-2 text-caption">
              <span className={complete ? "text-emerald-300" : "text-amber-300"}>
                {summary.reviewed_targets}/{summary.target_count} reviewed
                {complete ? " · no current blockers" : ` · ${blockers} item${blockers === 1 ? "" : "s"} need attention`}
              </span>
              <span className="flex-1" />
              {onOpenStatus && (
                <UiButton variant="ghost" size="compact" type="button" onClick={onOpenStatus}>
                  Review Status
                </UiButton>
              )}
            </div>
            {!onOpenStatus && !complete && (
              <p className="mt-1 text-caption text-muted-foreground">Review Status has the detailed blockers, checks, evidence, comments, and merge status.</p>
            )}
          </section>

          {handoff && (unsent > 0 || requestChanges || toPullRequest) && (
            <section className="mt-4 rounded-md border border-border/80 px-3 py-2" aria-label="Where fixes go">
              <div className="flex items-center gap-2 text-caption">
                <span className="text-muted-foreground">Pending feedback</span>
                <span className="font-mono text-neutral-200">{plural(unsent)}</span>
                <span aria-hidden="true">→</span>
                <span className="min-w-0 truncate text-neutral-200">{handoff.label}</span>
                <span className="flex-1" />
                {destinationControl}
              </div>
              {requestChanges ? (
                unsent > 0 ? (
                  <p className={reachable ? "mt-1 text-caption text-muted-foreground" : "mt-1 text-caption text-rose-300"}>
                    {reachable
                      ? `Finish will ${deliveryVerb(handoff)} this pending feedback explicitly.`
                      : handoff.reason || "This destination cannot receive feedback."}
                  </p>
                ) : (
                  <p className="mt-1 text-caption text-muted-foreground">
                    {outcomeSummary.trim()
                      ? "No pending comment bundle; the summary is recorded on the Review and goes with the next comments you send."
                      : "Add a comment or a summary for the fixer."}
                  </p>
                )
              ) : offerSend && (
                <label className="mt-2 flex items-center gap-2 text-caption text-neutral-300">
                  <UiInput
                    unstyled
                    type="checkbox"
                    checked={alsoSend}
                    onChange={(event) => setAlsoSend(event.target.checked)}
                  />
                  Also {deliveryVerb(handoff)} {unsent} to {handoff.label}
                </label>
              )}
            </section>
          )}

          {sendError && (
            <div role="alert" className="mt-4 rounded-md border border-rose-900/50 bg-rose-950/20 px-3 py-2 text-caption text-rose-300">
              Nothing was finished; the comments were not sent: {sendError}
            </div>
          )}
          {finishError && (
            <div role="alert" className="mt-4 rounded-md border border-rose-900/50 bg-rose-950/20 px-3 py-2 text-caption text-rose-300">
              {finishError}
            </div>
          )}
        </div>

        <div className="border-t border-border/80 bg-background/95 px-4 py-3">
          {sendError && (
            <div className="mb-2 flex flex-wrap justify-end gap-2">
              <UiButton
                variant="outline"
                type="button"
                disabled={busy}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: false })}
              >
                Finish without sending
              </UiButton>
              <UiButton
                type="button"
                disabled={busy}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: true })}
              >
                Retry
              </UiButton>
            </div>
          )}
          {!sendError && (
            <div className="flex justify-end gap-2">
              <UiButton variant="ghost" type="button" disabled={busy} onClick={onClose}>
                Continue reviewing
              </UiButton>
              <UiButton
                variant="default"
                type="button"
                disabled={busy || blockedRequest}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: sending })}
              >
                {busy ? "Recording…" : finishLabel}
              </UiButton>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
