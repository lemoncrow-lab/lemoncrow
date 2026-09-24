import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import WebReviewPreview from "./WebReviewPreview";

const api = vi.hoisted(() => ({ fetchLiveWebPreview: vi.fn() }));

vi.mock("./reviewApi", () => ({
  currentReviewId: () => "review-1",
  fetchLiveWebPreview: api.fetchLiveWebPreview,
}));

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    disconnect() {}
  });
  api.fetchLiveWebPreview.mockImplementation(
    async (_id: string, _path: string, side: "old" | "new", route: string) => ({
      url: `http://127.0.0.1:4567/token/${side}${route}`,
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
      { route: "/", surfaceKey: "web:/" },
      { route: "/scan", surfaceKey: "web:/scan" },
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
    expect(screen.getAllByRole("option").map((option) => option.textContent?.trim())).toEqual(["/", "/scan"]);
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
