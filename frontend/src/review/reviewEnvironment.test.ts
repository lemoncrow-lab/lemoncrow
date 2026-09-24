import { describe, expect, it } from "vitest";

import { fallbackReviewEnvironment, parseReviewEnvironment } from "./reviewEnvironment";

describe("Review environment discovery", () => {
  it("projects explicit local capabilities", () => {
    const environment = parseReviewEnvironment({
      deployment: { mode: "local" },
      product_capabilities: {
        review: {
          local_workspace: true,
          workspace_refresh: true,
          collaboration: false,
        },
      },
    }, "customer_hosted");

    expect(environment.mode).toBe("local");
    expect(environment.discovered).toBe(true);
    expect(environment.review.local_workspace).toBe(true);
    expect(environment.review.workspace_refresh).toBe(true);
    expect(environment.review.collaboration).toBe(false);
  });

  it("keeps hosted capabilities off when an older server does not advertise them", () => {
    const environment = parseReviewEnvironment({}, "customer_hosted");
    expect(environment.mode).toBe("customer_hosted");
    expect(environment.discovered).toBe(false);
    expect(environment.review.collaboration).toBe(false);
    expect(environment.review.participants).toBe(false);
    expect(environment.review.requests).toBe(false);
  });

  it("accepts hosted capabilities only when discovery advertises them", () => {
    const environment = parseReviewEnvironment({
      deployment: { mode: "managed_dedicated" },
      product_capabilities: {
        review: {
          cross_device_history: true,
          collaboration: true,
          participants: true,
          requests: true,
          organization_policy: true,
        },
      },
    }, "customer_hosted");

    expect(environment.mode).toBe("managed_dedicated");
    expect(environment.review.cross_device_history).toBe(true);
    expect(environment.review.collaboration).toBe(true);
    expect(environment.review.participants).toBe(true);
    expect(environment.review.requests).toBe(true);
    expect(environment.review.organization_policy).toBe(true);
  });

  it("uses local-only behavior in the local compatibility fallback", () => {
    const environment = fallbackReviewEnvironment("local");
    expect(environment.review.local_workspace).toBe(true);
    expect(environment.review.workspace_refresh).toBe(true);
    expect(environment.review.collaboration).toBe(false);
  });
});
