import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReviewMapEdge, ReviewMapGraph, ReviewMapNode } from "../types";
import ChangeStory from "./ChangeStory";

function node(id: string, status: ReviewMapNode["status"]): ReviewMapNode {
  const path = `src/${id}.ts`;
  return {
    id,
    label: `${id}.ts`,
    kind: "file",
    path,
    line: 0,
    end_line: 0,
    status,
    exactness: status === "context" ? "current_index" : "snapshot",
    target_ids: [],
    important_public_contract: false,
    boundaries: [],
    boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: id, path, line: 1, side: "new", exactness: "snapshot" },
  };
}

function edge(source: string, target: string, exactness: ReviewMapEdge["exactness"]): ReviewMapEdge {
  const id = `${source}->${target}`;
  return {
    id,
    source,
    target,
    kind: "imports",
    exactness,
    weight: 1,
    uncertain: false,
    selection: { kind: "review_map_edge", review_id: "r", revision_id: "v", edge_id: id, exactness },
  };
}

const GRAPH: ReviewMapGraph = {
  level: "architecture",
  focus: null,
  truncated: false,
  nodes: [node("api", "changed"), node("service", "changed"), node("store", "changed")],
  edges: [edge("api", "service", "snapshot"), edge("service", "store", "inferred")],
};

function renderStory(overrides: Partial<Parameters<typeof ChangeStory>[0]> = {}) {
  const onOpenSource = vi.fn();
  const onClose = vi.fn();
  const onStepChange = vi.fn();
  render(
    <ChangeStory
      graph={GRAPH}
      snapshotChecks={[{ name: "unit", status: "NOT_RUN", detail: "" }]}
      onOpenSource={onOpenSource}
      onClose={onClose}
      onStepChange={onStepChange}
      {...overrides}
    />,
  );
  return { onOpenSource, onClose, onStepChange };
}

describe("ChangeStory", () => {
  it("steps with buttons and arrow keys, opens source, and closes on Escape", async () => {
    const user = userEvent.setup();
    const { onOpenSource, onClose, onStepChange } = renderStory();
    const position = screen.getByTestId("change-story-position");
    expect(position).toHaveTextContent("1 / 3");
    expect(screen.getByTestId("change-story-step")).toHaveTextContent("Entry point");
    expect(screen.getByTestId("change-story-order")).toHaveTextContent("This is not a runtime order.");
    // Whole-Snapshot checks use the Brief's words and are never tied to a step.
    expect(screen.getByTestId("change-story-snapshot-checks")).toHaveTextContent("unit");
    expect(screen.getByTestId("change-story-snapshot-checks")).toHaveTextContent("did not run");

    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(position).toHaveTextContent("2 / 3");
    expect(screen.getByTestId("change-story-exactness")).toHaveTextContent("Snapshot-exact");
    expect(screen.getByTestId("change-story-link-legend")).toHaveTextContent("imports · Snapshot-exact");

    const story = screen.getByTestId("change-story");
    fireEvent.keyDown(story, { key: "ArrowRight" });
    expect(position).toHaveTextContent("3 / 3");
    // An inferred link is labelled in words and drawn still and dashed.
    expect(screen.getByTestId("change-story-link-legend")).toHaveTextContent("imports · inferred · drawn still and dashed");
    expect(screen.getByTestId("change-story-stage").dataset.motion).toBe("static");
    await waitFor(() => expect(onStepChange).toHaveBeenLastCalledWith("store"));

    fireEvent.keyDown(story, { key: "ArrowLeft" });
    expect(position).toHaveTextContent("2 / 3");

    await user.click(screen.getByRole("button", { name: "Open source" }));
    expect(onOpenSource).toHaveBeenCalledWith(expect.objectContaining({ node_id: "service", path: "src/service.ts" }));

    fireEvent.keyDown(story, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("opens the X-Ray on the current step in place of the stage; Escape leaves the X-Ray first", async () => {
    const user = userEvent.setup();
    const onXRayChange = vi.fn();
    const { onClose } = renderStory({ onXRayChange });
    await user.click(screen.getByRole("button", { name: "X-Ray this step" }));
    expect(onXRayChange).toHaveBeenLastCalledWith("after~r@v~api");
    expect(screen.getByTestId("xray")).toBeInTheDocument();
    expect(screen.queryByTestId("change-story-stage")).toBeNull();
    // The X-Ray follows the walkthrough.
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(onXRayChange).toHaveBeenLastCalledWith("after~r@v~service");
    fireEvent.keyDown(screen.getByTestId("change-story"), { key: "Escape" });
    expect(screen.queryByTestId("xray")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("labels itself as the older path when told a newer Snapshot exists", () => {
    renderStory();
    expect(screen.queryByTestId("change-story-stale")).toBeNull();
    cleanup();
    renderStory({ stale: "A newer Snapshot exists. This walkthrough shows Snapshot 2, which is no longer the current path." });
    expect(screen.getByTestId("change-story-stale")).toHaveTextContent("no longer the current path");
    // The steps stay readable; only the label changes.
    expect(screen.getByTestId("change-story-position")).toHaveTextContent("1 / 3");
  });

  it("says when findings could not load instead of showing a clean step", async () => {
    renderStory({ loadEvidence: () => Promise.reject(new Error("offline")) });
    expect(await screen.findByText("Findings could not load, so none are shown. That is not a clean result.")).toBeInTheDocument();
  });
});
