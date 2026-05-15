import { render, screen } from "@testing-library/react";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import Insights from "./Insights";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SAMPLE_INSIGHTS = {
  since: "2024-01-01T00:00:00Z",
  until: "2024-01-08T00:00:00Z",
  session_count: 5,
  total_duration_seconds: 9000,
  total_cost_usd: 2.5,
  total_atelier_savings_usd: 0.5,
  cost_by_vendor: { anthropic: 2.0, openai: 0.5 },
  cost_by_tool: { Bash: 1.2, Read: 0.8 },
  cost_by_model: { "claude-3-5-sonnet": 2.5 },
  top_sessions: [
    { session_id: "abc123xyz", cost_usd: 1.5, label: null, duration_seconds: 1800 },
  ],
  outcomes_summary: {
    route_decisions: 10,
    route_avg_score: 0.85,
    compact_events: 3,
    compact_avg_score: 0.9,
    sessions_with_high_extra_reads: [],
  },
  opportunities: [
    {
      kind: "routing",
      message: "Consider downtiering more turns",
      estimated_savings_usd: 0.3,
      sessions_affected: 2,
    },
  ],
};

function renderInsights() {
  return render(
    <TimeRangeProvider>
      <Insights />
    </TimeRangeProvider>
  );
}

describe("Insights page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => {}));
    renderInsights();
    expect(screen.getByText(/Loading insights/i)).toBeInTheDocument();
  });

  it("renders InsightsWindow data", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(SAMPLE_INSIGHTS));
    renderInsights();
    expect(await screen.findByText("5")).toBeInTheDocument(); // session_count
    expect(screen.getByText("$2.50")).toBeInTheDocument();
    // savings appears in MetricCard and possibly opportunity list
    expect(screen.getAllByText("$0.50").length).toBeGreaterThanOrEqual(1);
  });

  it("shows empty state when no sessions", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ ...SAMPLE_INSIGHTS, session_count: 0 })
    );
    renderInsights();
    expect(await screen.findByText(/No insights yet/i)).toBeInTheDocument();
  });
});
