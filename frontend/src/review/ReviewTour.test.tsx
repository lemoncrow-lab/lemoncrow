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
    onExit: vi.fn(),
    onBackToBrief: vi.fn(),
    onOpenTarget: vi.fn(),
    onInspectEvidence: vi.fn(),
    onAsk: vi.fn(),
    ...overrides,
  };
}

describe("ReviewTour guided review", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("is navigation only and opens the exact Code target", () => {
    const open = vi.fn();
    render(<ReviewTour {...props({ onOpenTarget: open })} />);

    expect(screen.getByText("Guided review · step 1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open code" }));
    expect(open).toHaveBeenCalledWith("t-api");
    expect(screen.getByText("Guided Review is navigation only. Visiting a step never changes your durable Reviewed state.")).toBeInTheDocument();
    expect(screen.queryByText(/mark.*reviewed/i)).toBeNull();
  });

  it("supports next/back keyboard reading, explicit Brief exit and exact Enter navigation", () => {
    const open = vi.fn();
    const back = vi.fn();
    const change = vi.fn();
    render(<ReviewTour {...props({ onOpenTarget: open, onBackToBrief: back, onCohortChange: change })} />);

    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByText("Guided review · step 2/2")).toBeInTheDocument();
    expect(change).toHaveBeenCalledWith("db");
    expect(screen.getByText("t-old · no longer maps to a current target")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Enter" });
    expect(open).toHaveBeenCalledWith("t-db");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(back).toHaveBeenCalledTimes(1);
  });

  it("persists semantic cohort position across remounts for one Snapshot", () => {
    const shared = props();
    const first = render(<ReviewTour {...shared} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("lc.review.guided.position.r-1.rev-1")).toBe("db");
    first.unmount();

    render(<ReviewTour {...shared} />);
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByText("Guided review · step 2/2")).toBeInTheDocument();
  });

  it("lets URL/browser state select the same canonical cohort", () => {
    const shared = props({ activeCohortId: "api" });
    const view = render(<ReviewTour {...shared} />);
    expect(screen.getByRole("heading", { name: "API contract" })).toBeInTheDocument();

    view.rerender(<ReviewTour {...shared} activeCohortId="db" />);
    expect(screen.getByRole("heading", { name: "Persistence" })).toBeInTheDocument();
    expect(screen.getByTestId("review-tour")).toHaveAttribute("data-guided-cohort", "db");
  });

  it("resets the Tour immediately when the Snapshot changes despite an equal step count", () => {
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

    expect(screen.getByText("AI guidance unavailable")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "API contract" })).toBeInTheDocument();
    expect(screen.getByText("Clients now send two fields.")).toBeInTheDocument();
  });

  it("keeps Ask and evidence bound to the current step target and exposes both exits", () => {
    const ask = vi.fn();
    const evidence = vi.fn();
    const exit = vi.fn();
    const back = vi.fn();
    render(<ReviewTour {...props({ onAsk: ask, onInspectEvidence: evidence, onExit: exit, onBackToBrief: back })} />);

    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect evidence" }));
    expect(ask).toHaveBeenCalledWith("t-api");
    expect(evidence).toHaveBeenCalledWith("t-api");

    fireEvent.click(screen.getByRole("button", { name: "Back to Brief" }));
    expect(back).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Exit guided review" }));
    expect(exit).toHaveBeenCalledTimes(1);
  });
});
