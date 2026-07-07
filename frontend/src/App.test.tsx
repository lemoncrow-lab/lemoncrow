import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { TimeRangeProvider } from "./lib/TimeRangeContext";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// Every route under test only needs to render without throwing — pages
// already degrade gracefully (empty state / error text) on failed fetches,
// so a single catch-all mock is enough for a router-level smoke check.
function mockAllFetches() {
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
    "/overview",
    "/sessions",
    "/swarms",
    "/costs",
    "/costs/spend",
    "/costs/savings",
    "/costs/advisor",
    "/costs/reports",
    "/knowledge",
    "/knowledge/blocks",
    "/knowledge/memory",
    "/knowledge/failures",
    "/knowledge/plans",
    "/knowledge/rubrics",
    "/system",
    "/system/health",
    "/system/hosts",
    "/system/agents",
    "/system/skills",
    "/system/mcp",
    "/system/telemetry",
    "/system/watchdogs",
    "/system/projection",
  ])("renders %s without crashing", async (path) => {
    mockAllFetches();
    const { container } = renderAt(path);
    expect(container).toBeTruthy();
  });

  it("renders all six top-level nav tabs", async () => {
    mockAllFetches();
    renderAt("/overview");
    expect(
      await screen.findByRole("link", { name: /overview/i })
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sessions/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /swarms/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /costs/i })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /knowledge/i })
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /system/i })).toBeInTheDocument();
  });
});
