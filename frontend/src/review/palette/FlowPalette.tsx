import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode,
} from "react";
import { Search } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";

import { Dialog, DialogContent, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { classifyFocus, flowAction } from "../flowKeymap";
import { useHomeSource } from "../home/homeApi";
import {
  buildSections, canSnooze, chipCounts, repoOf, sectionOf, SECTION_TITLE, snoozeUntil, stateText, untilText, resumePath, rowResume, NO_FILTERS,
  type HomeOptions, type SnoozeOption,
} from "../home/homeModel";
import { loadPrefs, savePrefs } from "../home/homePrefs";
import { SnoozePicker } from "../home/SnoozePicker";
import { SendFeedbackDialog } from "../feedback/SendFeedbackDialog";
import { useFlowToast } from "../flowToast";
import { openFlowSurface } from "../openSurface";
import { reviewHref } from "../reviewApi";
import { getFlowBindings } from "../settings/userSettings";
import type { ReviewDirectoryRow } from "../types";
import {
  allItems, contextActions, contextGroup, emptyText, pushRecent, resultsAnnouncement, shownGroups,
  type PaletteContext, type PaletteData, type PaletteItem, type PalettePart, type PaletteReview, type PaletteReviewAction, type RecentPlace, type ShownGroup,
} from "./paletteModel";

/** What the open screen tells the palette. Registered by `useFlowPaletteScreen`; the palette reads the latest on every key and render. */
export interface PaletteRegistration {
  context: PaletteContext;
  reviewRef?: string;
  apiReviewId?: string;
  parts?: readonly PalettePart[];
  /** Where this screen is, for Recent ("shop · Fix discount" and "Overview"). */
  place?: { label: string; sub: string };
  openShortcuts?: () => void;
}

interface Registered { owner: object; reg: PaletteRegistration }
interface RegisteredActions { owner: object; actions: readonly PaletteReviewAction[] }

interface PaletteApi {
  registered: MutableRefObject<Registered | null>;
  reviewActions: MutableRefObject<RegisteredActions | null>;
  /** Entries a mounted control adds to "In this review" (the top bar's Notes), by owner. */
  extras: MutableRefObject<Map<object, PaletteItem[]>>;
  open: (query?: string) => void;
  recordPlace: (place: RecentPlace) => void;
  copyLink: (href: string) => Promise<void>;
}

const PaletteContextValue = createContext<PaletteApi | null>(null);

const RECENT_KEY = "lemoncrow.review.recent";

function loadRecent(): RecentPlace[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENT_KEY) ?? "[]") as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is RecentPlace => Boolean(entry) && typeof entry.label === "string" && typeof entry.sub === "string" && typeof entry.path === "string" && entry.path.startsWith("/")).slice(0, 5);
  } catch {
    return [];
  }
}

function saveRecent(list: readonly RecentPlace[]) {
  try { window.localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* storage blocked: Recent lasts until the page closes */ }
}

/** Registers the open screen with the palette. A screen outside the provider (a unit test) registers nothing. */
export function useFlowPaletteScreen(reg: PaletteRegistration) {
  const api = useContext(PaletteContextValue);
  const owner = useRef({});
  const location = useLocation();
  const place = reg.place;
  if (api) api.registered.current = { owner: owner.current, reg };
  useEffect(() => {
    const mine = owner.current;
    return () => { if (api?.registered.current?.owner === mine) api.registered.current = null; };
  }, [api]);
  useEffect(() => {
    if (api && place) api.recordPlace({ label: place.label, sub: place.sub, path: `${location.pathname}${location.search}` });
  }, [api, place?.label, place?.sub, location.pathname, location.search]); // eslint-disable-line react-hooks/exhaustive-deps
}

/**
 * The open Review's own actions (the corrections catalog), listed under "In this review" on whichever screen is open.
 * Registered by the Reader, which outlives its screens; a Reader outside the provider registers nothing.
 */
export function useFlowPaletteReviewActions(actions: readonly PaletteReviewAction[]) {
  const api = useContext(PaletteContextValue);
  const owner = useRef({});
  if (api) api.reviewActions.current = { owner: owner.current, actions };
  useEffect(() => {
    const mine = owner.current;
    return () => { if (api?.reviewActions.current?.owner === mine) api.reviewActions.current = null; };
  }, [api]);
}

/** Adds entries to "In this review" while the caller is mounted (the top bar's Notes). Outside the provider it adds nothing. */
export function useFlowPaletteExtras(items: readonly Omit<PaletteItem, "group">[]) {
  const api = useContext(PaletteContextValue);
  const owner = useRef({});
  const grouped = items.map((item) => ({ ...item, group: "In this review" }));
  const latest = useRef(grouped);
  latest.current = grouped;
  if (api) api.extras.current.set(owner.current, grouped);
  useEffect(() => {
    const mine = owner.current;
    api?.extras.current.set(mine, latest.current);
    return () => { api?.extras.current.delete(mine); };
  }, [api]);
}

/** Opens the palette; null where no provider is mounted (the button then is not drawn). */
export function usePaletteOpener(): ((query?: string) => void) | null {
  return useContext(PaletteContextValue)?.open ?? null;
}

/** Copies a link and says so in the palette's toast (SPEC 4.12): "Link copied", or the link itself when the clipboard refuses. */
export function useCopyLink(): ((href: string) => Promise<void>) | null {
  return useContext(PaletteContextValue)?.copyLink ?? null;
}

function reviewsOf(rows: readonly ReviewDirectoryRow[], now: number, currentHref: string, options: HomeOptions): PaletteReview[] {
  const out: PaletteReview[] = [];
  for (const row of rows) {
    const section = sectionOf(row, now, options);
    if (!section) continue;
    const base = reviewHref(row.ref || row.id);
    out.push({
      id: row.id,
      label: `${repoOf(row)} · ${row.title}`,
      sub: stateText(row, section, new Date(now)) || SECTION_TITLE[section],
      path: resumePath(base, rowResume(row)),
      current: base === currentHref,
    });
  }
  return out;
}

function Highlighted({ text, idx }: { text: string; idx: readonly number[] }) {
  if (idx.length === 0) return <>{text}</>;
  const marked = new Set(idx);
  const parts: ReactNode[] = [];
  let buffer = "";
  let on = false;
  for (let i = 0; i < text.length; i += 1) {
    const hit = marked.has(i);
    if (hit !== on) { parts.push(on ? <mark key={i} className="bg-transparent font-semibold text-foreground underline">{buffer}</mark> : buffer); buffer = ""; on = hit; }
    buffer += text[i];
  }
  parts.push(on ? <mark key="end" className="bg-transparent font-semibold text-foreground underline">{buffer}</mark> : buffer);
  return <>{parts}</>;
}

export function FlowPaletteProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const registered = useRef<Registered | null>(null);
  const reviewActions = useRef<RegisteredActions | null>(null);
  const extras = useRef(new Map<object, PaletteItem[]>());
  const [open, setOpen] = useState(false);
  const [initial, setInitial] = useState("");
  const [recent, setRecent] = useState<RecentPlace[]>(loadRecent);
  const [rows, setRows] = useState<ReviewDirectoryRow[] | null>(null);
  const [teamsSupported, setTeamsSupported] = useState(false);
  // Nothing is offered that the source has not said it can do, until its load finishes.
  const [queueSupported, setQueueSupported] = useState(false);
  const [picker, setPicker] = useState(false);
  const [feedback, setFeedback] = useState(false);
  const toast = useFlowToast();
  const source = useHomeSource();
  const ranRef = useRef(false);
  const live = useRef({ open, picker });
  live.current = { open, picker };

  const openPalette = useCallback((query = "") => {
    if (!registered.current) return;
    ranRef.current = false;
    setInitial(query);
    setOpen(true);
  }, []);
  const close = useCallback(() => setOpen(false), []);

  // The reviews and repos come from the same Inbox read Home uses; a fresh read on every open.
  useEffect(() => {
    if (!open) return;
    let current = true;
    source.load().then((result) => { if (current) { setRows(result.rows); setTeamsSupported(result.teamsSupported); setQueueSupported(result.queueSupported !== false); } }).catch(() => { if (current) setRows([]); });
    return () => { current = false; };
  }, [open, source]);

  const recordPlace = useCallback((place: RecentPlace) => {
    setRecent((list) => {
      const next = pushRecent(list, place);
      saveRecent(next);
      return next;
    });
  }, []);

  const copyLink = useCallback(async (href: string) => {
    try {
      await navigator.clipboard.writeText(href);
      toast.show({ text: "Link copied" });
    } catch {
      toast.show({ text: `Couldn't copy · here is the link: ${href}`, tone: "alert" });
    }
  }, [toast.show]); // eslint-disable-line react-hooks/exhaustive-deps

  // The current review's own queue row: Snooze this review is offered only when it waits on the viewer.
  const reg = open || picker ? registered.current?.reg : undefined;
  const currentHref = reg?.reviewRef ? reviewHref(reg.reviewRef) : "";
  const currentRow = useMemo(
    () => (rows && currentHref ? rows.find((row) => reviewHref(row.ref || row.id) === currentHref) ?? null : null),
    [rows, currentHref],
  );
  const now = Date.now();

  const snoozeReview = useCallback(async (option: SnoozeOption) => {
    setPicker(false);
    const row = currentRow;
    if (!row) return;
    const at = new Date();
    const until = snoozeUntil(option, at);
    const save = queueSupported ? source.snooze : undefined;
    if (!save) return;
    try {
      await save(row.id, until.toISOString());
      toast.show({
        text: `Snoozed until ${untilText(until, at)}`,
        undo: () => { void save(row.id, null).catch(() => toast.show({ text: "Couldn't undo the snooze · try again", tone: "alert" })); },
      });
    } catch {
      toast.show({ text: "Couldn't snooze 1 review · try again", tone: "alert" });
    }
  }, [currentRow, source, queueSupported, toast.show]); // eslint-disable-line react-hooks/exhaustive-deps

  // ⌘K, ⌘⇧C and the snooze picker's 1 to 4, all decided by flowAction.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const entry = registered.current;
      if (!entry) return;
      const el = document.activeElement as HTMLElement | null;
      const state = live.current;
      const action = flowAction(event, {
        screen: entry.reg.context.screen,
        surface: state.open ? "palette" : state.picker ? "snooze" : openFlowSurface(),
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
        bindings: getFlowBindings(),
      });
      if (!action) return;
      if (action.intent === "palette") {
        event.preventDefault();
        if (state.open) setOpen(false); else openPalette();
      } else if (action.intent === "copyLink") {
        const item = contextActions(entry.reg.context).find((candidate) => candidate.id === "copyLink");
        if (item) { event.preventDefault(); item.run(); }
      } else if (action.intent === "snoozePick" && action.option) {
        event.preventDefault();
        void snoozeReviewRef.current(action.option);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPalette]);
  const snoozeReviewRef = useRef(snoozeReview);
  snoozeReviewRef.current = snoozeReview;

  const api = useMemo<PaletteApi>(() => ({ registered, reviewActions, extras, open: openPalette, recordPlace, copyLink }), [openPalette, recordPlace, copyLink]);

  // -- what the palette shows --------------------------------------------------
  let content: ReactNode = null;
  if (open && reg) {
    const placeholderActions = reg.context.placeholder
      ? { snoozeReview: queueSupported && source.snooze && currentRow && canSnooze(currentRow, now, { teamsSupported }) ? () => setPicker(true) : undefined, goHome: () => navigate("/reviews") }
      : {};
    // The person's own keys (Settings › Keyboard) show on the rows, as they do on every hint.
    const context: PaletteContext = { ...reg.context, bindings: getFlowBindings(), actions: { ...reg.context.actions, ...placeholderActions } };
    const sections = rows ? buildSections(rows, now, { teamsSupported }) : [];
    const waiting = new Map<string, number>();
    for (const section of sections) if (section.id === "yourTurn" || section.id === "cameBack") for (const row of section.rows) waiting.set(repoOf(row), (waiting.get(repoOf(row)) ?? 0) + 1);
    const data: PaletteData = {
      reviews: rows ? reviewsOf(rows, now, currentHref, { teamsSupported }) : [],
      repos: rows ? chipCounts(rows.filter((row) => row.status === "open")).repos.map((chip) => ({ name: chip.name, waiting: waiting.get(chip.name) ?? 0 })) : [],
      recent: recent.filter((place) => place.path !== `${location.pathname}${location.search}`),
      parts: reg.parts ?? [],
      reviewActions: reviewActions.current?.actions ?? [],
    };
    const items = allItems(context, data, {
      go: (path) => navigate(path),
      openRepo: (name) => {
        if (context.screen === "home" && context.actions.filterRepo) { context.actions.filterRepo(name); return; }
        savePrefs({ ...loadPrefs(), filters: { ...NO_FILTERS, repos: [name] } });
        navigate("/reviews");
      },
      openShortcuts: reg.openShortcuts,
      openFeedback: () => setFeedback(true),
      phone: typeof window.matchMedia === "function" && window.matchMedia("(max-width: 639px)").matches,
    }, !reg.context.placeholder);
    if (!reg.context.placeholder) for (const added of extras.current.values()) items.push(...added);
    content = <PaletteDialog key={initial} items={items} leading={contextGroup(context)} initial={initial} ranRef={ranRef} onClose={close} />;
  }

  return (
    <PaletteContextValue.Provider value={api}>
      {children}
      {content}
      <SnoozePicker open={picker} count={1} onPick={(option) => void snoozeReview(option)} onClose={() => setPicker(false)} />
      <SendFeedbackDialog open={feedback} onOpenChange={setFeedback} />
      {toast.element}
    </PaletteContextValue.Provider>
  );
}

function PaletteDialog({ items, leading, initial, ranRef, onClose }: {
  items: readonly PaletteItem[];
  leading: string;
  initial: string;
  ranRef: MutableRefObject<boolean>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState(initial);
  const [active, setActive] = useState(0);
  const groups = useMemo<ShownGroup[]>(() => shownGroups(items, query, leading), [items, query, leading]);
  const flat = groups.flatMap((group) => group.items);
  const current = Math.min(active, Math.max(0, flat.length - 1));
  useEffect(() => setActive(0), [query]);
  useEffect(() => { document.getElementById(`flow-pal-${current}`)?.scrollIntoView?.({ block: "nearest" }); }, [current]);

  const run = (item: PaletteItem | undefined) => {
    if (!item || item.disabled) return;
    ranRef.current = true;
    onClose();
    window.setTimeout(item.run, 0);
  };
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((current + 1) % Math.max(1, flat.length)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((current - 1 + flat.length) % Math.max(1, flat.length)); }
    else if (event.key === "Enter") { event.preventDefault(); run(flat[current]); }
    else if (event.key === "Tab") {
      event.preventDefault();
      if (groups.length === 0) return;
      const at = groups.findIndex((group) => group.items.includes(flat[current]));
      const next = groups[(at + (event.shiftKey ? -1 : 1) + groups.length) % groups.length];
      setActive(flat.indexOf(next.items[0]));
    }
  };

  let n = -1;
  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby={undefined}
        data-testid="flow-palette"
        className="top-[10%] max-h-[80dvh] translate-y-0 max-sm:top-[4%]"
        onCloseAutoFocus={(event) => { if (ranRef.current) event.preventDefault(); }}
      >
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <div className="relative flex shrink-0 items-center border-b border-border">
          <Search size={16} aria-hidden="true" className="pointer-events-none absolute left-3 text-muted-foreground" />
          {/* No autoFocus: it would move focus before the Dialog records the control to return to. The dialog focuses this field itself. */}
          <Input
            unstyled
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="flow-pal-list"
            aria-autocomplete="list"
            aria-activedescendant={flat.length ? `flow-pal-${current}` : undefined}
            aria-label="Search actions, parts, reviews and settings"
            placeholder="Type a function name, a repo, or an action"
            autoComplete="off"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            className="h-12 w-full bg-transparent pl-9 pr-3 text-copy outline-none placeholder:text-muted-foreground pointer-coarse:h-14"
          />
        </div>
        <div id="flow-pal-list" role={flat.length ? "listbox" : undefined} aria-label={flat.length ? "Results" : undefined} className="min-h-0 flex-1 overflow-y-auto py-1">
          {flat.length ? groups.map((group, gi) => (
            <div key={group.name} role="group" aria-labelledby={`flow-pal-g-${gi}`}>
              <div id={`flow-pal-g-${gi}`} className="px-3 pb-1 pt-2 font-mono text-tag uppercase text-muted-foreground">{group.name}</div>
              {group.items.map((item) => {
                n += 1;
                const index = n;
                return (
                  <div
                    key={item.id}
                    id={`flow-pal-${index}`}
                    role="option"
                    aria-selected={index === current}
                    aria-disabled={item.disabled || undefined}
                    data-pal-item={item.label}
                    className={`flex min-h-9 items-center justify-between gap-3 px-3 py-1.5 text-label ${item.disabled ? "cursor-default text-muted-foreground" : "cursor-pointer"} ${index === current ? "bg-accent text-foreground" : ""}`}
                    onMouseMove={() => { if (index !== current) setActive(index); }}
                    onClick={() => run(item)}
                  >
                    <span className="min-w-0 truncate">
                      <Highlighted text={item.label} idx={item.idx} />
                      {item.sub ? <span className={index === current ? "text-foreground" : "text-muted-foreground"}> · {item.sub}</span> : null}
                    </span>
                    {item.hint ? <kbd className={`shrink-0 rounded border border-border px-1 font-mono text-tag max-sm:hidden pointer-coarse:hidden ${index === current ? "text-foreground" : "text-muted-foreground"}`}>{item.hint}</kbd> : null}
                  </div>
                );
              })}
            </div>
          )) : (
            <p data-testid="flow-palette-empty" className="px-4 py-6 text-label text-muted-foreground">{emptyText(query)}</p>
          )}
        </div>
        <p role="status" className="sr-only" data-testid="flow-palette-live">{resultsAnnouncement(query, flat.length)}</p>
        <div className="shrink-0 border-t border-border px-3 py-2 text-meta text-muted-foreground max-sm:hidden pointer-coarse:hidden">↑ ↓ move · Enter go · Tab next group</div>
      </DialogContent>
    </Dialog>
  );
}
