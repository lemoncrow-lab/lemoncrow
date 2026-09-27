import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  currentReviewId: vi.fn(() => "review-1"),
  fetchSurfacePreview: vi.fn(),
}));
vi.mock("./reviewApi", () => api);

import SurfacePluginPreview, { previewSides, surfaceComparable } from "./SurfacePluginPreview";
import type { ReviewSurfaceInfo, ReviewSurfacePreviewDescriptor } from "./types";

let nextUrl = 0;
function installObjectUrls() {
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:preview-${nextUrl++}`) });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
}

afterEach(() => {
  cleanup();
  api.fetchSurfacePreview.mockReset();
  Reflect.deleteProperty(URL, "createObjectURL");
  Reflect.deleteProperty(URL, "revokeObjectURL");
});

function descriptor(overrides: Partial<ReviewSurfacePreviewDescriptor> = {}): ReviewSurfacePreviewDescriptor {
  return {
    version: 1,
    renderer: "storybook",
    display: "image",
    media_type: "image/png",
    label: "Storybook story",
    sides: ["old", "new"],
    scope: "web",
    options: { story_id: "design-button--primary" },
    status: "available",
    reason: "",
    ...overrides,
  };
}

function surface(overrides: Partial<ReviewSurfaceInfo> = {}): ReviewSurfaceInfo {
  return {
    id: "web:design-button--primary",
    provider: "storybook",
    kind: "storybook.story",
    title: "Design/Button · Primary",
    locator: "design-button--primary",
    runtime: "",
    affected_paths: ["web/src/Button.tsx"],
    capabilities: ["preview", "compare", "source"],
    metadata: {},
    preview: descriptor(),
    ...overrides,
  };
}

function artifact(side: string, type = "image/png", body = side) {
  return { blob: new Blob([body], { type }), mediaType: type, filename: `story-${side}.png`, revisionId: "rrv-7" };
}

describe("SurfacePluginPreview", () => {
  it("selects sides from the descriptor, not the surface kind", () => {
    expect(previewSides(descriptor(), "preview")).toEqual(["new"]);
    expect(previewSides(descriptor(), "compare")).toEqual(["old", "new"]);
    expect(previewSides(descriptor({ sides: ["old"] }), "preview")).toEqual(["old"]);
    expect(surfaceComparable(surface({ kind: "anything.at.all" }))).toBe(true);
    expect(surfaceComparable(surface({ preview: descriptor({ status: "unavailable" }) }))).toBe(false);
  });

  it("renders the new side of an image artifact pinned to the requested Snapshot", async () => {
    installObjectUrls();
    api.fetchSurfacePreview.mockImplementation(async (_id: string, _provider: string, _surface: string, side: string) => artifact(side));

    render(<SurfacePluginPreview surface={surface()} revisionId="rrv-7" mode="preview" />);

    const image = await screen.findByRole("img", { name: "Storybook story · new side" });
    expect(image.getAttribute("src")).toMatch(/^blob:preview-/);
    expect(api.fetchSurfacePreview).toHaveBeenCalledTimes(1);
    expect(api.fetchSurfacePreview).toHaveBeenCalledWith("review-1", "storybook", "web:design-button--primary", "new", "rrv-7");
  });

  it("compares both image sides with the overlay", async () => {
    installObjectUrls();
    api.fetchSurfacePreview.mockImplementation(async (_id: string, _provider: string, _surface: string, side: string) => artifact(side));

    render(<SurfacePluginPreview surface={surface()} revisionId="rrv-7" mode="compare" />);

    expect(await screen.findByTestId("image-overlay-compare")).toBeTruthy();
    expect(api.fetchSurfacePreview.mock.calls.map((call) => call[3]).sort()).toEqual(["new", "old"]);
  });

  it("keeps a failed side specific and retryable while the other side renders", async () => {
    installObjectUrls();
    let attempts = 0;
    api.fetchSurfacePreview.mockImplementation(async (_id: string, _provider: string, _surface: string, side: string) => {
      if (side === "old" && attempts++ === 0) {
        throw new Error("Storybook story is unavailable on the old side: story design-button--primary does not exist in the old side's Storybook");
      }
      return artifact(side, "text/plain", `${side} text`);
    });

    render(<SurfacePluginPreview surface={surface({ preview: descriptor({ display: "text", media_type: "text/plain" }) })} revisionId="rrv-7" mode="compare" />);

    const oldPane = await screen.findByTestId("plugin-preview-side:old");
    const alert = await within(oldPane).findByRole("alert");
    expect(alert.textContent).toContain("does not exist in the old side's Storybook");
    expect(alert.textContent).toContain("Source review below is unaffected.");
    expect(await within(screen.getByTestId("plugin-preview-side:new")).findByText("new text")).toBeTruthy();

    await userEvent.click(within(oldPane).getByRole("button", { name: /Retry/ }));
    expect(await within(screen.getByTestId("plugin-preview-side:old")).findByText("old text")).toBeTruthy();
  });

  it("shows untrusted HTML only inside an empty sandbox", async () => {
    installObjectUrls();
    api.fetchSurfacePreview.mockResolvedValue(artifact("new", "text/html", "<table><td>2</td></table>"));

    render(
      <SurfacePluginPreview
        surface={surface({ provider: "csv-table", kind: "data.table", preview: descriptor({ renderer: "csv-table", display: "html", media_type: "text/html", label: "CSV table" }) })}
        revisionId="rrv-7"
        mode="preview"
      />,
    );

    const frame = await screen.findByTitle("CSV table · new side");
    await waitFor(() => expect(frame.getAttribute("srcdoc")).toBe("<table><td>2</td></table>"));
    expect(frame.getAttribute("sandbox")).toBe("");
  });

  it("explains an unavailable renderer without fetching", () => {
    render(
      <SurfacePluginPreview
        surface={surface({ preview: descriptor({ renderer: "figma", label: "", status: "unavailable", reason: "preview renderer 'figma' is not installed" }) })}
        revisionId="rrv-7"
        mode="preview"
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("figma preview unavailable");
    expect(alert.textContent).toContain("preview renderer 'figma' is not installed");
    expect(api.fetchSurfacePreview).not.toHaveBeenCalled();
  });

  it("falls back to surface facts and runtime execution for an unknown kind", async () => {
    const onRun = vi.fn();
    const onOpenPath = vi.fn();
    render(
      <SurfacePluginPreview
        surface={surface({
          provider: "maestro",
          kind: "mobile.flow",
          locator: "flows/login.yaml",
          runtime: "android",
          capabilities: ["source", "execute", "compare"],
          affected_paths: ["flows/login.yaml"],
          metadata: { device: "pixel-8", surface_id: "flows" },
          preview: null,
        })}
        revisionId="rrv-7"
        mode="compare"
        run={{ new: { surface_id: "x", provider: "maestro", revision_id: "rrv-7", side: "new", status: "passed", summary: "flow passed", runner: "maestro", runtime: "android", exit_code: 0, duration_ms: 12, output: "", data: {} } }}
        onRun={onRun}
        onOpenPath={onOpenPath}
      />,
    );

    expect(screen.getByText("mobile.flow")).toBeTruthy();
    expect(screen.getByText("pixel-8")).toBeTruthy();
    expect(screen.getByText("flow passed")).toBeTruthy();
    expect(screen.getByText("Not run yet")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "flows/login.yaml" }));
    expect(onOpenPath).toHaveBeenCalledWith("flows/login.yaml");
    await userEvent.click(screen.getByRole("button", { name: "Run compare" }));
    expect(onRun).toHaveBeenCalledWith(true);
    expect(api.fetchSurfacePreview).not.toHaveBeenCalled();
  });
});
