import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewTourScreen, { type ReviewTourScreenProps } from "./ReviewTourScreen";
import type { ReviewArchitectureDelta, ReviewMapNode, ReviewOverview, ReviewTarget, ReviewTourProjection } from "./types";

function target(id: string, symbol: string, path: string): ReviewTarget {
  return {
    target_id: id, unit_key: id, kind: "symbol", path, label: symbol, symbol, start_line: 10, end_line: 20,
    hunk_ordinals: [0], spans: [], state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "",
    attention_rank: 1, attention_level: "high", reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 0 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "decision", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "review_first", outstanding: true, position: 0, reasons: [] },
  } as ReviewTarget;
}

const targets = [target("t-api", "handle", "src/api.ts"), target("t-db", "save", "src/db.ts")];
const overview = {
  title: "Persist orders",
  session: { title: "Persist orders" },
  revision: { id: "rev-1", revision_number: 1, provenance_host: "claude_code" },
} as unknown as ReviewOverview;

function step(key: string, title: string, target: ReviewTarget, position: number): ReviewTourProjection["steps"][number] {
  return {
    id: `tour:${key}`, semantic_key: key, title, why: "Review the changed implementation.", why_origin: "deterministic",
    target_ids: [target.target_id], stale_target_ids: [], change_type: "changed", signals: [], claim_ids: [], decision_refs: [],
    evidence_refs: [], stale: false, synthesis_needed: false, position,
    implementation: { target_id: target.target_id, path: target.path, symbol: target.symbol, start_line: 10, end_line: 20 },
  };
}

const TOUR: ReviewTourProjection = {
  schema_version: 1,
  step_count: 2,
  synthesis: { status: "none", mode: "deterministic_only", candidates: 0 },
  steps: [step("api", "API contract", targets[0], 0), step("db", "Persistence", targets[1], 1)],
};

function mapNode(id: string, path: string, status: ReviewMapNode["status"]): ReviewMapNode {
  return {
    id, label: path, kind: "file", path, line: 0, end_line: 0, status, exactness: "snapshot", target_ids: [],
    important_public_contract: false, boundaries: [], boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "rev-1", node_id: id, path, line: 12, side: "new", exactness: "snapshot" },
  };
}

function delta(status: ReviewMapNode["status"]): ReviewArchitectureDelta {
  return {
    graph: { level: "architecture", focus: null, truncated: false, nodes: [mapNode("db", "src/db.ts", status)], edges: [] },
    flow: null,
  } as unknown as ReviewArchitectureDelta;
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function show(props: Partial<ReviewTourScreenProps> = {}, initial = "/r/shop/walkthrough") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ReviewTourScreen reviewRef="shop" apiReviewId="r-1" step="" overview={overview} targets={targets} tour={TOUR} {...props} />
      <Where />
    </MemoryRouter>,
  );
}

const where = () => screen.getByTestId("where").textContent;
const onTheMap = () => screen.getByRole("radio", { name: "On the map" });

beforeEach(() => window.sessionStorage.clear());
afterEach(cleanup);

describe("the walkthrough screen", () => {
  it("lands its steps on the flow: Focus for the part, the Map's Dependencies lens for its place, the address for the step", () => {
    show();
    expect(screen.getByTestId("flow-tour-screen")).toBeInTheDocument();
    expect(screen.getByText(/Walkthrough · step 1 of 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open this part" }));
    expect(where()).toBe("/r/shop/one/handle");

    fireEvent.click(screen.getByRole("button", { name: "Show on the Map" }));
    expect(where()).toBe("/r/shop/map?lens=dependencies&view=changes");

    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(where()).toBe("/r/shop/walkthrough/db");
  });

  it("opens the step the address names, and Back returns to the Overview", () => {
    show({ step: "db" }, "/r/shop/walkthrough/db");
    expect(screen.getByText(/Walkthrough · step 2 of 2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Overview/ }));
    expect(where()).toBe("/r/shop");
  });

  it("says when there is no walkthrough instead of an empty screen", () => {
    show({ tour: null, loading: true });
    expect(screen.getByTestId("tour-unavailable")).toHaveTextContent("Preparing the walkthrough…");
    cleanup();
    show({ tour: null });
    expect(screen.getByTestId("tour-unavailable")).toHaveTextContent("No walkthrough is available for this revision.");
    fireEvent.click(screen.getByRole("button", { name: "Back to the Overview" }));
    expect(where()).toBe("/r/shop");
  });

  it("offers the map view only where the Reader can load the change map", () => {
    show();
    expect(screen.queryByRole("group", { name: "Walkthrough view" })).toBeNull();
    expect(screen.queryByText(/Change Story/)).toBeNull();
  });

  it("plays the same walkthrough on the map, with no second name, no order disclaimer and no marked counters; Escape returns to the steps", async () => {
    const loadStory = vi.fn().mockResolvedValue(delta("changed"));
    show({ loadStory });
    fireEvent.click(onTheMap());
    expect(where()).toBe("/r/shop/walkthrough?view=map");
    expect(loadStory).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("change-story-stage")).toBeInTheDocument();
    expect(screen.getByTestId("change-story-location")).toHaveTextContent("src/db.ts");
    expect(screen.getByRole("heading", { name: "Walkthrough" })).toBeInTheDocument();
    expect(screen.queryByText(/Change Story/)).toBeNull();
    expect(screen.queryByText(/runtime order/)).toBeNull();
    expect(screen.getByTestId("change-story-stage").textContent).not.toMatch(/\d+\/\d+/);

    fireEvent.keyDown(screen.getByTestId("change-story"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("change-story-stage")).toBeNull());
    expect(screen.getByText(/Walkthrough · step 1 of 2/)).toBeInTheDocument();
    expect(where()).toBe("/r/shop/walkthrough");
  });

  it("opens on the map when the address asks for it", async () => {
    show({ loadStory: vi.fn().mockResolvedValue(delta("changed")), view: "map" }, "/r/shop/walkthrough?view=map");
    expect(await screen.findByTestId("change-story-stage")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Steps" }));
    await waitFor(() => expect(screen.queryByTestId("change-story-stage")).toBeNull());
    expect(where()).toBe("/r/shop/walkthrough");
  });

  it("opens a story step's source in Focus, or on the Map when no part holds it", async () => {
    show({ loadStory: vi.fn().mockResolvedValue(delta("changed")) });
    fireEvent.click(onTheMap());
    fireEvent.click(await screen.findByRole("button", { name: /Open source/ }));
    expect(where()).toBe("/r/shop/one/save");
    cleanup();

    const elsewhere = delta("changed");
    elsewhere.graph.nodes = [mapNode("cfg", "src/config.ts", "changed")];
    show({ loadStory: vi.fn().mockResolvedValue(elsewhere) });
    fireEvent.click(onTheMap());
    fireEvent.click(await screen.findByRole("button", { name: /Open source/ }));
    expect(where()).toBe("/r/shop/map?lens=dependencies&view=changes");
  });

  it("says when the change map lists no changed file, or could not load", async () => {
    show({ loadStory: vi.fn().mockResolvedValue(delta("context")) });
    fireEvent.click(onTheMap());
    expect(await screen.findByText(/lists no changed file, so there is no change to walk through/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to the steps" }));
    expect(screen.getByText(/Walkthrough · step 1 of 2/)).toBeInTheDocument();
    cleanup();

    const failing = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(delta("changed"));
    show({ loadStory: failing });
    fireEvent.click(onTheMap());
    expect(await screen.findByText("The change map could not load. Try again.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("change-story-stage")).toBeInTheDocument();
    expect(failing).toHaveBeenCalledTimes(2);
  });
});
