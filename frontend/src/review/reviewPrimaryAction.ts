import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { ReviewGuideTarget } from "./readerModel";
import type {
  FeedbackDeliveryCapability,
  FeedbackStatus,
  ReviewProgress,
  ReviewSessionStatus,
} from "./types";

export type ReviewPrimaryActionKind =
  | "go_latest"
  | "reopen"
  | "restore"
  | "checking_comments"
  | "reload_state"
  | "send_feedback"
  | "publish_github"
  | "rereview_comments"
  | "delivery_pending"
  | "waiting_agent"
  | "review_comments"
  | "review_changed"
  | "review_target"
  | "review_requested_changes"
  | "review_readiness"
  | "finish";

export interface ReviewPrimaryAction {
  kind: ReviewPrimaryActionKind;
  label: string;
  detail: string;
  disabled: boolean;
  targetId?: string;
}

function destinationLabel(delivery: FeedbackDeliveryCapability | null | undefined): string {
  return delivery?.label || delivery?.host || (delivery?.supported ? "coding agent" : "author");
}

export function deriveReviewPrimaryAction({
  historical,
  sessionStatus,
  revisionAdvanced,
  commentsState,
  feedbackStatus,
  feedbackDelivery,
  progress,
  readiness,
  landing,
  nextTarget,
}: {
  historical: boolean;
  sessionStatus: ReviewSessionStatus;
  revisionAdvanced: boolean;
  sourceChanged: boolean;
  commentsState: "loading" | "ready" | "failed";
  feedbackStatus: FeedbackStatus;
  feedbackDelivery?: FeedbackDeliveryCapability | null;
  progress: ReviewProgress;
  readiness: ReviewReadinessModel;
  landing?: {
    armable: boolean;
    landing: { supported: boolean; armed: boolean; provider: string; state: string };
  } | null;
  nextTarget?: ReviewGuideTarget | null;
}): ReviewPrimaryAction {
  if (historical) {
    return {
      kind: "go_latest",
      label: "Back to latest",
      detail: "Historical Snapshots are read-only. Return to the current review state.",
      disabled: false,
    };
  }
  if (sessionStatus === "finished") {
    return {
      kind: "reopen",
      label: "Reopen review",
      detail: "Reopen this finished review before making new judgments.",
      disabled: false,
    };
  }
  if (sessionStatus === "archived") {
    return {
      kind: "restore",
      label: "Restore review",
      detail: "Restore this archived review before making changes.",
      disabled: false,
    };
  }
  if (revisionAdvanced) {
    return {
      kind: "reload_state",
      label: "Reload review",
      detail: "The review Snapshot advanced, but dependent reader state did not finish loading. Reload before judging the new Snapshot.",
      disabled: false,
    };
  }
  if (commentsState === "loading") {
    return {
      kind: "checking_comments",
      label: "Checking comments…",
      detail: "Existing review discussions are still loading; readiness cannot assume there are none.",
      disabled: true,
    };
  }
  if (commentsState === "failed") {
    return {
      kind: "reload_state",
      label: "Retry review state",
      detail: "Review discussions could not be loaded. Reload before deciding the review is complete.",
      disabled: false,
    };
  }

  const destination = destinationLabel(feedbackDelivery);
  // A pull request Review without an exact agent session hands feedback to the
  // PR author on GitHub; the agent handoff states below would never reach them.
  const github = feedbackDelivery?.supported ? undefined : feedbackStatus.github;
  if (github) {
    if (github.in_flight > 0) {
      return {
        kind: "delivery_pending",
        label: "Publish pending",
        detail: "A GitHub publish is in progress or its result is unknown; LemonCrow checks the pull request before posting again.",
        disabled: true,
      };
    }
    if (github.unpublished > 0) {
      return {
        kind: "publish_github",
        label: `Publish ${github.unpublished} to GitHub…`,
        detail: "Nothing is posted until you confirm the preview.",
        disabled: false,
      };
    }
  }
  if (!github && feedbackStatus.unpublished > 0) {
    return {
      kind: "send_feedback",
      label: feedbackDelivery?.supported
        ? `Send ${feedbackStatus.unpublished} to ${destination}…`
        : `Export ${feedbackStatus.unpublished} comment${feedbackStatus.unpublished === 1 ? "" : "s"}…`,
      detail: feedbackDelivery?.supported
        ? `Nothing is sent automatically. Review the bundle, then explicitly send all unsent comments to the exact ${destination} coding session.`
        : "Direct delivery is unavailable. Preview the unsent comments, then copy/export them for the author.",
      disabled: false,
    };
  }
  if (feedbackStatus.addressed > 0) {
    return {
      kind: "rereview_comments",
      label: `Re-review ${feedbackStatus.addressed}`,
      detail: "The coding agent marked these comments addressed; only your re-review can resolve them.",
      disabled: false,
    };
  }
  if (!github && feedbackStatus.in_flight > 0) {
    return {
      kind: "delivery_pending",
      label: "Delivery pending",
      detail: "This handoff is active or its final delivery state is not safe to retry automatically.",
      disabled: true,
    };
  }
  if (!github && feedbackStatus.published > 0) {
    return {
      kind: "waiting_agent",
      label: `Waiting on ${destination}`,
      detail: `Feedback is with ${destination}; wait for addressed items to return for re-review.`,
      disabled: true,
    };
  }
  if (!github && feedbackStatus.open_total > 0) {
    return {
      kind: "review_comments",
      label: "Review comments",
      detail: "Open human review discussions still need attention.",
      disabled: false,
    };
  }

  if (progress.changed_since_review > 0) {
    const count = progress.changed_since_review;
    return {
      kind: "review_changed",
      label: count === 1 ? "Re-review changed area" : `Re-review ${count} changed areas`,
      detail: nextTarget?.reason || "Content you already reviewed changed and needs another human judgment.",
      disabled: !nextTarget,
      targetId: nextTarget?.targetId,
    };
  }

  const unjudged = progress.unreviewed + progress.unknown;
  if (unjudged > 0) {
    return {
      kind: "review_target",
      label: "Review next area",
      detail: nextTarget?.reason || `${unjudged} review area${unjudged === 1 ? "" : "s"} still need human judgment.`,
      disabled: !nextTarget,
      targetId: nextTarget?.targetId,
    };
  }

  if (progress.needs_changes > 0) {
    return {
      kind: "review_requested_changes",
      label: progress.needs_changes === 1 ? "Review requested changes" : `Review ${progress.needs_changes} requested changes`,
      detail: nextTarget?.reason || "Review areas still carry a needs-changes judgment.",
      disabled: !nextTarget,
      targetId: nextTarget?.targetId,
    };
  }

  if (landing?.landing.supported && landing.landing.state === "merged") {
    return {
      kind: "review_readiness",
      label: "Merged",
      detail: `${landing.landing.provider || "The connected SCM"} merged the reviewed Snapshot.`,
      disabled: false,
    };
  }

  if (landing?.landing.supported && landing.armable) {
    const provider = landing.landing.provider || "connected SCM";
    if (landing.landing.armed) {
      return {
        kind: "review_readiness",
        label: "Merge when ready armed",
        detail: `Landing is delegated to ${provider}. Open readiness to inspect or cancel it.`,
        disabled: false,
      };
    }
    return {
      kind: "review_readiness",
      label: "Arm Merge when ready…",
      detail: `Inspect the exact landing conditions, then explicitly delegate landing to ${provider}.`,
      disabled: false,
    };
  }

  if (!readiness.ready) {
    if (readiness.failedChecks > 0) {
      return {
        kind: "review_readiness",
        label: readiness.failedChecks === 1 ? "Review failed check" : `Review ${readiness.failedChecks} failed checks`,
        detail: "Current verification explicitly reports failure.",
        disabled: false,
      };
    }
    if (readiness.unresolvedChecks > 0) {
      return {
        kind: "review_readiness",
        label: "Review verification",
        detail: "Verification is not run, unknown, stale, or otherwise unresolved.",
        disabled: false,
      };
    }
    return {
      kind: "review_readiness",
      label: "Review blockers",
      detail: readiness.detail,
      disabled: false,
    };
  }

  return {
    kind: "finish",
    label: "Record review outcome",
    detail: "Human judgments, discussions, and required verification have no current blockers.",
    disabled: false,
  };
}
