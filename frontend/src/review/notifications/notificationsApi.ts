import { hostedJson } from "../hostedOnly";

/** One item of the in-app feed (`GET /v1/reviews/notifications`, hosted only; SPEC 4.13, 7.1 P0-2). */
export interface NotificationItem {
  id: string;
  repo_id: string;
  review_id: string;
  kind: string;
  class: string;
  title: string;
  body: string;
  /** "Review now" for a returned revision, else "Open". */
  action: string;
  /** The exact place, a product link. */
  href: string;
  at: string;
  read: boolean;
}

export interface NotificationFeed {
  items: NotificationItem[];
  unread: number;
  truncated: boolean;
  marker_ttl_days: number;
}

const FEED = "/v1/reviews/notifications";

/** The feed, or `null` where this server has none (a local install). */
export function fetchNotifications(): Promise<NotificationFeed | null> {
  return hostedJson<NotificationFeed>(FEED);
}

const refOf = (item: NotificationItem) => ({ repo_id: item.repo_id, review_id: item.review_id, key: item.id });

export function markNotificationsRead(items: readonly NotificationItem[]): Promise<NotificationFeed | null> {
  return hostedJson<NotificationFeed>(`${FEED}/read`, { method: "POST", body: JSON.stringify({ items: items.map(refOf) }) });
}

export function clearNotifications(): Promise<NotificationFeed | null> {
  return hostedJson<NotificationFeed>(`${FEED}/clear`, { method: "POST", body: "{}" });
}

export const TIME_ZONE_KEY = "lemoncrow.review.timeZoneReported";

/**
 * Tell the server which time zone this browser is in, so the daily digest arrives at 09:00 where the person is
 * (SPEC 7.1 P9-5 H). At most once a day per zone: the server keeps it, and a changed zone is sent at once.
 */
export async function reportTimeZone(now: Date = new Date()): Promise<void> {
  let zone = "";
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return; }
  if (!zone) return;
  const stamp = `${zone}|${now.toISOString().slice(0, 10)}`;
  try { if (window.localStorage.getItem(TIME_ZONE_KEY) === stamp) return; } catch { /* storage can be unavailable: report anyway */ }
  await hostedJson("/v1/reviews/notifications/time-zone", { method: "PUT", body: JSON.stringify({ time_zone: zone }) });
  try { window.localStorage.setItem(TIME_ZONE_KEY, stamp); } catch { /* reported; asked again next time */ }
}

const PREFERENCES = "/v1/reviews/notification-preferences";

/** The person's own notification preferences, as far as the delivery choice needs them. */
export interface Delivery {
  delivery: "immediate" | "digest";
  version: number;
  enabled_classes: string[];
  default_classes: string[];
}

export function fetchDelivery(): Promise<Delivery | null> {
  return hostedJson<Delivery>(PREFERENCES);
}

/** Save the delivery choice, keeping the person's classes as they are (the default set stays "no explicit choice"). */
export async function saveDelivery(current: Delivery, delivery: "immediate" | "digest"): Promise<Delivery> {
  const sameAsDefault = [...current.enabled_classes].sort().join() === [...current.default_classes].sort().join();
  const saved = await hostedJson<Delivery>(PREFERENCES, {
    method: "PUT",
    body: JSON.stringify({ enabled_classes: sameAsDefault ? null : current.enabled_classes, delivery, version: current.version }),
  });
  if (!saved) throw new Error("Couldn't save that. Try again.");
  return saved;
}
