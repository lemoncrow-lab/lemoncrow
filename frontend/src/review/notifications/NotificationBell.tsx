import { Bell } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogTitle } from "../../components/ui/dialog";
import {
  DESKTOP_BLOCKED_NOTE, DESKTOP_OFF_LABEL, DESKTOP_ON_LABEL, DESKTOP_ON_NOTE, desktopState, itemId, newlyUnread, showDesktop, turnOffDesktop, turnOnDesktop, type DesktopState,
} from "./desktopNotify";
import { ALL_CAUGHT_UP, bellLabel, CLEAR_ALL, CLOSE, FEED_FAILED, MARK_READ, NOTIFICATIONS_TITLE } from "./notificationCopy";
import { reportTimeZone, type NotificationItem } from "./notificationsApi";
import { DeliveryControl } from "./DeliveryControl";
import { PushControl } from "./PushControl";
import { useNotificationFeed } from "./useNotificationFeed";

/**
 * The bell and its "Notifications" dialog (SPEC 4.13). Hosted only (SPEC 7.1 P0-2): on a local install the feed does
 * not exist, so nothing is drawn and nothing is asked. Every item opens the exact place by its product link; focus
 * returns to the bell on close.
 */
export function NotificationBell() {
  const navigate = useNavigate();
  const { feed, failed, absent, refresh, markRead, clearAll } = useNotificationFeed();
  const [open, setOpen] = useState(false);
  const bell = useRef<HTMLButtonElement>(null);
  const [desktop, setDesktop] = useState<DesktopState>(desktopState);
  // Items already known: the first read only fills this, so opening the page never announces what was waiting.
  const seen = useRef<Set<string> | null>(null);
  const openHref = useCallback((href: string) => navigate(href), [navigate]);
  const hasFeed = feed !== null;
  // The daily digest is at 09:00 where the person is: tell the server where that is, once the feed exists.
  useEffect(() => { if (hasFeed) void Promise.resolve(reportTimeZone()).catch(() => undefined); }, [hasFeed]);
  useEffect(() => {
    if (!feed) return;
    const known = seen.current;
    seen.current = new Set(feed.items.map(itemId));
    // Only a tab the person is not looking at is told: the bell already shows the rest.
    if (known && typeof document !== "undefined" && !document.hasFocus()) showDesktop(newlyUnread(feed.items, known), openHref);
  }, [feed, openHref]);
  if (absent || (feed === null && !failed)) return null;

  const items = feed?.items ?? [];
  const unread = feed?.unread ?? 0;
  const openItem = (item: NotificationItem) => {
    if (!item.read) void markRead([item]);
    setOpen(false);
    navigate(item.href);
  };

  return (
    <>
      <Button
        ref={bell}
        variant="ghost"
        size="icon"
        className="relative min-w-11"
        aria-label={bellLabel(unread)}
        aria-haspopup="dialog"
        data-testid="notification-bell"
        onClick={() => { setOpen(true); void refresh(); }}
      >
        <Bell size={16} aria-hidden="true" />
        {unread > 0 ? <span aria-hidden="true" className="absolute right-1.5 top-1.5 size-2 rounded-full bg-p0-solid" /> : null}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent aria-describedby={undefined} data-testid="notification-dialog">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3">
            <DialogTitle>{NOTIFICATIONS_TITLE}</DialogTitle>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="sm" disabled={items.length === 0} onClick={() => void clearAll()}>{CLEAR_ALL}</Button>
              <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>{CLOSE}</Button>
            </div>
          </div>
          <DialogBody>
            {desktop === "off" ? (
              <Button variant="outline" size="sm" className="mb-3" data-testid="desktop-on" onClick={() => void turnOnDesktop().then(setDesktop)}>{DESKTOP_ON_LABEL}</Button>
            ) : null}
            {desktop === "on" ? (
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <p className="text-meta text-muted-foreground" data-testid="desktop-state">{DESKTOP_ON_NOTE}</p>
                <Button variant="ghost" size="sm" data-testid="desktop-off" onClick={() => setDesktop(turnOffDesktop())}>{DESKTOP_OFF_LABEL}</Button>
              </div>
            ) : null}
            {desktop === "denied" ? <p className="mb-3 text-meta text-muted-foreground" data-testid="desktop-state">{DESKTOP_BLOCKED_NOTE}</p> : null}
            <DeliveryControl />
            <PushControl />
            {failed && feed === null ? <p role="alert" className="text-label">{FEED_FAILED}</p> : null}
            {items.length === 0 && !(failed && feed === null) ? <p className="text-label text-muted-foreground" data-testid="notification-empty">{ALL_CAUGHT_UP}</p> : null}
            <ul className="space-y-3">
              {items.map((item) => (
                <li key={`${item.review_id}:${item.id}`} className="space-y-1 rounded-md border border-border p-3" data-testid="notification-item" data-read={item.read ? "true" : "false"}>
                  <p className={`text-label ${item.read ? "text-muted-foreground" : "font-semibold"}`}>{item.title}</p>
                  {item.body ? <p className="text-meta text-muted-foreground">{item.body}</p> : null}
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <Button variant={item.action === "Review now" ? "solid" : "outline"} size="sm" onClick={() => openItem(item)}>{item.action}</Button>
                    {item.read ? null : (
                      <Button variant="ghost" size="sm" aria-label={`${MARK_READ}, ${item.title}`} onClick={() => void markRead([item])}>{MARK_READ}</Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </DialogBody>
        </DialogContent>
      </Dialog>
    </>
  );
}
