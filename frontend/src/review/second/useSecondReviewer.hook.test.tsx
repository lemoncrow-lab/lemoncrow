import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchSecondReviewer } from "./secondApi";
import { useSecondReviewer } from "./useSecondReviewer";

vi.mock("./secondApi", () => ({ fetchSecondReviewer: vi.fn() }));
const read = vi.mocked(fetchSecondReviewer);

function hidden(value: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

beforeEach(() => {
  vi.useFakeTimers();
  read.mockReset().mockResolvedValue({ state: "waiting" } as never);
});
afterEach(() => {
  cleanup();
  hidden(false);
  vi.useRealTimers();
});

describe("useSecondReviewer's watch", () => {
  it("a hidden tab asks nothing until it is shown", async () => {
    const view = renderHook(() => useSecondReviewer("r1", { watchMs: 1 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const first = read.mock.calls.length;
    hidden(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000); });
    expect(read.mock.calls.length).toBe(first);
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(read.mock.calls.length).toBe(first + 1);
    view.unmount();
  });

  it("leaves nothing behind when the screen goes while the tab is hidden", async () => {
    const view = renderHook(() => useSecondReviewer("r1", { watchMs: 1 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    hidden(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); }); // the poller is now waiting for the tab
    const before = read.mock.calls.length;
    view.unmount();
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(120_000); });
    expect(read.mock.calls.length).toBe(before);
  });
});
