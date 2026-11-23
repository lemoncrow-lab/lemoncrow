/**
 * The X-Ray screen (`/r/<review>/xray`): the change drawn from the revision's X-Ray graph document (lanes of
 * components, labelled arrows, flows and a walkthrough). Where the server has no such document (404, `unavailable`)
 * it falls back to the file picture on the revision's stored Map (`?at=<side~snapshot~node>`). It marks nothing; a
 * judgment on a behavior change goes through the same evidence-bound write as the Brief.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { BehaviorRow, type BehaviorHandlers } from "../brief/BehaviorChanges";
import { isChangedNode } from "../changeStory/changeStoryModel";
import { FLOW_RAIL_CLASS, FlowShortcutsDialog, FlowTopBar, repoNameOf, useShortcutsKey } from "../FlowTopBar";
import { counterFor } from "../focus/focusModel";
import { Label } from "../overview/OverviewParts";
import { partSlug } from "../overview/overviewModel";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { cameFromReview } from "../reviewBootLocation";
import type { ReviewArchitectureDelta, ReviewBehaviorSummary, ReviewBehaviorTarget, ReviewMapGraph, ReviewMapSelection, ReviewOverview, ReviewTarget } from "../types";
import { useReadingViewSwitch, useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { fetchXRayGraph, requestXRayGraph, type XRayGraphRequestResult, type XRayGraphState } from "./graph/xrayGraphApi";
import type { GraphSide, Hit } from "./graph/graphModel";
import type { XRayAnchor, XRayGraph } from "./graph/xrayGraphTypes";
import XRayGraphView, { type DrawResult } from "./graph/XRayGraphView";
import XRay from "./XRay";
import { decodeXRayView, encodeXRayLocation, graphSnapshot, partForSelection, type XRayLocation, type XRaySide } from "./xrayModel";

export interface ReviewXRayScreenProps extends BehaviorHandlers {
  reviewRef: string;
  apiReviewId: string;
  /** Where the file picture stands (`side~snapshot~node`); an empty node is the whole change, and an empty address starts there. */
  at: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  complete?: boolean;
  /** The Reader's cached architecture read, for the file picture; absent where this change has none. */
  load?: () => Promise<ReviewArchitectureDelta | null>;
  /** The Review's analysis is still loading, so `load` may yet arrive. */
  loading?: boolean;
  /** This revision's behavior summary; null when nothing observed it or the analysis is not loaded. */
  behavior?: ReviewBehaviorSummary | null;
  revisionId?: string;
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
  /** Reads the X-Ray graph document until it settles, reporting each pending answer; defaults to the server's endpoint. */
  loadGraph?: (onUpdate: (state: XRayGraphState) => void, signal: AbortSignal) => Promise<XRayGraphState>;
  /** Asks the server to draw again (with AI); defaults to the server's endpoint. */
  requestGraph?: (ai: boolean) => Promise<XRayGraphRequestResult>;
}

type Read = { state: "loading" } | { state: "failed" } | { state: "open"; delta: ReviewArchitectureDelta | null };
type GraphRead = { phase: "loading" } | { phase: "pending" | "settled"; state: XRayGraphState };

/** Where a bare `/xray` starts: the whole change, on After, when the Map lists a changed file at all. */
export function defaultXRayLocation(graph: ReviewMapGraph): XRayLocation | null {
  return graph.nodes.some(isChangedNode) ? { side: "after", snapshot: graphSnapshot(graph), rootId: "" } : null;
}

/** The behavior changes that name one of these files; a change with no path stays in the Brief. */
export function behaviorOnPath(behavior: ReviewBehaviorSummary, paths: Iterable<string>): ReviewBehaviorTarget[] {
  const wanted = new Set([...paths].filter(Boolean));
  return behavior.targets.filter((target) => target.paths.some((path) => wanted.has(path)));
}

/**
 * Where the graph picture stands, from the same `side~snapshot~node` address as the file picture: the side may also be
 * `both` (the default), and the node is a card of the drawing or a Map node (a link from the Map, the Walkthrough or
 * Investigate), which `graphNodeFor` places on the drawing.
 */
export function decodeGraphAt(at: string): { side: GraphSide; snapshot: string; node: string } {
  const [side = "", snapshot = "", ...rest] = at.split("~");
  return { side: side === "before" || side === "after" ? side : "both", snapshot, node: rest.join("~") };
}

export function encodeGraphAt(place: { side: GraphSide; snapshot: string; node: string }): string {
  return place.side === "both" && !place.node ? "" : `${place.side}~${place.snapshot}~${place.node}`;
}

/** The card a node names: itself on the drawing, else the card in that Map node's file (at its line when it has one). */
export function graphNodeFor(graph: XRayGraph, node: string, map: ReviewMapGraph | null): string | null {
  if (!node) return null;
  if (graph.nodes.some((card) => card.id === node)) return node;
  const place = map?.nodes.find((item) => item.id === node);
  const paths = new Set([place?.path, place?.old_path].filter((path): path is string => Boolean(path)));
  if (!place || !paths.size) return null;
  const inFile = graph.nodes.filter((card) => card.anchors.some((anchor) => paths.has(anchor.path)));
  const line = place.line || 0;
  const atLine = line > 0
    ? inFile.find((card) => card.anchors.some((anchor) => paths.has(anchor.path) && (anchor.start_line ?? 0) <= line && line <= (anchor.end_line ?? anchor.start_line ?? 0)))
    : undefined;
  return (atLine ?? inFile.find((card) => card.delta !== "unchanged") ?? inFile[0])?.id ?? null;
}

/** A graph anchor as the Map selection the Reader opens: its file and lines on its side. */
export function anchorSelection(anchor: XRayAnchor, reviewId: string, revisionId: string): ReviewMapSelection {
  return {
    kind: "review_map_node",
    review_id: reviewId,
    revision_id: revisionId,
    exactness: "snapshot",
    side: anchor.side === "base" ? "old" : "new",
    path: anchor.path,
    ...(anchor.start_line ? { line: anchor.start_line } : {}),
    ...(anchor.end_line ? { end_line: anchor.end_line } : {}),
  };
}

export default function ReviewXRayScreen(props: ReviewXRayScreenProps) {
  const { reviewRef, apiReviewId, overview, targets, behavior, onAskChange } = props;
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const readingView = useReadingViewSwitch(reviewRef, targets);
  const navigate = useNavigate();
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  useShortcutsKey(() => setShortcutsOpen(true));
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "X-Ray"),
    openShortcuts: () => setShortcutsOpen(true),
    context: { screen: "other", actions: {} },
  });

  const revisionId = props.revisionId || overview.revision.id;
  const { loadGraph: givenLoad, requestGraph: givenRequest } = props;
  const loadGraph = useMemo(
    () => givenLoad ?? ((onUpdate: (state: XRayGraphState) => void, signal: AbortSignal) => fetchXRayGraph(apiReviewId, revisionId, { onUpdate, signal })),
    [givenLoad, apiReviewId, revisionId],
  );
  const requestGraph = useMemo(() => givenRequest ?? ((ai: boolean) => requestXRayGraph(apiReviewId, revisionId, { ai })), [givenRequest, apiReviewId, revisionId]);
  const [graphRead, setGraphRead] = useState<GraphRead>({ phase: "loading" });
  const [graphAttempt, setGraphAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const live = () => !controller.signal.aborted;
    // A redraw keeps what is on screen (the picture, or the file fallback) while it waits.
    const keep = (state: XRayGraphState, prev: GraphRead): XRayGraphState => (state.graph || prev.phase === "loading" || !prev.state.graph ? state : { ...state, graph: prev.state.graph });
    setGraphRead((prev) => (prev.phase !== "loading" && prev.state.graph ? { phase: "pending", state: { ...prev.state, state: "pending" } } : prev));
    loadGraph((state) => { if (live()) setGraphRead((prev) => ({ phase: "pending", state: keep(state, prev) })); }, controller.signal)
      .then((state) => { if (live()) setGraphRead({ phase: "settled", state }); })
      .catch(() => { if (live()) setGraphRead({ phase: "settled", state: { state: "unavailable", reason: "The X-Ray graph could not load." } }); });
    return () => controller.abort();
  }, [loadGraph, graphAttempt]);

  // Back returns to the screen the reviewer came from (a part, the walkthrough); a direct link has only the Overview.
  const inSession = cameFromReview(reviewRef);
  const back = useCallback(() => {
    if (inSession) navigate(-1);
    else goTo({ kind: "overview" });
  }, [inSession, navigate, goTo]);
  const openSource = useCallback((selection: ReviewMapSelection) => {
    // A file the change did not touch has no part: its place is on the Map.
    const target = partForSelection(targets, selection);
    if (target) goTo({ kind: "one", part: partSlug(target, targets) });
    else goTo({ kind: "map", lens: "dependencies", view: "changes" });
  }, [targets, goTo]);
  const openAnchor = useCallback((anchor: XRayAnchor) => openSource(anchorSelection(anchor, apiReviewId, revisionId)), [openSource, apiReviewId, revisionId]);
  const openEvidence = useCallback((selection: ReviewMapSelection) => {
    // A traced arrow's recorded run sits with the checks of the part it reached: that part's card shows its proof and test.
    // A file the change did not touch has no part, and the Brief lists every recorded before and after run.
    const target = partForSelection(targets, selection);
    if (target) goTo({ kind: "one", part: partSlug(target, targets) });
    else goTo({ kind: "brief" });
  }, [targets, goTo]);

  const behaviorRow = (target: ReviewBehaviorTarget) => (
    <BehaviorRow
      key={`${props.revisionId ?? ""}:${target.behavior_key}:${target.fingerprint}:${target.environment_fingerprint}:${target.evidence?.digest ?? ""}:${target.title}:${target.summary}`}
      target={target}
      onLoadTarget={props.onLoadTarget}
      onJudge={props.onJudge}
    />
  );
  const renderBehavior = (paths: string[], scope: "file" | "arrow") => {
    const rows = behavior ? behaviorOnPath(behavior, paths) : [];
    const where = scope === "file" ? "this file" : "either end";
    return (
      <section className="flex flex-col gap-2 border-t border-border pt-3" aria-label={`Behavior changes on ${where}`} data-testid="xray-behavior" data-count={rows.length}>
        <Label>{rows.length ? `Behavior changes on ${where} · ${rows.length}` : `Behavior changes on ${where}`}</Label>
        {rows.map(behaviorRow)}
        {rows.length === 0 ? (
          <p className="text-caption text-muted-foreground" data-testid="xray-behavior-none">
            {behavior
              ? `No recorded before and after run touched ${scope === "file" ? "this file" : "these files"}. That is not proof ${scope === "file" ? "its" : "their"} behavior is unchanged.`
              : "No before and after run is recorded on this revision, so behavior here was not observed."}
          </p>
        ) : null}
        <Button variant="link" size="compact" className="h-auto self-start p-0 text-caption" onClick={() => goTo({ kind: "brief" })}>All behavior changes in the Brief</Button>
      </section>
    );
  };
  // The behavior change a payload names, by its key, with the same judge controls as the Brief.
  const renderBehaviorKey = (key: string) => {
    const rows = behavior?.targets.filter((target) => target.behavior_key === key) ?? [];
    return rows.length ? <>{rows.map(behaviorRow)}</> : null;
  };
  const drawWithAI = useCallback(async (): Promise<DrawResult> => {
    const result = await requestGraph(true);
    if (!result.ok) return result;
    if (result.state.state === "ready" && result.state.graph) setGraphRead({ phase: "settled", state: result.state });
    else setGraphAttempt((value) => value + 1);
    return { ok: true };
  }, [requestGraph]);

  const counter = counterFor(targets, "", props.complete !== false);
  const drawn = graphRead.phase !== "loading" && graphRead.state.graph && graphRead.state.state !== "unavailable" ? graphRead.state.graph : null;
  const fallback = graphRead.phase === "settled" && !drawn;

  // The drawing opens where the address says (its side and card, review 2026-11-21 e2e) and keeps both in the address,
  // so a reload, Back and a shared link land on the same picture. A Map node waits for the Map to be placed.
  const place = useMemo(() => decodeGraphAt(props.at), [props.at]);
  const [opening, setOpening] = useState<{ side: GraphSide; selection: Hit | null } | null>(null);
  const hasDrawing = Boolean(drawn);
  useEffect(() => {
    if (!hasDrawing || !drawn || opening) return;
    const open = (id: string | null) => setOpening({ side: place.side, selection: id ? { kind: "node", id } : null });
    if (!place.node || drawn.nodes.some((card) => card.id === place.node) || !props.load) {
      open(graphNodeFor(drawn, place.node, null));
      return;
    }
    let live = true;
    props.load()
      .then((delta) => { if (live) open(graphNodeFor(drawn, place.node, delta?.graph ?? null)); })
      .catch(() => { if (live) open(null); });
    return () => { live = false; };
  }, [hasDrawing, drawn, opening, place, props.load]);
  const { at } = props;
  const keepPlace = useCallback((next: { side: GraphSide; selection: Hit | null }) => {
    const node = next.selection?.kind === "node" ? next.selection.id : "";
    const value = encodeGraphAt({ side: next.side, snapshot: decodeGraphAt(at).snapshot, node });
    if (value !== at) goTo({ kind: "xray", at: value }, { replace: true });
  }, [at, goTo]);
  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="X-Ray"
        title={overview.title || overview.session.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: inSession ? "Back" : "Overview", onClick: back }}
        view={readingView}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={onAskChange}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <FlowShortcutsDialog
        open={shortcutsOpen}
        onOpenChange={setShortcutsOpen}
        legend={drawn
          ? "Click a card, arrow or step for its details · W walk through, ← → steps, Esc stop · scroll to move, ctrl and scroll to zoom · ? all shortcuts"
          : "Click a file to open its path, an arrow for its evidence · W walk through · ? all shortcuts"}
      />
      {/* The window scrolls (sticky top bar); the picture fills the screen below the bar and pans on its own. */}
      <div className="flex flex-1" data-testid="flow-xray-screen">
        <div role="region" aria-label="X-Ray" className="min-w-0 flex-1" data-review-scroll-root="xray">
          {drawn && opening ? (
            <XRayGraphView
              graph={drawn}
              pending={graphRead.phase === "pending"}
              onOpenAnchor={openAnchor}
              renderBehavior={renderBehaviorKey}
              renderFileBehavior={(paths) => renderBehavior(paths, "file")}
              onDrawWithAI={drawWithAI}
              initialSide={opening.side}
              initialSelection={opening.selection}
              onPlaceChange={keepPlace}
            />
          ) : fallback ? (
            <FileXRay {...props} goTo={goTo} back={back} inSession={inSession} openSource={openSource} openEvidence={openEvidence} renderBehavior={renderBehavior} />
          ) : (
            <div role="status" className="mx-auto flex max-w-3xl flex-col items-start gap-2 px-4 py-6 text-label text-muted-foreground" data-testid="xray-graph-note">
              <span>{graphRead.phase === "pending" ? "Drawing the X-Ray…" : "Opening the X-Ray…"}</span>
            </div>
          )}
        </div>
        {props.askPanel ? (
          <aside data-review-rail="ask" className={`review-secondary-rail ${FLOW_RAIL_CLASS} w-[min(32vw,400px)] shrink-0 border-l border-border bg-background`}>
            {props.askPanel}
          </aside>
        ) : null}
      </div>
    </>
  );
}

interface FileXRayProps extends ReviewXRayScreenProps {
  goTo: ReturnType<typeof useReviewFlowNavigation>["goTo"];
  back(): void;
  inSession: boolean;
  openSource(selection: ReviewMapSelection): void;
  openEvidence(selection: ReviewMapSelection): void;
  renderBehavior(paths: string[], scope: "file" | "arrow"): React.ReactNode;
}

/** The file picture on the revision's stored Map: the fallback where the server draws no X-Ray graph. */
function FileXRay({ at, load, loading, behavior, targets, goTo, back, inSession, openSource, openEvidence, renderBehavior }: FileXRayProps) {
  const [read, setRead] = useState<Read>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!load) return;
    let current = true;
    setRead({ state: "loading" });
    load()
      .then((delta) => { if (current) setRead({ state: "open", delta }); })
      .catch(() => { if (current) setRead({ state: "failed" }); });
    return () => { current = false; };
  }, [load, attempt]);

  const graph = read.state === "open" ? read.delta?.graph ?? null : null;
  const location = decodeXRayView(at);
  const start = useMemo(() => (graph && !location ? defaultXRayLocation(graph) : null), [graph, location]);
  const hasChange = useMemo(() => Boolean(graph?.nodes.some(isChangedNode)), [graph]);
  const behaviorCount = useMemo(() => {
    const counts = new Map<string, number>();
    for (const target of behavior?.targets ?? []) {
      for (const path of new Set(target.paths)) counts.set(path, (counts.get(path) ?? 0) + 1);
    }
    return (path: string) => counts.get(path) ?? 0;
  }, [behavior]);
  useEffect(() => {
    if (start) goTo({ kind: "xray", at: encodeXRayLocation(start) }, { replace: true });
  }, [start, goTo]);
  const move = useCallback((next: XRayLocation, replace: boolean) => goTo({ kind: "xray", at: encodeXRayLocation(next) }, { replace }), [goTo]);

  const note = !load
    ? loading ? "Preparing the X-Ray…" : "This change has no Map to walk, so there is no X-Ray for it."
    : read.state === "loading" ? "Opening the X-Ray…"
      : read.state === "failed" ? "The Map could not load."
        : !graph || (!location && !start) || (location && !location.rootId && !hasChange) ? "This revision's Map lists no changed file, so there is no change to draw."
          : "";
  if (graph && location && !note) {
    return (
      <XRay
        graph={graph}
        flow={read.state === "open" ? read.delta?.flow : null}
        targets={targets}
        rootId={location.rootId}
        side={location.side}
        walkSnapshot={location.snapshot}
        onSideChange={(side: XRaySide) => move({ ...location, side }, true)}
        onRootChange={(rootId) => move({ ...location, rootId }, false)}
        onRebuild={() => move({ ...location, snapshot: graphSnapshot(graph) }, true)}
        onOpenSource={openSource}
        onOpenEvidence={openEvidence}
        onOpenDataPath={() => goTo({ kind: "map", lens: "data-path", view: "changes" })}
        renderBehavior={renderBehavior}
        behaviorCount={behaviorCount}
      />
    );
  }
  return (
    <div role="status" className="mx-auto flex max-w-3xl flex-col items-start gap-2 px-4 py-6 text-label text-muted-foreground" data-testid="xray-note">
      <span>{note || "Opening the X-Ray…"}</span>
      {read.state === "failed" ? <Button variant="outline" size="sm" onClick={() => setAttempt((value) => value + 1)}>Try again</Button> : null}
      {note && read.state !== "loading" ? <Button variant="ghost" size="sm" onClick={back}>{inSession ? "Back" : "Back to the Overview"}</Button> : null}
    </div>
  );
}
