import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  currentReviewId: vi.fn(() => "review-1"),
  fetchMediaFile: vi.fn(),
}));
vi.mock("./reviewApi", () => api);
vi.mock("./PdfReviewDocument", () => ({
  default: ({ blob, label }: { blob: Blob; label: string }) => (
    <div data-testid="mock-pdf-review-document">{label}:{blob.type}</div>
  ),
}));

import MediaReviewPreview from "./MediaReviewPreview";

const createdUrls: string[] = [];

function installObjectUrls() {
  let next = 0;
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const value = `blob:media-${next++}`;
      createdUrls.push(value);
      return value;
    }),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
}

afterEach(() => {
  window.dispatchEvent(new Event("pagehide"));
  createdUrls.length = 0;
  api.fetchMediaFile.mockReset();
  Reflect.deleteProperty(URL, "createObjectURL");
  Reflect.deleteProperty(URL, "revokeObjectURL");
});

describe("MediaReviewPreview", () => {
  it("renders both frozen sides of an image in compare mode", async () => {
    installObjectUrls();
    api.fetchMediaFile.mockImplementation(async (_id: string, _path: string, side: "old" | "new") =>
      new Blob([side], { type: "image/gif" }),
    );

    render(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: "image/gif", media_kind: "image" }}
        documentPath="docs/demo.gif"
        revisionId="rrv-1"
        status="modified"
        mode="compare"
      />,
    );

    const compare = screen.getByTestId("media-review-compare");
    expect(compare.className).toContain("bg-surface-sunken");
    const before = await screen.findByRole("img", { name: "Before docs/demo.gif" });
    const after = await screen.findByRole("img", { name: "After docs/demo.gif" });
    await waitFor(() => expect(before.getAttribute("src")).toBe("blob:media-0"));
    await waitFor(() => expect(after.getAttribute("src")).toBe("blob:media-1"));
    expect(api.fetchMediaFile).toHaveBeenCalledWith("review-1", "docs/demo.gif", "old", "rrv-1");
    expect(api.fetchMediaFile).toHaveBeenCalledWith("review-1", "docs/demo.gif", "new", "rrv-1");
  });

  it("refetches the same media path when the Review revision changes", async () => {
    installObjectUrls();
    api.fetchMediaFile.mockResolvedValue(new Blob(["image"], { type: "image/png" }));

    const { rerender } = render(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: "image/png", media_kind: "image" }}
        documentPath="assets/screenshot.png"
        revisionId="rrv-1"
        status="modified"
        mode="preview"
      />,
    );
    await waitFor(() => expect(api.fetchMediaFile).toHaveBeenCalledWith(
      "review-1", "assets/screenshot.png", "new", "rrv-1",
    ));
    expect(api.fetchMediaFile).toHaveBeenCalledTimes(1);

    rerender(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: "image/png", media_kind: "image" }}
        documentPath="assets/screenshot.png"
        revisionId="rrv-2"
        status="modified"
        mode="preview"
      />,
    );
    await waitFor(() => expect(api.fetchMediaFile).toHaveBeenCalledWith(
      "review-1", "assets/screenshot.png", "new", "rrv-2",
    ));
    expect(api.fetchMediaFile).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["video", "video/mp4", "video"],
    ["audio", "audio/mpeg", "audio"],
  ] as const)("renders %s media with a native browser surface", async (_kind, mediaType, tagName) => {
    installObjectUrls();
    api.fetchMediaFile.mockResolvedValue(new Blob(["media"], { type: mediaType }));
    const mediaKind = mediaType === "video/mp4" ? "video" : "audio";

    const { container } = render(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: mediaType, media_kind: mediaKind }}
        documentPath={`assets/demo.${mediaKind === "video" ? "mp4" : "mp3"}`}
        status="added"
        mode="preview"
      />,
    );

    await waitFor(() => expect(container.querySelector(tagName)).toBeTruthy());
  });

  it("renders PDF through the in-app PDF renderer instead of an iframe", async () => {
    installObjectUrls();
    api.fetchMediaFile.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));

    const { container } = render(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: "application/pdf", media_kind: "pdf" }}
        documentPath="assets/demo.pdf"
        revisionId="rrv-1"
        status="added"
        mode="preview"
      />,
    );

    await waitFor(() => expect(screen.getByTestId("mock-pdf-review-document").textContent).toBe("After:application/pdf"));
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("uses Source for grounded metadata instead of inventing a textual binary diff", () => {
    render(
      <MediaReviewPreview
        preview={{ kind: "media", media_type: "image/png", media_kind: "image" }}
        documentPath="assets/screenshot.png"
        status="modified"
        mode="source"
      />,
    );

    expect(screen.getByText("assets/screenshot.png")).toBeTruthy();
    expect(screen.getByText("image/png")).toBeTruthy();
    expect(screen.getByText(/reviewed as rendered media rather than a textual diff/)).toBeTruthy();
  });
});
