import { render, screen } from "@testing-library/react";
import { SessionExplorerDetail } from "./SessionDetail";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch(responses: Record<string, Response>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(
    (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      for (const [key, response] of Object.entries(responses)) {
        if (url.includes(key)) return Promise.resolve(response);
      }
      return Promise.resolve(new Response("not found", { status: 404 }));
    }
  );
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
    expect(screen.getAllByText("claude-sonnet-4-6").length).toBeGreaterThanOrEqual(
      1
    );
    expect(screen.getByLabelText("Status: success")).toBeInTheDocument();
    expect(screen.queryByText("Estimated from trace tokens")).not.toBeInTheDocument();
  });
});
