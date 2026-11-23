/**
 * The browser's copy of the reviewer's server-held settings (SPEC 4.11, 7.1 P5-4). The server is the authority
 * once it has answered; until then, and whenever it cannot be reached, the browser's earlier choices stand
 * (`browserFallback`). Every change goes to the server; a change that cannot reach it stays in this browser and is
 * sent on the next load (`browserMigration`).
 */

import { useSyncExternalStore } from "react";

import { applyTheme } from "../../lib/theme";
import { isHostedReview } from "../hostedOrigin";
import { requestBrowserJson } from "../reviewApi";
import { countSettingsChange } from "../reviewFrictionCounts";
import type { KeyBindings } from "../flowKeymap";
import {
  DEFAULT_SETTINGS,
  bindingsOf,
  browserMigration,
  readSettings,
  resolveTheme,
  type SettingsPatch,
  type UserSettings,
} from "./settingsModel";

export const SETTINGS_PATH = "/api/review-settings";
/** The legacy per-browser keys: Overview's reading view and the app theme. They stay the offline copy. */
export const DEFAULT_VIEW_KEY = "lemoncrow.review.defaultView";
export const THEME_KEY = "lemoncrow-theme";
/** The last settings the server returned, so keys and flags survive a visit while it is unreachable. */
export const MIRROR_KEY = "lemoncrow.review.settingsMirror";

/** `offline`: the server did not answer. `unavailable`: it answered that it keeps no personal settings (a hosted workspace). */
export type SettingsStatus = "idle" | "loading" | "ready" | "offline" | "unavailable";

interface State {
  status: SettingsStatus;
  settings: UserSettings;
  /** The keys the server holds for this reviewer. */
  serverSet: readonly string[];
}

let state: State = { status: "idle", settings: DEFAULT_SETTINGS, serverSet: [] };
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;

function emit(next: State): void {
  state = next;
  listeners.forEach((listener) => listener());
}

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Storage blocked: the change lasts for this page only.
  }
}

/** What this browser knows without the server: the last server answer, then the legacy per-browser choices. */
export function browserFallback(): UserSettings {
  let mirrored: unknown = null;
  try {
    mirrored = JSON.parse(readStorage(MIRROR_KEY) ?? "null");
  } catch {
    mirrored = null;
  }
  const base = readSettings(mirrored);
  const legacyView = readStorage(DEFAULT_VIEW_KEY);
  return { ...base, default_view: legacyView === "all" ? "all" : legacyView === "one" ? "one" : base.default_view };
}

/** The settings now in force: the server's once it has answered, the browser's before that or when it cannot be reached. */
export function currentSettings(): UserSettings {
  return state.status === "ready" ? state.settings : browserFallback();
}

export function getFlowBindings(): KeyBindings {
  return bindingsOf(currentSettings());
}

function mirror(settings: UserSettings): void {
  writeStorage(MIRROR_KEY, JSON.stringify(settings));
  writeStorage(DEFAULT_VIEW_KEY, settings.default_view === "all" ? "all" : null);
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

/** Puts the chosen theme on the page. The header toggle keeps working; it writes the same choice back (App.tsx). */
export function applyThemeChoice(settings: UserSettings): void {
  applyTheme(resolveTheme(settings.theme, systemPrefersDark()), settings.theme === "auto" ? "auto" : true);
  window.dispatchEvent(new Event("lemoncrow-theme-change"));
}

interface Wire {
  settings: unknown;
  set: unknown;
}

/** A hosted workspace has no personal-settings route: answer as its 404 would, without asking (hosted-15.1). */
function settingsRequest(init?: RequestInit): Promise<Wire> {
  if (isHostedReview()) return Promise.reject(Object.assign(new Error("This server doesn't keep personal settings."), { status: 404 }));
  return init ? requestBrowserJson<Wire>(SETTINGS_PATH, init) : requestBrowserJson<Wire>(SETTINGS_PATH);
}

function setList(raw: unknown): readonly string[] {
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
}

function accept(wire: Wire): void {
  const settings = readSettings(wire.settings);
  mirror(settings);
  emit({ status: "ready", settings, serverSet: setList(wire.set) });
  applyThemeChoice(settings);
}

/** Loads the settings once per page, then sends any browser choice the server does not hold. Never throws. */
export function loadUserSettings(force = false): Promise<void> {
  if (inflight && !force) return inflight;
  if (state.status === "idle") emit({ ...state, status: "loading" });
  inflight = (async () => {
    try {
      const wire = await settingsRequest();
      // Read what the browser chose before the server's answer overwrites its copy.
      const browser = { defaultView: readStorage(DEFAULT_VIEW_KEY), theme: readStorage(THEME_KEY) };
      accept(wire);
      const patch = browserMigration(setList(wire.set), browser);
      if (Object.keys(patch).length > 0) accept(await settingsRequest(jsonInit("PATCH", patch)));
    } catch (error) {
      emit({ ...state, status: keepsNoSettings(error) ? "unavailable" : "offline" });
    }
  })();
  return inflight;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) };
}

export type PatchResult = { ok: true } | { ok: false; message: string; offline: boolean };

/** A hosted workspace has no such route (404) or refuses it (409): it keeps no personal settings for this browser. */
function keepsNoSettings(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  return status === 404 || status === 409;
}

function messageOf(error: unknown): { message: string; offline: boolean } {
  // The server's refusal (400 from the local server, 422 from the shared API) carries the sentence to show; anything
  // else (no answer, a 5xx) keeps the change here.
  const status = (error as { status?: number } | null)?.status;
  if ((status === 400 || status === 422) && error instanceof Error) return { message: error.message, offline: false };
  if (keepsNoSettings(error)) return { message: "This server doesn't keep personal settings. The change is kept in this browser.", offline: true };
  return { message: "Couldn't reach this machine's server. The change is kept in this browser.", offline: true };
}

/** Sends a change. When the server cannot be reached the change is kept in this browser and sent on the next load. */
export async function patchUserSettings(patch: SettingsPatch): Promise<PatchResult> {
  const before = state;
  // The change shows at once; the server's answer replaces it, and a refusal puts the earlier settings back.
  if (state.status === "ready") emit({ ...state, settings: readSettings({ ...state.settings, ...stripNull(patch) }) });
  try {
    accept(await settingsRequest(jsonInit("PATCH", patch)));
    countSettingsChange();
    return { ok: true };
  } catch (error) {
    const failure = messageOf(error);
    if (failure.offline) {
      const next = readSettings({ ...currentSettings(), ...stripNull(patch) });
      mirror(next);
      emit({ ...state, settings: next, status: before.status === "ready" ? "ready" : keepsNoSettings(error) ? "unavailable" : "offline" });
    } else {
      emit(before);
    }
    return { ok: false, ...failure };
  }
}

function stripNull(patch: SettingsPatch): Record<string, unknown> {
  return Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== null && value !== undefined));
}

/** Forgets every personal setting (back to the defaults) on the server and in this browser. */
export async function resetUserSettings(): Promise<PatchResult> {
  try {
    accept(await settingsRequest(jsonInit("DELETE")));
    writeStorage(MIRROR_KEY, null);
    return { ok: true };
  } catch (error) {
    return { ok: false, ...messageOf(error) };
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The settings in force, loading them from the server on first use. */
export function useUserSettings(): { settings: UserSettings; status: SettingsStatus } {
  const snapshot = useSyncExternalStore(subscribe, () => state);
  if (snapshot.status === "idle") void loadUserSettings();
  return { settings: snapshot.status === "ready" ? snapshot.settings : browserFallback(), status: snapshot.status };
}

export function useFlowBindings(): KeyBindings {
  return bindingsOf(useUserSettings().settings);
}

/** Whether this page has asked the server for the settings (the header theme toggle then writes its choice back). */
export function settingsRequested(): boolean {
  return state.status !== "idle";
}

/** Test seam: forget what was loaded. */
export function resetUserSettingsForTest(): void {
  state = { status: "idle", settings: DEFAULT_SETTINGS, serverSet: [] };
  inflight = null;
  listeners.clear();
}
