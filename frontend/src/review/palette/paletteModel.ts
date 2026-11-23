/**
 * The command palette's rules (SPEC 4.12), pure: which actions a screen offers in a given state, how a query matches,
 * and how results are grouped. Nothing here reads the DOM, the router or the network. The palette component feeds
 * it what the open screen registered; each action's `run` is the function the screen's own button or key calls.
 *
 * An action is listed only when the screen supplied it AND this state allows it. A placeholder review (R7) is a
 * review with no parts yet: it offers only Snooze, Go Home, other reviews and Settings.
 */

import { DEFAULT_BINDINGS, doneEnterTarget, keyHint, type FlowScreen, type KeyBindings } from "../flowKeymap";
import type { MarkState } from "../types";

export interface PaletteItem {
  id: string;
  group: string;
  label: string;
  /** The grey text after the label (a file and a state, a count). */
  sub?: string;
  /** The key that does the same (hidden on a phone). */
  hint?: string;
  run: () => void;
}

/** What a screen can do from the palette. A function is present only where the screen has that control. */
export interface PaletteActions {
  startNext?: () => void;
  snooze?: () => void;
  filterRepo?: (repo: string) => void;
  sort?: (key: string) => void;
  looksGood?: () => void;
  needsChange?: () => void;
  decideLater?: () => void;
  structure?: () => void;
  switchView?: () => void;
  wrapUp?: () => void;
  copyLink?: () => void;
  ask?: () => void;
  start?: () => void;
  send?: () => void;
  changeVerdict?: () => void;
  reviewRevision2?: () => void;
  startNextReview?: () => void;
  undo?: () => void;
  snoozeReview?: () => void;
  goHome?: () => void;
}

export interface HomePaletteState {
  /** A Your turn review exists to start. */
  hasNext: boolean;
  /** Rows selected on the list. */
  selected: number;
  /** The current or selected rows can be snoozed. */
  canSnooze: boolean;
  /** The dense list shows the sort and the repo chips; the palette's Filter and Sort rows exist only with them. */
  dense: boolean;
  repos: readonly string[];
  activeRepos: readonly string[];
  sorts: readonly { key: string; label: string }[];
  sort: string;
}

export interface PaletteContext {
  screen: FlowScreen;
  /** R7: a review with no parts yet. */
  placeholder?: boolean;
  bindings?: KeyBindings;
  /** The part the screen acts on; absent when there is none (the Overview, the interstitial). */
  part?: string;
  /** The person can answer now (can write, nothing pending). */
  canAnswer?: boolean;
  /** The part is a decision, so Decide later applies. */
  isDecision?: boolean;
  /** One at a time shows a part, so the structure drawer exists. */
  structureAvailable?: boolean;
  /** AI Ask is available (R6). */
  aiAsk?: boolean;
  /** The reading view the screen shows, for the switch label. */
  view?: "one" | "all";
  /** Wrap up. */
  recorded?: boolean;
  sendLabel?: string;
  /** Done. */
  revision2Ready?: boolean;
  revision2Label?: string;
  undoLive?: boolean;
  hasNextReview?: boolean;
  home?: HomePaletteState;
  /** Settings: the sections the page can jump to. */
  sections?: readonly { id: string; label: string; run: () => void }[];
  actions: PaletteActions;
}

const PART_SCREENS: readonly FlowScreen[] = ["one", "quickCheck", "allFiles"];
const VIEW_SCREENS: readonly FlowScreen[] = ["overview", "one", "quickCheck", "allFiles"];

/** The group heading that leads the list for a screen. */
export function contextGroup(ctx: Pick<PaletteContext, "screen" | "placeholder" | "part">): string {
  if (ctx.placeholder) return "This review";
  if (ctx.screen === "home") return "On Home";
  if (ctx.screen === "settings") return "On Settings";
  if (ctx.screen === "wrap") return "Wrap up";
  return ctx.part ? `This part · ${ctx.part}` : "In this review";
}

function chordHint(): string {
  return "⌘⇧C";
}

/** The actions valid for this screen in this state, in the order SPEC 4.12 lists them. */
export function contextActions(ctx: PaletteContext): PaletteItem[] {
  const group = contextGroup(ctx);
  const a = ctx.actions;
  const b = ctx.bindings ?? DEFAULT_BINDINGS;
  const out: PaletteItem[] = [];
  const add = (id: string, label: string, run: (() => void) | undefined, hint?: string) => {
    if (run) out.push({ id, group, label, hint, run });
  };

  if (ctx.placeholder) {
    add("snoozeReview", "Snooze this review", a.snoozeReview);
    add("goHome", "Go Home", a.goHome);
    return out;
  }

  if (ctx.screen === "home" && ctx.home) {
    const h = ctx.home;
    if (h.hasNext) add("startNext", "Start next review", a.startNext, "Enter");
    if (h.canSnooze) add("snooze", h.selected > 0 ? "Snooze selected" : "Snooze current review", a.snooze, "S");
    if (h.dense) {
      for (const repo of h.repos) {
        if (!h.activeRepos.includes(repo) && a.filterRepo) out.push({ id: `filter:${repo}`, group, label: `Filter: ${repo}`, run: () => a.filterRepo?.(repo) });
      }
      for (const choice of h.sorts) {
        if (choice.key !== h.sort && a.sort) out.push({ id: `sort:${choice.key}`, group, label: `Sort: ${choice.label.toLowerCase()}`, run: () => a.sort?.(choice.key) });
      }
    }
    return out;
  }

  if (ctx.screen === "settings") {
    for (const section of ctx.sections ?? []) out.push({ id: `section:${section.id}`, group, label: `Go to ${section.label}`, run: section.run });
    return out;
  }

  if (ctx.screen === "wrap") {
    if (!ctx.recorded) {
      add("send", ctx.sendLabel || "Record your verdict", a.send, "⌘Enter");
      add("changeVerdict", "Change verdict…", a.changeVerdict);
    }
    return out;
  }

  if (ctx.screen === "done") {
    // The one rule Enter follows on Done (flowKeymap `doneEnterTarget`).
    const target = doneEnterTarget({ revisionReady: Boolean(ctx.revision2Ready), undoLive: Boolean(ctx.undoLive), hasNext: Boolean(ctx.hasNextReview) });
    if (target === "revision") add("reviewRevision2", ctx.revision2Label || "Review Revision 2", a.reviewRevision2, "Enter");
    else if (target === "next") add("startNextReview", "Start next review", a.startNextReview, "Enter");
    if (ctx.undoLive) add("undo", "Undo the send", a.undo, "Z");
    return out;
  }

  if (ctx.screen === "overview") add("start", "Start review", a.start, "Enter");

  const onPart = Boolean(ctx.part) && (PART_SCREENS.includes(ctx.screen) || ctx.screen === "welcomeBack");
  if (onPart && ctx.canAnswer) {
    add("looksGood", "Looks good", a.looksGood, keyHint("looksGood", b));
    add("needsChange", ctx.screen === "welcomeBack" ? "Still needs a change" : "Needs a change", a.needsChange, keyHint("needsChange", b));
    if (ctx.isDecision && (ctx.screen === "one" || ctx.screen === "allFiles")) add("decideLater", "Decide later", a.decideLater, keyHint("decideLater", b));
  }
  if (ctx.screen === "one" && ctx.part && ctx.structureAvailable) add("structure", "Show structure", a.structure, "S");
  if (VIEW_SCREENS.includes(ctx.screen)) add("switchView", ctx.view === "all" ? "Switch to One at a time" : "Switch to All files", a.switchView, "G");
  if (VIEW_SCREENS.includes(ctx.screen)) add("wrapUp", "Go to Wrap up", a.wrapUp, "W");
  if (ctx.part && PART_SCREENS.includes(ctx.screen)) add("copyLink", "Copy link to this part", a.copyLink, chordHint());
  if (ctx.aiAsk) add("ask", ctx.part ? "Ask about this part" : "Ask about the change", a.ask, "Q");
  return out;
}

// ------------------------------------------------------------------ the rest of the list

export interface PaletteReview {
  id: string;
  label: string;
  sub: string;
  path: string;
  /** The review the person is in; a placeholder palette leaves it out. */
  current: boolean;
}

export interface PaletteRepo { name: string; waiting: number }
export interface PalettePart { id: string; name: string; file: string; state: string; path: string }
export interface RecentPlace { label: string; sub: string; path: string }

export interface PaletteData {
  reviews: readonly PaletteReview[];
  repos: readonly PaletteRepo[];
  recent: readonly RecentPlace[];
  parts: readonly PalettePart[];
}

export interface PaletteNav {
  go: (path: string) => void;
  openRepo: (name: string) => void;
  openShortcuts?: () => void;
  /** A phone has no keyboard, so the Keyboard settings row is left out. */
  phone?: boolean;
}

/** The Settings rows for this device: no Keyboard row on a phone. */
export function settingsItems(phone?: boolean) {
  return SETTINGS_ITEMS.filter((entry) => !(phone && entry.label === "Settings: Keyboard"));
}

export const SETTINGS_ITEMS: readonly { label: string; path: string }[] = [
  { label: "Settings: Default view", path: "/settings/review#reading" },
  { label: "Settings: Skip the Overview", path: "/settings/review#reading" },
  { label: "Settings: Notifications", path: "/settings/review#notifications" },
  { label: "Settings: Keyboard", path: "/settings/review#keyboard" },
  { label: "Settings: Look", path: "/settings/review#look" },
  { label: "Review policy: Always a decision", path: "/settings/review#policy" },
];

/** The answer a part carries, in SPEC words. An unanswered part never reads Looks good (R5). */
export function partStateLabel(state: MarkState, changedSinceMark = false): string {
  if (changedSinceMark) return "Not answered";
  if (state === "reviewed") return "Looks good";
  if (state === "needs_changes") return "Needs a change";
  if (state === "later") return "Left for later";
  return "Not answered";
}

/** Every item the palette can show for a screen and its data, in the order of SPEC 4.12. */
export function allItems(ctx: PaletteContext, data: PaletteData, nav: PaletteNav, showRecent: boolean): PaletteItem[] {
  const out = contextActions(ctx);
  if (ctx.placeholder) {
    for (const review of data.reviews) if (!review.current) out.push({ id: `review:${review.id}`, group: "Reviews", label: review.label, sub: review.sub, run: () => nav.go(review.path) });
    for (const entry of settingsItems(nav.phone)) out.push({ id: `settings:${entry.label}`, group: "Settings", label: entry.label, run: () => nav.go(entry.path) });
    return out;
  }
  if (showRecent) for (const place of data.recent) out.push({ id: `recent:${place.path}`, group: "Recent", label: place.label, sub: place.sub, run: () => nav.go(place.path) });
  for (const part of data.parts) out.push({ id: `part:${part.id}`, group: "Parts in this review", label: part.name, sub: `${part.file} · ${part.state}`, run: () => nav.go(part.path) });
  for (const review of data.reviews) out.push({ id: `review:${review.id}`, group: "Reviews", label: review.label, sub: review.sub, run: () => nav.go(review.path) });
  for (const repo of data.repos) out.push({ id: `repo:${repo.name}`, group: "Repos", label: repo.name, sub: `${repo.waiting} waiting`, run: () => nav.openRepo(repo.name) });
  for (const entry of settingsItems(nav.phone)) out.push({ id: `settings:${entry.label}`, group: "Settings", label: entry.label, run: () => nav.go(entry.path) });
  if (nav.openShortcuts) out.push({ id: "help", group: "Help", label: "Keyboard shortcuts", hint: "?", run: nav.openShortcuts });
  return out;
}

// ------------------------------------------------------------------ matching

export interface Fuzzy { score: number; idx: number[] }

/** Characters of the query found in order in the text; null when they are not all there. Consecutive and word-start hits score higher. */
export function fuzzy(query: string, text: string): Fuzzy | null {
  const needle = query.toLowerCase().replace(/\s+/g, "");
  if (!needle) return { score: 0, idx: [] };
  const hay = text.toLowerCase();
  let i = 0;
  let last = -2;
  let score = 0;
  const idx: number[] = [];
  for (let j = 0; j < hay.length && i < needle.length; j += 1) {
    if (hay[j] !== needle[i]) continue;
    idx.push(j);
    score += (j === last + 1 ? 5 : 1) + (j === 0 || /[\s_·:/#-]/.test(hay[j - 1]) ? 4 : 0);
    last = j;
    i += 1;
  }
  if (i < needle.length) return null;
  const sub = hay.indexOf(needle);
  if (sub >= 0) score += 12 - Math.min(sub, 10);
  return { score: score - idx[0] * 0.1, idx };
}

export interface ShownItem extends PaletteItem { idx: number[]; score: number }
export interface ShownGroup { name: string; items: ShownItem[] }

export const RESULTS_PER_GROUP = 6;
export const RECENT_MAX = 5;

const LEADING = new Set(["On Home", "On Settings", "Wrap up", "In this review", "This review"]);
const isLeading = (group: string) => LEADING.has(group) || group.startsWith("This part");

/**
 * The groups to draw. Empty query: the context group whole, Recent (5), then a short head of each other group.
 * With a query: fuzzy over the label, then over the sub text and group name (weaker); best groups first; at most 6 per group.
 */
export function shownGroups(items: readonly PaletteItem[], query: string, leading: string): ShownGroup[] {
  const q = query.trim();
  const matched: ShownItem[] = [];
  for (const item of items) {
    const byLabel = fuzzy(q, item.label);
    if (byLabel) { matched.push({ ...item, idx: byLabel.idx, score: byLabel.score + (isLeading(item.group) ? 2 : 0) }); continue; }
    const byRest = q ? fuzzy(q, `${item.sub ?? ""} ${item.group}`) : null;
    if (byRest) matched.push({ ...item, idx: [], score: byRest.score * 0.3 });
  }
  const order = [leading, "In this review", "Recent", "Parts in this review", "Reviews", "Repos", "Settings", "Help"];
  const names = [...new Set([...order, ...matched.map((item) => item.group)])];
  const groups: ShownGroup[] = [];
  for (const name of names) {
    let list = matched.filter((item) => item.group === name);
    if (!q) {
      if (name === "Recent") list = list.slice(0, RECENT_MAX);
      else if (!isLeading(name)) list = list.slice(0, name === "Parts in this review" ? 5 : 4);
    } else list = [...list].sort((x, y) => y.score - x.score).slice(0, RESULTS_PER_GROUP);
    if (list.length) groups.push({ name, items: list });
  }
  if (q) groups.sort((x, y) => y.items[0].score - x.items[0].score);
  return groups;
}

/** The text the live region announces after a search. */
export function resultsAnnouncement(query: string, count: number): string {
  if (!query.trim()) return "";
  return count > 0 ? `${count} ${count === 1 ? "result" : "results"}` : `No match for ${query.trim()}`;
}

export function emptyText(query: string): string {
  return `No match for '${query.trim()}'. Try a function name, a repo, or an action.`;
}

/** Adds a place to the recent list: newest first, no repeats of the same address, at most five. */
export function pushRecent(list: readonly RecentPlace[], place: RecentPlace): RecentPlace[] {
  return [place, ...list.filter((entry) => entry.path !== place.path)].slice(0, RECENT_MAX);
}
