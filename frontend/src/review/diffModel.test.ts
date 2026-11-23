import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTO_SPLIT_MIN_WIDTH, getInitialDiffStylePreference, initialDiffOverflow, motionSafeScrollBehavior, nextDiffStylePreference, persistDiffStylePreference, resolveDiffStyle, WRAP_DEFAULT_MAX_WIDTH } from "./diffModel";

describe("review diff overflow", () => {
  it("soft-wraps long code lines on a phone unless the reviewer chose otherwise", () => {
    expect(initialDiffOverflow(null, 390)).toBe("wrap");
    expect(initialDiffOverflow(null, WRAP_DEFAULT_MAX_WIDTH - 1)).toBe("wrap");
    expect(initialDiffOverflow(null, WRAP_DEFAULT_MAX_WIDTH)).toBe("scroll");
    expect(initialDiffOverflow(null, 1440)).toBe("scroll");
    expect(initialDiffOverflow("scroll", 390)).toBe("scroll");
    expect(initialDiffOverflow("wrap", 1440)).toBe("wrap");
    // An unknown width (no layout yet) keeps one line per row.
    expect(initialDiffOverflow(null, 0)).toBe("scroll");
  });
});

const originalMatchMedia = window.matchMedia;

afterEach(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
});

describe("review diff motion", () => {
  it("keeps explicit instant scrolling instant", () => {
    expect(motionSafeScrollBehavior("instant")).toBe("instant");
  });

  it("turns smooth review navigation into instant movement when reduced motion is requested", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn(() => ({ matches: true })),
    });
    expect(motionSafeScrollBehavior("smooth")).toBe("instant");
  });

  it("keeps smooth navigation when reduced motion is not requested", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn(() => ({ matches: false })),
    });
    expect(motionSafeScrollBehavior("smooth")).toBe("smooth");
  });
});


describe("responsive diff preference", () => {
  it("defaults Auto to unified until the actual diff viewport is wide enough", () => {
    expect(resolveDiffStyle("auto", AUTO_SPLIT_MIN_WIDTH - 1)).toBe("unified");
    expect(resolveDiffStyle("auto", AUTO_SPLIT_MIN_WIDTH)).toBe("split");
  });

  it("always honors explicit Split or Unified regardless of width", () => {
    expect(resolveDiffStyle("split", 390)).toBe("split");
    expect(resolveDiffStyle("unified", 1800)).toBe("unified");
  });

  it("persists the user preference and cycles Auto → Split → Unified", () => {
    window.localStorage.clear();
    expect(getInitialDiffStylePreference()).toBe("auto");
    persistDiffStylePreference("unified");
    expect(getInitialDiffStylePreference()).toBe("unified");
    expect(nextDiffStylePreference("auto")).toBe("split");
    expect(nextDiffStylePreference("split")).toBe("unified");
    expect(nextDiffStylePreference("unified")).toBe("auto");
  });
});
