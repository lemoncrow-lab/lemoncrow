import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import Savings from "./Savings";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderSavings() {
  return render(
    <TimeRangeProvider>
      <Savings />
    </TimeRangeProvider>
  );
}

describe("Savings page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders KPI, lever breakdown, and trend chart", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/savings/summary")) {
          return Promise.resolve(
            jsonResponse({
              window_days: 7,
              total_naive_tokens: 412000,
              total_actual_tokens: 198000,
              reduction_pct: 51.9,
              cost_basis: "context_budget",
              tracked_actual_cost_usd: 0.0421,
              tracked_baseline_cost_usd: 0.0837,
              tracked_saved_cost_usd: 0.0416,
              per_lever: {
                ast_truncation: 27000,
                search_read: 21000,
                batch_edit: 14500,
              },
              live_calls_saved: 7,
              live_saved_usd: 0.1234,
              top_sources: [
                {
                  lever: "search_read",
                  tool_name: "search",
                  calls_saved: 4,
                  tokens_saved: 21000,
                  cost_saved_usd: 0.0833,
                  time_saved_ms: 100000,
                },
              ],
              tool_aggregates: [
                {
                  tool_name: "search",
                  lever: "search_read",
                  turns: 3,
                  session_count: 1,
                  actual_tokens: 12000,
                  naive_tokens: 22000,
                  saved_tokens: 10000,
                  actual_cost_usd: 0.021,
                  baseline_cost_usd: 0.036,
                  saved_cost_usd: 0.015,
                  live_calls_saved: 4,
                  live_time_saved_ms: 100000,
                  live_saved_usd: 0.0833,
                },
              ],
              verification: {
                data_root: "/tmp/lemoncrow/.lemoncrow",
                headline_kind: "tracked_proof_reduction",
                headline_explanation:
                  "These top-line totals come from headline-eligible context-budget proof rows and exclude live-estimate-only overlays.",
                tracked_row_count: 1,
                tracked_run_count: 1,
                trace_linked_run_count: 1,
                ledger_backed_run_count: 1,
                live_event_count: 1,
                coverage_gap_count: 0,
                compact_output_row_count: 1,
                compact_output_saved_tokens: 5000,
                dominant_run: {
                  session_id: "run-proof-1",
                  agent: "codex",
                  task: "prove the savings session",
                  saved_tokens: 10000,
                  saved_cost_usd: 0.015,
                },
                dominant_item: {
                  session_id: "run-proof-1",
                  turn_index: 0,
                  tool_name: "search",
                  lever: "search_read",
                  actual_tokens: 6000,
                  naive_tokens: 11000,
                  saved_tokens: 5000,
                  created_at: "2026-04-12T10:00:00Z",
                },
                dominant_run_share_pct: 100,
                dominant_item_share_pct: 50,
                warning:
                  "1 compact-tool-output proof row(s) were excluded from the headline totals.",
              },
              session_proof: [
                {
                  session_id: "run-proof-1",
                  trace_id: "trace-proof-1",
                  agent: "codex",
                  task: "prove the savings session",
                  status: "success",
                  trace_confidence: "mcp_live",
                  created_at: "2026-04-12T10:00:00Z",
                  tracked_tool_calls: 3,
                  actual_tokens: 12000,
                  naive_tokens: 22000,
                  saved_tokens: 10000,
                  actual_cost_usd: 0.021,
                  baseline_cost_usd: 0.036,
                  saved_cost_usd: 0.015,
                  live_calls_saved: 4,
                  live_time_saved_ms: 100000,
                  live_saved_usd: 0.0833,
                  items: [
                    {
                      session_id: "run-proof-1",
                      turn_index: 0,
                      tool_name: "search",
                      lever: "search_read",
                      model: "test-model",
                      input_tokens: 0,
                      cache_read_tokens: 0,
                      cache_write_tokens: 0,
                      output_tokens: 6000,
                      actual_tokens: 6000,
                      naive_tokens: 11000,
                      saved_tokens: 5000,
                      actual_cost_usd: 0.01,
                      baseline_cost_usd: 0.018,
                      saved_cost_usd: 0.008,
                      lever_savings: { search_read: 5000 },
                      created_at: "2026-04-12T10:00:00Z",
                      source: "context_budget",
                    },
                  ],
                  has_ledger: true,
                },
              ],
              latest_benchmark: {
                session_id: "bench-ui",
                model: "test-model",
                n_prompts: 2,
                total_tokens_baseline: 1000,
                total_tokens_lemon: 600,
                tokens_saved: 400,
                reduction_pct: 40.0,
                total_cost_baseline_usd: 0.02,
                total_cost_lemoncrow_usd: 0.012,
                cost_saved_usd: 0.008,
                total_time_baseline_ms: 2000,
                total_time_lemoncrow_ms: 1500,
                time_saved_ms: 500,
                baseline_success_rate: 1,
                lemoncrow_success_rate: 1,
              },
              by_day: Array.from({ length: 7 }, (_, i) => ({
                day: `2026-04-${String(i + 10).padStart(2, "0")}`,
                naive: 30000 - i * 400,
                actual: 15000 - i * 180,
              })),
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    renderSavings();

    expect(
      await screen.findByText("Tracked Proof Reduction")
    ).toBeInTheDocument();
    expect(await screen.findByText("51.9%")).toBeInTheDocument();
    expect(await screen.findByText("Manual verification")).toBeInTheDocument();
    expect(
      await screen.findByText("Excluded Compact Output Rows")
    ).toBeInTheDocument();
    expect(await screen.findByText("Per-lever savings")).toBeInTheDocument();
    expect(await screen.findByText("Top savings sources")).toBeInTheDocument();
    expect(await screen.findByText("Per-tool cost proof")).toBeInTheDocument();
    expect(await screen.findByText("Session proof")).toBeInTheDocument();
    expect(
      await screen.findByText("Latest paired benchmark")
    ).toBeInTheDocument();
    expect(await screen.findByText("Ast Truncation")).toBeInTheDocument();
    expect(await screen.findAllByText("Search Read")).not.toHaveLength(0);
    expect(
      await screen.findByLabelText("7-day token savings trend")
    ).toBeInTheDocument();
  });

  it("renders coaching empty state when there is no telemetry", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/savings/summary")) {
          return Promise.resolve(
            jsonResponse({
              window_days: 7,
              total_naive_tokens: 0,
              total_actual_tokens: 0,
              reduction_pct: 0,
              per_lever: {},
              by_day: Array.from({ length: 7 }, (_, i) => ({
                day: `2026-04-${String(i + 10).padStart(2, "0")}`,
                naive: 0,
                actual: 0,
              })),
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    renderSavings();

    expect(
      await screen.findByText("No savings telemetry yet")
    ).toBeInTheDocument();
    expect(await screen.findByText("lemon mcp")).toBeInTheDocument();
  });

  it("renders an explicit capture-gap message instead of three empty proof columns", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/v1/savings/summary")) {
          return Promise.resolve(
            jsonResponse({
              window_days: 7,
              total_naive_tokens: 515,
              total_actual_tokens: 515,
              reduction_pct: 0,
              per_lever: {},
              by_day: Array.from({ length: 7 }, (_, i) => ({
                day: `2026-04-${String(i + 10).padStart(2, "0")}`,
                naive: 0,
                actual: 0,
              })),
              tool_aggregates: [
                {
                  tool_name: "unattributed",
                  lever: "unattributed",
                  turns: 1,
                  session_count: 1,
                  actual_tokens: 515,
                  naive_tokens: 515,
                  saved_tokens: 0,
                  actual_cost_usd: 0.007725,
                  baseline_cost_usd: 0.007725,
                  saved_cost_usd: 0,
                  live_calls_saved: 0,
                  live_time_saved_ms: 0,
                  live_saved_usd: 0,
                },
              ],
              session_proof: [
                {
                  session_id: "run-gap-1",
                  agent: "claude",
                  task: "",
                  status: "success",
                  created_at: "2026-04-12T10:00:00Z",
                  tracked_tool_calls: 1,
                  actual_tokens: 515,
                  naive_tokens: 515,
                  saved_tokens: 0,
                  actual_cost_usd: 0.007725,
                  baseline_cost_usd: 0.007725,
                  saved_cost_usd: 0,
                  live_calls_saved: 0,
                  live_time_saved_ms: 0,
                  live_saved_usd: 0,
                  capture_sources: [],
                  missing_surfaces: [],
                  items: [
                    {
                      session_id: "run-gap-1",
                      turn_index: 0,
                      tool_name: "unattributed",
                      lever: "unattributed",
                      model: "test-model",
                      input_tokens: 0,
                      cache_read_tokens: 0,
                      cache_write_tokens: 0,
                      output_tokens: 515,
                      actual_tokens: 515,
                      naive_tokens: 515,
                      saved_tokens: 0,
                      actual_cost_usd: 0.007725,
                      baseline_cost_usd: 0.007725,
                      saved_cost_usd: 0,
                      lever_savings: {},
                      created_at: "2026-04-12T10:00:00Z",
                      source: "context_budget",
                    },
                  ],
                  has_ledger: false,
                },
              ],
            })
          );
        }
        if (url.includes("/api/ledgers/run-gap-1")) {
          return Promise.resolve(
            jsonResponse({ session_id: "run-gap-1", status: "not_found" })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    renderSavings();

    await user.click(
      await screen.findByRole("button", { name: "Inspect evidence details" })
    );

    expect(
      await screen.findByText(
        /No detailed tool-call, command, or conversation proof was stored for this session\./
      )
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        /This session only has persisted context-budget rows\./
      )
    ).toBeInTheDocument();
  });
});
