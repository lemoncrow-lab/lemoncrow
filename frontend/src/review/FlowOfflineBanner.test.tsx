// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { FlowOfflineBanner, OFFLINE_BANNER_TEXT } from "./FlowOfflineBanner";

afterEach(() => { cleanup(); Object.defineProperty(window.navigator, "onLine", { value: true, configurable: true }); });

describe("offline banner (SPEC 4.20)", () => {
  it("claims only what is always true: marks and notes need the connection; no send promise", () => {
    expect(OFFLINE_BANNER_TEXT).toBe("Offline · marks and notes need the connection");
    expect(OFFLINE_BANNER_TEXT).not.toMatch(/saved|send/i);
  });

  it("follows the browser's real online flag", () => {
    Object.defineProperty(window.navigator, "onLine", { value: true, configurable: true });
    render(<FlowOfflineBanner />);
    expect(screen.queryByTestId("flow-offline-banner")).toBeNull();
    act(() => { Object.defineProperty(window.navigator, "onLine", { value: false, configurable: true }); window.dispatchEvent(new Event("offline")); });
    expect(screen.getByTestId("flow-offline-banner")).toHaveTextContent(OFFLINE_BANNER_TEXT);
    act(() => { Object.defineProperty(window.navigator, "onLine", { value: true, configurable: true }); window.dispatchEvent(new Event("online")); });
    expect(screen.queryByTestId("flow-offline-banner")).toBeNull();
  });
});
