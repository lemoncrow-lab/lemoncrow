import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
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
    workspaces: [],
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
    workspaces: [],
  },
};

const sampleSessions: SessionSummary[] = [
  {
    session_id: "abc123def456ghi",
    started_at: "2024-01-01T10:00:00Z",
    ended_at: "2024-01-01T10:30:00Z",
    updated_at: "2024-01-01T10:45:00Z",
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

function tracesPageResponse(start: number, count: number): Response {
  const items = Array.from({ length: count }, (_, i) => {
    const n = start + i;
    return {
      id: `s-${n}`,
      session_id: `s-${n}`,
      agent: "anthropic",
      model: "claude-3-5-sonnet",
      task: `Task ${n}`,
      status: "completed",
      files_touched: [],
      tools_called: [],
      commands_run: [],
      errors_seen: [],
      repeated_failures: [],
      validation_results: [],
      created_at: new Date(2024, 0, 1, 0, 0, n).toISOString(),
      input_tokens: 1,
      output_tokens: 1,
      cached_input_tokens: 0,
    };
  });
  return jsonResponse({
    items,
    metrics: {
      stats: { total: count, success: count, failed: 0, partial: 0 },
      hosts: [],
      domains: [],
      workspaces: [],
    },
  });
}

function mockFetch(
  responses: Record<string, Response | (() => Promise<never>)>
) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      for (const [key, response] of Object.entries(responses).sort(
        ([a], [b]) => b.length - a.length
      )) {
        if (url.includes(key)) {
          if (typeof response === "function") return response();
          return Promise.resolve(response);
        }
      }
      return Promise.resolve(new Response("not found", { status: 404 }));
    });
}

function renderSessions(initialEntry = "/sessions") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TimeRangeProvider>
        <Routes>
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/sessions/:id" element={<Sessions />} />
        </Routes>
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

  it("requests unbounded traces and long-window summaries", async () => {
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
          !String(input).includes("days=")
      )
    ).toBe(true);
    expect(
      fetchSpy.mock.calls.some(
        ([input]) =>
          String(input).includes("/api/v1/sessions?") &&
          String(input).includes("since=36500d")
      )
    ).toBe(true);
  });

  it("prefixes the session-row cost with ~ for estimated costs, and offers host/workspace filters", async () => {
    mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse(sampleSessions),
    });
    renderSessions();
    expect(await screen.findByText("Fix login bug")).toBeInTheDocument();
    // sampleSessions[0].cost_status is "estimated" — the row's Cost cell
    // (the token grid was cut down to Cost + Saved only) must show the ~
    // prefix that distinguishes it from a recorded cost.
    expect(screen.getByText("~$0.420")).toBeInTheDocument();
    expect(screen.getAllByText("$0.100").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByLabelText("Filter by host")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter by workspace")).toBeInTheDocument();
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

  it("opens the detail pane when a session route is selected", async () => {
    mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse(sampleSessions),
      "/api/v1/sessions/abc123def456ghi": jsonResponse({
        session_id: "abc123def456ghi",
        started_at: "2024-01-01T10:00:00Z",
        ended_at: "2024-01-01T10:30:00Z",
        duration_seconds: 1800,
        active_duration_seconds: 1500,
        vendor: "anthropic",
        started_model: "claude-3-5-sonnet",
        cost_status: "estimated",
        agent_settings: {},
        skills: [],
        telemetry: {},
        raw_artifact_ids: [],
        total_turns: 10,
        total_cost_usd: 0.42,
        total_atelier_savings_usd: 0.1,
        label: null,
        models_used: { "claude-3-5-sonnet": 10 },
        input_tokens: 500,
        output_tokens: 200,
        cached_input_tokens: 100,
        tool_call_count: 2,
        input_token_cost_usd: 0.21,
        cache_write_cost_usd: 0,
        cache_read_cost_usd: 0.01,
        output_token_cost_usd: 0.2,
        cache_write_tokens: 0,
        cache_read_tokens: 100,
        routing_downtiered_turns: 0,
        routing_savings_usd: 0,
        compact_events: 0,
        compact_savings_estimate_usd: 0,
        top_tools_by_cost: [],
      }),
      "/api/v1/traces/abc123def456ghi": jsonResponse({
        id: "abc123def456ghi",
        session_id: "abc123def456ghi",
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
      }),
      "/api/ledgers/abc123def456ghi": jsonResponse({
        conversations: [
          {
            kind: "agent_message",
            at: "2024-01-01T10:00:01Z",
            summary: "Assistant response",
            content: "Done.",
            model: "claude-3-5-sonnet",
          },
        ],
      }),
    });

    renderSessions("/sessions/abc123def456ghi");

    expect(await screen.findByText("Execution Flow")).toBeInTheDocument();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
  });

  it("prefers updated_at over started_at when sorting rows", async () => {
    mockFetch({
      "/api/traces": jsonResponse({
        items: [
          {
            id: "sess-a",
            session_id: "sess-a",
            agent: "anthropic",
            model: "claude-3-5-sonnet",
            task: "Older started session",
            status: "completed",
            files_touched: [],
            tools_called: [],
            commands_run: [],
            errors_seen: [],
            repeated_failures: [],
            validation_results: [],
            created_at: "2024-01-01T10:00:00Z",
            input_tokens: 10,
            output_tokens: 5,
            cached_input_tokens: 0,
          },
          {
            id: "sess-b",
            session_id: "sess-b",
            agent: "anthropic",
            model: "claude-3-5-sonnet",
            task: "Newer mtime session",
            status: "completed",
            files_touched: [],
            tools_called: [],
            commands_run: [],
            errors_seen: [],
            repeated_failures: [],
            validation_results: [],
            created_at: "2024-01-01T09:00:00Z",
            input_tokens: 12,
            output_tokens: 6,
            cached_input_tokens: 0,
          },
        ],
        metrics: {
          stats: { total: 2, success: 2, failed: 0, partial: 0 },
          hosts: [],
          domains: [],
          workspaces: [],
        },
      }),
      "/api/v1/sessions": jsonResponse([
        {
          ...sampleSessions[0],
          session_id: "sess-a",
          started_at: "2024-01-01T10:00:00Z",
          ended_at: null,
          updated_at: "2024-01-01T10:05:00Z",
          total_cost_usd: 0.2,
          total_atelier_savings_usd: 0.05,
        },
        {
          ...sampleSessions[0],
          session_id: "sess-b",
          started_at: "2024-01-01T09:00:00Z",
          ended_at: null,
          updated_at: "2024-01-01T11:00:00Z",
          total_cost_usd: 0.3,
          total_atelier_savings_usd: 0.07,
        },
      ]),
    });

    renderSessions();

    const rows = await screen.findAllByText(
      /(Older started session|Newer mtime session)/
    );
    expect(rows[0]).toHaveTextContent("Newer mtime session");
    expect(rows[1]).toHaveTextContent("Older started session");
  });

  it("auto-refresh replaces the loaded range instead of appending duplicates after Load More", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/sessions"))
          return Promise.resolve(jsonResponse([]));
        if (!url.includes("/api/traces")) {
          return Promise.resolve(new Response("not found", { status: 404 }));
        }
        const params = new URL(url, "http://localhost").searchParams;
        if (params.get("offset") === "0" && params.get("limit") === "100") {
          // Refresh must ask for the whole loaded range from offset 0.
          return Promise.resolve(tracesPageResponse(0, 100));
        }
        if (params.get("offset") === "50") {
          return Promise.resolve(tracesPageResponse(50, 50));
        }
        return Promise.resolve(tracesPageResponse(0, 50));
      });

    renderSessions();
    await screen.findByText("Task 0");

    fireEvent.click(await screen.findByText(/Load More/i));
    await screen.findByText("Task 50");

    fetchSpy.mockClear();
    fireEvent.click(screen.getByTitle("Refresh sessions"));

    // The refresh request must replace (offset=0, limit=100), not append
    // (offset=page*50) — the pre-fix request shape here was offset=50,
    // limit=50, which appends another copy of the last page every tick.
    const refreshCall = fetchSpy.mock.calls.find(([reqInput]) =>
      String(reqInput).includes("/api/traces")
    );
    expect(refreshCall).toBeDefined();
    const refreshParams = new URL(String(refreshCall![0]), "http://localhost")
      .searchParams;
    expect(refreshParams.get("offset")).toBe("0");
    expect(refreshParams.get("limit")).toBe("100");

    await waitFor(() => {
      expect(screen.getAllByText(/^Task \d+$/).length).toBe(100);
    });
  });
});
