import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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
    total_lemoncrow_savings_usd: 0.1,
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

function renderSessions(initialEntry = "/activity") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TimeRangeProvider>
        <Routes>
          <Route path="/activity" element={<Sessions />} />
          <Route path="/activity/:id" element={<Sessions />} />
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
    expect(screen.getAllByText("$0.42").length).toBeGreaterThanOrEqual(1);
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

  it("labels an estimated session-row cost as Priced, in a neutral colour, and offers host/workspace filters", async () => {
    mockFetch({
      "/api/traces": jsonResponse(sampleTraces),
      "/api/v1/sessions": jsonResponse(sampleSessions),
    });
    renderSessions();
    const task = await screen.findByText("Fix login bug");
    // sampleSessions[0].cost_status is "estimated": the row says "Priced" (and why, on hover)
    // instead of a red "~" prefix that read as an error.
    const row = task.closest("button") as HTMLElement;
    const priced = within(row).getByText("Priced").parentElement as HTMLElement;
    expect(priced).toHaveAttribute("title", expect.stringMatching(/host did not report a cost/));
    const value = within(priced).getByText("$0.42");
    expect(value).toHaveClass("text-foreground");
    expect(within(row).queryByText("~$0.42")).not.toBeInTheDocument();
    expect(screen.getAllByText("$0.10").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByLabelText("Filter by host")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter by workspace")).toBeInTheDocument();
  });

  it("keeps sessions visible without usage and does not render unavailable cost as $0", async () => {
    const internalTrace = {
      ...sampleTraces.items[0],
      id: "internal-trace",
      session_id: "internal-session",
      agent: "lemoncrow:code",
      host: "lemoncrow",
      model: "",
      task: "Internal LemonCrow task",
      input_tokens: 0,
      output_tokens: 0,
      cached_input_tokens: 0,
      cache_creation_input_tokens: 0,
    };
    const unpricedTrace = {
      ...sampleTraces.items[0],
      id: "opencode-session",
      session_id: "opencode-session",
      agent: "lemoncrow:code",
      host: "opencode",
      model: "opencode/big-pickle",
      task: "Real OpenCode session",
      input_tokens: 100,
      output_tokens: 20,
      cached_input_tokens: 0,
      cache_creation_input_tokens: 0,
    };
    const summaries: SessionSummary[] = [
      {
        session_id: "internal-session",
        started_at: "2024-01-01T10:00:00Z",
        ended_at: "2024-01-01T10:01:00Z",
        duration_seconds: 60,
        active_duration_seconds: 60,
        vendor: "unknown",
        cost_status: "unavailable",
        total_turns: 0,
        total_cost_usd: 0,
        total_lemoncrow_savings_usd: 0,
        label: null,
        models_used: {},
      },
      {
        session_id: "opencode-session",
        started_at: "2024-01-01T11:00:00Z",
        ended_at: "2024-01-01T11:01:00Z",
        duration_seconds: 60,
        active_duration_seconds: 60,
        vendor: "opencode",
        started_model: "opencode/big-pickle",
        cost_status: "unavailable",
        total_turns: 1,
        total_cost_usd: 0,
        total_lemoncrow_savings_usd: 0,
        label: null,
        models_used: { "opencode/big-pickle": 1 },
        input_tokens: 100,
        output_tokens: 20,
      },
    ];

    mockFetch({
      "/api/traces": jsonResponse({
        ...sampleTraces,
        items: [internalTrace, unpricedTrace],
      }),
      "/api/v1/sessions": jsonResponse(summaries),
    });

    renderSessions();

    const task = await screen.findByText("Real OpenCode session");
    expect(screen.getByText("Internal LemonCrow task")).toBeInTheDocument();
    const row = task.closest("button");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("—")).toBeInTheDocument();
    expect(within(row as HTMLElement).queryByText("$0.00")).toBeInTheDocument();
  });

  it("does not report savings nobody measured as zero", async () => {
    const summary = (id: string, extra: Partial<SessionSummary>): SessionSummary => ({
      session_id: id,
      started_at: "2024-01-01T10:00:00Z",
      ended_at: "2024-01-01T10:01:00Z",
      duration_seconds: 60,
      active_duration_seconds: 60,
      vendor: "claude",
      cost_status: "recorded",
      total_turns: 1,
      total_cost_usd: 1,
      total_lemoncrow_savings_usd: 0,
      label: null,
      models_used: {},
      ...extra,
    });
    mockFetch({
      "/api/traces": jsonResponse(emptyTraces),
      "/api/v1/sessions": jsonResponse([summary("unmeasured", { savings_status: "unavailable" })]),
    });
    renderSessions();
    const card = (await screen.findByText("Saved")).parentElement as HTMLElement;
    expect(within(card).getByText("—")).toBeInTheDocument();
    // "Efficiency" is gone; the ratio is Cost reduction, as on Home and Usage, and stays "—" without measured savings.
    expect(screen.queryByText("Efficiency")).not.toBeInTheDocument();
    const reduction = screen.getByText("Cost reduction").parentElement as HTMLElement;
    expect(within(reduction).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Totals for all 1 session.")).toBeInTheDocument();
  });

  it("says which sessions the totals cover and explains an at-least total", async () => {
    const summary = (id: string, extra: Partial<SessionSummary>): SessionSummary => ({
      session_id: id,
      started_at: "2024-01-01T10:00:00Z",
      ended_at: "2024-01-01T10:01:00Z",
      duration_seconds: 60,
      active_duration_seconds: 60,
      vendor: "claude",
      cost_status: "recorded",
      total_turns: 1,
      total_cost_usd: 1,
      total_lemoncrow_savings_usd: 0,
      label: null,
      models_used: {},
      ...extra,
    });
    mockFetch({
      "/api/traces": jsonResponse(emptyTraces),
      "/api/v1/sessions": jsonResponse([summary("priced", {}), summary("unpriced", { cost_status: "unavailable" })]),
    });
    renderSessions();
    expect(await screen.findByText("Totals for all 2 sessions.")).toBeInTheDocument();
    const cost = screen.getByText("Cost").parentElement as HTMLElement;
    expect(within(cost).getByText("≥$1.00")).toBeInTheDocument();
    expect(within(cost).getByText("At least: 1 session without a price.")).toBeInTheDocument();
  });

  it("states cost reduction with Home's label and precision, and cost in neutral text", async () => {
    mockFetch({
      "/api/traces": jsonResponse(emptyTraces),
      "/api/v1/sessions": jsonResponse([{
        session_id: "s-1", duration_seconds: 60, active_duration_seconds: 60, vendor: "claude", cost_status: "recorded",
        total_turns: 1, total_cost_usd: 6.97, total_lemoncrow_savings_usd: 2.67, savings_status: "recorded", label: null, models_used: {},
      }]),
    });
    renderSessions();
    const reduction = (await screen.findByText("Cost reduction")).parentElement as HTMLElement;
    // 2.67 ÷ (6.97 + 2.67) = 27.7%, as Home and Usage show it.
    expect(within(reduction).getByText("27.7%")).toBeInTheDocument();
    expect(screen.queryByText("Efficiency")).toBeNull();
  });

  it("shows empty state when no sessions", async () => {
    mockFetch({
      "/api/traces": jsonResponse(emptyTraces),
      "/api/v1/sessions": jsonResponse([]),
    });
    renderSessions();
    expect(await screen.findByText("No sessions yet", { selector: "div" })).toBeInTheDocument();
    // One empty state that says how sessions appear, not "Select a session" beside an empty list.
    expect(screen.getByRole("link", { name: "Open Integrations" })).toHaveAttribute("href", "/settings/integrations");
    expect(screen.queryByText("Select a session")).not.toBeInTheDocument();
  });

  it("explains unavailable hosted history without reporting zero activity", async () => {
    const message = "Coding-agent telemetry has not been connected to this hosted organization.";
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ error: { code: "not_configured", message } }, 501),
    );
    renderSessions();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText(/501|ApiError|No sessions/)).not.toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
    // No "—" totals or "Select a session" beside a list that failed to load.
    expect(screen.getByText("Sessions are unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Select a session")).not.toBeInTheDocument();
  });

  it("keeps an HTTP fallback when an upstream error is not JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response("<html>gateway unavailable</html>", { status: 503, statusText: "Service Unavailable" }),
    );
    renderSessions();
    expect(await screen.findByRole("alert")).toHaveTextContent("503 Service Unavailable");
    expect(screen.queryByText(/gateway unavailable/)).not.toBeInTheDocument();
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
        total_lemoncrow_savings_usd: 0.1,
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

    renderSessions("/activity/abc123def456ghi");

    expect(await screen.findByText("Execution Flow")).toBeInTheDocument();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
  });

  describe("arriving on the list", () => {
    const twoSessions = () => ({
      "/api/traces": jsonResponse({
        items: ["sess-a", "sess-b"].map((sid, index) => ({
          ...sampleTraces.items[0],
          id: sid,
          session_id: sid,
          task: sid === "sess-a" ? "Older session" : "Newest session",
          created_at: `2024-01-01T1${index}:00:00Z`,
        })),
        metrics: sampleTraces.metrics,
      }),
      // sess-a was created later, but sess-b has the newer activity.
      "/api/v1/sessions": jsonResponse([
        { ...sampleSessions[0], session_id: "sess-a", ended_at: null, updated_at: "2024-01-01T10:05:00Z" },
        { ...sampleSessions[0], session_id: "sess-b", ended_at: null, updated_at: "2024-01-01T12:00:00Z" },
      ]),
    });
    const setWidth = (desktop: boolean) =>
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        writable: true,
        value: (query: string) => ({ matches: desktop && query.includes("min-width"), media: query, addEventListener() {}, removeEventListener() {} }),
      });
    afterEach(() => {
      delete (window as { matchMedia?: unknown }).matchMedia;
    });

    it("opens the newest session beside the list on a desktop", async () => {
      setWidth(true);
      const fetchSpy = mockFetch(twoSessions());
      renderSessions("/activity");

      await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/api/v1/traces/sess-b"))).toBe(true));
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/api/v1/traces/sess-a"))).toBe(false);
      expect(screen.getByText("Older session")).toBeInTheDocument();
      expect(screen.queryByText("Select a session")).not.toBeInTheDocument();
    });

    it("keeps the list first on a phone", async () => {
      setWidth(false);
      const fetchSpy = mockFetch(twoSessions());
      renderSessions("/activity");

      expect(await screen.findByText("Newest session")).toBeInTheDocument();
      expect(await screen.findByText("Select a session")).toBeInTheDocument();
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/api/v1/traces/sess-"))).toBe(false);
    });
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
          total_lemoncrow_savings_usd: 0.05,
        },
        {
          ...sampleSessions[0],
          session_id: "sess-b",
          started_at: "2024-01-01T09:00:00Z",
          ended_at: null,
          updated_at: "2024-01-01T11:00:00Z",
          total_cost_usd: 0.3,
          total_lemoncrow_savings_usd: 0.07,
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
    fireEvent.click(screen.getByRole("button", { name: "Refresh sessions" }));

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
