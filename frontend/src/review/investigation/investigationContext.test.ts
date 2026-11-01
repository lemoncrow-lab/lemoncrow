import { describe, expect, it } from "vitest";

import { INVESTIGATION_FIXTURES, fixtureItem } from "./fixtures";
import { investigationContext, sameSnapshot } from "./investigationContext";

describe("investigationContext", () => {
  it("carries the Snapshot identity, SHAs, sources and checks of the Review subject", () => {
    const fixture = INVESTIGATION_FIXTURES.paymentRetry();
    const context = investigationContext(fixture);
    expect(context).toMatchObject({
      version: 1,
      review_id: "review-pay",
      revision_id: "rev-pay-1",
      snapshot_identity: "review-pay@rev-pay-1",
      base_sha: fixture.revision.base_sha,
      head_sha: fixture.revision.head_sha,
      subject: { kind: "review", id: "review-pay" },
      relationship_proof: "snapshot",
      sources_omitted: 0,
      unresolved_target_ids: [],
      unavailable: [],
    });
    expect(context.sources.map((source) => source.target_id)).toEqual(["pay-retry", "pay-key", "pay-test"]);
    expect(context.checks).toContainEqual(expect.objectContaining({ key: "pytest tests/payments", status: "PASS", origin: "snapshot_check", target_ids: [] }));
    expect(investigationContext(fixture)).toEqual(context);
  });

  it("leaves what the inputs do not carry null and names it, ignoring another Snapshot's revision", () => {
    const fixture = INVESTIGATION_FIXTURES.paymentRetry();
    const context = investigationContext({
      projection: fixture.projection,
      targets: fixture.targets,
      revision: { ...fixture.revision, id: "rev-other" },
    });
    expect(context.base_sha).toBeNull();
    expect(context.head_sha).toBeNull();
    expect(context.unavailable).toEqual(["base_sha", "head_sha", "snapshot_checks"]);
  });

  it("scopes a target subject to its own sources and linked evidence, never Review-level checks", () => {
    const fixture = INVESTIGATION_FIXTURES.authRefresh();
    const context = investigationContext({ ...fixture, subject: { kind: "target", id: "auth-refresh" } });
    expect(context.sources.map((source) => source.target_id)).toEqual(["auth-refresh"]);
    expect(context.checks.map((check) => [check.origin, check.key])).toEqual([["claim_evidence", "vitest auth/refresh"]]);
  });

  it("marks an AI-explained group inferred and reports ids the loaded list lacks", () => {
    const fixture = INVESTIGATION_FIXTURES.crossServicePartialIndex();
    const route = investigationContext({ ...fixture, subject: { kind: "item", id: "item:route" } });
    expect(route.relationship_proof).toBe("snapshot");
    expect(route.unresolved_target_ids).toEqual(["route-unloaded"]);
    const inferred = investigationContext({
      ...fixture,
      projection: { ...fixture.projection, items: [fixtureItem("item:ai", { rationale_origin: "inferred", primary_target_ids: ["route-store"] })] },
      subject: { kind: "item", id: "item:ai" },
    });
    expect(inferred.relationship_proof).toBe("inferred");
    expect(inferred.sources[0].proof).toBe("inferred");
  });

  it("is bound to one Snapshot: a newer Snapshot does not reuse it", () => {
    const before = INVESTIGATION_FIXTURES.billingBefore();
    const after = INVESTIGATION_FIXTURES.billingSuperseded();
    const context = investigationContext(before);
    expect(sameSnapshot(context, before.projection)).toBe(true);
    expect(sameSnapshot(context, after.projection)).toBe(false);
    const stale = investigationContext(after).checks.find((check) => check.origin === "claim_evidence");
    expect(stale).toMatchObject({ key: "pytest tests/billing", current: false, snapshot_number: 0 });
  });

  it("holds a 400+ target Review within its bound", () => {
    const context = investigationContext(INVESTIGATION_FIXTURES.largeReview());
    expect(context.sources).toHaveLength(420);
    expect(context.sources_omitted).toBe(0);
  });
});
