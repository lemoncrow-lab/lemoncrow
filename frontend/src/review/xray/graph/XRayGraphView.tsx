/**
 * The X-Ray graph screen body: the change drawn from its X-Ray graph document. Views run as tabs (the overview first,
 * then its drill-downs); Both / Before / After filters by the contract's rule; the walkthrough stages each step's view
 * or flow, dims everything outside its focus and moves the camera there. One legend row and at most one caveat line sit
 * above the picture; the rest of what we know about the drawing is behind "About this picture".
 */
import { ChevronLeft, ChevronRight, Info, ListOrdered, Network, Play, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Button } from "../../../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../../../components/ui/popover";
import { Sheet, SheetContent, SheetTitle } from "../../../components/ui/sheet";
import { ToggleGroup } from "../../../components/ui/toggle-group";
import { classifyFocus } from "../../flowKeymap";
import { layoutArchitecture } from "./archLayout";
import { ArchitectureView, highlightFromFocus, highlightFromSelection, type Highlight } from "./ArchitectureView";
import { layoutFlow } from "./flowLayout";
import { FlowView } from "./FlowView";
import {
  caveatLine,
  filterGraph,
  findView,
  focusBox,
  pulseOrder,
  resolveTourStep,
  sameHit,
  viewContent,
  viewForFlow,
  viewTabs,
  type GraphSide,
  type Hit,
} from "./graphModel";
import { Inspector } from "./Inspector";
import { useReducedMotion } from "./motion";
import { StackedGraph } from "./StackedGraph";
import { Viewport } from "./Viewport";
import type { XRayAnchor, XRayGraph, XRayView } from "./xrayGraphTypes";
import "./xrayGraph.css";

export type DrawResult = { ok: true } | { ok: false; reason: string };

export interface XRayGraphViewProps {
  graph: XRayGraph;
  /** The server is still drawing (the AI pass); this is the picture from the code index so far. */
  pending?: boolean;
  onOpenAnchor(anchor: XRayAnchor): void;
  /** The behavior change a payload names by `behavior_key`, with its judge controls; null when this revision lists none. */
  renderBehavior?(key: string): ReactNode;
  /** The behavior changes on these files, for a selected component; the payload's own key is `renderBehavior`. */
  renderFileBehavior?(paths: string[]): ReactNode;
  /** Asks the server for an AI drawing; offered when the document came from the code index alone. */
  onDrawWithAI?(): Promise<DrawResult>;
  /** The side and the selection, on mount and on every change, so the screen can keep them in its address. */
  onPlaceChange?(place: { side: GraphSide; selection: Hit | null }): void;
  /** Forces the wide or narrow layout; by default below 720px is narrow. */
  layout?: "wide" | "narrow";
  initialSide?: GraphSide;
  initialView?: string;
  initialSelection?: Hit | null;
  initialTourStep?: number | null;
}

const NARROW = "(max-width: 719px)";

function useNarrow(forced?: "wide" | "narrow"): boolean {
  const query = !forced && typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(NARROW) : null;
  const [narrow, setNarrow] = useState(() => (query ? query.matches : false));
  useEffect(() => {
    if (!query) return;
    const update = () => setNarrow(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, [query]);
  return forced ? forced === "narrow" : narrow;
}

const SIDES = [
  { value: "both", label: "Both", title: "The change: what is new, changed and removed" },
  { value: "before", label: "Before", title: "Before the change: no new parts" },
  { value: "after", label: "After", title: "After the change: no removed parts" },
];

export default function XRayGraphView(props: XRayGraphViewProps) {
  const { graph, pending = false, onOpenAnchor, renderBehavior, renderFileBehavior, onDrawWithAI } = props;
  const narrow = useNarrow(props.layout);
  const reduced = useReducedMotion();
  const [side, setSide] = useState<GraphSide>(props.initialSide ?? "both");
  const [view, setView] = useState<XRayView>(() => (props.initialView ? findView(graph.views, props.initialView)?.view : undefined) ?? graph.views[0]);
  const [selection, setSelection] = useState<Hit | null>(props.initialSelection ?? null);
  const steps = graph.walkthrough?.steps ?? [];
  const [tour, setTour] = useState<number | null>(props.initialTourStep ?? null);
  const [ai, setAi] = useState<{ busy: boolean; reason: string }>({ busy: false, reason: "" });
  const onPlaceChange = useRef(props.onPlaceChange);
  onPlaceChange.current = props.onPlaceChange;
  useEffect(() => { onPlaceChange.current?.({ side, selection }); }, [side, selection]);

  // A new document keeps the view when it still has it.
  useEffect(() => {
    setView((current) => findView(graph.views, current.id)?.view ?? (current.scope.kind === "flow" ? current : graph.views[0]));
  }, [graph]);

  const focus = useMemo(() => (tour !== null && steps[tour] ? resolveTourStep(graph, steps[tour]) : null), [graph, steps, tour]);
  const shown = focus ? focus.stage.view : view;
  const filtered = useMemo(() => filterGraph(graph, side), [graph, side]);
  const content = useMemo(() => viewContent(shown, filtered, graph), [shown, filtered, graph]);
  const nodes = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes]);
  const arch = useMemo(() => (content.lens === "architecture" ? layoutArchitecture(content) : null), [content]);
  const flow = useMemo(() => (content.lens === "flow" && content.flow ? layoutFlow(content.flow, nodes) : null), [content, nodes]);
  const pulse = useMemo(() => (content.lens === "architecture" ? pulseOrder(filtered, content.edges, side) : []), [content, filtered, side]);
  const highlight: Highlight | null = focus ? highlightFromFocus(focus.lit) : arch ? highlightFromSelection(arch, selection) : null;
  const atlas = arch?.atlas ?? flow?.atlas ?? null;
  const box = focus && atlas ? focusBox(atlas, focus.lit) : null;

  const startTour = useCallback((index: number) => {
    if (!steps.length) return;
    const next = Math.max(0, Math.min(steps.length - 1, index));
    setSelection(null);
    setTour(next);
  }, [steps.length]);
  const endTour = useCallback(() => {
    setTour((current) => {
      if (current !== null && steps[current]) setView(resolveTourStep(graph, steps[current]).stage.view);
      return null;
    });
  }, [graph, steps]);
  const advance = useCallback(() => {
    if (tour === null) startTour(0);
    else if (tour >= steps.length - 1) endTour();
    else startTour(tour + 1);
  }, [tour, steps.length, startTour, endTour]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target && classifyFocus({ tagName: target.tagName, isContentEditable: target.isContentEditable, role: target.getAttribute("role") }) === "field") return;
      if (document.querySelector("[role=dialog]")) return;
      if ((event.key === "w" || event.key === "W") && !event.shiftKey && steps.length) {
        event.preventDefault();
        advance();
      } else if (tour !== null && event.key === "ArrowRight") {
        event.preventDefault();
        advance();
      } else if (tour !== null && event.key === "ArrowLeft") {
        event.preventDefault();
        startTour(tour - 1);
      } else if (event.key === "Escape" && (tour !== null || selection)) {
        event.preventDefault();
        if (tour !== null) endTour();
        else setSelection(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, startTour, endTour, tour, selection, steps.length]);

  const choose = useCallback((next: XRayView) => {
    setTour(null);
    setSelection(null);
    setView(next);
  }, []);
  const showFlow = useCallback((flowId: string, stepId?: string) => {
    setTour(null);
    setView(viewForFlow(graph, flowId));
    setSelection(stepId ? { kind: "step", flow: flowId, id: stepId } : null);
  }, [graph]);
  const select = useCallback((hit: Hit) => {
    setSelection((current) => (sameHit(current, hit) ? null : hit));
  }, []);

  const drawWithAI = async () => {
    if (!onDrawWithAI) return;
    setAi({ busy: true, reason: "" });
    const result = await onDrawWithAI();
    setAi({ busy: false, reason: result.ok ? "" : result.reason });
  };

  const tabs = viewTabs(graph.views);
  const path = findView(graph.views, shown.id)?.path ?? [shown];
  const inTabs = tabs.some((tab) => tab.id === shown.id);
  const caveat = caveatLine(graph, pending);
  const offerAI = Boolean(onDrawWithAI) && graph.provenance.producer === "index" && !pending;
  const current = tour !== null ? steps[tour] : null;
  const inspector = selection ? (
    <Inspector
      graph={graph}
      hit={selection}
      onOpenAnchor={onOpenAnchor}
      renderBehavior={renderBehavior}
      renderFileBehavior={renderFileBehavior}
      onSelect={setSelection}
      onShowFlow={showFlow}
      onClose={() => setSelection(null)}
      className={narrow ? "xg-inspector-sheet" : undefined}
    />
  ) : null;
  const empty = content.lens === "flow"
    ? !content.flow ? "This flow is not in the drawing." : !flow?.messages.length ? "No step of this flow is on this side." : ""
    : !content.nodes.length ? (side === "both" ? "Nothing in this view." : `Nothing in this view ${side === "before" ? "before" : "after"} the change.`) : "";

  return (
    <div className="xg-root" data-testid="xg-root" data-narrow={narrow ? "true" : undefined} data-touring={current ? "true" : undefined} data-side={side}>
      <header className="xg-header">
        <div className="xg-heading">
          <h2 className="xg-title" title={graph.title}>{graph.title}</h2>
          <p className="xg-summary">{shown.summary || (content.lens === "flow" ? content.flow?.summary : "") || graph.summary}</p>
        </div>
        <div className="xg-controls">
          <ToggleGroup variant="segment" size="xs" aria-label="Side of the change" options={SIDES} value={side} onChange={(value) => setSide(value as GraphSide)} />
          {steps.length ? (
            <Button
              variant={current ? "solid" : "outline"}
              size="sm"
              onClick={() => (current ? endTour() : startTour(0))}
              icon={current ? <X size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
              tooltip={current ? "End the walkthrough (Esc)" : "Walk through the change (W)"}
              data-testid="xg-tour-toggle"
            >
              {current ? "End walkthrough" : `Walk through · ${steps.length}`}
            </Button>
          ) : null}
          {offerAI ? (
            <Button variant="outline" size="sm" onClick={() => void drawWithAI()} disabled={ai.busy} icon={<Sparkles size={14} aria-hidden="true" />} data-testid="xg-draw-ai">
              {ai.busy ? "Asking…" : "Draw with AI"}
            </Button>
          ) : null}
          {ai.reason ? <span role="status" className="xg-ai-reason" data-testid="xg-ai-reason">{ai.reason}</span> : null}
        </div>
      </header>

      <div className="xg-tabs" role="tablist" aria-label="Views">
        {tabs.map((tab) => (
          <Button
            key={tab.id}
            unstyled
            role="tab"
            className="xg-tab"
            aria-selected={tab.id === shown.id}
            data-testid="xg-view-tab"
            onClick={() => choose(tab)}
          >
            {tab.lens === "flow" ? <ListOrdered size={14} aria-hidden="true" /> : <Network size={14} aria-hidden="true" />}
            {tab.title}
            {tab.lens === "flow" ? <span className="xg-tab-kind">flow</span> : null}
          </Button>
        ))}
      </div>
      {!inTabs ? (
        <nav className="xg-crumbs" aria-label="Where you are">
          {path.map((entry, index) => (
            <span key={entry.id} className="flex items-center gap-1">
              {index ? <ChevronRight size={14} aria-hidden="true" /> : null}
              {index < path.length - 1 ? (
                <Button variant="link" size="compact" className="text-caption" onClick={() => choose(entry)}>{entry.title}</Button>
              ) : (
                <span className="font-medium text-foreground" aria-current="page">{entry.title}</span>
              )}
            </span>
          ))}
        </nav>
      ) : null}

      <div className="xg-subbar">
        <Legend />
        {caveat || graph.degraded.length || graph.provenance.recorded_runs.length ? (
          <div className="xg-caveat">
            {caveat ? <span className="xg-caveat-text" data-testid="xg-caveat" title={caveat}>{caveat}</span> : null}
            <AboutPicture graph={graph} />
          </div>
        ) : null}
      </div>

      <div className="xg-body">
        {narrow ? (
          <div className="flex flex-1 flex-col" style={current ? { paddingBottom: 132 } : undefined}>
            {empty ? <EmptyNote text={empty} /> : <StackedGraph arch={arch} flow={flow} nodes={nodes} selected={selection} highlight={highlight} onSelect={select} />}
          </div>
        ) : (
          <Viewport
            width={(arch ?? flow)?.width ?? 400}
            height={(arch ?? flow)?.height ?? 300}
            focus={box}
            focusKey={`${shown.id}|${tour ?? ""}`}
            reduced={reduced}
            label={`${shown.title}: ${content.lens === "flow" ? "flow" : "architecture"} picture. Drag or scroll to move, ctrl and scroll to zoom.`}
            bottomInset={current ? 112 : 0}
            onBackground={() => setSelection(null)}
          >
            {arch ? <ArchitectureView layout={arch} nodes={nodes} selected={selection} highlight={highlight} pulse={pulse} reduced={reduced} onSelect={select} /> : null}
            {flow ? <FlowView layout={flow} nodes={nodes} selected={selection} highlight={highlight} reduced={reduced} onSelect={select} /> : null}
          </Viewport>
        )}
        {!narrow && empty ? <div className="pointer-events-none absolute inset-x-0 top-10 flex justify-center"><EmptyNote text={empty} /></div> : null}
        {!narrow && inspector}
        {current ? (
          <div className="xg-caption" role="region" aria-label="Walkthrough" aria-live="polite" data-testid="xg-caption">
            <span className="xg-caption-step">{`${tour! + 1} / ${steps.length}`}</span>
            <div className="xg-caption-text">
              <span className="xg-caption-heading" data-testid="xg-caption-heading">{current.heading}</span>
              <span className="xg-caption-body">{current.body}</span>
              <span className="xg-caption-dots mt-1" aria-hidden="true">
                {steps.map((step, index) => <span key={step.id} data-on={index === tour ? "true" : undefined} />)}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Button variant="ghost" size="icon" aria-label="Previous step" tooltip="Previous (←)" disabled={tour === 0} onClick={() => startTour(tour! - 1)} icon={<ChevronLeft size={16} aria-hidden="true" />} />
              <Button variant="solid" size="sm" onClick={advance} data-testid="xg-tour-next">{tour! >= steps.length - 1 ? "Done" : "Next"}</Button>
              <Button variant="ghost" size="icon" aria-label="End the walkthrough" tooltip="End (Esc)" onClick={endTour} icon={<X size={16} aria-hidden="true" />} />
            </div>
          </div>
        ) : null}
      </div>
      {narrow ? (
        <Sheet open={Boolean(inspector)} onOpenChange={(open) => { if (!open) setSelection(null); }}>
          <SheetContent side="bottom" className="xg-vars flex max-h-[80dvh] flex-col" aria-describedby={undefined}>
            <SheetTitle className="sr-only">Details</SheetTitle>
            {inspector}
          </SheetContent>
        </Sheet>
      ) : null}
    </div>
  );
}

function EmptyNote({ text }: { text: string }) {
  return <p className="m-4 rounded-lg border border-border bg-card px-3 py-2 text-caption text-muted-foreground" role="status" data-testid="xg-empty">{text}</p>;
}

function Swatch({ tone, glyph }: { tone: string; glyph: string }) {
  return (
    <svg width={18} height={14} aria-hidden="true" className="xg-legend-swatch">
      <g className="xg-card" data-tone={tone}>
        <rect className="xg-card-bg" x={0.5} y={0.5} width={17} height={13} rx={3.5} />
        <text x={9} y={10.5} textAnchor="middle" className="xg-legend-glyph" data-tone={tone}>{glyph}</text>
      </g>
    </svg>
  );
}

function LineSample({ dashed, tone = "unchanged" }: { dashed?: boolean; tone?: string }) {
  return (
    <svg width={26} height={10} aria-hidden="true">
      <g className="xg-edge" data-tone={tone} data-line={dashed ? "dashed" : "solid"}>
        <path className="xg-edge-line" d="M1 5 L19 5" />
        <path className="xg-edge-head" d="M18 1.5 L25 5 L18 8.5 Z" />
      </g>
    </svg>
  );
}

/** One row: the three changes, then the three kinds of evidence and the pulse. */
function Legend() {
  return (
    <ul className="xg-legend" aria-label="Legend" data-testid="xg-legend">
      <li><Swatch tone="added" glyph="+" />New</li>
      <li><Swatch tone="modified" glyph="~" />Changed</li>
      <li><Swatch tone="removed" glyph={"−"} />Removed</li>
      <li className="xg-legend-sep" aria-hidden="true" />
      <li><LineSample />Recorded in a run</li>
      <li><LineSample dashed />Checked in code</li>
      <li>
        <LineSample dashed />
        <span className="rounded-sm border border-ai-line bg-ai-bg px-1 text-micro font-semibold text-ai">AI</span>
        AI guess
      </li>
      <li>
        <svg width={12} height={12} aria-hidden="true"><g className="xg-pulse" data-tone="unchanged"><circle className="xg-pulse-glow" cx={6} cy={6} r={6} /><circle className="xg-pulse-dot" cx={6} cy={6} r={3} /></g></svg>
        Data moving, in run order
      </li>
    </ul>
  );
}

function AboutPicture({ graph }: { graph: XRayGraph }) {
  const { provenance } = graph;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="compact" icon={<Info size={14} aria-hidden="true" />} data-testid="xg-about">About this picture</Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="xg-notes">
        <p className="m-0">
          {provenance.producer === "index+ai"
            ? `Drawn from the code index; names, flows and the walkthrough were added by AI${provenance.model ? ` (${provenance.model})` : ""}.`
            : "Drawn from the code index alone."}
        </p>
        {provenance.recorded_runs.length ? (
          <div>
            <span className="xg-eyebrow">Recorded runs</span>
            <ul>
              {provenance.recorded_runs.map((run) => (
                <li key={run.ref}>{`${run.recipe} · ${run.side === "base" ? "before" : "after"} the change · `}<span className="xg-mono">{run.ref}</span></li>
              ))}
            </ul>
          </div>
        ) : <p className="m-0 text-muted-foreground">No run was recorded on this revision.</p>}
        {graph.degraded.length ? (
          <div>
            <span className="xg-eyebrow">Left out or uncertain</span>
            <ul data-testid="xg-degraded">{graph.degraded.map((note) => <li key={note}>{note}</li>)}</ul>
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
