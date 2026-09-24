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
    expect(screen.getByText(/Loading benchmark reports/i)).toBeInTheDocument();
  });

  it("shows empty state when no reports", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));
    renderReports();
    expect(await screen.findByText(/No benchmark reports yet/i)).toBeInTheDocument();
  });

  it("renders report list and content", async () => {
    const reportList = [
      {
        id: "proj_test:codebench:20260920T203438Z",
        project_id: "proj_test",
        project_root: "/workspace/lemoncrow",
        project_label: "lemoncrow",
        suite: "codebench",
        run_id: "20260920T203438Z",
        generated_at: "2026-09-20T20:34:38Z",
        has_report: true,
        files: ["report.txt", "summary.csv"],
      },
    ];
    const reportContent = {
      ...reportList[0],
      markdown: "# CodeBench\n\nSome **content** here.",
    };

    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (
        url.includes(
          "/v1/reports/proj_test/codebench/20260920T203438Z"
        )
      ) {
        return Promise.resolve(jsonResponse(reportContent));
      }
      return Promise.resolve(jsonResponse(reportList));
    });

    renderReports();
    expect(await screen.findByText("codebench")).toBeInTheDocument();
    expect(screen.getByText("20260920T203438Z")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "CodeBench" })).toBeInTheDocument();
  });
});
