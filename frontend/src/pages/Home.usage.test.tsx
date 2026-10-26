import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Home from "./Home";
import { api } from "../api";

vi.mock("../lib/RuntimeCapabilities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/RuntimeCapabilities")>();
  return {
    ...actual,
    useRuntimeCapabilities: () => ({ ...actual.LOCAL_RUNTIME_CAPABILITIES, localIntegrations: false }),
  };
});
vi.mock("../review/reviewApi", () => ({
  fetchReviewDirectory: () => Promise.resolve({ reviews: [] }),
  reviewHref: (ref: string) => ref,
}));

describe("Home usage availability", () => {
  afterEach(() => vi.restoreAllMocks());

  function show(spendAvailable: boolean, actualCost = 0, savingsAvailable = false) {
    vi.spyOn(api, "insightsWindow").mockResolvedValue({ session_count: 16 } as never);
    vi.spyOn(api, "traces").mockResolvedValue({ items: [] } as never);
    vi.spyOn(api, "savingsSummary").mockResolvedValue({
      window_days: 7, total_naive_tokens: 0, total_actual_tokens: 0,
      reduction_pct: 0, per_lever: {}, by_day: [],
      spend_available: spendAvailable, savings_available: savingsAvailable,
      actually_cost_usd: actualCost, would_have_cost_usd: actualCost,
    });
    render(<MemoryRouter><Home /></MemoryRouter>);
  }

  it("says what will appear and where to go when nothing is recorded, instead of four No data tiles", async () => {
    show(false);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    // Real sessions are still reported; the measures are an empty state, not a fabricated zero.
    expect(await within(summary).findByText(/Last 7 days · 16 sessions/)).toBeInTheDocument();
    expect(within(summary).getByText("No usage or savings recorded yet")).toBeInTheDocument();
    expect(within(summary).getByText(/Saved, reduction, cost and tokens saved appear here once a connected coding agent records them/)).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Open Integrations" })).toHaveAttribute("href", "/settings/integrations");
    expect(within(summary).queryByText("No data")).not.toBeInTheDocument();
    expect(within(summary).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows spend when savings evidence is missing, and explains the savings that are not there yet", async () => {
    show(true, 2);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("$2.000")).toBeInTheDocument();
    expect(within(summary).getByText("No savings measured yet")).toBeInTheDocument();
    expect(within(summary).getByText(/Saved, reduction and tokens saved appear once a task runs with/)).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "How savings are measured" })).toHaveAttribute("href", "/usage?inspect=savings");
    expect(within(summary).queryByText("No data")).not.toBeInTheDocument();
    expect(within(summary).queryByText(/baseline/)).not.toBeInTheDocument();
  });

  it("shows savings when no priced usage is recorded, and explains cost and reduction", async () => {
    show(false, 0, true);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("No priced usage yet")).toBeInTheDocument();
    expect(within(summary).getByText(/Cost and reduction appear once a session records usage for a model with a known price/)).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Open Usage" })).toHaveAttribute("href", "/usage");
    expect(within(summary).getByText("Saved")).toBeInTheDocument();
    expect(within(summary).getByText("Tokens saved")).toBeInTheDocument();
    expect(within(summary).queryByText("Cost")).not.toBeInTheDocument();
    expect(within(summary).queryByText("No data")).not.toBeInTheDocument();
  });

  it("keeps measured zero distinct from missing data", async () => {
    show(true, 0, true);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("16 sessions")).toBeInTheDocument();
    expect(within(summary).queryByText("No data")).not.toBeInTheDocument();
    expect(within(summary).getAllByText("$0.000").length).toBeGreaterThan(0);
  });
});
