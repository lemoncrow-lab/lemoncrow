import { describe, expect, it } from "vitest";
import { eventLabel, shortSessionId } from "./telemetryLabels";

describe("telemetry labels", () => {
  it("names known event types in plain words", () => {
    expect(eventLabel("frustration_signal_behavioral")).toBe("Retry or undo pattern");
    expect(eventLabel("cli_command_invoked")).toBe("CLI command started");
  });

  it("reads an unknown event type as words, keeping acronyms", () => {
    expect(eventLabel("mcp_cache_warmed")).toBe("MCP cache warmed");
    expect(eventLabel("review_opened")).toBe("Review opened");
    expect(eventLabel("")).toBe("");
  });

  it("shortens a session id to its first eight characters", () => {
    expect(shortSessionId("0f9c2b1e-5d4a-4b8e-9a77-3c2d1e0f9a8b")).toBe("0f9c2b1e");
    expect(shortSessionId("abc")).toBe("abc");
  });
});
