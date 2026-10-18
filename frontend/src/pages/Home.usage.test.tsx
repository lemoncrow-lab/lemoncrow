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

  it("shows real sessions with no data instead of a fabricated zero", async () => {
    show(false);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("16 sessions")).toBeInTheDocument();
    expect(within(summary).getAllByText("No data")).toHaveLength(4);
    expect(within(summary).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows spend when savings evidence is missing", async () => {
    show(true, 2);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("$2.000")).toBeInTheDocument();
    expect(within(summary).getAllByText("No data")).toHaveLength(3);
    expect(within(summary).queryByText(/baseline/)).not.toBeInTheDocument();
  });

  it("keeps measured zero distinct from missing data", async () => {
    show(true, 0, true);
    const summary = screen.getByRole("region", { name: "Savings summary" });
    expect(await within(summary).findByText("16 sessions")).toBeInTheDocument();
    expect(within(summary).queryByText("No data")).not.toBeInTheDocument();
    expect(within(summary).getAllByText("$0.000").length).toBeGreaterThan(0);
  });
});
