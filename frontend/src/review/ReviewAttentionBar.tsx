import { Button as UiButton } from "../components/ui/button";
import {
  CheckCircle2,
  Clock3,
  Eye,
  MessageSquareText,
  RefreshCw,
  Send,
  ShieldCheck,
} from "lucide-react";

import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import { handoffDestinationLabel, handoffToCodingAgent, handoffToPullRequest } from "./reviewPrimaryAction";
import type { FeedbackDelivery, FeedbackDeliveryCapability, FeedbackStatus, ReviewProgress } from "./types";

interface ReviewAttentionBarProps {
  revisionNumber: number;
  progress: ReviewProgress;
  openCommentCount: number;
  commentsState?: "loading" | "ready" | "failed";
  feedbackStatus: FeedbackStatus;
  feedbackDelivery?: FeedbackDeliveryCapability | null;
  addressedNeedsRereview: number;
  historical: boolean;
  delivery: FeedbackDelivery | null;
  readiness: ReviewReadinessModel;
  onShowComments(): void;
  onShowReadiness(): void;
}

export type AttentionState = {
  label: string;
  detail: string;
  tone: "neutral" | "amber" | "sky" | "emerald";
  icon: typeof Eye;
};

export function attentionState({
  revisionNumber,
  progress,
  commentsState,
  feedbackStatus,
  feedbackDelivery,
  addressedNeedsRereview,
  historical,
  delivery,
  readiness,
}: Pick<
  ReviewAttentionBarProps,
  "revisionNumber" | "progress" | "openCommentCount" | "commentsState" | "feedbackStatus" | "feedbackDelivery" | "addressedNeedsRereview" | "historical" | "delivery" | "readiness"
>): AttentionState {
  if (historical) {
    return {
      label: "Historical",
      detail: `Snapshot ${revisionNumber} is read-only. Review state is replayed from history.`,
      tone: "neutral",
      icon: Clock3,
    };
  }

  if (commentsState && commentsState !== "ready") {
    return {
      label: commentsState === "loading" ? "Loading review state" : "Discussion unavailable",
      detail: commentsState === "loading"
        ? "Checking existing review discussions before readiness is calculated."
        : "Existing review discussions could not be loaded. The source diff remains available.",
      tone: commentsState === "loading" ? "neutral" : "amber",
      icon: MessageSquareText,
    };
  }

  const destination = handoffDestinationLabel(feedbackDelivery);
  const toAgent = handoffToCodingAgent(feedbackDelivery);
  const responseActor = toAgent ? "The coding agent" : "The author";
  const handedTo = feedbackDelivery?.kind === "export" ? "copied for the author" : `sent to ${destination}`;
  const addressed = Math.max(addressedNeedsRereview, feedbackStatus.addressed);
  if (addressed > 0 || progress.changed_since_review > 0) {
    const parts = [];
    if (addressed > 0) parts.push(`${addressed} addressed comment${addressed === 1 ? "" : "s"}`);
    if (progress.changed_since_review > 0) {
      parts.push(`${progress.changed_since_review} changed area${progress.changed_since_review === 1 ? "" : "s"}`);
    }
    const attentionCount = addressed + progress.changed_since_review;
    const preserved = progress.changed_since_review > 0
      ? ` ${progress.reviewed} judgment${progress.reviewed === 1 ? "" : "s"} preserved.`
      : " Unchanged judgments stay preserved.";
    return {
      label: "Re-review",
      detail: `${parts.join(" · ")} ${attentionCount === 1 ? "needs" : "need"} another look.${preserved}`,
      tone: "sky",
      icon: RefreshCw,
    };
  }

  // A pull request Review without an exact agent session answers to the PR
  // author on GitHub, so its feedback state is what is (not) on the PR yet.
  const github = handoffToPullRequest(feedbackDelivery, feedbackStatus) ? feedbackStatus.github : undefined;
  if (github) {
    const remaining = progress.unreviewed + progress.unknown;
    if (github.in_flight > 0) {
      return {
        label: "Publish pending",
        detail: `${github.in_flight} comment${github.in_flight === 1 ? "" : "s"} have a GitHub publish in progress or with an unknown result. LemonCrow checks the pull request before posting them again.`,
        tone: "sky",
        icon: Send,
      };
    }
    if (github.unpublished > 0) {
      const comments = `${github.unpublished} comment${github.unpublished === 1 ? "" : "s"}`;
      return {
        label: remaining > 0 ? "Review in progress" : "Ready to publish",
        detail: remaining > 0
          ? `${remaining} area${remaining === 1 ? "" : "s"} left · ${comments} not on GitHub yet.`
          : `${comments} ready to publish to the pull request.`,
        tone: "amber",
        icon: MessageSquareText,
      };
    }
    if (github.published > 0 && remaining === 0) {
      return {
        label: "Published to GitHub",
        detail: `${github.published} comment${github.published === 1 ? "" : "s"} on the pull request. When the author pushes, rerun lc review --pr to see what changed.`,
        tone: "sky",
        icon: Send,
      };
    }
  }

  if (!github && feedbackStatus.in_flight > 0 && delivery?.state !== "sent") {
    return {
      label: "Delivery pending",
      detail: `${feedbackStatus.in_flight} comment${feedbackStatus.in_flight === 1 ? "" : "s"} have an active or ambiguous delivery attempt. LemonCrow will not resend them automatically until that state is safe.`,
      tone: "sky",
      icon: Send,
    };
  }

  if (!github && (delivery?.state === "sent" || (feedbackStatus.published > 0 && feedbackStatus.unpublished === 0))) {
    const handedOff = delivery?.state === "sent"
      ? delivery.annotation_count
      : feedbackStatus.published;
    return {
      label: `Waiting on ${destination}`,
      detail: `${handedOff} comment${handedOff === 1 ? "" : "s"} ${handedTo}. ${responseActor} will mark addressed items for your re-review; sent does not mean fixed.`,
      tone: "sky",
      icon: Send,
    };
  }

  if (!github && feedbackStatus.unpublished > 0) {
    const remaining = progress.unreviewed + progress.unknown;
    return {
      label: remaining > 0 ? "Review in progress" : "Feedback ready",
      detail: remaining > 0
        ? toAgent
          ? `${remaining} area${remaining === 1 ? "" : "s"} left · ${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to send to ${destination}.`
          : `${remaining} area${remaining === 1 ? "" : "s"} left · ${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to export.`
        : toAgent
          ? `${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to send to ${destination} as one correction round.`
          : `${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to export.`,
      tone: "amber",
      icon: MessageSquareText,
    };
  }

  const remaining = progress.unreviewed + progress.unknown + progress.changed_since_review;
  if (remaining > 0) {
    return {
      label: "Review in progress",
      detail: `${remaining} area${remaining === 1 ? "" : "s"} left.`,
      tone: "amber",
      icon: Eye,
    };
  }

  if (!readiness.ready) {
    return {
      label: readiness.failedChecks > 0
        ? "Verification failed"
        : readiness.unresolvedChecks > 0
          ? "Verification incomplete"
          : "Needs attention",
      detail: readiness.detail,
      tone: "amber",
      icon: ShieldCheck,
    };
  }

  return {
    label: "Ready to finish",
    detail: "Human judgments, discussions, and required verification have no current blockers.",
    tone: "emerald",
    icon: CheckCircle2,
  };
}

const BAR_TONE_CLASSES: Record<AttentionState["tone"], string> = {
  neutral: "border-border bg-muted/45 text-neutral-300",
  amber: "border-amber-900/55 bg-amber-950/20 text-amber-200",
  sky: "border-sky-900/55 bg-sky-950/20 text-sky-200",
  emerald: "border-emerald-900/55 bg-emerald-950/15 text-emerald-200",
};

/** Readiness blockers beyond ordinary unfinished review coverage. */
export function exceptionalReadinessBlockers(
  readiness: ReviewReadinessModel,
  progress: ReviewProgress,
): ReviewReadinessModel["blockers"] {
  return readiness.blockers.filter((blocker) => {
    if (blocker.id === "unjudged" || blocker.id === "changed") return false;
    // Server-authoritative landing readiness folds ordinary unfinished coverage
    // into one condition. Treat that like normal review work unless the current
    // human judgment explicitly requests changes.
    if (blocker.id === "latest-snapshot-reviewed" && progress.needs_changes === 0) return false;
    if (blocker.id === "landing-readiness" && readiness.label === "Checking merge readiness") return false;
    return true;
  });
}

/** Compact readiness control text and tone, shared by the header and the legacy bar. */
export function readinessControl(
  readiness: ReviewReadinessModel,
  progress: ReviewProgress,
  includeProgress = false,
): { text: string; className: string } {
  const exceptional = exceptionalReadinessBlockers(readiness, progress);
  const needsAttention = exceptional.length > 0;
  const statusText = readiness.ready
    ? "Ready"
    : needsAttention
      ? `${exceptional.length} blocker${exceptional.length === 1 ? "" : "s"}`
      : "Readiness";
  return {
    text: includeProgress ? `${progress.reviewed}/${progress.target_count} reviewed · ${statusText}` : statusText,
    className: readiness.ready
      ? "text-emerald-300 hover:text-emerald-200"
      : readiness.failedChecks > 0 || readiness.outcome?.outcome === "changes_requested"
        ? "text-rose-300 hover:text-rose-200"
        : needsAttention
          ? "text-amber-300 hover:text-amber-200"
          : "text-neutral-400 hover:text-neutral-200",
  };
}

/** Attention labels that restate ordinary progress; the header already carries them. */
export const ROUTINE_ATTENTION_LABELS = new Set(["Review in progress", "Ready to finish", "Feedback ready", "Loading review state"]);

export default function ReviewAttentionBar(props: ReviewAttentionBarProps) {
  const state = attentionState(props);
  const Icon = state.icon;
  const control = readinessControl(props.readiness, props.progress);

  return (
    <div
      className={`review-attention-bar flex min-h-9 shrink-0 items-center gap-2 border-b px-3 ${BAR_TONE_CLASSES[state.tone]}`}
      role="status"
      aria-live="polite"
    >
      <Icon size={12} strokeWidth={1.9} aria-hidden="true" className="shrink-0 opacity-85" />
      <span className="shrink-0 text-body font-semibold">{state.label}</span>
      <span className="hidden min-w-0 flex-1 truncate text-body md:block" title={state.detail}>
        {state.detail}
      </span>
      <span className="flex-1" />
      <UiButton
        variant="ghost"
        size="compact"
        type="button"
        onClick={props.onShowReadiness}
        aria-label={`Review readiness: ${props.readiness.label}`}
        tooltip={props.readiness.detail}
        className={`h-6 ${control.className}`}
      >
        <ShieldCheck size={11} />
        <span>{control.text}</span>
      </UiButton>
      {props.openCommentCount > 0 && (
        <UiButton
          variant="ghost"
          size="compact"
          type="button"
          onClick={props.onShowComments}
          className="h-6"
          aria-label={`Open ${props.openCommentCount} review comments`}
        >
          <MessageSquareText size={11} />
          <span className="font-mono text-caption">{props.openCommentCount}</span>
        </UiButton>
      )}
    </div>
  );
}
