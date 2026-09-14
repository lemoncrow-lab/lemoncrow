import { afterEach, describe, expect, it, vi } from "vitest";

import { motionSafeScrollBehavior } from "./diffModel";

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
