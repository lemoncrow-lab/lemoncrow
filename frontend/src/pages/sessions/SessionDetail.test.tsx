import { render, screen, fireEvent } from "@testing-library/react";
import { SessionExplorerDetail } from "./SessionDetail";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch(responses: Record<string, Response>) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      for (const [key, response] of Object.entries(responses)) {
        if (url.includes(key)) return Promise.resolve(response);
      }
      return Promise.resolve(new Response("not found", { status: 404 }));
    });
}

describe("SessionExplorerDetail", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the started model and pricing source in the header", async () => {
    mockFetch({
      "/api/v1/sessions/sess-001": jsonResponse({
        session_id: "sess-001",
        started_at: "2026-05-16T00:00:00Z",
        ended_at: "2026-05-16T00:05:00Z",
        duration_seconds: 300,
        active_duration_seconds: 240,
        vendor: "Anthropic",
        started_model: "claude-sonnet-4-6",
        cost_status: "estimated",
        agent_settings: {},
        skills: [],
        telemetry: {},
        raw_artifact_ids: [],
        total_turns: 4,
        total_cost_usd: 0.52,
        total_atelier_savings_usd: 0.08,
        label: null,
        models_used: { "claude-sonnet-4-6": 4 },
        input_tokens: 1200,
        output_tokens: 300,
        cached_input_tokens: 200,
        tool_call_count: 2,
        input_token_cost_usd: 0.21,
        cache_write_cost_usd: 0,
        cache_read_cost_usd: 0.01,
        output_token_cost_usd: 0.3,
        cache_write_tokens: 0,
        cache_read_tokens: 200,
        routing_downtiered_turns: 0,
        routing_savings_usd: 0,
        compact_events: 0,
        compact_savings_estimate_usd: 0,
        top_tools_by_cost: [],
      }),
      "/api/v1/traces/sess-001": jsonResponse({
        id: "sess-001",
        session_id: "sess-001",
        agent: "copilot",
        model: "claude-sonnet-4-6",
        task: "Audit sessions explorer",
        status: "success",
        files_touched: [],
        tools_called: [],
        commands_run: [],
        errors_seen: [],
        repeated_failures: [],
        validation_results: [],
        created_at: "2026-05-16T00:00:00Z",
      }),
      "/api/ledgers/sess-001": jsonResponse({
        conversations: [
          {
            kind: "agent_message",
            at: "2026-05-16T00:00:01Z",
            summary: "Assistant response",
            content: "Done.",
            model: "claude-sonnet-4-6",
          },
        ],
      }),
    });

    render(<SessionExplorerDetail sessionId="sess-001" />);

    expect(await screen.findByText(/Model/i)).toBeInTheDocument();
    expect(
      screen.getAllByText("claude-sonnet-4-6").length
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByLabelText("Status: success")).toBeInTheDocument();
    expect(
      screen.queryByText("Estimated from trace tokens")
    ).not.toBeInTheDocument();
  });

  it("reattaches savings to the tool call whose window it falls in, skipping a stale earlier row", async () => {
    mockFetch({
      "/api/v1/sessions/sess-002": jsonResponse({
        session_id: "sess-002",
        started_at: "2026-01-01T00:00:00Z",
        ended_at: "2026-01-01T00:02:00Z",
        duration_seconds: 120,
        active_duration_seconds: 120,
        vendor: "Anthropic",
        started_model: "claude-sonnet-4-6",
        cost_status: "estimated",
        agent_settings: {},
        skills: [],
        telemetry: {},
        raw_artifact_ids: [],
        total_turns: 2,
        total_cost_usd: 1,
        total_atelier_savings_usd: 0.5,
        label: null,
        models_used: { "claude-sonnet-4-6": 2 },
        input_tokens: 100,
        output_tokens: 50,
        cached_input_tokens: 0,
        tool_call_count: 2,
        input_token_cost_usd: 0.1,
        cache_write_cost_usd: 0,
        cache_read_cost_usd: 0.15,
        output_token_cost_usd: 0.2,
        cache_write_tokens: 0,
        cache_read_tokens: 0,
        routing_downtiered_turns: 0,
        routing_savings_usd: 0,
        compact_events: 0,
        compact_savings_estimate_usd: 0,
        top_tools_by_cost: [],
        tool_savings: [
          {
            tool: "read",
            tokens_saved: 500,
            calls_saved: 1,
            cost_saved_usd: 0.01,
            model: "claude-sonnet-4-6",
            at: "2025-12-31T23:00:00Z",
          },
          {
            tool: "read",
            tokens_saved: 900,
            calls_saved: 1,
            cost_saved_usd: 0.02,
            model: "claude-sonnet-4-6",
            at: "2026-01-01T00:01:05Z",
          },
        ],
      }),
      "/api/v1/traces/sess-002": jsonResponse({
        id: "sess-002",
        session_id: "sess-002",
        agent: "copilot",
        model: "claude-sonnet-4-6",
        task: "Two reads",
        status: "success",
        files_touched: [],
        tools_called: [],
        commands_run: [],
        errors_seen: [],
        repeated_failures: [],
        validation_results: [],
        created_at: "2026-01-01T00:00:00Z",
      }),
      "/api/ledgers/sess-002": jsonResponse({
        conversations: [
          {
            kind: "tool_call",
            at: "2026-01-01T00:00:00Z",
            summary: "Called read",
            tool_name: "read",
            tool_use_id: "call-1",
            content: "",
          },
          {
            kind: "tool_call",
            at: "2026-01-01T00:01:00Z",
            summary: "Called read",
            tool_name: "read",
            tool_use_id: "call-2",
            content: "",
          },
        ],
      }),
    });

    render(<SessionExplorerDetail sessionId="sess-002" />);

    // Under the pre-fix pointer logic, the stale first savings row (older
    // than either tool call by more than the 2s window) never matches
    // anything and is never skipped, so it permanently blocks the queue and
    // no later savings badge for this tool ever appears.
    expect(await screen.findByText(/\$0\.020/)).toBeInTheDocument();
  });

  it("shows a neutral zero duration instead of a runaway value when a turn timestamp is missing, and relabels cache-read cost", async () => {
    mockFetch({
      "/api/v1/sessions/sess-003": jsonResponse({
        session_id: "sess-003",
        started_at: "2026-01-01T00:00:00Z",
        ended_at: "2026-01-01T00:00:05Z",
        duration_seconds: 5,
        active_duration_seconds: 0,
        vendor: "Anthropic",
        started_model: "claude-sonnet-4-6",
        cost_status: "estimated",
        agent_settings: {},
        skills: [],
        telemetry: {},
        raw_artifact_ids: [],
        total_turns: 2,
        total_cost_usd: 0.1,
        total_atelier_savings_usd: 0.02,
        label: null,
        models_used: { "claude-sonnet-4-6": 2 },
        input_tokens: 10,
        output_tokens: 5,
        cached_input_tokens: 0,
        tool_call_count: 0,
        input_token_cost_usd: 0.01,
        cache_write_cost_usd: 0,
        cache_read_cost_usd: 0.15,
        output_token_cost_usd: 0.01,
        cache_write_tokens: 0,
        cache_read_tokens: 0,
        routing_downtiered_turns: 0,
        routing_savings_usd: 0,
        compact_events: 0,
        compact_savings_estimate_usd: 0,
        top_tools_by_cost: [],
      }),
      "/api/v1/traces/sess-003": jsonResponse({
        id: "sess-003",
        session_id: "sess-003",
        agent: "copilot",
        model: "claude-sonnet-4-6",
        task: "Bad timestamp turn",
        status: "success",
        files_touched: [],
        tools_called: [],
        commands_run: [],
        errors_seen: [],
        repeated_failures: [],
        validation_results: [],
        created_at: "2026-01-01T00:00:00Z",
      }),
      "/api/ledgers/sess-003": jsonResponse({
        conversations: [
          {
            kind: "user_message",
            at: "2026-01-01T00:00:00Z",
            summary: "User",
            content: "Hi",
          },
          {
            kind: "agent_message",
            at: "",
            summary: "Assistant",
            content: "Done.",
          },
        ],
      }),
    });

    render(<SessionExplorerDetail sessionId="sess-003" />);

    await screen.findByText("Done.");

    // The pre-fix computation maps a missing `at` to epoch (1970),
    // producing a runaway negative duration (e.g. "-49037h") instead of
    // skipping the invalid timestamp.
    const timeLabel = screen.getByText("Time");
    expect(timeLabel.nextElementSibling?.textContent).toBe("0s");

    // cache_read_cost_usd is cost paid for cache reads, not savings.
    fireEvent.click(screen.getByTitle("Toggle Detailed Metrics"));
    expect(await screen.findByText("Cache Read Cost")).toBeInTheDocument();
    expect(screen.queryByText("Cache Savings")).not.toBeInTheDocument();
  });

  it("truncates a long session id with copy-on-click, and surfaces route/compact outcome chips", async () => {
    const longId = "session-2026-05-16-abcdefghijklmnopqrstuvwxyz";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    mockFetch({
      [`/api/v1/sessions/${longId}`]: jsonResponse({
        session_id: longId,
        started_at: "2026-05-16T00:00:00Z",
        ended_at: "2026-05-16T00:05:00Z",
        duration_seconds: 300,
        active_duration_seconds: 240,
        vendor: "Anthropic",
        started_model: "claude-sonnet-4-6",
        cost_status: "recorded",
        agent_settings: {},
        skills: [],
        telemetry: {},
        raw_artifact_ids: [],
        total_turns: 1,
        total_cost_usd: 0.1,
        total_atelier_savings_usd: 0.01,
        label: null,
        models_used: { "claude-sonnet-4-6": 1 },
        input_tokens: 100,
        output_tokens: 50,
        cached_input_tokens: 0,
        tool_call_count: 0,
        input_token_cost_usd: 0.05,
        cache_write_cost_usd: 0,
        cache_read_cost_usd: 0,
        output_token_cost_usd: 0.05,
        cache_write_tokens: 0,
        cache_read_tokens: 0,
        routing_downtiered_turns: 0,
        routing_savings_usd: 0,
        compact_events: 0,
        compact_savings_estimate_usd: 0,
        top_tools_by_cost: [],
      }),
      [`/api/v1/traces/${longId}`]: jsonResponse({
        id: longId,
        session_id: longId,
        agent: "copilot",
        model: "claude-sonnet-4-6",
        task: "Long id session",
        status: "success",
        files_touched: [],
        tools_called: [],
        commands_run: [],
        errors_seen: [],
        repeated_failures: [],
        validation_results: [],
        created_at: "2026-05-16T00:00:00Z",
      }),
      [`/api/ledgers/${longId}`]: jsonResponse({ conversations: [] }),
      [`/api/v1/outcomes/${longId}`]: jsonResponse([
        { kind: "route", outcome_window: { outcome_score: 0.8 } },
        { kind: "compact", outcome_window: { outcome_score: 0.6 } },
      ]),
    });

    render(<SessionExplorerDetail sessionId={longId} />);

    const sessionPill = await screen.findByTitle(`Copy: ${longId}`);
    expect(sessionPill.textContent).toMatch(/…/);
    expect(sessionPill.textContent).not.toBe(`Session${longId}`);

    fireEvent.click(sessionPill);
    expect(writeText).toHaveBeenCalledWith(longId);
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("Toggle Detailed Metrics"));
    expect(await screen.findByText("0.80")).toBeInTheDocument();
    expect(await screen.findByText("0.60")).toBeInTheDocument();
  });
});
