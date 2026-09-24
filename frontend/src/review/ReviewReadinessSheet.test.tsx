import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import ReviewReadinessSheet, { deriveReviewReadiness } from "./ReviewReadinessSheet";
import type { EvidenceRow, ReviewOutcome, ReviewProgress } from "./types";

const progress: ReviewProgress = {
  target_count: 4,
  reviewed: 4,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
};

const verification = { pass: 3, fail: 0, not_run: 0, unknown: 0 };

function outcome(overrides: Partial<ReviewOutcome> = {}): ReviewOutcome {
  return {
    id: "rot-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    reviewer_id: "alice",
    outcome: "lgtm",
    summary: "Looks good.",
    created_at: "2026-09-18T00:00:00Z",
    stale: false,
    ...overrides,
  };
}

describe("deriveReviewReadiness", () => {
  it("is ready only when target, feedback, verification, and policy blockers are clear", () => {
    const result = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification,
      outcome: null,
    });
    expect(result.ready).toBe(true);
    expect(result.label).toBe("Ready for verdict");
    expect(result.blockers).toEqual([]);
  });

  it("never treats unavailable discussion state as zero open comments", () => {
    const result = deriveReviewReadiness({
      progress,
      openComments: 0,
      commentsState: "failed",
      verification,
      outcome: null,
    });
    expect(result.ready).toBe(false);
    expect(result.blockers.map((item) => item.id)).toContain("comments-unavailable");
    expect(result.blockers[0]?.label).toBe("Review discussions unavailable");
  });

  it("treats a previous-revision LGTM as stale rather than carrying approval forward", () => {
    const result = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification,
      outcome: outcome({ stale: true }),
    });
    expect(result.ready).toBe(true);
    expect(result.label).toBe("Ready for re-verdict");
    expect(result.blockers).toEqual([]);
  });

  it("keeps a current changes-requested verdict as an independent blocker", () => {
    const result = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification,
      outcome: outcome({ outcome: "changes_requested", summary: "Fix retry behavior." }),
    });
    expect(result.ready).toBe(false);
    expect(result.label).toBe("Changes requested");
    expect(result.blockers.map((item) => item.id)).toContain("outcome");
  });

  it("keeps named verification facts explicit and marks old artifacts stale", () => {
    const evidence: EvidenceRow[] = [
      { name: "Unit tests", status: "PASS", detail: "42 passed", source: "pytest", scope: "review" },
      { name: "API contract", status: "FAIL", detail: "response changed", source: "integration", scope: "review" },
      { name: "Browser flow", status: "NOT_RUN", detail: "service unavailable", source: "playwright", scope: "review" },
      { name: "Migration rollback", status: "UNKNOWN", detail: "", source: "manual", scope: "review" },
    ];
    const result = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification: { pass: 1, fail: 1, not_run: 1, unknown: 1 },
      evidence,
      staleArtifacts: 2,
      outcome: null,
    });

    expect(result.evidenceFacts.map((item) => [item.label, item.state])).toEqual([
      ["Unit tests", "verified"],
      ["API contract", "failed"],
      ["Browser flow", "not_run"],
      ["Migration rollback", "unknown"],
      ["2 stale artifacts", "stale"],
    ]);
    expect(result.blockers.map((item) => item.id)).toEqual(["failed-checks", "unresolved-checks"]);
  });

  it("accepts extra blockers without changing local target truth", () => {
    const result = deriveReviewReadiness({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      openComments: 0,
      verification,
      outcome: null,
      additionalBlockers: [
        { id: "external-0", label: "Additional review requirement", detail: "Supplied by an outer composition.", tone: "amber" },
      ],
    });
    expect(result.ready).toBe(false);
    expect(result.blockers.map((item) => item.id)).toEqual(["unjudged", "external-0"]);
  });
});


describe("ReviewReadinessSheet focus", () => {
  it("separates human blockers from named evidence instead of repeating aggregate check counts", () => {
    const model = deriveReviewReadiness({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      openComments: 0,
      verification: { pass: 1, fail: 1, not_run: 0, unknown: 0 },
      evidence: [
        { name: "Unit tests", status: "PASS", detail: "42 passed", source: "pytest", scope: "review" },
        { name: "API contract", status: "FAIL", detail: "response changed", source: "integration", scope: "review" },
      ],
      staleArtifacts: 1,
      outcome: null,
    });
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);

    expect(screen.getByText("Human review")).toBeTruthy();
    expect(screen.getByText("1 target need judgment")).toBeTruthy();
    expect(screen.getByText("Evidence")).toBeTruthy();
    expect(screen.getByText("Unit tests")).toBeTruthy();
    expect(screen.getByText("API contract")).toBeTruthy();
    expect(screen.getByText("1 stale artifact")).toBeTruthy();
    expect(screen.getByText("PASS")).toBeTruthy();
    expect(screen.getByText("FAIL")).toBeTruthy();
    expect(screen.queryByText("1 failed check")).toBeNull();
  });

  it("moves initial focus inside the modal", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close review readiness" }));
  });
});
