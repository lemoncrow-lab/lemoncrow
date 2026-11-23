import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  dedicatedReviewItemUrl,
  fullReviewUrlFromDedicatedItem,
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
    expect(url.searchParams.get("review-return")).toBe("/review?review-return-path=src%2Fcomponents%2FHero.tsx");
    expect(url.search).not.toContain("secret-token");

    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    expect(readDedicatedReviewItem()).toEqual({ kind: "file", path: "src/components/Hero.tsx" });
    expect(fullReviewUrlFromDedicatedItem()).toBe("/review?review-return-path=src%2Fcomponents%2FHero.tsx#t=secret-token&r=review-1");
  });

  it("lands a file tab on a part and leaves this tab's Review Map behind", () => {
    window.history.replaceState(null, "", "/review?base=1&target=2&map=file&map-focus=file%3Aa&map-node=file%3Aa#r=review-1");
    const url = new URL(dedicatedReviewItemUrl({ kind: "file", path: "src/a.py", targetId: "target-a" }));

    expect(Object.fromEntries(url.searchParams)).toEqual({
      base: "1",
      target: "2",
      "review-item": "file",
      "review-path": "src/a.py",
      "review-target": "target-a",
      "review-return": "/review?base=1&target=2&map=file&map-focus=file%3Aa&map-node=file%3Aa",
    });
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    expect(readDedicatedReviewItem()).toEqual({ kind: "file", path: "src/a.py", targetId: "target-a" });
    expect(fullReviewUrlFromDedicatedItem()).toBe("/review?base=1&target=2&map=file&map-focus=file%3Aa&map-node=file%3Aa#r=review-1");
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
    expect(url.searchParams.get("review-return")).toBe("/review?review-return-surface=web%3A%2Fscan");
    expect(url.hash).toBe("#t=secret-token&r=review-1");
    expect(open).toHaveBeenCalledWith(href, "_blank", "noopener,noreferrer");

    open.mockRestore();
  });
});
