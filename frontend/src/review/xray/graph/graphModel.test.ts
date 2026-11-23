import { describe, expect, it } from "vitest";

import { layoutArchitecture } from "./archLayout";
import { MINIMAL_CHANGE, PRICING_CHANGE, withFlowLocalStepIds } from "./fixtures";
import { layoutFlow } from "./flowLayout";
import { contains } from "./geometry";
import {
  caveatLine,
  edgeLook,
  filterGraph,
  focusBox,
  hitArchitecture,
  hitFlow,
  onSide,
  payloadForEdge,
  pulseOrder,
  resolveTourStep,
  sameHit,
  stepKey,
  stepLook,
  viewContent,
  viewTabs,
} from "./graphModel";
import { contractProblems } from "./xrayGraphContract";

const G = PRICING_CHANGE;
const ids = (items: readonly { id: string }[]) => items.map((item) => item.id);

describe("Before and After", () => {
  it("follows the contract's rule for each delta", () => {
    expect(["added", "modified", "removed", "unchanged"].map((d) => onSide(d as never, "before"))).toEqual([false, true, true, true]);
    expect(["added", "modified", "removed", "unchanged"].map((d) => onSide(d as never, "after"))).toEqual([true, true, false, true]);
  });

  it("drops new parts before the change and removed parts after it, with their arrows and steps", () => {
    const before = filterGraph(G, "before");
    expect(ids(before.nodes)).toContain("api_summary_old");
    expect(ids(before.nodes)).not.toContain("api_summary");
    expect(ids(before.edges)).not.toContain("e_overview_summary");
    expect(ids(before.flows[1].steps)).not.toContain("o_counts");
    const after = filterGraph(G, "after");
    expect(ids(after.nodes)).not.toContain("api_summary_old");
    expect(ids(after.edges)).not.toContain("e_overview_summary_old");
    expect(ids(after.edges)).toContain("e_overview_summary");
    expect(filterGraph(G, "both")).toBe(G);
  });

  it("keeps a view's lanes when one side empties a lane, so nothing moves", () => {
    const view = G.views[0].children!.find((v) => v.id === "v_counts")!;
    const both = viewContent(view, G, G);
    const after = viewContent(view, filterGraph(G, "after"), G);
    expect(both.lens === "architecture" && after.lens === "architecture" && ids(after.lanes)).toEqual(both.lens === "architecture" && ids(both.lanes));
  });
});

describe("line looks", () => {
  it("draws recorded solid with a pulse, static dashed, and an AI guess dashed with a tag", () => {
    expect(edgeLook({ delta: "modified", evidence: "recorded" })).toMatchObject({ tone: "modified", line: "solid", pulse: true, ai: false, ghost: false });
    expect(edgeLook({ delta: "unchanged", evidence: "static" })).toMatchObject({ line: "dashed", pulse: false, ai: false });
    expect(edgeLook({ delta: "unchanged", evidence: "inferred" })).toMatchObject({ line: "dashed", pulse: false, ai: true });
    expect(edgeLook({ delta: "removed", evidence: "recorded" })).toMatchObject({ tone: "removed", ghost: true });
    expect(edgeLook({ delta: "added", evidence: "recorded", hero: true }).hero).toBe(true);
  });

  it("draws a return as a return line whatever its evidence", () => {
    expect(stepLook({ delta: "modified", evidence: "recorded", kind: "return" }).line).toBe("return");
    expect(stepLook({ delta: "modified", evidence: "recorded", kind: "sync" }).line).toBe("solid");
  });

  it("pulses recorded arrows in the order the runs walked them, removed ones only before", () => {
    expect(pulseOrder(G, G.edges, "both")).toEqual(["e_pricing_plans", "e_plans_csv", "e_overview_summary", "e_surface_settings"]);
    expect(pulseOrder(G, G.edges, "before")).toContain("e_overview_summary_old");
  });
});

describe("payloads on arrows", () => {
  it("takes an arrow's request and response from the steps between its two cards", () => {
    const edge = G.edges.find((e) => e.id === "e_pricing_plans")!;
    const payload = payloadForEdge(G, edge)!;
    expect(payload.steps.map((s) => `${s.flow.id}/${s.step.id}`)).toEqual(["f_pricing/s_request", "f_pricing/s_response"]);
    expect(payload.response?.type).toBe("PlansResponse");
    expect(payload.request).toBeDefined();
    expect(payloadForEdge(G, G.edges.find((e) => e.id === "e_app_pricing")!)).toBeNull();
  });
});

describe("walkthrough focus", () => {
  const steps = G.walkthrough!.steps;

  it("stages a view and lights an arrow with its two ends", () => {
    const focus = resolveTourStep(G, steps[0]);
    expect(focus.stage).toMatchObject({ kind: "view", view: { id: "v_overview" } });
    expect([...focus.lit!.nodes].sort()).toEqual(["api_plans", "ui_pricing"]);
    expect([...focus.lit!.edges]).toEqual(["e_pricing_plans"]);
  });

  it("stages a flow through its view and lights the messages and their participants", () => {
    const focus = resolveTourStep(G, steps[2]);
    expect(focus.stage).toMatchObject({ kind: "flow", flow: "f_pricing", view: { id: "v_flow_pricing" } });
    expect([...focus.lit!.steps].sort()).toEqual([stepKey("f_pricing", "s_request"), stepKey("f_pricing", "s_response")].sort());
    expect([...focus.lit!.nodes].sort()).toEqual(["api_plans", "ui_pricing"]);
  });

  it("zooms to the lit cards in the drawn layout, and to the whole picture when nothing lit is drawn", () => {
    const layout = layoutArchitecture({ lanes: G.lanes, nodes: G.nodes, edges: G.edges });
    const focus = resolveTourStep(G, steps[1]);
    const box = focusBox(layout.atlas, focus.lit)!;
    for (const id of focus.lit!.nodes) {
      const card = layout.atlas.nodes[id];
      expect(contains(box, { x: card.x, y: card.y }) && contains(box, { x: card.x + card.w, y: card.y + card.h })).toBe(true);
    }
    expect(focusBox(layout.atlas, { nodes: new Set(["nope"]), edges: new Set(), steps: new Set() })).toBeNull();
    expect(focusBox(layout.atlas, null)).toBeNull();
  });
});

describe("steps whose ids repeat across flows", () => {
  const S = withFlowLocalStepIds(G);
  const nodes = new Map(S.nodes.map((n) => [n.id, n]));

  it("is still a valid document: step ids are unique within a flow", () => {
    expect(S.flows.map((flow) => flow.steps[0].id)).toEqual(["s1", "s1"]);
    expect(contractProblems(S)).toEqual([]);
  });

  it("keys the walkthrough focus, the atlas and hits by flow and step", () => {
    const tour = S.walkthrough!.steps.find((step) => step.stage.kind === "flow")!;
    const focus = resolveTourStep(S, tour);
    expect([...focus.lit!.steps].sort()).toEqual([stepKey("f_pricing", "s1"), stepKey("f_pricing", "s4")].sort());
    const pricing = layoutFlow(S.flows[0], nodes);
    const overview = layoutFlow(S.flows[1], nodes);
    expect(Object.keys(overview.atlas.steps)).toContain(stepKey("f_overview", "s1"));
    // The other flow's s1 is not lit, so nothing there is zoomed to.
    expect(focusBox(overview.atlas, { ...focus.lit!, nodes: new Set() })).toBeNull();
    const row4 = pricing.messages[3].box;
    expect(contains(focusBox(pricing.atlas, focus.lit)!, { x: row4.x, y: row4.y })).toBe(true);
    const row = overview.messages[0].box;
    expect(hitFlow(overview, { x: row.x + row.w / 2, y: row.y + row.h / 2 })).toEqual({ kind: "step", flow: "f_overview", id: "s1" });
    expect(sameHit({ kind: "step", flow: "f_pricing", id: "s1" }, { kind: "step", flow: "f_overview", id: "s1" })).toBe(false);
    expect(sameHit({ kind: "step", flow: "f_overview", id: "s1" }, { kind: "step", flow: "f_overview", id: "s1" })).toBe(true);
  });

  it("takes an arrow's payload from the steps of its own flow", () => {
    const edge = S.edges.find((e) => e.id === "e_overview_summary")!;
    expect(payloadForEdge(S, edge)!.steps.map((s) => `${s.flow.id}/${s.step.id}`)).toEqual(["f_overview/s1"]);
  });
});

describe("views, hits and the caveat", () => {
  it("lists the overview, then its drill-downs, as tabs", () => {
    expect(ids(viewTabs(G.views))).toEqual(["v_overview", "v_price_path", "v_counts", "v_flow_pricing", "v_flow_overview"]);
  });

  it("finds the card, label or step under a point", () => {
    const arch = layoutArchitecture({ lanes: G.lanes, nodes: G.nodes, edges: G.edges });
    const card = arch.cards.find((c) => c.id === "api_plans")!.box;
    expect(hitArchitecture(arch, { x: card.x + 10, y: card.y + 10 })).toEqual({ kind: "node", id: "api_plans" });
    const label = arch.arrows.find((a) => a.id === "e_plans_csv")!.label!.box;
    expect(hitArchitecture(arch, { x: label.x + 4, y: label.y + 4 })).toEqual({ kind: "edge", id: "e_plans_csv" });
    const flow = layoutFlow(G.flows[0], new Map(G.nodes.map((n) => [n.id, n])));
    const row = flow.messages[1].box;
    expect(hitFlow(flow, { x: row.x + row.w / 2, y: row.y + row.h / 2 })).toEqual({ kind: "step", flow: "f_pricing", id: "s_read" });
  });

  it("shows one caveat: the AI pass first, then no recorded run, then the first degraded note", () => {
    expect(caveatLine(G, true)).toMatch(/AI is still adding/);
    expect(caveatLine(MINIMAL_CHANGE, false)).toMatch(/No run was recorded/);
    expect(caveatLine(G, false)).toBe(G.degraded[0]);
    expect(caveatLine({ ...G, degraded: [] }, false)).toBeNull();
  });
});
