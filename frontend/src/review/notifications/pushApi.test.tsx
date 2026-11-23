import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { hostedJson } from "../hostedOnly";
import { PushControl } from "./PushControl";
import { browserPush, pushState, SERVICE_WORKER_PATH, turnOffPush, turnOnPush } from "./pushApi";

vi.mock("../hostedOnly", () => ({ hostedJson: vi.fn() }));
const hosted = vi.mocked(hostedJson);

// A 65-byte uncompressed P-256 point, url-safe base64 (what the server sends as its public key).
const PUBLIC = btoa(String.fromCharCode(4, ...Array.from({ length: 64 }, (_, n) => n + 1))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

let subscription: { endpoint: string; toJSON: () => unknown; unsubscribe: ReturnType<typeof vi.fn> } | null;
const subscribe = vi.fn();

function install(permission: NotificationPermission) {
  subscription = null;
  subscribe.mockReset().mockImplementation(async () => {
    subscription = { endpoint: "https://fcm.googleapis.com/fcm/send/abc", toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/abc", keys: { p256dh: "p", auth: "a" } }), unsubscribe: vi.fn(async () => true) };
    return subscription;
  });
  const registration = { pushManager: { subscribe, getSubscription: async () => subscription } };
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { register: vi.fn(async () => registration), ready: Promise.resolve(registration), getRegistration: vi.fn(async () => registration) },
  });
  Object.defineProperty(window, "PushManager", { configurable: true, value: class {} });
  Object.defineProperty(window, "Notification", { configurable: true, value: { permission, requestPermission: vi.fn(async () => "granted") } });
}

beforeEach(() => { hosted.mockReset(); install("default"); });
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, "serviceWorker");
  Object.defineProperty(window, "PushManager", { configurable: true, value: undefined });
  Object.defineProperty(window, "Notification", { configurable: true, value: undefined });
});

describe("Web Push on this device (NOTIF-2)", () => {
  it("subscribes with the server's public key, sends the subscription to the server, and can turn it off", async () => {
    hosted.mockResolvedValue({});
    expect(await turnOnPush(PUBLIC)).toBe("on");
    const key = subscribe.mock.calls[0]![0].applicationServerKey as Uint8Array;
    expect(subscribe.mock.calls[0]![0].userVisibleOnly).toBe(true);
    expect(key.length).toBe(65);
    expect(key[0]).toBe(4);
    expect(hosted).toHaveBeenCalledWith("/v1/reviews/push/subscription", { method: "PUT", body: JSON.stringify({ endpoint: "https://fcm.googleapis.com/fcm/send/abc", keys: { p256dh: "p", auth: "a" } }) });
    expect(await pushState({ supported: true, public_key: PUBLIC, subscribed: true })).toBe("on");
    const gone = subscription!;
    expect(await turnOffPush()).toBe("off");
    expect(hosted).toHaveBeenLastCalledWith("/v1/reviews/push/subscription", { method: "DELETE", body: JSON.stringify({ endpoint: "https://fcm.googleapis.com/fcm/send/abc" }) });
    expect(gone.unsubscribe).toHaveBeenCalled();
    expect(SERVICE_WORKER_PATH).toBe("/push-sw.js");
  });

  it("subscribes nothing when the person says no to the browser's question", async () => {
    (window.Notification.requestPermission as ReturnType<typeof vi.fn>).mockResolvedValue("denied");
    expect(await turnOnPush(PUBLIC)).toBe("denied");
    expect(subscribe).not.toHaveBeenCalled();
    expect(hosted).not.toHaveBeenCalled();
  });

  it("is blocked when the browser blocks it, and unsupported where the server cannot push or the browser has no service worker", async () => {
    install("denied");
    expect(await pushState({ supported: true, public_key: PUBLIC, subscribed: false })).toBe("denied");
    expect(await pushState({ supported: false, public_key: "", subscribed: false })).toBe("unsupported");
    expect(await pushState(null)).toBe("unsupported");
    Reflect.deleteProperty(navigator, "serviceWorker");
    expect(browserPush()).toBe("unsupported");
  });

  it("the control draws nothing where push is not offered, and offers it where it is", async () => {
    hosted.mockResolvedValueOnce({ supported: false, public_key: "", subscribed: false });
    const none = render(<PushControl />);
    await waitFor(() => expect(hosted).toHaveBeenCalled());
    expect(none.container.querySelector("[data-testid=push-control]")).toBeNull();
    none.unmount();
    hosted.mockResolvedValueOnce({ supported: true, public_key: PUBLIC, subscribed: false });
    render(<PushControl />);
    const button = await screen.findByRole("button", { name: "Get notifications on this device" });
    hosted.mockResolvedValue({});
    await userEvent.click(button);
    expect((await screen.findByTestId("push-state")).textContent).toBe("Notifications on this device are on.");
    await userEvent.click(screen.getByRole("button", { name: "Turn off notifications on this device" }));
    expect(await screen.findByRole("button", { name: "Get notifications on this device" })).toBeTruthy();
  });
});
