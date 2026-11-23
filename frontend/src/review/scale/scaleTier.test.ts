import { describe, expect, it } from "vitest";

import { SCALE_THRESHOLDS, mapByModule, scaleTier } from "./scaleTier";

describe("scaleTier boundaries (each threshold starts its tier)", () => {
  it.each([
    [0, "small"], [1, "small"], [29, "small"],
    [30, "medium"], [31, "medium"], [229, "medium"],
    [230, "large"], [231, "large"], [999, "large"],
    [1000, "huge"], [1001, "huge"], [50_000, "huge"],
  ] as const)("%i parts is %s", (parts, tier) => {
    expect(scaleTier(parts)).toBe(tier);
  });

  it("uses the SPEC thresholds", () => {
    expect(SCALE_THRESHOLDS).toEqual({ medium: 30, large: 230, huge: 1000 });
  });

  it("lists the Map by module from 30 parts", () => {
    expect([mapByModule(29), mapByModule(30), mapByModule(31)]).toEqual([false, true, true]);
  });
});
