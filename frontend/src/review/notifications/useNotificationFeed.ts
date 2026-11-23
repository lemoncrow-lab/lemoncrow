import { useCallback, useEffect, useState } from "react";

import { desktopState, HIDDEN_POLL_MS } from "./desktopNotify";
import { clearNotifications, fetchNotifications, markNotificationsRead, type NotificationFeed, type NotificationItem } from "./notificationsApi";

/** Seconds between two reads while all is well; the hosted quota is 120 requests a minute per person (the shipped policy). */
export const POLL_BASE_MS = 60_000;
/** The longest wait after repeated failures. */
export const POLL_MAX_MS = 300_000;

/**
 * The wait before the next read: one a minute, doubling after each failure up to five minutes. That is at most one
 * request a minute per open tab while it works and fewer while it fails, and none while the tab is hidden.
 */
export function pollDelayMs(failures: number): number {
  return Math.min(POLL_BASE_MS * 2 ** Math.max(0, failures), POLL_MAX_MS);
}

export interface NotificationView {
  /** `null` until read, and for good where this server has no feed (a local install): nothing is drawn. */
  feed: NotificationFeed | null;
  /** The server has a feed and the last read failed: say so, never read it as empty. */
  failed: boolean;
  /** A local install, known after the first answer. */
  absent: boolean;
  refresh: () => Promise<void>;
  markRead: (items: readonly NotificationItem[]) => Promise<void>;
  clearAll: () => Promise<void>;
}

/** The caller's feed: one read at mount, then paced reads that pause while the tab is hidden. */
export function useNotificationFeed(): NotificationView {
  const [feed, setFeed] = useState<NotificationFeed | null>(null);
  const [failed, setFailed] = useState(false);
  const [absent, setAbsent] = useState(false);

  const apply = useCallback((next: NotificationFeed | null) => {
    if (next === null) { setAbsent(true); setFeed(null); } else { setFeed(next); setAbsent(false); }
    setFailed(false);
  }, []);
  const refresh = useCallback(async () => {
    try { apply(await fetchNotifications()); } catch { setFailed(true); }
  }, [apply]);

  useEffect(() => {
    let live = true;
    let timer = 0;
    let failures = 0;
    let onShow: (() => void) | null = null;
    const stopWaitingForShow = () => {
      if (onShow) document.removeEventListener("visibilitychange", onShow);
      onShow = null;
    };
    const read = async (): Promise<boolean> => {
      try {
        const next = await fetchNotifications();
        failures = 0;
        if (!live) return true;
        apply(next);
        // A server with no feed is asked once; there is nothing to wait for.
        return next === null;
      } catch {
        if (live) setFailed(true);
        failures += 1;
        return false;
      }
    };
    const tick = async () => {
      const done = await read();
      if (live && !done) schedule();
    };
    function schedule() {
      // A hidden tab that has desktop notifications on keeps reading, slowly, so a new item can still be announced.
      const announcing = () => typeof document !== "undefined" && document.hidden && desktopState() === "on";
      if (announcing()) {
        // And a tab shown again reads at once instead of waiting out the slow pace.
        stopWaitingForShow();
        onShow = () => {
          if (document.hidden) return;
          stopWaitingForShow();
          window.clearTimeout(timer);
          void tick();
        };
        document.addEventListener("visibilitychange", onShow);
      }
      timer = window.setTimeout(() => {
        if (typeof document !== "undefined" && document.hidden && !announcing()) {
          // A hidden tab asks nothing; it reads once when it is shown again.
          onShow = () => { stopWaitingForShow(); void tick(); };
          document.addEventListener("visibilitychange", onShow);
          return;
        }
        void tick();
      }, announcing() ? Math.max(pollDelayMs(failures), HIDDEN_POLL_MS) : pollDelayMs(failures));
    }
    void tick();
    return () => { live = false; window.clearTimeout(timer); stopWaitingForShow(); };
  }, [apply]);

  const markRead = useCallback(async (items: readonly NotificationItem[]) => {
    if (items.length === 0) return;
    try { apply(await markNotificationsRead(items)); } catch { setFailed(true); }
  }, [apply]);
  const clearAll = useCallback(async () => {
    try { apply(await clearNotifications()); } catch { setFailed(true); }
  }, [apply]);

  return { feed, failed, absent, refresh, markRead, clearAll };
}
