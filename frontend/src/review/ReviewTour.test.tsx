import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewTour from "./ReviewTour";
import type { ReviewTarget, ReviewTourProjection } from "./types";

const targets = [
  { target_id: "t-api", path: "src/api.ts", symbol: "handle", start_line: 10, end_line: 20 },
  { target_id: "t-db", path: "src/db.ts", symbol: "save", start_line: 30, end_line: 40 },
] as ReviewTarget[];

function tour(mode: "deterministic_only" | "unavailable" = "deterministic_only"): ReviewTourProjection {
  return {
    schema_version: 1,
    step_count: 2,
    synthesis: { status: mode === "unavailable" ? "disabled" : "none", mode, candidates: 1 },
    steps: [
      {
        id: "tour:api",
        semantic_key: "api",
        title: "API contract",
        why: "Clients now send two fields.",
        why_origin: "deterministic",
        target_ids: ["t-api"],
        stale_target_ids: [],
        change_type: "changed",
        signals: ["public_contract"],
        claim_ids: ["claim:api"],
        decision_refs: ["decision:shape"],
        evidence_refs: [{
          claim_id: "claim:api",
          key: "contract",
          title: "Contract test",
          status: "PASS",
          state: "current",
          snapshot_number: 2,
        }],
        stale: false,
        synthesis_needed: false,
        position: 0,
        implementation: { target_id: "t-api", path: "src/api.ts", symbol: "handle", start_line: 10, end_line: 20 },
      },
      {
        id: "tour:db",
        semantic_key: "db",
        title: "Persistence",
        why: "Review the changed implementation and its observable effect.",
        why_origin: "deterministic",
        target_ids: ["t-db"],
        stale_target_ids: ["t-old"],
        change_type: "added",
        signals: ["state_persistence"],
        claim_ids: [],
        decision_refs: [],
        evidence_refs: [],
        stale: true,
        synthesis_needed: true,
        position: 1,
        implementation: { target_id: "t-db", path: "src/db.ts", symbol: "save", start_line: 30, end_line: 40 },
      },
    ],
  };
}

function props(overrides: Partial<React.ComponentProps<typeof ReviewTour>> = {}) {
  return {
    reviewId: "r-1",
    revisionId: "rev-1",
    tour: tour(),
    targets,
    onBack: vi.fn(),
    onOpenTarget: vi.fn(),
    onShowOnMap: vi.fn(),
    onAsk: vi.fn(),
    ...overrides,
  };
}

describe("ReviewTour walkthrough", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("is navigation only and opens the exact part", () => {
    const open = vi.fn();
    render(<ReviewTour {...props({ onOpenTarget: open })} />);

    expect(screen.getByText("Walkthrough · step 1 of 2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open this part" }));
    expect(open).toHaveBeenCalledWith("t-api");
    fireEvent.click(screen.getByRole("button", { name: "src/api.ts · handle" }));
    expect(open).toHaveBeenLastCalledWith("t-api");
    expect(screen.getByText("The walkthrough only moves you between parts. Visiting a step never changes what you have reviewed.")).toBeInTheDocument();
    expect(screen.queryByText(/mark.*reviewed/i)).toBeNull();
  });

  it("tells a file's hunks apart by their lines", () => {
    const hunks = [
      { target_id: "t-api", kind: "hunk", path: "src/api.ts", symbol: "", start_line: 10, end_line: 20 },
      { target_id: "t-db", kind: "hunk", path: "src/api.ts", symbol: "", start_line: 40, end_line: 40 },
      { target_id: "t-top", kind: "hunk", path: "src/api.ts", symbol: "", start_line: 1, end_line: 15 },
    ];
    const merged = tour();
    merged.steps[0].target_ids = ["t-api", "t-db", "t-top"];
    render(<ReviewTour {...props({ tour: merged, targets: hunks as unknown as typeof targets })} />);

    expect(screen.getByRole("button", { name: "src/api.ts · lines 10–20" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "src/api.ts · line 40" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "src/api.ts · top of file, lines 1–15" })).toBeInTheDocument();
    expect(screen.getByText("3 parts in this step")).toBeInTheDocument();
  });

  it("supports next/back keyboard reading, Esc back to the Overview and exact Enter navigation", () => {
    const open = vi.fn();
    const back = vi.fn();
    const change = vi.fn();
    render(<ReviewTour {...props({ onOpenTarget: open, onBack: back, onCohortChange: change })} />);

    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByText("Walkthrough · step 2 of 2")).toBeInTheDocument();
    expect(change).toHaveBeenCalledWith("db");
    expect(screen.getByText("t-old · no longer maps to a current part")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Enter" });
    expect(open).toHaveBeenCalledWith("t-db");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(back).toHaveBeenCalledTimes(1);
  });

  it("leaves keys pressed inside the Ask rail to Ask", () => {
    const back = vi.fn();
    render(<><ReviewTour {...props({ onBack: back })} /><aside data-review-rail="ask"><button type="button">Ask control</button></aside></>);
    const control = screen.getByRole("button", { name: "Ask control" });
    fireEvent.keyDown(control, { key: "Escape" });
    fireEvent.keyDown(control, { key: "j" });
    expect(back).not.toHaveBeenCalled();
    expect(screen.getByText("Walkthrough · step 1 of 2")).toBeInTheDocument();
  });

  it("leaves Enter on a focused button to that button", () => {
    const open = vi.fn();
    render(<ReviewTour {...props({ onOpenTarget: open })} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Next" }), { key: "Enter" });
    expect(open).not.toHaveBeenCalled();
  });

  it("persists semantic cohort position across remounts for one revision", () => {
    const shared = props();
    const first = render(<ReviewTour {...shared} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("lc.review.guided.position.r-1.rev-1")).toBe("db");
    first.unmount();

    render(<ReviewTour {...shared} />);
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByText("Walkthrough · step 2 of 2")).toBeInTheDocument();
  });

  it("lets URL/browser state select the same canonical cohort", () => {
    const shared = props({ activeCohortId: "api" });
    const view = render(<ReviewTour {...shared} />);
    expect(screen.getByRole("heading", { name: "API contract" })).toBeInTheDocument();

    view.rerender(<ReviewTour {...shared} activeCohortId="db" />);
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByTestId("review-tour")).toHaveAttribute("data-guided-cohort", "db");
  });

  it("resets the Tour immediately when the revision changes despite an equal step count", () => {
    const shared = props();
    const view = render(<ReviewTour {...shared} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    // The new Snapshot deliberately has the same number of Tour steps. The
    // previously selected cohort must not appear for one stale render.
    view.rerender(<ReviewTour {...shared} revisionId="rev-2" />);
    expect(screen.getByRole("heading", { name: "API contract" })).toBeInTheDocument();
    expect(screen.getByTestId("review-tour")).toHaveAttribute("data-guided-cohort", "api");
    expect(window.sessionStorage.getItem("lc.review.guided.position.r-1.rev-2")).toBe("api");
    view.unmount();
  });

  it("degrades to deterministic content when AI is unavailable", () => {
    render(<ReviewTour {...props({ tour: tour("unavailable") })} />);

    expect(screen.getByText("Built from the code only · no AI wording")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "API contract" })).toBeInTheDocument();
    expect(screen.getByText("Clients now send two fields.")).toBeInTheDocument();
  });

  it("offers Refine explanation only when a model could answer", () => {
    const synthesize = vi.fn(async () => undefined);
    const view = render(<ReviewTour {...props({ onSynthesize: synthesize })} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("button", { name: "Refine explanation" })).toBeInTheDocument();
    view.unmount();

    render(<ReviewTour {...props({ tour: tour("unavailable"), onSynthesize: synthesize })} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refine explanation" })).toBeNull();
  });

  it("keeps Ask bound to the current step part, and offers the Map and the host's view switch", () => {
    const ask = vi.fn();
    const map = vi.fn();
    const { rerender } = render(<ReviewTour {...props({ onAsk: ask, onShowOnMap: map })} />);

    fireEvent.click(screen.getByRole("button", { name: "Ask about this part" }));
    expect(ask).toHaveBeenCalledWith("t-api");
    fireEvent.click(screen.getByRole("button", { name: "Show on the Map" }));
    expect(map).toHaveBeenCalledTimes(1);
    // The map view is offered only where the host can draw it, and only under the Walkthrough's own name.
    expect(screen.queryByTestId("walkthrough-switch")).toBeNull();
    expect(screen.queryByText(/Change Story/)).toBeNull();
    rerender(<ReviewTour {...props({ onAsk: ask, onShowOnMap: map, viewSwitch: <span data-testid="walkthrough-switch" /> })} />);
    expect(screen.getByTestId("walkthrough-switch")).toBeInTheDocument();
  });

  it("says so and goes back to the Overview when the revision has no steps", () => {
    const back = vi.fn();
    render(<ReviewTour {...props({ onBack: back, tour: { ...tour(), step_count: 0, steps: [] } })} />);
    fireEvent.click(screen.getByRole("button", { name: "Back to the Overview" }));
    expect(back).toHaveBeenCalledTimes(1);
  });
});
