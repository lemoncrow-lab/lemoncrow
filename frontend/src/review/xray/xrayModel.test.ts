import { describe, expect, it } from "vitest";

import { INVESTIGATION_MAP_FIXTURES, fixtureMapEdge, fixtureMapNode } from "../investigation/mapFixtures";
import type { ReviewMapGraph } from "../types";
import { buildXRay, decodeXRayLocation, encodeXRayLocation, graphSnapshot } from "./xrayModel";

const byNode = (xray: ReturnType<typeof buildXRay>) => new Map([...xray.consumers, ...xray.steps].map((step) => [step.nodeId, step]));

describe("buildXRay", () => {
  it("flips the payment retry path between base and head, observed links in order", () => {
    const { graph, flow, fixture } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const after = buildXRay({ graph, flow, rootId: "retry", side: "after", targets: fixture.targets });
    expect(after.status).toBe("ready");
    const head = byNode(after);
    expect(after.steps[0]).toMatchObject({ nodeId: "retry", role: "entry", depth: 0 });
    expect(after.consumers.map((step) => step.nodeId)).toEqual(["checkout", "test"]);
    expect(after.consumers[0]).toMatchObject({ role: "consumer", depth: -1 });
    expect(head.get("checkout")?.link).toMatchObject({ order: "observed", sequence: 1, animate: true });
    expect(head.get("key")?.link).toMatchObject({ change: "added", mark: { glyph: "+" }, order: "observed", sequence: 2, animate: true });
    expect(head.get("key")?.link?.trace).toMatchObject({ side: "head", recipe: "pytest tests/payments", calleeFn: "idempotency_key" });
    expect(head.get("ledger")).toMatchObject({ role: "boundary", exit: "boundary" });
    expect(head.get("ledger")?.link).toMatchObject({ change: "changed", weights: { base: 1, head: 2 }, order: "reachable", animate: false });
    // The added settlement queue is an async handoff, not a traced call chain.
    expect(head.get("settle")).toMatchObject({ role: "handoff", exit: "handoff" });
    expect(head.get("settle")?.link).toMatchObject({ change: "added", order: "reachable", animate: false, handoff: { kind: "async" } });
    expect(head.get("provider")).toMatchObject({ role: "handoff", roleLabel: "Remote handoff" });
    expect(head.has("backoff")).toBe(false);
    expect(after.diff).toEqual({ added: 2, removed: 0, changed: 1, otherSideOnly: 1, notCompared: 0 });
    expect(head.get("retry")?.checks?.targets).toBe(1);

    const before = buildXRay({ graph, flow, rootId: "retry", side: "before" });
    const base = byNode(before);
    expect(base.has("settle")).toBe(false);
    expect(base.has("key")).toBe(false);
    expect(base.get("backoff")?.link).toMatchObject({ change: "removed", mark: { glyph: "−", label: "removed by this change" }, order: "observed", sequence: 2 });
    expect(before.diff.otherSideOnly).toBe(2);
  });

  it("keeps a zero-trace auth refresh useful: error branch, boundaries, nothing animated", () => {
    const { graph, flow } = INVESTIGATION_MAP_FIXTURES.authRefreshMap();
    const xray = buildXRay({ graph, flow, rootId: "refresh", side: "after" });
    const steps = byNode(xray);
    expect(xray.observedLinks).toBe(0);
    expect(xray.orderNote).toContain("not an execution order");
    expect([...steps.values()].every((step) => !step.link?.animate)).toBe(true);
    expect(steps.get("errors")).toMatchObject({ role: "error" });
    expect(steps.get("errors")?.link?.mark.glyph).toBe("+");
    expect(steps.get("refresh")?.errorHints).toContainEqual({ name: "handleRefreshError", basis: "name" });
    expect(steps.get("tokens")).toMatchObject({ role: "boundary", exit: "boundary" });
    expect(steps.get("idp")).toMatchObject({ role: "handoff", canOpenSource: false });
    expect(xray.degraded.join(" ")).toContain("current index");
    expect(xray.notMeasured.map((item) => item.key)).toEqual(expect.arrayContaining(["observed_order", "error_branches", "call_order"]));
    expect(steps.get("idp")?.link?.mark).toMatchObject({ glyph: "?", tone: "unknown" });
    // In the base, the session-error handler does not exist yet, and a current-index link is never drawn as a base fact.
    const before = buildXRay({ graph, flow, rootId: "refresh", side: "before" });
    expect(byNode(before).has("errors")).toBe(false);
    expect(byNode(before).has("idp")).toBe(false);
    expect(before.diff).toMatchObject({ otherSideOnly: 1, notCompared: 1 });
  });

  it("invalidates a walk opened on an older Snapshot, and says when Before is not measured", () => {
    const { graph, flow } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    expect(graphSnapshot(graph)).toBe("review-pay@rev-pay-1");
    const stale = buildXRay({ graph, flow, rootId: "retry", side: "after", walkSnapshot: "review-pay@rev-pay-0" });
    expect(stale).toMatchObject({ status: "stale", steps: [], consumers: [] });

    const headOnly: ReviewMapGraph = { ...graph, edges: graph.edges.map(({ delta_status: _d, base_weight: _b, snapshot_weight: _s, ...edge }) => edge) };
    const before = buildXRay({ graph: headOnly, rootId: "retry", side: "before" });
    expect(before.status).toBe("not_measured");
    expect(before.notMeasured[0].needs).toContain("delta_status");
    expect(buildXRay({ graph: headOnly, rootId: "retry", side: "after" }).steps[1].link?.mark.label).toBe("change not measured");

    const location = { side: "before" as const, snapshot: "review-pay@rev-pay-1", rootId: "file:a~b" };
    expect(decodeXRayLocation(encodeXRayLocation(location))).toEqual(location);
  });

  it("bounds a 420-file graph and counts what it leaves out", () => {
    const ids = { review: "r", revision: "v" };
    const nodes = Array.from({ length: 420 }, (_, index) => fixtureMapNode(ids, `n${index}`, `src/large/module_${index}.py`));
    // Every module calls the next eight: wide and deep.
    const edges = nodes.flatMap((_, index) => Array.from({ length: 8 }, (_, step) => index + step + 1)
      .filter((to) => to < nodes.length)
      .map((to) => fixtureMapEdge(ids, `n${index}`, `n${to}`, { delta_status: "unchanged" })));
    const started = performance.now();
    const xray = buildXRay({ graph: { level: "architecture", focus: null, nodes, edges, truncated: false }, rootId: "n0", side: "after" });
    expect(performance.now() - started).toBeLessThan(250);
    expect(xray.steps.length).toBeLessThanOrEqual(xray.bounds.steps);
    expect(Math.max(...xray.steps.map((step) => step.depth))).toBeLessThanOrEqual(xray.bounds.depth);
    expect(xray.truncation.branching).toBeGreaterThan(0);
    expect(xray.truncation.depth + xray.truncation.total).toBeGreaterThan(0);
  });
});
