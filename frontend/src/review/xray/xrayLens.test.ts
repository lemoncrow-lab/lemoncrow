import { describe, expect, it } from "vitest";

import { INVESTIGATION_MAP_FIXTURES, fixtureMapEdge, fixtureMapNode } from "../investigation/mapFixtures";
import type { ReviewMapEdge, ReviewMapGraph, ReviewMapNode } from "../types";
import { edgePaths, layoutLens } from "./xrayLayout";
import {
  LENS_EDGE_CAP,
  LENS_LANE_CAP,
  buildChangeLens,
  buildFocusLens,
  edgeStyle,
  laneFor,
  lensView,
  walkthroughOrder,
  type LensDiagram,
  type LensView,
} from "./xrayLens";

const ids = { review: "review-pay", revision: "rev-pay-1" };
const file = (path: string, overrides: Partial<ReviewMapNode> = {}) => ({ path, category: "", boundaries: [], label: path.split("/").pop() ?? path, ...overrides });
const cardsOf = (view: LensView) => view.cards.map((card) => card.id).sort();
const edgeBetween = (view: LensView, from: string, to: string) => view.edges.find((edge) => edge.from === from && edge.to === to);

describe("laneFor", () => {
  it("reads the layer from paths, file names and the Map's boundary tags", () => {
    expect(laneFor(file("frontend/src/pages/PricingPage.jsx"), true)).toBe("entry");
    expect(laneFor(file("frontend/src/components/SurfaceCard.css"), false)).toBe("entry");
    expect(laneFor(file("cli/main.py"), true)).toBe("entry");
    expect(laneFor(file("src/jobs/sweep.py"), true)).toBe("entry");
    expect(laneFor(file("backend/app.py"), true)).toBe("api");
    expect(laneFor(file("src/checkout/charge_route.py"), true)).toBe("api");
    expect(laneFor(file("server/handlers/orders.go"), true)).toBe("api");
    expect(laneFor(file("src/payments/retry.py"), true)).toBe("domain");
    expect(laneFor(file("src/core/mod_01.py"), true)).toBe("domain");
    expect(laneFor(file("src/payments/anything.py", { boundaries: ["persistence"] }), true)).toBe("storage");
    expect(laneFor(file("src/payments/settlement_queue.py"), true)).toBe("storage");
    expect(laneFor(file("src/payments/provider_client.py"), true)).toBe("storage");
    expect(laneFor(file("server/db/orders.py"), true)).toBe("storage");
    expect(laneFor(file("data/plans.csv"), true)).toBe("storage");
    expect(laneFor(file("tests/payments/test_retry.py", { category: "test" }), true)).toBe("tests");
    // Docs, config and assets with no code relationship sit under the picture; connected, they take a lane.
    expect(laneFor(file("README.md", { category: "docs" }), false)).toBe("other");
    expect(laneFor(file("compose.yml", { category: "config" }), false)).toBe("other");
    expect(laneFor(file("frontend/src/content.json", { category: "config" }), true)).toBe("entry");
  });
});

describe("buildChangeLens", () => {
  const { graph, flow, fixture } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
  const diagram = buildChangeLens({ graph, flow, targets: fixture.targets, walkSnapshot: "review-pay@rev-pay-1" });
  const card = (id: string) => diagram.cards.find((item) => item.id === id)!;

  it("puts every changed file and its unchanged neighbours in lanes, left to right by layer", () => {
    expect(diagram.status).toBe("ready");
    expect([...new Set(diagram.columns.map((column) => column.group))]).toEqual(["api", "domain", "storage"]);
    expect(card("checkout")).toMatchObject({ lane: "api", context: true });
    expect(card("retry")).toMatchObject({ lane: "domain", context: false, change: "changed" });
    expect(card("ledger")).toMatchObject({ lane: "storage", context: true });
    expect(card("settle")).toMatchObject({ lane: "storage", change: "added" });
    expect(card("test")).toMatchObject({ lane: "tests", place: "tests" });
    // Within a lane, who uses whom splits it into sub-columns.
    expect(card("checkout").column).toBeLessThan(card("retry").column);
    expect(card("retry").column).toBeLessThan(card("key").column);
    expect(card("key").column).toBeLessThan(card("ledger").column);
    expect(diagram.totals).toEqual({ files: 8, changed: 5, context: 3 });
    expect(buildChangeLens({ graph, flow, targets: fixture.targets })).toEqual(diagram);
  });

  it("draws an arrow solid only when the side's recorded run observed it", () => {
    const after = lensView(diagram, "after");
    const observed = edgeBetween(after, "retry", "key")!;
    expect(edgeStyle(observed)).toMatchObject({ line: "solid", tone: "added", glyph: "+", animate: true });
    expect(edgeStyle(observed, true).animate).toBe(false);
    expect(observed).toMatchObject({ observed: true, sequence: 2 });
    expect(observed.link.trace).toMatchObject({ callerFn: "charge_with_retry", calleeFn: "idempotency_key" });
    const inferred = edgeBetween(after, "retry", "ledger")!;
    expect(edgeStyle(inferred)).toMatchObject({ line: "dashed", tone: "changed", glyph: "~", animate: false });
    expect(edgeStyle(inferred).label).toContain("static relationship, not observed");
    const settle = edgeBetween(after, "retry", "settle")!;
    expect(edgeStyle(settle)).toMatchObject({ line: "dashed", tone: "added", glyph: "+" });
    expect(edgeStyle(edgeBetween(after, "checkout", "retry")!)).toMatchObject({ line: "solid", tone: "unchanged", glyph: null });
    // The base run observed the old backoff call; on Before that arrow is solid and marked removed.
    const before = lensView(diagram, "before");
    expect(edgeStyle(edgeBetween(before, "retry", "backoff")!)).toMatchObject({ line: "solid", tone: "removed", glyph: "−" });
    expect(after.caveat?.text).toBe("3 of 6 arrows observed in a recorded run; dashed ones are static.");
  });

  it("shows removed files only on Before and added ones only on After, on the same layout", () => {
    const before = lensView(diagram, "before");
    const after = lensView(diagram, "after");
    expect(cardsOf(after)).toEqual(["checkout", "key", "ledger", "provider", "retry", "settle", "test"]);
    expect(cardsOf(before)).toEqual(["backoff", "checkout", "key", "ledger", "provider", "retry", "test"]);
    expect(edgeBetween(after, "retry", "backoff")).toBeUndefined();
    expect(edgeBetween(before, "retry", "key")).toBeUndefined();
    expect(edgeBetween(before, "retry", "ledger")).toBeDefined();
    // Positions come from both sides at once, so a card keeps its place across the toggle.
    const retryBefore = before.cards.find((item) => item.id === "retry")!;
    const retryAfter = after.cards.find((item) => item.id === "retry")!;
    expect([retryBefore.column, retryBefore.row]).toEqual([retryAfter.column, retryAfter.row]);
  });

  it("walks observed calls in recorded order first, then the rest of the change in reading order", () => {
    const after = lensView(diagram, "after");
    expect(after.steps.map((step) => step.cardId)).toEqual(["checkout", "retry", "key", "provider", "settle", "test"]);
    expect(after.steps.map((step) => step.basis)).toEqual(["observed", "observed", "observed", "observed", "reading", "reading"]);
    expect(after.steps[2]).toMatchObject({ edgeKey: edgeBetween(after, "retry", "key")!.key, label: "Observed #2 · retry.py → idempotency.py" });
    expect(after.steps[4]).toMatchObject({ edgeKey: edgeBetween(after, "retry", "settle")!.key, label: "Static · retry.py → settlement_queue.py" });
    expect(after.steps[5].edgeKey).toBeNull();
    const before = lensView(diagram, "before");
    expect(before.steps.map((step) => step.cardId)).toEqual(["checkout", "retry", "backoff", "provider", "key", "test"]);
    // An unchanged neighbour no run observed is drawn, not walked.
    expect(after.steps.some((step) => step.cardId === "ledger")).toBe(false);
  });

  it("says Before has no base comparison when no relationship carries a base comparison, and draws no Before arrow", () => {
    const headOnly: ReviewMapGraph = { ...graph, edges: graph.edges.map(({ delta_status: _d, base_weight: _b, snapshot_weight: _s, ...edge }) => edge) };
    const lens = buildChangeLens({ graph: headOnly, flow: null });
    const before = lensView(lens, "before");
    expect(before.edges).toEqual([]);
    expect(before.caveat?.text).toContain("Before has no base comparison");
    expect(lens.notMeasured.map((item) => item.key)).toEqual(["base_side", "observed_order", "payloads"]);
  });

  it("with no recorded run draws every arrow dashed and offers one way to more evidence", () => {
    const auth = INVESTIGATION_MAP_FIXTURES.authRefreshMap();
    const view = lensView(buildChangeLens({ graph: auth.graph, flow: auth.flow }), "after");
    expect(view.edges.length).toBeGreaterThan(0);
    expect(view.edges.every((edge) => edgeStyle(edge).line === "dashed")).toBe(true);
    expect(view.caveat).toEqual({ text: "No recorded run on After: every arrow is a static code relationship.", action: "data_path" });
    expect(view.steps.every((step) => step.basis === "reading")).toBe(true);
  });

  it("is stale when the Snapshot moved on, and empty when nothing changed", () => {
    expect(buildChangeLens({ graph, flow, walkSnapshot: "review-pay@rev-pay-0" }).status).toBe("stale");
    expect(buildChangeLens({ graph: { ...graph, nodes: [], edges: [] } }).status).toBe("missing_root");
  });
});

function bigGraph(files: number): ReviewMapGraph {
  const dirs = ["frontend/src/pages", "src/api/routes", "src/services", "src/db", "tests"];
  const nodes: ReviewMapNode[] = [];
  const edges: ReviewMapEdge[] = [];
  for (let index = 0; index < files; index += 1) {
    const dir = dirs[index % dirs.length];
    const name = dir === "tests" ? `test_mod_${index}.py` : `mod_${index}.py`;
    nodes.push(fixtureMapNode(ids, `n${index}`, `${dir}/${name}`, { delta_status: "changed", category: dir === "tests" ? "test" : "production" }));
    if (index >= 1) edges.push(fixtureMapEdge(ids, `n${index - 1}`, `n${index}`, { delta_status: "unchanged", base_weight: 1, snapshot_weight: 1 }));
    if (index >= 7) edges.push(fixtureMapEdge(ids, `n${index - 7}`, `n${index}`, { delta_status: "added", base_weight: 0, snapshot_weight: 1 }));
  }
  for (let index = 0; index < 8; index += 1) nodes.push(fixtureMapNode(ids, `docs${index}`, `docs/page_${index}.md`, { delta_status: "changed", category: "docs" }));
  return { level: "architecture", focus: null, nodes, edges, truncated: false };
}

describe("scale", () => {
  it("folds a 120-file change into bounded lanes with +N more cards, deterministically", () => {
    const graph = bigGraph(120);
    const diagram = buildChangeLens({ graph });
    expect(buildChangeLens({ graph })).toEqual(diagram);
    const lanes = new Map<string, LensDiagram["cards"]>();
    for (const item of diagram.cards) lanes.set(item.lane, [...(lanes.get(item.lane) ?? []), item]);
    for (const [lane, cards] of lanes) {
      const plain = cards.filter((item) => item.kind === "node");
      expect(plain.length, lane).toBeLessThanOrEqual(lane === "tests" ? 6 : lane === "other" ? 12 : LENS_LANE_CAP);
    }
    const folds = diagram.cards.filter((item) => item.kind === "fold");
    expect(folds.length).toBeGreaterThan(0);
    const drawn = diagram.cards.filter((item) => item.kind === "node").length + folds.reduce((sum, item) => sum + (item.folded?.count ?? 0), 0);
    expect(drawn).toBe(128);
    expect(folds.every((item) => item.title === `+${item.folded?.count} more`)).toBe(true);
    expect(diagram.bounded.some((line) => line.includes("folded into"))).toBe(true);
    const view = lensView(diagram, "after");
    expect(view.edges.length).toBeLessThanOrEqual(LENS_EDGE_CAP);
    // Arrows into a fold stand for several relationships and are never drawn as observed.
    expect(view.edges.filter((edge) => edge.fold).every((edge) => !edge.observed)).toBe(true);
    const geometry = layoutLens(diagram, 1080);
    expect(geometry.width).toBeGreaterThanOrEqual(1080);
    expect(geometry.height).toBeLessThan(1400);
    expect(edgePaths(view.edges, geometry, view.cards).size).toBe(view.edges.length);
  });

  it("draws a one-file change as one card with nothing to caveat", () => {
    const graph: ReviewMapGraph = { level: "architecture", focus: null, truncated: false, nodes: [fixtureMapNode(ids, "only", "src/payments/retry.py", { delta_status: "changed" })], edges: [] };
    const diagram = buildChangeLens({ graph });
    const view = lensView(diagram, "after");
    expect(view.cards.map((item) => item.id)).toEqual(["only"]);
    expect(view.edges).toEqual([]);
    expect(view.caveat).toBeNull();
    expect(view.steps.map((step) => step.cardId)).toEqual(["only"]);
    const geometry = layoutLens(diagram, 1080);
    const box = geometry.boxes.get("only")!;
    // Centred, not stranded in a corner.
    expect(Math.abs(box.x + box.w / 2 - geometry.width / 2)).toBeLessThan(2);
  });
});

describe("layoutLens", () => {
  it("keeps cards inside their lane and apart from each other", () => {
    const { graph, flow } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const diagram = buildChangeLens({ graph, flow });
    const geometry = layoutLens(diagram, 1080);
    const boxes = [...geometry.boxes.values()];
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i];
        const b = boxes[j];
        const apart = a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
        expect(apart).toBe(true);
      }
    }
    for (const item of diagram.cards.filter((card) => card.place === "main")) {
      const lane = geometry.lanes.find((candidate) => candidate.id === item.lane)!;
      const box = geometry.boxes.get(item.id)!;
      expect(box.x).toBeGreaterThanOrEqual(lane.x);
      expect(box.x + box.w).toBeLessThanOrEqual(lane.x + lane.w);
    }
    expect(layoutLens(diagram, 1080)).toEqual(geometry);
  });
});

describe("buildFocusLens", () => {
  it("zooms into one file's path: callers, the file, then what it uses, on both sides", () => {
    const { graph, flow, fixture } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const diagram = buildFocusLens({ graph, flow, targets: fixture.targets, rootId: "retry" });
    expect(diagram).toMatchObject({ mode: "focus", status: "ready", rootId: "retry", rootTitle: "retry.py" });
    expect(diagram.columns.map((column) => column.label)).toEqual(["Reached from", "This file", "Calls / uses"]);
    const card = (id: string) => diagram.cards.find((item) => item.id === id)!;
    expect(card("checkout").column).toBe(0);
    expect(card("retry").column).toBe(1);
    expect(card("ledger").column).toBe(2);
    const after = lensView(diagram, "after");
    const before = lensView(diagram, "before");
    expect(after.cards.some((item) => item.id === "settle")).toBe(true);
    expect(after.cards.some((item) => item.id === "backoff")).toBe(false);
    expect(before.cards.some((item) => item.id === "backoff")).toBe(true);
    expect(before.cards.some((item) => item.id === "settle")).toBe(false);
    expect(edgeStyle(edgeBetween(after, "retry", "key")!)).toMatchObject({ line: "solid", glyph: "+" });
    expect(edgeStyle(edgeBetween(after, "checkout", "retry")!).line).toBe("solid");
    expect(walkthroughOrder(after, "focus").map((step) => step.cardId).slice(0, 4)).toEqual(["checkout", "retry", "key", "provider"]);
  });

  it("says when the file is not on a side", () => {
    const { graph, flow } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const diagram = buildFocusLens({ graph, flow, rootId: "settle" });
    expect(lensView(diagram, "before")).toMatchObject({ status: "absent", cards: [] });
    expect(lensView(diagram, "after").status).toBe("ready");
  });
});
