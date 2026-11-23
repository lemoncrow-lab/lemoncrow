import { usePolicyApprovable } from "../policy/policyEdit";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Search, Settings2 } from "lucide-react";
import { NotificationBell } from "../notifications/NotificationBell";
import { Link, useNavigate } from "react-router-dom";

import lemonCrowMark from "../../assets/lemoncrow-mark.svg";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Select, SelectItem } from "../../components/ui/select";
import { classifyFocus, flowAction, homeLegend, type FlowKeyContext } from "../flowKeymap";
import { FlowShortcutsDialog } from "../FlowTopBar";
import { openFlowSurface } from "../openSurface";
import { useFlowPaletteScreen, usePaletteOpener } from "../palette/FlowPalette";
import { plural } from "../overview/overviewModel";
import { reviewHref } from "../reviewApi";
import type { ReviewDirectoryRow, TeamState } from "../types";
import { LOCAL_QUEUE_CLEAR, useHomeSource, type HomeSource } from "./homeApi";
import {
  budgetLine, budgetOf, buildSections, canSnooze, chipCounts, filtersActive, isAuthorTurn, isDense, isFirstRun, isQueueClear,
  matchesFilters, NO_FILTERS, resumePath, rowResume, SORT_CHOICES, sortRows, snoozeUntil, untilText, isSortKey, SECTION_TITLE,
  type HomeFilters, type HomeOptions, type HomeSection, type HomeSectionId, type SnoozeOption, type SortKey,
} from "./homeModel";
import { askedFromJournal } from "./homeTask";
import { loadPrefs, savePrefs } from "./homePrefs";
import { Hero, QueueRow, type HeroTask } from "./HomeRows";
import HomeSavedFilters from "./HomeSavedFilters";
import { openCommand, OpenInTerminal } from "./OpenInTerminal";
import { SnoozePicker } from "./SnoozePicker";
import { useFlowToast } from "../flowToast";
import { AiBudgetBanner } from "../AiBudgetBanner";
import { FlowOfflineBanner } from "../FlowOfflineBanner";
import { TeamRowLine } from "../team/TeamRowLine";
import { teamLine } from "../team/teamCopy";
import { useTeamRowActions } from "../team/useTeamRowActions";

const TICK_MS = 30_000;
const FOLDED: readonly HomeSectionId[] = ["team", "done", "finished"];

function basePath(row: ReviewDirectoryRow): string {
  return reviewHref(row.ref || row.id);
}

function continuePath(row: ReviewDirectoryRow): string {
  return resumePath(basePath(row), rowResume(row));
}

/** A message whose `backticked` words are a command: drawn as code, never as literal backticks. */
function withCode(text: string): ReactNode {
  return text.split(/`([^`]+)`/).map((piece, index) => index % 2 === 1 ? <code key={index} className="rounded bg-muted px-1 font-mono text-meta">{piece}</code> : piece);
}

/** Home (SPEC 4.1): what waits on you, and the next review in one key. Rendered at `/reviews`. */
export default function ReviewHomeScreen({ source: given, barExtras }: {
  /** Where the reviews come from; the app's own (local by default, the hosted composition's) when not passed (P9-2). */
  source?: HomeSource;
  /** Hosted only: the workspace links the hosted header carried (Team, Organization, Account). */
  barExtras?: ReactNode;
} = {}) {
  const navigate = useNavigate();
  const provided = useHomeSource();
  const source = given ?? provided;
  const [rows, setRows] = useState<ReviewDirectoryRow[] | null>(null);
  const [teamsSupported, setTeamsSupported] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [queueSupported, setQueueSupported] = useState(true);
  const [queueComplete, setQueueComplete] = useState(true);
  const [found, setFound] = useState<{ query: string; rows: ReviewDirectoryRow[] } | null>(null);
  const [sourceFirstRun, setSourceFirstRun] = useState<ReactNode>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now);
  const [prefs, setPrefs] = useState(loadPrefs);
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState("");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [open, setOpen] = useState<ReadonlySet<HomeSectionId>>(new Set());
  const [picker, setPicker] = useState<readonly string[] | null>(null);
  const [terminalFor, setTerminalFor] = useState("");
  const [firstRunCommand, setFirstRunCommand] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [live, setLive] = useState("");
  const [task, setTask] = useState<{ id: string; value: HeroTask | null } | null>(null);
  // A server that keeps no per-person queue state (PostgreSQL until its table lands) gets no snooze, so nothing is inert.
  const saveSnooze = queueSupported ? source.snooze : undefined;
  const searchRef = useRef<HTMLInputElement>(null);
  const rowRefs = useRef(new Map<string, HTMLElement>());
  const focusAfter = useRef("");
  const toast = useFlowToast();

  // -- data ---------------------------------------------------------------
  // A read that began before the person took, released or handed off a review may have been answered before the server
  // had it: its row must not overwrite what the server told them (two loads are in flight at mount and on every return).
  const clock = useRef(0);
  const written = useRef(new Map<string, { stamp: number; team: TeamState }>());
  /** The start of the newest read drawn so far: an older read answered after it is out of date, whatever it says. */
  const drawn = useRef(0);
  const load = useCallback(async () => {
    const began = ++clock.current;
    try {
      const result = await source.load();
      if (began < drawn.current) return;
      drawn.current = began;
      for (const [id, edit] of written.current) if (edit.stamp < began) written.current.delete(id);
      setRows(written.current.size
        ? result.rows.map((row) => { const edit = written.current.get(row.id); return edit ? { ...row, team: edit.team } : row; })
        : result.rows);
      setSourceFirstRun(result.firstRun ?? null);
      setTeamsSupported(result.teamsSupported);
      setTruncated(result.truncated);
      setTotal(result.total ?? null);
      setQueueSupported(result.queueSupported !== false);
      setQueueComplete(result.queueComplete !== false);
      setError("");
    } catch (reason) {
      if (began < drawn.current) return;
      setError(reason instanceof Error && reason.message ? reason.message : "Reviews couldn't load.");
    }
  }, [source]);
  useEffect(() => {
    void load();
    const onVisible = () => { if (document.visibilityState === "visible") void load(); };
    document.addEventListener("visibilitychange", onVisible);
    const tick = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => { document.removeEventListener("visibilitychange", onVisible); window.clearInterval(tick); };
  }, [load]);
  useEffect(() => { savePrefs(prefs); }, [prefs]);

  // Some reviews were left out of the read: a search asks the source, which looks through all of them.
  const needle = query.trim();
  useEffect(() => {
    if (!truncated || !needle || !source.search) { setFound(null); return; }
    let current = true;
    const timer = window.setTimeout(() => {
      source.search?.(needle).then((hits) => { if (current) setFound({ query: needle, rows: hits }); }).catch(() => { if (current) setFound(null); });
    }, 300);
    return () => { current = false; window.clearTimeout(timer); };
  }, [truncated, needle, source]);

  // -- the list -----------------------------------------------------------
  const listed = rows ?? [];
  // Rows the source found for this search that the first read left out; the source already matched them, so the local match is skipped.
  const hits = useMemo(() => (found && found.query === needle ? found.rows : []), [found, needle]);
  const hitIds = useMemo(() => new Set(hits.map((row) => row.id)), [hits]);
  const all = useMemo(() => {
    const seen = new Set(listed.map((row) => row.id));
    return [...listed, ...hits.filter((row) => !seen.has(row.id))];
  }, [listed, hits]);
  const { approvable: policyApprovable } = usePolicyApprovable(rows);
  const policyKey = [...policyApprovable].sort().join("\u0000");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const options = useMemo<HomeOptions>(() => ({ teamsSupported, policyApprovable }), [teamsSupported, policyKey]);
  const dense = isDense(listed, now, options);
  const firstRun = rows !== null && isFirstRun(listed);
  const filtersOn = filtersActive(prefs.filters, query);
  const visible = useMemo(
    () => sortRows(all, prefs.sort).filter((row) => matchesFilters(row, prefs.filters, hitIds.has(row.id) ? "" : query)),
    [all, prefs.sort, prefs.filters, query, hitIds],
  );
  const sections = useMemo(() => buildSections(visible, now, options), [visible, now, options]);
  const unfilteredTurn = useMemo(() => buildSections(all, now, options).find((s) => s.id === "yourTurn")?.rows.length ?? 0, [all, now, options]);
  const folded = (section: HomeSection) => FOLDED.includes(section.id) || (dense && section.id !== "yourTurn");
  const isOpen = (section: HomeSection) => !folded(section) || open.has(section.id);
  const shown = sections.filter(isOpen);
  // The hero is the next review to read; a turn on the viewer's own change (feedback, a failed check) has none to start.
  const hero = sections.find((s) => s.id === "yourTurn")?.rows.find((row) => !isAuthorTurn(row)) ?? null;
  const orderedIds = shown.flatMap((section) => section.rows.map((row) => row.id));
  const byId = useMemo(() => new Map(all.map((row) => [row.id, row])), [all]);
  const budget = budgetOf(sections.filter((s) => s.id === "yourTurn" || s.id === "cameBack").flatMap((s) => s.rows).filter((row) => !isAuthorTurn(row)));
  const queueClear = rows !== null && !firstRun && isQueueClear(sections) && !filtersOn;

  // The roving selection starts on the hero and survives a refresh while its row still exists.
  useEffect(() => {
    if (!orderedIds.length) { if (activeId) setActiveId(""); return; }
    if (!orderedIds.includes(activeId)) setActiveId(orderedIds[0]);
  }, [orderedIds.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps

  // The hero shows what the agent was asked, read from its session (untrusted text).
  useEffect(() => {
    if (!hero || !source.journal) return;
    if (task?.id === hero.id) return;
    let live = true;
    source.journal(hero.id)
      .then((journal) => { if (live) setTask({ id: hero.id, value: askedFromJournal(journal) }); })
      .catch(() => { if (live) setTask({ id: hero.id, value: null }); });
    return () => { live = false; };
  }, [hero?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // After Undo the restored row takes focus again.
  useEffect(() => {
    if (!focusAfter.current) return;
    const node = rowRefs.current.get(focusAfter.current);
    if (node) { node.focus(); focusAfter.current = ""; }
  });

  const setRowRef = (id: string) => (node: HTMLElement | null) => {
    if (node) rowRefs.current.set(id, node); else rowRefs.current.delete(id);
  };

  // -- actions --------------------------------------------------------------
  const announce = (text: string) => {
    setLive(text);
    window.setTimeout(() => setLive((current) => (current === text ? "" : current)), 2_000);
  };

  // Hosted only (SPEC 4.16): a review a teammate is in asks first (SPEC 4.1); taking it and handing it off.
  const teamActions = useTeamRowActions({
    rows: all,
    now,
    setTeam: (id, team) => {
      written.current.set(id, { stamp: ++clock.current, team });
      setRows((current) => (current ?? []).map((row) => (row.id === id ? { ...row, team } : row)));
    },
    open: (row) => navigate(continuePath(row)),
    toast: (next) => toast.show(next), // a toast is itself a status message: announcing it again would read it twice
    focusRow: (id) => { const node = rowRefs.current.get(id); if (node) node.focus(); else focusAfter.current = id; },
    another: (row) => {
      const other = orderedIds.find((id) => id !== row.id && !teamActions.asksFirst(byId.get(id) as ReviewDirectoryRow));
      if (other) { setActiveId(other); focusAfter.current = other; }
    },
  });
  const openRow = (row: ReviewDirectoryRow) => { setActiveId(row.id); teamActions.openRow(row); };

  const move = (delta: 1 | -1, extend: boolean) => {
    if (!orderedIds.length) return;
    const index = Math.max(0, orderedIds.indexOf(activeId));
    const next = orderedIds[Math.min(orderedIds.length - 1, Math.max(0, index + delta))];
    setActiveId(next);
    if (extend) setSelected((current) => new Set([...current, orderedIds[index], next]));
    rowRefs.current.get(next)?.focus();
    rowRefs.current.get(next)?.scrollIntoView?.({ block: "nearest" });
    const row = byId.get(next);
    if (row) announce(`${row.title}, ${orderedIds.indexOf(next) + 1} of ${orderedIds.length}`);
  };

  const toggle = (id: string) => setSelected((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const startSnooze = (ids: readonly string[]) => {
    if (!saveSnooze) return;
    const rowsFor = ids.map((id) => byId.get(id)).filter((row): row is ReviewDirectoryRow => Boolean(row));
    const able = rowsFor.filter((row) => canSnooze(row, now, options));
    if (able.length === 0) { toast.show({ text: "Only reviews waiting on you can be snoozed." }); return; }
    setPicker(able.map((row) => row.id));
  };

  const setQueue = (ids: readonly string[], snoozedUntil: string) => setRows((current) => (current ?? []).map((row) =>
    ids.includes(row.id) ? { ...row, queue: { snoozed_until: snoozedUntil, resume: row.queue?.resume ?? null } } : row));

  const snooze = async (option: SnoozeOption) => {
    const ids = picker ?? [];
    setPicker(null);
    if (!ids.length) return;
    const at = new Date();
    const until = snoozeUntil(option, at);
    const iso = until.toISOString();
    setQueue(ids, iso);
    setSelected(new Set());
    const results = await Promise.allSettled(ids.map((id) => (saveSnooze as NonNullable<HomeSource["snooze"]>)(id, iso)));
    const failed = ids.filter((_id, index) => results[index].status === "rejected");
    if (failed.length) setQueue(failed, "");
    const done = ids.filter((id) => !failed.includes(id));
    if (failed.length) toast.show({ text: `Couldn't snooze ${plural(failed.length, "review")} · try again`, tone: "alert" });
    if (done.length) {
      toast.show({
        text: `Snoozed until ${untilText(until, at)}`,
        undo: () => {
          setQueue(done, "");
          focusAfter.current = done[0];
          setActiveId(done[0]);
          void Promise.allSettled(done.map((id) => (saveSnooze as NonNullable<HomeSource["snooze"]>)(id, null))).then((undone) => {
            const stuck = done.filter((_id, index) => undone[index].status === "rejected");
            if (stuck.length) { setQueue(stuck, iso); toast.show({ text: "Couldn't undo the snooze · try again", tone: "alert" }); }
          });
        },
      });
    }
  };

  const copy = (command: string) => {
    const write = navigator.clipboard?.writeText(command);
    if (!write) { toast.show({ text: `Couldn't copy · run: ${command}`, tone: "alert" }); return; }
    write.then(() => toast.show({ text: "Command copied" }), () => toast.show({ text: `Couldn't copy · run: ${command}`, tone: "alert" }));
  };

  const focusSearch = () => { searchRef.current?.focus(); searchRef.current?.select(); };
  const setFilters = (filters: HomeFilters) => setPrefs((current) => ({ ...current, filters }));
  const clearFilters = () => { setFilters(NO_FILTERS); setQuery(""); };

  // -- keys (all through flowAction) ---------------------------------------------
  const live_ = useRef({ ownFirstRun: sourceFirstRun !== null, move, startSnooze, snooze, openRow, activeId, selected, firstRun, dense, byId, picker });
  live_.current = { ownFirstRun: sourceFirstRun !== null, move, startSnooze, snooze, openRow, activeId, selected, firstRun, dense, byId, picker };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const state = live_.current;
      const el = document.activeElement as HTMLElement | null;
      const context: FlowKeyContext = {
        screen: "home",
        surface: state.picker ? "snooze" : openFlowSurface(),
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
        firstRun: state.firstRun,
        dense: state.dense,
        hasSelection: state.selected.size > 0,
        searchFocused: el === searchRef.current,
      };
      const action = flowAction(event, context);
      if (!action) return;
      switch (action.intent) {
        case "help": event.preventDefault(); setShortcutsOpen(true); break;
        // ⌘K (intent "palette") belongs to the palette provider; Home only handles "/".
        case "searchFocus": event.preventDefault(); focusSearch(); break;
        case "clearSearch": event.preventDefault(); setQuery(""); break;
        case "rowNext": event.preventDefault(); state.move(1, action.extend === true); break;
        case "rowPrevious": event.preventDefault(); state.move(-1, action.extend === true); break;
        case "rowToggle": event.preventDefault(); if (state.activeId) toggle(state.activeId); break;
        case "clearSelection": event.preventDefault(); setSelected(new Set()); break;
        case "rowOpen": { const row = state.byId.get(state.activeId); if (row) { event.preventDefault(); state.openRow(row); } break; }
        case "start": if (!state.ownFirstRun) { event.preventDefault(); setFirstRunCommand(true); } break;
        case "snooze": event.preventDefault(); state.startSnooze(state.selected.size > 0 ? [...state.selected] : [state.activeId]); break;
        case "snoozePick": event.preventDefault(); if (action.option) void state.snooze(action.option); break;
        default: break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // -- view -------------------------------------------------------------------
  const chips = useMemo(() => chipCounts(all.filter((row) => row.status === "open")), [all]);
  const openPalette = usePaletteOpener();
  const snoozeIds = selected.size > 0 ? [...selected] : [activeId];
  useFlowPaletteScreen({
    place: { label: "Home", sub: "Reviews" },
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "home",
      home: {
        hasNext: hero !== null && !firstRun,
        selected: selected.size,
        canSnooze: !firstRun && Boolean(saveSnooze) && snoozeIds.some((id) => { const row = byId.get(id); return Boolean(row) && canSnooze(row as ReviewDirectoryRow, now, options); }),
        dense,
        repos: chips.repos.map((chip) => chip.name),
        activeRepos: prefs.filters.repos,
        sorts: SORT_CHOICES,
        sort: prefs.sort,
      },
      actions: {
        startNext: hero ? () => openRow(hero) : undefined,
        snooze: saveSnooze ? () => startSnooze(snoozeIds) : undefined,
        filterRepo: (repo) => { setFilters({ ...NO_FILTERS, repos: [repo] }); setQuery(""); },
        sort: (key) => { if (isSortKey(key)) setPrefs((current) => ({ ...current, sort: key })); },
      },
    },
  });
  const toggleChip = (kind: "repos" | "sources", name: string) => {
    const current = prefs.filters[kind] as readonly string[];
    const next = current.includes(name) ? current.filter((item) => item !== name) : [...current, name];
    setFilters({ ...prefs.filters, [kind]: next } as HomeFilters);
  };

  const bar = (
    <header className="sticky top-0 z-30 flex h-[52px] shrink-0 items-center gap-2 border-b border-border bg-background px-3 sm:px-4" data-testid="home-bar">
      <Link to="/reviews" aria-label="LemonCrow home" className="flex min-w-11 shrink-0 items-center justify-center gap-2 rounded-md font-mono text-small font-semibold uppercase tracking-wordmark">
        <img src={lemonCrowMark} alt="" aria-hidden="true" width={24} height={24} className="size-6 rounded-md" />
        <span className="hidden sm:inline">LemonCrow</span>
      </Link>
      {!firstRun ? (
        // On a phone with the palette, its button is the search (one search control, not a field and an icon).
        <div className={`relative mx-auto min-w-0 max-w-xl flex-1 items-center ${openPalette ? "hidden sm:flex" : "flex"}`}>
          <Search size={14} aria-hidden="true" className="pointer-events-none absolute left-2.5 text-muted-foreground" />
          <Input
            ref={searchRef}
            type="search"
            value={query}
            aria-label="Search reviews"
            placeholder="Search reviews, repos, people"
            className="h-9 pl-8 pr-14 pointer-coarse:h-11"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              // Enter in the search opens the palette with the query (SPEC 4.12).
              if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); openPalette?.(query); }
            }}
          />
          <kbd className="pointer-events-none absolute right-2 rounded border border-border px-1 font-mono text-tag text-muted-foreground pointer-coarse:hidden">⌘K</kbd>
        </div>
      ) : <div className="flex-1" />}
      <div className="ml-auto flex shrink-0 items-center gap-1">
        {openPalette ? (
          <Button variant="ghost" size="icon" className="min-w-11 sm:hidden" aria-label="Search and commands" aria-haspopup="dialog" onClick={() => openPalette()}>
            <Search size={16} aria-hidden="true" />
          </Button>
        ) : null}
        <NotificationBell />
        {source.savedFilters && !firstRun ? (
          <HomeSavedFilters
            api={source.savedFilters}
            filters={prefs.filters}
            query={query}
            rows={all}
            onApply={(repos, saved) => { setFilters({ ...NO_FILTERS, repos }); setQuery(saved); }}
          />
        ) : null}
        {barExtras}
        <Button variant="ghost" size="sm" className="hidden sm:inline-flex" aria-haspopup="dialog" aria-keyshortcuts="?" onClick={() => setShortcutsOpen(true)}>
          Shortcuts
          <kbd className="rounded border border-border px-1 font-mono text-tag">?</kbd>
        </Button>
        <Button variant="ghost" size="icon" aria-label="Review settings" onClick={() => navigate(source.settingsPath ?? "/settings/review")}>
          <Settings2 size={16} aria-hidden="true" />
        </Button>
      </div>
    </header>
  );

  // A source's own first run has no "Enter start": its one continuation is a link.
  const legendText = firstRun && sourceFirstRun ? "? all shortcuts" : homeLegend(firstRun, Boolean(saveSnooze));
  const legend = (
    <>
      <footer className="sticky bottom-0 z-20 shrink-0 border-t border-border bg-background px-[max(1rem,calc((100%-48rem)/2+1rem))] py-2 text-meta text-muted-foreground pointer-coarse:hidden" data-testid="home-legend">
        <span className="block truncate">{legendText}</span>
      </footer>
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={legendText} />
    </>
  );

  const shell = (children: React.ReactNode, mode: string) => (
    <div data-testid="review-home" data-home={mode} data-dense={dense ? "true" : "false"} className="flex min-h-dvh flex-col bg-background text-foreground">
      {bar}
      <FlowOfflineBanner />
      <AiBudgetBanner />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-5 pb-20">
        <h1 className="sr-only">Reviews</h1>
        {error && rows !== null ? (
          // A refresh that failed after a good first load keeps the rows on screen and says so.
          <div role="alert" data-testid="home-refresh-failed" className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-p1-line bg-p1-bg px-3 py-2 text-label">
            <span>These reviews may be out of date. {error}</span>
            <Button variant="outline" onClick={() => void load()}>Try again</Button>
          </div>
        ) : null}
        {children}
      </main>
      {/* Nothing to move through while the list is loading or failed: no key legend. */}
      {mode === "error" || mode === "loading" ? null : legend}
      <div aria-live="polite" role="status" data-testid="home-live" className="sr-only">{live}</div>
      {toast.element}
    </div>
  );

  if (rows === null) {
    return shell(
      error ? (
        <div role="alert" className="rounded-lg border border-p1-line bg-p1-bg p-4 text-label" data-testid="home-load-failed">
          {/^\d{3}$/.test(error.trim()) ? <>
            <p>Couldn't load your reviews. The local server returned an error.</p>
            <p className="mt-1 text-meta text-muted-foreground">Server answered {error.trim()}.</p>
          </> : <p>{withCode(error)}</p>}
          <Button variant="outline" className="mt-3" onClick={() => void load()}>Try again</Button>
        </div>
      ) : <p role="status" className="text-label text-muted-foreground">Loading reviews…</p>,
      error ? "error" : "loading",
    );
  }

  if (firstRun && sourceFirstRun) return shell(<section data-testid="home-first-run" className="mx-auto mt-10 max-w-md space-y-4">{sourceFirstRun}</section>, "first");

  if (firstRun) {
    return shell(
      <section data-testid="home-first-run" className="mx-auto mt-10 max-w-md space-y-4">
        <p data-fr="lede" className="text-lead">LemonCrow helps you review what your coding agent changed. You decide; AI only points.</p>
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-2 text-meta"><Badge>Local</Badge></div>
          <h2 className="mt-2 text-heading font-semibold">Review your last Claude Code session</h2>
          <Button variant="brand" className="mt-4" data-testid="home-start" onClick={() => setFirstRunCommand(true)}>
            Start review
            <kbd className="rounded border border-current px-1 font-mono text-tag pointer-coarse:hidden">Enter</kbd>
          </Button>
          {firstRunCommand ? <OpenInTerminal command="lc review" onCopy={copy} intro="Run this in your terminal. The review opens here when it is ready." /> : null}
        </div>
      </section>,
      "first",
    );
  }

  const turn = sections.find((section) => section.id === "yourTurn");
  const turnRows = turn ? turn.rows.filter((row) => row.id !== hero?.id) : [];
  // The team's open requests offer "I'll take this" and "Hand off" whichever section the row is in (a row the viewer is
  // also asked for sits in Your turn). Hosted only: the server sets `team` and the `assigned_to_my_teams` view.
  const teamNode = (row: ReviewDirectoryRow, section: HomeSectionId): ReactNode => row.team || section === "team" || teamRequested(row) ? (
    <TeamRowLine row={row} now={now} lineId={teamLineId(row)} actions={teamRequested(row)} onTake={() => teamActions.take(row)} onHandOff={() => { setActiveId(row.id); teamActions.handOff(row); }} onRelease={() => teamActions.release(row)} />
  ) : undefined;
  const rowProps = (section: HomeSectionId) => (row: ReviewDirectoryRow) => (
    <QueueRow
      key={row.id}
      ref={setRowRef(row.id)}
      row={row}
      section={section}
      now={now}
      dense={dense}
      active={activeId === row.id}
      selected={selected.has(row.id)}
      selectable={(section === "yourTurn" || section === "cameBack") && !isAuthorTurn(row)}
      href={section === "secondSignature" ? `${basePath(row)}/second` : continuePath(row)}
      basePath={basePath(row)}
      onToggle={() => toggle(row.id)}
      team={teamNode(row, section)}
      describedBy={teamLineText(row, now) ? teamLineId(row) : undefined}
      onOpen={(event) => { if (teamActions.asksFirst(row)) { event.preventDefault(); openRow(row); } }}
    />
  );

  return shell(
    <div className="space-y-6">
      {dense ? (
        <div data-testid="home-toolbar" className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Select aria-label="Sort" value={prefs.sort} uiSize="xs" className="text-meta sm:min-h-0" onValueChange={(value) => { if (isSortKey(value)) setPrefs((current) => ({ ...current, sort: value as SortKey })); }}>
              {SORT_CHOICES.map((choice) => <SelectItem key={choice.key} value={choice.key}>{choice.label}</SelectItem>)}
            </Select>
            {chips.repos.map((chip) => (
              <Button key={chip.name} size="xs" className="sm:min-h-0" variant="outline" aria-pressed={prefs.filters.repos.includes(chip.name)} active={prefs.filters.repos.includes(chip.name)} onClick={() => toggleChip("repos", chip.name)}>
                {chip.name} · {chip.count}
              </Button>
            ))}
            {chips.sources.map((chip) => (
              <Button key={chip.name} size="xs" className="sm:min-h-0" variant="outline" aria-pressed={prefs.filters.sources.includes(chip.name)} active={prefs.filters.sources.includes(chip.name)} onClick={() => toggleChip("sources", chip.name)}>
                {chip.name} · {chip.count}
              </Button>
            ))}
            {chips.cameBack > 0 || prefs.filters.cameBack ? <Button size="xs" className="sm:min-h-0" variant="outline" aria-pressed={prefs.filters.cameBack} active={prefs.filters.cameBack} onClick={() => setFilters({ ...prefs.filters, cameBack: !prefs.filters.cameBack })}>
              Came back · {chips.cameBack}
            </Button> : null}
            {filtersOn ? <Button size="xs" className="sm:min-h-0" variant="ghost" onClick={clearFilters}>Clear filters</Button> : null}
          </div>
        </div>
      ) : null}

      {truncated ? (
        <p role="status" data-testid="home-truncated" className="rounded-lg border border-p1-line bg-p1-bg px-3 py-2 text-label">
          {total !== null
            ? `Showing ${rows?.length ?? 0} of ${total} reviews.${queueComplete ? " Everything that waits on you is here" : ""}${source.search ? (queueComplete ? "; search" : " Search") + " looks through all of them" : ""}.`
            : "Showing the 500 most recently updated reviews. Older ones are left out."}
        </p>
      ) : null}

      {selected.size > 0 ? (
        <div data-testid="home-bulk" className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
          <span className="text-label font-medium">{selected.size} selected</span>
          {saveSnooze ? (
            <Button variant="outline" onClick={() => startSnooze([...selected])}>
              Snooze
              <kbd className="rounded border border-border px-1 font-mono text-tag pointer-coarse:hidden">S</kbd>
            </Button>
          ) : null}
          <Button variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      ) : null}

      {queueClear ? (
        <section data-testid="home-queue-clear" className="rounded-lg border border-border bg-card p-4 text-center">
          <CheckCircle2 size={20} aria-hidden="true" className="mx-auto text-ok" />
          <h2 className="mt-2 text-heading font-semibold">{dense ? "Queue clear" : "Nothing waits on you."}</h2>
          <p className="mt-1 text-label text-muted-foreground">
            {dense ? (source.queueClear ?? LOCAL_QUEUE_CLEAR).dense : (source.queueClear ?? LOCAL_QUEUE_CLEAR).sparse}
          </p>
        </section>
      ) : null}

      {sections.length === 0 && filtersOn ? (
        <p data-testid="home-no-match" className="text-label text-muted-foreground">
          No reviews match. <Button variant="link" onClick={clearFilters}>Clear filters</Button>
        </p>
      ) : null}

      {sections.map((section) => {
        const unfoldedRows = section.id === "yourTurn" ? turnRows : section.rows;
        return (
          <section key={section.id} aria-labelledby={`home-sec-${section.id}`} data-testid={`home-section-${section.id}`} className="space-y-2">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <h2 id={`home-sec-${section.id}`} className="text-heading font-semibold">{SECTION_TITLE[section.id]}</h2>
              {section.id === "yourTurn" ? (
                <p data-testid="home-budget" className="text-label text-muted-foreground">
                  {budgetLine(budget, dense)}
                  {dense && filtersOn ? ` · showing ${section.rows.length} of ${unfilteredTurn}` : ""}
                </p>
              ) : null}
            </div>
            {folded(section) && !isOpen(section) ? (
              <Button variant="outline" aria-expanded={false} onClick={() => setOpen((current) => new Set([...current, section.id]))}>
                {foldText(section)} · Show
              </Button>
            ) : (
              <>
                {folded(section) ? (
                  <Button variant="ghost" aria-expanded onClick={() => setOpen((current) => new Set([...current].filter((id) => id !== section.id)))}>
                    {foldText(section)} · Hide
                  </Button>
                ) : null}
                {section.id === "yourTurn" && hero ? (
                  <Hero
                    ref={setRowRef(hero.id)}
                    row={hero}
                    path={basePath(hero)}
                    now={now}
                    active={activeId === hero.id}
                    task={task?.id === hero.id ? task.value : null}
                    onStart={() => openRow(hero)}
                    onTerminal={source.terminal ? () => setTerminalFor((current) => (current === hero.id ? "" : hero.id)) : undefined}
                    onSnooze={saveSnooze ? () => { setActiveId(hero.id); startSnooze([hero.id]); } : undefined}
                    terminalOpen={terminalFor === hero.id}
                  >
                    {teamNode(hero, "yourTurn")}
                    {terminalFor === hero.id ? <OpenInTerminal command={openCommand(basePath(hero))} onCopy={copy} /> : null}
                  </Hero>
                ) : null}
                {unfoldedRows.length > 0 ? <ul className="space-y-2">{unfoldedRows.map(rowProps(section.id))}</ul> : null}
              </>
            )}
          </section>
        );
      })}

      {teamActions.element}
      <SnoozePicker open={picker !== null} count={picker?.length ?? 0} onPick={(option) => void snooze(option)} onClose={() => setPicker(null)} />
    </div>,
    "returning",
  );
}

const teamLineId = (row: ReviewDirectoryRow): string => `team-line-${row.id}`;
const teamLineText = (row: ReviewDirectoryRow, now: number): string => teamLine(row.team, now)?.text ?? "";

/** The team's open requests (SPEC 4.16): hosted only, as the server sets the view. */
function teamRequested(row: ReviewDirectoryRow): boolean {
  return row.status === "open" && (row.views ?? []).includes("assigned_to_my_teams");
}

function foldText(section: HomeSection): string {
  const count = section.rows.length;
  if (section.id === "finished") return `${count} finished`;
  if (section.id === "done") return `${count} approved by you`;
  if (section.id === "team") return `${plural(count, "review")} requested from your team`;
  return `${SECTION_TITLE[section.id]} · ${count}`;
}
