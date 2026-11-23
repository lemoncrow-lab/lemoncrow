import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell } from "./NotificationBell";
import { clearNotifications, fetchNotifications, markNotificationsRead, type NotificationFeed, type NotificationItem } from "./notificationsApi";

vi.mock("./notificationsApi", () => ({ fetchNotifications: vi.fn(), markNotificationsRead: vi.fn(), clearNotifications: vi.fn(), reportTimeZone: vi.fn().mockResolvedValue(undefined), fetchDelivery: vi.fn().mockResolvedValue(null), saveDelivery: vi.fn() }));
const read = vi.mocked(fetchNotifications);
const mark = vi.mocked(markNotificationsRead);
const clear = vi.mocked(clearNotifications);

function item(over: Partial<NotificationItem>): NotificationItem {
  return { id: "k", repo_id: "repo", review_id: "rev", kind: "review_requested", class: "review_requested", title: "", body: "", action: "Open", href: "/r/rev", at: "2026-11-08T10:00:00Z", read: false, ...over };
}
const items = [
  item({ id: "a", title: "Revision 2 is back · shop", body: "Revision 2 changed assigned work", action: "Review now", href: "/r/rev1/rev/2" }),
  item({ id: "b", title: "Dana Kim asked you to review payments", review_id: "rev2", href: "/r/rev2" }),
  item({ id: "c", title: "Required check failed: tests", review_id: "rev3", href: "/r/rev3", kind: "check_failed" }),
  item({ id: "d", title: "Landing blocked", review_id: "rev4", href: "/r/rev4", kind: "landing_attention" }),
];
const feedOf = (rows: NotificationItem[]): NotificationFeed => ({ items: rows, unread: rows.filter((row) => !row.read).length, truncated: false, marker_ttl_days: 30 });

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}
function page() {
  return render(
    <MemoryRouter initialEntries={["/reviews"]}>
      <NotificationBell />
      <Routes><Route path="*" element={<Where />} /></Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => { read.mockReset().mockResolvedValue(feedOf(items)); mark.mockReset(); clear.mockReset(); });
afterEach(cleanup);

describe("the bell (SPEC 4.13, NOTIF-1)", () => {
  it("names the unread count and lists every notification with Mark read, the place it opens and a deep link", async () => {
    page();
    const bell = await screen.findByRole("button", { name: "Notifications, 4 unread" });
    await userEvent.click(bell);
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const rows = within(dialog).getAllByTestId("notification-item");
    expect(rows).toHaveLength(4);
    expect(within(rows[0]).getByRole("button", { name: "Review now" })).toBeTruthy();
    expect(within(rows[1]).getByRole("button", { name: "Open" })).toBeTruthy();
    expect(within(dialog).getAllByRole("button", { name: /^Mark read/ })).toHaveLength(4);
    expect(within(dialog).getByRole("button", { name: "Clear all" })).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Close" })).toBeTruthy();
  });

  it("Mark read marks one item and lowers the count", async () => {
    mark.mockResolvedValue(feedOf(items.map((row) => (row.id === "b" ? { ...row, read: true } : row))));
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 4 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: "Mark read, Dana Kim asked you to review payments" }));
    expect(mark).toHaveBeenCalledWith([expect.objectContaining({ id: "b", review_id: "rev2" })]);
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Mark read/ })).toHaveLength(3));
    expect(screen.getByTestId("notification-bell").getAttribute("aria-label")).toBe("Notifications, 3 unread");
  });

  it("an item opens its exact place and is marked read", async () => {
    mark.mockResolvedValue(feedOf(items));
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 4 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: "Review now" }));
    expect(screen.getByTestId("where").textContent).toBe("/r/rev1/rev/2");
    expect(mark).toHaveBeenCalledWith([expect.objectContaining({ id: "a" })]);
  });

  it("Clear all empties the list with SPEC's empty line, and focus returns to the bell on close", async () => {
    clear.mockResolvedValue(feedOf([]));
    page();
    const bell = await screen.findByRole("button", { name: "Notifications, 4 unread" });
    await userEvent.click(bell);
    await userEvent.click(await screen.findByRole("button", { name: "Clear all" }));
    expect(await screen.findByText("You're all caught up.")).toBeTruthy();
    expect(screen.queryAllByTestId("notification-item")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("notification-bell"));
  });

  it("Esc closes the dialog and focus returns to the bell", async () => {
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 4 unread" }));
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("notification-bell"));
  });

  it("a read that failed says so; it does not read as an empty feed", async () => {
    read.mockRejectedValue(new Error("down"));
    page();
    await userEvent.click(await screen.findByTestId("notification-bell"));
    expect(await screen.findByText("Couldn't load notifications.")).toBeTruthy();
    expect(screen.queryByText("You're all caught up.")).toBeNull();
  });
});

describe("a local install (SPEC 7.1 P0-2: absent, not disabled)", () => {
  it("draws no bell at all", async () => {
    read.mockResolvedValue(null);
    page();
    await waitFor(() => expect(read).toHaveBeenCalled());
    expect(screen.queryByTestId("notification-bell")).toBeNull();
    expect(screen.queryByRole("button", { name: /Notifications/ })).toBeNull();
  });
});
