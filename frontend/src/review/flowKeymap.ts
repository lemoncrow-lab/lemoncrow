/**
 * The keyboard map for the new Review flow (SPEC §3.5). Pure: a key event and the screen's context in, one
 * intent out (or null). Nothing here reads the DOM; a screen classifies its focus with `classifyFocus` and calls
 * `flowAction` from its single keydown listener. This is the only key map.
 *
 * Rules (§3.5 first paragraph):
 * - Plain keys never fire while focus is in an input, textarea, select or contenteditable.
 * - A modifier blocks the key unless it is one of the listed chords (⌘K, ⌘↵, ⌘⇧C, Shift+J/K on the dense Home list,
 *   Shift+Enter in the coverage dialog). Alt blocks everything. `?` is exempt from the Shift rule: it is typed with Shift.
 * - While a surface (composer, coverage dialog, snooze picker, palette, shortcuts, other dialog or menu) is open, only
 *   that surface's keys fire.
 * - Enter on a focused button or link belongs to the control.
 * - Looks good, Needs a change and Decide later are rebindable. A rebound key replaces the default everywhere,
 *   including `keyHint` and the footer legends.
 */

export type FlowScreen =
  | "home"
  | "overview"
  | "one"
  | "quickCheck"
  | "allFiles"
  | "wrap"
  | "done"
  | "welcomeBack"
  | "settings"
  | "org"
  | "other";

/** What owns the keyboard above the screen. `none` means the screen itself. */
export type FlowSurface = "none" | "composer" | "coverage" | "snooze" | "palette" | "shortcuts" | "other";

/** What the focused element is. `field` is typing; `button` and `link` keep Enter for the control. */
export type FocusKind = "field" | "button" | "link" | "other";

export type RebindableIntent = "looksGood" | "needsChange" | "decideLater";

export type KeyBindings = Readonly<Record<RebindableIntent, string>>;

export const DEFAULT_BINDINGS: KeyBindings = { looksGood: "a", needsChange: "c", decideLater: "n" };

/** Keys no rebinding may take: the fixed review, navigation and chord keys. Copied from the prototype's `RESERVED`. */
export const RESERVED_KEYS: readonly string[] = ["j", "k", "s", "w", "u", "g", "e", "q", "r", "x", "z", "?", "/", "[", "]", "enter", "escape", "tab", " "];

/** What each reserved key does, so a refusal names it (SPEC 4.11). Mirrors `RESERVED_ACTIONS` in user_settings.py. */
const RESERVED_ACTION: Readonly<Record<string, string>> = {
  j: "next part", k: "previous part", s: "structure", w: "wrap up", u: "next unseen risky part", g: "the other view", e: "show the whole part",
  q: "ask", r: "run test", x: "select", z: "undo", "?": "all shortcuts", "/": "search", "[": "previous file", "]": "next file",
};

const LABEL: Readonly<Record<RebindableIntent, string>> = { looksGood: "Looks good", needsChange: "Needs a change", decideLater: "Decide later" };

export interface FlowKeyEvent {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}

export interface FlowKeyContext {
  screen: FlowScreen;
  surface?: FlowSurface;
  focus?: FocusKind;
  bindings?: KeyBindings;
  /** The Home search input has focus; Esc clears it. */
  searchFocused?: boolean;
  /** AI Ask is available: Q and the Ask hints appear. */
  aiAsk?: boolean;
  /**
   * Why new AI cannot run right now ("AI budget is $0", "AI budget used up until Dec 1"), or unset. Q then keeps its
   * place in the legend and answers with this reason instead of starting Ask (SPEC 7.1 OWNER-1, OWNER-3).
   */
  aiBlocked?: string;
  /** The screen's test exists and has not run: R runs it. */
  testAvailable?: boolean;
  /** The undo window after Send is open: Z undoes it on any screen. */
  undoLive?: boolean;
  /** Done: Revision 2 is ready to review (Enter then means "Review Revision 2"). */
  revision2Ready?: boolean;
  /** Done: another review is waiting. Unset counts as yes. */
  hasNextReview?: boolean;
  /** The current part is a decision, so N applies. */
  isDecision?: boolean;
  /** The current part already reads Looks good: A and J keep it and move on. */
  answeredGood?: boolean;
  /** The current part has an answer. */
  answered?: boolean;
  /** One at a time: the pause strip is showing (Enter keeps the answer). */
  pauseStrip?: boolean;
  /** One at a time: the interstitial after the last part. */
  interstitial?: boolean;
  /** Home: the first run, before any review exists. Enter starts; J, K, S and X do nothing. */
  firstRun?: boolean;
  /** Home: the dense list is on (Shift+J/K extend). */
  dense?: boolean;
  /** Home: at least one row is selected (Esc clears). */
  hasSelection?: boolean;
}

export type FlowIntent =
  | "help"
  | "palette"
  | "searchFocus"
  | "rowNext"
  | "rowPrevious"
  | "rowToggle"
  | "rowOpen"
  | "snooze"
  | "snoozePick"
  | "clearSelection"
  | "clearSearch"
  | "closeSurface"
  | "backToSettings"
  | "start"
  | "looksGood"
  | "needsChange"
  | "decideLater"
  | "ask"
  | "aiUnavailable"
  | "runTest"
  | "showWhole"
  | "structure"
  | "nextUnseenRisky"
  | "wrapUp"
  | "switchView"
  | "nextPart"
  | "previousPart"
  | "nextFile"
  | "previousFile"
  | "keepAnswer"
  | "goQuickCheck"
  | "goWrapUp"
  | "saveNote"
  | "sendOrRecord"
  | "coverageOpen"
  | "coverageWrapAnyway"
  | "reviewRevision2"
  | "startNextReview"
  | "undo"
  | "copyLink";

export interface FlowAction {
  intent: FlowIntent;
  /** looksGood (A) and nextPart (J): the part is already Looks good, so keep it and move on. Never set for C or N. */
  moveOn?: boolean;
  /** rowNext / rowPrevious with Shift: extend the selection. */
  extend?: boolean;
  /** aiUnavailable: the reason to say (a toast). */
  reason?: string;
  /** snoozePick: 1 hour, This afternoon, Tomorrow 9:00, Next week. */
  option?: 1 | 2 | 3 | 4;
  /** closeSurface: which surface closes (the composer keeps its draft). */
  surface?: Exclude<FlowSurface, "none">;
}

const REVIEW_PART_SCREENS: readonly FlowScreen[] = ["one", "quickCheck", "allFiles"];
const REVIEW_SCREENS: readonly FlowScreen[] = ["overview", "one", "quickCheck", "allFiles", "wrap", "done", "welcomeBack"];

/** Interactive ARIA roles: Enter on one goes to the control, as on a button. */
const CONTROL_ROLES: ReadonlySet<string> = new Set(["button", "tab", "menuitem", "option", "checkbox", "switch", "radio", "combobox"]);

/** Classifies a focused element for `FlowKeyContext.focus`. `tagName` is lower- or upper-case. */
export function classifyFocus(el: { tagName: string; isContentEditable?: boolean; role?: string | null }): FocusKind {
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable) return "field";
  if (tag === "button" || tag === "summary" || (el.role !== undefined && el.role !== null && CONTROL_ROLES.has(el.role))) return "button";
  if (tag === "a" || el.role === "link") return "link";
  return "other";
}

function chord(lk: string, key: string, shift: boolean, ctx: FlowKeyContext, surface: FlowSurface): FlowAction | null {
  if (lk === "k" && !shift) {
    // ⌘K opens the palette and closes it again; no other surface may be open.
    return surface === "none" || surface === "palette" ? { intent: "palette" } : null;
  }
  if (surface !== "none") {
    return key === "Enter" && !shift && surface === "composer" ? { intent: "saveNote" } : null;
  }
  if (lk === "c" && shift && REVIEW_SCREENS.includes(ctx.screen)) return { intent: "copyLink" };
  if (key === "Enter" && !shift && ctx.screen === "wrap") return { intent: "sendOrRecord" };
  return null;
}

function escape(ctx: FlowKeyContext, surface: FlowSurface, focus: FocusKind): FlowAction | null {
  if (surface !== "none") return { intent: "closeSurface", surface };
  if (focus === "field") return ctx.searchFocused ? { intent: "clearSearch" } : null;
  if (ctx.screen === "home" && ctx.hasSelection && !ctx.firstRun) return { intent: "clearSelection" };
  if (ctx.screen === "org") return { intent: "backToSettings" };
  return null;
}

/**
 * What Enter does on Done, the one rule the key, the legend, the buttons' Enter chip and the palette share: Revision N
 * when it is ready and undo is closed; else the next review; with undo open and no next review, Revision N; else nothing.
 */
export function doneEnterTarget(state: { revisionReady: boolean; undoLive: boolean; hasNext?: boolean }): "revision" | "next" | null {
  const hasNext = state.hasNext !== false;
  if (state.revisionReady && !state.undoLive) return "revision";
  if (hasNext) return "next";
  return state.revisionReady ? "revision" : null;
}

function enter(ctx: FlowKeyContext): FlowAction | null {
  switch (ctx.screen) {
    case "home":
      return { intent: ctx.firstRun ? "start" : "rowOpen" };
    case "overview":
      return { intent: "start" };
    case "one":
      if (ctx.pauseStrip) return { intent: "keepAnswer" };
      return ctx.interstitial ? { intent: "goQuickCheck" } : null;
    case "quickCheck":
      return ctx.answered ? { intent: "goWrapUp" } : null;
    case "done":
{
      const target = doneEnterTarget({ revisionReady: Boolean(ctx.revision2Ready), undoLive: Boolean(ctx.undoLive), hasNext: ctx.hasNextReview });
      return target === "revision" ? { intent: "reviewRevision2" } : target === "next" ? { intent: "startNextReview" } : null;
    }
    default:
      return null;
  }
}

function plain(lk: string, ctx: FlowKeyContext, bindings: KeyBindings): FlowAction | null {
  const s = ctx.screen;
  const moveOn = Boolean(ctx.answeredGood);
  // The interstitial after the last part: the part keys (A, C, N, S, E, R, J) do nothing; K goes back; Enter goes to Quick check.
  // G, W, U, Q and Z fall through as on Focus (§3.4 and §3.5).
  if (s === "one" && ctx.interstitial && (lk === bindings.looksGood || lk === bindings.needsChange || lk === bindings.decideLater || ["s", "e", "r", "j"].includes(lk))) return null;
  if (lk === "/" && s === "home" && !ctx.firstRun) return { intent: "searchFocus" };
  if (lk === "z" && ctx.undoLive) return { intent: "undo" };
  if (s === "home") {
    if (ctx.firstRun) return null;
    if (lk === "j") return { intent: "rowNext" };
    if (lk === "k") return { intent: "rowPrevious" };
    // X is in the Home legend (D22), so it works on the short list too (SPEC 7.1 P5-1 F).
    if (lk === "x") return { intent: "rowToggle" };
    if (lk === "s") return { intent: "snooze" };
    return null;
  }
  if (lk === bindings.looksGood && (REVIEW_PART_SCREENS.includes(s) || s === "welcomeBack")) return { intent: "looksGood", moveOn };
  if (lk === bindings.needsChange && (REVIEW_PART_SCREENS.includes(s) || s === "welcomeBack")) return { intent: "needsChange" };
  if (lk === bindings.decideLater && (s === "one" || s === "allFiles") && ctx.isDecision) return { intent: "decideLater" };
  if (lk === "q" && ctx.aiAsk && ["overview", "one", "quickCheck", "allFiles", "welcomeBack"].includes(s)) {
    return ctx.aiBlocked ? { intent: "aiUnavailable", reason: ctx.aiBlocked } : { intent: "ask" };
  }
  if (lk === "r" && ctx.testAvailable && REVIEW_PART_SCREENS.includes(s)) return { intent: "runTest" };
  if (lk === "e" && s === "one") return { intent: "showWhole" };
  if (lk === "s" && s === "one") return { intent: "structure" };
  if (lk === "u" && REVIEW_PART_SCREENS.includes(s)) return { intent: "nextUnseenRisky" };
  if (lk === "w" && ["overview", "one", "quickCheck", "allFiles"].includes(s)) return { intent: "wrapUp" };
  if (lk === "g" && ["overview", "one", "quickCheck", "allFiles"].includes(s)) return { intent: "switchView" };
  if (lk === "j" && s === "one") return { intent: "nextPart", moveOn };
  if (lk === "k" && s === "one") return { intent: "previousPart" };
  if (lk === "j" && s === "allFiles") return { intent: "nextPart" };
  if (lk === "k" && s === "allFiles") return { intent: "previousPart" };
  if (lk === "]" && s === "allFiles") return { intent: "nextFile" };
  if (lk === "[" && s === "allFiles") return { intent: "previousFile" };
  return null;
}

/**
 * The intent for one key event in one context, or null when the key does nothing (the browser or the focused
 * control keeps it).
 */
export function flowAction(event: FlowKeyEvent, ctx: FlowKeyContext): FlowAction | null {
  if (event.altKey) return null;
  const surface = ctx.surface ?? "none";
  const focus = ctx.focus ?? "other";
  const bindings = ctx.bindings ?? DEFAULT_BINDINGS;
  const key = event.key;
  const lk = key.length === 1 ? key.toLowerCase() : key;
  const shift = Boolean(event.shiftKey);
  if (event.metaKey || event.ctrlKey) return chord(lk, key, shift, ctx, surface);
  if (key === "Escape") return escape(ctx, surface, focus);
  if (focus === "field") return null;
  // An open surface owns its keys first (the coverage dialog's Enter is its own), so the control rule applies only with none open.
  if (surface !== "none") {
    if (surface === "snooze" && /^[1-4]$/.test(key) && !shift) return { intent: "snoozePick", option: Number(key) as 1 | 2 | 3 | 4 };
    if (surface === "coverage" && key === "Enter") return { intent: shift ? "coverageWrapAnyway" : "coverageOpen" };
    return null;
  }
  if (key === "Enter" && (focus === "button" || focus === "link")) return null;
  if (key === "?") return { intent: "help" };
  // Shift blocks letters only, so a symbol typed with Shift ("!", or "/" on a German layout) reaches its binding.
  if (shift && /^[a-z]$/i.test(key)) {
    const denseHome = ctx.screen === "home" && ctx.dense && !ctx.firstRun;
    if (denseHome && (lk === "j" || lk === "k")) return { intent: lk === "j" ? "rowNext" : "rowPrevious", extend: true };
    return null;
  }
  if (key === "Enter") return shift ? null : enter(ctx);
  return plain(lk, ctx, bindings);
}

/** The key a user presses for an intent, uppercase for hints. Only the three rebindable intents can differ from their default. */
export function keyHint(intent: RebindableIntent, bindings: KeyBindings = DEFAULT_BINDINGS): string {
  return bindings[intent].toUpperCase();
}

/** The Focus card footer. "Q ask" drops out when AI Ask is unavailable (§3.5 footer rule). */
export function focusLegend(opts: { aiAsk: boolean; testAvailable?: boolean; bindings?: KeyBindings }): string {
  const b = opts.bindings ?? DEFAULT_BINDINGS;
  const parts = [
    `${keyHint("looksGood", b)} good`,
    `${keyHint("needsChange", b)} change`,
    `${keyHint("decideLater", b)} decide later`,
    ...(opts.aiAsk ? ["Q ask"] : []),
    "S structure",
    ...(opts.testAvailable ? ["R run test"] : []),
    "? all shortcuts",
  ];
  return parts.join(" · ");
}

function enterLine(target: "revision" | "next" | null): string[] {
  return target === "revision" ? ["Enter review Revision 2"] : target === "next" ? ["Enter start next review"] : [];
}

/** The key line the Shortcuts dialog lists on the new-flow screens other than One at a time. Each line names a key that works there (D22). */
export function screenLegend(
  screen: "quickCheck" | "allFiles" | "wrap" | "done" | "welcomeBack" | "map" | "preview",
  opts: { aiAsk: boolean; testAvailable?: boolean; undoLive?: boolean; revision2Ready?: boolean; hasNext?: boolean; bindings?: KeyBindings },
): string {
  const b = opts.bindings ?? DEFAULT_BINDINGS;
  const ask = opts.aiAsk ? ["Q ask"] : [];
  const test = opts.testAvailable ? ["R run test"] : [];
  const lines: string[] = screen === "quickCheck"
    ? [`${keyHint("looksGood", b)} good`, `${keyHint("needsChange", b)} change`, ...ask, ...test, "U next unseen risky part", "W wrap up", "G all files", "Enter wrap up when answered"]
    : screen === "allFiles"
      ? [`${keyHint("looksGood", b)} good`, `${keyHint("needsChange", b)} change`, `${keyHint("decideLater", b)} decide later`, ...ask, ...test, "J / K move", "] / [ file", "U next unseen risky part", "W wrap up", "G one at a time"]
      : screen === "wrap"
        ? ["⌘ / Ctrl + Enter send or record"]
        : screen === "done"
          ? [...enterLine(doneEnterTarget({ revisionReady: Boolean(opts.revision2Ready), undoLive: Boolean(opts.undoLive), hasNext: opts.hasNext })), ...(opts.undoLive ? ["Z undo the send"] : [])]
          : screen === "welcomeBack"
            ? [`${keyHint("looksGood", b)} all look fixed`, `${keyHint("needsChange", b)} still needs a change`, ...ask]
            : ["Arrow keys move between tabs and the list"];
  return [...lines, "? all shortcuts"].join(" · ");
}

export interface ShortcutGroup {
  title: string;
  keys: readonly { key: string; does: string }[];
}

/**
 * Every key the flow binds, grouped, for the Shortcuts dialog: the three rebindable answers (as rebound), each reserved
 * key's action (`RESERVED_ACTION`, the same names a rebinding refusal uses) and the chords `chord` handles. Some keys
 * work only on some screens; "On this screen" (the screen's legend) says which.
 */
export function allShortcuts(bindings: KeyBindings = DEFAULT_BINDINGS): ShortcutGroup[] {
  const reserved = (key: string, label = key.toUpperCase()) => ({ key: label, does: RESERVED_ACTION[key] });
  return [
    {
      title: "Answer a part",
      keys: (Object.keys(LABEL) as RebindableIntent[]).map((intent) => ({ key: keyHint(intent, bindings), does: LABEL[intent] })),
    },
    {
      title: "Move",
      keys: [reserved("j"), reserved("k"), reserved("]"), reserved("["), reserved("u"), reserved("e"), reserved("s"), reserved("g"), reserved("w")],
    },
    {
      title: "Anywhere",
      keys: [
        { key: "⌘ / Ctrl + K", does: "search and commands" },
        { key: "⌘ / Ctrl + Shift + C", does: "copy a link to this part" },
        { key: "⌘ / Ctrl + Enter", does: "send or record (Wrap up)" },
        reserved("q"),
        reserved("r"),
        reserved("z"),
        reserved("/"),
        reserved("x"),
        { key: "Esc", does: "close the open panel" },
        reserved("?"),
      ],
    },
  ];
}

/** The Home footer; first run shows the short form. */
export function homeLegend(firstRun: boolean, snooze = true): string {
  if (firstRun) return "Enter start · ? all shortcuts";
  return `J/K move · Enter open · X select · ${snooze ? "S snooze · " : ""}⌘K search · ? all shortcuts`;
}

export type RebindResult = { ok: true; bindings: KeyBindings } | { ok: false; reason: string };

/** Moves one rebindable intent to a new key. Rejects anything that is not one character, is reserved, or is already another intent's key. */
export function rebindKey(current: KeyBindings, intent: RebindableIntent, rawKey: string, shift = false): RebindResult {
  if (rawKey.length !== 1) return { ok: false, reason: "Pick a single letter, number or symbol." };
  // Shift blocks letters, so a letter typed with Shift could never fire. A capital without Shift (Caps Lock) is the same key.
  if (shift && /^[a-z]$/i.test(rawKey)) return { ok: false, reason: "Press the letter without Shift." };
  const key = rawKey.toLowerCase();
  const other = (Object.keys(current) as RebindableIntent[]).find((name) => name !== intent && current[name] === key);
  if (other) return { ok: false, reason: `${key.toUpperCase()} is already used for ${LABEL[other]}. Pick another key.` };
  if (RESERVED_KEYS.includes(key)) return { ok: false, reason: `${key.toUpperCase()} is already used for ${RESERVED_ACTION[key] ?? "another shortcut"}. Pick another key.` };
  return { ok: true, bindings: { ...current, [intent]: key } };
}
