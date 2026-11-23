/**
 * Phase 9 items 24 and 34 in the existing review surfaces.
 *
 * Stack position rides the Review header metadata (no new view or control);
 * base-only movement is a label on existing target rows and in the "changed
 * since" summary, never a changed target; a provider fresh-approval rule is an
 * SCM requirement; and feedback on lower-layer code names its owning Change.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AnnotationCard from "./AnnotationCard";
import ReviewIdentityBar, { stackSummary } from "./ReviewIdentityBar";
import ReviewReadinessSheet, {
  deriveReviewReadiness,
  scmRequirementBlockers,
} from "./ReviewReadinessSheet";
import ReviewTargetHeader from "./ReviewTargetHeader";
import { baseOnlyNote } from "./movementLabels";
import type { Annotation, ReviewMovementSummary, ReviewOverview, ReviewProgress, ReviewRelationshipInfo, ReviewStackInfo, ReviewStackMember, ReviewTarget } from "./types";

function member(overrides: Partial<ReviewStackMember> = {}): ReviewStackMember {
  return {
    position: "lower",
    distance: 1,
    change_ref: "r/lower000000000000000000000000000",
    review_id: "lower000000000000000000000000000",
    title: "Add the lower layer",
    provider: "git",
    external_id: "",
    url: "",
    head_ref: "feature-a",
    head_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    status: "open",
    readiness: { state: "approved", label: "Approved", detail: "" },
    relation: "exact",
    owned_paths: [],
    ...overrides,
  };
}

function stack(overrides: Partial<ReviewStackInfo> = {}): ReviewStackInfo {
  return {
    version: 1,
    source: "local_ancestry",
    mutation: "read_only",
    layer: 2,
    depth: 3,
    base: {
      kind: "lower_change",
      ref: "feature-a",
      sha: "a".repeat(40),
      change_ref: "r/lower000000000000000000000000000",
    },
    head_ref: "feature-b",
    lower: [member()],
    upper: [
      member({
        position: "upper",
        change_ref: "r/upper000000000000000000000000000",
        title: "Top",
        head_ref: "feature-c",
        readiness: { state: "in_review", label: "In review", detail: "" },
        relation: "",
      }),
    ],
    note: "",
    complete: true,
    ...overrides,
  };
}

function overview(
  change: Partial<NonNullable<ReviewOverview["change"]>> | null
): ReviewOverview {
  return {
    session: {
      id: "upper",
      ref: "r/upper",
      status: "open",
      actor_type: "agent",
      repo_root: "/repo",
    },
    revision: { revision_number: 1 },
    title: "Upper layer",
    change_story: [],
    change: change
      ? { relations: [], work_items: [], scm_requirements: [], ...change }
      : null,
  } as unknown as ReviewOverview;
}

function movement(
  overrides: Partial<ReviewMovementSummary> = {}
): ReviewMovementSummary {
  return {
    kind: "rebase_only",
    base_moved: true,
    head_moved: true,
    deterministic: true,
    base_only_paths: ["pkg/app.py"],
    authored_paths: [],
    undecided_paths: [],
    unit_counts: {
      unchanged: 0,
      authored: 0,
      base_only: 1,
      added: 0,
      removed: 0,
    },
    reason: "",
    relative_to: "your_review",
    from_revision_number: 1,
    base_only_targets: 1,
    authored_targets: 0,
    ...overrides,
  };
}

const progress: ReviewProgress = {
  target_count: 2,
  reviewed: 2,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
};

const TARGET: ReviewTarget = {
  target_id: "target:app",
  unit_key: "file:pkg/app.py",
  kind: "file",
  path: "pkg/app.py",
  label: "pkg/app.py",
  symbol: "",
  start_line: 1,
  end_line: 20,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 5, end_line: 9, hunk_ordinal: 0 }],
  state: "reviewed",
  changed_since_mark: false,
  reviewed_revision_id: "rrv-1",
  attention_rank: 1,
  attention_level: "normal",
  reasons: [],
  additions: 1,
  deletions: 1,
  fingerprint_method: "file_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

function header(target: ReviewTarget) {
  return render(
    <ReviewTargetHeader
      target={target}
      active
      busy={false}
      onFocus={vi.fn()}
      onMark={vi.fn()}
      onComment={vi.fn()}
      onContext={vi.fn()}
    />
  );
}

describe("stack position in the Review header (item 24)", () => {
  it("summarises the layer, the lower Change with readiness and the layers above", () => {
    const summary = stackSummary(stack());
    expect(summary.label).toBe(
      "Layer 2 of 3 · on r/lower000 (Approved) · 1 above"
    );
    expect(summary.title).toContain(
      "Below: r/lower000 Add the lower layer: Approved"
    );
    expect(summary.title).toContain("Above: r/upper000 Top: In review");
    expect(summary.title).toContain("its work is not shown as this Change's");
    expect(summary.title).toContain(
      "Read-only: LemonCrow never rebases, retargets or restacks branches."
    );
  });

  it("names hosted layers by their pull request and discloses an incomplete walk", () => {
    const summary = stackSummary(
      stack({
        source: "provider",
        layer: 3,
        depth: 3,
        lower: [
          member({
            provider: "github",
            external_id: "#41",
            readiness: {
              state: "changes_requested",
              label: "Changes requested",
              detail: "",
            },
          }),
          member({ distance: 2, provider: "github", external_id: "#40" }),
        ],
        upper: [],
        note: "Some layers may be missing: the stack walk stopped at its bound.",
        complete: false,
      })
    );
    expect(summary.label).toBe("Layer 3 of 3+ · on #41 (Changes requested)");
    expect(summary.title).toContain("Further below: #40");
    expect(summary.title).toContain("Some layers may be missing");
  });

  it("keeps healthy stack context out of chrome and promotes only incomplete or blocked stacks", () => {
    const props = {
      focusMode: false,
      onShowOverview: vi.fn(),
      onShowHistory: vi.fn(),
    };
    const healthy = render(
      <ReviewIdentityBar overview={overview({ stack: stack() })} {...props} />
    );
    expect(screen.queryByTestId("review-stack-layer")).toBeNull();
    healthy.unmount();

    const incomplete = render(
      <ReviewIdentityBar overview={overview({ stack: stack({ complete: false, note: "Stack walk stopped at its bound." }) })} {...props} />
    );
    const incompleteLabel = screen.getByTestId("review-stack-layer");
    expect(incompleteLabel.textContent).toBe("Stack incomplete");
    expect(incompleteLabel.getAttribute("aria-label")).toContain("Stack needs attention: Layer 2 of 3+");
    expect(incompleteLabel.getAttribute("title")).toContain("Stack walk stopped at its bound.");
    incomplete.unmount();

    const blocked = render(
      <ReviewIdentityBar overview={overview({ stack: stack({ lower: [member({ readiness: { state: "changes_requested", label: "Changes requested", detail: "" } })] }) })} {...props} />
    );
    expect(screen.getByTestId("review-stack-layer").textContent).toBe("Stack blocked");
    expect(screen.queryByRole("button", { name: /rebase|restack|retarget/i })).toBeNull();
    blocked.unmount();

    render(<ReviewIdentityBar overview={overview({ stack: null })} {...props} />);
    expect(screen.queryByTestId("review-stack-layer")).toBeNull();
  });
});

describe("base-only movement labels (item 34)", () => {
  it("labels a base-only part row without calling it changed", () => {
    header({ ...TARGET, movement: "base_only" });
    expect(screen.getByTestId("target-base-only").textContent).toBe(
      "Base moved"
    );
    expect(screen.queryByText(/changed since/i)).toBeNull();
  });

  it("does not label authored movement as base-only", () => {
    header({
      ...TARGET,
      movement: "authored",
      state: "changed_since_review",
      changed_since_mark: true,
    });
    expect(screen.queryByTestId("target-base-only")).toBeNull();
  });

  it("notes rebase-only movement in the changed-since summary and adds no changed work", () => {
    expect(baseOnlyNote(movement())).toBe(
      "Rebase only · 1 file base-only, judgments kept"
    );
    expect(
      baseOnlyNote(
        movement({
          kind: "rebase_with_authored_changes",
          base_only_paths: ["a", "b"],
        })
      )
    ).toBe("Base moved · 2 files base-only, judgments kept");
    expect(baseOnlyNote(movement({ base_moved: false }))).toBe("");
    expect(baseOnlyNote(movement({ base_only_paths: [] }))).toBe("");
    expect(baseOnlyNote(null)).toBe("");

    const model = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
      outcome: null,
      movement: movement(),
    });
    expect(model.changedSinceReview).toBe(0);
    expect(model.baseOnlyNote).toBe(
      "Rebase only · 1 file base-only, judgments kept"
    );
    render(<ReviewReadinessSheet model={model} onClose={vi.fn()} />);
    expect(screen.getByTestId("readiness-base-only").textContent).toBe(
      "Rebase only · 1 file base-only, judgments kept"
    );
    expect(screen.queryByText(/changed since your review/)).toBeNull();
  });

  it("shows the provider fresh-approval rule as an SCM blocker, not as changed work", () => {
    const rule: ReviewRelationshipInfo = {
      id: "provider-rule:fresh_approval",
      kind: "scm_requirement",
      relation: "requires",
      provider: "github",
      external_id: "fresh_approval",
      target_change_ref: "",
      url: "",
      title:
        "Re-approval of the latest push (the provider dismisses stale approvals)",
      status: "pending",
      source: "provider",
      metadata: {},
      created_at: "",
      updated_at: "",
    };
    const blockers = scmRequirementBlockers([rule]);
    expect(blockers).toHaveLength(1);
    expect(blockers[0].id).toBe("scm-requirement:provider-rule:fresh_approval");
    expect(blockers[0].label).toBe(
      "Re-approval of the latest push (the provider dismisses stale approvals) · pending"
    );
    expect(
      scmRequirementBlockers([{ ...rule, status: "satisfied" }])
    ).toHaveLength(0);

    const model = deriveReviewReadiness({
      progress,
      openComments: 0,
      verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
      outcome: null,
      movement: movement(),
      additionalBlockers: blockers,
    });
    expect(model.ready).toBe(false);
    expect(model.changedSinceReview).toBe(0);
    expect(model.targetPending).toBe(0);
  });
});

describe("feedback on lower-layer code (item 24)", () => {
  const base: Annotation = {
    id: "ann-1",
    review_id: "upper",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "off by one",
    created_by: "local",
    created_by_actor: "human",
    source: "human",
    source_id: "local",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "x.py",
    side: "old",
    start_line: 11,
    end_line: 11,
    unit_key: "",
    symbol: "",
    origin_symbol: "",
  };

  function card(comment: Annotation) {
    return render(
      <AnnotationCard
        comments={[comment]}
        all={[comment]}
        label="Comment"
        composing={false}
        busy={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
      />
    );
  }

  it("names the owning lower Change on the comment", () => {
    card({
      ...base,
      owning_change: {
        change_ref: "r/lower000000000000000000000000000",
        review_id: "lower",
        title: "Add the lower layer",
        reason: "introduced by the lower layer",
      },
    });
    const owner = screen.getByTestId("annotation-owning-change");
    expect(owner.textContent).toBe("belongs to r/lower000");
    expect(owner.getAttribute("title")).toContain(
      "introduced by the lower layer"
    );
  });

  it("stays silent for feedback this Change owns", () => {
    card(base);
    expect(screen.queryByTestId("annotation-owning-change")).toBeNull();
  });
});
