import { describe, expect, it } from "vitest";

import { layoutArchitecture, rankLane, type ArchInput } from "./archLayout";
import { PRICING_CHANGE, SCALE_CHANGE } from "./fixtures";
import { overlaps } from "./geometry";
import type { XRayNode } from "./xrayGraphTypes";

const input = (graph = PRICING_CHANGE): ArchInput => ({ lanes: graph.lanes, nodes: graph.nodes, edges: graph.edges });
const cardsIn = (layout: ReturnType<typeof layoutArchitecture>, lane: string) => layout.cards.filter((card) => card.lane === lane).map((card) => [card.id, card.box]);

describe("layoutArchitecture", () => {
  it("draws the same document the same way, byte for byte", () => {
    const first = JSON.stringify(layoutArchitecture(input()));
    const copy = JSON.parse(JSON.stringify(PRICING_CHANGE)) as typeof PRICING_CHANGE;
    expect(JSON.stringify(layoutArchitecture(input(copy)))).toBe(first);
    expect(JSON.stringify(layoutArchitecture(input(SCALE_CHANGE)))).toBe(JSON.stringify(layoutArchitecture(input(SCALE_CHANGE))));
  });

  it("keeps a lane's cards in place when another lane grows", () => {
    const before = layoutArchitecture(input());
    const extra: XRayNode = { id: "data_extra", label: "extra.csv", kind: "datastore", lane: "data", delta: "added", badges: [], anchors: [], source: "static" };
    const after = layoutArchitecture({ ...input(), nodes: [...PRICING_CHANGE.nodes, extra] });
    expect(cardsIn(after, "web")).toEqual(cardsIn(before, "web"));
    expect(cardsIn(after, "api")).toEqual(cardsIn(before, "api"));
    expect(after.cards.find((card) => card.id === "data_extra")?.lane).toBe("data");
  });

  it("puts every card inside its own lane, in lane order", () => {
    const layout = layoutArchitecture(input(SCALE_CHANGE));
    const lanes = new Map(layout.lanes.map((lane) => [lane.id, lane.box]));
    for (const card of layout.cards) {
      const lane = lanes.get(card.lane)!;
      expect(card.box.x).toBeGreaterThanOrEqual(lane.x);
      expect(card.box.x + card.box.w).toBeLessThanOrEqual(lane.x + lane.w);
      expect(card.box.y + card.box.h).toBeLessThanOrEqual(lane.y + lane.h);
    }
    expect(layout.lanes.map((lane) => lane.id)).toEqual(SCALE_CHANGE.lanes.map((lane) => lane.id));
    for (let i = 1; i < layout.lanes.length; i += 1) expect(layout.lanes[i].box.x).toBeGreaterThan(layout.lanes[i - 1].box.x + layout.lanes[i - 1].box.w);
  });

  it("places no label on a card, a lane title or another label", () => {
    for (const graph of [PRICING_CHANGE, SCALE_CHANGE]) {
      const layout = layoutArchitecture(input(graph));
      const labels = layout.arrows.flatMap((arrow) => (arrow.label ? [arrow.label.box] : []));
      expect(labels.length).toBe(graph.edges.filter((edge) => edge.label || edge.evidence === "inferred").length);
      for (const label of labels) for (const card of layout.cards) expect(overlaps(label, card.box)).toBe(false);
      labels.forEach((a, i) => labels.slice(i + 1).forEach((b) => expect(overlaps(a, b)).toBe(false)));
      for (const label of labels) for (const lane of layout.lanes) expect(overlaps(label, { x: lane.box.x + 8, y: lane.box.y + 8, w: lane.box.w - 16, h: 26 })).toBe(false);
    }
  });

  it("draws the hero arrow last so it reads on top, and every arrow ends at its target card", () => {
    const layout = layoutArchitecture(input());
    const hero = layout.arrows.find((arrow) => arrow.hero)!;
    expect(hero.id).toBe("e_pricing_plans");
    const boxes = new Map(layout.cards.map((card) => [card.id, card.box]));
    for (const arrow of layout.arrows) {
      const tip = arrow.points[arrow.points.length - 1];
      const to = boxes.get(arrow.edge.to)!;
      expect(tip.x >= to.x - 1 && tip.x <= to.x + to.w + 1 && tip.y >= to.y - 1 && tip.y <= to.y + to.h + 1).toBe(true);
    }
  });
});

describe("rankLane", () => {
  it("ranks by the longest path in document order", () => {
    const { rank, back } = rankLane(["a", "b", "c", "d"], [{ from: "a", to: "b" }, { from: "b", to: "c" }, { from: "a", to: "c" }]);
    expect(Object.fromEntries(rank)).toEqual({ a: 0, b: 1, c: 2, d: 0 });
    expect(back.size).toBe(0);
  });

  it("sets aside the arrow that closes a cycle, and a self arrow", () => {
    const { rank, back } = rankLane(["a", "b", "c"], [{ from: "a", to: "b" }, { from: "b", to: "c" }, { from: "c", to: "a" }, { from: "b", to: "b" }]);
    expect([...back]).toEqual([2, 3]);
    expect(Object.fromEntries(rank)).toEqual({ a: 0, b: 1, c: 2 });
  });

  it("ignores arrows that leave the lane", () => {
    const { rank, back } = rankLane(["a", "b"], [{ from: "a", to: "x" }, { from: "x", to: "b" }]);
    expect(Object.fromEntries(rank)).toEqual({ a: 0, b: 0 });
    expect(back.size).toBe(0);
  });
});
