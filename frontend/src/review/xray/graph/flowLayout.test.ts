import { describe, expect, it } from "vitest";

import { PRICING_CHANGE, SCALE_CHANGE } from "./fixtures";
import { layoutFlow } from "./flowLayout";
import { overlaps } from "./geometry";

const nodes = (graph = PRICING_CHANGE) => new Map(graph.nodes.map((node) => [node.id, node]));

describe("layoutFlow", () => {
  it("is deterministic and keeps the participants in their order", () => {
    const flow = PRICING_CHANGE.flows[0];
    const layout = layoutFlow(flow, nodes());
    expect(JSON.stringify(layoutFlow(flow, nodes()))).toBe(JSON.stringify(layout));
    expect(layout.columns.map((column) => column.node)).toEqual(flow.participants.map((p) => p.node));
    for (let i = 1; i < layout.columns.length; i += 1) expect(layout.columns[i].head.x).toBeGreaterThan(layout.columns[i - 1].head.x + layout.columns[i - 1].head.w);
  });

  it("stacks steps top to bottom in order, with no two rows overlapping", () => {
    for (const flow of [...PRICING_CHANGE.flows, ...SCALE_CHANGE.flows]) {
      const layout = layoutFlow(flow, new Map([...nodes(PRICING_CHANGE), ...nodes(SCALE_CHANGE)]));
      expect(layout.messages.map((m) => m.id)).toEqual(flow.steps.map((s) => s.id));
      for (let i = 1; i < layout.messages.length; i += 1) {
        expect(layout.messages[i].y).toBeGreaterThan(layout.messages[i - 1].y);
        expect(overlaps(layout.messages[i].box, layout.messages[i - 1].box)).toBe(false);
      }
    }
  });

  it("points a call at its target and a return back at its caller, open-headed", () => {
    const layout = layoutFlow(PRICING_CHANGE.flows[0], nodes());
    const x = new Map(layout.columns.map((column) => [column.node, column.lifeline.x]));
    const call = layout.messages.find((m) => m.id === "s_request")!;
    const back = layout.messages.find((m) => m.id === "s_response")!;
    expect(call.points[0].x).toBeLessThan(call.points[call.points.length - 1].x);
    expect(back.points[0].x).toBeGreaterThan(back.points[back.points.length - 1].x);
    expect(Math.abs(back.points[back.points.length - 1].x - x.get("ui_pricing")!)).toBeLessThan(12);
    expect(back.open).toBe(true);
    expect(call.open).toBe(false);
  });

  it("loops a self step and shows its repeat count", () => {
    const layout = layoutFlow(PRICING_CHANGE.flows[0], nodes());
    const self = layout.messages.find((m) => m.id === "s_render")!;
    expect(self.points.length).toBeGreaterThanOrEqual(4);
    expect(self.label.repeat).toBe(4);
  });
});
