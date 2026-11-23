import { describe, expect, it } from "vitest";

import { DEFAULT_BINDINGS } from "../flowKeymap";
import {
  DEFAULT_SETTINGS,
  bindingsOf,
  browserMigration,
  captureKey,
  readSettings,
  resetMessage,
  resolveTheme,
  skipsOverview,
  startView,
} from "./settingsModel";

describe("reading a server answer", () => {
  it("keeps a real answer as it is", () => {
    const wire = { default_view: "all", skip_overview_single_decision: false, start_all_files_over_30_parts: true, theme: "auto", key_looks_good: "y", key_needs_change: "c", key_decide_later: "n" };
    expect(readSettings(wire)).toEqual(wire);
  });

  it("falls back per field for junk, so one bad field never drops the rest", () => {
    expect(readSettings({ default_view: "grid", theme: 3, key_looks_good: "ab", skip_overview_single_decision: "no", key_needs_change: "t" })).toEqual({ ...DEFAULT_SETTINGS, key_needs_change: "t" });
    expect(readSettings(null)).toEqual(DEFAULT_SETTINGS);
  });
});

describe("moving browser preferences to the server", () => {
  it("sends a browser choice the server does not hold", () => {
    expect(browserMigration([], { defaultView: "all", theme: "dark" })).toEqual({ default_view: "all", theme: "dark" });
  });

  it("sends nothing the server already holds or that is only the default", () => {
    expect(browserMigration(["default_view", "theme"], { defaultView: "all", theme: "dark" })).toEqual({});
    expect(browserMigration([], { defaultView: "one", theme: "light" })).toEqual({});
    expect(browserMigration([], { defaultView: null, theme: null })).toEqual({});
  });
});

describe("Look", () => {
  it("Auto follows the system, a fixed choice does not", () => {
    expect(resolveTheme("auto", true)).toBe("dark");
    expect(resolveTheme("auto", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });
});

describe("where a review opens", () => {
  it("skips the Overview for exactly 1 decision, and only while the switch is on", () => {
    expect(skipsOverview(DEFAULT_SETTINGS, 1)).toBe(true);
    expect(skipsOverview(DEFAULT_SETTINGS, 0)).toBe(false);
    expect(skipsOverview(DEFAULT_SETTINGS, 2)).toBe(false);
    expect(skipsOverview({ ...DEFAULT_SETTINGS, skip_overview_single_decision: false }, 1)).toBe(false);
  });

  it("opens All files for a change over 30 parts, not at 30, and not when the switch is off", () => {
    expect(startView(DEFAULT_SETTINGS, 31)).toBe("all");
    expect(startView(DEFAULT_SETTINGS, 30)).toBe("one");
    expect(startView({ ...DEFAULT_SETTINGS, start_all_files_over_30_parts: false }, 31)).toBe("one");
    expect(startView({ ...DEFAULT_SETTINGS, default_view: "all" }, 3)).toBe("all");
  });
});

describe("rebinding a key (SET-3)", () => {
  it("takes the next plain key", () => {
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "y" })).toEqual({ kind: "set", key: "y", message: "Looks good is now Y" });
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "Y" })).toMatchObject({ kind: "set", key: "y" });
  });

  it("names the other action when the key is taken", () => {
    const rebound = { ...DEFAULT_BINDINGS, needsChange: "y" };
    expect(captureKey(rebound, "looksGood", { key: "y" })).toEqual({ kind: "refused", message: "Y is already used for Needs a change. Pick another key." });
  });

  it("refuses a reserved key and keeps the one it had", () => {
    expect(captureKey(DEFAULT_BINDINGS, "decideLater", { key: "j" })).toEqual({ kind: "refused", message: "J is already used for next part. Pick another key." });
  });

  it("cancels on Esc, waits on a bare modifier, refuses a chord and a Shift letter", () => {
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "Escape" })).toEqual({ kind: "cancel" });
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "Shift" })).toEqual({ kind: "wait" });
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "y", metaKey: true })).toMatchObject({ kind: "refused" });
    expect(captureKey(DEFAULT_BINDINGS, "looksGood", { key: "Y", shiftKey: true })).toEqual({ kind: "refused", message: "Press the letter without Shift." });
  });

  it("reads the three bindings from the settings and words the reset", () => {
    expect(bindingsOf({ ...DEFAULT_SETTINGS, key_looks_good: "y" })).toEqual({ looksGood: "y", needsChange: "c", decideLater: "n" });
    expect(resetMessage()).toBe("Keys reset to A, C and N");
  });
});
