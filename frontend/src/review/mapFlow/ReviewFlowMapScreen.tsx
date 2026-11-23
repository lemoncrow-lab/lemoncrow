/** P3-3 — a snapshot-based map in the redesigned Review flow.
 * In contrast to the original Reader Map, this view never guesses an edge:
 * it renders the stored architecture comparison as returned by the server.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, GitBranch, List, Loader2, Map as MapIcon, Maximize2, Network, RotateCcw } from "lucide-react";
import { Button } from "../../components/ui/button";
import { fetchHistoricalReviewMap } from "../reviewApi";
import { isColdLoad } from "../reviewBootLocation";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf, useShortcutsKey } from "../FlowTopBar";
import { screenLegend } from "../flowKeymap";
import { counterFor } from "../focus/focusModel";
import ReviewViewTabs from "../ReviewViewTabs";
import { usePreviewCount } from "../usePreviewCount";
import { type MapLens, type MapView } from "../reviewRoutes";
import { useReadingViewSwitch, useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { partSlug } from "../overview/overviewModel";
import ProjectHistory from "../ProjectHistory";
import { aggregateModules, moduleCountsText } from "../scale/mapModules";
import { mapByModule } from "../scale/scaleTier";
import { useCompactReviewLayout } from "../reviewLayout";
import type { ReviewMapPayload, ReviewOverview, ReviewTarget } from "../types";
import {
  MAP_LANE_TITLES, buildFlowLayout, compactFlowLayout, dataPathAvailable, dataPathRunFailure, dataPathUnavailableText, edgePath, exampleMap,
  footerText, indexRefreshDelay, mapRowLabel, mapStateSymbol, mapStateText, nodeBasisText, provenanceCounts, relatedTarget, relationshipsIndexing, summaryLine,
  type MapNodeLayout, type MapState, type MapStep,
} from "./mapFlowModel";
import { fetchDataPathGate, postDataPathRun, type DataPathGate } from "./dataPathApi";
import { DATA_PATH_RUNNING, DataPathFlows, DataPathRunStrip, type DataPathRun } from "./DataPathPanel";

const STEP_KEY = "lemoncrow.review.map.context.v1";
const DISPLAY_KEY = "lemoncrow.review.map.display.v1";
const DASHED_KEY = "lemoncrow.review.map.unverified-connections.v1";

function readOption<T extends string>(key: string, values: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return values.find((candidate) => candidate === value) ?? fallback;
  } catch { return fallback; }
}
function saveOption(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* Storage disabled: the on-screen choice still works. */ }
}
const STATE_CLASSES: Record<MapState, string> = {
  added: "border-emerald-500/50 bg-emerald-500/5",
  removed: "border-red-500/50 border-dashed bg-red-500/5",
  changed: "border-amber-500/50 bg-amber-500/5",
  unchanged: "border-border bg-card",
  unverified: "border-dashed border-muted-foreground/40 bg-muted/30",
};
const STATE_MARKS: Record<MapState, string> = {
  // Semantic text tokens (lc-tokens.css): AA on the tints in Light and Dark. The legacy hue ramps in index.css are not.
  added: "text-ok",
  removed: "text-p0",
  changed: "text-p1",
  unchanged: "text-muted-foreground",
  unverified: "text-muted-foreground",
};
function StatusGlyph({ state }: { state: MapState }) {
  return <span aria-label={mapStateText(state)} title={mapStateText(state)}
    className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-current/20 font-mono text-label font-semibold ${STATE_MARKS[state]}`}>
    {mapStateSymbol(state)}
  </span>;
}
/** The two reads the Map makes: the architecture comparison first, then the plain change map. */
function loadMap(apiReviewId: string, revisionNumber: number, revisionId: string): Promise<ReviewMapPayload> {
  const exact = (payload: ReviewMapPayload) => {
    if (payload.revision_id !== revisionId) throw new Error("The map belongs to a different revision.");
    return payload;
  };
  // A server without architecture comparison can still provide a genuine
  // current-change map, but it must never claim Before/After comparison.
  return fetchHistoricalReviewMap(apiReviewId, revisionNumber, "architecture", "", "architecture_delta").then(exact)
    .catch(() => fetchHistoricalReviewMap(apiReviewId, revisionNumber).then(exact));
}
/**
 * The Map in two steps, for a screen that must not wait on the comparison: parsing both sides of a large repository takes
 * minutes the first time, the plain change map well under a second. `onPlain` shows the plain map until the comparison lands.
 */
function loadMapInStages(apiReviewId: string, revisionNumber: number, revisionId: string, onPlain: (payload: ReviewMapPayload) => void): Promise<ReviewMapPayload> {
  const exact = (payload: ReviewMapPayload) => {
    if (payload.revision_id !== revisionId) throw new Error("The map belongs to a different revision.");
    return payload;
  };
  let compared = false;
  const plain = fetchHistoricalReviewMap(apiReviewId, revisionNumber).then(exact);
  plain.then((payload) => { if (!compared) onPlain(payload); }, () => undefined);
  return fetchHistoricalReviewMap(apiReviewId, revisionNumber, "architecture", "", "architecture_delta").then(exact)
    .then((payload) => { compared = true; return payload; })
    .catch(() => plain);
}
export interface ReviewFlowMapScreenProps {
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  lens: MapLens;
  view: MapView;
  /** The whole parts list is loaded (the bar counts only from a complete list). */
  complete?: boolean;
  /** The Review is writable on its latest revision (neither read-only nor historical): on-request runs may start. */
  canRun?: boolean;
  /** "Ask about the change" (the top bar); absent when AI Ask is off. */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}
/** Files listed under an opened module; a module of thousands of files points to All files for the rest. */
const MODULE_FILE_CAP = 100;

export function ReviewFlowMapScreen({ reviewRef, apiReviewId, overview, targets, lens, view, complete = true, canRun = false, onAskChange, askPanel }: ReviewFlowMapScreenProps) {
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  useShortcutsKey(() => setShortcutsOpen(true));
  // ⌘K: Map has no keys of its own, so the palette offers only the shared groups (parts, reviews, repos, settings, help).
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Map"),
    openShortcuts: () => setShortcutsOpen(true),
    context: { screen: "other", actions: {} },
  });
  const previewCount = usePreviewCount(apiReviewId, overview.revision.id, targets);
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const readingView = useReadingViewSwitch(reviewRef, targets);
  const [result, setResult] = useState<{ revision: string; payload: ReviewMapPayload | null; error: string; comparing?: boolean }>({ revision: "", payload: null, error: "" });
  const revisionId = overview.revision.id;
  // Landed history is asked for by the files this change touches: the Map's nodes come and go with its controls.
  const changedPaths = useMemo(() => [...new Set(targets.map((target) => target.path))].sort(), [targets]);
  const heading = useRef<HTMLHeadingElement>(null);
  const [steps, setSteps] = useState<MapStep>(() => Number(readOption(STEP_KEY, ["0", "1", "2"], "1")) as MapStep);
  const [display, setDisplay] = useState<"map" | "list">(() => readOption(DISPLAY_KEY, ["map", "list"], "map"));
  const [example, setExample] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [showUnverified, setShowUnverified] = useState(() => readOption(DASHED_KEY, ["show", "hide"], "show") === "show");
  const [hot, setHot] = useState<string | null>(null);
  const [openModule, setOpenModule] = useState<string | null>(null);
  const [gate, setGate] = useState<{ revision: string; gate: DataPathGate | null } | null>(null);
  const [run, setRun] = useState<DataPathRun>({ state: "idle" });
  const mounted = useRef(true);
  const revisionNow = useRef(revisionId);
  revisionNow.current = revisionId;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  // A cold load keeps the browser's own start (SPEC 3.6); every move to Map puts focus on its heading.
  useEffect(() => { if (!isColdLoad()) heading.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    let live = true;
    setResult({ revision: revisionId, payload: null, error: "", comparing: true });
    setRun({ state: "idle" });
    loadMapInStages(apiReviewId, overview.revision.revision_number, revisionId, (plain) => { if (live) setResult({ revision: revisionId, payload: plain, error: "", comparing: true }); })
      .then((payload) => { if (live) setResult({ revision: revisionId, payload, error: "" }); })
      .catch(() => { if (live) setResult({ revision: revisionId, payload: null, error: "The code map could not load. Your review and answers are unchanged." }); });
    return () => { live = false; };
  }, [apiReviewId, revisionId, overview.revision.revision_number]);

  const examplePayload = useMemo(() => exampleMap(), []);
  const payload = example ? examplePayload : result.revision === revisionId ? result.payload : null;
  // The plain map is on screen and the comparison is still running: a Before or After link waits for it.
  const comparing = !example && result.revision === revisionId && result.comparing === true;
  const dataPathOk = payload !== null && !example && dataPathAvailable(payload);
  // The stored data path report of this revision; absent for the example and on a server without it.
  const report = payload && !example ? payload.data_path : undefined;
  // Whether the run plan runs the data path here is asked only while the report has no flows and the view may run it.
  const loaded = result.revision === revisionId ? result.payload : null;
  const askGate = canRun && Boolean(loaded?.data_path) && !(loaded && dataPathAvailable(loaded));
  useEffect(() => {
    if (!askGate) return;
    let live = true;
    fetchDataPathGate(apiReviewId, revisionId)
      .then((answer) => { if (live) setGate({ revision: revisionId, gate: answer }); })
      .catch(() => { if (live) setGate({ revision: revisionId, gate: null }); });
    return () => { live = false; };
  }, [askGate, apiReviewId, revisionId]);
  const runGate = gate?.revision === revisionId ? gate.gate : null;
  // Surrounding links may still be indexing when the Map opens (`context.readiness.graph`): the note says so, and the Map
  // reads itself again with backoff until the index is ready. `indexRefreshDelay` bounds the rounds, so polling always
  // stops; Check again then starts one more bounded round. Each settled read moves `tick`, failed reads included.
  const indexAttempts = useRef(0);
  const [indexPoll, setIndexPoll] = useState({ revision: "", tick: 0, stopped: false });
  const indexing = !example && !comparing && loaded !== null && relationshipsIndexing(loaded);
  const indexStopped = indexPoll.revision === revisionId && indexPoll.stopped;
  const loadedProjection = useRef(loaded?.projection);
  loadedProjection.current = loaded?.projection;
  useEffect(() => {
    if (!indexing) { indexAttempts.current = 0; return; }
    if (indexStopped) return;
    const delay = indexRefreshDelay(indexAttempts.current + 1);
    if (delay === null) { setIndexPoll({ revision: revisionId, tick: 0, stopped: true }); return; }
    indexAttempts.current += 1;
    let live = true;
    const revisionNumber = overview.revision.revision_number;
    const timer = window.setTimeout(() => {
      // The same read the screen holds: a comparison stays a comparison, the plain map stays plain.
      const again = loadedProjection.current === "architecture_delta"
        ? fetchHistoricalReviewMap(apiReviewId, revisionNumber, "architecture", "", "architecture_delta")
        : fetchHistoricalReviewMap(apiReviewId, revisionNumber);
      again.then((next) => { if (live && next.revision_id === revisionId) setResult({ revision: revisionId, payload: next, error: "" }); })
        .catch(() => undefined) // A background read is best effort: the map on screen stays.
        .finally(() => { if (live) setIndexPoll((current) => ({ revision: revisionId, tick: current.tick + 1, stopped: false })); });
    }, delay);
    return () => { live = false; window.clearTimeout(timer); };
  }, [indexing, indexStopped, indexPoll.tick, apiReviewId, revisionId, overview.revision.revision_number]);
  const checkIndexAgain = () => { indexAttempts.current = 0; setIndexPoll({ revision: revisionId, tick: 0, stopped: false }); };
  // The default route lens is data-path; with no data-path facts the screen shows the Dependencies lens instead.
  const effectiveLens: MapLens = lens === "data-path" && !dataPathOk ? "dependencies" : lens;
  // A large change (SPEC 4.15) lists modules, not files; the count is the complete parts list, never a startup slice.
  const moduleLevel = !example && complete && mapByModule(targets.length);
  const agg = useMemo(() => moduleLevel && payload ? aggregateModules(payload, targets) : null, [moduleLevel, payload, targets]);
  const layout = useMemo(() => payload ? buildFlowLayout(agg ? agg.payload : payload, example || agg ? [] : targets, effectiveLens, comparing ? "changes" : view, steps) : null,
    [payload, agg, targets, effectiveLens, view, steps, example, comparing]);
  // Phone (review-12.4): the diagram comes first, drawn compact to the width it has, so it never scrolls sideways.
  const compact = useCompactReviewLayout();
  const [canvasWidth, setCanvasWidth] = useState(0);
  const canvasObserver = useRef<ResizeObserver | null>(null);
  const measureCanvas = useCallback((element: HTMLDivElement | null) => {
    canvasObserver.current?.disconnect();
    canvasObserver.current = null;
    if (!element) return;
    setCanvasWidth(element.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    canvasObserver.current = new ResizeObserver(() => setCanvasWidth(element.clientWidth));
    canvasObserver.current.observe(element);
  }, []);
  // Before the first measure: the screen less the page padding and the card's border.
  const compactWidth = canvasWidth > 0 ? canvasWidth : typeof window === "undefined" ? 0 : Math.max(0, window.innerWidth - 34);
  const drawn = useMemo(() => layout && compact && compactWidth > 0 ? compactFlowLayout(layout, compactWidth) : layout, [layout, compact, compactWidth]);
  const bands = drawn?.bands;
  const changedModules = agg ? [...agg.stats.values()].filter((row) => row.changedFiles > 0).length : 0;
  const comparable = layout?.comparable === true;
  const wantView: MapView = layout && !example && !comparable && !comparing ? "changes" : view;
  useEffect(() => {
    if (layout && !example && (effectiveLens !== lens || wantView !== view)) {
      // A stale deep link must never display an invented old/new comparison or an empty lens. The redirect
      // replaces the entry, so Back leaves the Map instead of returning to the link that was just corrected.
      goTo({ kind: "map", lens: effectiveLens, view: wantView }, { replace: true });
    }
  }, [layout !== null, example, effectiveLens, lens, wantView, view, goTo]);
  const visibleRows = useMemo(() => layout?.nodes.filter((n) => n.visible && n.active) ?? [], [layout]);
  const counts = useMemo(() => layout ? provenanceCounts(layout, showUnverified) : { checked: 0, notVerified: 0 }, [layout, showUnverified]);
  const unmatched = useMemo(() => {
    // Per node: how many shown connections are not verified (name-matched or only in the current index).
    const byNode = new Map<string, number>();
    for (const edge of layout?.edges ?? []) {
      if (!edge.visible || !edge.active || edge.basis === "checked") continue;
      byNode.set(edge.from.id, (byNode.get(edge.from.id) ?? 0) + 1);
      byNode.set(edge.to.id, (byNode.get(edge.to.id) ?? 0) + 1);
    }
    return byNode;
  }, [layout]);
  // The example's own banner already says it is illustrative, so the note under the controls stays empty for it.
  const dataNote = example ? ""
    : effectiveLens === "data-path" && report ? report.disclosure
    // Until the map has answered there is no report: "not available" would be a claim about nothing.
    // With a report, the run strip below already says why the lens is off (its setup steps folded), so the note does not repeat it.
    : lens === "data-path" && !dataPathOk && payload && !report ? dataPathUnavailableText(report)
    : comparing ? "Showing this revision's connections while the comparison with the base finishes. On a large repository the first comparison takes a few minutes."
    : !comparable ? "Before and After require a verified comparison. Showing the available code relationships without claiming how they changed."
    : layout?.edges.length === 0 ? "No verified connection of this kind was returned. Switch lenses to see other relationships."
    : payload?.context.source !== "current_index" ? "Surrounding call context is not available. Only the stored code comparison is shown."
    : "This map uses recorded code relationships. Connections from a current index are not proof that they existed in the captured revision.";

  const select = (n: MapNodeLayout) => {
    if (example) return;
    if (agg) { setOpenModule((current) => current === n.node.module ? null : n.node.module ?? null); setHot(n.id); return; }
    const target = n.target ?? relatedTarget(n.node, targets);
    if (target) goTo({ kind: "all", part: partSlug(target, targets) });
    else setHot(n.id);
  };
  const changeView = (next: MapView) => {
    if ((next === "before" || next === "after") && !comparable && !example) return;
    goTo({ kind: "map", lens: effectiveLens, view: next });
  };
  const changeLens = (next: MapLens) => goTo({ kind: "map", lens: next, view: wantView });
  const changeNameMatched = (show: boolean) => { setShowUnverified(show); saveOption(DASHED_KEY, show ? "show" : "hide"); };
  // One live region for the whole screen: it speaks load, view and depth changes, never hover or focus.
  // It is empty when the screen opens (SPEC 5.3, A11Y-3): the first summary is what the heading and map already show.
  const summary = !payload ? ""
    : effectiveLens === "data-path" && report ? `Map · Data path · ${report.summary}`
    : `${example ? "Example map" : "Map"} · ${wantView === "changes" ? "Changes" : wantView === "before" ? "Before" : "After"} · follow ${steps} · ${visibleRows.length} ${agg ? "modules" : "parts"} shown`;
  const summaryOnArrival = useRef<string | null>(null);
  if (summaryOnArrival.current === null && summary) summaryOnArrival.current = summary;
  const summaryNow = useRef(summary);
  summaryNow.current = summary;
  // A data path run speaks its progress and outcome until the summary moves on (the lens it enabled, or the user).
  const runSpoken = run.state !== "idle" && run.summary === summary ? run.spoken : "";
  const announce = result.error && !payload ? result.error : runSpoken || (summary === summaryOnArrival.current ? "" : summary);
  const changeSteps = (next: MapStep) => { setSteps(next); saveOption(STEP_KEY, String(next)); };
  const changeDisplay = (next: "map" | "list") => { setDisplay(next); saveOption(DISPLAY_KEY, next); };
  const runDataPath = () => {
    if (run.state === "running") return;
    const revision = revisionId;
    const revisionNumber = overview.revision.revision_number;
    const current = () => mounted.current && revisionNow.current === revision;
    const settle = (state: "done" | "failed", spoken: string) => { if (current()) setRun({ state, spoken, summary: summaryNow.current }); };
    setRun({ state: "running", spoken: DATA_PATH_RUNNING, summary: summaryNow.current });
    postDataPathRun(apiReviewId, revision).then(
      // The stored report reaches the lens through the map payload, so the map is read again.
      () => loadMap(apiReviewId, revisionNumber, revision).then((next) => {
        if (!current()) return;
        setResult({ revision, payload: next, error: "" });
        const ran = dataPathAvailable(next);
        settle("done", ran ? `The data path finished: ${next.data_path?.summary ?? ""}.` : `The data path finished. ${dataPathUnavailableText(next.data_path)}`);
        if (ran) goTo({ kind: "map", lens: "data-path", view: wantView });
      }, () => settle("failed", "The data path ran, but the map could not reload. Reload the page to see its result.")),
      (error: unknown) => settle("failed", dataPathRunFailure(error)),
    );
  };

  const counter = counterFor(targets, "", complete);

  return <section className="flex min-w-0 flex-1 flex-col" data-testid="flow-map-screen">
    <p className="sr-only" role="status" aria-live="polite" data-testid="map-live">{announce}</p>
<FlowTopBar
      repo={repoNameOf(overview)}
      documentTitle="Map"
      title={overview.title}
      revisionNumber={overview.revision.revision_number}
      back={{ text: "Overview", onClick: () => goTo({ kind: "overview" }) }}
      view={readingView}
      counter={counter}
      status={counter ? undefined : { text: "Loading all parts…" }}
      onAsk={onAskChange}
      onShortcuts={() => setShortcutsOpen(true)}
    />
    <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("map", { aiAsk: false })} />
    <ReviewViewTabs reviewRef={reviewRef} current="map" previewCount={previewCount}/>
    <div role="region" className="min-w-0 flex-1" data-review-scroll-root="map" aria-label="Review map">
      <div className="mx-auto max-w-[1560px] space-y-5 p-4 pb-14 sm:p-6" data-testid="flow-map-content">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 ref={heading} tabIndex={-1} className="text-title font-semibold outline-none">Map</h1>
            <p className="mt-1 max-w-2xl text-label text-muted-foreground">See what changed, how the pieces connect, and follow a part back to the actual code.</p>
          </div>
          {/* review-12.2: a synthetic map has no place on a real review; only the demo's review offers it. */}
          {overview.example_review === true && <Button variant={example ? "solid" : "outline"} size="sm"
            onClick={() => { setExample((prev) => !prev); setHot(null); }} data-testid="map-bigger-example">
            {example ? <><RotateCcw size={16} aria-hidden="true"/> Back to this change</> : <><Maximize2 size={16} aria-hidden="true"/> See a bigger-change example</>}
          </Button>}
        </div>
        {example && <div className="rounded-md border border-amber-500/50 bg-amber-500/5 p-3 text-label" data-testid="map-example-warning"><strong>Illustrative example only.</strong> The files and connections below are synthetic. They were not checked in your repository.</div>}
        {/* Phone: the controls fold behind one button that names what is shown, so the diagram is on the first screen (review-12.4). */}
        <Button variant="outline" size="sm" className="sm:hidden" aria-expanded={optionsOpen} aria-controls="map-controls" data-testid="map-options-toggle"
          onClick={() => setOptionsOpen((open) => !open)}>
          {optionsOpen ? "Hide map options" : `Map options · ${effectiveLens === "data-path" ? "Data path" : "Dependencies"} · ${wantView === "changes" ? "Changes" : wantView === "before" ? "Before" : "After"} · follow ${steps}`}
        </Button>
        <div id="map-controls" className={`flex flex-wrap items-center gap-x-5 gap-y-3 rounded-xl border border-border bg-card p-3 sm:p-4 ${optionsOpen ? "" : "max-sm:hidden"}`}>
          <fieldset className="flex flex-wrap items-center gap-1" aria-label="Map lens">
            <legend className="mb-1 text-meta font-medium text-muted-foreground">Lens</legend>
            <Button size="sm" variant={effectiveLens === "data-path" ? "solid" : "ghost"} aria-pressed={effectiveLens === "data-path"} onClick={() => changeLens("data-path")}
              disabled={!dataPathOk} tooltip={!dataPathOk ? dataPathUnavailableText(report) : undefined}><GitBranch size={16} aria-hidden="true"/> Data path</Button>
            <Button size="sm" variant={effectiveLens === "dependencies" ? "solid" : "ghost"} aria-pressed={effectiveLens === "dependencies"} onClick={() => changeLens("dependencies")}><Network size={16} aria-hidden="true"/> Dependencies</Button>
            <Button size="sm" variant="ghost" disabled tooltip="No data-model changes in this change">Data model</Button>
            <Button size="sm" variant="ghost" disabled tooltip="No API changes in this change">API</Button>
          </fieldset>
          <div className="hidden h-9 border-l border-border md:block"/>
          <fieldset className="flex items-center gap-1" aria-label="Map view">
            <legend className="mb-1 text-meta font-medium text-muted-foreground">View</legend>
            {(["changes", "before", "after"] as const).map((option) =>
              <Button key={option} size="sm" variant={wantView === option ? "solid" : "ghost"} aria-pressed={wantView === option}
                disabled={option !== "changes" && !comparable} title={option !== "changes" && !comparable ? "Old/new graph comparison not available" : undefined}
                onClick={() => changeView(option)} className="capitalize">{option === "changes" ? "Changes" : option === "before" ? "Before" : "After"}</Button>)}
          </fieldset>
          <fieldset className="flex items-center gap-1" aria-label="How far to follow" data-testid="map-context-control">
            <legend className="mb-1 text-meta font-medium text-muted-foreground">How far to follow</legend>
            {([0, 1, 2] as const).map((s) => <Button key={s} size="sm" variant={steps === s ? "solid" : "ghost"} aria-pressed={steps === s}
              onClick={() => changeSteps(s)} className="min-w-8">{s}</Button>)}
          </fieldset>
          <fieldset className="flex items-center gap-1" aria-label="Guessed connections" data-testid="map-name-matched-control">
            <legend className="mb-1 text-meta font-medium text-muted-foreground">Guessed connections</legend>
            <Button size="sm" variant={showUnverified ? "solid" : "ghost"} aria-pressed={showUnverified} onClick={() => changeNameMatched(true)}>Show dashed</Button>
            <Button size="sm" variant={!showUnverified ? "solid" : "ghost"} aria-pressed={!showUnverified} onClick={() => changeNameMatched(false)}>Hide</Button>
          </fieldset>
          <div className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" data-testid="map-display-toggle"
              onClick={() => changeDisplay(display === "map" ? "list" : "map")}>
              {display === "map" ? <><List size={16} aria-hidden="true"/> Show as list</> : <><MapIcon size={16} aria-hidden="true"/> Show map</>}
            </Button>
          </div>
        </div>
        {dataNote && <p className="break-words text-label text-muted-foreground" data-testid="map-provenance-note">{dataNote}</p>}
        {indexing && <p className="flex flex-wrap items-center gap-2 text-label text-muted-foreground" data-testid="map-indexing-note" data-stopped={indexStopped ? "true" : undefined}>
          {!indexStopped && <Loader2 size={14} className="shrink-0 animate-spin" aria-hidden="true"/>}
          <span>Still indexing the surrounding code, so some connections may be missing.{indexStopped ? "" : " Checking again…"}</span>
          {indexStopped && <Button variant="outline" size="xs" onClick={checkIndexAgain}>Check again</Button>}
        </p>}
        {report && !dataPathOk && <DataPathRunStrip report={report} canRun={canRun} gate={runGate} run={run} onRun={runDataPath}/>}
        {!payload && !result.error && <div className="rounded-xl border border-border bg-card p-10 text-center text-label text-muted-foreground">Loading the captured code map…</div>}
        {!payload && result.error && <div className="rounded-xl border border-border bg-card p-6">
          <p className="text-label">{result.error}</p><Button variant="outline" className="mt-3" onClick={() => goTo({ kind: "all", part: "" })}>Read the code instead</Button>
        </div>}
        {effectiveLens === "data-path" && report && <DataPathFlows report={report}/>}
        {layout && payload && effectiveLens !== "data-path" && <>
          {layout.truncated && <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-label">This graph was truncated by the source. Counts reflect only the returned part of the graph.</p>}
          <div className={`grid min-w-0 gap-4 ${display === "map" ? "xl:grid-cols-[minmax(0,1.55fr)_minmax(320px,0.8fr)]" : "grid-cols-1"}`}>
            {/* The diagram comes first: on phone it is drawn compact to the screen width with the list below it (review-12.4); on desktop the list stays beside it. */}
            {display === "map" && drawn && <section aria-label="Code connection diagram" className="min-w-0 rounded-xl border border-border bg-card" data-testid="map-canvas" data-map-compact={bands ? "true" : undefined}>
              <div className="border-b border-border px-4 py-3">
                <h2 className="text-heading font-semibold">Connection diagram</h2>
              </div>
              <div ref={measureCanvas} className="max-w-full overflow-x-auto" data-testid="map-scroll">
                <div className="relative" style={{ width: drawn.width, height: drawn.height }} data-testid="map-stable-layout">
                  {bands ? bands.map((band) => <span key={band.lane} className="pointer-events-none absolute left-2 font-mono text-tag text-muted-foreground" style={{ top: band.y + 6 }} data-testid="map-band">{MAP_LANE_TITLES[band.lane]}</span>)
                    : <div className="pointer-events-none absolute inset-x-0 top-3 flex justify-around font-mono text-tag text-muted-foreground">
                      <span>{MAP_LANE_TITLES.used_by}</span><span>{MAP_LANE_TITLES.subject}</span><span>{MAP_LANE_TITLES.uses}</span>
                    </div>}
                  <svg className="pointer-events-none absolute inset-0" viewBox={`0 0 ${drawn.width} ${drawn.height}`} width={drawn.width} height={drawn.height} aria-hidden="true">
                    <defs><marker id="map-flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="currentColor" opacity=".55"/></marker></defs>
                    {drawn.edges.filter((edge) => edge.visible && (showUnverified || edge.basis === "checked")).map((edge) => {
                      const highlighted = hot === edge.from.id || hot === edge.to.id;
                      return <path key={edge.id} data-testid="map-edge" data-edge-id={edge.id} data-active={edge.active} data-basis={edge.basis}
                        d={edgePath(edge.from, edge.to, Boolean(bands))}
                        fill="none" stroke="currentColor" strokeWidth={highlighted ? 2.5 : 1.4}
                        strokeDasharray={edge.state === "removed" || edge.basis !== "checked" ? "5 5" : undefined}
                        opacity={edge.active ? highlighted ? 0.75 : 0.28 : 0} markerEnd="url(#map-flow-arrow)"/>;
                    })}
                  </svg>
                  {drawn.nodes.filter((node) => node.visible).map((row) => {
                    const highlighted = hot === row.id;
                    const clickable = !example && !agg && Boolean(row.target ?? relatedTarget(row.node, targets));
                    return <Button key={row.id} unstyled data-testid="map-node" data-node-id={row.id} data-map-view-visible={String(row.active)}
                      aria-label={`${mapRowLabel(row.node)}, ${mapStateText(row.state)}`}
                      aria-hidden={!row.active ? true : undefined} tabIndex={!row.active ? -1 : 0}
                      className={`absolute flex min-w-0 items-start gap-2 overflow-hidden rounded-lg border ${bands ? "p-2" : "p-2.5"} text-left transition-colors ${STATE_CLASSES[row.state]} ${highlighted ? "ring-2 ring-foreground/40" : ""} ${row.active ? "" : "pointer-events-none opacity-0"}`}
                      style={{ left: row.x, top: row.y, width: row.width, height: row.height }}
                      onMouseEnter={() => setHot(row.id)} onMouseLeave={() => setHot(null)} onFocus={() => setHot(row.id)} onBlur={() => setHot(null)}
                      onClick={() => select(row)}>
                      <StatusGlyph state={row.state}/>
                      <span className="min-w-0 flex-1 overflow-hidden">
                        <span className={`block truncate text-label font-semibold ${row.state === "removed" ? "line-through" : ""}`}>{mapRowLabel(row.node)}</span>
                        {/* Phone nodes keep one line under the name; the list below carries the rest. */}
                        {!agg && !(bands && example) && <span className="block truncate font-mono text-tag tracking-normal text-muted-foreground">{row.node.path}</span>}
                        {!(bands && !agg && !example) && <span className="block truncate text-tag text-muted-foreground">{example ? "Example data" : agg ? moduleCountsText(agg.stats.get(row.node.module ?? "")!) : clickable ? "Open changed code" : nodeBasisText(row.node)}</span>}
                      </span>
                    </Button>;
                  })}
                  {drawn.folds.map((fold) => steps < 2
                    ? <Button key={fold.lane} unstyled data-testid="map-fold" onClick={() => changeSteps(2)}
                      className="absolute flex items-center justify-center rounded-lg border border-dashed border-border text-label text-muted-foreground hover:bg-muted/40"
                      style={{ left: fold.x, top: fold.y, width: fold.width, height: fold.height }}>+{fold.count} unchanged</Button>
                    : <span key={fold.lane} data-testid="map-fold" className="absolute flex items-center justify-center rounded-lg border border-dashed border-border text-label text-muted-foreground"
                      style={{ left: fold.x, top: fold.y, width: fold.width, height: fold.height }}>+{fold.count} unchanged</span>)}
                </div>
              </div>
            </section>}
            <section aria-label="Map changes, as a list" className="min-w-0 rounded-xl border border-border bg-card" data-testid="map-list">
              <div className="border-b border-border p-4">
                <h2 className="text-heading font-semibold" data-testid={agg ? "map-module-heading" : undefined}>{agg ? `Modules · ${changedModules} changed` : "Changes, as a list"}</h2>
                <p className="mt-1 text-label font-medium tabular-nums" data-testid="map-summary">{summaryLine(layout, example)}</p>
                <p className="mt-1 text-meta text-muted-foreground">{visibleRows.length} shown · {layout.folded} unchanged folded</p>
              </div>
              <ol className="max-h-[720px] overflow-y-auto" aria-label="Mapped code parts">
                {visibleRows.map((row, i) => {
                  const stats = agg?.stats.get(row.node.module ?? "");
                  const expandable = Boolean(agg && stats && (stats.changedFiles > 0 || stats.parts > 0));
                  const expanded = expandable && openModule === row.node.module;
                  const linkable = !agg && !example && Boolean(row.target ?? relatedTarget(row.node, targets));
                  const active = hot === row.id;
                  const open = unmatched.get(row.id) ?? 0;
                  return <li key={row.id} data-node-id={row.id} className={`border-b border-border/70 ${active ? "bg-accent" : "hover:bg-muted/40"}`}>
                    <Button unstyled className="flex w-full min-h-14 min-w-0 items-start gap-3 p-3 text-left" data-testid="map-list-row"
                      aria-label={`${mapRowLabel(row.node)}, ${mapStateText(row.state)}`}
                      aria-current={active ? "true" : undefined}
                      aria-expanded={expandable ? expanded : undefined}
                      onMouseEnter={() => setHot(row.id)} onMouseLeave={() => setHot(null)}
                      onFocus={() => setHot(row.id)} onBlur={() => setHot(null)}
                      onClick={() => select(row)}>
                      <span className="w-5 shrink-0 pt-1 font-mono text-tag text-muted-foreground tabular-nums">{i + 1}</span>
                      <StatusGlyph state={row.state}/>
                      <span className="min-w-0 flex-1">
                        <span className={`block break-all text-label font-semibold ${row.state === "removed" ? "line-through decoration-red-500/70" : ""}`}>{mapRowLabel(row.node)}</span>
                        {agg && stats ? <span className="mt-0.5 block text-tag text-muted-foreground" data-testid="map-module-counts">{moduleCountsText(stats)}</span>
                          : <span className="mt-0.5 block break-all font-mono text-tag tracking-normal text-muted-foreground">{row.node.path}</span>}
                        <span className="mt-1 block text-meta text-muted-foreground">{agg ? [agg.beyondMap.has(row.node.module ?? "") ? "Not verified · the server's map did not include this module" : agg.leftOut.has(row.node.module ?? "") ? `Not verified · the server's map left out ${agg.leftOut.get(row.node.module ?? "")} of ${stats?.files} files` : nodeBasisText(row.node), expandable ? (expanded ? "Hide files" : "Show files") : ""].filter(Boolean).join(" · ") : example ? "Example data · not checked in your code" : nodeBasisText(row.node)}{agg ? "" : <> · {row.node.line > 0 ? `line ${row.node.line}` : "file"}{linkable ? " · Open part" : example ? "" : " · no part to open"}</>}</span>
                        {open > 0 && showUnverified && <span className="block text-meta text-muted-foreground">{open} {open === 1 ? "connection" : "connections"} not verified · dashed on the map</span>}
                      </span>
                      {linkable && <ArrowRight size={16} className="mt-2 shrink-0 text-muted-foreground" aria-hidden="true"/>}
                    </Button>
                    {expanded && <ul className="border-t border-border/70 bg-muted/20" aria-label={`Files in ${row.node.label}`} data-testid="map-module-files">
                      {(agg?.members.get(row.node.module ?? "") ?? []).slice(0, MODULE_FILE_CAP).map((file) => {
                        const part = file.part;
                        return <li key={file.path}>
                          <Button unstyled disabled={!part} data-testid="map-module-file" className="flex w-full min-w-0 items-start gap-2 py-2 pl-10 pr-3 text-left"
                            onClick={() => { if (part) goTo({ kind: "all", part: partSlug(part, targets) }); }}>
                            <span className="min-w-0 flex-1 break-all font-mono text-tag tracking-normal">{file.path}</span>
                            <span className="shrink-0 text-meta text-muted-foreground">{part ? "Open part" : "No part to open"}</span>
                          </Button>
                        </li>;
                      })}
                      {(agg?.members.get(row.node.module ?? "") ?? []).length > MODULE_FILE_CAP && <li className="p-3 pl-10 text-meta text-muted-foreground" data-testid="map-module-more">{(agg?.members.get(row.node.module ?? "") ?? []).length - MODULE_FILE_CAP} more files in this module. Open All files to reach them.</li>}
                    </ul>}
                  </li>;
                })}
                {layout.folded > 0 && <li className="p-3 text-meta text-muted-foreground" data-testid="map-list-folded">+{layout.folded} unchanged</li>}
                {!visibleRows.length && <li className="p-5 text-label text-muted-foreground">No connections are available for this lens this far out. Choose another lens or set How far to follow to 2.</li>}
              </ol>
            </section>
          </div>
          <footer className="rounded-xl border border-border bg-card p-4" data-testid="map-footer">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-label font-medium">{example ? "Illustrative example · not verified against your code" : footerText(payload, counts)}</p>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-meta text-muted-foreground" aria-label="Map legend">
                <span className="inline-flex items-center gap-1"><svg width="28" height="8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke="currentColor" strokeWidth="1.5"/></svg>checked in code</span>
                <span className="inline-flex items-center gap-1"><svg width="28" height="8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke="currentColor" strokeWidth="1.5" strokeDasharray="5 4"/></svg>not verified</span>
                <span className="inline-flex items-center gap-1"><StatusGlyph state="added"/>new</span>
                <span className="inline-flex items-center gap-1"><StatusGlyph state="changed"/>changed</span>
                <span className="inline-flex items-center gap-1"><StatusGlyph state="removed"/>removed</span>
              </div>
            </div>
            <p className="mt-2 text-meta text-muted-foreground">Maps are drawn only from code we checked. AI never adds a connection. Viewing a map does not mark code reviewed.</p>
          </footer>
          {!example && <ProjectHistory apiReviewId={apiReviewId} revisionNumber={overview.revision.revision_number} purpose="map" paths={changedPaths}/>}
        </>}
      </div>
    </div>
    <FlowAskOverlay>{askPanel}</FlowAskOverlay>
  </section>;
}

export default ReviewFlowMapScreen;
