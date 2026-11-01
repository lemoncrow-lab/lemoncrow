import { describe, expect, it } from "vitest";

import { INVESTIGATION_FIXTURES, type InvestigationFixture } from "../investigation/fixtures";
import { investigationContext } from "../investigation/investigationContext";
import { authRefreshWithConsumers } from "../whatIf/fixtures";
import { MAX_ITEMS_PER_CATEGORY, blindSpotsModel, type BlindSpotCapabilities, type BlindSpotCategoryKey } from "./blindSpotsModel";

const ALL: BlindSpotCapabilities = { openSource: true, evidence: true, ask: true, decide: true };

function model(fixture: InvestigationFixture, capabilities: BlindSpotCapabilities = ALL) {
  return blindSpotsModel({ context: investigationContext(fixture), projection: fixture.projection, targets: fixture.targets, capabilities });
}

function category(result: ReturnType<typeof model>, key: BlindSpotCategoryKey) {
  const found = result.categories.find((row) => row.key === key);
  if (!found) throw new Error(`no category ${key}`);
  return found;
}

describe("blindSpotsModel", () => {
  it("invents nothing for a fully measured clean change and never claims it is clear", () => {
    const result = model(INVESTIGATION_FIXTURES.cleanNoGraph());
    expect(result.counts.items).toBe(0);
    expect(result.categories.filter((row) => row.state === "known")).toEqual([]);
    expect(category(result, "checks")).toMatchObject({ state: "nothing_open", scope: "1 recorded check on this Snapshot" });
    expect(category(result, "missing_traces").state).toBe("nothing_open");
    // Missing analysis stays visible as its own state, not as zero risk.
    expect(category(result, "unexamined_consumers").state).toBe("not_measured");
    expect(category(result, "snapshot_reopened").state).toBe("not_applicable");
    const text = JSON.stringify(result.categories);
    expect(text).not.toMatch(/all clear|%|confidence/i);
  });

  it("keeps missing index, failed check, unknown trace and unresolved decision distinct", () => {
    const route = model(INVESTIGATION_FIXTURES.crossServicePartialIndex());
    const index = category(route, "index_coverage");
    expect(index.state).toBe("known");
    expect(index.items.map((item) => item.title)).toEqual([
      "Only 3 of 6 changed targets are loaded",
      "1 author reference could not be matched",
      "Code graph covers 2 of 5 services",
      "Side-effect register covers changed files only",
    ]);
    expect(index.items.every((item) => item.anchor.level === "review")).toBe(true);
    expect(index.notMeasured).toEqual(["Code-graph index readiness is not carried by this projection; the Map reports it."]);
    expect(category(route, "external_handoffs").items[0]).toMatchObject({
      title: "storage service: not verified",
      anchor: { level: "target", targetId: "route-handler" },
    });

    const auth = model(INVESTIGATION_FIXTURES.authRefresh());
    const checks = category(auth, "checks").items;
    expect(checks.map((item) => [item.title, item.anchor.level])).toEqual([
      ["Check failed: vitest auth/refresh error branch", "review"],
      ["Check did not run: playwright login", "review"],
      ["Linked check failed: handleRefreshError", "target"],
    ]);
    // No behavior data: traces are not measured, not "nothing found".
    expect(category(auth, "missing_traces")).toMatchObject({ state: "not_measured", items: [] });
    expect(category(auth, "unresolved_human").items[0]).toMatchObject({ title: "2 open comments on withAuth", anchor: { level: "target", targetId: "auth-client" } });

    const pay = model(INVESTIGATION_FIXTURES.paymentRetry());
    expect(category(pay, "missing_traces").items).toEqual([expect.objectContaining({ title: "Never executed: idempotency_key", anchor: expect.objectContaining({ targetId: "pay-key" }) })]);
    const decision = category(pay, "unresolved_human").items.find((item) => item.decisionKey === "retry-limit");
    expect(decision?.actions).toEqual(["open_source", "ask", "record_decision"]);
    expect(category(pay, "untested_hypotheses").items.map((item) => item.title)).toEqual([
      "A retried charge reuses the original idempotency key.",
      "Not exercised: retry after a successful charge",
      "Can a timeout retry double-charge?",
    ]);
  });

  it("offers only actions the host can perform and keeps unprovable anchors at Review level", () => {
    const fixture = INVESTIGATION_FIXTURES.paymentRetry();
    const none = model(fixture, { openSource: false, evidence: false, ask: false, decide: false });
    expect(none.categories.flatMap((row) => row.items).every((item) => item.actions.length === 0)).toBe(true);
    const unanchored = model({
      ...fixture,
      projection: { ...fixture.projection, claims: fixture.projection.claims.map((claim) => ({ ...claim, target_ids: ["not-in-this-snapshot"] })) },
    });
    const claim = category(unanchored, "untested_hypotheses").items[0];
    expect(claim.anchor).toEqual({ level: "review" });
    expect(claim.actions).toEqual(["see_evidence", "ask"]);
  });

  it("reopens only the affected work on a new Snapshot and keeps unrelated accepted judgments", () => {
    const before = model(INVESTIGATION_FIXTURES.billingBefore());
    expect(before.counts.items).toBe(0);
    const after = model(INVESTIGATION_FIXTURES.billingSuperseded());
    const reopened = category(after, "snapshot_reopened");
    expect(reopened.items.map((item) => item.id)).toEqual([
      "snapshot_reopened:decision:rounding-mode",
      "snapshot_reopened:target:bill-round",
    ]);
    expect(reopened.items[0].actions).toContain("record_decision");
    // Superseded evidence is listed once, from the claim, not again from the delta.
    expect(category(after, "checks").items.map((item) => item.title)).toEqual(["Evidence from an earlier Snapshot: pytest tests/billing"]);
    // The accepted currency decision is unaffected and appears nowhere.
    expect(JSON.stringify(after.categories)).not.toContain("currency");
    expect(category(after, "unresolved_human").items).toEqual([]);
  });

  it("updates only the item for a target once it is reviewed", () => {
    const fixture = INVESTIGATION_FIXTURES.paymentRetry();
    expect(category(model(fixture), "uninspected_regions").items.map((item) => item.anchor)).toEqual([
      expect.objectContaining({ targetId: "pay-retry" }),
      expect.objectContaining({ targetId: "pay-key" }),
    ]);
    const reviewed = {
      ...fixture,
      targets: fixture.targets.map((target) => (target.target_id === "pay-retry" ? { ...target, state: "reviewed" as const } : target)),
    };
    const result = model(reviewed);
    expect(category(result, "uninspected_regions").items.map((item) => item.id)).toEqual(["uninspected_regions:target:pay-key"]);
    expect(category(result, "untested_hypotheses").items).toHaveLength(3);
  });

  it("lists unchanged consumers of unreviewed changed targets and reports index readiness from the projection", () => {
    const fixture = authRefreshWithConsumers({ index_status: "stale", degraded: ["caller_queries_truncated"] });
    const result = model(fixture);
    const consumers = category(result, "unexamined_consumers");
    expect(consumers.state).toBe("known");
    // The in-patch caller is not an unchanged consumer; the file-level join stays Review level only when ambiguous.
    expect(consumers.items.map((item) => [item.title, item.anchor.level])).toEqual([
      ["src/api/session.ts:12 · session.renew", "target"],
      ["src/jobs/keepalive.ts · keepalive.tick", "target"],
      ["web/app.ts:7", "target"],
    ]);
    expect(consumers.items[1].detail).toContain("2 definitions named tick");
    expect(consumers.notMeasured).toEqual([
      "Code index is stale: no known consumer is not the same as no consumer.",
      "Caller search was capped: no known consumer is not the same as no consumer.",
    ]);
    const index = category(result, "index_coverage");
    expect(index.notMeasured).toEqual([]);
    expect(index.items.map((item) => item.title)).toContain("Code index is stale");

    // Once the changed target is reviewed its consumers leave the list.
    const reviewed = { ...fixture, targets: fixture.targets.map((target) => (target.target_id === "auth-refresh" ? { ...target, state: "reviewed" as const } : target)) };
    expect(category(model(reviewed), "unexamined_consumers").items.map((item) => item.title)).toEqual(["web/app.ts:7"]);

    // No consumers with an incomplete index is not measured, never "nothing open".
    const empty = authRefreshWithConsumers({ index_status: "absent" });
    empty.projection = { ...empty.projection, context: { ...empty.projection.context!, consumers: [] } };
    expect(category(model(empty), "unexamined_consumers").state).toBe("not_measured");
    const fresh = authRefreshWithConsumers();
    fresh.projection = { ...fresh.projection, context: { ...fresh.projection.context!, consumers: [] } };
    expect(category(model(fresh), "unexamined_consumers").state).toBe("nothing_open");
    expect(category(model(fresh), "index_coverage").items.some((item) => item.id.includes("readiness"))).toBe(false);
  });

  it("stays bounded and deterministic on a 400+ target Review", () => {
    const fixture = INVESTIGATION_FIXTURES.largeReview();
    const result = model(fixture);
    expect(category(result, "uninspected_regions").items).toHaveLength(84);
    expect(category(result, "checks").items).toHaveLength(11);
    expect(model(fixture)).toEqual(result);
    const ids = result.categories.flatMap((row) => row.items.map((item) => item.id));
    expect(new Set(ids).size).toBe(ids.length);

    const wide = {
      ...fixture,
      targets: fixture.targets.map((target) => ({ ...target, annotation_counts: { open: 1, orphaned: 0, addressed_needs_rereview: 0 } })),
    };
    const human = category(model(wide), "unresolved_human");
    expect(human.items).toHaveLength(MAX_ITEMS_PER_CATEGORY);
    expect(human.omitted).toBe(420 - MAX_ITEMS_PER_CATEGORY);
  });
});
