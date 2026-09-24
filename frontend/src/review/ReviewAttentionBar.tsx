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

type AttentionState = {
  label: string;
  detail: string;
  tone: "neutral" | "amber" | "sky" | "emerald";
  icon: typeof Eye;
};

function attentionState({
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
      detail: `Revision ${revisionNumber} is read-only. Review state is replayed from history.`,
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

  const destination = feedbackDelivery?.label || feedbackDelivery?.host || (feedbackDelivery?.supported ? "coding agent" : "author");
  const responseActor = feedbackDelivery?.supported ? "The coding agent" : "The author";
  const addressed = Math.max(addressedNeedsRereview, feedbackStatus.addressed);
  if (addressed > 0 || progress.changed_since_review > 0) {
    const parts = [];
    if (addressed > 0) parts.push(`${addressed} addressed comment${addressed === 1 ? "" : "s"}`);
    if (progress.changed_since_review > 0) {
      parts.push(`${progress.changed_since_review} changed target${progress.changed_since_review === 1 ? "" : "s"}`);
    }
    return {
      label: "Re-review",
      detail: `${parts.join(" · ")} need another look. Unchanged judgments stay preserved.`,
      tone: "sky",
      icon: RefreshCw,
    };
  }

  if (feedbackStatus.in_flight > 0 && delivery?.state !== "sent") {
    return {
      label: "Delivery pending",
      detail: `${feedbackStatus.in_flight} comment${feedbackStatus.in_flight === 1 ? "" : "s"} have an active or ambiguous delivery attempt. LemonCrow will not resend them automatically until that state is safe.`,
      tone: "sky",
      icon: Send,
    };
  }

  if (delivery?.state === "sent" || (feedbackStatus.published > 0 && feedbackStatus.unpublished === 0)) {
    const handedOff = delivery?.state === "sent"
      ? delivery.annotation_count
      : feedbackStatus.published;
    return {
      label: `Waiting on ${destination}`,
      detail: `${handedOff} comment${handedOff === 1 ? "" : "s"} sent to ${destination}. ${responseActor} will mark addressed items for your re-review; sent does not mean fixed.`,
      tone: "sky",
      icon: Send,
    };
  }

  if (feedbackStatus.unpublished > 0) {
    const remaining = progress.unreviewed + progress.unknown;
    return {
      label: remaining > 0 ? "Review in progress" : "Feedback ready",
      detail: remaining > 0
        ? `${remaining} target${remaining === 1 ? "" : "s"} remain · ${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to send to ${destination}.`
        : `${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"} ready to send to ${destination} as one correction round.`,
      tone: "amber",
      icon: MessageSquareText,
    };
  }

  const remaining = progress.unreviewed + progress.unknown + progress.changed_since_review;
  if (remaining > 0) {
    return {
      label: "Review in progress",
      detail: `${remaining} target${remaining === 1 ? "" : "s"} remain.`,
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
  neutral: "border-neutral-800 bg-neutral-900/45 text-neutral-300",
  amber: "border-amber-900/55 bg-amber-950/20 text-amber-200",
  sky: "border-sky-900/55 bg-sky-950/20 text-sky-200",
  emerald: "border-emerald-900/55 bg-emerald-950/15 text-emerald-200",
};

export default function ReviewAttentionBar(props: ReviewAttentionBarProps) {
  const state = attentionState(props);
  const Icon = state.icon;

  return (
    <div
      className={`review-attention-bar flex min-h-9 shrink-0 items-center gap-2 border-b px-3 ${BAR_TONE_CLASSES[state.tone]}`}
      role="status"
      aria-live="polite"
    >
      <Icon size={12} strokeWidth={1.9} aria-hidden="true" className="shrink-0 opacity-85" />
      <span className="shrink-0 text-[12px] font-semibold">{state.label}</span>
      <span className="hidden min-w-0 flex-1 truncate text-[12px] md:block" title={state.detail}>
        {state.detail}
      </span>
      <span className="flex-1" />
      <button
        type="button"
        onClick={props.onShowReadiness}
        aria-label={`Review readiness: ${props.readiness.label}`}
        title={props.readiness.detail}
        className={`review-compact-action h-6 ${props.readiness.ready ? "text-emerald-300 hover:text-emerald-200" : props.readiness.failedChecks > 0 || props.readiness.outcome?.outcome === "changes_requested" ? "text-rose-300 hover:text-rose-200" : "text-amber-300 hover:text-amber-200"}`}
      >
        <ShieldCheck size={11} />
        <span>{props.readiness.ready ? "Ready" : `${props.readiness.blockers.length} blocker${props.readiness.blockers.length === 1 ? "" : "s"}`}</span>
      </button>
      {props.openCommentCount > 0 && (
        <button
          type="button"
          onClick={props.onShowComments}
          className="review-compact-action h-6"
          aria-label={`Open ${props.openCommentCount} review comments`}
        >
          <MessageSquareText size={11} />
          <span className="font-mono text-[10px]">{props.openCommentCount}</span>
        </button>
      )}
    </div>
  );
}
