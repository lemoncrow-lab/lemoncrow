import { createElement, type ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DiscoveredProductCapabilitiesProvider,
  LOCAL_DASHBOARD_PRODUCT_CAPABILITIES,
  LOCAL_PRODUCT_CAPABILITIES,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  applyProductEntitlements,
  parseProductCapabilities,
  useProductCapabilitiesResolution,
  useResolvedProductCapabilities,
} from "./RuntimeCapabilities";

describe("product capability discovery", () => {
  it("projects deployment product presence onto the frontend manifest", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "local" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          runtime: { installed: false, configured: false, available: false },
        },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.products.review.available).toBe(true);
    expect(capabilities.products.runtime).toMatchObject({
      installed: false,
      configured: false,
      available: false,
      mode: "none",
    });
    expect(capabilities.features["review.directory"]).toBe(true);
    expect(capabilities.features["review.ask"]).toBe(true);
    expect(capabilities.features["runtime.code"]).toBe(false);
    expect(capabilities.features["runtime.runs"]).toBe(false);
  });

  it("keeps the composition fallback for older servers without product discovery", () => {
    expect(parseProductCapabilities({}, REVIEW_ONLY_PRODUCT_CAPABILITIES)).toEqual(
      REVIEW_ONLY_PRODUCT_CAPABILITIES,
    );
  });

  it("keeps lightweight local Code actions when the server advertises them", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "local" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          dashboard: { installed: true, configured: true, available: true },
          runtime: { installed: true, configured: true, available: true },
        },
        product_capabilities: {
          dashboard: {
            code_map: true,
            code_ask: true,
            local_editor: true,
            review_capture: true,
            project_knowledge: true,
          },
        },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.features["runtime.project_knowledge"]).toBe(true);
    expect(capabilities.features["runtime.runs"]).toBe(true);
    expect(capabilities.features["core.usage"]).toBe(true);
    expect(capabilities.features["runtime.integrations"]).toBe(true);
    expect(capabilities.features["runtime.diagnostics"]).toBe(true);
    expect(capabilities.features["runtime.code"]).toBe(false);
    expect(capabilities.features["runtime.code_map"]).toBe(true);
    expect(capabilities.features["runtime.swarms"]).toBe(false);
    expect(capabilities.features["runtime.code_ask"]).toBe(true);
    expect(capabilities.features["runtime.local_editor"]).toBe(true);
    expect(capabilities).toEqual(LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);
  });

  it("does not invent Code capabilities for an older basic local Dashboard", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "local" },
        products: {
          core: { installed: true, configured: true, available: true },
          dashboard: { installed: true, configured: true, available: true },
          runtime: { installed: true, configured: true, available: true },
        },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.features["runtime.code_map"]).toBe(false);
    expect(capabilities.features["runtime.code"]).toBe(false);
    expect(capabilities.features["runtime.code_ask"]).toBe(false);
    expect(capabilities.features["runtime.local_editor"]).toBe(false);
    expect(capabilities.features["runtime.project_knowledge"]).toBe(false);
    expect(capabilities.runtime.localReviewCapture).toBe(false);
  });
  it("keeps both code features on a hosted deployment", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "hosted" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          runtime: { installed: true, configured: true, available: true },
        },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.features["runtime.code"]).toBe(true);
    expect(capabilities.features["runtime.code_map"]).toBe(true);
    // Hosted serves no Project Knowledge route yet.
    expect(capabilities.features["runtime.project_knowledge"]).toBe(false);
  });

  it("keeps the full local surface for servers that predate the basic Dashboard", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "local" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          runtime: { installed: true, configured: true, available: true },
        },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.features["runtime.code"]).toBe(true);
    expect(capabilities.features["runtime.code_map"]).toBe(true);
    expect(capabilities.features["runtime.swarms"]).toBe(true);
  });

  it("maps managed deployment names to the hosted frontend mode", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "managed_shared" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          runtime: { installed: true, configured: true, available: true },
        },
      },
      REVIEW_ONLY_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.products.core.mode).toBe("hosted");
    expect(capabilities.products.review.mode).toBe("hosted");
    expect(capabilities.products.runtime.mode).toBe("hosted");
  });
  it("narrows available products to the signed-in account grants", () => {
    const capabilities = applyProductEntitlements(LOCAL_PRODUCT_CAPABILITIES, { products: ["review"] });

    expect(capabilities.products.review).toMatchObject({ entitled: true, available: true });
    expect(capabilities.products.runtime).toMatchObject({ entitled: false, available: false, installed: true });
    expect(capabilities.features["review.directory"]).toBe(true);
    expect(capabilities.features["runtime.code"]).toBe(false);
  });

  it("keeps deployment capabilities when an older account response has no product grants", () => {
    expect(applyProductEntitlements(REVIEW_ONLY_PRODUCT_CAPABILITIES, {})).toEqual(
      REVIEW_ONLY_PRODUCT_CAPABILITIES,
    );
  });

  it("honors Review Ask capability independently of Review installation", () => {
    const capabilities = parseProductCapabilities(
      {
        deployment: { mode: "local" },
        products: {
          core: { installed: true, configured: true, available: true },
          review: { installed: true, configured: true, available: true },
          runtime: { installed: false, configured: false, available: false },
        },
        product_capabilities: { review: { ask: false } },
      },
      LOCAL_PRODUCT_CAPABILITIES,
    );

    expect(capabilities.products.review.available).toBe(true);
    expect(capabilities.features["review.directory"]).toBe(true);
    expect(capabilities.features["review.ask"]).toBe(false);
  });


  it("settles failed discovery to safe degraded defaults instead of leaving Review controls pending", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const wrapper = ({ children }: { children: ReactNode }) => createElement(
      DiscoveredProductCapabilitiesProvider,
      { fallback: REVIEW_ONLY_PRODUCT_CAPABILITIES, children },
    );
    const { result } = renderHook(() => ({
      resolution: useProductCapabilitiesResolution(),
      capabilities: useResolvedProductCapabilities(),
    }), { wrapper });

    expect(result.current.resolution).toBe("pending");
    expect(result.current.capabilities).toBeNull();
    await waitFor(() => expect(result.current.resolution).toBe("degraded"));
    expect(result.current.capabilities?.features["review.directory"]).toBe(true);
    expect(result.current.capabilities?.features["review.ask"]).toBe(true);
    vi.unstubAllGlobals();
  });

});
