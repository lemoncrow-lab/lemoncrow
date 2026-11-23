/** The bell's words (SPEC 4.13). Strings with no SPEC text are P7-3 rows in SPEC 7.1 ("agent default, owner may revise"). */

export const NOTIFICATIONS_TITLE = "Notifications";
export const CLEAR_ALL = "Clear all";
export const CLOSE = "Close";
export const MARK_READ = "Mark read";
export const ALL_CAUGHT_UP = "You're all caught up.";
/** P7-3 B: the feed could not be read (never shown as an empty feed). */
export const FEED_FAILED = "Couldn't load notifications.";

/** "Notifications, 4 unread"; with none unread, the plain name. */
export function bellLabel(unread: number): string {
  return unread > 0 ? `${NOTIFICATIONS_TITLE}, ${unread} unread` : NOTIFICATIONS_TITLE;
}
