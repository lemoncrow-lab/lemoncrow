import { beforeEach, describe, expect, it, vi } from "vitest";

const hosted = vi.hoisted(() => ({ value: false }));
const request = vi.hoisted(() => vi.fn(async () => ({ recorded: true })));
vi.mock("./hostedOnly", () => ({ isHostedWorkspace: async () => hosted.value }));
vi.mock("./reviewApi", () => ({ requestBrowserJson: request }));

import { REVIEW_FIRST_JUDGMENT_KEY, ReviewFrictionRecorder } from "./reviewFriction";
import { REVIEW_FRICTION_PATH, countBlockedReason, countMapOpen, countSettingsChange, countWaiting } from "./reviewFrictionCounts";

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const sent = () => request.mock.calls.map((call) => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)));

describe("review friction counts", () => {
  beforeEach(() => {
    hosted.value = false;
    request.mockClear();
    window.localStorage.clear();
  });

  it("sends fixed metric names and only the open Review's id for a wait", async () => {
    countMapOpen(true);
    countMapOpen(false);
    countBlockedReason();
    countWaiting("rev_1");
    await flush();
    expect(request.mock.calls.every((call) => (call as unknown as [string])[0] === REVIEW_FRICTION_PATH)).toBe(true);
    expect(sent()).toEqual([
      { metric: "map_open_with_delta" },
      { metric: "map_open_without_delta" },
      { metric: "blocked_reason" },
      { metric: "waiting", review_id: "rev_1" },
    ]);
  });

  it("counts settings changes only until this browser's first judgment", async () => {
    countSettingsChange();
    await flush();
    expect(sent()).toEqual([{ metric: "settings_before_first_review" }]);
    request.mockClear();
    new ReviewFrictionRecorder("rev_1").judgment();
    expect(window.localStorage.getItem(REVIEW_FIRST_JUDGMENT_KEY)).toBe("1");
    countSettingsChange();
    await flush();
    expect(request).not.toHaveBeenCalled();
  });

  it("asks a hosted workspace nothing and survives a failed request", async () => {
    hosted.value = true;
    countBlockedReason();
    await flush();
    expect(request).not.toHaveBeenCalled();
    hosted.value = false;
    request.mockRejectedValueOnce(new Error("offline"));
    expect(() => countBlockedReason()).not.toThrow();
    await flush();
  });
});
