import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestBrowserJson } from "../reviewApi";
import { fetchReviewEnvironment } from "../reviewEnvironment";
import { clearNotifications, fetchNotifications, markNotificationsRead, reportTimeZone, TIME_ZONE_KEY } from "./notificationsApi";

vi.mock("../reviewApi", () => ({ requestBrowserJson: vi.fn() }));
vi.mock("../reviewEnvironment", () => ({ fetchReviewEnvironment: vi.fn() }));

const ask = vi.mocked(requestBrowserJson);
const environment = vi.mocked(fetchReviewEnvironment);
const item = { id: "k1", repo_id: "repo", review_id: "rev", title: "t", body: "", action: "Open", href: "/r/rev", at: "", read: false, kind: "review_requested", class: "review_requested" };

beforeEach(() => { ask.mockReset(); environment.mockReset(); });

describe("the notification feed is hosted only (SPEC 7.1 P0-2)", () => {
  it("a local install is never asked, for the feed or for a write", async () => {
    environment.mockResolvedValue({ mode: "local", discovered: true } as never);
    expect(await fetchNotifications()).toBeNull();
    expect(await markNotificationsRead([item])).toBeNull();
    expect(await clearNotifications()).toBeNull();
    expect(ask).not.toHaveBeenCalled();
  });

  it("a hosted server is asked for the feed, and a mark names exactly the item's repository, review and key", async () => {
    environment.mockResolvedValue({ mode: "managed_shared", discovered: true } as never);
    ask.mockResolvedValue({ items: [], unread: 0 } as never);
    await fetchNotifications();
    await markNotificationsRead([item]);
    await clearNotifications();
    expect(ask.mock.calls[0]).toEqual(["/v1/reviews/notifications"]);
    expect(ask.mock.calls[1][0]).toBe("/v1/reviews/notifications/read");
    expect(JSON.parse(String((ask.mock.calls[1][1] as RequestInit).body))).toEqual({ items: [{ repo_id: "repo", review_id: "rev", key: "k1" }] });
    expect(ask.mock.calls[2][0]).toBe("/v1/reviews/notifications/clear");
  });

  it("a discovery that failed is a failure, not a local install", async () => {
    environment.mockResolvedValue({ mode: "local", discovered: false } as never);
    await expect(fetchNotifications()).rejects.toThrow("Couldn't reach the server");
    expect(ask).not.toHaveBeenCalled();
  });

  it("an older hosted server without the route has no feed; any other failure is a failure, not an empty feed", async () => {
    environment.mockResolvedValue({ mode: "managed_shared", discovered: true } as never);
    ask.mockRejectedValueOnce(Object.assign(new Error("Not Found"), { status: 404 }));
    expect(await fetchNotifications()).toBeNull();
    ask.mockRejectedValueOnce(Object.assign(new Error("Too many requests"), { status: 429 }));
    await expect(fetchNotifications()).rejects.toThrow("Too many requests");
  });
});

describe("the browser reports its time zone for the daily digest (P9-5)", () => {
  beforeEach(() => { window.localStorage.removeItem(TIME_ZONE_KEY); environment.mockResolvedValue({ mode: "hosted", discovered: true } as never); ask.mockResolvedValue({}); });

  it("sends the IANA zone once a day, and again when the zone changes", async () => {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    await reportTimeZone(new Date("2026-11-09T10:00:00Z"));
    expect(ask).toHaveBeenCalledWith("/v1/reviews/notifications/time-zone", { method: "PUT", body: JSON.stringify({ time_zone: zone }) });
    await reportTimeZone(new Date("2026-11-09T18:00:00Z"));
    expect(ask).toHaveBeenCalledTimes(1); // the same zone, the same day
    await reportTimeZone(new Date("2026-11-10T08:00:00Z"));
    expect(ask).toHaveBeenCalledTimes(2); // a new day: the server is told again
  });

  it("a local install sends nothing", async () => {
    environment.mockResolvedValue({ mode: "local", discovered: true } as never);
    await reportTimeZone();
    expect(ask).not.toHaveBeenCalled();
  });
});
