import type { XRayGraph } from "../xrayGraphTypes";

export { PRICING_CHANGE } from "./pricingChange";
export { SCALE_CHANGE } from "./scaleChange";
export { MINIMAL_CHANGE } from "./minimalChange";

/**
 * The same document with each flow's steps renumbered s1, s2, … as the backend numbers them, so step ids repeat across
 * flows (the contract only makes them unique within a flow). The walkthrough's messages follow the renumbering.
 */
export function withFlowLocalStepIds(graph: XRayGraph): XRayGraph {
  const renamed = new Map<string, Map<string, string>>();
  const flows = graph.flows.map((flow) => {
    const ids = new Map(flow.steps.map((step, index) => [step.id, `s${index + 1}`]));
    renamed.set(flow.id, ids);
    return { ...flow, steps: flow.steps.map((step) => ({ ...step, id: ids.get(step.id)! })) };
  });
  const walkthrough = graph.walkthrough && {
    steps: graph.walkthrough.steps.map((step) => {
      if (step.stage.kind !== "flow" || step.focus.kind === "all" || !step.focus.messages) return step;
      const ids = renamed.get(step.stage.flow);
      return { ...step, focus: { ...step.focus, messages: step.focus.messages.map((id) => ids?.get(id) ?? id) } };
    }),
  };
  return { ...graph, flows, walkthrough };
}
