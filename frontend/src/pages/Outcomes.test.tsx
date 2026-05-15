import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import Outcomes from "./Outcomes";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderOutcomes() {
  return render(
    <MemoryRouter>
      <TimeRangeProvider>
        <Outcomes />
      </TimeRangeProvider>
    </MemoryRouter>
  );
}

describe("Outcomes page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => {}));
    renderOutcomes();
    expect(screen.getByText(/Loading outcomes/i)).toBeInTheDocument();
  });

  it("shows empty state when no data", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        route_decisions: 0,
        route_avg_score: 0,
        compact_events: 0,
        compact_avg_score: 0,
        sessions_with_high_extra_reads: [],
      })
    );
    renderOutcomes();
    expect(await screen.findByText(/No outcomes captured yet/i)).toBeInTheDocument();
  });

  it("renders metrics when data is present", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        route_decisions: 42,
        route_avg_score: 0.87,
        compact_events: 15,
        compact_avg_score: 0.91,
        sessions_with_high_extra_reads: ["sess-abc123"],
      })
    );
    renderOutcomes();
    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(screen.getByText("0.870")).toBeInTheDocument();
  });
});
