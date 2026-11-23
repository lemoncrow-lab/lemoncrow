import { render, screen, waitFor, within } from "@testing-library/react";
import { TimeRangeProvider } from "../lib/TimeRangeContext";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
});
import {
  LOCAL_PRODUCT_CAPABILITIES,
  ProductCapabilitiesProvider,
  createProductCapabilities,
  type LemonCrowCapabilities,
} from "../lib/RuntimeCapabilities";

vi.mock("./Analytics", () => ({ default: () => <div>Analytics body</div> }));
vi.mock("../review/analytics/ReviewAnalytics", () => ({ default: () => <div>Review analytics body</div> }));
vi.mock("./Savings", () => ({ default: () => <div>Savings body</div> }));
vi.mock("./Optimizations", () => ({ default: () => <div>Optimizations body</div> }));
vi.mock("./Reports", () => ({ default: () => <div>Reports body</div> }));

import Usage from "./Usage";

const HOSTED = createProductCapabilities({
  review: true,
  runtime: true,
  mode: "hosted",
  runtimeCapabilities: {
    localMachine: false,
    localIntegrations: false,
    localEditor: false,
    liveWorkspace: false,
    localReviewCapture: false,
    codeAsk: false,
    swarms: false,
    localDiagnostics: false,
    knowledge: false,
  },
});

function renderUsage(path: string, capabilities: LemonCrowCapabilities) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <TimeRangeProvider>
        <ProductCapabilitiesProvider value={capabilities}>
          <Usage />
        </ProductCapabilitiesProvider>
      </TimeRangeProvider>
    </MemoryRouter>,
  );
}

async function openInspectorViews(path: string, capabilities: LemonCrowCapabilities) {
  renderUsage(path, capabilities);
  await userEvent.click(await screen.findByRole("combobox", { name: "Usage inspector view" }));
}

function mockReports(count: number) {
  vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) =>
    Promise.resolve(
      String(input).includes("/v1/reports")
        ? new Response(JSON.stringify(Array.from({ length: count }, (_, index) => ({ id: `r${index}` }))), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("not found", { status: 404 }),
    ),
  );
}

describe("Usage inspector", () => {
  it("offers the advisor and benchmarks locally once a benchmark report exists", async () => {
    mockReports(1);
    await openInspectorViews("/usage?inspect=savings", LOCAL_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("option", { name: "Optimization advisor" })).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "Benchmarks" })).toBeInTheDocument();
  });

  it("leaves Benchmarks out while there are no benchmark reports", async () => {
    mockReports(0);
    await openInspectorViews("/usage?inspect=savings", LOCAL_PRODUCT_CAPABILITIES);

    expect(await screen.findByRole("option", { name: "Optimization advisor" })).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(globalThis.fetch)).toHaveBeenCalled());
    expect(screen.queryByRole("option", { name: "Benchmarks" })).not.toBeInTheDocument();
  });

  it("hides the local-only advisor and benchmarks where the server lacks them", async () => {
    await openInspectorViews("/usage?inspect=savings", HOSTED);

    expect(await screen.findByRole("option", { name: "Savings evidence" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Optimization advisor" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Benchmarks" })).not.toBeInTheDocument();
  });

  it("falls back to savings evidence for a bookmarked advisor link", async () => {
    renderUsage("/usage?inspect=advisor", HOSTED);

    expect(await screen.findByText("Savings body")).toBeInTheDocument();
    expect(screen.queryByText("Optimizations body")).not.toBeInTheDocument();
  });

  it("draws an evidence view in the page under a pinned toolbar, not in a drawer that scrolls itself", async () => {
    mockReports(0);
    renderUsage("/usage?inspect=savings", LOCAL_PRODUCT_CAPABILITIES);

    expect(await screen.findByText("Savings body")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const toolbar = screen.getByTestId("usage-inspector-toolbar");
    expect(toolbar.className).toContain("sticky");
    expect(within(toolbar).getByRole("heading", { name: "Savings evidence" })).toBeInTheDocument();
    expect(within(toolbar).getByRole("combobox", { name: "Usage time window" })).toBeInTheDocument();
    // Nothing between the page and the view keeps its own scroll box.
    for (let node = screen.getByText("Savings body").parentElement; node; node = node.parentElement) {
      expect(node.className).not.toMatch(/overflow-(?:y-)?auto/);
    }

    await userEvent.click(within(toolbar).getByRole("button", { name: "Usage" }));
    expect(await screen.findByText("Analytics body")).toBeInTheDocument();
    expect(screen.queryByText("Savings body")).not.toBeInTheDocument();
  });

  it("gives the dashboard its own time window selector", async () => {
    renderUsage("/usage", LOCAL_PRODUCT_CAPABILITIES);

    expect(await screen.findByText("Analytics body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("combobox", { name: "Usage time window" }));
    await userEvent.click(await screen.findByRole("option", { name: "Last 30 days" }));
    expect(screen.getByRole("combobox", { name: "Usage time window" })).toHaveAttribute("data-value", "30d");
  });
});
