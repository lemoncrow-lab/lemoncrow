import { describe, expect, it } from "vitest";

import {
  adaptiveReviewLanding,
  cohortForTarget,
  guidedReviewUseful,
  isTinyMechanicalReview,
  persistentReviewViews,
  projectionSearchTextByTarget,
  reviewCohorts,
  reviewMapUseful,
} from "./reviewSurfaceModel";
import type { ReviewProjection, ReviewTarget } from "./types";

function target(id: string, path: string, attention: ReviewTarget["attention_level"] = "normal"): ReviewTarget {
  return {
    target_id: id,
    unit_key: "unit:" + id,
    kind: "symbol",
    path,
    label: id,
    symbol: id,
    start_line: 1,
    end_line: 2,
    hunk_ordinals: [0],
    spans: [],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 10,
    attention_level: attention,
    reasons: [],
    additions: 2,
    deletions: 1,
    fingerprint_method: "symbol",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

function projection(
  items: Array<{ id: string; key: string; kind: "mechanical" | "behavior" | "area"; title: string; targets: string[] }>,
  files: number,
): ReviewProjection {
  const projectionItems = items.map((item, position) => ({
    id: item.id,
    key: item.key,
    kind: item.kind,
    title: item.title,
    before: "",
    after: "",
    rationale: "",
    rationale_origin: "none" as const,
    source_label: "",
    producer: "",
    stale: false,
    attention: item.kind === "mechanical" ? "routine" as const : "understand" as const,
    tier: item.kind === "mechanical" ? 5 : 4,
    position,
    primary_target_ids: item.targets,
    related_target_ids: [],
    claim_ids: [],
    question_ids: [],
    concerns: [],
    unresolved_refs: [],
    limitations: [],
    internal_only: false,
  }));
  return {
    projection_version: 1,
    review_id: "r-1",
    revision_id: "rev-1",
    snapshot_number: 1,
    historical: false,
    plan: { schema_version: 1, input_digest: "digest", intents: [], limitations: [] },
    actions: [],
    questions: [],
    items: projectionItems,
    claims: [],
    decisions: [],
    coverage: {
      target_count: items.flatMap((item) => item.targets).length,
      author_explained: 0,
      fallback_grouped: 0,
      other: 0,
      routine: 0,
      unresolved_refs: 0,
      unexplained_targets: 0,
      file_count: files,
    },
    counts: { review_first: 0, understand: 0, routine: 0, done: 0, decisions: 0 },
    queue: items.flatMap((item) => item.targets),
    tour: {
      schema_version: 1,
      step_count: projectionItems.length,
      steps: projectionItems.map((item, position) => ({
        id: "tour:" + item.key,
        semantic_key: item.key,
        title: item.title,
        why: "",
        why_origin: "deterministic",
        target_ids: item.primary_target_ids,
        stale_target_ids: [],
        change_type: "changed",
        signals: item.kind === "area" ? ["public_contract"] : [],
        claim_ids: [],
        decision_refs: [],
        evidence_refs: [],
        stale: false,
        synthesis_needed: false,
        position,
        implementation: item.primary_target_ids[0]
          ? { target_id: item.primary_target_ids[0], path: "", symbol: "", start_line: 1, end_line: 2 }
          : null,
      })),
    },
  };
}

describe("reviewSurfaceModel", () => {
  it("keeps a tiny mechanical change in Code with no Guided Review or Map", () => {
    const targets = [target("t-1", "src/version.ts", "mechanical")];
    const plan = projection([{ id: "item-1", key: "cohort:version", kind: "mechanical", title: "Version", targets: ["t-1"] }], 1);

    expect(isTinyMechanicalReview(plan, targets)).toBe(true);
    expect(adaptiveReviewLanding(plan, targets, true)).toBe("implementation");
    expect(persistentReviewViews(plan, targets, true)).toEqual(["implementation"]);
    expect(guidedReviewUseful(plan, targets)).toBe(false);
    expect(reviewMapUseful(plan, targets)).toBe(false);
  });

  it("uses projection item keys as the same stable cohort identity across surfaces", () => {
    const targets = [target("t-auth", "src/auth.ts"), target("t-test", "tests/auth.test.ts")];
    const plan = projection([
      { id: "generated-display-id", key: "cohort:auth", kind: "behavior", title: "Authentication", targets: ["t-auth", "t-test"] },
    ], 2);

    const cohorts = reviewCohorts(plan, targets);
    expect(cohorts).toHaveLength(1);
    expect(cohorts[0].id).toBe("cohort:auth");
    expect(plan.tour?.steps[0].semantic_key).toBe(cohorts[0].id);
    expect(cohortForTarget(cohorts, "t-test")?.id).toBe("cohort:auth");
    expect(cohorts[0].paths).toEqual(["src/auth.ts", "tests/auth.test.ts"]);
  });

  it("keeps independent concerns as multiple cohorts and earns Map only when useful", () => {
    const targets = [
      target("t-auth", "src/auth.ts"),
      target("t-billing", "src/billing.ts"),
      target("t-ui", "src/ui.ts"),
      target("t-test", "tests/integration.test.ts"),
    ];
    const plan = projection([
      { id: "i-auth", key: "cohort:auth", kind: "behavior", title: "Authentication", targets: ["t-auth"] },
      { id: "i-billing", key: "cohort:billing", kind: "behavior", title: "Billing", targets: ["t-billing", "t-test"] },
      { id: "i-ui", key: "cohort:ui", kind: "behavior", title: "Frontend", targets: ["t-ui"] },
    ], 4);

    expect(reviewCohorts(plan, targets).map((cohort) => cohort.id)).toEqual([
      "cohort:auth",
      "cohort:billing",
      "cohort:ui",
    ]);
    expect(adaptiveReviewLanding(plan, targets, true)).toBe("brief");
    expect(guidedReviewUseful(plan, targets)).toBe(true);
    expect(reviewMapUseful(plan, targets)).toBe(true);
    expect(persistentReviewViews(plan, targets, true)).toEqual(["brief", "implementation", "map"]);
  });

  it("indexes Brief and Guided Review concepts back onto exact revision parts", () => {
    const plan = projection([
      { id: "i-auth", key: "cohort:auth", kind: "behavior", title: "Session rotation", targets: ["t-auth"] },
    ], 1);
    plan.items[0].rationale = "Rotate refresh tokens after privileged login";
    plan.tour!.steps[0].why = "Protect the authentication boundary";
    plan.tour!.steps[0].signals = ["security_trust_boundary"];
    plan.summary = {
      paragraphs: [],
      statements: [{ text: "Privileged sessions now rotate tokens.", origin: "observed", target_ids: ["t-auth"], claim_ids: [] }],
      origin: "observed",
      scope: "",
      verification: "",
      attention: "",
      additional_explanations: 0,
    };

    const search = projectionSearchTextByTarget(plan);
    expect(search.get("t-auth")).toContain("Session rotation");
    expect(search.get("t-auth")).toContain("authentication boundary");
    expect(search.get("t-auth")).toContain("Privileged sessions now rotate tokens");
  });

  it("does not render Map merely for symmetry on an ordinary one-concern change", () => {
    const targets = [target("t-auth", "src/auth.ts"), target("t-test", "tests/auth.test.ts")];
    const plan = projection([
      { id: "i-auth", key: "cohort:auth", kind: "behavior", title: "Authentication", targets: ["t-auth", "t-test"] },
    ], 2);

    expect(adaptiveReviewLanding(plan, targets, true)).toBe("brief");
    expect(reviewMapUseful(plan, targets)).toBe(false);
    expect(persistentReviewViews(plan, targets, true)).toEqual(["brief", "implementation"]);
  });
});
