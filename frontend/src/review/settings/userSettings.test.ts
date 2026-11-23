import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.fn();
vi.mock("../reviewApi", () => ({ requestBrowserJson: (...args: unknown[]) => request(...args) }));

import { flowAction } from "../flowKeymap";
import {
  DEFAULT_VIEW_KEY,
  MIRROR_KEY,
  currentSettings,
  getFlowBindings,
  loadUserSettings,
  patchUserSettings,
  resetUserSettings,
  resetUserSettingsForTest,
  settingsRequested,
} from "./userSettings";
import { configureHostedReview } from "../hostedOrigin";
import { DEFAULT_SETTINGS } from "./settingsModel";

const wire = (settings: Record<string, unknown>, set: string[]) => ({ settings: { ...DEFAULT_SETTINGS, ...settings }, set });

beforeEach(() => {
  window.localStorage.clear();
  resetUserSettingsForTest();
  request.mockReset();
});
afterEach(() => window.localStorage.clear());

describe("loading the settings", () => {
  it("uses the server's answer once it has come", async () => {
    request.mockResolvedValueOnce(wire({ default_view: "all", key_looks_good: "y" }, ["default_view", "key_looks_good"]));
    await loadUserSettings();
    expect(currentSettings().default_view).toBe("all");
    expect(getFlowBindings().looksGood).toBe("y");
  });

  it("sends a browser choice the server lacks once, then uses the server's answer", async () => {
    window.localStorage.setItem(DEFAULT_VIEW_KEY, "all");
    window.localStorage.setItem("lemoncrow-theme", "dark");
    request.mockResolvedValueOnce(wire({}, [])).mockResolvedValueOnce(wire({ default_view: "all", theme: "dark" }, ["default_view", "theme"]));
    await loadUserSettings();
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1][0]).toBe("/api/review-settings");
    expect(request.mock.calls[1][1]).toMatchObject({ method: "PATCH", body: JSON.stringify({ default_view: "all", theme: "dark" }) });
    expect(currentSettings().theme).toBe("dark");
  });

  it("does not send what the server already holds", async () => {
    window.localStorage.setItem(DEFAULT_VIEW_KEY, "all");
    request.mockResolvedValueOnce(wire({ default_view: "one" }, ["default_view"]));
    await loadUserSettings();
    expect(request).toHaveBeenCalledTimes(1);
    expect(currentSettings().default_view).toBe("one");
  });

  it("falls back to this browser when the server cannot be reached", async () => {
    window.localStorage.setItem(DEFAULT_VIEW_KEY, "all");
    window.localStorage.setItem(MIRROR_KEY, JSON.stringify({ ...DEFAULT_SETTINGS, key_looks_good: "y" }));
    request.mockRejectedValueOnce(new Error("Failed to fetch"));
    await loadUserSettings();
    expect(currentSettings().default_view).toBe("all");
    expect(getFlowBindings().looksGood).toBe("y");
  });
});

describe("changing a setting", () => {
  it("shows the change at once and keeps the server's answer", async () => {
    request.mockResolvedValueOnce(wire({}, []));
    await loadUserSettings();
    let finish: (value: unknown) => void = () => undefined;
    request.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const pending = patchUserSettings({ key_looks_good: "y" });
    expect(getFlowBindings().looksGood).toBe("y");
    finish(wire({ key_looks_good: "y" }, ["key_looks_good"]));
    expect(await pending).toEqual({ ok: true });
  });

  it("puts the earlier key back and returns the server's sentence when it refuses", async () => {
    request.mockResolvedValueOnce(wire({}, []));
    await loadUserSettings();
    request.mockRejectedValueOnce(Object.assign(new Error("Y is already used for Needs a change. Pick another key."), { status: 422 }));
    const result = await patchUserSettings({ key_looks_good: "y" });
    expect(result).toEqual({ ok: false, message: "Y is already used for Needs a change. Pick another key.", offline: false });
    expect(getFlowBindings().looksGood).toBe("a");
  });

  it("keeps the change in this browser when the server cannot be reached", async () => {
    request.mockResolvedValueOnce(wire({}, []));
    await loadUserSettings();
    request.mockRejectedValueOnce(new Error("Failed to fetch"));
    const result = await patchUserSettings({ default_view: "all" });
    expect(result).toMatchObject({ ok: false, offline: true });
    expect(currentSettings().default_view).toBe("all");
    expect(window.localStorage.getItem(DEFAULT_VIEW_KEY)).toBe("all");
  });
});

describe("the hosted bundle (hosted-15.1)", () => {
  afterEach(() => configureHostedReview(false));

  it("never asks the local-only settings route and keeps a change in this browser", async () => {
    configureHostedReview();
    await loadUserSettings();
    expect(settingsRequested()).toBe(true);
    const result = await patchUserSettings({ default_view: "all" });
    expect(result).toEqual({ ok: false, message: "This server doesn't keep personal settings. The change is kept in this browser.", offline: true });
    expect(currentSettings().default_view).toBe("all");
    expect(await resetUserSettings()).toMatchObject({ ok: false, offline: true });
    expect(request).not.toHaveBeenCalled();
  });
});

describe("a rebound key reaches the keymap (SET-3)", () => {
  it("fires the new key and not the old one", async () => {
    request.mockResolvedValueOnce(wire({ key_looks_good: "y" }, ["key_looks_good"]));
    await loadUserSettings();
    const ctx = { screen: "one" as const, bindings: getFlowBindings() };
    expect(flowAction({ key: "y" }, ctx)?.intent).toBe("looksGood");
    expect(flowAction({ key: "a" }, ctx)).toBeNull();
  });
});
