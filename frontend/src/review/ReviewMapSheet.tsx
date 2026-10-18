import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronRight, Compass, Eye, EyeOff, Loader2, Network, Search, X } from "lucide-react";

import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { ToggleGroup } from "../components/ui/toggle-group";
import { explainHistoricalReviewArchitecture, fetchHistoricalReviewMap } from "./reviewApi";
import {
  REVIEW_MAP_OVERVIEW,
  reviewMapLocationKey,
  sameReviewMapLevel,
  type ReviewMapLocation,
} from "./reviewMapLocation";
import {
  buildMapModel,
  hiddenAsTest,
  isChangedStatus,
  isTestNode,
  localFileFocus,
  nodeName,
  searchMap,
  type LinkMode,
  type MapChapter,
  type SearchHit,
} from "./reviewMapModel";
import ReviewMapCanvas from "./ReviewMapCanvas";
import ReviewMapInspector, { type CodeTarget } from "./ReviewMapInspector";
import type {
  ReviewArchitectureExplanation,
  ReviewMapLevel,
  ReviewMapMemberSymbol,
  ReviewMapPayload,
  ReviewMapProjection,
  ReviewMapSelection,
  ReviewProjectContext,
  ReviewProjectContextSelection,
} from "./types";

interface ReviewMapSheetProps {
  reviewId: string;
  revisionNumber: number;
  historical: boolean;
  /** URL-owned view; Back/Forward and Ask references move the map through it. */
  location?: ReviewMapLocation | null;
  onLocationChange?(location: ReviewMapLocation, mode: "push" | "replace"): void;
  /** Dedicated diff URL for a selection in the patch; opened in a new tab so the map stays put. */
  selectionHref?(selection: ReviewMapSelection): string | null;
  /** Leaves the Map view (Escape at the top of the drill-down). */
  onClose(): void;
  /** Fallback for selections without a diff URL, such as Snapshot source outside the patch. */
  onOpenSelection(selection: ReviewMapSelection): void;
  onAskSelection?(selection: ReviewMapSelection, label: string): void;
  /** Shared Ask conversation shown within the Map's right panel. */
  askPanel?: ReactNode;
  onCloseAsk?(): void;
  /** Opens a file or symbol in Code; absent without a trusted local workspace. */
  onOpenInCode?(target: CodeTarget): void;
  /** The brief's reading-order chapters; the change overview groups by them. */
  chapters?: readonly MapChapter[];
  /** Reports the selected node, so switching to Implementation lands on it. */
  onSelectionChange?(selection: ReviewMapSelection | null): void;
  /** PK2 landed history for a selected component or edge; absent without Project Knowledge. */
  loadProjectHistory?(selection: ReviewProjectContextSelection): Promise<ReviewProjectContext | null>;
}

/** One step of the drill-down: change -> file -> symbol. */
interface MapView {
  level: ReviewMapLevel;
  nodeId: string;
  label: string;
  payload: ReviewMapPayload | null;
  selectedId: string | null;
  selectedEdgeId: string | null;
}

// Below this window width the lanes need the room; the inspector overlays.
const INSPECTOR_BREAKPOINT = 1280;

function plural(count: number, one: string, many = one + "s"): string {
  return count.toLocaleString() + " " + (count === 1 ? one : many);
}

function locationOf(stack: MapView[]): ReviewMapLocation | null {
  const top = stack[stack.length - 1];
  if (!top?.payload) return null;
  return {
    level: top.level,
    projection: top.level === "architecture" && top.payload.projection === "architecture_delta" ? "architecture_delta" : "change_map",
    focus: top.level === "architecture" ? "" : top.nodeId,
    node: top.selectedId ?? "",
    edge: top.selectedEdgeId ?? "",
    path: "",
  };
}

/** Applies a location's selection to a loaded view; `fallback` stands when it names nothing on the graph. */
function withSelection(view: MapView, location: ReviewMapLocation, fallback: string | null): MapView {
  const graph = view.payload?.graph;
  if (!graph) return view;
  if (location.edge && graph.edges.some((edge) => edge.id === location.edge)) {
    return { ...view, selectedId: null, selectedEdgeId: location.edge };
  }
  const direct = location.node ? graph.nodes.find((node) => node.id === location.node) : undefined;
  const byPath = !direct && location.path ? graph.nodes.find((node) => node.path === location.path) : undefined;
  return { ...view, selectedId: direct?.id ?? byPath?.id ?? fallback, selectedEdgeId: null };
}

function MapSkeleton() {
  const column = (count: number, height: number, width: string) => (
    <div className="flex flex-col gap-2" style={{ width }}>
      <div className="rm-skeleton h-3 w-24" />
      {Array.from({ length: count }, (_, index) => <div key={index} className="rm-skeleton" style={{ height }} />)}
    </div>
  );
  return (
    <div className="flex h-full items-start justify-center gap-16 overflow-hidden bg-surface-sunken px-8 pt-16" aria-hidden="true">
      {column(5, 30, "200px")}
      {column(7, 46, "340px")}
      {column(4, 30, "200px")}
    </div>
  );
}

/**
 * The Map view of the Review shell (Brief · Implementation · Map): the change,
 * what it reaches, and what it relies on, drawn by the shared lane engine.
 */
export default function ReviewMapSheet({
  reviewId,
  revisionNumber,
  historical,
  location: locationProp,
  onLocationChange,
  selectionHref,
  onClose,
  onOpenSelection,
  onAskSelection = () => {},
  askPanel,
  onCloseAsk,
  onOpenInCode,
  chapters,
  onSelectionChange,
  loadProjectHistory,
}: ReviewMapSheetProps) {
  const rawLocation = locationProp ?? REVIEW_MAP_OVERVIEW;
  const [projection, setProjection] = useState<ReviewMapProjection>(rawLocation.projection ?? "change_map");
  useEffect(() => {
    setProjection(rawLocation.level === "architecture" ? rawLocation.projection ?? "change_map" : "change_map");
  }, [rawLocation.level, rawLocation.projection]);
  const location = useMemo<ReviewMapLocation>(
    () => ({ ...rawLocation, projection: rawLocation.level === "architecture" ? projection : "change_map" }),
    [projection, rawLocation],
  );
  const viewRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const requestGeneration = useRef(0);
  const graphRefreshAttempts = useRef(0);
  const [stack, setStack] = useState<MapView[]>([]);
  const stackRef = useRef(stack);
  stackRef.current = stack;
  // The location the stack shows, and the Snapshot it was loaded for. A URL
  // change equal to it is this view's own report echoing back.
  const shownLocation = useRef<ReviewMapLocation | null>(null);
  const loadedFor = useRef("");
  const onLocationChangeRef = useRef(onLocationChange);
  onLocationChangeRef.current = onLocationChange;
  const onSelectionChangeRef = useRef(onSelectionChange);
  onSelectionChangeRef.current = onSelectionChange;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchIndex, setSearchIndex] = useState(0);
  const [linkMode, setLinkMode] = useState<LinkMode>("key");
  const [hideTests, setHideTests] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const [architectureExplanation, setArchitectureExplanation] = useState<ReviewArchitectureExplanation | null>(null);
  const [architectureExplainLoading, setArchitectureExplainLoading] = useState(false);
  const [architectureExplainError, setArchitectureExplainError] = useState("");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [canvasWidth, setCanvasWidth] = useState(0);
  const [sheetWidth, setSheetWidth] = useState(typeof window === "undefined" ? 1440 : window.innerWidth);

  const view = stack[stack.length - 1] ?? null;
  const payload = view?.payload ?? null;
  const overview = stack[0]?.payload ?? null;
  const selectedId = view?.selectedId ?? null;
  const selectedEdgeId = view?.selectedEdgeId ?? null;

  const updateView = useCallback((patch: Partial<MapView>) => {
    setStack((current) => current.length
      ? [...current.slice(0, -1), { ...current[current.length - 1], ...patch }]
      : current);
  }, []);
  const select = useCallback((id: string | null) => updateView({ selectedId: id, selectedEdgeId: null }), [updateView]);
  const selectEdge = useCallback((id: string) => updateView({ selectedId: null, selectedEdgeId: id }), [updateView]);

  useEffect(() => {
    const onResize = () => setSheetWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const fetchArchitecture = useCallback(
    () => projection === "architecture_delta"
      ? fetchHistoricalReviewMap(reviewId, revisionNumber, "architecture", "", "architecture_delta")
      : fetchHistoricalReviewMap(reviewId, revisionNumber),
    [projection, reviewId, revisionNumber],
  );

  const fetchLevel = useCallback(async (level: ReviewMapLevel, nodeId: string, base: ReviewMapPayload | null) => {
    if (level === "architecture") return fetchArchitecture();
    if (level === "symbol") return fetchHistoricalReviewMap(reviewId, revisionNumber, "symbol", nodeId);
    try {
      return await fetchHistoricalReviewMap(reviewId, revisionNumber, "file", nodeId);
    } catch (reason) {
      // Servers that predate the file level: derive the focus from the overview.
      const local = base ? localFileFocus(base, nodeId) : null;
      if (local) return local;
      throw reason;
    }
  }, [fetchArchitecture, reviewId, revisionNumber]);

  // Records what the stack now shows. When that differs from the location it
  // was asked for, the URL is corrected in place rather than adding history.
  const settle = useCallback((requested: ReviewMapLocation, views: MapView[]) => {
    const shown = locationOf(views) ?? requested;
    shownLocation.current = shown;
    if (reviewMapLocationKey(shown) !== reviewMapLocationKey(requested)) onLocationChangeRef.current?.(shown, "replace");
  }, []);

  // Initial load, a location from outside (Back/Forward, an Ask reference), or
  // a new Snapshot.
  useEffect(() => {
    const snapshot = `${reviewId}:${revisionNumber}:${historical}:${projection}`;
    const sameSnapshot = loadedFor.current === snapshot;
    const shown = shownLocation.current;
    if (sameSnapshot && shown && reviewMapLocationKey(shown) === reviewMapLocationKey(location)) return;
    // A step already on the drill-down stack comes back as the reviewer left it.
    const current = stackRef.current;
    const index = sameSnapshot
      ? current.findIndex((item) => item.payload && item.level === location.level && (location.level === "architecture" || item.nodeId === location.focus))
      : -1;
    if (index >= 0) {
      requestGeneration.current += 1;
      setLoading(false);
      setError("");
      setQuery("");
      const views = [...current.slice(0, index), withSelection(current[index], location, null)];
      setStack(views);
      settle(location, views);
      return;
    }
    // Focus the view itself: keyboard users land inside it without a focus
    // ring flashing on the first control. Deferred past the focus return of a
    // palette or menu that opened the Map.
    window.setTimeout(() => viewRef.current?.focus({ preventScroll: true }), 0);
    const generation = ++requestGeneration.current;
    const isCurrent = () => requestGeneration.current === generation;
    let settled = false;
    shownLocation.current = null;
    setLoading(true);
    setStack([]);
    setError("");
    setQuery("");
    setExpanded(new Set());
    graphRefreshAttempts.current = 0;
    const load = async () => {
      const base = await fetchArchitecture();
      if (!isCurrent()) return;
      const views: MapView[] = [{ level: "architecture", nodeId: "", label: "Change", payload: base, selectedId: null, selectedEdgeId: null }];
      const { level, focus } = location;
      if (level !== "architecture" && focus) {
        const next = await fetchLevel(level, focus, base);
        if (!isCurrent()) return;
        const focusNode = next.graph.nodes.find((node) => node.id === next.graph.focus);
        views.push({
          level,
          nodeId: focus,
          label: focusNode ? nodeName(focusNode) : level,
          payload: next,
          selectedId: next.graph.focus,
          selectedEdgeId: null,
        });
      }
      const top = views.length - 1;
      views[top] = withSelection(views[top], location, views[top].selectedId);
      settled = true;
      loadedFor.current = snapshot;
      setStack(views);
      settle(location, views);
    };
    void load().catch((reason: unknown) => {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (isCurrent()) setLoading(false);
    });
    // Cancel only a load still in flight; a finished one must not void a
    // drill-down or readiness refresh that started after it.
    return () => { if (!settled) requestGeneration.current += 1; };
  }, [historical, location, reviewId, revisionNumber, fetchLevel, settle]);

  // Reports the reviewer's own moves: a new drill-down step adds history, a
  // selection change on the same step replaces it.
  useEffect(() => {
    const next = locationOf(stack);
    const previous = shownLocation.current;
    if (!next || !previous || reviewMapLocationKey(next) === reviewMapLocationKey(previous)) return;
    shownLocation.current = next;
    onLocationChangeRef.current?.(next, sameReviewMapLevel(previous, next) ? "replace" : "push");
  }, [stack]);

  // Map node jumps stay in the Review: the Reader records the Map on its
  // backtrail, so Back returns here. A dedicated tab is the secondary link.
  const openSelection = useCallback((selection: ReviewMapSelection) => {
    onOpenSelection(selection);
  }, [onOpenSelection]);

  useEffect(() => {
    setArchitectureExplanation(null);
    setArchitectureExplainError("");
    setArchitectureExplainLoading(false);
  }, [projection, reviewId, revisionNumber]);

  const explainArchitecture = useCallback(() => {
    if (projection !== "architecture_delta" || architectureExplainLoading) return;
    setArchitectureExplainLoading(true);
    setArchitectureExplainError("");
    void explainHistoricalReviewArchitecture(reviewId, revisionNumber)
      .then((response) => {
        if (response.status === "ready" || response.status === "not_applicable") {
          setArchitectureExplanation(response.result);
          return;
        }
        setArchitectureExplainError(response.error || `Architecture explanation ended as ${response.status}.`);
      })
      .catch((reason: unknown) => {
        setArchitectureExplainError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => setArchitectureExplainLoading(false));
  }, [architectureExplainLoading, projection, reviewId, revisionNumber]);

  // Surrounding relationships may still be indexing when the map opens. Poll
  // the overview briefly with backoff; Snapshot facts stay on screen meanwhile.
  useEffect(() => {
    if (!overview) return;
    const graphState = overview.context.readiness?.graph?.state;
    const hasCurrentRelationships = overview.graph.edges.some((edge) => edge.exactness === "current_index");
    if (graphState === "ready" || hasCurrentRelationships) {
      graphRefreshAttempts.current = 0;
      return;
    }
    if (overview.context.source !== "current_index" || graphState !== "building") return;
    // A cold link index can take tens of seconds after a server restart; keep
    // checking for about a minute, backing off to one request every 5s.
    if (graphRefreshAttempts.current >= 14) return;
    const attempt = ++graphRefreshAttempts.current;
    const generation = requestGeneration.current;
    const delay = Math.min(5000, 500 * 2 ** (attempt - 1));
    const timer = window.setTimeout(() => {
      void fetchArchitecture()
        .then((next) => {
          if (requestGeneration.current !== generation || next.level !== "architecture") return;
          setStack((current) => current.length ? [{ ...current[0], payload: next }, ...current.slice(1)] : current);
        })
        .catch(() => {
          // Background readiness refresh is best-effort; keep the current map.
        });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [fetchArchitecture, overview]);

  const navigate = useCallback(async (level: ReviewMapLevel, nodeId: string, label: string, parents: MapView[] = []) => {
    const generation = ++requestGeneration.current;
    const isCurrent = () => requestGeneration.current === generation;
    setLoading(true);
    setError("");
    setQuery("");
    try {
      const next = await fetchLevel(level, nodeId, overview);
      if (!isCurrent()) return;
      setStack((current) => [
        ...current.slice(0, 1),
        ...parents,
        { level, nodeId, label, payload: next, selectedId: next.graph.focus, selectedEdgeId: null },
      ]);
    } catch (reason) {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [fetchLevel, overview]);

  const goTo = useCallback((index: number) => {
    requestGeneration.current += 1;
    setLoading(false);
    setError("");
    setQuery("");
    const target = stack[index];
    if (!target) return;
    if (target.payload) {
      setStack(stack.slice(0, index + 1));
      return;
    }
    // A breadcrumb added on the way to a symbol; load it now.
    const parents = stack.slice(1, index);
    void navigate(target.level, target.nodeId, target.label, parents);
  }, [navigate, stack]);

  const focusFile = useCallback((nodeId: string) => {
    const node = payload?.graph.nodes.find((item) => item.id === nodeId);
    if (!node || node.kind !== "file" || node.exactness !== "snapshot") return;
    void navigate("file", nodeId, nodeName(node));
  }, [navigate, payload]);

  const openSymbol = useCallback((symbol: ReviewMapMemberSymbol) => {
    const fileView = stack.find((item) => item.level === "file" && item.payload?.graph.nodes.find((node) => node.id === item.nodeId)?.path === symbol.path);
    const fileNode = overview?.graph.nodes.find((node) => node.kind === "file" && node.path === symbol.path && node.exactness === "snapshot");
    const parents: MapView[] = fileView
      ? [fileView]
      : fileNode
        ? [{ level: "file", nodeId: fileNode.id, label: nodeName(fileNode), payload: null, selectedId: fileNode.id, selectedEdgeId: null }]
        : [];
    void navigate("symbol", symbol.id, symbol.label, parents);
  }, [navigate, overview, stack]);

  const openNode = useCallback((nodeId: string) => {
    if (!payload) return;
    const node = payload.graph.nodes.find((item) => item.id === nodeId);
    if (!node) return;
    if (node.kind === "file" && node.exactness === "snapshot" && !(payload.level === "file" && nodeId === payload.graph.focus)) {
      focusFile(nodeId);
      return;
    }
    select(nodeId);
  }, [focusFile, payload, select]);

  const toggleGroup = useCallback((key: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const inspectorDocked = sheetWidth >= INSPECTOR_BREAKPOINT;
  const model = useMemo(
    () => payload ? buildMapModel(payload, {
      width: canvasWidth,
      linkMode: projection === "architecture_delta" ? "all" : linkMode,
      hideTests,
      expanded,
      chapters: projection === "architecture_delta" ? undefined : chapters,
    }) : null,
    [canvasWidth, chapters, expanded, hideTests, linkMode, payload, projection],
  );

  // Selecting something that is folded away (search, inspector links) unfolds it.
  useEffect(() => {
    if (!payload || !model || !selectedId || model.cardById.has(selectedId)) return;
    const node = payload.graph.nodes.find((item) => item.id === selectedId);
    if (!node) return;
    if (hideTests && isTestNode(node)) {
      setHideTests(false);
      return;
    }
    // The group the model placed it in: a chapter, not its folder, when the
    // change is grouped by chapter.
    const key = model.groupKeyOf.get(node.id);
    if (key && !expanded.has(key)) setExpanded((current) => new Set(current).add(key));
  }, [expanded, hideTests, model, payload, selectedId]);

  const selected = payload?.graph.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedEdge = payload?.graph.edges.find((edge) => edge.id === selectedEdgeId) ?? null;
  useEffect(() => {
    onSelectionChangeRef.current?.(selected?.selection ?? null);
  }, [selected]);
  const hits = useMemo(() => payload ? searchMap(payload, query) : [], [payload, query]);
  const testCount = useMemo(() => payload?.graph.nodes.filter((node) => hiddenAsTest(payload, node)).length ?? 0, [payload]);

  const pickHit = (hit: SearchHit) => {
    setQuery("");
    if (hit.symbol) {
      openSymbol(hit.symbol);
      return;
    }
    select(hit.id);
  };

  const stats = overview?.graph.stats;
  const architectureDelta = overview?.architecture_delta;
  const architectureSummary = architectureDelta?.summary;
  const overviewNodes = overview?.graph.nodes ?? [];
  const changedCount = stats?.changed_files ?? overviewNodes.filter(isChangedStatus).length;
  const affectedCount = stats?.impacted_files ?? overviewNodes.filter((node) => node.status === "impacted").length;
  const hiddenChanged = stats?.shown_changed !== undefined ? Math.max(0, changedCount - stats.shown_changed) : 0;
  const hiddenRelated = Math.max(0, (overview?.graph.omitted_node_count ?? 0) - hiddenChanged);
  const graphState = overview?.context.readiness?.graph?.state;
  const hasCurrentIndexEdges = Boolean(overview?.graph.edges.some((edge) => edge.exactness === "current_index"));
  const relationshipSource = overview?.context.source !== "current_index"
    ? "Relationships unavailable"
    : hasCurrentIndexEdges || graphState === "ready" || !graphState
      ? "Relationships from the current code index"
      : `Relationships ${graphState}`;
  const indexing = overview?.context.source === "current_index" && graphState === "building" && !hasCurrentIndexEdges;

  const showGuide = payload?.level === "architecture" && !selected && !selectedEdge;
  const showInspector = Boolean(payload) && (inspectorDocked || selected || selectedEdge || (guideOpen && showGuide));

  return (
    <section
      ref={viewRef}
      tabIndex={-1}
      aria-labelledby="review-map-title"
      onKeyDown={(event) => {
        if (event.target instanceof Element && event.target.closest("[data-review-map-ask-panel]")) {
          if (event.key === "Escape") {
            event.preventDefault();
            onCloseAsk?.();
          }
          return;
        }
        const typing = event.target instanceof HTMLInputElement;
        if (event.key === "Escape") {
          if (askPanel) {
            event.preventDefault();
            onCloseAsk?.();
            return;
          }
          // Escape steps back out: clear the search, then the selection, then
          // the drill-down, and finally leave the Map.
          event.preventDefault();
          if (typing && query) { setQuery(""); return; }
          if (selectedId || selectedEdgeId) {
            if (payload?.level === "architecture") updateView({ selectedId: null, selectedEdgeId: null });
            else if (stack.length > 1) goTo(stack.length - 2);
            else onClose();
            return;
          }
          if (stack.length > 1) goTo(stack.length - 2);
          else onClose();
          return;
        }
        if (typing) return;
        if (event.key === "/") {
          event.preventDefault();
          searchRef.current?.focus();
        } else if (event.key === "o" && !event.metaKey && !event.ctrlKey && selected?.exactness === "snapshot" && selected.path) {
          // Same jump the inspector's primary action makes: map -> diff.
          event.preventDefault();
          openSelection(selected.selection);
        } else if ((event.key === "Backspace" || (event.altKey && event.key === "ArrowLeft")) && stack.length > 1) {
          event.preventDefault();
          goTo(stack.length - 2);
        }
      }}
      className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-background outline-none"
    >
      <header className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/80 bg-background px-4 py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-muted/60 text-neutral-300"><Network size={15} aria-hidden="true" /></div>
          <div className="min-w-0">
            <nav aria-label="Review map location" className="flex min-w-0 items-center gap-1 text-heading">
              <h2 id="review-map-title" className="shrink-0 text-heading font-semibold tracking-tight">Review Map</h2>
              {stack.length > 1 && stack.map((item, index) => (
                <span key={item.level + item.nodeId} className="flex min-w-0 items-center gap-1">
                  <ChevronRight size={13} className="shrink-0 text-muted-foreground" aria-hidden="true" />
                  {index === stack.length - 1 ? (
                    <span className="truncate font-medium text-neutral-200" aria-current="location">{item.label}</span>
                  ) : (
                    <Button unstyled className="truncate rounded px-1 text-neutral-400 transition-colors hover:bg-muted hover:text-foreground" onClick={() => goTo(index)}>
                      {item.label}
                    </Button>
                  )}
                </span>
              ))}
              {loading && payload && <Loader2 size={13} className="ml-1 shrink-0 animate-spin text-muted-foreground" aria-label="Loading" />}
            </nav>
            <div className="mt-0.5 truncate text-label text-muted-foreground">
              {overview
                ? projection === "architecture_delta"
                  ? [
                      `Snapshot ${revisionNumber}`,
                      architectureSummary ? `${architectureSummary.changed_components} components changed` : "Architecture delta",
                      architectureSummary ? `${architectureSummary.changed_edges} relationship changes` : "",
                      architectureDelta?.status === "unavailable" ? "exact comparison unavailable" : architectureDelta?.status === "degraded" ? "exact delta · context degraded" : "base ↔ Snapshot",
                    ].filter(Boolean).join(" · ")
                  : [
                      `Snapshot ${revisionNumber}`,
                      plural(changedCount, "file") + " changed",
                      affectedCount ? `${affectedCount} affected outside the change` : "",
                      relationshipSource,
                      hiddenChanged ? `${plural(hiddenChanged, "changed file")} not mapped` : "",
                      hiddenRelated ? `${plural(hiddenRelated, "related file")} not shown` : "",
                    ].filter(Boolean).join(" · ")
                : `Snapshot ${revisionNumber}`}
              {indexing && <span className="ml-1 inline-flex items-center gap-1 text-neutral-400"><Loader2 size={10} className="animate-spin" aria-hidden="true" />indexing</span>}
            </div>
          </div>
        </div>

        {payload && (
          <div className="flex items-center gap-2">
            {payload.level === "architecture" && (
              <ToggleGroup
                aria-label="Map projection"
                options={[
                  { value: "change_map", label: "Change map", title: "Changed files and surrounding relationships" },
                  { value: "architecture_delta", label: "Architecture Δ", title: "Base-to-Snapshot architecture relationship delta" },
                ]}
                value={projection}
                onChange={(value) => {
                  const next: ReviewMapProjection = value === "architecture_delta" ? "architecture_delta" : "change_map";
                  if (next === projection) return;
                  setProjection(next);
                  setArchitectureExplanation(null);
                  setArchitectureExplainError("");
                  setQuery("");
                  setExpanded(new Set());
                  updateView({ selectedId: null, selectedEdgeId: null });
                  onLocationChangeRef.current?.({ ...REVIEW_MAP_OVERVIEW, projection: next }, "replace");
                }}
                className="flex-nowrap gap-1"
              />
            )}
            <div className="relative w-[260px] max-w-[40vw]">
              <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                ref={searchRef}
                uiSize="xs"
                value={query}
                onChange={(event) => { setQuery(event.target.value); setSearchIndex(0); }}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                    event.preventDefault();
                    setSearchIndex((index) => Math.max(0, Math.min(hits.length - 1, index + (event.key === "ArrowDown" ? 1 : -1))));
                  } else if (event.key === "Enter" && hits[searchIndex]) {
                    event.preventDefault();
                    pickHit(hits[searchIndex]);
                  }
                }}
                placeholder="Find a file or symbol"
                className="pl-7 pr-12"
                aria-label="Search review map"
                role="combobox"
                aria-expanded={Boolean(query.trim())}
                aria-controls="review-map-search-results"
              />
              {query ? (
                <Button unstyled onClick={() => setQuery("")} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-neutral-200" aria-label="Clear map search"><X size={11} /></Button>
              ) : (
                <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-border px-1 text-caption text-muted-foreground">/</kbd>
              )}
              {query.trim() && (
                <div id="review-map-search-results" role="listbox" className="absolute left-0 top-[36px] z-50 w-[360px] max-w-[calc(100vw-48px)] overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-lg shadow-black/20">
                  {hits.length ? hits.map((hit, index) => (
                    <Button
                      unstyled
                      key={hit.kind + hit.id}
                      role="option"
                      aria-selected={index === searchIndex}
                      aria-label={`Select ${hit.label} on map`}
                      className={"flex w-full items-center gap-2.5 border-b border-border px-3 py-2 text-left last:border-b-0 " + (index === searchIndex ? "bg-accent" : "")}
                      onMouseEnter={() => setSearchIndex(index)}
                      onClick={() => pickHit(hit)}
                    >
                      <span className="rm-kind-tag">{hit.kind === "symbol" ? hit.symbol?.kind || "symbol" : "file"}</span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-body font-medium text-foreground">{hit.label}</span>
                        <span className="mt-0.5 block truncate font-mono text-caption text-muted-foreground" title={hit.detail}>{hit.detail}</span>
                      </span>
                      <span className="shrink-0 text-caption text-muted-foreground">{isChangedStatus(hit.node) ? "changed" : hit.node.status === "impacted" ? "affected" : ""}</span>
                    </Button>
                  )) : <div className="px-3 py-3 text-label text-muted-foreground">Nothing on this map matches “{query.trim()}”.</div>}
                </div>
              )}
            </div>
            {projection !== "architecture_delta" && (
              <ToggleGroup
                aria-label="Map links"
                options={[
                  { value: "key", label: "Key links", title: "Impacts, calls, and imports" },
                  { value: "all", label: "All links", title: "Also draw name references" },
                ]}
                value={linkMode}
                onChange={(value) => setLinkMode(value === "all" ? "all" : "key")}
                className="flex-nowrap gap-1"
              />
            )}
            {testCount > 0 && (
              <Button
                variant="outline"
                size="xs"
                className="h-8"
                active={hideTests}
                aria-pressed={hideTests}
                onClick={() => setHideTests((value) => !value)}
                icon={hideTests ? <EyeOff size={12} /> : <Eye size={12} />}
                tooltip={hideTests ? "Show test files" : "Hide test files"}
              >
                {hideTests ? `${testCount} tests hidden` : "Tests"}
              </Button>
            )}
          </div>
        )}
        {!inspectorDocked && payload?.level === "architecture" && (
          <Button
            variant="outline"
            size="xs"
            className="h-8"
            active={guideOpen && showGuide}
            aria-pressed={guideOpen && showGuide}
            onClick={() => {
              updateView({ selectedId: null, selectedEdgeId: null });
              setGuideOpen((value) => !(value && showGuide));
            }}
            icon={<Compass size={12} />}
          >
            Guide
          </Button>
        )}
      </header>

      {error && <div role="alert" className="shrink-0 border-b border-rose-900/60 bg-rose-950/20 px-4 py-2 text-label text-rose-200">{error}</div>}

      <div className="relative flex min-h-0 flex-1">
        <div className={"relative min-w-0 flex-1 transition-opacity duration-150 " + (loading && payload ? "opacity-60" : "")}>
          {!payload && loading ? (
            <MapSkeleton />
          ) : payload && model && model.cards.length > 0 ? (
            <ReviewMapCanvas
              model={model}
              selectedId={selectedId}
              selectedEdgeId={selectedEdgeId}
              onSelect={select}
              onSelectEdge={selectEdge}
              onOpen={openNode}
              onOpenSymbol={openSymbol}
              onToggleGroup={toggleGroup}
              onWidth={setCanvasWidth}
            />
          ) : payload ? (
            <div className="flex h-full flex-col items-center justify-center bg-surface-sunken px-6 text-center">
              <Network size={22} className="mb-3 text-muted-foreground" aria-hidden="true" />
              <div className="text-heading font-medium text-neutral-200">
                {payload.graph.nodes.length ? "Every file on this map is hidden" : "No map facts are available for this Snapshot"}
              </div>
              <div className="mt-1.5 max-w-sm text-label leading-5 text-muted-foreground">
                {payload.graph.reason || (payload.graph.nodes.length ? "Show test files again to bring them back." : "The Snapshot recorded no changed files or symbols to map.")}
              </div>
              {hideTests && payload.graph.nodes.length > 0 && <Button variant="outline" size="xs" className="mt-3" onClick={() => setHideTests(false)}>Show tests</Button>}
            </div>
          ) : null}
        </div>

        {askPanel ? (
          <div data-review-map-ask-panel className={inspectorDocked
            ? "w-[400px] shrink-0 border-l border-border"
            : "absolute inset-y-0 right-0 z-30 w-[min(400px,92vw)] border-l border-border shadow-[-24px_0_56px_rgba(0,0,0,0.3)]"}>
            {askPanel}
          </div>
        ) : showInspector && payload && (
          <div className={inspectorDocked ? "w-[360px] shrink-0" : "absolute inset-y-0 right-0 z-30 w-[min(360px,92vw)] shadow-[-24px_0_56px_rgba(0,0,0,0.3)]"}>
            <ReviewMapInspector
              payload={payload}
              overview={overview}
              selected={selected}
              selectedEdge={selectedEdge}
              onSelectNode={select}
              onSelectEdge={selectEdge}
              onClear={() => {
                updateView({ selectedId: null, selectedEdgeId: null });
                setGuideOpen(false);
              }}
              overlay={!inspectorDocked}
              onFocusFile={focusFile}
              onOpenSymbol={openSymbol}
              selectionHref={selectionHref}
              onOpenSelection={onOpenSelection}
              onAskSelection={onAskSelection}
              onOpenInCode={onOpenInCode}
              architectureExplanation={architectureExplanation}
              architectureExplainLoading={architectureExplainLoading}
              architectureExplainError={architectureExplainError}
              onExplainArchitecture={explainArchitecture}
              loadProjectHistory={loadProjectHistory}
            />
          </div>
        )}
      </div>
    </section>
  );
}
