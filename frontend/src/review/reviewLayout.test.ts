import { afterEach, describe, expect, it, vi } from "vitest";

import { isCompactReviewViewport, isNarrowReviewViewport, REVIEW_COMPACT_QUERY, REVIEW_NARROW_QUERY } from "./reviewLayout";

function stubMedia(width: number) {
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    return { matches: width <= max, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() };
  });
}

describe("review viewport queries", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("treats everything below md as narrow while the phone composition stays below sm", () => {
    expect(REVIEW_NARROW_QUERY).toBe("(max-width: 767px)");
    expect(REVIEW_COMPACT_QUERY).toBe("(max-width: 639px)");
    for (const [width, narrow, compact] of [[390, true, true], [700, true, false], [768, false, false], [1440, false, false]] as const) {
      stubMedia(width);
      expect(isNarrowReviewViewport()).toBe(narrow);
      expect(isCompactReviewViewport()).toBe(compact);
    }
  });
});
