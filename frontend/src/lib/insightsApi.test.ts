import { __setBootstrapForTest } from "../review/reviewApi";
import {
  acknowledgeTelemetry,
  getTelemetryConfig,
  hasLocalTelemetryAcknowledgement,
} from "./insightsApi";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function telemetryConfig(overrides: Record<string, unknown> = {}) {
  return {
    remote_enabled: true,
    lexical_frustration_enabled: true,
    posthog_key: "phc_test",
    posthog_host: "https://example.test",
    anon_id: "anon",
    acknowledged: false,
    service_version: "0.1.0",
    dev_mode: false,
    ...overrides,
  };
}

describe("telemetry API", () => {
  afterEach(() => {
    localStorage.clear();
    __setBootstrapForTest({ token: "", reviewId: "" });
    vi.restoreAllMocks();
  });

  it("uses the local browser capability for protected telemetry endpoints", async () => {
    __setBootstrapForTest({ token: "", reviewId: "" });
    localStorage.setItem("lemoncrow.review.browserSession", "browser-capability");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      expect(String(input)).toBe("/api/telemetry/config");
      expect(new Headers(init?.headers).get("X-LemonCrow-Browser-Session")).toBe(
        "browser-capability"
      );
      expect(init?.credentials).toBe("same-origin");
      return Promise.resolve(jsonResponse(telemetryConfig()));
    });

    await expect(getTelemetryConfig()).resolves.toMatchObject({ remote_enabled: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("treats a browser acknowledgement as acknowledged when the server has reset", async () => {
    localStorage.setItem("lemoncrow.telemetry.acknowledged", "1");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(telemetryConfig({ acknowledged: false }))
    );

    await expect(getTelemetryConfig()).resolves.toMatchObject({
      acknowledged: true,
    });
  });

  it("stores the acknowledgement locally before posting it to the server", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(telemetryConfig({ acknowledged: false }))
    );

    await expect(acknowledgeTelemetry()).resolves.toMatchObject({
      acknowledged: true,
    });
    expect(hasLocalTelemetryAcknowledgement()).toBe(true);
  });
});
