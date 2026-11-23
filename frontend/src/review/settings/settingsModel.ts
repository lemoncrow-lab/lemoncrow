/**
 * Per-user Review settings (SPEC 4.11): the keys, the defaults, and the pure rules around them. The server
 * (`user_settings.py`) validates and stores them; this module reads what it returns and decides what the flow does
 * with it. Nothing here reads the browser or the network.
 */

import { DEFAULT_BINDINGS, rebindKey, type KeyBindings, type RebindableIntent } from "../flowKeymap";
import type { ReadingView } from "../overview/defaultView";

export type ThemeChoice = "light" | "dark" | "auto";

export interface UserSettings {
  default_view: ReadingView;
  skip_overview_single_decision: boolean;
  start_all_files_over_30_parts: boolean;
  theme: ThemeChoice;
  key_looks_good: string;
  key_needs_change: string;
  key_decide_later: string;
}

export type SettingsPatch = { [K in keyof UserSettings]?: UserSettings[K] | null };

/** Mirrors `DEFAULTS` in `user_settings.py`. */
export const DEFAULT_SETTINGS: UserSettings = {
  default_view: "one",
  skip_overview_single_decision: true,
  start_all_files_over_30_parts: true,
  theme: "auto",
  key_looks_good: "a",
  key_needs_change: "c",
  key_decide_later: "n",
};

/** The Overview's "over 30 parts" rule (SPEC 4.11 Reading). */
export const LARGE_CHANGE_PARTS = 30;

const KEY_FIELD: Readonly<Record<RebindableIntent, "key_looks_good" | "key_needs_change" | "key_decide_later">> = {
  looksGood: "key_looks_good",
  needsChange: "key_needs_change",
  decideLater: "key_decide_later",
};

export function keyField(intent: RebindableIntent): (typeof KEY_FIELD)[RebindableIntent] {
  return KEY_FIELD[intent];
}

export function bindingsOf(settings: UserSettings): KeyBindings {
  return { looksGood: settings.key_looks_good, needsChange: settings.key_needs_change, decideLater: settings.key_decide_later };
}

/** A server answer read defensively: any field that is missing or not one of its allowed values falls back to its default. */
export function readSettings(raw: unknown): UserSettings {
  const source = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
  const key = (value: unknown, fallback: string) => (typeof value === "string" && value.length === 1 ? value : fallback);
  const bool = (value: unknown, fallback: boolean) => (typeof value === "boolean" ? value : fallback);
  const d = DEFAULT_SETTINGS;
  return {
    default_view: source.default_view === "all" ? "all" : source.default_view === "one" ? "one" : d.default_view,
    skip_overview_single_decision: bool(source.skip_overview_single_decision, d.skip_overview_single_decision),
    start_all_files_over_30_parts: bool(source.start_all_files_over_30_parts, d.start_all_files_over_30_parts),
    theme: source.theme === "dark" || source.theme === "auto" || source.theme === "light" ? source.theme : d.theme,
    key_looks_good: key(source.key_looks_good, d.key_looks_good),
    key_needs_change: key(source.key_needs_change, d.key_needs_change),
    key_decide_later: key(source.key_decide_later, d.key_decide_later),
  };
}

/** What this browser stored before the server held it. `null` means the browser never chose. */
export interface BrowserPreferences {
  defaultView: string | null;
  theme: string | null;
}

/**
 * The one-time move of browser preferences to the server: only a choice that differs from the default, and only for
 * a key the server has not been given. Run on every load, it also replays a choice made while the server was
 * unreachable, because that choice never reached the server's `set` list.
 */
export function browserMigration(serverSet: readonly string[], browser: BrowserPreferences): SettingsPatch {
  const patch: SettingsPatch = {};
  if (!serverSet.includes("default_view") && browser.defaultView === "all") patch.default_view = "all";
  if (!serverSet.includes("theme") && browser.theme === "dark") patch.theme = "dark";
  return patch;
}

/** The theme to put on the page: Auto follows the system. */
export function resolveTheme(choice: ThemeChoice, systemPrefersDark: boolean): "light" | "dark" {
  return choice === "auto" ? (systemPrefersDark ? "dark" : "light") : choice;
}

/**
 * Where a review opens (SPEC 4.2, 4.11 Reading): the reading view Start uses. A change over 30 parts opens in All
 * files when that setting is on; otherwise the reviewer's default view.
 */
export function startView(settings: UserSettings, partCount: number): ReadingView {
  return settings.start_all_files_over_30_parts && partCount > LARGE_CHANGE_PARTS ? "all" : settings.default_view;
}

/** Whether opening a review from outside it goes straight past the Overview (SPEC 4.11, SET-2). */
export function skipsOverview(settings: UserSettings, decisionCount: number): boolean {
  return settings.skip_overview_single_decision && decisionCount === 1;
}

export const INTENT_LABEL: Readonly<Record<RebindableIntent, string>> = { looksGood: "Looks good", needsChange: "Needs a change", decideLater: "Decide later" };
export const REBINDABLE: readonly RebindableIntent[] = ["looksGood", "needsChange", "decideLater"];

export type CaptureResult =
  | { kind: "wait" }
  | { kind: "cancel" }
  | { kind: "refused"; message: string }
  | { kind: "set"; key: string; message: string };

/** One key pressed while "Press the new key" is open (SPEC 4.11 Keyboard). A bare modifier keeps waiting; Esc cancels. */
export function captureKey(
  current: KeyBindings,
  intent: RebindableIntent,
  event: { key: string; shiftKey?: boolean; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean },
): CaptureResult {
  if (event.key === "Escape") return { kind: "cancel" };
  if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(event.key)) return { kind: "wait" };
  if (event.metaKey || event.ctrlKey || event.altKey) return { kind: "refused", message: "Press the key on its own, without ⌘, Ctrl or Alt." };
  const result = rebindKey(current, intent, event.key, event.shiftKey === true);
  if (!result.ok) return { kind: "refused", message: result.reason };
  return { kind: "set", key: result.bindings[intent], message: `${INTENT_LABEL[intent]} is now ${result.bindings[intent].toUpperCase()}` };
}

/** "Keys reset to A, C and N" (SPEC 4.11). */
export function resetMessage(): string {
  const d = DEFAULT_BINDINGS;
  return `Keys reset to ${d.looksGood.toUpperCase()}, ${d.needsChange.toUpperCase()} and ${d.decideLater.toUpperCase()}`;
}

/** The fields that put the three keys back to their defaults. */
export const RESET_KEYS_PATCH: SettingsPatch = { key_looks_good: null, key_needs_change: null, key_decide_later: null };
