import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import ReviewReadinessSheet, { blockingSummary, deriveReviewReadiness, scmRequirementBlockers } from "./ReviewReadinessSheet";
import type { EvidenceRow, ReviewLandingReadiness, ReviewOutcome, ReviewProgress } from "./types";
import { fmtWhen } from "../lib/format";

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
  it("is ready only when part, feedback, verification, and policy blockers are clear", () => {
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

  it("accepts extra blockers without changing local part truth", () => {
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
        detail: "github remains authoritative for this merge requirement.",
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

    expect(screen.getByText("Your review")).toBeTruthy();
    expect(screen.getByText("3/4 reviewed")).toBeTruthy();
    expect(screen.getByText("1 part need judgment")).toBeTruthy();
    expect(screen.getByText("Verification")).toBeTruthy();
    expect(screen.getByText("Unit tests")).toBeTruthy();
    expect(screen.getByText("API contract")).toBeTruthy();
    expect(screen.getByText("1 stale artifact")).toBeTruthy();
    expect(screen.getByText("Passed")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getAllByText("Evidence details").every((summary) => !summary.closest("details")?.hasAttribute("open"))).toBe(true);
  });

  it("uses reviewer-facing language for unresolved checks and keeps evidence detail collapsed", () => {
    const model = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification: { pass: 0, fail: 0, not_run: 1, unknown: 1 },
      outcome: null,
    });
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);

    expect(screen.getByText("1 check did not run")).toBeTruthy();
    expect(screen.getByText("1 check result could not be determined")).toBeTruthy();
    expect(screen.getByText("Did not run")).toBeTruthy();
    expect(screen.getByText("Couldn’t determine")).toBeTruthy();
    expect(screen.queryByText("NOT RUN")).toBeNull();
    expect(screen.queryByText("UNKNOWN")).toBeNull();
    expect(screen.getAllByText("Evidence details").every((summary) => !summary.closest("details")?.hasAttribute("open"))).toBe(true);
  });

  it("keeps automation and signed-record internals behind closed disclosures", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(
      <ReviewReadinessSheet
        model={model}
        automation={<div>acts as service account · needs review:manage</div>}
        recordSection={<div>lc review verify-record record.json --keys keys.json</div>}
        onClose={vi.fn()}
      />,
    );

    const automation = screen.getByTestId("review-automation-details") as HTMLDetailsElement;
    const record = screen.getByTestId("review-record-details") as HTMLDetailsElement;
    expect(automation.open).toBe(false);
    expect(record.open).toBe(false);

    fireEvent.click(within(automation).getByText("Automation"));
    fireEvent.click(within(record).getByText("Review record"));
    expect(automation.open).toBe(true);
    expect(record.open).toBe(true);
  });

  it("keeps Your review, Team review, and Merge readiness conceptually distinct", () => {
    const model = deriveReviewReadiness({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      openComments: 0,
      verification,
      outcome: null,
      additionalBlockers: [
        { id: "organization-policy-0", label: "Reviewer B has not completed", detail: "Team policy requires Reviewer B.", tone: "amber" },
      ],
    });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1",
      snapshot_id: "rr-2",
      snapshot_number: 2,
      tree_fingerprint: "tree-2",
      source_fingerprint: "source-2",
      conditions: [
        {
          id: "scm-approval",
          label: "Code-owner approval",
          state: "pending",
          detail: "GitHub invalidated the previous approval after a new commit.",
          owner: "scm",
          category: "approval",
          url: "",
        },
      ],
      ready: false,
      armable: true,
      blocker_count: 1,
      local_blocker_count: 0,
      conditions_hash: "conditions-2",
      work_items: [],
      landing: {
        supported: true,
        provider: "github",
        state: "not_armed",
        armed: false,
        semantic_stale: false,
        revision_id: "",
        tree_fingerprint: "",
        conditions_hash: "",
        detail: "",
        url: "",
        mechanism: "",
      },
    };

    render(
      <ReviewReadinessSheet
        model={model}
        landing={landing}
        extraSections={<div>Required reviewers and scoped assignments</div>}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Your review")).toBeTruthy();
    expect(screen.getByText("Team review")).toBeTruthy();
    expect(screen.getByText("Reviewer B has not completed")).toBeTruthy();
    expect(screen.getByText("Merge readiness")).toBeTruthy();
    expect(screen.getByText("Provider requires renewed approval")).toBeTruthy();
    expect(screen.getByText("Code-owner approval")).toBeTruthy();
    expect(screen.getByText(/invalidated the previous approval/)).toBeTruthy();
    expect(screen.getByText("1 part need judgment")).toBeTruthy();
    expect(screen.queryByText(/changed since your review/)).toBeNull();
  });

  it("shows exact landing conditions and requires an explicit merge-when-ready action", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const onArmLanding = vi.fn();
    const landing: ReviewLandingReadiness = {
      review_id: "review-1",
      snapshot_id: "rr-2",
      snapshot_number: 2,
      tree_fingerprint: "tree-2",
      source_fingerprint: "source-2",
      conditions: [
        { id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" },
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

    render(<ReviewReadinessSheet model={model} landing={landing} finished onArmLanding={onArmLanding} onClose={vi.fn()} />);

    expect(screen.getByText("Not ready · 1 condition")).toBeTruthy();
    expect(screen.getByText("Latest revision reviewed")).toBeTruthy();
    expect(screen.getByText("Provider")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Fix callback race/i })).toBeTruthy();
    const mergeWhenReady = screen.getByRole("button", { name: "Merge when ready" });
    expect(mergeWhenReady).not.toHaveProperty("disabled", true);
    fireEvent.click(mergeWhenReady);
    expect(onArmLanding).toHaveBeenCalledTimes(1);
  });

  it("shows a merged landing as done and never offers to enable merge again", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "merged", armed: false, semantic_stale: false, revision_id: "rr-2", tree_fingerprint: "tree-2", conditions_hash: "conditions-2", detail: "GitHub merged exactly the reviewed head bbbbbbbbbbbb.", url: "https://github.example/pr/7", mechanism: "exact_head_merge" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onCancelLanding={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText("merged")).toBeTruthy();
    expect(screen.getByText("GitHub merged exactly the reviewed head bbbbbbbbbbbb.")).toBeTruthy();
    expect(screen.queryByText("Ready to merge")).toBeNull();
    expect(screen.queryByRole("button", { name: /Merge when ready/ })).toBeNull();
  });

  it("shows the deployment line of a merged Change without changing readiness", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const { rerender } = render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);
    const before = screen.getAllByRole("button").length;
    rerender(
      <ReviewReadinessSheet
        model={model}
        deployments={<div data-testid="deployments">Deployed to staging · production canary 10%</div>}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByTestId("deployments").textContent).toBe("Deployed to staging · production canary 10%");
    expect(screen.getAllByRole("button").length).toBe(before); // no new control
  });

  it("still offers to cancel an enabled merge when the sheet is otherwise read-only", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const onCancelLanding = vi.fn();
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "armed", armed: true, semantic_stale: false, revision_id: "rr-2", tree_fingerprint: "tree-2", conditions_hash: "conditions-2", detail: "Waiting for GitHub requirements.", url: "https://github.example/pr/7", mechanism: "lemoncrow_watch" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} readOnly onArmLanding={vi.fn()} onCancelLanding={onCancelLanding} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel merge when ready" }));
    expect(onCancelLanding).toHaveBeenCalledTimes(1);
  });

  it("never offers to enable merge from a read-only sheet", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} readOnly onArmLanding={vi.fn()} onCancelLanding={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Merge when ready" })).toBeNull();
  });

  it("shows a human-waived condition as waived rather than passed", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "verification", label: "Verification waived", state: "waived", detail: "1 check(s) waived by a human on this revision, not passed: CI (retired).", owner: "lemoncrow", category: "verification", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };
    render(<ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText("Verification waived")).toBeTruthy();
    expect(screen.getByText("waived by a human")).toBeTruthy();
  });

  it("names the policy generation and evidence freshness without new chrome", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [
        {
          id: "supply-chain-policy", label: "Dependency / supply-chain policy passed", state: "pass", detail: "Current dependency / supply-chain policy results passed.", owner: "lemoncrow", category: "supply_chain", url: "", reason: "satisfied",
          evidence_refs: [
            { evidence_id: "ev-sbom", freshness_class: "snapshot_intrinsic", state: "current", observed_at: "2026-10-01T12:00:00Z", source_revision: "", expires_at: "", reason: "" },
            { evidence_id: "ev-scan", freshness_class: "external_observation", state: "current", observed_at: "2026-10-01T12:00:00Z", source_revision: "db@100", expires_at: "2026-10-02T12:00:00Z", reason: "" },
          ],
        },
        {
          id: "feed", label: "Required advisory feed", state: "unknown", detail: "The advisory observation is no longer current; re-observe on this revision.", owner: "lemoncrow", category: "verification", url: "", reason: "evidence_expired",
          evidence_refs: [{ evidence_id: "ev-feed", freshness_class: "external_observation", state: "expired", observed_at: "2026-09-29T12:00:00Z", source_revision: "", expires_at: "2026-09-30T12:00:00Z", reason: "external observation expired" }],
        },
      ],
      ready: false, armable: false, blocker_count: 1, local_blocker_count: 1, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "armed", armed: true, semantic_stale: false, revision_id: "rr-2", tree_fingerprint: "tree-2", conditions_hash: "conditions-2", detail: "", url: "", mechanism: "", policy_generation: "policy:old", policy_stale: true },
      policy_generation: {
        digest: "policy:abcdef1234567890",
        short: "abcdef123456",
        authority: "provisional",
        layers: [
          { layer: "built_in", source: "lemoncrow", digest: "1", state: "final" },
          { layer: "trusted_base", source: "trusted base .lemoncrow/review.yaml (provisional)", digest: "2", state: "provisional" },
        ],
      },
      evaluated_at: "2026-10-01T13:00:00Z",
      valid_until: "2026-10-02T12:00:00Z",
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onCancelLanding={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByText(/Policy abcdef123456/)).toBeNull();
    const policy = screen.getByText("Policy details · provisional");
    fireEvent.click(policy);
    const policyDetails = screen.getByTestId("review-policy-details");
    expect(policyDetails.textContent).toContain("Generation abcdef123456");
    expect(policyDetails.getAttribute("title")).toBeNull();
    expect(within(policyDetails).getByTitle(/policy:abcdef1234567890/)).toBeTruthy();
    expect(screen.getByText(`External observation valid until ${fmtWhen("2026-10-02T12:00:00Z")}`)).toBeTruthy();
    expect(screen.getByText("1 external observation is no longer current")).toBeTruthy();
    expect(screen.getByText(/Enabled under an earlier Review policy/)).toBeTruthy();
    expect(screen.queryByText("Ready to merge")).toBeNull();
  });

  it("does not allow merge when ready while a LemonCrow-owned blocker remains", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "comment", label: "No unresolved request-change comments", state: "fail", detail: "1 unresolved human request-change remains.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: false, armable: false, blocker_count: 1, local_blocker_count: 1, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };
    render(<ReviewReadinessSheet model={model} landing={landing} finished onArmLanding={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Merge when ready" })).toHaveAttribute("aria-disabled", "true");
  });

  it("does not call review-ready state ready to land when automatic merge is unavailable", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1",
      snapshot_id: "rr-2",
      snapshot_number: 2,
      tree_fingerprint: "tree-2",
      source_fingerprint: "source-2",
      conditions: [
        { id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" },
        { id: "supply-chain-policy", label: "Dependency / supply-chain policy", state: "not_applicable", detail: "No dependency / supply-chain policy evidence is required.", owner: "lemoncrow", category: "supply_chain", url: "" },
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

    expect(screen.getByText("Review ready · merge unavailable")).toBeTruthy();
    expect(screen.queryByText("Ready to merge")).toBeNull();
    // Nothing to do here: met and not-required conditions fold into one line, and no merge box says merge is unavailable.
    const settled = screen.getByTestId("readiness-settled-conditions");
    expect(settled.hasAttribute("open")).toBe(false);
    expect(within(settled).getByText("1 met · 1 not required")).toBeTruthy();
    expect(screen.queryByText("Automatic merge is unavailable for this Change.")).toBeNull();
  });

  it("shows an authoritative-readiness failure instead of silently presenting local readiness as complete", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(<ReviewReadinessSheet model={model} landingError="Merge readiness unavailable: offline" onClose={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain("Merge readiness unavailable: offline");
    expect(screen.getByRole("alert").textContent).toContain("Readiness is not assumed from partial local state");
  });

  it("moves initial focus inside the modal", () => {
    const model = deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close review status" }));
  });
});

describe("ReviewReadinessSheet landing decisions", () => {
  const model = () => deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });
  function landing(extra: Partial<ReviewLandingReadiness["landing"]>, overrides: Partial<ReviewLandingReadiness> = {}): ReviewLandingReadiness {
    return {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "merge-result", label: "Merge result", state: "fail", detail: "1 check(s) pass on the reviewed head but fail on the merge queue group: ci.", owner: "lemoncrow", category: "landing", url: "" }],
      ready: false, armable: false, blocker_count: 1, local_blocker_count: 1, conditions_hash: "conditions-2", work_items: [],
      blocking_reasons: [
        { id: "merge-result", owner: "lemoncrow", state: "fail", action: "Merged result fails checks" },
        { id: "org-policy:check:ci", owner: "lemoncrow", state: "pending", action: "Waiting for checks" },
        { id: "org-policy:check:lint", owner: "lemoncrow", state: "pending", action: "Waiting for checks" },
      ],
      landing: {
        supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false,
        revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "", ...extra,
      },
      ...overrides,
    };
  }

  it("names the exact blocking actions once each, failing first", () => {
    render(<ReviewReadinessSheet model={model()} landing={landing({})} onClose={vi.fn()} />);
    expect(screen.getByTestId("readiness-blocking").textContent).toBe("Blocked: Merged result fails checks · Waiting for checks");
    expect(blockingSummary([{ id: "c", owner: "scm", state: "pending", action: "Provider requirement" }])).toBe("Waiting: Provider requirement");
    expect(blockingSummary([])).toBe("");
  });

  it("never shows blocking actions for a ready Change", () => {
    render(<ReviewReadinessSheet model={model()} landing={landing({}, { ready: true })} onClose={vi.fn()} />);
    expect(screen.queryByTestId("readiness-blocking")).toBeNull();
  });

  it("shows the policy approval, its failed gates, and the provider-owned merge state", () => {
    const { rerender } = render(
      <ReviewReadinessSheet
        model={model()}
        landing={landing({
          auto_approval: { state: "approved", eligible: true, blocking: [] },
          scm: { managed_by: "github", mechanism: "", auto_merge: "enabled", queue_state: "checks_requested" },
        })}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByTestId("readiness-auto-approval").textContent).toContain("Approved by organization policy on GitHub. Branch protection still applies.");
    expect(screen.getByTestId("readiness-scm-state").textContent).toBe("Managed by GitHub: auto-merge enabled · merge queue checks requested");
    rerender(
      <ReviewReadinessSheet
        model={model()}
        landing={landing({ auto_approval: { state: "none", eligible: false, blocking: ["Provider in sync", "Requested reviews current"] } })}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByTestId("readiness-auto-approval").textContent).toContain("Not auto-approved: Provider in sync · Requested reviews current");
    expect(screen.queryByTestId("readiness-scm-state")).toBeNull();
    rerender(<ReviewReadinessSheet model={model()} landing={landing({ auto_approval: { state: "none", eligible: null, blocking: [] } })} onClose={vi.fn()} />);
    expect(screen.queryByTestId("readiness-auto-approval")).toBeNull(); // nothing decided: no chrome
  });

  it("shows who broke glass, what was bypassed and the follow-up review state", () => {
    const record = (state: "open" | "overdue" | "completed") => ({
      id: `bg_${state}`, revision_id: "rr-2", actor_id: "user_admin", reason: "Checkout outage", incident_url: "https://status.example.com/7",
      bypassed: [{ id: "latest-snapshot-reviewed", label: "Latest revision reviewed", state: "pending", reason: "" }],
      created_at: "2026-10-05T09:00:00Z", policy_generation: "gen",
      follow_up: { state, due_at: "2026-10-06T09:00:00Z", completed_at: state === "completed" ? "2026-10-05T12:00:00Z" : "", completed_by: state === "completed" ? "bob" : "" },
    });
    render(<ReviewReadinessSheet model={model()} landing={landing({ break_glass: [record("completed"), record("overdue"), record("open")] })} onClose={vi.fn()} />);
    const section = screen.getByTestId("readiness-break-glass");
    // A principal id is shown as a name, never raw.
    expect(within(section).getAllByText("admin bypassed Latest revision reviewed")).toHaveLength(3);
    expect(within(section).getByText("Follow-up review by bob")).toBeTruthy();
    expect(within(section).getByText(`Follow-up review overdue (was due ${fmtWhen("2026-10-06T09:00:00Z")})`)).toBeTruthy();
    expect(within(section).getByText(`Follow-up review due ${fmtWhen("2026-10-06T09:00:00Z")}`)).toBeTruthy();
    expect(within(section).getAllByRole("link", { name: /Incident/ })).toHaveLength(3);
  });
});

describe("ReviewReadinessSheet landing recovery", () => {
  function landingFor(armed: boolean): ReviewLandingReadiness {
    return {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "reviewed", label: "Latest revision reviewed", state: "pass", detail: "All parts reviewed.", owner: "lemoncrow", category: "human_review", url: "" }],
      ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: {
        supported: true, provider: "github", state: armed ? "armed" : "not_armed", armed, semantic_stale: false,
        revision_id: armed ? "rr-2" : "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "",
      },
    };
  }
  const model = () => deriveReviewReadiness({ progress, openComments: 0, verification, outcome: null });

  it("holds merge when ready while the request is in flight so it cannot be sent twice", () => {
    const onArmLanding = vi.fn();
    render(<ReviewReadinessSheet model={model()} landing={landingFor(false)} finished landingBusy onArmLanding={onArmLanding} onClose={vi.fn()} />);
    const enable = screen.getByRole("button", { name: "Enabling…" });
    expect(enable).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(enable);
    expect(onArmLanding).not.toHaveBeenCalled();
  });

  it("keeps merge when ready available for a retry after enabling failed, with the failure named", () => {
    const onArmLanding = vi.fn();
    render(
      <ReviewReadinessSheet
        model={model()}
        landing={landingFor(false)}
        finished
        landingError="Could not arm landing: GitHub refused"
        onArmLanding={onArmLanding}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Could not arm landing: GitHub refused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Merge when ready" }));
    expect(onArmLanding).toHaveBeenCalledTimes(1);
  });

  it("offers Merge when ready only once the review is finished, and keeps an armed merge and its cancel visible", () => {
    const onArmLanding = vi.fn();
    const onCancelLanding = vi.fn();
    const { rerender } = render(
      <ReviewReadinessSheet model={model()} landing={landingFor(false)} onArmLanding={onArmLanding} onClose={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Merge when ready" })).toBeNull();
    expect(screen.getByTestId("readiness-merge-after-finish")).toHaveTextContent("Finish review first");
    // Every requirement met, but nothing merges before Finish: the heading says so.
    expect(screen.getByText("Ready to merge once finished")).toBeTruthy();

    rerender(<ReviewReadinessSheet model={model()} landing={landingFor(false)} finished onArmLanding={onArmLanding} onClose={vi.fn()} />);
    expect(screen.queryByTestId("readiness-merge-after-finish")).toBeNull();
    expect(screen.getByText("Ready to merge")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Merge when ready" }));
    expect(onArmLanding).toHaveBeenCalledTimes(1);

    // Reopened with a merge still armed: the state and its cancel stay; enabling is not offered.
    rerender(<ReviewReadinessSheet model={model()} landing={landingFor(true)} onCancelLanding={onCancelLanding} onClose={vi.fn()} />);
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.queryByTestId("readiness-merge-after-finish")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel merge when ready" }));
    expect(onCancelLanding).toHaveBeenCalledTimes(1);
  });

  it("counts a Needs changes part as reviewed and keeps its own counter and blocker", () => {
    const judged = deriveReviewReadiness({
      progress: { ...progress, reviewed: 2, needs_changes: 1, unreviewed: 1 },
      openComments: 0,
      verification,
      outcome: null,
    });
    render(<ReviewReadinessSheet model={judged} onClose={vi.fn()} />);
    expect(screen.getByText("3/4 reviewed")).toBeTruthy();
    expect(screen.getByText("1 need changes")).toBeTruthy();
    expect(screen.getByText("1 part need changes")).toBeTruthy();
    expect(judged.ready).toBe(false);
  });

  it("keeps Cancel available after cancelling failed, and holds it while in flight", () => {
    const onCancelLanding = vi.fn();
    const { rerender } = render(
      <ReviewReadinessSheet
        model={model()}
        landing={landingFor(true)}
        landingError="Could not cancel landing: timeout"
        onCancelLanding={onCancelLanding}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Could not cancel landing: timeout")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Merge when ready" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cancel merge when ready" }));
    expect(onCancelLanding).toHaveBeenCalledTimes(1);

    rerender(
      <ReviewReadinessSheet model={model()} landing={landingFor(true)} landingBusy onCancelLanding={onCancelLanding} onClose={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Cancelling…" })).toBeDisabled();
  });
});
