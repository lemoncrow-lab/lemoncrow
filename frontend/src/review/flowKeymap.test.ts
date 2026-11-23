// SPEC section 3.5 as unit tests: one block per row of the table, then the rules in its first paragraph.
import { describe, expect, it } from "vitest";

import {
  allShortcuts,
  doneEnterTarget,
  DEFAULT_BINDINGS,
  classifyFocus,
  flowAction,
  focusLegend,
  homeLegend,
  keyHint,
  rebindKey,
  screenLegend,
  type FlowIntent,
  type FlowKeyContext,
  type FlowKeyEvent,
  type FlowScreen,
} from "./flowKeymap";

const press = (k: string, mods: Partial<FlowKeyEvent> = {}): FlowKeyEvent => ({ key: k, ...mods });
const intentOf = (e: FlowKeyEvent, ctx: FlowKeyContext): FlowIntent | null => flowAction(e, ctx)?.intent ?? null;
const at = (screen: FlowScreen, extra: Partial<FlowKeyContext> = {}): FlowKeyContext => ({ screen, focus: "other", ...extra });
const PART_SCREENS: FlowScreen[] = ["one", "quickCheck", "allFiles"];

describe("Keyboard shortcuts dialog: the full list comes from the key map", () => {
  it("groups every binding and follows a rebinding", () => {
    const groups = allShortcuts();
    expect(groups.map((group) => group.title)).toEqual(["Answer a part", "Move", "Anywhere", "On a review", "On Home"]);
    const keys = groups.flatMap((group) => group.keys.map((row) => row.key.toLowerCase()));
    for (const key of ["j", "k", "g", "w", "?"]) expect(keys).toContain(key);
    expect(groups.flatMap((group) => group.keys).every((row) => row.does)).toBe(true);
    const moved = rebindKey(DEFAULT_BINDINGS, "looksGood", "y");
    if (!moved.ok) throw new Error(moved.reason);
    expect(allShortcuts(moved.bindings)[0].keys.map((row) => row.key)).toContain("Y");
  });
});

describe("3.5 row: ? anywhere opens Keyboard shortcuts", () => {
  it.each<FlowScreen>(["home", "one", "allFiles", "wrap", "done", "welcomeBack", "settings"])("on %s", (screen) => {
    expect(intentOf(press("?", { shiftKey: true }), at(screen))).toBe("help");
  });
});

describe("3.5 row: Ctrl/Cmd+K opens or closes the palette anywhere", () => {
  it.each<FlowScreen>(["home", "overview", "one", "quickCheck", "allFiles", "wrap", "done", "welcomeBack", "settings"])("on %s", (screen) => {
    expect(intentOf(press("k", { metaKey: true }), at(screen))).toBe("palette");
    expect(intentOf(press("K", { ctrlKey: true }), at(screen))).toBe("palette");
  });
  it("closes the open palette and nothing else may be open", () => {
    expect(intentOf(press("k", { metaKey: true }), at("home", { surface: "palette" }))).toBe("palette");
    expect(intentOf(press("k", { metaKey: true }), at("home", { surface: "shortcuts" }))).toBeNull();
  });
});

describe("3.5 row: / on Home focuses search", () => {
  it("Home focuses its search; a review screen opens the ⌘K palette", () => {
    expect(intentOf(press("/"), at("home"))).toBe("searchFocus");
    for (const screen of ["overview", "one", "quickCheck", "allFiles", "wrap", "done", "welcomeBack", "other"] as const) {
      expect(intentOf(press("/"), at(screen)), screen).toBe("palette");
    }
  });
  it("/ opens nothing while typing, while a surface is open, or off a review", () => {
    expect(intentOf(press("/"), at("one", { focus: "field" }))).toBeNull();
    expect(intentOf(press("/"), at("one", { surface: "palette" }))).toBeNull();
    expect(intentOf(press("/"), at("overview", { surface: "shortcuts" }))).toBeNull();
    expect(intentOf(press("/"), at("settings"))).toBeNull();
    expect(intentOf(press("/"), at("org"))).toBeNull();
  });
});

describe("Keyboard shortcuts dialog: every key is listed where it works", () => {
  const group = (title: string) => allShortcuts().find((item) => item.title === title)!;
  const keyOf = (label: string) => (label.length === 1 ? label.toLowerCase() : label);
  it("lists neither / nor X under Anywhere", () => {
    const anywhere = group("Anywhere").keys.map((row) => row.key);
    expect(anywhere).not.toContain("/");
    expect(anywhere).not.toContain("X");
  });
  it("every On a review key fires on every review screen", () => {
    for (const row of group("On a review").keys) {
      for (const screen of ["overview", "one", "quickCheck", "allFiles", "wrap", "done", "welcomeBack", "other"] as const) {
        expect(intentOf(press(keyOf(row.key)), at(screen)), `${row.key} on ${screen}`).not.toBeNull();
      }
    }
  });
  it("every On Home key fires on Home, and X does nothing on a review", () => {
    const keys = group("On Home").keys.map((row) => keyOf(row.key));
    expect(keys).toEqual(["/", "x"]);
    for (const key of keys) expect(intentOf(press(key), at("home")), key).not.toBeNull();
    expect(intentOf(press("x"), at("one"))).toBeNull();
  });
});

describe("3.5 rows: J / K move the Home selection", () => {
  it("J and K on Home", () => {
    expect(intentOf(press("j"), at("home"))).toBe("rowNext");
    expect(intentOf(press("K"), at("home"))).toBe("rowPrevious");
  });
  it("Shift+J and Shift+K extend the selection on the dense list only", () => {
    expect(flowAction(press("J", { shiftKey: true }), at("home", { dense: true }))).toEqual({ intent: "rowNext", extend: true });
    expect(intentOf(press("K", { shiftKey: true }), at("home", { dense: false }))).toBeNull();
  });
});

describe("3.5 row: X on Home selects or unselects the row (the legend lists it for every list, SPEC 7.1 P5-1 F)", () => {
  it("works on the short list and the dense one, never on first run", () => {
    expect(intentOf(press("x"), at("home", { dense: true }))).toBe("rowToggle");
    expect(intentOf(press("x"), at("home", { dense: false }))).toBe("rowToggle");
    expect(intentOf(press("x"), at("home", { firstRun: true }))).toBeNull();
  });
});

describe("3.5 rows: Enter and S on Home", () => {
  it("Enter opens the selected row; S snoozes", () => {
    expect(intentOf(press("Enter"), at("home"))).toBe("rowOpen");
    expect(intentOf(press("s"), at("home"))).toBe("snooze");
  });
  it("Enter on a focused row button belongs to the button", () => {
    expect(intentOf(press("Enter"), at("home", { focus: "button" }))).toBeNull();
  });
});

describe("3.5 rows: 1 to 4 pick a snooze option; Esc clears the Home selection", () => {
  it.each([
    ["1", 1],
    ["2", 2],
    ["3", 3],
    ["4", 4],
  ])("%s in the picker picks option %i", (k, option) => {
    expect(flowAction(press(k), at("home", { surface: "snooze" }))).toEqual({ intent: "snoozePick", option });
  });
  it("5 and digits outside the picker do nothing", () => {
    expect(intentOf(press("5"), at("home", { surface: "snooze" }))).toBeNull();
    expect(intentOf(press("1"), at("home"))).toBeNull();
  });
  it("Esc clears the selection on Home only with a selection", () => {
    expect(intentOf(press("Escape"), at("home", { dense: true, hasSelection: true }))).toBe("clearSelection");
    expect(intentOf(press("Escape"), at("home", { dense: false, hasSelection: true }))).toBe("clearSelection");
    expect(intentOf(press("Escape"), at("home", { dense: true, hasSelection: false }))).toBeNull();
  });
});

describe("3.5 row: Enter on Overview starts the review", () => {
  it("start", () => {
    expect(intentOf(press("Enter"), at("overview"))).toBe("start");
  });
});

describe("3.5 rows: A and C on One at a time, Quick check, All files (and Welcome back)", () => {
  it.each(PART_SCREENS)("A and C on %s", (screen) => {
    expect(intentOf(press("a"), at(screen))).toBe("looksGood");
    expect(intentOf(press("C"), at(screen))).toBe("needsChange");
  });
  it("A keeps and moves on when the part already reads Looks good", () => {
    expect(flowAction(press("a"), at("one", { answeredGood: true }))).toEqual({ intent: "looksGood", moveOn: true });
  });
  it("A and C on Welcome back; not on Home", () => {
    expect(intentOf(press("a"), at("welcomeBack"))).toBe("looksGood");
    expect(intentOf(press("c"), at("welcomeBack"))).toBe("needsChange");
    expect(intentOf(press("a"), at("home"))).toBeNull();
  });
});

describe("3.5 row: N decides later on decision parts in One at a time and All files", () => {
  it("decision parts only", () => {
    expect(intentOf(press("n"), at("one", { isDecision: true }))).toBe("decideLater");
    expect(intentOf(press("n"), at("allFiles", { isDecision: true }))).toBe("decideLater");
    expect(intentOf(press("n"), at("one", { isDecision: false }))).toBeNull();
    expect(intentOf(press("n"), at("quickCheck", { isDecision: true }))).toBeNull();
  });
});

describe("3.5 row: Q asks, only when AI Ask is available", () => {
  it.each<FlowScreen>(["overview", "one", "quickCheck", "allFiles", "welcomeBack"])("on %s with Ask on", (screen) => {
    expect(intentOf(press("q"), at(screen, { aiAsk: true }))).toBe("ask");
  });
  it("nothing with Ask off, or on Home", () => {
    expect(intentOf(press("q"), at("one", { aiAsk: false }))).toBeNull();
    expect(intentOf(press("q"), at("home", { aiAsk: true }))).toBeNull();
  });
});

describe("3.5 row: R runs the test only where one exists and has not run", () => {
  it.each(PART_SCREENS)("on %s", (screen) => {
    expect(intentOf(press("r"), at(screen, { testAvailable: true }))).toBe("runTest");
    expect(intentOf(press("r"), at(screen, { testAvailable: false }))).toBeNull();
  });
});

describe("3.5 rows: E and S on One at a time", () => {
  it("E shows the whole change; S toggles Structure", () => {
    expect(intentOf(press("e"), at("one"))).toBe("showWhole");
    expect(intentOf(press("s"), at("one"))).toBe("structure");
  });
  it("E is One at a time only", () => {
    expect(intentOf(press("e"), at("allFiles"))).toBeNull();
  });
});

describe("3.5 row: U goes to the next unseen risky part", () => {
  it.each(PART_SCREENS)("on %s", (screen) => {
    expect(intentOf(press("u"), at(screen))).toBe("nextUnseenRisky");
  });
});

describe("3.5 rows: W wraps up and G switches view", () => {
  const WITH_W_AND_G: FlowScreen[] = ["overview", "one", "quickCheck", "allFiles"];
  it.each(WITH_W_AND_G)("W and G on %s", (screen) => {
    expect(intentOf(press("w"), at(screen))).toBe("wrapUp");
    expect(intentOf(press("g"), at(screen))).toBe("switchView");
  });
  it("W does nothing on Welcome back (Q13)", () => {
    expect(intentOf(press("w"), at("welcomeBack"))).toBeNull();
  });
});

describe("3.5 rows: J and K on One at a time", () => {
  it("next and previous part", () => {
    expect(intentOf(press("j"), at("one"))).toBe("nextPart");
    expect(intentOf(press("k"), at("one"))).toBe("previousPart");
  });
  it("J on an answered part keeps it and moves on", () => {
    expect(flowAction(press("j"), at("one", { answeredGood: true }))).toEqual({ intent: "nextPart", moveOn: true });
  });
  it("on the interstitial K goes back and J does nothing", () => {
    expect(intentOf(press("k"), at("one", { interstitial: true }))).toBe("previousPart");
    expect(intentOf(press("j"), at("one", { interstitial: true }))).toBeNull();
  });
});

describe("3.5 rows: Enter on One at a time and Quick check", () => {
  it("keeps the answer while the pause strip shows; on the interstitial goes to Quick check", () => {
    expect(intentOf(press("Enter"), at("one", { pauseStrip: true }))).toBe("keepAnswer");
    expect(intentOf(press("Enter"), at("one", { interstitial: true }))).toBe("goQuickCheck");
    expect(intentOf(press("Enter"), at("one"))).toBeNull();
  });
  it("Quick check goes to Wrap up only once the part is answered", () => {
    expect(intentOf(press("Enter"), at("quickCheck", { answered: true }))).toBe("goWrapUp");
    expect(intentOf(press("Enter"), at("quickCheck", { answered: false }))).toBeNull();
  });
});

describe("3.5 rows: J, K, ] and [ on All files", () => {
  it("part and file movement", () => {
    expect(intentOf(press("j"), at("allFiles"))).toBe("nextPart");
    expect(intentOf(press("k"), at("allFiles"))).toBe("previousPart");
    expect(intentOf(press("]"), at("allFiles"))).toBe("nextFile");
    expect(intentOf(press("["), at("allFiles"))).toBe("previousFile");
  });
});

describe("3.5 rows: note composer and Wrap up chords", () => {
  it("Cmd/Ctrl+Enter saves the note even while typing in the composer", () => {
    const composer = at("one", { surface: "composer", focus: "field" });
    expect(intentOf(press("Enter", { metaKey: true }), composer)).toBe("saveNote");
    expect(intentOf(press("Enter", { ctrlKey: true }), composer)).toBe("saveNote");
  });
  it("Esc closes the composer and keeps the draft", () => {
    expect(flowAction(press("Escape"), at("one", { surface: "composer", focus: "field" }))).toEqual({ intent: "closeSurface", surface: "composer" });
  });
  it("Cmd/Ctrl+Enter sends or records on Wrap up; plain Enter does nothing there", () => {
    expect(intentOf(press("Enter", { metaKey: true }), at("wrap"))).toBe("sendOrRecord");
    expect(intentOf(press("Enter"), at("wrap"))).toBeNull();
  });
  it("Enter opens and Shift+Enter wraps up anyway in the coverage dialog", () => {
    expect(intentOf(press("Enter"), at("allFiles", { surface: "coverage" }))).toBe("coverageOpen");
    expect(intentOf(press("Enter", { shiftKey: true }), at("allFiles", { surface: "coverage" }))).toBe("coverageWrapAnyway");
  });
});

describe("3.5 rows: Enter on Done, Z to undo", () => {
  it("Review Revision 2 when ready and undo is not live; otherwise Start next review", () => {
    expect(intentOf(press("Enter"), at("done", { revision2Ready: true, undoLive: false }))).toBe("reviewRevision2");
    expect(intentOf(press("Enter"), at("done", { revision2Ready: true, undoLive: true }))).toBe("startNextReview");
    expect(intentOf(press("Enter"), at("done", { revision2Ready: false }))).toBe("startNextReview");
  });
  it("Z undoes on Done and on any screen while the undo window is live", () => {
    expect(intentOf(press("z"), at("done", { undoLive: true }))).toBe("undo");
    expect(intentOf(press("z"), at("home", { undoLive: true }))).toBe("undo");
    expect(intentOf(press("z"), at("one", { undoLive: false }))).toBeNull();
  });
});

describe("3.5 row: Cmd/Ctrl+Shift+C copies the link on any review screen", () => {
  it("review screens only", () => {
    expect(intentOf(press("C", { metaKey: true, shiftKey: true }), at("one"))).toBe("copyLink");
    expect(intentOf(press("c", { ctrlKey: true, shiftKey: true }), at("wrap"))).toBe("copyLink");
    expect(intentOf(press("c", { metaKey: true, shiftKey: true }), at("home"))).toBeNull();
  });
});

describe("3.5 row: Esc on the Organization page goes back to Settings", () => {
  it("backToSettings", () => {
    expect(intentOf(press("Escape"), at("org"))).toBe("backToSettings");
  });
});

describe("3.5 rules: typing, modifiers, surfaces, controls", () => {
  it("plain keys never fire while typing in a field", () => {
    const typing = at("one", { focus: "field" });
    expect(intentOf(press("a"), typing)).toBeNull();
    expect(intentOf(press("j"), typing)).toBeNull();
    expect(intentOf(press("?", { shiftKey: true }), typing)).toBeNull();
  });
  it("classifyFocus tells a field from a button, a link and anything else", () => {
    expect(classifyFocus({ tagName: "INPUT" })).toBe("field");
    expect(classifyFocus({ tagName: "textarea" })).toBe("field");
    expect(classifyFocus({ tagName: "select" })).toBe("field");
    expect(classifyFocus({ tagName: "div", isContentEditable: true })).toBe("field");
    expect(classifyFocus({ tagName: "button" })).toBe("button");
    expect(classifyFocus({ tagName: "a" })).toBe("link");
    expect(classifyFocus({ tagName: "section" })).toBe("other");
  });
  it("Enter on a focused button or link goes to the control", () => {
    expect(intentOf(press("Enter"), at("overview", { focus: "button" }))).toBeNull();
    expect(intentOf(press("Enter"), at("done", { focus: "link" }))).toBeNull();
    expect(intentOf(press("Enter"), at("overview", { focus: "other" }))).toBe("start");
  });
  it("a modifier blocks a plain key; Alt blocks everything", () => {
    expect(intentOf(press("a", { metaKey: true }), at("one"))).toBeNull();
    expect(intentOf(press("a", { shiftKey: true }), at("one"))).toBeNull();
    expect(intentOf(press("a", { altKey: true }), at("one"))).toBeNull();
    expect(intentOf(press("k", { altKey: true, metaKey: true }), at("one"))).toBeNull();
  });
  it("an open dialog, sheet, menu or palette keeps its own keys only", () => {
    expect(intentOf(press("a"), at("one", { surface: "other" }))).toBeNull();
    expect(intentOf(press("j"), at("one", { surface: "shortcuts" }))).toBeNull();
    expect(intentOf(press("?", { shiftKey: true }), at("one", { surface: "other" }))).toBeNull();
    expect(flowAction(press("Escape"), at("one", { surface: "other" }))).toEqual({ intent: "closeSurface", surface: "other" });
  });
  it("Esc in Home search clears the query", () => {
    expect(intentOf(press("Escape"), at("home", { focus: "field", searchFocused: true }))).toBe("clearSearch");
  });
});

describe("3.5 review: the coverage dialog keeps its keys with a focused button", () => {
  it("Enter and Shift+Enter map even when the focused element is a button or link", () => {
    expect(intentOf(press("Enter"), at("allFiles", { surface: "coverage", focus: "button" }))).toBe("coverageOpen");
    expect(intentOf(press("Enter", { shiftKey: true }), at("allFiles", { surface: "coverage", focus: "link" }))).toBe("coverageWrapAnyway");
  });
  it("with no surface open, a focused button still keeps Enter", () => {
    expect(intentOf(press("Enter"), at("allFiles", { focus: "button" }))).toBeNull();
  });
});

describe("3.5 moveOn: only A and J keep the part and move on", () => {
  it("C and N never set moveOn, even on an answered-good part", () => {
    expect(flowAction(press("c"), at("one", { answeredGood: true }))?.moveOn).toBeUndefined();
    expect(flowAction(press("n"), at("one", { answeredGood: true, isDecision: true }))?.moveOn).toBeUndefined();
  });
  it("A and J set it", () => {
    expect(flowAction(press("a"), at("one", { answeredGood: true }))?.moveOn).toBe(true);
    expect(flowAction(press("j"), at("one", { answeredGood: true }))?.moveOn).toBe(true);
  });
});

describe("3.5 Shift blocks letters only", () => {
  it("a symbol typed with Shift reaches a rebound binding", () => {
    const r = rebindKey(DEFAULT_BINDINGS, "looksGood", "!");
    if (!r.ok) throw new Error("rebind failed");
    expect(intentOf({ key: "!", shiftKey: true }, at("one", { bindings: r.bindings }))).toBe("looksGood");
  });
  it("/ typed with Shift still focuses search on Home", () => {
    expect(intentOf({ key: "/", shiftKey: true }, at("home"))).toBe("searchFocus");
  });
  it("Shift+Enter outside the coverage dialog does nothing", () => {
    expect(intentOf(press("Enter", { shiftKey: true }), at("overview"))).toBeNull();
  });
  it("a capital typed with Shift cannot be a binding; the default keeps working after the refusal", () => {
    expect(rebindKey(DEFAULT_BINDINGS, "looksGood", "B", true)).toEqual({ ok: false, reason: "Press the letter without Shift." });
    expect(intentOf(press("a"), at("one", { bindings: DEFAULT_BINDINGS }))).toBe("looksGood");
  });
  it("a capital typed without Shift (Caps Lock) is the same letter, lower-cased", () => {
    expect(rebindKey(DEFAULT_BINDINGS, "looksGood", "B")).toEqual({ ok: true, bindings: { ...DEFAULT_BINDINGS, looksGood: "b" } });
  });
});

describe("3.5 minors", () => {
  it("the interstitial: the part keys do nothing; K goes back, Enter goes to Quick check", () => {
    const inter = { interstitial: true, isDecision: true, testAvailable: true };
    for (const k of ["a", "c", "n", "s", "e", "r", "j"]) expect(intentOf(press(k), at("one", inter))).toBeNull();
    expect(intentOf(press("k"), at("one", inter))).toBe("previousPart");
    expect(intentOf(press("Enter"), at("one", inter))).toBe("goQuickCheck");
  });
  it("the interstitial still takes G, W, U, Q and Z", () => {
    const inter = { interstitial: true, aiAsk: true, undoLive: true };
    expect(intentOf(press("g"), at("one", inter))).toBe("switchView");
    expect(intentOf(press("w"), at("one", inter))).toBe("wrapUp");
    expect(intentOf(press("u"), at("one", inter))).toBe("nextUnseenRisky");
    expect(intentOf(press("q"), at("one", inter))).toBe("ask");
    expect(intentOf(press("z"), at("one", inter))).toBe("undo");
  });
  it("classifyFocus counts summary and role=button / role=link as controls", () => {
    expect(classifyFocus({ tagName: "summary" })).toBe("button");
    expect(classifyFocus({ tagName: "div", role: "button" })).toBe("button");
    expect(classifyFocus({ tagName: "span", role: "link" })).toBe("link");
    expect(classifyFocus({ tagName: "span", role: null })).toBe("other");
  });
  it("classifyFocus treats the interactive ARIA roles as controls", () => {
    for (const role of ["tab", "menuitem", "option", "checkbox", "switch", "radio", "combobox"]) {
      expect(classifyFocus({ tagName: "div", role }), role).toBe("button");
    }
  });
  it("first-run Home: Shift+J/K, / and Esc clear do nothing", () => {
    const first = at("home", { firstRun: true, dense: true, hasSelection: true });
    expect(flowAction(press("J", { shiftKey: true }), first)).toBeNull();
    expect(flowAction(press("K", { shiftKey: true }), first)).toBeNull();
    expect(intentOf(press("/"), first)).toBeNull();
    expect(intentOf(press("Escape"), first)).toBeNull();
  });
  it("first-run Home: Enter starts; J, K, S and X do nothing", () => {
    const first = at("home", { firstRun: true, dense: true, hasSelection: true });
    expect(intentOf(press("Enter"), first)).toBe("start");
    for (const k of ["j", "k", "s", "x"]) expect(intentOf(press(k), first)).toBeNull();
  });
});

describe("3.5 rebinding: Looks good, Needs a change, Decide later", () => {
  const bound = rebindKey(DEFAULT_BINDINGS, "looksGood", "b");
  it("accepts a free lowercase letter", () => {
    expect(bound.ok).toBe(true);
    if (!bound.ok) return;
    expect(bound.bindings.looksGood).toBe("b");
  });
  it("the rebound key replaces the default everywhere", () => {
    if (!bound.ok) throw new Error("rebind failed");
    const ctx = at("one", { bindings: bound.bindings });
    expect(intentOf(press("b"), ctx)).toBe("looksGood");
    expect(intentOf(press("a"), ctx)).toBeNull();
    expect(intentOf(press("b"), at("allFiles", { bindings: bound.bindings }))).toBe("looksGood");
    expect(intentOf(press("a"), at("welcomeBack", { bindings: bound.bindings }))).toBeNull();
  });
  it("hints and the Focus legend show the rebound key", () => {
    if (!bound.ok) throw new Error("rebind failed");
    expect(keyHint("looksGood", bound.bindings)).toBe("B");
    expect(focusLegend({ aiAsk: true, bindings: bound.bindings })).toBe("B good · C change · N decide later · Q ask · S structure · ? all shortcuts");
  });
  it("the Focus legend drops Q ask when Ask is off", () => {
    expect(focusLegend({ aiAsk: false })).toBe("A good · C change · N decide later · S structure · ? all shortcuts");
  });
  it("Home legend and first-run legend", () => {
    expect(homeLegend(false)).toBe("J/K move · Enter open · X select · S snooze · ⌘K search · ? all shortcuts");
    expect(homeLegend(true)).toBe("Enter start · ? all shortcuts");
  });
  it("refuses reserved keys, a second key, and another intent's key", () => {
    expect(rebindKey(DEFAULT_BINDINGS, "needsChange", "j").ok).toBe(false);
    expect(rebindKey(DEFAULT_BINDINGS, "needsChange", "ab").ok).toBe(false);
    expect(rebindKey(DEFAULT_BINDINGS, "needsChange", "c").ok).toBe(true);
    const clash = rebindKey(DEFAULT_BINDINGS, "needsChange", "n");
    expect(clash).toEqual({ ok: false, reason: "N is already used for Decide later. Pick another key." });
  });
  it("Decide later can be rebound too", () => {
    const r = rebindKey(DEFAULT_BINDINGS, "decideLater", "m");
    if (!r.ok) throw new Error("rebind failed");
    expect(intentOf(press("m"), at("one", { isDecision: true, bindings: r.bindings }))).toBe("decideLater");
    expect(intentOf(press("n"), at("one", { isDecision: true, bindings: r.bindings }))).toBeNull();
  });
});

describe("3.3 Shortcuts legends name only keys that work", () => {
  it("Quick check lists Run test and Ask only when they work", () => {
    expect(screenLegend("quickCheck", { aiAsk: false })).not.toMatch(/Q ask|R run test/);
    expect(screenLegend("quickCheck", { aiAsk: true, testAvailable: true })).toMatch(/Q ask.*R run test/);
    expect(intentOf(press("g"), at("quickCheck"))).toBe("switchView");
  });
  it("All files lists decide later and J / K, and Done follows the undo window", () => {
    expect(screenLegend("allFiles", { aiAsk: false })).toContain("N decide later");
    expect(screenLegend("done", { aiAsk: false, undoLive: true })).toContain("Z undo the send");
    expect(screenLegend("done", { aiAsk: false, revision2Ready: true })).toContain("Enter review Revision 2");
    expect(screenLegend("done", { aiAsk: false })).toContain("Enter start next review");
  });

  it("Done's Enter rule: key, legend and chip agree in every state", () => {
    const table: [boolean, boolean, boolean, "revision" | "next" | null][] = [
      // revisionReady, undoLive, hasNext, target
      [false, false, true, "next"], [false, false, false, null], [false, true, true, "next"], [false, true, false, null],
      [true, false, true, "revision"], [true, false, false, "revision"], [true, true, true, "next"], [true, true, false, "revision"],
    ];
    for (const [revisionReady, undoLive, hasNext, target] of table) {
      expect(doneEnterTarget({ revisionReady, undoLive, hasNext })).toBe(target);
      const intent = flowAction({ key: "Enter" }, { screen: "done", revision2Ready: revisionReady, undoLive, hasNextReview: hasNext })?.intent ?? null;
      expect(intent).toBe(target === "revision" ? "reviewRevision2" : target === "next" ? "startNextReview" : null);
      const legend = screenLegend("done", { aiAsk: false, revision2Ready: revisionReady, undoLive, hasNext });
      expect(legend.includes("Enter review Revision 2")).toBe(target === "revision");
      expect(legend.includes("Enter start next review")).toBe(target === "next");
    }
  });
});

describe("Welcome back legend follows the rebound keys (D22, SET-3)", () => {
  it("names the bound keys, not A and C", () => {
    const legend = screenLegend("welcomeBack", { aiAsk: false, bindings: { looksGood: "y", needsChange: "t", decideLater: "n" } });
    expect(legend).toBe("Y all look fixed · T still needs a change · ? all shortcuts");
  });
});

describe("typing in the Ask box (a text field) fires no shortcut", () => {
  it.each(["q", "j", "k", "r", "x", "a", "c", "n", "s", "w", "[", "]", "/"])("%s", (key) => {
    for (const screen of ["one", "allFiles", "quickCheck", "welcomeBack"] as const) {
      expect(intentOf(press(key), at(screen, { focus: "field" })), `${key} on ${screen}`).toBeNull();
    }
  });
});
