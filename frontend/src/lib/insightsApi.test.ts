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
    vi.restoreAllMocks();
  });

  it("treats a browser acknowledgement as acknowledged when the server has reset", async () => {
    localStorage.setItem("atelier.telemetry.acknowledged", "1");
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
