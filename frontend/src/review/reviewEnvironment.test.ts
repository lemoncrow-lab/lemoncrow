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

  it("shows Ask by compatibility default but honors an explicit disabled capability", () => {
    expect(fallbackReviewEnvironment("local").review.ask).toBe(true);
    expect(parseReviewEnvironment({}, "customer_hosted").review.ask).toBe(true);
    const off = parseReviewEnvironment({
      deployment: { mode: "local" },
      product_capabilities: { review: { ask: false } },
    }, "local");
    expect(off.review.ask).toBe(false);
  });

  it("names a Reader feature the server's manifest declares off", () => {
    const environment = parseReviewEnvironment({
      deployment: { mode: "local" },
      product_capabilities: {
        review: { manifest: 1, ask: false, capture: true, evidence_read: true, evidence_upload: false },
      },
    }, "local");
    expect(environment.review.ask).toBe(false);
    expect(environment.review.capture).toBe(true);
    expect(environment.review.evidence_upload).toBe(false);
    expect(environment.unsupported).toEqual(["Ask"]);
  });

  it("names nothing when the server does not say a feature is off", () => {
    expect(fallbackReviewEnvironment("local").unsupported).toEqual([]);
    expect(parseReviewEnvironment({}, "customer_hosted").unsupported).toEqual([]);
    const on = parseReviewEnvironment({
      deployment: { mode: "managed_shared" },
      product_capabilities: { review: { manifest: 1, ask: true } },
    }, "local");
    expect(on.unsupported).toEqual([]);
    expect(on.review.capture).toBe(true);
  });

  it("shows the Brief unless discovery turns it off", () => {
    expect(fallbackReviewEnvironment("local").review.brief).toBe(true);
    expect(parseReviewEnvironment({}, "customer_hosted").review.brief).toBe(true);
    const off = parseReviewEnvironment({
      deployment: { mode: "local" },
      product_capabilities: { review: { brief: false } },
    }, "local");
    expect(off.review.brief).toBe(false);
  });

  it("keeps hosted capabilities off when an older server does not advertise them", () => {
    const environment = parseReviewEnvironment({}, "customer_hosted");
    expect(environment.mode).toBe("customer_hosted");
    expect(environment.discovered).toBe(false);
    expect(environment.review.collaboration).toBe(false);
    expect(environment.review.participants).toBe(false);
    expect(environment.review.requests).toBe(false);
    expect(environment.review.audit_history).toBe(false);
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
          audit_history: true,
        },
      },
    }, "customer_hosted");

    expect(environment.mode).toBe("managed_dedicated");
    expect(environment.review.cross_device_history).toBe(true);
    expect(environment.review.collaboration).toBe(true);
    expect(environment.review.participants).toBe(true);
    expect(environment.review.requests).toBe(true);
    expect(environment.review.organization_policy).toBe(true);
    expect(environment.review.audit_history).toBe(true);
  });

  it("uses local-only behavior in the local compatibility fallback", () => {
    const environment = fallbackReviewEnvironment("local");
    expect(environment.review.local_workspace).toBe(true);
    expect(environment.review.workspace_refresh).toBe(true);
    expect(environment.review.collaboration).toBe(false);
  });
});

describe("Review environment before discovery answers", () => {
  it("keeps the Brief off until the server says whether it is on", async () => {
    const { renderHook, waitFor } = await import("@testing-library/react");
    const { vi } = await import("vitest");
    vi.resetModules();
    let answer: (value: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { answer = resolve; })));
    try {
      const module = await import("./reviewEnvironment");
      const { result } = renderHook(() => module.useReviewEnvironment("local"));
      expect(result.current.review.brief).toBe(false);
      expect(result.current.review.ask).toBe(false);
      answer(new Response(JSON.stringify({ deployment: { mode: "local" }, product_capabilities: { review: { brief: false, ask: false } } })));
      await waitFor(() => expect(result.current.discovered).toBe(true));
      expect(result.current.review.brief).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows the Brief once discovery answers, or when it cannot be reached", async () => {
    const { renderHook, waitFor } = await import("@testing-library/react");
    const { vi } = await import("vitest");
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    try {
      const module = await import("./reviewEnvironment");
      const { result } = renderHook(() => module.useReviewEnvironment("local"));
      await waitFor(() => expect(result.current.review.brief).toBe(true));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
