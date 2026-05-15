import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import SessionDetail from "./SessionDetail";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SAMPLE_REPORT = {
  session_id: "abc123def456ghi789",
  started_at: "2024-01-01T10:00:00Z",
  ended_at: "2024-01-01T10:30:00Z",
  duration_seconds: 1800,
  vendor: "anthropic",
  total_turns: 12,
  total_cost_usd: 0.55,
  total_atelier_savings_usd: 0.12,
  label: null,
  models_used: { "claude-3-5-sonnet": 12 },
  tool_call_count: 45,
  input_token_cost_usd: 0.3,
  cache_write_cost_usd: 0.05,
  cache_read_cost_usd: 0.01,
  output_token_cost_usd: 0.19,
  input_tokens: 10000,
  cache_write_tokens: 5000,
  cache_read_tokens: 3000,
  output_tokens: 2000,
  routing_downtiered_turns: 4,
  routing_savings_usd: 0.08,
  compact_events: 2,
  compact_savings_estimate_usd: 0.04,
  top_tools_by_cost: [{ tool: "Bash", calls: 20, cost_usd: 0.22 }],
};

function renderDetail(id = "abc123def456ghi789") {
  return render(
    <MemoryRouter initialEntries={[`/sessions/${id}`]}>
      <Routes>
        <Route
          path="/sessions/:id"
          element={
            <TimeRangeProvider>
              <SessionDetail />
            </TimeRangeProvider>
          }
        />
      </Routes>
    </MemoryRouter>
  );
}

describe("SessionDetail page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => {}));
    renderDetail();
    expect(screen.getByText(/Loading session report/i)).toBeInTheDocument();
  });

  it("renders cost metrics after load", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(SAMPLE_REPORT));
    renderDetail();
    expect(await screen.findByText("$0.55")).toBeInTheDocument();
    expect(screen.getByText("$0.12")).toBeInTheDocument();
  });

  it("shows error on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ detail: "not found" }, 404));
    renderDetail("unknown-id");
    expect(await screen.findByText(/not found|error|404/i)).toBeInTheDocument();
  });
});
