import { describe, expect, it, vi } from "vitest";

import { DEFAULT_BINDINGS } from "../flowKeymap";
import {
  allItems, contextActions, emptyText, fuzzy, partStateLabel, pushRecent, resultsAnnouncement, shownGroups,
  type PaletteActions, type PaletteContext, type PaletteData, type PaletteItem,
} from "./paletteModel";

const fn = () => vi.fn();
const everything = (): PaletteActions => ({
  startNext: fn(), snooze: fn(), filterRepo: fn(), sort: fn(), looksGood: fn(), needsChange: fn(), decideLater: fn(), structure: fn(),
  switchView: fn(), wrapUp: fn(), copyLink: fn(), ask: fn(), start: fn(), send: fn(), changeVerdict: fn(), reviewRevision2: fn(),
  startNextReview: fn(), undo: fn(), snoozeReview: fn(), goHome: fn(),
});
const labels = (ctx: PaletteContext) => contextActions(ctx).map((item) => item.label);
const home = { hasNext: true, selected: 0, canSnooze: true, dense: true, repos: ["shop", "payments"], activeRepos: [] as string[], sorts: [{ key: "waiting", label: "Waiting longest" }, { key: "fewest", label: "Fewest decisions first" }], sort: "waiting" };

describe("contextActions: Home", () => {
  it("lists Start next review, Snooze current review, Filter and Sort in SPEC order", () => {
    expect(labels({ screen: "home", home, actions: everything() })).toEqual([
      "Start next review", "Snooze current review", "Filter: shop", "Filter: payments", "Sort: fewest decisions first",
    ]);
  });
  it("says Snooze selected when rows are selected", () => {
    expect(labels({ screen: "home", home: { ...home, selected: 2 }, actions: everything() })).toContain("Snooze selected");
  });
  it("drops Snooze when nothing can be snoozed and Start next when nothing waits", () => {
    const out = labels({ screen: "home", home: { ...home, hasNext: false, canSnooze: false }, actions: everything() });
    expect(out).not.toContain("Start next review");
    expect(out.some((label) => label.startsWith("Snooze"))).toBe(false);
  });
  it("offers Filter and Sort only with the dense list, and not a filter or sort already on", () => {
    const sparse = labels({ screen: "home", home: { ...home, dense: false }, actions: everything() });
    expect(sparse.some((label) => label.startsWith("Filter") || label.startsWith("Sort"))).toBe(false);
    const on = labels({ screen: "home", home: { ...home, activeRepos: ["shop"], sort: "fewest" }, actions: everything() });
    expect(on).toContain("Filter: payments");
    expect(on).not.toContain("Filter: shop");
    expect(on).toContain("Sort: waiting longest");
    expect(on).not.toContain("Sort: fewest decisions first");
  });
  it("runs the screen's own function with the repo or sort key", () => {
    const actions = everything();
    const items = contextActions({ screen: "home", home, actions });
    items.find((item) => item.label === "Filter: payments")?.run();
    items.find((item) => item.label === "Sort: fewest decisions first")?.run();
    expect(actions.filterRepo).toHaveBeenCalledWith("payments");
    expect(actions.sort).toHaveBeenCalledWith("fewest");
  });
});

describe("contextActions: reading a part", () => {
  const base: PaletteContext = { screen: "one", part: "apply_discount", canAnswer: true, isDecision: true, structureAvailable: true, aiAsk: true, view: "one", actions: everything() };
  it("One at a time lists the part group exactly as SPEC 4.12 does", () => {
    expect(labels(base)).toEqual(["Looks good", "Needs a change", "Decide later", "Show structure", "Switch to All files", "Go to Wrap up", "Copy link to this part", "Ask about this part"]);
  });
  it("names the group after the part, and 'In this review' with no part", () => {
    expect(contextActions(base)[0].group).toBe("This part · apply_discount");
    expect(contextActions({ ...base, part: undefined, canAnswer: false })[0].group).toBe("In this review");
  });
  it("shows the bound keys, so a rebound key shows everywhere", () => {
    const hints = contextActions({ ...base, bindings: { looksGood: "y", needsChange: "x", decideLater: "m" } }).map((item) => item.hint);
    expect(hints.slice(0, 3)).toEqual(["Y", "X", "M"]);
    expect(contextActions(base).slice(0, 3).map((item) => item.hint)).toEqual(["A", "C", "N"]);
    expect(DEFAULT_BINDINGS.looksGood).toBe("a");
  });
  it("drops the answers when the person cannot answer (read-only, saving, paused)", () => {
    const out = labels({ ...base, canAnswer: false });
    expect(out).not.toContain("Looks good");
    expect(out).not.toContain("Needs a change");
    expect(out).not.toContain("Decide later");
    expect(out).toContain("Go to Wrap up");
  });
  it("offers Decide later only on a decision, and never on Quick check", () => {
    expect(labels({ ...base, isDecision: false })).not.toContain("Decide later");
    expect(labels({ ...base, screen: "quickCheck", isDecision: true })).not.toContain("Decide later");
  });
  it("offers Show structure only on One at a time with a part", () => {
    expect(labels({ ...base, structureAvailable: false })).not.toContain("Show structure");
    expect(labels({ ...base, screen: "allFiles" })).not.toContain("Show structure");
  });
  it("labels the view switch for where you are", () => {
    expect(labels({ ...base, screen: "allFiles", view: "all" })).toContain("Switch to One at a time");
    expect(labels({ ...base, screen: "quickCheck" })).toContain("Switch to All files");
  });
  it("has no Ask when AI is off (R6)", () => {
    expect(labels({ ...base, aiAsk: false }).some((label) => label.startsWith("Ask"))).toBe(false);
  });
  it("has no row for a function the screen did not give it", () => {
    const { copyLink: _copy, ...rest } = everything();
    expect(labels({ ...base, actions: rest })).not.toContain("Copy link to this part");
  });
});

describe("contextActions: other screens", () => {
  it("Overview offers Start review, the view switch and Wrap up, but no answers or Copy link", () => {
    expect(labels({ screen: "overview", aiAsk: false, actions: everything() })).toEqual(["Start review", "Switch to All files", "Go to Wrap up"]);
  });
  it("Wrap up offers the button's label and Change verdict, and nothing once recorded", () => {
    expect(labels({ screen: "wrap", sendLabel: "Send 2 notes to Claude Code", actions: everything() })).toEqual(["Send 2 notes to Claude Code", "Change verdict…"]);
    expect(labels({ screen: "wrap", actions: everything() })[0]).toBe("Record your verdict");
    expect(labels({ screen: "wrap", recorded: true, actions: everything() })).toEqual([]);
  });
  it("Done follows the Enter rule: Revision 2 when ready and undo is closed, else the next review", () => {
    expect(labels({ screen: "done", revision2Ready: true, revision2Label: "Review Revision 2", hasNextReview: true, actions: everything() })).toEqual(["Review Revision 2"]);
    expect(labels({ screen: "done", revision2Ready: true, undoLive: true, hasNextReview: true, actions: everything() })).toEqual(["Start next review", "Undo the send"]);
    expect(labels({ screen: "done", hasNextReview: false, actions: everything() })).toEqual([]);
    expect(labels({ screen: "done", revision2Ready: true, undoLive: true, hasNextReview: false, actions: everything() })).toEqual(["Review Revision 2", "Undo the send"]);
  });
  it("Welcome back answers the first open change and has no view switch or Wrap up", () => {
    expect(labels({ screen: "welcomeBack", part: "apply_discount", canAnswer: true, aiAsk: true, actions: everything() })).toEqual(["Looks good", "Still needs a change", "Ask about this part"]);
  });
  it("Map and Preview have no context row", () => {
    expect(labels({ screen: "other", actions: everything() })).toEqual([]);
  });
});

describe("Settings", () => {
  it("offers a jump to each section and nothing about parts", () => {
    const run = vi.fn();
    const sections = [{ id: "keyboard", label: "Keyboard", run }, { id: "policy", label: "Review policy", run }];
    expect(labels({ screen: "settings", sections, actions: everything() })).toEqual(["Go to Keyboard", "Go to Review policy"]);
    expect(contextActions({ screen: "settings", sections, actions: {} })[0].group).toBe("On Settings");
  });
  it("the Settings rows open the real page at the section's id", () => {
    const go = vi.fn();
    const items = allItems({ screen: "one", actions: {} }, { reviews: [], repos: [], recent: [], parts: [] }, { go, openRepo: vi.fn() }, false);
    items.filter((item) => item.group === "Settings").forEach((item) => item.run());
    expect(go.mock.calls.map((call) => call[0])).toEqual([
      "/settings/review#reading", "/settings/review#reading", "/settings/review#notifications", "/settings/review#keyboard", "/settings/review#look", "/settings/review#policy",
    ]);
  });
});

describe("Settings rows on a phone", () => {
  it("leaves out Keyboard, which a phone has no use for", () => {
    const data = { reviews: [], repos: [], recent: [], parts: [] };
    const names = (phone: boolean) => allItems({ screen: "one", actions: {} }, data, { go: vi.fn(), openRepo: vi.fn(), phone }, false).filter((item) => item.group === "Settings").map((item) => item.label);
    expect(names(false)).toContain("Settings: Keyboard");
    expect(names(true)).not.toContain("Settings: Keyboard");
    expect(names(true)).toHaveLength(5);
  });
});

describe("placeholder reviews (R7)", () => {
  const data: PaletteData = {
    reviews: [{ id: "a", label: "payments · Refund", sub: "Your turn", path: "/r/a", current: true }, { id: "b", label: "shop · Cart", sub: "Your turn", path: "/r/b", current: false }],
    repos: [{ name: "shop", waiting: 1 }],
    recent: [{ label: "Home", sub: "Reviews", path: "/reviews" }],
    parts: [{ id: "p", name: "apply_discount", file: "app/orders.py", state: "Not answered", path: "/r/a/one/apply_discount" }],
    };
  const nav = { go: vi.fn(), openRepo: vi.fn(), openShortcuts: vi.fn() };
  it("offers only Snooze, Go Home, other reviews and Settings", () => {
    const ctx: PaletteContext = { screen: "overview", placeholder: true, part: "ignored", canAnswer: true, aiAsk: true, actions: everything() };
    const items = allItems(ctx, data, nav, false);
    const groups = [...new Set(items.map((item) => item.group))];
    expect(groups).toEqual(["This review", "Reviews", "Settings"]);
    expect(items.filter((item) => item.group === "This review").map((item) => item.label)).toEqual(["Snooze this review", "Go Home"]);
    expect(items.filter((item) => item.group === "Reviews").map((item) => item.label)).toEqual(["shop · Cart"]);
    const text = items.map((item) => item.label).join("|");
    expect(text).not.toMatch(/Wrap up|Copy link|apply_discount|Looks good|Keyboard shortcuts/);
  });
  it("drops Snooze when the review cannot be snoozed", () => {
    const { snoozeReview: _snooze, ...rest } = everything();
    expect(labels({ screen: "overview", placeholder: true, actions: rest })).toEqual(["Go Home"]);
  });
});

describe("allItems for a real review", () => {
  const data: PaletteData = {
    reviews: [{ id: "a", label: "shop · Fix discount", sub: "Your turn", path: "/r/a", current: true }],
    repos: [{ name: "shop", waiting: 2 }],
    recent: [{ label: "Home", sub: "Reviews", path: "/reviews" }],
    parts: [{ id: "p", name: "apply_discount", file: "app/orders.py", state: "Not answered", path: "/r/a/one/apply_discount" }],
  };
  const ctx: PaletteContext = { screen: "one", part: "apply_discount", canAnswer: true, actions: everything() };
  it("orders the groups as SPEC 4.12 does", () => {
    const items = allItems(ctx, data, { go: vi.fn(), openRepo: vi.fn(), openShortcuts: vi.fn() }, true);
    expect([...new Set(items.map((item) => item.group))]).toEqual(["This part · apply_discount", "Recent", "Parts in this review", "Reviews", "Repos", "Settings", "Help"]);
    expect(items.find((item) => item.group === "Repos")?.sub).toBe("2 waiting");
    expect(items.find((item) => item.group === "Parts in this review")?.sub).toBe("app/orders.py · Not answered");
  });
  it("lists the six Settings rows of SPEC 4.12", () => {
    const items = allItems(ctx, data, { go: vi.fn(), openRepo: vi.fn() }, false);
    expect(items.filter((item) => item.group === "Settings").map((item) => item.label)).toEqual([
      "Settings: Default view", "Settings: Skip the Overview", "Settings: Notifications", "Settings: Keyboard", "Settings: Look", "Review policy: Always a decision",
    ]);
    expect(items.some((item) => item.group === "Help")).toBe(false);
  });
});

describe("partStateLabel (R5)", () => {
  it("never reads Looks good for an unanswered or changed part", () => {
    expect(partStateLabel("unreviewed")).toBe("Not answered");
    expect(partStateLabel("unknown")).toBe("Not answered");
    expect(partStateLabel("changed_since_review")).toBe("Not answered");
    expect(partStateLabel("reviewed", true)).toBe("Not answered");
    expect(partStateLabel("reviewed")).toBe("Looks good");
    expect(partStateLabel("needs_changes")).toBe("Needs a change");
    expect(partStateLabel("later")).toBe("Left for later");
  });
});

describe("fuzzy", () => {
  it("matches characters in order and returns their positions", () => {
    expect(fuzzy("apply", "apply_discount")?.idx).toEqual([0, 1, 2, 3, 4]);
    expect(fuzzy("adc", "apply_discount")?.idx).toEqual([0, 6, 9]);
  });
  it("is null when a character is missing, and matches everything for an empty query", () => {
    expect(fuzzy("xyzq", "apply_discount")).toBeNull();
    expect(fuzzy("", "anything")).toEqual({ score: 0, idx: [] });
  });
  it("scores a run and a word start above scattered letters", () => {
    expect((fuzzy("disc", "apply_discount")?.score ?? 0)).toBeGreaterThan(fuzzy("disc", "dxixsxcx")?.score ?? 0);
  });
});

describe("shownGroups", () => {
  const item = (group: string, label: string, sub?: string): PaletteItem => ({ id: `${group}:${label}`, group, label, sub, run: () => undefined });
  it("caps each group at six while typing and ranks the best group first", () => {
    const items = [
      ...Array.from({ length: 9 }, (_unused, index) => item("Reviews", `shop · Review ${index}`)),
      item("Settings", "Settings: Review policy"),
    ];
    const groups = shownGroups(items, "review", "In this review");
    expect(groups.find((group) => group.name === "Reviews")?.items).toHaveLength(6);
  });
  it("with no query shows the context group whole, Recent at five, and a short head of the rest", () => {
    const items = [
      ...Array.from({ length: 6 }, (_unused, index) => item("This part · x", `Action ${index}`)),
      ...Array.from({ length: 8 }, (_unused, index) => item("Recent", `Place ${index}`)),
      ...Array.from({ length: 8 }, (_unused, index) => item("Reviews", `Review ${index}`)),
    ];
    const groups = shownGroups(items, "", "This part · x");
    expect(groups.map((group) => [group.name, group.items.length])).toEqual([["This part · x", 6], ["Recent", 5], ["Reviews", 4]]);
  });
  it("finds a row by its file or state when the label does not match, weaker than a label match", () => {
    const groups = shownGroups([item("Parts in this review", "apply_discount", "app/orders.py · Not answered"), item("Reviews", "orders cleanup")], "orders", "In this review");
    expect(groups.flatMap((group) => group.items.map((entry) => entry.label))).toEqual(["orders cleanup", "apply_discount"]);
  });
  it("is empty for a query nothing matches", () => {
    expect(shownGroups([item("Reviews", "shop")], "zzzz", "In this review")).toEqual([]);
  });
});

describe("copy and recent", () => {
  it("states the empty result and the count exactly", () => {
    expect(emptyText("xyz")).toBe("No match for 'xyz'. Try a function name, a repo, or an action.");
    expect(resultsAnnouncement("xyz", 0)).toBe("No match for xyz");
    expect(resultsAnnouncement("a", 1)).toBe("1 result");
    expect(resultsAnnouncement("a", 4)).toBe("4 results");
    expect(resultsAnnouncement("", 4)).toBe("");
  });
  it("keeps the last five distinct places, newest first", () => {
    let list = [] as ReturnType<typeof pushRecent>;
    for (const n of [1, 2, 3, 4, 5, 6, 3]) list = pushRecent(list, { label: `P${n}`, sub: "", path: `/p${n}` });
    expect(list.map((place) => place.label)).toEqual(["P3", "P6", "P5", "P4", "P2"]);
  });
});
