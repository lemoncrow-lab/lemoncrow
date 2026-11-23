import type { NotificationItem } from "./notificationsApi";

/**
 * Desktop notifications from the open tab (SPEC 4.13, NOTIF-2). The words are the bell item's own, so the bell, the
 * desktop notification and a phone's say the same thing (SPEC 7.1 P9-5). This is the browser's Notification API: it
 * speaks while the page is open and polling, and it needs the person's permission. Push to a closed browser or a
 * phone needs a push service and its credentials, which this server does not have (7.1 P9-5).
 */
/** `off`: not turned on here (the browser may or may not have been asked); `on`: turned on here and allowed. */
export type DesktopState = "unsupported" | "off" | "on" | "denied";

export const DESKTOP_OPT_IN_KEY = "lemoncrow.review.desktopNotifications";
/** While the tab is hidden and desktop notifications are on, the feed is read this often (otherwise not at all). */
export const HIDDEN_POLL_MS = 300_000;

export const DESKTOP_OFF_LABEL = "Turn off desktop notifications";
export const DESKTOP_ON_LABEL = "Turn on desktop notifications";
export const DESKTOP_ON_NOTE = "Desktop notifications are on.";
export const DESKTOP_BLOCKED_NOTE = "Desktop notifications are blocked in this browser.";
/** At most this many notifications for one read; the rest are in the bell. */
export const MAX_DESKTOP_AT_ONCE = 3;

/** The person's own choice on this browser. The browser's permission alone is not a choice: it may be left over. */
export function desktopOptedIn(): boolean {
  try { return window.localStorage.getItem(DESKTOP_OPT_IN_KEY) === "1"; } catch { return false; }
}
function rememberOptIn(on: boolean): void {
  try { if (on) window.localStorage.setItem(DESKTOP_OPT_IN_KEY, "1"); else window.localStorage.removeItem(DESKTOP_OPT_IN_KEY); } catch { /* storage can be unavailable: the choice then lasts only this page */ }
}

export function desktopState(): DesktopState {
  if (typeof window === "undefined" || typeof window.Notification === "undefined") return "unsupported";
  const permission = window.Notification.permission;
  if (permission === "denied") return "denied";
  return permission === "granted" && desktopOptedIn() ? "on" : "off";
}

/** "Turn on": ask the browser if it has not been asked, and remember the choice only when it is allowed. */
export async function turnOnDesktop(): Promise<DesktopState> {
  if (desktopState() === "unsupported") return "unsupported";
  try { await window.Notification.requestPermission(); } catch { /* an old browser that only takes a callback: read the answer below */ }
  if (window.Notification.permission === "granted") rememberOptIn(true);
  return desktopState();
}

export function turnOffDesktop(): DesktopState {
  rememberOptIn(false);
  return desktopState();
}

/** Items the person has not been told about: unread and not seen by an earlier read. */
export function newlyUnread(items: readonly NotificationItem[], seen: ReadonlySet<string>): NotificationItem[] {
  return items.filter((item) => !item.read && !seen.has(itemId(item)));
}

export const itemId = (item: NotificationItem): string => `${item.review_id}:${item.id}`;

/** Show one desktop notification per new item (the bell item's title and body), opening its place when pressed. */
export function showDesktop(items: readonly NotificationItem[], open: (href: string) => void): number {
  if (desktopState() !== "on") return 0;
  let shown = 0;
  for (const item of items.slice(0, MAX_DESKTOP_AT_ONCE)) {
    try {
      const note = new window.Notification(item.title, { body: item.body || undefined, tag: itemId(item) });
      note.onclick = () => { window.focus(); note.close(); open(item.href); };
      shown += 1;
    } catch {
      // Some phone browsers only show a notification from a service worker; the bell still has it.
      break;
    }
  }
  return shown;
}
