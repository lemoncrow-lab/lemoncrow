import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DESKTOP_OPT_IN_KEY, HIDDEN_POLL_MS } from "./desktopNotify";
import { fetchNotifications } from "./notificationsApi";
import { POLL_BASE_MS, POLL_MAX_MS, pollDelayMs, useNotificationFeed } from "./useNotificationFeed";

vi.mock("./notificationsApi", () => ({ fetchNotifications: vi.fn(), markNotificationsRead: vi.fn(), clearNotifications: vi.fn(), reportTimeZone: vi.fn().mockResolvedValue(undefined), fetchDelivery: vi.fn().mockResolvedValue(null), saveDelivery: vi.fn() }));
const read = vi.mocked(fetchNotifications);
const feed = { items: [], unread: 0, truncated: false, marker_ttl_days: 30 };

function hidden(value: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

beforeEach(() => { vi.useFakeTimers(); read.mockReset().mockResolvedValue(feed); });
afterEach(() => { cleanup(); hidden(false); vi.useRealTimers(); });

describe("the polling budget (the shipped hosted quota is 120 requests a minute per person)", () => {
  it("is one read a minute while it works and doubles to five minutes while it fails", () => {
    expect([0, 1, 2, 3, 9].map(pollDelayMs)).toEqual([POLL_BASE_MS, 120_000, 240_000, POLL_MAX_MS, POLL_MAX_MS]);
    // The slowest working pace is one read a minute per open tab: two orders of magnitude under the quota.
    expect(60_000 / POLL_BASE_MS).toBeLessThanOrEqual(1);
  });
});

describe("useNotificationFeed", () => {
  it("reads at mount, then once a minute", async () => {
    renderHook(() => useNotificationFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS); });
    expect(read).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS * 3); });
    expect(read).toHaveBeenCalledTimes(5);
  });

  it("a server with no feed (a local install) is asked once and never again", async () => {
    read.mockResolvedValue(null);
    const view = renderHook(() => useNotificationFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MAX_MS * 4); });
    expect(read).toHaveBeenCalledTimes(1);
    expect(view.result.current.absent).toBe(true);
    expect(view.result.current.failed).toBe(false);
  });

  it("backs off after a failure, says it failed, and recovers", async () => {
    read.mockRejectedValueOnce(new Error("down"));
    const view = renderHook(() => useNotificationFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(view.result.current.failed).toBe(true);
    expect(view.result.current.absent).toBe(false);
    // One failure doubles the wait: nothing a minute later, a read two minutes after the failure.
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS); });
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(view.result.current.failed).toBe(false);
  });

  it("a hidden tab asks nothing until it is shown, then reads once", async () => {
    renderHook(() => useNotificationFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    hidden(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS * 5); });
    expect(read).toHaveBeenCalledTimes(1);
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("a hidden tab with desktop notifications on keeps reading, slowly, so a new item can be announced", async () => {
    vi.stubGlobal("Notification", class { static permission = "granted"; });
    Object.defineProperty(window, "Notification", { value: (globalThis as { Notification: unknown }).Notification, configurable: true });
    window.localStorage.setItem(DESKTOP_OPT_IN_KEY, "1");
    try {
      renderHook(() => useNotificationFeed());
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      hidden(true);
      await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS * 2); });
      const afterFirstWait = read.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(HIDDEN_POLL_MS * 3); });
      // About one read every HIDDEN_POLL_MS (five minutes), never one a minute.
      expect(read.mock.calls.length - afterFirstWait).toBeGreaterThanOrEqual(2);
      expect(read.mock.calls.length - afterFirstWait).toBeLessThanOrEqual(4);
      // Shown again: read at once, not up to five minutes later.
      const before = read.mock.calls.length;
      hidden(false);
      await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
      expect(read.mock.calls.length).toBe(before + 1);
    } finally {
      window.localStorage.clear();
      vi.unstubAllGlobals();
      Object.defineProperty(window, "Notification", { value: undefined, configurable: true });
    }
  });

  it("leaves nothing behind when the bell goes while the tab is hidden", async () => {
    const view = renderHook(() => useNotificationFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    hidden(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_BASE_MS + 1); });
    view.unmount();
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(POLL_MAX_MS * 2); });
    expect(read).toHaveBeenCalledTimes(1);
  });
});
