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
                total_tokens_lemoncrow: 600,
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

    expect(await screen.findByText("Cost reduction")).toBeInTheDocument();
    // Plain language up top; the audit numbers sit behind a disclosure.
    expect(await screen.findByText("How this figure is worked out")).toBeInTheDocument();
    expect(screen.queryByText(/statusline\/CLI reconciliation/)).not.toBeInTheDocument();
    expect(screen.getByText("Details for auditing the total").closest("details")).not.toHaveAttribute("open");
    expect(await screen.findByText("Left out of the headline")).toBeInTheDocument();
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
    expect(await screen.findByText("lc mcp")).toBeInTheDocument();
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

  it("renders the exact shape the server sends (core/service/api.py) without crashing", async () => {
    const user = userEvent.setup();
    // Field for field what /v1/savings/summary returns for one context-budget session:
    // session_proof entries carry no status/created_at, items no model/source, benchmark uses total_tokens_lemoncrow.
    const serverShape = {
      window_days: 7,
      total_naive_tokens: 1000,
      total_actual_tokens: 600,
      reduction_pct: 40.0,
      per_lever: { batch_edit: 400 },
      by_day: [{ day: "2026-04-12", naive: 1000, actual: 600 }],
      saved_usd: 0.0012,
      saved_pct: 100.0,
      would_have_cost_usd: 0.0012,
      actually_cost_usd: 0.0,
      tracked_actual_cost_usd: 0.009,
      tracked_baseline_cost_usd: 0.0102,
      tracked_saved_cost_usd: 0.0012,
      total_calls: 0,
      live_calls_saved: 2,
      live_time_saved_ms: 50000,
      live_saved_usd: 1.66278,
      ops_saved_usd: 0,
      carry_usd: 0,
      carry_tokens: 0,
      read_saved_usd: 0,
      total_saved_usd: 0.0012,
      ledger_tokens_saved: 0,
      ledger_calls_saved: 0,
      routing_saved_usd: 0,
      top_sources: [{ lever: "batch_edit", tool_name: "edit", calls_saved: 2, tokens_saved: 400, cost_saved_usd: 0.0012, time_saved_ms: 50000 }],
      cost_only_sources: [],
      latest_benchmark: {
        session_id: "bench", model: "m", n_prompts: 2,
        total_tokens_baseline: 1000, total_tokens_lemoncrow: 600, tokens_saved: 400, reduction_pct: 40,
        total_cost_baseline_usd: 0.02, total_cost_lemoncrow_usd: 0.012, cost_saved_usd: 0.008,
        total_time_baseline_ms: 2000, total_time_lemoncrow_ms: 1500, time_saved_ms: 500,
        baseline_success_rate: 1, lemoncrow_success_rate: 1,
      },
      tracked_tool_calls: 1,
      cost_basis: "context_budget",
      tool_aggregates: [],
      session_proof: [
        {
          session_id: "run-live-1",
          agent: "codex",
          task: "ship it",
          saved_tokens: 400,
          saved_cost_usd: 0.0012,
          tracked_tool_calls: 1,
          live_calls_saved: 2,
          items: [
            {
              session_id: "run-live-1", turn_index: 0, tool_name: "mcp__lc__edit", lever: "batch_edit",
              actual_tokens: 600, naive_tokens: 1000, saved_tokens: 400,
              actual_cost_usd: 0.009, baseline_cost_usd: 0.0102, saved_cost_usd: 0.0012,
              created_at: "2026-04-12T10:00:00Z",
            },
          ],
        },
      ],
      coverage_gaps: [],
      verification: {},
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/savings/summary")) return Promise.resolve(jsonResponse(serverShape));
      if (url.includes("/api/ledgers/run-live-1")) return Promise.resolve(jsonResponse({ session_id: "run-live-1" }));
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    renderSavings();

    expect(await screen.findByText(/LemonCrow-enabled 600/)).toBeInTheDocument();
    expect(screen.getByText("ship it")).toBeInTheDocument();
    expect(screen.getByText("Tool Turns").nextSibling).toHaveTextContent("1");
    expect(screen.getByText("Live Calls Saved").nextSibling).toHaveTextContent("2");
    await user.click(screen.getByRole("button", { name: "Inspect evidence details" }));
    expect(await screen.findAllByText("Edit")).not.toHaveLength(0);
  });

  it("survives a server that leaves the counters out, showing a dash or zero instead of crashing", async () => {
    // An older server: no tracked_tool_calls / live_calls_saved anywhere, and a benchmark without token totals.
    const partial = {
      window_days: 7,
      total_naive_tokens: 1000,
      total_actual_tokens: 600,
      reduction_pct: 40.0,
      per_lever: { batch_edit: 400 },
      by_day: [{ day: "2026-04-12", naive: 1000, actual: 600 }],
      saved_usd: 0.0012,
      total_calls: 0,
      top_sources: [],
      cost_only_sources: [],
      latest_benchmark: { session_id: "bench", model: "m", n_prompts: 2, reduction_pct: 40 },
      cost_basis: "context_budget",
      tool_aggregates: [],
      session_proof: [{ session_id: "run-old-1", agent: "codex", task: "old server", saved_tokens: 400, items: [] }],
      coverage_gaps: [],
      verification: {},
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/savings/summary")) return Promise.resolve(jsonResponse(partial));
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    renderSavings();

    expect(await screen.findByText("old server")).toBeInTheDocument();
    expect(screen.getByText("Tool Turns").nextSibling).toHaveTextContent("0");
    expect(screen.getByText("Live Calls Saved").nextSibling).toHaveTextContent("0");
    expect(screen.getByText(/LemonCrow-enabled —/)).toBeInTheDocument();
  });
});
