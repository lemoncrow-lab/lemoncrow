import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewAttentionBar from "./ReviewAttentionBar";
import type { ReviewReadinessModel } from "./ReviewReadinessSheet";

const progress = {
  target_count: 8,
  reviewed: 5,
  changed_since_review: 1,
  needs_changes: 0,
  unreviewed: 2,
  unknown: 0,
  mechanical: 0,
};

const readiness: ReviewReadinessModel = {
  ready: false,
  label: "Not ready · 2",
  detail: "2 targets need judgment",
  blockers: [
    { id: "unjudged", label: "2 targets need judgment", detail: "Current targets need judgment.", tone: "amber" },
    { id: "changed", label: "1 target changed", detail: "Previous judgment is stale.", tone: "sky" },
  ],
  evidenceFacts: [],
  targetPending: 3,
  openComments: 1,
  failedChecks: 0,
  unresolvedChecks: 0,
  outcome: null,
};

describe("ReviewAttentionBar", () => {
  it("shows ambiguous delivery as pending rather than implying it was sent", () => {
    render(
      <ReviewAttentionBar
        revisionNumber={2}
        progress={{ ...progress, changed_since_review: 0 }}
        openCommentCount={1}
        feedbackStatus={{ open_total: 1, unpublished: 0, published: 0, in_flight: 1, addressed: 0 }}
        feedbackDelivery={{
          supported: true,
          host: "claude",
          session_id: "session-1",
          target_ref: "claude:session-1",
          label: "Claude",
          reason: "",
        }}
        addressedNeedsRereview={0}
        historical={false}
        delivery={{ state: "uncertain", target_ref: "claude:session-1", remote_ref: "job-1", message: "verification failed", annotation_count: 1, operation_id: "fop-1" }}
        readiness={readiness}
        onShowComments={vi.fn()}
        onShowReadiness={vi.fn()}
      />,
    );

    expect(screen.getByText("Delivery pending")).toBeTruthy();
    expect(screen.getByText(/will not resend them automatically/)).toBeTruthy();
    expect(screen.queryByText(/Waiting on Claude/)).toBeNull();
  });

  it("never says ready while verification is unresolved", () => {
    render(
      <ReviewAttentionBar
        revisionNumber={2}
        progress={{ ...progress, reviewed: 8, changed_since_review: 0, unreviewed: 0 }}
        openCommentCount={0}
        feedbackStatus={{ open_total: 0, unpublished: 0, published: 0, in_flight: 0, addressed: 0 }}
        addressedNeedsRereview={0}
        historical={false}
        delivery={null}
        readiness={{
          ...readiness,
          ready: false,
          label: "Not ready · 1",
          detail: "1 check not run or unknown",
          blockers: [{ id: "unresolved-checks", label: "1 check not run or unknown", detail: "Verification is incomplete.", tone: "amber" }],
          targetPending: 0,
          openComments: 0,
          unresolvedChecks: 1,
        }}
        onShowComments={vi.fn()}
        onShowReadiness={vi.fn()}
      />,
    );

    expect(screen.getByText("Verification incomplete")).toBeTruthy();
    expect(screen.queryByText("Ready to finish")).toBeNull();
  });

  it("keeps the strip action-oriented instead of repeating progress metrics", async () => {
    const onShowReadiness = vi.fn();
    render(
      <ReviewAttentionBar
        revisionNumber={2}
        progress={progress}
        openCommentCount={1}
        feedbackStatus={{ open_total: 1, unpublished: 0, published: 0, in_flight: 0, addressed: 0 }}
        addressedNeedsRereview={0}
        historical={false}
        delivery={null}
        readiness={readiness}
        onShowComments={vi.fn()}
        onShowReadiness={onShowReadiness}
      />,
    );

    expect(screen.getByText("Re-review")).toBeTruthy();
    expect(screen.getByText("2 blockers")).toBeTruthy();
    expect(screen.queryByText("5/8")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /review readiness/i }));
    expect(onShowReadiness).toHaveBeenCalledTimes(1);
  });
});
