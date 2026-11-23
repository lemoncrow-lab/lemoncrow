// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { clearOfflineSend, readOfflineSend } from "./done/offlineReviewSend";
import { readPendingSendToken } from "./done/pendingSendToken";
import { DEFAULT_PREFS, HOME_PREFS_KEY, loadPrefs, parsePrefs } from "./home/homePrefs";
import { EVIDENCE_OPEN_KEY, getEvidenceOpen, getPlanApproved } from "./overview/defaultView";
import { parseLedger } from "./seen/seenModel";
import { browserFallback, MIRROR_KEY } from "./settings/userSettings";
import { DEFAULT_SETTINGS, readSettings } from "./settings/settingsModel";
import { readWrapImportance, wrapImportanceKey } from "./wrap/wrapModel";

afterEach(() => { window.localStorage.clear(); window.sessionStorage.clear(); });

const OFFLINE_KEY = "lc.review.offline-send.v1";
const PENDING_KEY = "lc.review.pending-send.v1";
const goodOffline = {
  reviewRef: "r1", reviewId: "id1", revisionId: "rev1", operationId: `fs-${"a".repeat(32)}`, outcome: "lgtm", summary: "ok",
  feedback: { revision_id: "rev1", feedback_hash: "h", operation_id: "op" }, createdAt: "2026-01-01T00:00:00Z", noteCount: 2,
  state: "waiting", attempted: false, error: "",
};

describe("saved state loads without throwing and falls back field by field (ST-4)", () => {
  it("Home prefs: junk JSON, wrong types, old shape", () => {
    for (const raw of ["{", "null", "42", "\"x\"", "[]", "{\"sort\":5,\"filters\":7}", "{\"filters\":{\"repos\":\"a\",\"sources\":[1,\"Nope\"],\"cameBack\":\"yes\"}}"]) {
      expect(() => parsePrefs(raw)).not.toThrow();
      expect(parsePrefs(raw).filters).toEqual(DEFAULT_PREFS.filters);
      expect(parsePrefs(raw).sort).toBe(DEFAULT_PREFS.sort);
    }
    // One good field survives a bad neighbour.
    expect(parsePrefs("{\"sort\":\"waiting\",\"filters\":{\"repos\":[\"a\",3],\"sources\":\"x\"}}").filters.repos).toEqual(["a"]);
    window.localStorage.setItem(HOME_PREFS_KEY, "{");
    expect(loadPrefs()).toEqual(DEFAULT_PREFS);
  });

  it("settings cache: wrong types fall back per field", () => {
    expect(readSettings("junk")).toEqual(DEFAULT_SETTINGS);
    const mixed = readSettings({ default_view: 3, theme: "dark", key_looks_good: "ab", key_needs_change: 5, skip_overview_single_decision: "no" });
    expect(mixed.theme).toBe("dark");
    expect(mixed.default_view).toBe(DEFAULT_SETTINGS.default_view);
    expect(mixed.key_looks_good).toBe(DEFAULT_SETTINGS.key_looks_good);
    expect(mixed.key_needs_change).toBe(DEFAULT_SETTINGS.key_needs_change);
    expect(mixed.skip_overview_single_decision).toBe(DEFAULT_SETTINGS.skip_overview_single_decision);
    window.localStorage.setItem(MIRROR_KEY, "{not json");
    expect(browserFallback()).toEqual(DEFAULT_SETTINGS);
  });

  it("Overview: unknown stored values read as the default", () => {
    window.localStorage.setItem(EVIDENCE_OPEN_KEY, "{}");
    expect(getEvidenceOpen()).toBe(false);
    window.localStorage.setItem("lemoncrow.review.planApproved.k", "maybe");
    expect(getPlanApproved("k")).toBe(false);
  });

  it("Wrap importance: only known kinds survive", () => {
    const key = wrapImportanceKey("r", "v");
    for (const raw of ["{", "[]", "7", "null"]) { window.localStorage.setItem(key, raw); expect(readWrapImportance("r", "v")).toEqual({}); }
    window.localStorage.setItem(key, JSON.stringify({ a: "must_fix", b: "bogus", c: 4 }));
    expect(readWrapImportance("r", "v")).toEqual({ a: "must_fix" });
  });

  it("Seen ledger: wrong version, wrong owner, junk rows", () => {
    const empty = parseLedger(null, "r", "u", "v");
    for (const raw of ["{", "null", "[]", "{\"version\":0}", JSON.stringify({ version: 1, reviewId: "other", revisionId: "v", reviewerId: "u", rows: {} }),
      JSON.stringify({ version: 1, reviewId: "r", revisionId: "v", reviewerId: "u", rows: [] })]) {
      expect(parseLedger(raw, "r", "u", "v")).toEqual(empty);
    }
    const mixed = parseLedger(JSON.stringify({ version: 1, reviewId: "r", revisionId: "v", reviewerId: "u",
      rows: { a: { blockKey: "a", elapsedMs: 5, source: "dwell" }, b: { blockKey: "b", elapsedMs: "5", source: "dwell" }, c: null },
      omission: { blockKeys: [1, "k"], recordedCount: "3" } }), "r", "u", "v");
    expect(Object.keys(mixed.rows)).toEqual(["a"]);
    expect(mixed.omission?.blockKeys).toEqual(["k"]);
    expect(mixed.omission?.recordedCount).toBeUndefined();
  });

  it("offline send: junk is none; a bad field falls back; a good record loads", () => {
    for (const raw of ["{", "null", "[]", "5", JSON.stringify({ ...goodOffline, state: "done" }), JSON.stringify({ ...goodOffline, operationId: "x" }),
      JSON.stringify({ ...goodOffline, outcome: "ship" }), JSON.stringify({ ...goodOffline, attempted: "no" })]) {
      window.sessionStorage.setItem(OFFLINE_KEY, raw);
      expect(readOfflineSend()).toBeNull();
    }
    window.sessionStorage.setItem(OFFLINE_KEY, JSON.stringify({ ...goodOffline, feedback: "x", createdAt: 5, noteCount: "2", error: {} }));
    const read = readOfflineSend();
    expect(read).toMatchObject({ feedback: null, createdAt: "", error: "" });
    expect(read && "noteCount" in read).toBe(false);
    window.sessionStorage.setItem(OFFLINE_KEY, JSON.stringify(goodOffline));
    expect(readOfflineSend()).toMatchObject({ noteCount: 2, feedback: goodOffline.feedback });
    clearOfflineSend(goodOffline.operationId);
  });

  it("pending send token: a bad required field drops it; a bad label only loses the label", () => {
    const good = { reviewRef: "r", reviewId: "i", operationId: `fs-${"b".repeat(32)}`, sendAfter: "2026-01-01T00:00:00Z" };
    for (const raw of ["{", "null", "[]", JSON.stringify({ ...good, sendAfter: "soon" }), JSON.stringify({ ...good, reviewRef: 3 }),
      JSON.stringify({ ...good, reviewId: "" }), JSON.stringify({ ...good, operationId: "fs-x" })]) {
      window.sessionStorage.setItem(PENDING_KEY, raw);
      expect(readPendingSendToken()).toBeNull();
    }
    window.sessionStorage.setItem(PENDING_KEY, JSON.stringify({ ...good, destinationLabel: 4, extra: "x" }));
    expect(readPendingSendToken()).toEqual(good);
    window.sessionStorage.setItem(PENDING_KEY, JSON.stringify({ ...good, destinationLabel: "Claude Code" }));
    expect(readPendingSendToken()).toEqual({ ...good, destinationLabel: "Claude Code" });
  });

  it("offline send: a malformed presentation is dropped, a good one kept", () => {
    const presentation = { verdict: "lgtm", importance: { n1: "must_fix" }, test_with_fix: false, summary: "ok" };
    const bad = [{ ...presentation, verdict: "maybe" }, { ...presentation, importance: { n1: "huge" } }, { ...presentation, importance: [] },
      { ...presentation, test_with_fix: "no" }, { ...presentation, summary: 3 }, "x", []];
    for (const p of bad) {
      window.sessionStorage.setItem(OFFLINE_KEY, JSON.stringify({ ...goodOffline, feedback: { ...goodOffline.feedback, presentation: p } }));
      expect(readOfflineSend()?.feedback).toEqual(goodOffline.feedback);
    }
    window.sessionStorage.setItem(OFFLINE_KEY, JSON.stringify({ ...goodOffline, feedback: { ...goodOffline.feedback, presentation } }));
    expect(readOfflineSend()?.feedback).toEqual({ ...goodOffline.feedback, presentation });
    clearOfflineSend(goodOffline.operationId);
  });
});
