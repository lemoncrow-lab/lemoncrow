import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import Sessions from "./Sessions";
import type { TraceListResponse, SessionSummary } from "../api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const emptyTraces: TraceListResponse = {
  items: [],
  metrics: {
    stats: { total: 0, success: 0, failed: 0, partial: 0 },
    hosts: [],
    domains: [],
  },
};

const sampleTraces: TraceListResponse = {
  items: [
    {
      id: "abc123def456ghi",
      agent: "anthropic",
      model: "claude-3-5-sonnet",
      task: "Fix login bug",
      status: "completed",
      files_touched: [],
      tools_called: [],
      commands_run: [],
      errors_seen: [],
      repeated_failures: [],
      validation_results: [],
      created_at: "2024-01-01T10:00:00Z",
      input_tokens: 500,
      output_tokens: 200,
      cached_input_tokens: 100,
    },
  ],
  metrics: {
    stats: { total: 1, success: 1, failed: 0, partial: 0 },
    hosts: [],
    domains: [],
  },
};

const sampleSessions: SessionSummary[] = [
  {
    session_id: "abc123def456ghi",
    started_at: "2024-01-01T10:00:00Z",
    ended_at: "2024-01-01T10:30:00Z",
    duration_seconds: 1800,
    active_duration_seconds: 1500,
    vendor: "anthropic",
    started_model: "claude-3-5-sonnet",
    cost_status: "estimated",
    total_turns: 10,
    total_cost_usd: 0.42,
    total_atelier_savings_usd: 0.1,
    label: null,
    models_used: { "claude-3-5-sonnet": 10 },
    input_tokens: 500,
    output_tokens: 200,
    cached_input_tokens: 100,
  },
];

function mockFetch(responses: Record<string, Response | (() => Promise<never>)>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(
    (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      for (const [key, response] of Object.entries(responses)) {
        if (url.includes(key)) {
          if (typeof response === "function") return response();
          return Promise.resolve(response);
        }
      }
      return Promise.resolve(new Response("not found", { status: 404 }));
    }
  );
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
    expect(screen.getByText(/Scanning.../)).toBeInTheDocument();
  });

  it("renders session rows after load", async () => {
    mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse(sampleSessions),
    });
    renderSessions();
    // Task text from the trace item
    expect(await screen.findByText("Fix login bug")).toBeInTheDocument();
    expect(screen.getByText("claude-3-5-sonnet")).toBeInTheDocument();
    expect(screen.getByLabelText("Status: completed")).toBeInTheDocument();
    expect(screen.queryByText("Estimated from tokens")).not.toBeInTheDocument();
    // $0.42 appears in summary MetricCards
    expect(screen.getAllByText("$0.420").length).toBeGreaterThanOrEqual(1);
  });

  it("requests traces for the active time window", async () => {
    const fetchSpy = mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse(sampleSessions),
    });
    renderSessions();
    await screen.findByText("Fix login bug");
    expect(
      fetchSpy.mock.calls.some(
        ([input]) =>
          String(input).includes("/api/traces?") &&
          String(input).includes("days=7")
      )
    ).toBe(true);
  });

  it("falls back to trace token counts when summary tokens are zero", async () => {
    mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse([
        {
          ...sampleSessions[0],
          input_tokens: 0,
          output_tokens: 0,
          cached_input_tokens: 0,
        },
      ]),
    });
    renderSessions();
    expect(await screen.findByText("Fix login bug")).toBeInTheDocument();
    expect(screen.getAllByText("500").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("200").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("100").length).toBeGreaterThanOrEqual(1);
  });

  it("shows empty state when no sessions", async () => {
    mockFetch({
      "/api/traces": jsonResponse(emptyTraces),
      "/api/v1/sessions": jsonResponse([]),
    });
    renderSessions();
    expect(await screen.findByText(/No sessions found/i)).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network error"));
    renderSessions();
    expect(await screen.findByText(/network error/i)).toBeInTheDocument();
  });
});
