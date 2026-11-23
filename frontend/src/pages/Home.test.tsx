import Home, { reviewPlace } from "./Home";
import { render, screen, waitFor, within } from "@testing-library/react";
import { HomeSourceProvider } from "../review/home/homeApi";
import { MemoryRouter } from "react-router-dom";
import { api } from "../api";
import * as reviewApi from "../review/reviewApi";
import {
  LOCAL_RUNTIME_CAPABILITIES,
  ProductCapabilitiesProvider,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  RUNTIME_ONLY_PRODUCT_CAPABILITIES,
  RuntimeCapabilitiesProvider,
} from "../lib/RuntimeCapabilities";
import type { ReviewDirectoryRow } from "../review/types";

const row = (overrides: Partial<ReviewDirectoryRow>): ReviewDirectoryRow => ({
  id: "rev-1",
  ref: "r/abc",
  review_path: "/r/abc",
  repo_id: "repo_6bab3bd1e08710426daa8d09afdef3c9",
  title: "working tree",
  source_ref: "working_tree",
  status: "open",
  revision_number: 0,
  revision_id: "rrv-1",
  updated_at: "",
  created_at: "",
  ...overrides,
});

afterEach(() => vi.restoreAllMocks());

describe("Home source availability", () => {
  it("keeps review content while marking failed run history unavailable", async () => {
    vi.spyOn(api, "insightsWindow").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(api, "traces").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(api, "savingsSummary").mockRejectedValue(new Error("unavailable"));
    vi.spyOn(reviewApi, "fetchReviewDirectory").mockResolvedValue({
      reviews: [row({ title: "A real review", project: "billing-api", branch: "main", next_action: { owner_ids: ["me"], for_me: true, my_reasons: [], reasons: [] } })], next_cursor: "",
    } as Awaited<ReturnType<typeof reviewApi.fetchReviewDirectory>>);
    render(<MemoryRouter><RuntimeCapabilitiesProvider value={{ ...LOCAL_RUNTIME_CAPABILITIES, localIntegrations: false }}>
      <Home />
    </RuntimeCapabilitiesProvider></MemoryRouter>);
    expect(await screen.findByText("Coding-agent activity is unavailable.")).toBeInTheDocument();
    expect(screen.getAllByText("A real review")).toHaveLength(2);
    expect(screen.queryByText("No active sessions.")).not.toBeInTheDocument();
    expect(screen.queryByText("No failed sessions.")).not.toBeInTheDocument();
    // The page has a name, and one word for agent work: sessions, as the Activity page lists them.
    expect(screen.getByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByText("Failed sessions")).toBeInTheDocument();
    expect(screen.queryByText(/\b[Rr]uns?\b/)).toBeNull();
    expect(screen.queryByText("No recent runs.")).not.toBeInTheDocument();
    expect(screen.getByText("1 item")).toBeInTheDocument();
    // Recent reviews name the place as Needs attention does, never the opaque repository id.
    expect(screen.getAllByText(/billing-api · main/)).toHaveLength(2);
    expect(screen.queryByText("repo_6bab3bd1e08710426daa8d09afdef3c9")).not.toBeInTheDocument();
  });

  it("shows one server-down state with the start command and Retry when nothing answers", async () => {
    const down = new TypeError("Failed to fetch");
    vi.spyOn(api, "insightsWindow").mockRejectedValue(down);
    const traces = vi.spyOn(api, "traces").mockRejectedValue(down);
    vi.spyOn(api, "savingsSummary").mockRejectedValue(down);
    vi.spyOn(reviewApi, "fetchReviewDirectory").mockRejectedValue(down);
    render(<MemoryRouter><RuntimeCapabilitiesProvider value={{ ...LOCAL_RUNTIME_CAPABILITIES, localIntegrations: false }}>
      <Home />
    </RuntimeCapabilitiesProvider></MemoryRouter>);
    expect(await screen.findByText("Can't reach LemonCrow on this machine")).toBeInTheDocument();
    expect(screen.getByText(/local_server\.sh restart/)).toBeInTheDocument();
    expect(screen.queryByText(/is unavailable/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Savings couldn't be loaded/)).not.toBeInTheDocument();
    screen.getByRole("button", { name: /Retry/ }).click();
    await waitFor(() => expect(traces).toHaveBeenCalledTimes(2));
  });
});

describe("Home and the Reviews screen agree", () => {
  it("reads the Home source and counts only reviews that wait on the viewer", async () => {
    const load = vi.fn().mockResolvedValue({
      rows: [
        row({ id: "mine", title: "Waiting on me", next_action: { owner_ids: ["me"], for_me: true, my_reasons: [], reasons: [] } }),
        row({ id: "theirs", title: "Waiting on someone else", views: ["participated"], next_action: { owner_ids: ["x"], for_me: false, my_reasons: [], reasons: [] } }),
      ],
      teamsSupported: true,
      truncated: false,
    });
    const directory = vi.spyOn(reviewApi, "fetchReviewDirectory");
    render(
      <MemoryRouter>
        <ProductCapabilitiesProvider value={REVIEW_ONLY_PRODUCT_CAPABILITIES}>
          <HomeSourceProvider value={{ load, terminal: false }}>
            <Home />
          </HomeSourceProvider>
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByText("1 item")).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
    expect(directory).not.toHaveBeenCalled();
    const waiting = screen.getByText("Waiting on you").closest(".hunk") as HTMLElement;
    expect(within(waiting).getByText("Waiting on me")).toBeInTheDocument();
    expect(within(waiting).queryByText("Waiting on someone else")).toBeNull();
  });
});

describe("Home product isolation", () => {
  it("does not call Runtime APIs in a Review-only installation", async () => {
    const insights = vi.spyOn(api, "insightsWindow");
    const traces = vi.spyOn(api, "traces");
    const savings = vi.spyOn(api, "savingsSummary");
    const reviews = vi.spyOn(reviewApi, "fetchReviewDirectory").mockResolvedValue({
      reviews: [], status: "all", query: "", next_cursor: "",
    });

    render(
      <MemoryRouter>
        <ProductCapabilitiesProvider value={REVIEW_ONLY_PRODUCT_CAPABILITIES}>
          <Home />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(reviews).toHaveBeenCalledTimes(1));
    // The bounded Inbox view mode, never the legacy list that opens every Review.
    expect(reviews.mock.calls[0][0].view).toBe("all");
    expect(insights).not.toHaveBeenCalled();
    expect(traces).not.toHaveBeenCalled();
    expect(savings).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Savings summary")).not.toBeInTheDocument();
    expect(screen.queryByText("Active now")).not.toBeInTheDocument();
  });

  it("does not call Review APIs in a Runtime-only installation", async () => {
    vi.spyOn(api, "insightsWindow").mockResolvedValue({} as Awaited<ReturnType<typeof api.insightsWindow>>);
    vi.spyOn(api, "traces").mockResolvedValue({
      items: [],
      metrics: {
        stats: { total: 0, success: 0, failed: 0, partial: 0 },
        hosts: [],
        domains: [],
        workspaces: [],
      },
    });
    const savings = vi.spyOn(api, "savingsSummary").mockResolvedValue({
      window_days: 7,
      total_naive_tokens: 0,
      total_actual_tokens: 0,
      reduction_pct: 0,
      per_lever: {},
      by_day: [],
    } as Awaited<ReturnType<typeof api.savingsSummary>>);
    const reviews = vi.spyOn(reviewApi, "fetchReviewDirectory");

    render(
      <MemoryRouter>
        <ProductCapabilitiesProvider value={RUNTIME_ONLY_PRODUCT_CAPABILITIES}>
          <Home />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(savings).toHaveBeenCalledTimes(1));
    expect(reviews).not.toHaveBeenCalled();
    expect(screen.queryByText("Recent reviews")).not.toBeInTheDocument();
  });
});

describe("Home review place", () => {
  it("names the project, a distinct worktree and the branch instead of the opaque repository id", () => {
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "agentic-review",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · agentic-review · main");
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "lemoncrow-dev",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · main");
    expect(reviewPlace(row({ directory: "demo" }))).toBe("demo");
  });

  it("names a hosted review by its repository name, never by an id echoed as the name", () => {
    expect(reviewPlace(row({ repo_name: "preview-shop" }))).toBe("preview-shop");
    expect(reviewPlace(row({ repo_name: "preview-shop", branch: "fix-tax" }))).toBe("preview-shop · fix-tax");
    expect(reviewPlace(row({ repo_name: "repo_c0b1dcab0123456789abcdef" }))).toBe("repo_6bab3bd1e08710426daa8d09afdef3c9");
  });

  it("falls back to the repository id only when nothing human is known", () => {
    expect(reviewPlace(row({}))).toBe("repo_6bab3bd1e08710426daa8d09afdef3c9");
  });
});
