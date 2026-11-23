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
    // The run id sits behind a copy button, not in the reading line.
    expect(await screen.findByRole("button", { name: /Copy Run ID 20260920T203438Z/ })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "CodeBench" })).toBeInTheDocument();
  });

  it("keeps a plain-text report's columns instead of folding them into a paragraph", async () => {
    const meta = {
      id: "p:codebench:r1", project_id: "p", project_root: "/w", project_label: "w",
      suite: "codebench", run_id: "r1", generated_at: "2026-09-20T20:34:38Z", has_report: true, files: ["report.txt"],
    };
    const text = "=== CodeBench head-to-head ===\nmetric     baseline  lemoncrow\ncost_usd   6.1842    4.0100";
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(jsonResponse(String(input).includes("/v1/reports/p/") ? { ...meta, markdown: text, format: "text" } : [meta])),
    );
    renderReports();
    const pre = await screen.findByText(/=== CodeBench head-to-head ===/);
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe(text);
  });
});
