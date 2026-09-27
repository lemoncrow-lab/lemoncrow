import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import ReviewReadinessSheet, { deriveReviewReadiness, scmRequirementBlockers } from "./ReviewReadinessSheet";
import type { EvidenceRow, ReviewLandingReadiness, ReviewOutcome, ReviewProgress } from "./types";

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

  it("projects connected-SCM requirements as readiness blockers without owning SCM workflow", () => {
    const blockers = scmRequirementBlockers([
      {
        id: "rel-checks",
        kind: "scm_requirement",
        relation: "requires",
        provider: "github",
        external_id: "required-checks",
        target_change_ref: "",
        url: "",
        title: "Required checks",
        status: "pending",
        source: "imported",
        metadata: {},
        created_at: "",
        updated_at: "",
      },
      {
        id: "rel-approval",
        kind: "scm_requirement",
        relation: "requires",
        provider: "github",
        external_id: "approval",
        target_change_ref: "",
        url: "",
        title: "Code-owner approval",
        status: "passed",
        source: "imported",
        metadata: {},
        created_at: "",
        updated_at: "",
      },
    ]);

    expect(blockers).toEqual([
      {
        id: "scm-requirement:rel-checks",
        label: "Required checks · pending",
        detail: "github remains authoritative for this landing requirement.",
        tone: "amber",
      },
    ]);
    const readiness = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification,
      additionalBlockers: blockers,
      outcome: null,
    });
    expect(readiness.ready).toBe(false);
    expect(readiness.blockers[0]?.id).toBe("scm-requirement:rel-checks");
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
    expect(screen.getByText("1 review area need judgment")).toBeTruthy();
    expect(screen.getByText("Evidence")).toBeTruthy();
    expect(screen.getByText("Unit tests")).toBeTruthy();
    expect(screen.getByText("API contract")).toBeTruthy();
    expect(screen.getByText("1 stale artifact")).toBeTruthy();
    expect(screen.getByText("PASS")).toBeTruthy();
    expect(screen.getByText("FAIL")).toBeTruthy();
    expect(screen.queryByText("1 failed check")).toBeNull();
  });

  it("shows exact landing conditions and requires an explicit arm action", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const onArmLanding = vi.fn();
    const landing: ReviewLandingReadiness = {
      review_id: "review-1",
      snapshot_id: "rr-2",
      snapshot_number: 2,
      tree_fingerprint: "tree-2",
      source_fingerprint: "source-2",
      conditions: [
        { id: "reviewed", label: "Latest Snapshot reviewed", state: "pass", detail: "All targets reviewed.", owner: "lemoncrow", category: "human_review", url: "" },
        { id: "checks", label: "Required checks", state: "pending", detail: "GitHub is waiting on CI.", owner: "scm", category: "scm", url: "https://github.example/pr/7" },
      ],
      ready: false,
      armable: true,
      blocker_count: 1,
      local_blocker_count: 0,
      conditions_hash: "conditions-2",
      work_items: [{ id: "work-1", kind: "work_item", relation: "implements", provider: "linear", external_id: "ENG-431", target_change_ref: "", url: "https://linear.example/ENG-431", title: "Fix callback race", status: "", source: "imported", metadata: {}, created_at: "", updated_at: "" }],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "https://github.example/pr/7", mechanism: "" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={onArmLanding} onClose={vi.fn()} />);

    expect(screen.getByText("Not ready · 1 condition")).toBeTruthy();
    expect(screen.getByText("Latest Snapshot reviewed")).toBeTruthy();
    expect(screen.getByText("SCM-owned")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Fix callback race/i })).toBeTruthy();
    const arm = screen.getByRole("button", { name: "Arm Merge when ready" });
    expect(arm).not.toHaveProperty("disabled", true);
    fireEvent.click(arm);
    expect(onArmLanding).toHaveBeenCalledTimes(1);
  });

  it("shows a merged landing as done and never offers to arm it again", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest Snapshot reviewed", state: "pass", detail: "All targets reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "merged", armed: false, semantic_stale: false, revision_id: "rr-2", tree_fingerprint: "tree-2", conditions_hash: "conditions-2", detail: "GitHub merged exactly the reviewed head bbbbbbbbbbbb.", url: "https://github.example/pr/7", mechanism: "exact_head_merge" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onCancelLanding={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText("merged")).toBeTruthy();
    expect(screen.getByText("GitHub merged exactly the reviewed head bbbbbbbbbbbb.")).toBeTruthy();
    expect(screen.queryByText("Ready to land")).toBeNull();
    expect(screen.queryByRole("button", { name: /Merge when ready/ })).toBeNull();
  });

  it("still offers to cancel an armed landing when the sheet is otherwise read-only", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const onCancelLanding = vi.fn();
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest Snapshot reviewed", state: "pass", detail: "All targets reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "armed", armed: true, semantic_stale: false, revision_id: "rr-2", tree_fingerprint: "tree-2", conditions_hash: "conditions-2", detail: "Waiting for GitHub requirements.", url: "https://github.example/pr/7", mechanism: "lemoncrow_watch" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} readOnly onArmLanding={vi.fn()} onCancelLanding={onCancelLanding} onClose={vi.fn()} />);

    // Cancelling is always safe: a discarded or no-longer-current view may
    // still withdraw landing, but it may never arm it.
    fireEvent.click(screen.getByRole("button", { name: "Cancel merge when ready" }));
    expect(onCancelLanding).toHaveBeenCalledTimes(1);
  });

  it("never offers to arm from a read-only sheet", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest Snapshot reviewed", state: "pass", detail: "All targets reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} readOnly onArmLanding={vi.fn()} onCancelLanding={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByRole("button", { name: /Arm Merge when ready/ })).toBeNull();
  });

  it("shows a human-waived condition as waived rather than passed", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "verification", label: "Verification waived", state: "waived", detail: "1 check(s) waived by a human on this Snapshot, not passed: CI (retired).", owner: "lemoncrow", category: "verification", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };
    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText("Verification waived")).toBeTruthy();
    expect(screen.getByText("waived by a human")).toBeTruthy();
  });

  it("does not allow arming while a LemonCrow-owned landing blocker remains", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "comment", label: "No unresolved request-change comments", state: "fail", detail: "1 unresolved human request-change remains.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: false, armable: false, blocker_count: 1, local_blocker_count: 1, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };
    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Arm Merge when ready" })).toHaveProperty("disabled", true);
  });

  it("does not call review-ready state ready to land when no SCM landing adapter exists", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1",
      snapshot_id: "rr-2",
      snapshot_number: 2,
      tree_fingerprint: "tree-2",
      source_fingerprint: "source-2",
      conditions: [
        { id: "reviewed", label: "Latest Snapshot reviewed", state: "pass", detail: "All targets reviewed.", owner: "lemoncrow", category: "human_review", url: "" },
      ],
      ready: true,
      armable: true,
      blocker_count: 0,
      local_blocker_count: 0,
      conditions_hash: "conditions-2",
      work_items: [],
      landing: { supported: false, provider: "git", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onClose={vi.fn()} />);

    expect(screen.getByText("Review ready · SCM landing unavailable")).toBeTruthy();
    expect(screen.queryByText("Ready to land")).toBeNull();
  });

  it("shows an authoritative-readiness failure instead of silently presenting local readiness as complete", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(<ReviewReadinessSheet model={model} landingError="Landing readiness unavailable: offline" onClose={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain("Landing readiness unavailable: offline");
    expect(screen.getByRole("alert").textContent).toContain("Readiness is not assumed from partial local state");
  });

  it("moves initial focus inside the modal", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close review readiness" }));
  });
});
