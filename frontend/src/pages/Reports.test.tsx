import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Reports from "./Reports";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderReports() {
  return render(
    <MemoryRouter>
      <Reports />
    </MemoryRouter>
  );
}

describe("Reports page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => {}));
    renderReports();
    expect(screen.getByText(/Loading reports/i)).toBeInTheDocument();
  });

  it("shows empty state when no reports", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));
    renderReports();
    expect(await screen.findByText(/No reports published yet/i)).toBeInTheDocument();
  });

  it("renders report list and content", async () => {
    const reportList = [
      {
        week: "2026-W20",
        week_start: "2026-05-11",
        generated_at: "2026-05-17T12:00:00Z",
        routing_sessions: 5,
        total_routing_savings_usd: 1.23,
        routing_quality_score: 0.85,
        compact_retention_score: 0.9,
      },
    ];
    const reportContent = {
      week: "2026-W20",
      markdown: "# Week 20\n\nSome **content** here.",
      json: {},
    };

    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/v1/reports/2026-W20")) {
        return Promise.resolve(jsonResponse(reportContent));
      }
      return Promise.resolve(jsonResponse(reportList));
    });

    renderReports();
    expect(await screen.findByText("2026-W20")).toBeInTheDocument();
  });
});
