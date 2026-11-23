import { beforeEach, describe, expect, it, vi } from "vitest";

import { RETIRED_REVIEW_FLOW_KEY, forgetRetiredReviewFlow } from "./retiredFlowSetting";

describe("the retired review.flow setting", () => {
  beforeEach(() => window.localStorage.clear());

  it("drops a stored classic value", () => {
    window.localStorage.setItem(RETIRED_REVIEW_FLOW_KEY, "classic");
    forgetRetiredReviewFlow();
    expect(window.localStorage.getItem(RETIRED_REVIEW_FLOW_KEY)).toBeNull();
  });

  it("does not fail when storage is blocked", () => {
    const spy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => forgetRetiredReviewFlow()).not.toThrow();
    spy.mockRestore();
  });
});
