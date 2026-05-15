import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import Sessions from "./Sessions";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderSessions() {
  return render(
    <MemoryRouter>
      <TimeRangeProvider>
        <Sessions />
      </TimeRangeProvider>
    </MemoryRouter>
  );
}

describe("Sessions page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => {}));
    renderSessions();
    expect(screen.getByText(/Loading sessions/i)).toBeInTheDocument();
  });

  it("renders session rows after load", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse([
        {
          session_id: "abc123def456ghi",
          started_at: "2024-01-01T10:00:00Z",
          ended_at: "2024-01-01T10:30:00Z",
          duration_seconds: 1800,
          vendor: "anthropic",
          total_turns: 10,
          total_cost_usd: 0.42,
          total_atelier_savings_usd: 0.1,
          label: null,
          models_used: { "claude-3-5-sonnet": 10 },
        },
      ])
    );
    renderSessions();
    expect(await screen.findByText("abc123def456…")).toBeInTheDocument();
    // $0.42 appears in both the summary MetricCard and the table row
    expect(screen.getAllByText("$0.42").length).toBeGreaterThanOrEqual(1);
  });

  it("shows empty state when no sessions", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));
    renderSessions();
    expect(await screen.findByText(/No sessions yet/i)).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network error"));
    renderSessions();
    expect(await screen.findByText(/network error/i)).toBeInTheDocument();
  });
});
