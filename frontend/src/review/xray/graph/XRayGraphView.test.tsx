import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MINIMAL_CHANGE, PRICING_CHANGE, withFlowLocalStepIds } from "./fixtures";
import XRayGraphView, { type XRayGraphViewProps } from "./XRayGraphView";

const G = PRICING_CHANGE;
const steps = G.walkthrough!.steps;

function show(props: Partial<XRayGraphViewProps> = {}) {
  const onOpenAnchor = vi.fn();
  const utils = render(<XRayGraphView graph={G} onOpenAnchor={onOpenAnchor} layout="wide" {...props} />);
  return { ...utils, onOpenAnchor };
}

const card = (id: string) => document.querySelector(`[data-testid="xg-card"][data-node-id="${id}"]`) as SVGGElement | null;
const edge = (id: string) => document.querySelector(`[data-testid="xg-edge"][data-edge-id="${id}"]`) as SVGGElement | null;

afterEach(cleanup);

describe("XRayGraphView", () => {
  it("draws the overview with its tabs, one legend row and one caveat line", () => {
    show();
    expect(screen.getAllByTestId("xg-view-tab").map((tab) => tab.textContent)).toEqual(["Whole change", "Price path", "Review counts", "Load the pricing pageflow", "Open the overviewflow"]);
    expect(screen.getAllByTestId("xg-card")).toHaveLength(G.nodes.length);
    expect(screen.getAllByTestId("xg-edge")).toHaveLength(G.edges.length);
    expect(screen.getAllByTestId("xg-legend")).toHaveLength(1);
    expect(screen.getAllByTestId("xg-caveat")).toHaveLength(1);
    expect(screen.getByTestId("xg-caveat")).toHaveTextContent(G.degraded[0]);
    expect(edge("e_pricing_plans")).toHaveAttribute("data-line", "solid");
    expect(edge("e_overview_content")).toHaveAttribute("data-line", "dashed");
    expect(edge("e_overview_summary_old")).toHaveAttribute("data-ghost", "true");
  });

  it("filters Before and After without moving what stays", async () => {
    show();
    const place = () => card("api_plans")!.getAttribute("transform");
    const at = place();
    await userEvent.click(screen.getByRole("radio", { name: "Before" }));
    expect(card("api_summary")).toBeNull();
    expect(card("api_summary_old")).not.toBeNull();
    expect(place()).toBe(at);
    await userEvent.click(screen.getByRole("radio", { name: "After" }));
    expect(card("api_summary_old")).toBeNull();
    expect(edge("e_overview_summary")).not.toBeNull();
    expect(place()).toBe(at);
  });

  it("opens an arrow's recorded payload diff and the behavior change it names", async () => {
    show({ renderBehavior: (key) => <span data-testid="behavior-row">{key}</span> });
    await userEvent.click(edge("e_pricing_plans")!);
    const inspector = screen.getByTestId("xg-inspector");
    expect(within(inspector).getByTestId("xg-inspector-title")).toHaveTextContent("fetch plans");
    expect(within(inspector).getByTestId("xg-evidence-text")).toHaveTextContent(/Recorded in a run/);
    expect(within(inspector).getByTestId("xg-diff-summary")).toHaveTextContent("1 changed · 1 added");
    const monthly = within(inspector).getAllByTestId("xg-diff-row").find((row) => row.getAttribute("data-kind") === "changed" && /"monthly"/.test(row.textContent ?? ""))!;
    expect(monthly.querySelector(".xg-diff-was")).toHaveTextContent("49");
    expect(monthly).toHaveTextContent(/59$/);
    expect(within(inspector).getByTestId("xg-behavior")).toHaveAttribute("data-key", "api:GET /api/plans");
    expect(within(inspector).getByTestId("behavior-row")).toHaveTextContent("api:GET /api/plans");
    await userEvent.click(within(inspector).getByTestId("xg-payload-tab-request"));
    expect(within(inspector).getByTestId("xg-diff-summary")).toHaveTextContent("Same on both sides.");
  });

  it("says so when a payload's behavior change is not listed, and opens a card's code", async () => {
    const { onOpenAnchor } = show({ renderBehavior: () => null });
    await userEvent.click(edge("e_pricing_plans")!);
    expect(screen.getByTestId("xg-behavior")).toHaveTextContent("No behavior change is listed for api:GET /api/plans on this revision.");
    await userEvent.click(card("api_plans")!);
    await userEvent.click(within(screen.getByTestId("xg-anchors")).getAllByRole("button")[0]);
    expect(onOpenAnchor).toHaveBeenCalledWith(G.nodes.find((n) => n.id === "api_plans")!.anchors[0]);
  });

  it("draws a flow as steps and selects a step", async () => {
    show();
    await userEvent.click(screen.getByRole("tab", { name: /Load the pricing page/ }));
    expect(screen.getByTestId("xg-flow")).toBeInTheDocument();
    expect(screen.getAllByTestId("xg-step").map((s) => s.getAttribute("data-step-id"))).toEqual(G.flows[0].steps.map((s) => s.id));
    await userEvent.click(document.querySelector('[data-step-id="s_response"]')!);
    expect(screen.getByTestId("xg-inspector")).toHaveAttribute("data-kind", "step");
    expect(screen.getByTestId("xg-status")).toHaveTextContent("200 → 200");
  });

  it("keeps a step to its own flow when two flows share step ids", async () => {
    const S = withFlowLocalStepIds(G);
    show({ graph: S });
    await userEvent.click(screen.getByRole("tab", { name: /Open the overview/ }));
    await userEvent.click(document.querySelector('[data-step-id="s1"]')!);
    const title = () => screen.getByTestId("xg-inspector-title");
    expect(title()).toHaveTextContent("GET /api/review-summary");
    expect(within(screen.getByTestId("xg-inspector")).getByLabelText("Ends")).toHaveTextContent(/From\s*OverviewPage/);
    expect(screen.getByTestId("xg-payload-panel")).toHaveTextContent("ReviewSummary");
    expect(document.querySelector('[data-step-id="s1"]')).toHaveAttribute("data-selected", "true");

    // An arrow's steps link to their own flow only.
    await userEvent.click(screen.getByRole("tab", { name: "Whole change" }));
    await userEvent.click(edge("e_overview_summary")!);
    const links = within(screen.getByTestId("xg-payload")).getAllByRole("button", { name: /^Step / });
    expect(links.map((link) => link.textContent)).toEqual(["Step 1 of “Open the overview”"]);
    await userEvent.click(links[0]);
    expect(screen.getByTestId("xg-flow")).toBeInTheDocument();
    expect(title()).toHaveTextContent("GET /api/review-summary");
    cleanup();

    // The walkthrough lights the staged flow's steps.
    show({ graph: S, initialTourStep: 2 });
    expect(document.querySelector('[data-flow-id="f_pricing"][data-step-id="s2"]')).toHaveAttribute("data-dim");
    expect(document.querySelector('[data-flow-id="f_pricing"][data-step-id="s1"]')).not.toHaveAttribute("data-dim");
  });

  it("names only the run that observed a step, and lists no run when the step names none", async () => {
    const head = G.provenance.recorded_runs.find((run) => run.side === "head")!;
    const graph = {
      ...G,
      provenance: {
        ...G.provenance,
        recorded_runs: [
          ...G.provenance.recorded_runs.map((run) => ({ ...run, recipe: run === head ? "Plans.bru" : "ReviewSummary.bru" })),
          { ...head, ref: "run_settings", recipe: "Settings.bru" },
        ],
      },
      flows: G.flows.map((flow) => ({ ...flow, steps: flow.steps.map((step) => (step.id === "s_read" ? { ...step, recorded: undefined } : step)) })),
    };
    show({ graph, initialView: "v_flow_pricing" });
    const text = () => screen.getByTestId("xg-evidence-text").textContent ?? "";
    await userEvent.click(document.querySelector('[data-step-id="s_response"]')!);
    expect(text()).toMatch(/^Recorded in a run: seen once in Plans\.bru \(after the change, /);
    expect(text()).not.toMatch(/ReviewSummary|Settings/);
    await userEvent.click(document.querySelector('[data-step-id="s_read"]')!);
    expect(text()).toBe("Recorded in a run: this step was observed in a run on this revision.");
  });

  it("walks through with W, the arrows and Escape, dimming what is out of focus", async () => {
    show();
    fireEvent.keyDown(window, { key: "w" });
    expect(screen.getByTestId("xg-caption-heading")).toHaveTextContent(steps[0].heading);
    expect(card("ui_pricing")).not.toHaveAttribute("data-dim");
    expect(card("data_content")).toHaveAttribute("data-dim");
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.getByTestId("xg-caption-heading")).toHaveTextContent(steps[1].heading);
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.getByTestId("xg-caption-heading")).toHaveTextContent(steps[2].heading);
    expect(screen.getByTestId("xg-flow")).toBeInTheDocument();
    expect(document.querySelector('[data-step-id="s_read"]')).toHaveAttribute("data-dim");
    expect(document.querySelector('[data-step-id="s_request"]')).not.toHaveAttribute("data-dim");
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(screen.getByTestId("xg-caption-heading")).toHaveTextContent(steps[1].heading);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("xg-caption")).toBeNull();
    expect(card("data_content")).not.toHaveAttribute("data-dim");
  });

  it("ends the walkthrough with Done on the last step", async () => {
    show({ initialTourStep: steps.length - 1 });
    expect(screen.getByTestId("xg-tour-next")).toHaveTextContent("Done");
    await userEvent.click(screen.getByTestId("xg-tour-next"));
    expect(screen.queryByTestId("xg-caption")).toBeNull();
  });

  it("offers Draw with AI only for an index drawing, and shows why it could not", async () => {
    const onDrawWithAI = vi.fn(async () => ({ ok: false as const, reason: "Review AI is off for this workspace." }));
    const { unmount } = show({ graph: MINIMAL_CHANGE, onDrawWithAI });
    expect(screen.getByTestId("xg-caveat")).toHaveTextContent(/No run was recorded/);
    await userEvent.click(screen.getByTestId("xg-draw-ai"));
    expect(onDrawWithAI).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("xg-ai-reason")).toHaveTextContent("Review AI is off for this workspace.");
    unmount();
    show({ onDrawWithAI });
    expect(screen.queryByTestId("xg-draw-ai")).toBeNull();
  });

  it("stacks the picture as a list on a narrow screen, with the same inspector", async () => {
    show({ layout: "narrow" });
    expect(screen.queryByTestId("xg-canvas")).toBeNull();
    const plans = screen.getAllByTestId("xg-stack-card").find((b) => /GET \/api\/plans/.test(b.textContent ?? ""))!;
    await userEvent.click(plans);
    await waitFor(() => expect(screen.getByTestId("xg-inspector")).toBeInTheDocument());
    expect(screen.getByTestId("xg-inspector-title")).toHaveTextContent("GET /api/plans");
  });
});
