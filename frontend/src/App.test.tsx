import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import {
  CORE_ONLY_PRODUCT_CAPABILITIES,
  LOCAL_DASHBOARD_PRODUCT_CAPABILITIES,
  LOCAL_DASHBOARD_RUNTIME_CAPABILITIES,
  LOCAL_PRODUCT_CAPABILITIES,
  ProductCapabilitiesProvider,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  RUNTIME_ONLY_PRODUCT_CAPABILITIES,
  createProductCapabilities,
  type LemonCrowCapabilities,
} from "./lib/RuntimeCapabilities";
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
    if (url.includes("/api/project-knowledge")) {
      return Promise.resolve(
        jsonResponse({
          available: true,
          scope: "local",
          repositories: [],
          truncated: false,
          sync_command: "lc knowledge project sync",
          legacy_overlay_note: "Legacy overlay note",
        })
      );
    }
    if (url.includes("/api/health")) {
      return Promise.resolve(
        jsonResponse({ status: "ok", timestamp: "2026-05-08T09:00:00Z" })
      );
    }
    return Promise.resolve(new Response("not found", { status: 404 }));
  });
}

function renderAt(path: string, capabilities: LemonCrowCapabilities = LOCAL_PRODUCT_CAPABILITIES) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProductCapabilitiesProvider value={capabilities}>
        <TimeRangeProvider>
          <App />
        </TimeRangeProvider>
      </ProductCapabilitiesProvider>
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
    "/activity",
    "/activity/example",
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
    "/settings/ai",
    "/settings/editor",
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

  it("allows a hosted wrapper to inject private routes and header actions without replacing the product shell", () => {
    mockAllFetches();
    const HostedAccount = () => <div>Hosted account page</div>;
    const HostedActions = () => <a href="/account">Hosted account</a>;

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <TimeRangeProvider>
          <App
            shellComposition={{
              HeaderActions: HostedActions,
              routes: [{ path: "/account", Component: HostedAccount }],
              titles: [{ prefix: "/account", title: "Account" }],
            }}
          />
        </TimeRangeProvider>
      </MemoryRouter>
    );

    expect(screen.getByText("Hosted account page")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Hosted account" })).toHaveAttribute("href", "/account");
    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary.querySelectorAll("a")).toHaveLength(5);
    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^code$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^activity$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^reviews$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^usage$/i })).toBeInTheDocument();
  });

  it("renders composed Settings navigation as a sidebar instead of a horizontal shell row", () => {
    mockAllFetches();
    const SettingsNavigation = () => <nav aria-label="Hosted settings test"><a href="/settings/example">Example</a></nav>;
    const HostedSettingsPage = () => <div>Hosted settings page</div>;

    render(
      <MemoryRouter initialEntries={["/settings/example"]}>
        <TimeRangeProvider>
          <App
            shellComposition={{
              SettingsNavigation,
              settingsPath: "/settings/example",
            }}
            pageComposition={{ Settings: HostedSettingsPage }}
          />
        </TimeRangeProvider>
      </MemoryRouter>,
    );

    const settingsNavs = screen.getAllByRole("navigation", { name: "Hosted settings test" });
    expect(settingsNavs).toHaveLength(2);
    expect(settingsNavs.some((nav) => nav.closest("aside") !== null)).toBe(true);
    expect(settingsNavs.some((nav) => nav.parentElement?.className.includes("md:hidden"))).toBe(true);
    expect(screen.getByText("Hosted settings page")).toBeInTheDocument();
  });

  it("types the LemonCrow wordmark on hover and collapses back to the prompt", () => {
    vi.useFakeTimers();
    mockAllFetches();
    renderAt("/home");

    const brand = screen.getByRole("link", { name: "LemonCrow home" });
    const mark = screen.getByTestId("lemoncrow-brand-mark");
    expect(mark).toHaveTextContent("_");
    expect(mark).not.toHaveTextContent("LEMONCROW");
    expect(brand).toHaveStyle({ width: "2.5ch" });

    fireEvent.mouseEnter(brand);
    expect(brand).toHaveStyle({ width: "14.5ch" });
    act(() => {
      vi.advanceTimersByTime(36 * 3);
    });
    expect(mark).toHaveTextContent("LEM_");

    act(() => {
      vi.advanceTimersByTime(36 * 7);
    });
    expect(mark).toHaveTextContent("LEMONCROW_");

    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(mark).toHaveTextContent("LEMONCROW");

    fireEvent.mouseLeave(brand);
    expect(mark).toHaveTextContent("_");
    expect(brand).toHaveStyle({ width: "2.5ch" });

    vi.useRealTimers();
  });

  it("routes the LemonCrow brand back to Home", () => {
    mockAllFetches();
    renderAt("/usage");

    const brand = screen.getByRole("link", { name: "LemonCrow home" });
    expect(brand).toHaveAttribute("href", "/home");

    fireEvent.click(brand);
    expect(screen.getByRole("link", { name: /^home$/i })).toHaveAttribute("aria-current", "page");
  });

  it("moves the primary destinations into the prompt menu on compact screens", async () => {
    mockAllFetches();
    renderAt("/usage");

    const trigger = screen.getByRole("button", { name: "Open navigation" });
    expect(trigger).toHaveClass("md:hidden");

    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary).toHaveClass("hidden", "md:flex");

    await userEvent.click(trigger);
    const menu = await screen.findByRole("menu");

    for (const label of ["Home", "Code", "Activity", "Reviews", "Usage"]) {
      expect(within(menu).getByRole("menuitem", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }
    expect(within(menu).getByRole("menuitem", { name: /^usage$/i })).toHaveAttribute(
      "aria-current",
      "page"
    );
  });

  it("keeps only the core product jobs in the top-level navigation", async () => {
    mockAllFetches();
    renderAt("/home");
    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary.querySelectorAll("a")).toHaveLength(5);
    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^code$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^activity$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^reviews$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^usage$/i })).toBeInTheDocument();
    expect(screen.getByText("beta")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("link", { name: /^integrations$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^runs$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^settings$/i })).toHaveAttribute(
      "href",
      "/settings/products"
    );
  });

  it("shows only Core and Review navigation in a Review-only installation", () => {
    mockAllFetches();
    renderAt("/home", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary.querySelectorAll("a")).toHaveLength(3);
    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^reviews$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^usage$/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^code$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^activity$/i })).not.toBeInTheDocument();
  });

  it("shows only Core and Runtime navigation in a Runtime-only installation", () => {
    mockAllFetches();
    renderAt("/home", RUNTIME_ONLY_PRODUCT_CAPABILITIES);

    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary.querySelectorAll("a")).toHaveLength(4);
    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^code$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^activity$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^usage$/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^reviews$/i })).not.toBeInTheDocument();
  });

  // A local Dashboard serves the deterministic code map plus local-machine
  // editor/Ask/Review-capture actions. Full-index intelligence stays separate.
  it("shows the basic Dashboard pages and the local code map on a local server", () => {
    mockAllFetches();
    renderAt("/home", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^activity$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^usage$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^code$/i })).toHaveAttribute("href", "/code");
  });

  it("opens the deterministic /code page on a local Dashboard", async () => {
    mockAllFetches();
    renderAt("/code", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("searchbox", { name: "Find code" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "This page is available on LemonCrow hosted" })).not.toBeInTheDocument();
    // Project Knowledge is reached from Code, not from a new navigation item.
    fireEvent.click(screen.getByRole("button", { name: "Project Knowledge" }));
    expect(await screen.findByText("Nothing synced yet", undefined, { timeout: 5000 })).toBeInTheDocument();
  });

  it("offers no Project Knowledge link where the server does not serve it", async () => {
    mockAllFetches();
    renderAt("/code");

    expect(await screen.findByRole("searchbox", { name: "Find code" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Project Knowledge" })).not.toBeInTheDocument();
  });

  it("does not open hosted-only knowledge on a local Dashboard", async () => {
    mockAllFetches();
    renderAt("/knowledge/memory", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("heading", { name: "This page is available on LemonCrow hosted" })).toBeInTheDocument();
  });

  it("opens only Project Knowledge under Knowledge on a local Dashboard", async () => {
    mockAllFetches();
    renderAt("/knowledge", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    expect(await screen.findByText("Nothing synced yet", undefined, { timeout: 5000 })).toBeInTheDocument();
    const sections = screen.getByLabelText("Knowledge section");
    expect(within(sections).getByText("Project")).toBeInTheDocument();
    expect(within(sections).queryByText("Playbooks")).not.toBeInTheDocument();
    expect(within(sections).queryByText("Agent Memory")).not.toBeInTheDocument();
  });

  it("does not offer Project Knowledge where no server advertises it", async () => {
    mockAllFetches();
    renderAt("/knowledge/project");

    expect(await screen.findByRole("heading", { name: "This page is available on LemonCrow hosted" })).toBeInTheDocument();
  });

  it("does not open /code on a local Dashboard whose server serves no code map", async () => {
    mockAllFetches();
    renderAt(
      "/code",
      createProductCapabilities({
        review: true,
        runtime: true,
        mode: "local",
        runtimeCapabilities: { ...LOCAL_DASHBOARD_RUNTIME_CAPABILITIES, codeMap: false },
      }),
    );

    expect(await screen.findByRole("heading", { name: "This page is available on LemonCrow hosted" })).toBeInTheDocument();
  });

  it("lists no code-map projection inspector on a local Dashboard", async () => {
    mockAllFetches();
    renderAt("/settings/advanced", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    // Settings is a lazy chunk; under a loaded full run it can take over a second.
    expect(await screen.findByRole("link", { name: /hosts/i }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /projection/i })).not.toBeInTheDocument();
  });

  it("offers no swarm activity on a local Dashboard", async () => {
    mockAllFetches();
    renderAt("/activity", LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);

    await screen.findByRole("link", { name: /^activity$/i });
    expect(screen.queryByLabelText("Activity view")).not.toBeInTheDocument();
  });

  it("keeps a Core-only shell usable without Review or Runtime", () => {
    mockAllFetches();
    renderAt("/home", CORE_ONLY_PRODUCT_CAPABILITIES);

    const primary = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(primary.querySelectorAll("a")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /^home$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^settings$/i })).toHaveAttribute("href", "/settings/products");
  });

  it("explains an unavailable Runtime on direct deep links", () => {
    mockAllFetches();
    renderAt("/code", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    expect(screen.getByRole("heading", { name: "LemonCrow Runtime is not installed" })).toBeInTheDocument();
    expect(screen.getByText(/Other installed LemonCrow products are unaffected/)).toBeInTheDocument();
  });

  it("explains an unavailable Review on direct deep links", () => {
    mockAllFetches();
    renderAt("/reviews", RUNTIME_ONLY_PRODUCT_CAPABILITIES);

    expect(screen.getByRole("heading", { name: "LemonCrow Review is not installed" })).toBeInTheDocument();
    expect(screen.getByText(/Other installed LemonCrow products are unaffected/)).toBeInTheDocument();
  });

  it("does not expose Runtime settings in a Review-only installation", async () => {
    mockAllFetches();
    renderAt("/settings/integrations", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("heading", { name: "LemonCrow Runtime is not installed" })).toBeInTheDocument();
    const settingsNav = screen.getAllByRole("navigation").find((nav) => nav.textContent?.includes("Products"));
    expect(settingsNav).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Integrations" })).not.toBeInTheDocument();
  });

  it("shows installed product state from the capability manifest", async () => {
    mockAllFetches();
    renderAt("/settings/products", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    expect(await screen.findByTestId("product-state-review")).toHaveTextContent("Available");
    expect(screen.getByTestId("product-state-runtime")).toHaveTextContent("Not installed");
  });

  it("shows canonical savings prominently on Home", async () => {
    mockAllFetches();
    renderAt("/home");

    const summary = await screen.findByLabelText("Savings summary");
    // Pages are lazy route chunks: the region can mount before its data lands.
    await waitFor(() => expect(summary).toHaveTextContent("12 sessions"));
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

  it("folds swarm execution into Activity as an activity-type view", async () => {
    mockAllFetches();
    renderAt("/activity");
    const activityView = await screen.findByLabelText("Activity view");
    expect(activityView.closest("header")).toBeInTheDocument();
    expect(activityView).toHaveAttribute("data-value", "sessions");
    await userEvent.click(activityView);
    expect(screen.getByRole("option", { name: "Swarms" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^swarms$/i })).not.toBeInTheDocument();
  });

  it("redirects legacy Runs URLs to Activity", async () => {
    mockAllFetches();
    renderAt("/runs/example?q=retry");
    expect(await screen.findByRole("link", { name: /^activity$/i })).toHaveAttribute("aria-current", "page");
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
    expect(screen.getByLabelText("Usage inspector view")).toHaveAttribute("data-value", "benchmarks");
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

  it("keeps Advanced selected and links back from its detail pages", async () => {
    mockAllFetches();
    renderAt("/system/agents");

    expect(screen.getByRole("link", { name: /^advanced$/i })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("link", { name: /back to advanced/i }));
    expect(screen.getByRole("link", { name: /^agents internal agent definitions\./i })).toBeInTheDocument();
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

function renderComposed(
  path: string,
  capabilities: LemonCrowCapabilities,
  pageComposition: React.ComponentProps<typeof App>["pageComposition"] = {},
) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProductCapabilitiesProvider value={capabilities}>
        <TimeRangeProvider>
          <App pageComposition={pageComposition} />
        </TimeRangeProvider>
      </ProductCapabilitiesProvider>
    </MemoryRouter>
  );
}

const HOSTED_RUNTIME_FLAGS = {
  localMachine: false,
  localIntegrations: false,
  localEditor: false,
  liveWorkspace: false,
  localReviewCapture: false,
  codeAsk: false,
  swarms: false,
  localDiagnostics: false,
  knowledge: false,
};
const HOSTED_FULL_CAPABILITIES = createProductCapabilities({
  review: true,
  runtime: true,
  mode: "hosted",
  runtimeCapabilities: HOSTED_RUNTIME_FLAGS,
});
const HOSTED_REVIEW_ONLY_CAPABILITIES = createProductCapabilities({
  review: true,
  runtime: false,
  mode: "hosted",
});

describe("App shell failure and availability handling", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it("clears a page render error when the reader navigates to another page", async () => {
    mockAllFetches();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Broken = () => {
      throw new Error("usage exploded");
    };
    renderComposed("/usage", LOCAL_PRODUCT_CAPABILITIES, {
      Usage: Broken,
      Home: () => <div>Home page body</div>,
    });

    expect(await screen.findByText("usage exploded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: /^home$/i }));

    expect(await screen.findByText("Home page body")).toBeInTheDocument();
    expect(screen.queryByText("usage exploded")).not.toBeInTheDocument();
  });

  it("reloads once when a page chunk is missing after a deploy", async () => {
    mockAllFetches();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    const MissingChunk = () => {
      throw new Error("Failed to fetch dynamically imported module: https://x.test/assets/Usage-abc.js");
    };

    const view = renderComposed("/usage", LOCAL_PRODUCT_CAPABILITIES, { Usage: MissingChunk });
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));

    view.unmount();
    renderComposed("/usage", LOCAL_PRODUCT_CAPABILITIES, { Usage: MissingChunk });
    expect(await screen.findByRole("button", { name: /reload page/i })).toBeInTheDocument();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("lists only available products in the compact navigation menu", async () => {
    mockAllFetches();
    renderAt("/home", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    const menu = await screen.findByRole("menu");

    for (const label of ["Home", "Reviews", "Usage"]) {
      expect(within(menu).getByRole("menuitem", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }
    for (const label of ["Code", "Activity"]) {
      expect(within(menu).queryByRole("menuitem", { name: new RegExp(label, "i") })).not.toBeInTheDocument();
    }
  });

  it("tells a hosted user to ask their administrator when the product is not granted", async () => {
    mockAllFetches();
    renderAt("/code", HOSTED_REVIEW_ONLY_CAPABILITIES);

    expect(
      await screen.findByText(
        "Your organization doesn't have LemonCrow Runtime. Ask your LemonCrow workspace administrator for access.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view your products/i })).toHaveAttribute("href", "/account");
    expect(within(screen.getByRole("status")).queryByRole("link", { name: /installed products/i })).not.toBeInTheDocument();
    expect(screen.getByRole("status").querySelector('a[href="/settings/products"]')).toBeNull();
  });

  it("keeps the local wording for a product that is not installed", async () => {
    mockAllFetches();
    renderAt("/code", REVIEW_ONLY_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("heading", { name: "LemonCrow Runtime is not installed" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View installed products" })).toHaveAttribute(
      "href",
      "/settings/products",
    );
  });

  it("sends a hosted reader away from /knowledge sections the server does not serve", async () => {
    const fetchSpy = mockAllFetches();
    renderComposed("/knowledge/blocks", HOSTED_FULL_CAPABILITIES, { Code: () => <div>Code map page</div> });

    expect(await screen.findByText("Code map page")).toBeInTheDocument();
    expect(screen.queryByLabelText("Knowledge section")).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([input]) => String(input).includes("/api/blocks"))).toBe(false);
  });

  it("sends a hosted reader from the /knowledge index to Code", async () => {
    mockAllFetches();
    renderComposed("/knowledge", HOSTED_FULL_CAPABILITIES, { Code: () => <div>Code map page</div> });

    expect(await screen.findByText("Code map page")).toBeInTheDocument();
  });

  it("still serves /knowledge sections where the full dashboard is local", async () => {
    mockAllFetches();
    renderAt("/knowledge/blocks");

    expect(await screen.findByLabelText("Knowledge section", undefined, { timeout: 5000 })).toBeInTheDocument();
  });
});
