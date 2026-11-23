import { describe, expect, it } from "vitest";

import { MINIMAL_CHANGE, PRICING_CHANGE, SCALE_CHANGE } from "./fixtures";
import { contractProblems } from "./xrayGraphContract";

describe("X-Ray graph fixtures", () => {
  it.each([
    ["pricing change", PRICING_CHANGE],
    ["scale change", SCALE_CHANGE],
    ["minimal change", MINIMAL_CHANGE],
  ])("%s follows every producer rule", (_name, graph) => {
    expect(contractProblems(graph)).toEqual([]);
  });

  it("rejects a recorded step that names a run outside provenance", () => {
    const flows = PRICING_CHANGE.flows.map((flow, i) => (i ? flow : { ...flow, steps: flow.steps.map((step, j) => (j ? step : { ...step, recorded: { ref: "run_missing", count: 1 } })) }));
    expect(contractProblems({ ...PRICING_CHANGE, flows })).toEqual(["flow f_pricing step s_request names run run_missing, which is not in provenance"]);
  });

  it("has the sizes the screenshots need", () => {
    expect(PRICING_CHANGE.walkthrough?.steps).toHaveLength(5);
    expect(SCALE_CHANGE.nodes).toHaveLength(45);
    expect(SCALE_CHANGE.lanes).toHaveLength(4);
    expect(SCALE_CHANGE.flows).toHaveLength(3);
    expect(MINIMAL_CHANGE.nodes).toHaveLength(1);
  });

  it("records the Team price going from 49 to 59", () => {
    const step = PRICING_CHANGE.flows[0].steps.find((s) => s.id === "s_request")!;
    const response = step.payload!.response!;
    const team = (side: unknown) => (side as { plans: { name: string; monthly: number }[] }).plans.find((plan) => plan.name === "Team")!.monthly;
    expect([team(response.before), team(response.after)]).toEqual([49, 59]);
    expect(response.evidence).toBe("recorded");
  });
});
