import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, Pause, Play, RefreshCw, ScanSearch } from "lucide-react";

import { Button } from "../../components/ui/button";
import { ToggleGroup } from "../../components/ui/toggle-group";
import { classifyFocus } from "../flowKeymap";
import type { ReviewMapGraph, ReviewMapSelection, ReviewTarget } from "../types";
import { LensCanvas, LensStack, LineSample, cardGlyph, type LensSelection } from "./XRayCanvas";
import { CardPanel, EdgePanel, type PanelHandlers } from "./XRayPanel";
import { buildChangeLens, buildFocusLens, lensView, type LensCard, type LensEdge } from "./xrayLens";
import type { XRay as XRayModel, XRayFlow, XRaySide, XRayStep } from "./xrayModel";
import "./xray.css";

export interface XRayProps {
  graph: ReviewMapGraph;
  flow?: XRayFlow | null;
  targets?: readonly ReviewTarget[] | null;
  /** The file whose path is zoomed into; "" draws the whole change. */
  rootId: string;
  side: XRaySide;
  /** The Snapshot this X-Ray was opened on; when the graph shows another, it is stale. */
  walkSnapshot: string;
  onSideChange(side: XRaySide): void;
  /** Zooms into a file's path, or back out to the whole change with "". */
  onRootChange(nodeId: string): void;
  /** Rebuilds a stale X-Ray on the Snapshot shown now. */
  onRebuild(): void;
  /** Jumps to exact code (a file or a relationship's site) through the host's own source navigation. */
  onOpenSource(selection: ReviewMapSelection): void;
  /** N04: challenge a step's claim on the zoomed-in path; without it no Challenge control renders. */
  onChallenge?(step: XRayStep, xray: XRayModel): void;
  /** Opens the recorded run behind a traced arrow; without it the trace is text. */
  onOpenEvidence?(selection: ReviewMapSelection): void;
  /** Where a reviewer goes for more than static arrows (the Map's Data path); without it no call to action renders. */
  onOpenDataPath?(): void;
  /** The behavior changes the host recorded on these files. */
  renderBehavior?(paths: string[], scope: "file" | "arrow"): ReactNode;
  /** How many recorded behavior changes name this file, for the card's marker. */
  behaviorCount?(path: string): number;
}

const SIDE_OPTIONS = [
  { value: "before", label: "Before", title: "The base: the picture before this change" },
  { value: "after", label: "After", title: "This revision: the picture after this change" },
];

/** Below this the lanes stack as lists: a canvas narrower than two lanes reads worse than a list. */
const STACK_BELOW = 720;
const PULSE_MS = 1500;
const PLAY_MS = 2400;

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** Reduced motion unless the browser says otherwise; without `matchMedia` (tests, old engines) nothing moves. */
function useReducedMotion(): boolean {
  const query = typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const [reduced, setReduced] = useState(() => (query ? query.matches : true));
  useEffect(() => {
    if (!query) return;
    const update = () => setReduced(query.matches);
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, [query]);
  return reduced;
}

/**
 * Behavioral X-Ray as a picture: the whole change in lanes by layer, or one file's path zoomed in, on the Before or
 * After side. An arrow is solid only when this Snapshot's recorded run observed it on the side shown; a dot runs
 * along those in their recorded order. Every other arrow is dashed: a relationship in the code, not an execution.
 */
export default function XRay({
  graph,
  flow,
  targets,
  rootId,
  side,
  walkSnapshot,
  onSideChange,
  onRootChange,
  onRebuild,
  onOpenSource,
  onChallenge,
  onOpenEvidence,
  onOpenDataPath,
  renderBehavior,
  behaviorCount,
}: XRayProps) {
  const diagram = useMemo(
    () => (rootId ? buildFocusLens({ graph, flow, targets, rootId, walkSnapshot }) : buildChangeLens({ graph, flow, targets, walkSnapshot })),
    [flow, graph, rootId, targets, walkSnapshot],
  );
  const view = useMemo(() => lensView(diagram, side), [diagram, side]);
  const reduced = useReducedMotion();
  const ready = view.status === "ready";

  const stageRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const measure = () => setWidth(stage.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [ready]);
  const stacked = width > 0 && width < STACK_BELOW;

  const [selection, setSelection] = useState<LensSelection>(null);
  const [walk, setWalk] = useState<{ index: number; playing: boolean } | null>(null);
  // A new picture (zoom, root, Snapshot) starts unselected; a side switch keeps what is still drawn.
  useEffect(() => { setSelection(null); setWalk(null); }, [diagram]);
  useEffect(() => {
    setWalk(null);
    setSelection((current) => {
      if (!current) return current;
      const present = current.kind === "card" ? view.cards.some((card) => card.id === current.id) : view.edges.some((edge) => edge.pair === current.id);
      return present ? current : null;
    });
  }, [view]);

  const steps = view.steps;
  const step = walk ? steps[Math.min(walk.index, steps.length - 1)] ?? null : null;
  const startCard = steps[0]?.cardId ?? view.cards.find((card) => card.kind === "node")?.id ?? "";
  const defaultCard = diagram.mode === "focus" ? diagram.rootId : startCard;
  const shown: LensSelection = step ? { kind: "card", id: step.cardId } : selection ?? (defaultCard ? { kind: "card", id: defaultCard } : null);
  const selectedCard = shown?.kind === "card" ? view.cards.find((card) => card.id === shown.id) ?? null : null;
  const selectedEdge = shown?.kind === "edge" ? view.edges.find((edge) => edge.pair === shown.id) ?? null : null;

  const revealPanel = useCallback(() => {
    if (stacked) panelRef.current?.scrollIntoView?.({ block: "start", behavior: reduced ? "auto" : "smooth" });
  }, [stacked, reduced]);
  const chooseCard = useCallback((card: LensCard) => {
    setWalk(null);
    // On the whole change a file opens its path; a fold, or any card on a zoomed-in path, shows its details.
    if (diagram.mode === "change" && card.kind === "node") {
      onRootChange(card.nodeId);
      return;
    }
    setSelection({ kind: "card", id: card.id });
    revealPanel();
  }, [diagram.mode, onRootChange, revealPanel]);
  const chooseEdge = useCallback((edge: LensEdge) => {
    setWalk(null);
    setSelection({ kind: "edge", id: edge.pair });
    revealPanel();
  }, [revealPanel]);

  // The walkthrough: play steps on its own, W steps by hand, Escape stops.
  const advance = useCallback(() => {
    if (!steps.length) return;
    setWalk((current) => (!current ? { index: 0, playing: false } : { index: Math.min(steps.length - 1, current.index + 1), playing: false }));
  }, [steps.length]);
  const togglePlay = () => {
    if (!steps.length) return;
    setWalk((current) => {
      if (!current) return { index: 0, playing: true };
      if (current.playing) return { ...current, playing: false };
      return { index: current.index >= steps.length - 1 ? 0 : current.index, playing: true };
    });
  };
  useEffect(() => {
    if (!walk?.playing) return;
    if (walk.index >= steps.length - 1) {
      setWalk({ ...walk, playing: false });
      return;
    }
    const timer = setTimeout(() => setWalk({ index: walk.index + 1, playing: true }), PLAY_MS);
    return () => clearTimeout(timer);
  }, [walk, steps.length]);
  useEffect(() => {
    if (!ready) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target && classifyFocus({ tagName: target.tagName, isContentEditable: target.isContentEditable, role: target.getAttribute("role") }) === "field") return;
      if (document.querySelector("[role=dialog]")) return;
      if ((event.key === "w" || event.key === "W") && !event.shiftKey) {
        event.preventDefault();
        advance();
      } else if (event.key === "Escape" && walk) {
        event.preventDefault();
        setWalk(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready, advance, walk]);

  // The moving dot: along each arrow observed in order, one after another; on the walkthrough's arrow when it is one.
  const inOrder = useMemo(
    () => view.edges.filter((edge) => edge.observed && edge.sequence !== null).sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0) || a.key.localeCompare(b.key)),
    [view.edges],
  );
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (reduced || !inOrder.length || stacked) return;
    const timer = setInterval(() => setTick((value) => value + 1), PULSE_MS);
    return () => clearInterval(timer);
  }, [reduced, inOrder.length, stacked]);
  const activeEdge = step?.edgeKey ? view.edges.find((edge) => edge.key === step.edgeKey) ?? null : null;
  const pulse = reduced || stacked
    ? null
    : walk
      ? activeEdge && activeEdge.observed && activeEdge.sequence !== null ? { key: activeEdge.key, tick } : null
      : inOrder.length ? { key: inOrder[tick % inOrder.length].key, tick } : null;

  const handlers: PanelHandlers = {
    onOpenSource,
    onFocus: (nodeId) => { if (nodeId) onRootChange(nodeId); },
    onSelectCard: (id) => { setWalk(null); setSelection({ kind: "card", id }); },
    onSelectEdge: (pair) => { setWalk(null); setSelection({ kind: "edge", id: pair }); },
    onOpenEvidence,
    onChallenge: onChallenge && diagram.walks
      ? (card) => {
        const walked = diagram.walks?.[side];
        const own = card.steps?.[side];
        if (walked && own) onChallenge(own, walked);
      }
      : undefined,
    renderBehavior,
  };

  const sideWord = side === "before" ? "Before" : "After";
  const summary = diagram.mode === "change"
    ? [plural(diagram.totals.changed, "changed file"), diagram.totals.context ? plural(diagram.totals.context, "unchanged neighbour") : "", `${plural(view.edges.length, "arrow")} on ${sideWord}`, view.edges.length ? `${view.observed} observed` : ""]
    : [plural(view.cards.filter((card) => card.kind === "node").length, "file"), `${plural(view.edges.length, "arrow")} on ${sideWord}`, view.edges.length ? `${view.observed} observed` : ""];
  const others = view.cards.filter((card) => card.place === "other");
  const notes = diagram.notMeasured.length + diagram.degraded.length + diagram.bounded.length;
  const draw = { diagram, view, side, selection: shown, active: step ? { cardId: step.cardId, edgeKey: step.edgeKey } : null, pulse, behaviorCount, onCard: chooseCard, onEdge: chooseEdge };

  return (
    <section
      className="xr-root"
      aria-label="Behavioral X-Ray"
      data-testid="xray"
      data-side={side}
      data-status={view.status}
      data-mode={diagram.mode}
      data-layout={stacked ? "stack" : "canvas"}
    >
      <header className="xr-header">
        <div className="xr-heading-row">
          <ScanSearch size={16} aria-hidden="true" className="shrink-0 text-subtle" />
          <nav className="xr-crumbs" aria-label="X-Ray level">
            {diagram.mode === "focus" ? (
              <>
                <Button variant="ghost" size="compact" data-testid="xray-whole-change" onClick={() => onRootChange("")} icon={<ArrowLeft size={14} />}>
                  Whole change
                </Button>
                <span className="xr-crumb-sep" aria-hidden="true">/</span>
                <h3 className="xr-crumb-current font-mono" data-testid="xray-root-title">{`${diagram.rootTitle} · its path`}</h3>
              </>
            ) : (
              <h3 className="xr-crumb-current">Whole change</h3>
            )}
          </nav>
          {ready && <span className="xr-summary" data-testid="xray-summary">{summary.filter(Boolean).join(" · ")}</span>}
        </div>
        <div className="xr-controls">
          <ToggleGroup
            aria-label="Side of the change"
            variant="segment"
            options={SIDE_OPTIONS}
            value={side}
            onChange={(value) => onSideChange(value === "before" ? "before" : "after")}
          />
          {ready && (
            <Button
              variant="outline"
              size="xs"
              data-testid="xray-walk-play"
              disabled={!steps.length}
              tooltip={steps.length ? "Step through the picture: observed calls first, then reading order (W)" : ""}
              onClick={togglePlay}
              icon={walk?.playing ? <Pause size={14} /> : <Play size={14} />}
            >
              {walk ? (walk.playing ? "Pause" : "Play") : "Walk through"}
            </Button>
          )}
        </div>
      </header>

      {ready && (
        <div className="xr-subbar">
          <ul className="xr-legend" aria-label="How to read the picture" data-testid="xray-legend">
            <li><span className="xr-glyph" data-tone="added">+</span>added</li>
            <li><span className="xr-glyph" data-tone="changed">~</span>changed</li>
            <li><span className="xr-glyph" data-tone="removed">−</span>removed</li>
            <li><span className="xr-glyph" data-tone="unchanged">=</span>unchanged</li>
            <li><LineSample line="solid" tone="unchanged" />observed in a run</li>
            <li><LineSample line="dashed" tone="unchanged" />static</li>
            {!reduced && inOrder.length > 0 && <li><span className="xr-dot-sample" aria-hidden="true" />recorded order</li>}
          </ul>
          {view.caveat && (
            <p className="xr-caveat" data-testid="xray-caveat" data-observed={view.observed}>
              <span>{view.caveat.text}</span>
              {view.caveat.action === "data_path" && onOpenDataPath && (
                <Button
                  variant="link"
                  size="compact"
                  className="text-caption"
                  data-testid="xray-caveat-action"
                  tooltip="The Map's Data path traces sources to sinks in the code. A recorded run of this revision is what makes an arrow solid here."
                  onClick={onOpenDataPath}
                >
                  Trace data on the Map
                </Button>
              )}
            </p>
          )}
        </div>
      )}

      {ready && walk && step && (
        <div className="xr-walk" data-testid="xray-walk" role="group" aria-label="Walkthrough">
          <Button variant="ghost" size="icon" aria-label="Previous step" disabled={walk.index === 0} onClick={() => setWalk({ index: Math.max(0, walk.index - 1), playing: false })}>
            <ChevronLeft size={14} />
          </Button>
          <span className="xr-walk-count">{`Step ${walk.index + 1} of ${steps.length}`}</span>
          <span className="xr-walk-label" data-testid="xray-walk-step" data-basis={step.basis} aria-live="polite">{step.label}</span>
          <Button variant="ghost" size="icon" aria-label="Next step" disabled={walk.index >= steps.length - 1} onClick={() => setWalk({ index: Math.min(steps.length - 1, walk.index + 1), playing: false })}>
            <ChevronRight size={14} />
          </Button>
          <Button variant="ghost" size="compact" onClick={() => setWalk(null)}>Stop</Button>
        </div>
      )}

      {!ready ? (
        <div className="xr-notice" role="status" data-testid="xray-status">
          <p>{view.statusNote}</p>
          {view.status === "stale" && (
            <Button variant="outline" size="xs" onClick={onRebuild} icon={<RefreshCw size={14} />}>Rebuild on this revision</Button>
          )}
          {view.status === "absent" && (
            <Button variant="outline" size="xs" onClick={() => onSideChange(side === "before" ? "after" : "before")}>
              {`Show ${side === "before" ? "After" : "Before"}`}
            </Button>
          )}
          {diagram.mode === "focus" && view.status !== "stale" && (
            <Button variant="ghost" size="xs" onClick={() => onRootChange("")} icon={<ArrowLeft size={14} />}>Whole change</Button>
          )}
          {diagram.notMeasured.length > 0 && view.status === "not_measured" && <About diagram={diagram} notes={diagram.notMeasured.length} />}
        </div>
      ) : (
        <div className="xr-body" data-layout={stacked ? "stack" : "canvas"}>
          <div className="xr-stage" ref={stageRef}>
            <div className="xr-scroll">
              {stacked ? <LensStack {...draw} /> : <LensCanvas {...draw} width={width} />}
            </div>
            {others.length > 0 && (
              <section className="xr-other" aria-label="Docs, config and assets in this change" data-testid="xray-other">
                <h4 className="xr-lane-label">{`Docs, config & assets · ${others.reduce((sum, card) => sum + (card.folded ? card.folded.count : 1), 0)}`}</h4>
                <ul>
                  {others.map((card) => (
                    <li key={card.id}>
                      <Button
                        unstyled
                        className="xr-other-chip"
                        data-testid="xray-card"
                        data-card-id={card.id}
                        data-kind={card.kind}
                        data-tone={card.kind === "fold" ? "fold" : card.change}
                        data-selected={shown?.kind === "card" && shown.id === card.id ? "true" : "false"}
                        tooltip={card.kind === "node" ? card.path : ""}
                        onClick={() => { setSelection({ kind: "card", id: card.id }); revealPanel(); }}
                      >
                        <span className="xr-glyph" data-tone={card.kind === "fold" ? "fold" : card.change} aria-hidden="true">{cardGlyph(card)}</span>
                        <span>{card.title}</span>
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <About diagram={diagram} notes={notes} />
          </div>
          <div className="xr-side" ref={panelRef}>
            {selectedEdge ? (
              <EdgePanel edge={selectedEdge} view={view} side={side} handlers={handlers} />
            ) : selectedCard ? (
              <CardPanel
                card={selectedCard}
                diagram={diagram}
                view={view}
                side={side}
                start={!selection && !step && diagram.mode === "change" && selectedCard.id === startCard}
                targetsKnown={Boolean(targets)}
                handlers={handlers}
              />
            ) : (
              <aside className="xr-panel" data-testid="xray-detail">
                <p className="xr-panel-note">Choose a file or an arrow to see what is known about it.</p>
              </aside>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function About({ diagram, notes }: { diagram: ReturnType<typeof buildChangeLens>; notes: number }) {
  return (
    <details className="xr-about" data-testid="xray-not-measured">
      <summary>{`About this view · ${plural(notes, "note")}`}</summary>
      <ul>
        {diagram.notMeasured.map((item) => (
          <li key={item.key} data-key={item.key}>
            <span className="font-medium text-foreground">{item.label}</span>
            <span>{item.reason}</span>
            <span className="xr-needs">{`Needs: ${item.needs}`}</span>
          </li>
        ))}
        {diagram.degraded.map((line) => <li key={line} data-testid="xray-degraded">{line}</li>)}
        {diagram.bounded.map((line) => <li key={line} data-testid="xray-truncated">{line}</li>)}
        <li>Click a file to open its path, an arrow to see its evidence. W walks through; Escape stops.</li>
      </ul>
    </details>
  );
}
