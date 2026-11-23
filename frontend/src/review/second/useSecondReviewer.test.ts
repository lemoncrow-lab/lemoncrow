import { describe, expect, it } from "vitest";

import { watchDelayMs, worstCaseReadsPerMinute } from "./useSecondReviewer";

describe("the polling budget while a signature is owed", () => {
  it("backs off from 5 s to a 30 s ceiling", () => {
    expect([0, 1, 2, 3, 4, 10].map(watchDelayMs)).toEqual([5_000, 10_000, 20_000, 30_000, 30_000, 30_000]);
  });
  it("one watcher never makes more than 5 reads in any minute, so a page's few watchers stay far below the shipped 120 a minute", () => {
    const one = worstCaseReadsPerMinute();
    expect(one).toBeLessThanOrEqual(5);
    // a screen holds at most the strip, Wrap up's own read and the record line's: three watchers, ~10 page-load reads of 100.
    expect(one * 3).toBeLessThan(120 - 100);
  });
});
