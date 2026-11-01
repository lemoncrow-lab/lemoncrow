import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { blindSpotsModel } from "../blindSpots/blindSpotsModel";
import { claimDigest, claimFromBlindSpot } from "../challenge/challengeModel";
import { useHeldChallenge } from "../challenge/heldChallenge";
import { INVESTIGATION_FIXTURES, fixtureDecision, fixtureDecisionState, fixtureTarget } from "../investigation/fixtures";
import { investigationContext } from "../investigation/investigationContext";
import { INVESTIGATION_MAP_FIXTURES } from "../investigation/mapFixtures";
import type { ReviewTarget } from "../types";
import { authRefreshWithConsumers } from "../whatIf/fixtures";
import { decodeXRayLocation, graphSnapshot } from "../xray/xrayModel";
import { blindSpotNote, holdClaim, investigateModel, whatIfTargetFor, xrayLocationFor } from "./investigateModel";

function target(fixture: { targets: readonly ReviewTarget[] }, id: string): ReviewTarget {
  const found = fixture.targets.find((row) => row.target_id === id);
  if (!found) throw new Error(`no target ${id}`);
  return found;
}

describe("investigateModel", () => {
  afterEach(() => window.sessionStorage.clear());

  it("scopes context, blind spots, What if and decisions to the one target", () => {
    const fixture = authRefreshWithConsumers();
    const projection = {
      ...fixture.projection,
      decisions: [
        fixtureDecision("client-retry", { question: "Retry the client on 401?", target_ids: ["auth-client"] }),
        fixtureDecision("client-old", { target_ids: ["auth-client"], state: fixtureDecisionState({ withdrawn: true }) }),
        fixtureDecision("client-done", { target_ids: ["auth-client"], state: fixtureDecisionState({ outstanding: false, disposition: "accepted" }) }),
        fixtureDecision("refresh-only", { target_ids: ["auth-refresh"] }),
      ],
    };
    const input = { ...fixture, projection, map: true };
    const client = investigateModel({ ...input, target: target(fixture, "auth-client") });
    expect(client.analysed).toBe(true);
    expect(client.context?.subject).toEqual({ kind: "target", id: "auth-client" });
    expect(client.context?.sources.map((row) => row.target_id)).toEqual(["auth-client"]);
    expect(client.blindSpots.items.length).toBeGreaterThan(0);
    expect(client.blindSpots.items.every((item) => item.anchor.level === "target" && item.anchor.targetId === "auth-client")).toBe(true);
    expect(client.blindSpots.items.map((item) => item.title)).toContain("2 open comments on withAuth");
    expect(client.decisions.map((row) => row.key)).toEqual(["client-retry"]);
    expect(client.whatIf).toMatchObject({ available: true, targetId: "auth-client" });
    expect(client.walk.available).toBe(true);

    const refresh = investigateModel({ ...input, target: target(fixture, "auth-refresh") });
    expect(refresh.blindSpots.items.map((item) => item.title)).not.toContain("2 open comments on withAuth");
    expect(refresh.decisions.map((row) => row.key)).toEqual(["refresh-only"]);

    // A file-level target starts What if on the first contract in its file.
    const file = fixtureTarget("auth-file", "src/auth/refresh.ts", "");
    expect(whatIfTargetFor([file, ...fixture.targets], file)).toBe("auth-refresh");
    expect(whatIfTargetFor([file], file)).toBeNull();
  });

  it("degrades to source and recorded facts, and never reads as all clear", () => {
    const fixture = INVESTIGATION_FIXTURES.authRefresh();
    const client = target(fixture, "auth-client");

    const bare = investigateModel({ projection: null, targets: fixture.targets, target: client, map: false });
    expect(bare.analysed).toBe(false);
    expect(bare.context).toBeNull();
    expect(bare.facts[0]).toEqual({ label: "Source", value: `${client.path}:${client.start_line}–${client.end_line}` });
    expect(bare.facts.map((fact) => fact.label)).toEqual(["Source", "Kind", "Changed", "Your review", "Checks on it", "Open comments"]);
    expect(bare.walk).toMatchObject({ available: false });
    expect(bare.walk.reason).toMatch(/no Map graph/);
    expect(bare.whatIf.available).toBe(false);
    expect(bare.blindSpots.items).toEqual([]);
    expect(bare.decisions).toEqual([]);

    // No projection.context: What if says it is unavailable rather than showing nothing.
    const noImpact = investigateModel({ ...fixture, target: client, map: true });
    expect(fixture.projection.context).toBeUndefined();
    expect(noImpact.whatIf).toMatchObject({ available: false, targetId: null });
    expect(noImpact.whatIf.reason).toMatch(/impact data/);

    // A target with nothing anchored on it says so, and says that is not verification.
    const clean = investigateModel({ ...INVESTIGATION_FIXTURES.cleanNoGraph(), target: INVESTIGATION_FIXTURES.cleanNoGraph().targets[0], map: false });
    expect(clean.blindSpots.items).toEqual([]);
    const note = blindSpotNote(clean);
    expect(note).toMatch(/not the same as verified/);
    expect(JSON.stringify(clean)).not.toMatch(/all clear/i);
  });

  it("walks from the target's own Map node, on the side it exists in", () => {
    const { fixture, graph } = INVESTIGATION_MAP_FIXTURES.authRefreshMap();
    const location = xrayLocationFor(graph, target(fixture, "auth-refresh"));
    expect(location).toMatchObject({ level: "architecture", projection: "architecture_delta", node: "refresh", story: "refresh", path: "src/auth/refresh.ts" });
    expect(decodeXRayLocation(location?.xray)).toEqual({ side: "after", snapshot: graphSnapshot(graph), rootId: "refresh" });

    const payment = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const removed = xrayLocationFor(payment.graph, { path: "src/payments/legacy_backoff.py" });
    expect(decodeXRayLocation(removed?.xray)?.side).toBe("before");

    expect(xrayLocationFor(graph, { path: "src/not/on/map.ts" })).toBeNull();
    expect(xrayLocationFor(null, target(fixture, "auth-refresh"))).toBeNull();
  });

  it("holds the Brief's claim: same digest, one held challenge across hosts", () => {
    const fixture = INVESTIGATION_FIXTURES.paymentRetry();
    const retry = target(fixture, "pay-retry");
    const model = investigateModel({ ...fixture, target: retry, map: true });
    const item = model.blindSpots.items.find((row) => row.category === "untested_hypotheses" && row.id.includes("double-charge"));
    expect(item).toBeTruthy();
    const drawerClaim = claimFromBlindSpot(item!, fixture.projection, fixture.targets, model.reviewContext!.snapshot_identity);

    // The Brief builds the same item from its own Review-level model.
    const briefContext = investigationContext({ projection: fixture.projection, targets: fixture.targets, checks: fixture.checks, revision: fixture.revision });
    const brief = blindSpotsModel({ context: briefContext, projection: fixture.projection, targets: fixture.targets, capabilities: { openSource: true, evidence: true, ask: true, decide: true } });
    const briefItem = brief.categories.flatMap((row) => row.items).find((row) => row.id === item!.id)!;
    const briefClaim = claimFromBlindSpot(briefItem, fixture.projection, fixture.targets, briefContext.snapshot_identity);
    expect(drawerClaim && briefClaim && claimDigest(drawerClaim)).toBe(briefClaim && claimDigest(briefClaim));

    // Two holders of one Review (the Brief and the drawer) show one held challenge.
    const reviewId = fixture.projection.review_id;
    const briefHost = renderHook(() => useHeldChallenge(reviewId));
    const drawerHost = renderHook(() => useHeldChallenge(reviewId));
    const run = { claimDigest: claimDigest(briefClaim!), snapshotIdentity: briefClaim!.snapshotIdentity } as never;
    act(() => briefHost.result.current[1]({ claim: briefClaim!, run }));
    expect(drawerHost.result.current[0]?.claim.text).toBe(briefClaim!.text);

    // Re-holding the same claim from the drawer keeps the Brief's run; another claim starts clean.
    const again = holdClaim(drawerHost.result.current[0], drawerClaim!);
    expect(again.run).toBe(run);
    act(() => drawerHost.result.current[1](again));
    expect(briefHost.result.current[0]?.run).toBe(run);
    const other = { ...drawerClaim!, ref: "other", text: "Another claim" };
    expect(holdClaim(again, other).run).toBeNull();

    act(() => drawerHost.result.current[1](null));
    expect(briefHost.result.current[0]).toBeNull();
    // A remount reads the same tab-scoped hold.
    act(() => briefHost.result.current[1](again));
    expect(renderHook(() => useHeldChallenge(reviewId)).result.current[0]?.claim.ref).toBe(drawerClaim!.ref);
  });
});
