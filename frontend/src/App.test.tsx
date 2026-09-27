import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { TimeRangeProvider } from "./lib/TimeRangeContext";

vi.mock("./components/CodeGraph", () => ({
  default: () => <div data-testid="code-graph" />,
}));

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// Every route under test only needs to render without throwing — pages
// already degrade gracefully (empty state / error text) on failed fetches,
// so a single catch-all mock is enough for a router-level smoke check.
function mockAllFetches(options: { failSavings?: boolean } = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/api/traces")) {
      return Promise.resolve(
        jsonResponse({
          items: [],
          metrics: {
            stats: { total: 0, success: 0, failed: 0, partial: 0 },
            hosts: [],
            domains: [],
          },
        })
      );
    }
    if (url.includes("/api/v1/swarm/runs")) {
      return Promise.resolve(jsonResponse([]));
    }
    if (url.includes("/api/v1/insights")) {
      return Promise.resolve(
        jsonResponse({
          since: "2026-09-16T00:00:00Z",
          until: "2026-09-23T00:00:00Z",
          session_count: 12,
          total_duration_seconds: 0,
          total_cost_usd: 93,
          total_lemoncrow_savings_usd: 999,
          cost_by_vendor: {},
          cost_by_tool: {},
          cost_by_model: {},
          top_sessions: [],
          outcomes_summary: {
            route_decisions: 0,
            route_avg_score: 0,
            compact_events: 0,
            compact_avg_score: 0,
            sessions_with_high_extra_reads: [],
          },
          opportunities: [],
        })
      );
    }
    if (url.includes("/api/v1/savings/summary")) {
      if (options.failSavings) {
        return Promise.resolve(new Response("savings unavailable", { status: 500 }));
      }
      return Promise.resolve(
        jsonResponse({
          window_days: 7,
          total_naive_tokens: 200000,
          total_actual_tokens: 125000,
          reduction_pct: 37.5,
          per_lever: {},
          by_day: [],
          saved_usd: 40,
          saved_pct: 37.5,
          carry_usd: 2.18,
          total_saved_usd: 42.18,
          would_have_cost_usd: 135.18,
          actually_cost_usd: 93,
          cost_basis: "session_ledger",
          ledger_tokens_saved: 75000,
        })
      );
    }
    if (
      url.includes("/api/v1/sessions") ||
      url.includes("/api/blocks") ||
      url.includes("/api/clusters") ||
      url.includes("/api/plans") ||
      url.includes("/api/skills") ||
      url.includes("/api/agents") ||
      url.includes("/api/hosts") ||
      url.includes("/api/mcp/status") ||
      url.includes("/api/v1/rubrics") ||
      url.includes("/api/v1/reports") ||
      url.includes("/api/v1/memory/facts")
    ) {
      return Promise.resolve(jsonResponse([]));
    }
    if (url.includes("/api/health")) {
      return Promise.resolve(
        jsonResponse({ status: "ok", timestamp: "2026-05-08T09:00:00Z" })
      );
    }
    return Promise.resolve(new Response("not found", { status: 404 }));
  });
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <TimeRangeProvider>
        <App />
      </TimeRangeProvider>
    </MemoryRouter>
  );
}

describe("App router smoke test", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    "/home",
    "/reviews",
    "/runs",
    "/runs/example",
    "/code",
    "/usage",
    "/knowledge",
    "/knowledge/blocks",
    "/knowledge/memory",
    "/knowledge/failures",
    "/knowledge/plans",
    "/knowledge/rubrics",
    "/settings",
    "/settings/integrations",
    "/settings/diagnostics",
    "/settings/telemetry",
    "/settings/advanced",
    "/system/hosts",
    "/system/agents",
    "/system/skills",
    "/system/mcp",
    "/system/watchdogs",
    "/system/projection",
  ])("renders %s without crashing", async (path) => {
    mockAllFetches();
    const { container } = renderAt(path);
    expect(container).toBeTruthy();
  });

  it("keeps only the core product jobs in the top-level navigation", async () => {
    mockAllFetches();
    renderAt("/home");
    for (const label of ["Home", "Reviews", "Runs", "Code", "Usage"]) {
      expect(
        await screen.findByRole("link", { name: new RegExp(`^${label}$`, "i") })
      ).toBeInTheDocument();
    }
    expect(screen.queryByRole("link", { name: /^integrations$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^activity$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^settings$/i })).toHaveAttribute(
      "href",
      "/settings/integrations"
    );
  });

  it("shows canonical savings prominently on Home", async () => {
    mockAllFetches();
    renderAt("/home");

    const summary = await screen.findByLabelText("Savings summary");
    expect(summary).toHaveTextContent("12 sessions");
    expect(summary).toHaveTextContent("$42.180");
    expect(summary).toHaveTextContent("37.5%");
    expect(summary).toHaveTextContent("$93.000 / $135.18 baseline");
    expect(summary).toHaveTextContent("75.0k");
    expect(screen.getByRole("link", { name: /details/i })).toHaveAttribute(
      "href",
      "/usage?inspect=savings"
    );
    expect(summary).not.toHaveTextContent("$999");
  });

  it("surfaces a partial savings failure on Home", async () => {
    mockAllFetches({ failSavings: true });
    renderAt("/home");

    expect(
      await screen.findByText("Savings data is unavailable. Usage details may still be available.")
    ).toBeInTheDocument();
    expect(screen.queryByText("Home data is unavailable.")).not.toBeInTheDocument();
  });

  it("folds swarm execution into Runs as a run-type filter", async () => {
    mockAllFetches();
    renderAt("/runs");
    expect(await screen.findByLabelText("Run type")).toHaveValue("sessions");
    expect(screen.getByRole("option", { name: "Swarm runs" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^swarms$/i })).not.toBeInTheDocument();
  });

  it("does not expose internal Knowledge as Code navigation", async () => {
    mockAllFetches();
    renderAt("/knowledge/memory");
    expect(screen.queryByRole("link", { name: /index & map/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^knowledge$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^code$/i })).toHaveAttribute(
      "aria-current",
      "page"
    );
  });

  it("keeps Usage as one dashboard without secondary product tabs", async () => {
    mockAllFetches();
    renderAt("/usage");
    expect(screen.getByRole("link", { name: /^usage$/i })).toHaveAttribute(
      "aria-current",
      "page"
    );
    for (const hidden of ["Spend", "Savings", "Benchmarks", "Advisor", "Reports"]) {
      expect(
        screen.queryByRole("link", { name: new RegExp(`^${hidden}$`, "i") })
      ).not.toBeInTheDocument();
    }
  });

  it("opens deeper Usage data in the inspector instead of subroutes", async () => {
    mockAllFetches();
    renderAt("/usage?inspect=benchmarks");
    expect(await screen.findByLabelText("Usage inspector")).toBeInTheDocument();
    expect(screen.getByLabelText("Usage inspector view")).toHaveValue("benchmarks");
  });

  it("does not keep obsolete product routes", async () => {
    mockAllFetches();
    for (const oldPath of ["/overview", "/sessions", "/swarms", "/map", "/integrations", "/costs"]) {
      const { unmount } = renderAt(oldPath);
      expect(
        await screen.findByRole("link", { name: /^home$/i })
      ).toHaveAttribute("aria-current", "page");
      unmount();
    }
  });

  it("uses Settings secondary navigation for setup and diagnostics", async () => {
    mockAllFetches();
    renderAt("/settings/telemetry");
    for (const label of ["Integrations", "Diagnostics", "Telemetry", "Advanced"]) {
      expect(
        await screen.findByRole("link", { name: new RegExp(`^${label}$`, "i") })
      ).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: /^telemetry$/i })).toHaveAttribute(
      "aria-current",
      "page"
    );
    for (const internal of ["Hosts", "Agents", "Skills", "MCP", "Watchdogs", "Projection"]) {
      expect(
        screen.queryByRole("link", { name: new RegExp(`^${internal}$`, "i") })
      ).not.toBeInTheDocument();
    }
  });
});
