import { describe, expect, it } from "vitest";

import { rangeText, sumRanges, timeRange } from "./timeEstimate";

describe("time range from real part counts", () => {
  it("is always a range, even for one part", () => {
    for (const counts of [{ decisions: 0, quickChecks: 0 }, { decisions: 0, quickChecks: 1 }, { decisions: 1, quickChecks: 0 }]) {
      const range = timeRange(counts);
      expect(range.high).toBeGreaterThan(range.low);
    }
  });

  it("reads one decision as 4–7 min and two decisions with a quick check as 8–14 min", () => {
    expect(rangeText(timeRange({ decisions: 1, quickChecks: 0 }))).toBe("4–7 min");
    expect(rangeText(timeRange({ decisions: 2, quickChecks: 1 }))).toBe("8–14 min");
  });

  it("grows with every decision and every quick check", () => {
    const base = timeRange({ decisions: 2, quickChecks: 2 });
    expect(timeRange({ decisions: 3, quickChecks: 2 }).low).toBeGreaterThan(base.low);
    expect(timeRange({ decisions: 2, quickChecks: 4 }).high).toBeGreaterThan(base.high);
  });

  it("adds the ranges of a queue and writes hours once it passes one", () => {
    const budget = sumRanges([timeRange({ decisions: 1, quickChecks: 0 }), timeRange({ decisions: 1, quickChecks: 0 })]);
    expect(rangeText(budget)).toBe("8–14 min");
    expect(rangeText({ low: 70, high: 125 })).toBe("1 h 10 min–2 h 5 min");
    expect(rangeText({ low: 55, high: 120 })).toBe("55 min–2 h");
  });
});
