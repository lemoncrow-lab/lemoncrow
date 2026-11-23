import { hostedJson } from "../hostedOnly";

/**
 * Web Push to this device (SPEC 4.13 NOTIF-2, 7.1 P9-5): the same words as the bell and the desktop notification,
 * sent by the browser's push service to a service worker, so a phone or a closed browser is told too.
 */
export interface PushStatus { supported: boolean; public_key: string; subscribed: boolean }

export const PUSH_ON_LABEL = "Get notifications on this device";
export const PUSH_OFF_LABEL = "Turn off notifications on this device";
export const PUSH_ON_NOTE = "Notifications on this device are on.";
export const PUSH_BLOCKED_NOTE = "Notifications are blocked for this site on this device.";
export const PUSH_IOS_NOTE = "On an iPhone, add LemonCrow to your Home Screen first, then turn this on from there.";
export const SERVICE_WORKER_PATH = "/push-sw.js";

export type PushState = "unsupported" | "needs-home-screen" | "off" | "on" | "denied";

export const fetchPushStatus = () => hostedJson<PushStatus>("/v1/reviews/push");

/** This browser can take a push (a service worker and a PushManager), or why not. */
export function browserPush(): "ok" | "needs-home-screen" | "unsupported" {
  if (typeof window === "undefined" || !("serviceWorker" in navigator) || typeof window.PushManager === "undefined") {
    const ios = typeof navigator !== "undefined" && /iPhone|iPad|iPod/.test(navigator.userAgent);
    const standalone = typeof window !== "undefined" && (window.matchMedia?.("(display-mode: standalone)").matches ?? false);
    return ios && !standalone ? "needs-home-screen" : "unsupported";
  }
  return "ok";
}

function keyBytes(base64Url: string): Uint8Array<ArrayBuffer> {
  const padded = base64Url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64Url.length / 4) * 4, "=");
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export async function pushState(status: PushStatus | null): Promise<PushState> {
  if (!status?.supported) return "unsupported";
  const support = browserPush();
  if (support !== "ok") return support === "needs-home-screen" ? "needs-home-screen" : "unsupported";
  if (typeof Notification !== "undefined" && Notification.permission === "denied") return "denied";
  const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_PATH);
  const here = registration ? await registration.pushManager.getSubscription() : null;
  return here && status.subscribed ? "on" : "off";
}

export async function turnOnPush(publicKey: string): Promise<PushState> {
  if (browserPush() !== "ok") return "unsupported";
  const registration = await navigator.serviceWorker.register(SERVICE_WORKER_PATH);
  await navigator.serviceWorker.ready;
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
  await hostedJson("/v1/reviews/push/subscription", { method: "PUT", body: JSON.stringify(subscription.toJSON()) });
  return "on";
}

export async function turnOffPush(): Promise<PushState> {
  const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_PATH);
  const subscription = registration ? await registration.pushManager.getSubscription() : null;
  if (subscription) {
    await hostedJson("/v1/reviews/push/subscription", { method: "DELETE", body: JSON.stringify({ endpoint: subscription.endpoint }) });
    await subscription.unsubscribe();
  }
  return "off";
}
