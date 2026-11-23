import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DESKTOP_OPT_IN_KEY, desktopState, MAX_DESKTOP_AT_ONCE, newlyUnread, showDesktop } from "./desktopNotify";
import { NotificationBell } from "./NotificationBell";
import { fetchNotifications, type NotificationFeed, type NotificationItem } from "./notificationsApi";

vi.mock("./notificationsApi", () => ({ fetchNotifications: vi.fn(), markNotificationsRead: vi.fn(), clearNotifications: vi.fn(), reportTimeZone: vi.fn().mockResolvedValue(undefined), fetchDelivery: vi.fn().mockResolvedValue(null), saveDelivery: vi.fn() }));
const read = vi.mocked(fetchNotifications);

function item(over: Partial<NotificationItem>): NotificationItem {
  return { id: "k", repo_id: "repo", review_id: "rev", kind: "work_returned", class: "work_returned", title: "", body: "", action: "Open", href: "/r/rev", at: "2026-11-08T10:00:00Z", read: false, ...over };
}
const back = item({ id: "a", title: "Revision 2 is back · shop", body: "2 parts came back · your notes were answered", action: "Review now", href: "/r/rev1/rev/2" });
const asked = item({ id: "b", review_id: "rev2", title: "Dana Kim asked you to review payments", href: "/r/rev2" });
const feedOf = (rows: NotificationItem[]): NotificationFeed => ({ items: rows, unread: rows.filter((r) => !r.read).length, truncated: false, marker_ttl_days: 30 });

class FakeNotification {
  static permission: NotificationPermission = "granted";
  static made: FakeNotification[] = [];
  static requestPermission = vi.fn(async () => { FakeNotification.permission = "granted"; return "granted" as NotificationPermission; });
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(public title: string, public options?: NotificationOptions) { FakeNotification.made.push(this); }
}

beforeEach(() => {
  FakeNotification.permission = "granted";
  window.localStorage.setItem(DESKTOP_OPT_IN_KEY, "1"); // the person turned them on here
  FakeNotification.made = [];
  FakeNotification.requestPermission.mockClear();
  vi.stubGlobal("Notification", FakeNotification);
  Object.defineProperty(window, "Notification", { value: FakeNotification, configurable: true });
  read.mockReset();
});
afterEach(() => { window.localStorage.clear(); cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("desktop notifications say what the bell says (NOTIF-2)", () => {
  it("one notification per new item, with the bell item's own title and body, opening its place when pressed", () => {
    const open = vi.fn();
    vi.spyOn(window, "focus").mockImplementation(() => undefined);
    expect(showDesktop([back], open)).toBe(1);
    const [made] = FakeNotification.made;
    expect(made.title).toBe("Revision 2 is back · shop");
    expect(made.options?.body).toBe("2 parts came back · your notes were answered");
    made.onclick?.();
    expect(open).toHaveBeenCalledWith("/r/rev1/rev/2");
  });

  it("speaks only with permission, at most three at a time, and only for what is new and unread", () => {
    FakeNotification.permission = "default";
    expect(showDesktop([back], vi.fn())).toBe(0);
    // A browser permission left over from another time is not the person's choice here.
    FakeNotification.permission = "granted";
    window.localStorage.removeItem(DESKTOP_OPT_IN_KEY);
    expect(desktopState()).toBe("off");
    expect(showDesktop([back], vi.fn())).toBe(0);
    window.localStorage.setItem(DESKTOP_OPT_IN_KEY, "1");
    FakeNotification.permission = "denied";
    expect(desktopState()).toBe("denied");
    expect(showDesktop([back], vi.fn())).toBe(0);
    FakeNotification.permission = "granted";
    const many = Array.from({ length: 5 }, (_, n) => item({ id: `n${n}`, title: `t${n}` }));
    expect(showDesktop(many, vi.fn())).toBe(MAX_DESKTOP_AT_ONCE);
    expect(newlyUnread([back, asked, { ...asked, id: "c", read: true }], new Set(["rev:a"])).map((row) => row.id)).toEqual(["b"]);
  });

  it("a browser that refuses to make one (a phone without a service worker) is not an error", () => {
    class Refusing { static permission = "granted"; constructor() { throw new TypeError("Illegal constructor"); } }
    Object.defineProperty(window, "Notification", { value: Refusing, configurable: true });
    expect(showDesktop([back], vi.fn())).toBe(0);
  });
});

describe("the bell announces what arrives while the tab is not in front", () => {
  function page() {
    return render(<MemoryRouter><NotificationBell /></MemoryRouter>);
  }

  it("does not announce what was waiting when the page opened, does announce a new item, and says it once", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    read.mockResolvedValueOnce(feedOf([back]));
    page();
    const bell = await screen.findByRole("button", { name: "Notifications, 1 unread" });
    expect(FakeNotification.made).toHaveLength(0);
    read.mockResolvedValue(feedOf([back, asked]));
    await userEvent.click(bell);
    expect(await screen.findAllByTestId("notification-item")).toHaveLength(2);
    expect(FakeNotification.made.map((made) => made.title)).toEqual(["Dana Kim asked you to review payments"]);
    // A later read of the same feed does not say it again.
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 2 unread" }));
    expect(await screen.findAllByTestId("notification-item")).toHaveLength(2);
    expect(FakeNotification.made).toHaveLength(1);
  });

  it("says nothing while the person is looking at the tab", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    read.mockResolvedValueOnce(feedOf([back]));
    page();
    const bell = await screen.findByRole("button", { name: "Notifications, 1 unread" });
    read.mockResolvedValue(feedOf([back, asked]));
    await userEvent.click(bell);
    expect(await screen.findAllByTestId("notification-item")).toHaveLength(2);
    expect(FakeNotification.made).toHaveLength(0);
  });

  it("offers to turn them on, asks the browser, remembers the choice, and can turn them off again", async () => {
    FakeNotification.permission = "default";
    window.localStorage.removeItem(DESKTOP_OPT_IN_KEY);
    read.mockResolvedValue(feedOf([back]));
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: "Turn on desktop notifications" }));
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    expect((await screen.findByTestId("desktop-state")).textContent).toBe("Desktop notifications are on.");
    expect(screen.queryByTestId("desktop-on")).toBeNull();
    expect(window.localStorage.getItem(DESKTOP_OPT_IN_KEY)).toBe("1");
    await userEvent.click(screen.getByRole("button", { name: "Turn off desktop notifications" }));
    expect(window.localStorage.getItem(DESKTOP_OPT_IN_KEY)).toBeNull();
    expect(screen.queryByTestId("desktop-state")).toBeNull();
    expect(screen.getByRole("button", { name: "Turn on desktop notifications" })).toBeTruthy();
  });

  it("says so when the browser blocks them, and offers nothing where there is no such API", async () => {
    FakeNotification.permission = "denied";
    read.mockResolvedValue(feedOf([back]));
    const first = page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    expect((await screen.findByTestId("desktop-state")).textContent).toBe("Desktop notifications are blocked in this browser.");
    first.unmount();
    Object.defineProperty(window, "Notification", { value: undefined, configurable: true });
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    expect(screen.queryByTestId("desktop-on")).toBeNull();
    expect(screen.queryByTestId("desktop-state")).toBeNull();
  });
});
