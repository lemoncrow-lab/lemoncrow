import { act, cleanup, render, renderHook } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { currentFlowToast, dismissFlowToast } from "../flowToast";
import { sendPresence } from "./teamApi";
import {
  BEAT_CEILING_MS, BEAT_MS, beatDelayMs, FIRST_BEAT_MS, queueEntryToast, SEEN_MS, SETTLE_MS, takeEntryToast, TeamNoticeAnchor,
  useReviewPresence,
} from "./useReviewPresence";

vi.mock("./teamApi", () => ({ sendPresence: vi.fn() }));
const beat = vi.mocked(sendPresence);
const EMPTY = { reviewing: [], claim: null, handoff: null };

function hidden(value: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
/** The page has been on screen long enough for the first beat. */
const arrived = () => tick(FIRST_BEAT_MS);

beforeEach(() => {
  vi.useFakeTimers();
  beat.mockReset().mockResolvedValue(EMPTY);
  dismissFlowToast();
  render(<MemoryRouter><TeamNoticeAnchor /></MemoryRouter>); // the flow screen is up
});
afterEach(() => {
  cleanup();
  hidden(false);
  vi.useRealTimers();
});

describe("the beat's pace", () => {
  it("is a minute, doubles after a failure, and never passes five minutes", () => {
    expect([0, 1, 2, 3, 9].map(beatDelayMs)).toEqual([60_000, 120_000, 240_000, 300_000, 300_000]);
    expect(BEAT_CEILING_MS).toBe(300_000);
  });
  it("stays far under the 120 requests a minute the server allows", () => {
    expect(60_000 / BEAT_MS).toBeLessThanOrEqual(1);
  });
});

describe("useReviewPresence", () => {
  it("asks nothing for the first ten seconds, then beats once a minute", async () => {
    renderHook(() => useReviewPresence("r1", true));
    await tick(FIRST_BEAT_MS - 1);
    expect(beat).not.toHaveBeenCalled(); // a page only passed through asks nothing
    await tick(1);
    expect(beat).toHaveBeenCalledTimes(1);
    await tick(BEAT_MS * 3);
    expect(beat).toHaveBeenCalledTimes(4);
  });

  it("asks nothing when it is not enabled (a local page, or a screen that is not a review)", async () => {
    renderHook(() => useReviewPresence("r1", false));
    renderHook(() => useReviewPresence("", true));
    await tick(BEAT_MS * 5);
    expect(beat).not.toHaveBeenCalled();
  });

  it("a server with no team stops the beat for good", async () => {
    beat.mockResolvedValue(null);
    renderHook(() => useReviewPresence("r1", true));
    await arrived();
    await tick(BEAT_MS * 5);
    expect(beat).toHaveBeenCalledTimes(1);
  });

  it("a page opened in a hidden tab sends no first beat, and waits ten shown seconds after it is shown", async () => {
    hidden(true);
    renderHook(() => useReviewPresence("r1", true));
    await tick(BEAT_MS * 4);
    expect(beat).not.toHaveBeenCalled();
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(beat).not.toHaveBeenCalled();
    await tick(FIRST_BEAT_MS);
    expect(beat).toHaveBeenCalledTimes(1);
  });

  it("a tab hidden between beats sends nothing and beats once when it is shown again", async () => {
    renderHook(() => useReviewPresence("r1", true));
    await arrived();
    hidden(true);
    await tick(BEAT_MS * 4);
    const before = beat.mock.calls.length;
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(beat.mock.calls.length).toBe(before + 1);
  });

  it("leaves nothing behind when the screen goes while the tab is hidden", async () => {
    const view = renderHook(() => useReviewPresence("r1", true));
    await arrived();
    hidden(true);
    await tick(BEAT_MS + 10);
    const before = beat.mock.calls.length;
    view.unmount();
    hidden(false);
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(BEAT_MS * 5); });
    expect(beat.mock.calls.length).toBe(before);
  });

  it("slows after a failure instead of retrying faster", async () => {
    beat.mockRejectedValue(new Error("502"));
    renderHook(() => useReviewPresence("r1", true));
    await arrived();
    expect(beat).toHaveBeenCalledTimes(1);
    await tick(BEAT_MS + 10);
    expect(beat).toHaveBeenCalledTimes(1); // the wait after one failure is two minutes, not one
    await tick(BEAT_MS);
    expect(beat).toHaveBeenCalledTimes(2);
    await tick(BEAT_MS * 3);
    expect(beat).toHaveBeenCalledTimes(2);
    await tick(BEAT_MS + 10);
    expect(beat).toHaveBeenCalledTimes(3);
  });

  it("shows a held notice, and once however many beats carry it", async () => {
    beat.mockResolvedValue({ ...EMPTY, notices: [{ kind: "handed_off", by: { id: "usr_sam", name: "Sam Rivera" }, note: "Auth first.", at: "2026-11-09T09:00:00Z" }] });
    renderHook(() => useReviewPresence("r1", true));
    await arrived();
    expect(currentFlowToast()?.text).toBe("Sam Rivera handed this review to you: Auth first.");
    dismissFlowToast();
    await tick(BEAT_MS + 10);
    expect(currentFlowToast()).toBeNull();
  });

  it("shows the line queued for this review as soon as its screen has settled, without waiting for a beat", async () => {
    queueEntryToast("r1", "Reviewing together with Maya · notes stay private until sent");
    queueEntryToast("r9", "for another review");
    renderHook(() => useReviewPresence("r1", true));
    await tick(SETTLE_MS);
    expect(beat).not.toHaveBeenCalled();
    expect(currentFlowToast()?.text).toBe("Reviewing together with Maya · notes stay private until sent");
    expect(takeEntryToast("r1")).toBe("");
    expect(takeEntryToast("r9")).toBe("for another review");
  });

  it("a notice that arrives before the screen is up waits for it", async () => {
    cleanup(); // the anchor goes: the screen is not up
    beat.mockResolvedValue({ ...EMPTY, notices: [{ kind: "taken", by: { id: "usr_sam", name: "Sam Rivera" }, at: "2026-11-09T09:30:00Z" }] });
    renderHook(() => useReviewPresence("r2", true));
    await tick(FIRST_BEAT_MS + SETTLE_MS * 2);
    expect(currentFlowToast()).toBeNull();
    render(<MemoryRouter><TeamNoticeAnchor /></MemoryRouter>);
    await tick(SETTLE_MS);
    expect(currentFlowToast()?.text).toBe("Sam took this review");
  });

  it("a notice cleared by a late redirect shows again on the next settled path, and stops once it has been seen", async () => {
    beat.mockResolvedValue({ ...EMPTY, notices: [{ kind: "taken", by: { id: "usr_sam", name: "Sam Rivera" }, at: "2026-11-09T09:40:00Z" }] });
    cleanup();
    let go: (to: string) => void = () => undefined;
    function Nav() { const navigate = useNavigate(); go = navigate; return null; }
    render(<MemoryRouter initialEntries={["/r/x"]}><Nav /><TeamNoticeAnchor /></MemoryRouter>);
    renderHook(() => useReviewPresence("r3", true));
    await tick(FIRST_BEAT_MS + SETTLE_MS);
    expect(currentFlowToast()?.text).toBe("Sam took this review");
    dismissFlowToast(); // the host clears it at the redirect
    await act(async () => { go("/r/x/one"); });
    await tick(SETTLE_MS);
    expect(currentFlowToast()?.text).toBe("Sam took this review");
    dismissFlowToast();
    await tick(SEEN_MS);
    await act(async () => { go("/r/x/all"); });
    await tick(SETTLE_MS);
    expect(currentFlowToast()).toBeNull(); // it had been on screen long enough
  });
});
