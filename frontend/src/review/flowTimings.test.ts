import { describe, expect, it } from "vitest";

import {
  DEFAULT_FLOW_TIMINGS,
  FLOW_TIMING_GLOBALS,
  MAX_TIMING_MS,
  parseTimingOverride,
  readFlowTimings,
  seenThresholdMs,
  signingGapMs,
} from "./flowTimings";

describe("flow timing defaults", () => {
  it("match SPEC: 10 s undo, seen rule max(0.6 s, 40 ms x lines) capped at 3 s", () => {
    expect(DEFAULT_FLOW_TIMINGS).toEqual({ undoMs: 10_000, seenMinMs: 600, seenPerLineMs: 40, seenCapMs: 3_000, signingFirstGapMs: 500, signingMaxGapMs: 5_000, signingLooks: 15 });
  });

  it("names the prototype-compatible undo global", () => {
    expect(FLOW_TIMING_GLOBALS.undoMs).toBe("LC_FLOW_UNDO_MS");
  });
});

describe("seenThresholdMs (D7)", () => {
  it("floors at 0.6 s for zero and small blocks", () => {
    expect(seenThresholdMs(0)).toBe(600);
    expect(seenThresholdMs(1)).toBe(600);
    expect(seenThresholdMs(-4)).toBe(600);
    expect(seenThresholdMs(Number.NaN)).toBe(600);
  });

  it("is 40 ms a line at the floor boundary (15 lines = 0.6 s)", () => {
    expect(seenThresholdMs(14)).toBe(600);
    expect(seenThresholdMs(15)).toBe(600);
    expect(seenThresholdMs(16)).toBe(640);
  });

  it("reaches the 3 s cap at 75 lines and stays there past it", () => {
    expect(seenThresholdMs(74)).toBe(2_960);
    expect(seenThresholdMs(75)).toBe(3_000);
    expect(seenThresholdMs(76)).toBe(3_000);
    expect(seenThresholdMs(1_000_000)).toBe(3_000);
    expect(seenThresholdMs(Number.POSITIVE_INFINITY)).toBe(3_000);
  });

  it("uses overridden seen timings when given", () => {
    const timings = { ...DEFAULT_FLOW_TIMINGS, seenMinMs: 50, seenPerLineMs: 10, seenCapMs: 200 };
    expect(seenThresholdMs(0, timings)).toBe(50);
    expect(seenThresholdMs(10, timings)).toBe(100);
    expect(seenThresholdMs(100, timings)).toBe(200);
  });
});

describe("parseTimingOverride", () => {
  it("accepts positive finite numbers and numeric strings", () => {
    expect(parseTimingOverride(400, 10_000)).toBe(400);
    expect(parseTimingOverride(0.5, 10_000)).toBe(0.5);
    expect(parseTimingOverride("250", 10_000)).toBe(250);
  });

  it("accepts the largest timer delay and ignores anything above it", () => {
    expect(parseTimingOverride(MAX_TIMING_MS, 10_000)).toBe(MAX_TIMING_MS);
    expect(parseTimingOverride(MAX_TIMING_MS + 1, 10_000)).toBe(10_000);
    expect(parseTimingOverride("1e12", 10_000)).toBe(10_000);
  });

  it.each([
    ["zero", 0],
    ["negative", -5],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["-Infinity", Number.NEGATIVE_INFINITY],
    ["empty string", ""],
    ["blank string", "   "],
    ["non-numeric string", "soon"],
    ["boolean", true],
    ["null", null],
    ["undefined", undefined],
    ["object", {}],
  ])("ignores %s and keeps the fallback", (_label, value) => {
    expect(parseTimingOverride(value, 10_000)).toBe(10_000);
  });
});

describe("readFlowTimings", () => {
  it("returns the defaults when no global is set", () => {
    expect(readFlowTimings({})).toEqual(DEFAULT_FLOW_TIMINGS);
  });

  it("reads each override from its window global", () => {
    expect(
      readFlowTimings({
        LC_FLOW_UNDO_MS: 400,
        LC_FLOW_SEEN_MIN_MS: 100,
        LC_FLOW_SEEN_PER_LINE_MS: 5,
        LC_FLOW_SEEN_CAP_MS: 900,
      }),
    ).toEqual({ ...DEFAULT_FLOW_TIMINGS, undoMs: 400, seenMinMs: 100, seenPerLineMs: 5, seenCapMs: 900 });
  });

  it("accepts window itself without a cast", () => {
    Object.assign(window, { LC_FLOW_UNDO_MS: 400, LC_FLOW_SEEN_CAP_MS: Number.POSITIVE_INFINITY });
    try {
      expect(readFlowTimings(window)).toEqual({ ...DEFAULT_FLOW_TIMINGS, undoMs: 400 });
    } finally {
      Reflect.deleteProperty(window, "LC_FLOW_UNDO_MS");
      Reflect.deleteProperty(window, "LC_FLOW_SEEN_CAP_MS");
    }
  });

  it("ignores bad globals one by one and keeps good ones", () => {
    expect(readFlowTimings({ LC_FLOW_UNDO_MS: -1, LC_FLOW_SEEN_CAP_MS: "1500", LC_FLOW_SEEN_MIN_MS: 0 })).toEqual({
      ...DEFAULT_FLOW_TIMINGS,
      seenCapMs: 1_500,
    });
  });
});

describe("signingGapMs (P6-1 O)", () => {
  it("doubles from 500 ms and stops growing at 5 s", () => {
    expect([1, 2, 3, 4, 5, 6, 40].map((look) => signingGapMs(look))).toEqual([500, 1000, 2000, 4000, 5000, 5000, 5000]);
  });
});
