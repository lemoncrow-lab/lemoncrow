import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import WebReviewPreview from "./WebReviewPreview";

const api = vi.hoisted(() => {
  const fetchSurfaceImpact = vi.fn();
  return {
    fetchLiveWebPreview: vi.fn(),
    fetchWebPreview: vi.fn(),
    fetchReviewVerification: vi.fn(),
    fetchSurfaceImpact,
    fetchSurfaceImpactWithRetry: fetchSurfaceImpact,
  };
});

vi.mock("./reviewApi", () => ({
  currentReviewId: () => "review-1",
  fetchLiveWebPreview: api.fetchLiveWebPreview,
  fetchWebPreview: api.fetchWebPreview,
  fetchReviewVerification: api.fetchReviewVerification,
  fetchSurfaceImpact: api.fetchSurfaceImpact,
  fetchSurfaceImpactWithRetry: api.fetchSurfaceImpactWithRetry,
}));

let objectUrlCounter = 0;

beforeEach(() => {
  vi.clearAllMocks();
  objectUrlCounter = 0;
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => `blob:preview-${++objectUrlCounter}`),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    disconnect() {}
  });
  api.fetchReviewVerification.mockResolvedValue({ checks: [], summary: {}, artifacts: [] });
  api.fetchLiveWebPreview.mockImplementation(
    async (_id: string, _path: string, side: "old" | "new", route: string) => ({
      url: `http://127.0.0.1:4567/token/${side}${route}`,
    }),
  );
  api.fetchWebPreview.mockImplementation(
    async (_id: string, _path: string, side: "old" | "new") => new Blob([side], { type: "image/png" }),
  );
  api.fetchSurfaceImpact.mockImplementation(
    async (_id: string, provider: string, surfaceId: string) => ({
      revision_id: "rrv-1",
      provider,
      surface_id: surfaceId,
      source: "web",
      comparator: "image",
      changed: true,
      magnitude: surfaceId === "/" ? 0.2 : 0.1,
      label: surfaceId === "/" ? "Δ 20.0% pixels" : "Δ 10.0% pixels",
      reasons: ["visible pixels changed"],
      metrics: {},
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const preview = {
  kind: "web" as const,
  framework: "next",
  routes: ["/"],
  default_route: "/",
};

function props(overrides: Partial<React.ComponentProps<typeof WebReviewPreview>> = {}): React.ComponentProps<typeof WebReviewPreview> {
  return {
    preview,
    documentPath: "src/Hero.tsx",
    revisionId: "rrv-1",
    surfaceKey: "web:/",
    surfaceTitle: "/",
    serviceName: "app",
    affectedPaths: ["src/Hero.tsx", "src/hero.css"],
    framework: "next",
    chromeTheme: "light",
    active: true,
    routeOptions: [
      { route: "/", surfaceKey: "web:/", documentPath: "src/Hero.tsx" },
      { route: "/scan", surfaceKey: "web:/scan", documentPath: "src/Hero.tsx" },
    ],
    onActivate: vi.fn(),
    onOpenPath: vi.fn(),
    onOpenDedicated: vi.fn(),
    onNavigateSurface: vi.fn(),
    ...overrides,
  };
}

describe("WebReviewPreview", () => {
  it("keeps route, modes, device, theme, and affected files on one toolbar and reuses the live route", async () => {
    const first = props();
    const { rerender } = render(<WebReviewPreview {...first} />);

    await waitFor(() => expect(api.fetchLiveWebPreview).toHaveBeenCalledWith("review-1", "src/Hero.tsx", "new", "/", "rrv-1"));
    const frame = screen.getByTestId("web-live-frame-new");
    expect(frame.getAttribute("src")).toContain("lc-theme=light");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
    expect((frame.parentElement as HTMLElement).style.width).toBe("1440px");

    const routePicker = screen.getByRole("button", { name: "Rendered route" });
    expect(routePicker.textContent).toContain("/");
    await userEvent.click(routePicker);
    expect(screen.getAllByRole("option").map((option) => option.textContent?.trim())).toEqual([
      "/Δ 20.0% pixels",
      "/scanΔ 10.0% pixels",
    ]);
    expect(screen.getByRole("button", { name: "Preview mode" }).getAttribute("aria-pressed")).toBe("true");
    const openSurface = screen.getByRole("button", { name: "Open web surface / in dedicated tab" });
    expect(openSurface.textContent).toContain("Web · app");
    await userEvent.click(openSurface);
    expect(first.onOpenDedicated).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Compare mode" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Source mode" })).toBeNull();
    const viewOptions = screen.getByRole("button", { name: "Web preview options" });
    const desktop = screen.getByRole("button", { name: "Desktop viewport" });
    expect(desktop.closest("details")?.hasAttribute("open")).toBe(false);
    await userEvent.click(viewOptions);
    expect(desktop.closest("details")?.hasAttribute("open")).toBe(true);
    expect(desktop.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: /Preview theme: Auto/ })).toBeTruthy();
    expect(screen.getByText("Affected files · 2")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Mobile viewport" }));
    expect((screen.getByTestId("web-live-frame-new").parentElement as HTMLElement).style.width).toBe("390px");
    expect(api.fetchLiveWebPreview).toHaveBeenCalledTimes(1);

    rerender(<WebReviewPreview {...props({ documentPath: "src/layout.tsx" })} />);
    await waitFor(() => expect(api.fetchLiveWebPreview).toHaveBeenCalledWith("review-1", "src/layout.tsx", "new", "/", "rrv-1"));
    expect(api.fetchLiveWebPreview).toHaveBeenCalledTimes(2);

    rerender(<WebReviewPreview {...props({ documentPath: "src/layout.tsx", revisionId: "rrv-2" })} />);
    await waitFor(() => expect(api.fetchLiveWebPreview).toHaveBeenCalledWith("review-1", "src/layout.tsx", "new", "/", "rrv-2"));
    expect(api.fetchLiveWebPreview).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Mobile viewport" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("renders an inspectable frozen before/after screenshot diff for the exact Snapshot", async () => {
    render(<WebReviewPreview {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Screenshot diff mode" }));

    await waitFor(() => expect(api.fetchWebPreview).toHaveBeenCalledTimes(2));
    expect(api.fetchWebPreview).toHaveBeenCalledWith("review-1", "src/Hero.tsx", "old", "/", 1440, 5000, "rrv-1");
    expect(api.fetchWebPreview).toHaveBeenCalledWith("review-1", "src/Hero.tsx", "new", "/", 1440, 5000, "rrv-1");
    expect(await screen.findByTestId("web-frozen-screenshot-compare")).toBeTruthy();
    expect(screen.getByAltText("Before · old source screenshot")).toBeTruthy();
    expect(screen.getByAltText("After · new source screenshot")).toBeTruthy();
    expect(screen.getByText(/Source remains authoritative/i)).toBeTruthy();

    const divider = screen.getByRole("slider", { name: "Screenshot comparison divider" });
    expect(divider.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(divider, { key: "End" });
    expect(divider.getAttribute("aria-valuenow")).toBe("100");
    expect((screen.getByAltText("After · new source screenshot") as HTMLImageElement).style.clipPath).toBe("inset(0 0% 0 0)");
  });

  it("ranks only affected route options by deterministic frozen screenshot delta", async () => {
    api.fetchSurfaceImpact.mockImplementation(
      async (_id: string, provider: string, surfaceId: string) => ({
        revision_id: "rrv-1",
        provider,
        surface_id: surfaceId,
        source: "web",
        comparator: "image",
        changed: true,
        magnitude: surfaceId === "/scan" ? 0.42 : 0.03,
        label: surfaceId === "/scan" ? "Δ 42.0% pixels" : "Δ 3.0% pixels",
        reasons: ["visible pixels changed"],
        metrics: {},
      }),
    );
    render(<WebReviewPreview {...props()} />);

    await waitFor(() => expect(api.fetchSurfaceImpact).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Ranking pixels…")).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Rendered route" }));
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent?.trim())).toEqual([
      "/scanΔ 42.0% pixels",
      "/Δ 3.0% pixels",
    ]);
    expect(api.fetchSurfaceImpact).toHaveBeenCalledWith(
      "review-1", "web", "/scan", "rrv-1", 3,
    );
  });

  it("shows the exact-Snapshot browser result for this route without another pane", async () => {
    api.fetchReviewVerification.mockResolvedValue({
      checks: [
        {
          name: "Home regression · 1280x800", status: "FAIL", state: "current", required: true, detail: "/ · 1 assertion(s) failed",
          browser: { scenario: "home", route: "/", configured: true, applicability: "", run: null, baseline: null },
        },
        {
          name: "Scan smoke", status: "PASS", state: "current", required: false, detail: "",
          browser: { scenario: "scan", route: "/scan", configured: true, applicability: "", run: null, baseline: null },
        },
        {
          name: "Home regression · 1280x800", status: "PASS", state: "stale", required: false, detail: "",
          browser: { scenario: "home", route: "/", configured: true, applicability: "", run: null, baseline: null },
        },
      ],
      summary: {},
      artifacts: [],
    });
    render(<WebReviewPreview {...props()} />);
    const chip = await screen.findByText("Browser FAIL");
    expect(chip.getAttribute("title")).toContain("required");
    expect(api.fetchReviewVerification).toHaveBeenCalledWith("review-1", "rrv-1");
    expect(screen.queryByText("Browser PASS")).toBeNull();
  });

  it("filters routes without clearing the search after selection", async () => {
    const onNavigateSurface = vi.fn();
    render(<WebReviewPreview {...props({ onNavigateSurface })} />);

    await userEvent.click(screen.getByRole("button", { name: "Rendered route" }));
    const search = screen.getByRole("searchbox", { name: "Rendered route search" });
    await userEvent.type(search, "scan");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option").textContent).toContain("/scan");
    await userEvent.click(screen.getByRole("option"));
    expect(onNavigateSurface).toHaveBeenCalledWith("web:/scan");

    await userEvent.click(screen.getByRole("button", { name: "Rendered route" }));
    expect((screen.getByRole("searchbox", { name: "Rendered route search" }) as HTMLInputElement).value).toBe("scan");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(window.localStorage.getItem("lemoncrow-review-surface-filter:review-1:web")).toBe("scan");
  });

  it("fits Compare into one pane, shows scale, and can link or unlink scrolling", async () => {
    render(<WebReviewPreview {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: "Compare mode" }));

    await waitFor(() => expect(screen.getByTestId("web-live-frame-old")).toBeTruthy());
    const shell = screen.getByTestId("web-live-compare-shell");
    expect(shell.className).toContain("overflow-hidden");
    expect(screen.getAllByText(/1440px · \d+%/)).toHaveLength(2);
    expect(screen.queryByTitle("Logical viewport · presentation scale")).toBeNull();

    const before = screen.getByTestId("web-live-frame-old") as HTMLIFrameElement;
    const after = screen.getByTestId("web-live-frame-new") as HTMLIFrameElement;
    const postMessage = vi.spyOn(after.contentWindow!, "postMessage");
    const linked = new MessageEvent("message", { data: { type: "lc-review-preview-scroll", ratio: 0.42 } });
    Object.defineProperty(linked, "source", { value: before.contentWindow });
    window.dispatchEvent(linked);
    expect(postMessage).toHaveBeenCalledWith(
      { type: "lc-review-preview-scroll-to", ratio: 0.42 },
      "http://127.0.0.1:4567",
    );

    postMessage.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Web preview options" }));
    await userEvent.click(screen.getByRole("button", { name: "Unlink compare scrolling" }));
    const unlinked = new MessageEvent("message", { data: { type: "lc-review-preview-scroll", ratio: 0.7 } });
    Object.defineProperty(unlinked, "source", { value: before.contentWindow });
    window.dispatchEvent(unlinked);
    expect(postMessage).not.toHaveBeenCalled();
  });

  it("supports keyboard mode/device/navigation shortcuts and a preview-only theme override", async () => {
    const onNavigateSurface = vi.fn();
    render(<WebReviewPreview {...props({ onNavigateSurface })} />);
    await waitFor(() => expect(screen.getByTestId("web-live-frame-new")).toBeTruthy());

    await userEvent.keyboard("2");
    expect(screen.getByRole("button", { name: "Compare mode" }).getAttribute("aria-pressed")).toBe("true");
    await userEvent.keyboard("m");
    await userEvent.click(screen.getByRole("button", { name: "Web preview options" }));
    expect(screen.getByRole("button", { name: "Mobile viewport" }).getAttribute("aria-pressed")).toBe("true");
    await userEvent.keyboard("}");
    expect(onNavigateSurface).toHaveBeenCalledWith("web:/scan");

    await userEvent.click(screen.getByRole("button", { name: /Preview theme: Auto/ }));
    expect(screen.getByRole("button", { name: /Preview theme: Light/ })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /Preview theme: Light/ }));
    expect(screen.getByTestId("web-live-frame-new").getAttribute("src")).toContain("lc-theme=dark");
    expect(window.sessionStorage.getItem("lemoncrow-review-surface:review-1:web:/")).toContain('\"previewTheme\":\"dark\"');
    await userEvent.click(screen.getByRole("button", { name: /Preview theme: Dark/ }));
    expect(screen.getByRole("button", { name: /Preview theme: Auto/ })).toBeTruthy();
  });
});
