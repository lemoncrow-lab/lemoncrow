import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { ReviewGuideTarget } from "./readerModel";
import type {
  FeedbackDeliveryCapability,
  FeedbackStatus,
  ReviewLandingCondition,
  ReviewProgress,
  ReviewSessionStatus,
} from "./types";
import { laterCount } from "./answerModel";
import { partCount } from "./vocabulary";

export type ReviewPrimaryActionKind =
  | "create_snapshot"
  | "go_latest"
  | "reopen"
  | "restore"
  | "checking_comments"
  | "reload_state"
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

/** Where running the primary action takes the reviewer; the Reader dispatches on this. */
export type ReviewPrimaryDestination =
  | "snapshot"
  | "latest_snapshot"
  | "finish_sheet"
  | "reload"
  | "comments"
  | "handoff"
  | "target"
  | "readiness"
  | "none";

const DESTINATION: Record<ReviewPrimaryActionKind, ReviewPrimaryDestination> = {
  create_snapshot: "snapshot",
  go_latest: "latest_snapshot",
  reopen: "finish_sheet",
  restore: "finish_sheet",
  finish: "finish_sheet",
  reload_state: "reload",
  checking_comments: "none",
  rereview_comments: "comments",
  review_comments: "comments",
  delivery_pending: "none",
  waiting_agent: "handoff",
  review_changed: "target",
  review_target: "target",
  review_requested_changes: "target",
  review_readiness: "readiness",
};

export function primaryActionDestination(action: ReviewPrimaryAction): ReviewPrimaryDestination {
  return action.disabled ? "none" : DESTINATION[action.kind];
}

/**
 * Whether "Finish review…" is in the menu: on any open, current review, also
 * while it is the primary action, so the menu always has a way to finish.
 */
export function finishAvailableInMenu({
  sessionStatus,
  historical,
  revisionAdvanced,
  action,
}: {
  sessionStatus: ReviewSessionStatus;
  historical: boolean;
  revisionAdvanced: boolean;
  action: ReviewPrimaryAction;
}): boolean {
  return sessionStatus === "open" && !historical && !revisionAdvanced && action.kind !== "create_snapshot";
}

/**
 * Whether "Reopen review" is in the menu: on a finished, current review whose
 * primary action is something else (Merge when ready, set up or not), so a
 * finished review always has a way back to open.
 */
export function reopenAvailableInMenu({
  sessionStatus,
  historical,
  revisionAdvanced,
  action,
}: {
  sessionStatus: ReviewSessionStatus;
  historical: boolean;
  revisionAdvanced: boolean;
  action: ReviewPrimaryAction;
}): boolean {
  return sessionStatus === "finished" && !historical && !revisionAdvanced && action.kind !== "reopen";
}

/** Who the Review's feedback goes to, in words: "Claude a1b2", "PR #12", "author". */
export function handoffDestinationLabel(delivery: FeedbackDeliveryCapability | null | undefined): string {
  if (delivery?.kind === "export") return "author";
  return delivery?.label || delivery?.host || (delivery?.supported ? "coding agent" : "author");
}

/** The feedback goes to a coding-agent session (exact, pinned or new), not to a person or the PR. */
export function handoffToCodingAgent(delivery: FeedbackDeliveryCapability | null | undefined): boolean {
  return delivery?.kind ? delivery.kind === "agent" || delivery.kind === "new_session" : Boolean(delivery?.supported);
}

/** The feedback goes to the pull request (an older server: a PR Review with no exact agent session). */
export function handoffToPullRequest(
  delivery: FeedbackDeliveryCapability | null | undefined,
  status: FeedbackStatus,
): boolean {
  return delivery?.kind === "github" || (!delivery?.kind && !delivery?.supported && Boolean(status.github));
}

/**
 * The handoff counts for where this Review's feedback actually goes: a pull
 * request route counts what is on the PR, every other route what reached an
 * agent or the author.
 */
export function handoffCounts(
  status: FeedbackStatus,
  delivery: FeedbackDeliveryCapability | null | undefined,
): { unsent: number; published: number; inFlight: number } {
  const github = handoffToPullRequest(delivery, status) ? status.github : undefined;
  if (github) return { unsent: github.unpublished, published: github.published, inFlight: github.in_flight };
  return { unsent: status.unpublished, published: status.published, inFlight: status.in_flight };
}

/**
 * The header's one brand-styled action. Unsent feedback is the next step only
 * while there is some to send; otherwise the review step itself leads and the
 * feedback control steps aside (it stays in the palette and the Send sheet).
 */
export function headerLeadsWithSend({
  unsent,
  locked,
  sessionStatus,
  commentsState,
}: {
  unsent: number;
  locked: boolean;
  sessionStatus: ReviewSessionStatus;
  commentsState: "loading" | "ready" | "failed";
}): boolean {
  return unsent > 0 && !locked && sessionStatus === "open" && commentsState === "ready";
}

/** The Send control's words: "Send 3 comments". */
export function sendCommentsLabel(unsent: number): string {
  return `Send ${unsent} comment${unsent === 1 ? "" : "s"}`;
}

/** A review step for the next area in the queue, or null when no area needs judgment. */
export function targetStep({
  progress,
  nextTarget,
  afterHandoff,
  handoff,
  started,
}: {
  progress: ReviewProgress;
  nextTarget?: ReviewGuideTarget | null;
  afterHandoff?: ReviewGuideTarget | null;
  handoff: { unsent: number; published: number; inFlight: number };
  started: boolean;
}): ReviewPrimaryAction | null {
  const unjudged = progress.unreviewed + progress.unknown + laterCount(progress);
  if (progress.needs_changes + progress.changed_since_review + unjudged === 0) return null;
  // Requested changes that are already with the fixer are waited on, not re-judged:
  // review goes on from the first part that is not one of them.
  const handedOff = handoff.published + handoff.inFlight > 0 && handoff.unsent === 0;
  const head = handedOff && nextTarget?.reasonCode === "request_change_open" ? (afterHandoff ?? null) : (nextTarget ?? null);
  // The head of the canonical queue is where this action goes, so its typed
  // reason picks the label: label, detail and destination cannot disagree.
  // A specific label names a count, so it needs one; otherwise the generic
  // "Review next" is never wrong. Without a head (nothing navigable yet) the
  // counts follow the queue's tiers.
  const code = head
    ? head.reasonCode
    : progress.needs_changes > 0 && !handedOff
      ? "request_change_open"
      : progress.changed_since_review > 0
        ? "changed_since_judgment"
        : "";
  const step = { disabled: !head, targetId: head?.targetId };
  if (code === "request_change_open" && progress.needs_changes > 0) {
    const count = progress.needs_changes;
    return {
      kind: "review_requested_changes",
      label: count === 1 ? "Review requested changes" : `Review ${count} requested changes`,
      detail: head?.reason || "Parts still carry a needs-changes judgment.",
      ...step,
    };
  }
  if (code === "changed_since_judgment" && progress.changed_since_review > 0) {
    const count = progress.changed_since_review;
    return {
      kind: "review_changed",
      label: count === 1 ? "Re-review changed part" : `Re-review ${partCount(count)}`,
      detail: head?.reason || "Content you already reviewed changed and needs another human judgment.",
      ...step,
    };
  }
  if (unjudged + progress.changed_since_review > 0 || !handedOff) {
    // "Start review" only until the reviewer is in the code; from there the step names the part.
    const untouched = !started && progress.reviewed + progress.needs_changes + progress.changed_since_review === 0;
    return {
      kind: "review_target",
      label: untouched ? "Start review" : head?.label ? `Review next: ${head.label}` : "Review next part",
      detail: head?.reason || `${partCount(unjudged)} still need human judgment.`,
      ...step,
    };
  }
  return null;
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
  afterHandoff,
  started = false,
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
    conditions?: ReviewLandingCondition[];
  } | null;
  nextTarget?: ReviewGuideTarget | null;
  /** The first queue part that is not a requested change: where review goes once those are handed off. */
  afterHandoff?: ReviewGuideTarget | null;
  /** The reviewer is already in the code (Code tab or guided tour), so the first step is no longer "Start review". */
  started?: boolean;
}): ReviewPrimaryAction {
  if (historical) {
    return {
      kind: "go_latest",
      label: "Back to latest",
      detail: "Historical revisions are read-only. Return to the latest revision.",
      disabled: false,
    };
  }
  if (sessionStatus === "finished") {
    if (landing?.landing.supported && landing.armable && landing.landing.state !== "merged") {
      const provider = landing.landing.provider || "the connected provider";
      return {
        kind: "review_readiness",
        label: landing.landing.armed ? "Merge when ready enabled" : "Merge when ready",
        detail: landing.landing.armed
          ? `Review finished. ${provider} will merge when its remaining requirements pass.`
          : `Review finished. Enable ${provider} to merge when its remaining requirements pass.`,
        disabled: false,
      };
    }
    return {
      kind: "reopen",
      label: "Reopen review",
      detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.",
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
      detail: "A newer revision exists and this one no longer accepts changes. Reload before judging the new revision.",
      disabled: false,
    };
  }
  const handoff = handoffCounts(feedbackStatus, feedbackDelivery);
  if (commentsState === "loading") {
    // Reviewing the next area never depends on the comments; only finishing does.
    const step = targetStep({ progress, nextTarget, afterHandoff, handoff, started });
    if (step) return step;
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

  // Unsent feedback has its own Send control (see headerLeadsWithSend); this
  // descriptor is the review step, and the menu item while Send leads.
  const destination = handoffDestinationLabel(feedbackDelivery);
  const toPullRequest = handoffToPullRequest(feedbackDelivery, feedbackStatus);
  if (toPullRequest && handoff.inFlight > 0) {
    return {
      kind: "delivery_pending",
      label: "Publish pending",
      detail: "A GitHub publish is in progress or its result is unknown; LemonCrow checks the pull request before posting again.",
      disabled: true,
    };
  }
  const toAgent = handoffToCodingAgent(feedbackDelivery);
  if (feedbackStatus.addressed > 0) {
    return {
      kind: "rereview_comments",
      label: `Re-review ${feedbackStatus.addressed}`,
      detail: `${toAgent ? "The coding agent" : "The author"} marked these comments addressed; only your re-review can resolve them.`,
      disabled: false,
    };
  }

  const step = targetStep({ progress, nextTarget, afterHandoff, handoff, started });
  if (step) return step;

  // Queued, working or in doubt: nothing to judge until the fix returns. The
  // action opens the Send sheet to inspect delivery, never to resend.
  if (handoff.published + handoff.inFlight > 0) {
    return {
      kind: "waiting_agent",
      label: `Waiting on ${destination}`,
      detail: `Feedback is with ${destination}; the next revision or addressed comments return this Review to you.`,
      disabled: false,
    };
  }
  if (!toPullRequest && feedbackStatus.unpublished === 0 && feedbackStatus.open_total > 0) {
    return {
      kind: "review_comments",
      label: "Review comments",
      detail: "Open human review discussions still need attention.",
      disabled: false,
    };
  }

  if (landing?.landing.supported && landing.landing.state === "merged") {
    return {
      kind: "review_readiness",
      label: "Merged",
      detail: `${landing.landing.provider || "The connected provider"} merged the reviewed revision.`,
      disabled: false,
    };
  }

  // Merge when ready is offered only after Finish (the finished branch above); an
  // open review whose work is done leads with Finish review. A merge still armed
  // stays visible so it can be inspected or cancelled.
  if (landing?.landing.supported && landing.armable && landing.landing.armed) {
    const provider = landing.landing.provider || "the connected provider";
    return {
      kind: "review_readiness",
      label: "Merge when ready enabled",
      detail: `Merge when ready is enabled in ${provider}. Open Status to inspect or cancel it.`,
      disabled: false,
    };
  }

  if (!readiness.ready) {
    // Once landing conditions are available, they are the authority for what
    // actually blocks this Snapshot. The Brief also carries informational
    // NOT_RUN/UNKNOWN placeholders; those must not invent a second client-side
    // verification gate or hide a pending human-review condition.
    const landingCoverage = landing?.conditions?.find(
      (condition) => condition.id === "latest-snapshot-reviewed"
        && !["pass", "waived", "not_applicable"].includes(condition.state),
    );
    if (landingCoverage) {
      return {
        kind: "review_readiness",
        label: "Review coverage",
        detail: landingCoverage.detail,
        disabled: false,
      };
    }
    // Every part is judged and nothing failed: checks that never ran are said in the Finish sheet,
    // not a reason to keep the reviewer from finishing. A check that failed still leads.
    const unmet = landing?.conditions?.filter((condition) => !["pass", "waived", "not_applicable"].includes(condition.state)) ?? [];
    const onlyChecksNotRun = unmet.length > 0
      ? unmet.every((condition) => condition.category === "verification" && condition.state !== "fail")
      : !landing?.conditions
        && readiness.failedChecks === 0
        && readiness.unresolvedChecks > 0
        && readiness.blockers.every((blocker) => blocker.id === "unresolved-checks");
    if (onlyChecksNotRun) {
      return {
        kind: "finish",
        label: "Finish review",
        detail: "Every part is reviewed. Tests or checks have not run; Finish lists what is still open.",
        disabled: false,
      };
    }
    const landingVerification = landing?.conditions?.find(
      (condition) => condition.category === "verification"
        && !["pass", "waived", "not_applicable"].includes(condition.state),
    );
    if (landingVerification?.state === "fail") {
      return {
        kind: "review_readiness",
        label: "Review failed check",
        detail: landingVerification.detail,
        disabled: false,
      };
    }
    if (landingVerification?.state === "pending") {
      return {
        kind: "review_readiness",
        label: "Review verification",
        detail: landingVerification.detail,
        disabled: false,
      };
    }
    if (!landing && readiness.failedChecks > 0) {
      return {
        kind: "review_readiness",
        label: readiness.failedChecks === 1 ? "Review failed check" : `Review ${readiness.failedChecks} failed checks`,
        detail: "Current verification explicitly reports failure.",
        disabled: false,
      };
    }
    if (!landing && readiness.unresolvedChecks > 0) {
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
    label: "Finish review",
    detail: "Human judgments, discussions, and required verification have no current blockers.",
    disabled: false,
  };
}
