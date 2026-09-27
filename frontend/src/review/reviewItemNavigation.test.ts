import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  dedicatedReviewItemUrl,
  openDedicatedReviewItem,
  readDedicatedReviewItem,
} from "./reviewItemNavigation";

beforeEach(() => {
  window.history.replaceState(null, "", "/review#t=secret-token&r=review-1");
});

describe("dedicated review item navigation", () => {
  it("keeps workspace auth in the fragment while scoping the Reader URL to a file", () => {
    const href = dedicatedReviewItemUrl({ kind: "file", path: "src/components/Hero.tsx" });
    const url = new URL(href);

    expect(url.pathname).toBe("/review");
    expect(url.searchParams.get("review-item")).toBe("file");
    expect(url.searchParams.get("review-path")).toBe("src/components/Hero.tsx");
    expect(url.hash).toBe("#t=secret-token&r=review-1");
    expect(url.searchParams.has("t")).toBe(false);

    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    expect(readDedicatedReviewItem()).toEqual({ kind: "file", path: "src/components/Hero.tsx" });
  });

  it("opens a Reader-owned surface deep link instead of a raw preview URL", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    openDedicatedReviewItem({ kind: "surface", provider: "web", id: "/scan" });

    expect(open).toHaveBeenCalledTimes(1);
    const href = String(open.mock.calls[0]?.[0]);
    const url = new URL(href);
    expect(url.pathname).toBe("/review");
    expect(url.searchParams.get("review-item")).toBe("surface");
    expect(url.searchParams.get("review-provider")).toBe("web");
    expect(url.searchParams.get("review-surface")).toBe("/scan");
    expect(url.hash).toBe("#t=secret-token&r=review-1");
    expect(open).toHaveBeenCalledWith(href, "_blank", "noopener,noreferrer");

    open.mockRestore();
  });
});
