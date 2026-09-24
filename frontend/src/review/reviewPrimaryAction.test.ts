import { describe, expect, it } from "vitest";

import { deriveReviewPrimaryAction } from "./reviewPrimaryAction";
import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { FeedbackStatus, ReviewProgress } from "./types";

const progress: ReviewProgress = {
  target_count: 4,
  reviewed: 4,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
};
const feedback: FeedbackStatus = { open_total: 0, unpublished: 0, published: 0, in_flight: 0, addressed: 0 };
const readiness: ReviewReadinessModel = {
  ready: true,
  label: "Ready for verdict",
  detail: "Ready",
  blockers: [],
  evidenceFacts: [],
  targetPending: 0,
  openComments: 0,
  failedChecks: 0,
  unresolvedChecks: 0,
  outcome: null,
};

function action(overrides: Partial<Parameters<typeof deriveReviewPrimaryAction>[0]> = {}) {
  return deriveReviewPrimaryAction({
    historical: false,
    sessionStatus: "open",
    revisionAdvanced: false,
    sourceChanged: false,
    commentsState: "ready",
    feedbackStatus: feedback,
    progress,
    readiness,
    nextTarget: null,
    ...overrides,
  });
}

describe("deriveReviewPrimaryAction", () => {
  it("never offers finish while current target judgment remains", () => {
    expect(action({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: { targetId: "t-1", path: "src/a.py", label: "authorize", reason: "public contract changed" },
    })).toMatchObject({ kind: "review_target", label: "Review next area", targetId: "t-1", disabled: false });
  });

  it("puts changed work ahead of ordinary pending review", () => {
    expect(action({
      progress: { ...progress, reviewed: 2, changed_since_review: 1, unreviewed: 1 },
      nextTarget: { targetId: "changed", path: "src/a.py", label: "authorize", reason: "Changed since your last review" },
    })).toMatchObject({ kind: "review_changed", label: "Re-review changed area", targetId: "changed" });
  });

  it("keeps correction handoff ahead of remaining targets", () => {
    expect(action({
      feedbackStatus: { ...feedback, open_total: 1, unpublished: 1 },
      feedbackDelivery: { supported: true, host: "claude", session_id: "s-1", target_ref: "claude:s-1", label: "Claude", reason: "" },
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: { targetId: "t-1", path: "src/a.py", label: "authorize", reason: "Needs judgment" },
    })).toMatchObject({ kind: "send_feedback", label: "Send 1 to Claude…" });
  });

  it("keeps source drift separate from review workflow while revision/loading failures remain recovery actions", () => {
    expect(action({ revisionAdvanced: true })).toMatchObject({ kind: "reload_state", label: "Reload review" });
    expect(action({
      sourceChanged: true,
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: { targetId: "t-1", path: "src/a.py", label: "authorize", reason: "Needs judgment" },
    })).toMatchObject({ kind: "review_target", label: "Review next area", targetId: "t-1" });
    expect(action({ commentsState: "failed" })).toMatchObject({ kind: "reload_state", label: "Retry review state" });
  });

  it("routes verification blockers to evidence rather than finish", () => {
    expect(action({
      readiness: { ...readiness, ready: false, failedChecks: 2, blockers: [{ id: "failed-checks", label: "2 failed checks", detail: "fail", tone: "rose" }] },
    })).toMatchObject({ kind: "review_readiness", label: "Review 2 failed checks" });
    expect(action({
      readiness: { ...readiness, ready: false, unresolvedChecks: 1, blockers: [{ id: "unresolved-checks", label: "1 unresolved check", detail: "unknown", tone: "amber" }] },
    })).toMatchObject({ kind: "review_readiness", label: "Review verification" });
  });

  it("only offers finish after durable state and readiness are clear", () => {
    expect(action()).toMatchObject({ kind: "finish", label: "Finish review", disabled: false });
  });
});
