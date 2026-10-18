import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { BookOpen, Braces, ChevronDown, ChevronRight, ChevronUp, CircleAlert, CircleCheck, CircleHelp, Database, Folder, MessageSquare, ShieldAlert } from "lucide-react";

import { Button } from "../components/ui/button";
import FileTypeIcon from "../components/FileTypeIcon";
import {
  EDGE_KIND_LABEL,
  LAYOUT,
  baseName,
  changeLetter,
  isChangedStatus,
  isFullyReviewed,
  isTestNode,
  nodeName,
  symbolKindLabel,
  type MapCardLayout,
  type MapEdgeLayout,
  type MapLane,
  type MapModel,
} from "./mapModel";
import type { MapLevel, MapMemberSymbol, MapNode } from "./types";
import "./map.css";

type CardState = "idle" | "selected" | "neighbor" | "dim";

const LANE_TITLE: Record<MapLevel, Record<MapLane, string>> = {
  architecture: { used_by: "Used by", subject: "Changed", uses: "Uses" },
  file: { used_by: "Used by", subject: "File", uses: "Uses" },
  symbol: { used_by: "Callers & affected sites", subject: "Symbol", uses: "Calls" },
};

const LANE_HINT: Record<MapLane, string> = {
  used_by: "Code that calls, imports, or is affected by the subject. Changes here can break it.",
  subject: "What this review is about.",
  uses: "Code the subject calls or imports.",
};

function plural(count: number, one: string, many = one + "s"): string {
  return count.toLocaleString() + " " + (count === 1 ? one : many);
}

export function ChangeBadge({ node, small = false }: { node: MapNode; small?: boolean }) {
  const letter = changeLetter(node);
  const title = letter === "A" ? "Added" : letter === "D" ? "Deleted" : letter === "R" ? "Renamed" : letter === "C" ? "Copied" : "Modified";
  return <span className="rm-badge" data-change={letter} data-small={small} title={title} aria-label={title}>{letter}</span>;
}

function DiffBar({ additions, deletions }: { additions: number; deletions: number }) {
  const total = additions + deletions;
  if (!total) return null;
  const blocks = 5;
  const filled = Math.max(1, Math.min(blocks, Math.round(Math.log10(total + 1) * 2)));
  const add = Math.round((additions / total) * filled);
  return (
    <span className="rm-diffbar" aria-hidden="true">
      {Array.from({ length: blocks }, (_, index) => (
        <i key={index} data-v={index < add ? "add" : index < filled ? "del" : ""} />
      ))}
    </span>
  );
}

export function NodeSignals({ node, size = 12 }: { node: MapNode; size?: number }) {
  const signals = [];
  if (node.important_public_contract) {
    signals.push(<Braces key="contract" size={size} className="rm-signal" aria-label="Public contract changed"><title>Public contract changed</title></Braces>);
  }
  if (node.boundaries?.includes("security")) {
    signals.push(<ShieldAlert key="security" size={size} className="rm-signal" aria-label="Security-sensitive path"><title>Security-sensitive path</title></ShieldAlert>);
  }
  if (node.boundaries?.includes("persistence")) {
    signals.push(<Database key="persistence" size={size} className="rm-signal" aria-label="Persistence path"><title>Persistence path</title></Database>);
  }
  if (node.uncertain) {
    signals.push(<CircleHelp key="uncertain" size={size} className="rm-signal" data-tone="muted" aria-label="Uncertain impact"><title>Uncertain impact</title></CircleHelp>);
  }
  return <>{signals}</>;
}

function ReviewMark({ node }: { node: MapNode }) {
  const review = node.review;
  if (!review || !review.targets) return null;
  const threads = review.open_threads > 0
    ? <span className="inline-flex items-center gap-0.5" title={plural(review.open_threads, "open thread")}><MessageSquare size={11} aria-hidden="true" />{review.open_threads}</span>
    : null;
  if (review.needs_changes) {
    return <>{threads}<CircleAlert size={13} className="rm-signal" data-tone="bad" aria-label="Needs changes"><title>Needs changes</title></CircleAlert></>;
  }
  if (isFullyReviewed(node)) {
    return <>{threads}<CircleCheck size={13} className="rm-signal" data-tone="good" aria-label="Reviewed"><title>Reviewed</title></CircleCheck></>;
  }
  return <>{threads}<span title={`${review.reviewed} of ${review.targets} review targets marked`}>{review.reviewed}/{review.targets}</span></>;
}

/** Live overlay: which agent sessions are touching this node right now. */
function SessionMarks({ node }: { node: MapNode }) {
  if (!node.sessions?.length) return null;
  return (
    <span className="rm-sessions" title={`Agent activity: ${node.sessions.join(", ")}`}>
      {node.sessions.slice(0, 3).map((mark) => <i key={mark}>{mark}</i>)}
    </span>
  );
}

function stageY(value: number): number {
  return value - LAYOUT.laneHeader;
}

interface CardProps {
  card: MapCardLayout;
  state: CardState;
  activeSymbolId: string | null;
  weight: number;
  onSelect(id: string): void;
  onOpen(id: string): void;
  onHover(id: string | null): void;
  onSymbolHover(symbolId: string | null): void;
  onOpenSymbol(symbol: MapMemberSymbol): void;
  onToggleFold(key: string): void;
  registerRef(id: string, element: HTMLButtonElement | null): void;
}

const MapCard = memo(function MapCard({
  card,
  state,
  activeSymbolId,
  weight,
  onSelect,
  onOpen,
  onHover,
  onSymbolHover,
  onOpenSymbol,
  onToggleFold,
  registerRef,
}: CardProps) {
  const node = card.node;
  const style = { left: card.x, top: stageY(card.y), width: card.w, height: card.h };
  const test = isTestNode(node);
  const common = {
    ref: (element: HTMLButtonElement | null) => registerRef(card.id, element),
    "data-node-id": card.id,
    "data-state": state,
    "data-variant": card.variant,
    "data-exactness": node.exactness,
    "data-activity": node.activity,
    "aria-pressed": state === "selected",
    onClick: () => onSelect(card.id),
    onDoubleClick: () => onOpen(card.id),
    onMouseEnter: () => onHover(card.id),
    onMouseLeave: () => onHover(null),
    onFocus: () => onHover(null),
  };

  if (card.variant === "context") {
    const changed = isChangedStatus(node);
    return (
      <Button
        unstyled
        {...common}
        className="rm-card"
        style={style}
        aria-label={`${nodeName(node)}, ${node.status === "impacted" ? "affected by the change" : changed ? "also changed" : "surrounding code"}`}
        title={node.path}
      >
        {changed ? <ChangeBadge node={node} small /> : <FileTypeIcon path={node.path} size={13} />}
        <span className="rm-card-name">{nodeName(node)}</span>
        <span className="rm-trailing">
          <SessionMarks node={node} />
          {node.status === "impacted" && <span className="rm-affected">affected</span>}
          {card.sharedBy
            ? <span title={`Linked to ${card.sharedBy} changed files. Hover to see its links.`}>{card.sharedBy} files</span>
            : node.status === "context" && weight > 1 && <span title="Link sites">×{weight}</span>}
        </span>
      </Button>
    );
  }

  if (card.variant === "symbol-focus") {
    const calls = typeof node.caller_count === "number" && node.caller_count >= 0 ? node.caller_count : null;
    return (
      <Button unstyled {...common} className="rm-card" style={{ ...style, justifyContent: "flex-start", paddingTop: 12, paddingBottom: 12, gap: 6 }} aria-label={`${node.label}, focused symbol`}>
        <span className="rm-card-row">
          {isChangedStatus(node) && <ChangeBadge node={node} />}
          <span className="rm-card-name" style={{ fontSize: 14 }}>{node.label}</span>
          <span className="rm-trailing"><NodeSignals node={node} /></span>
        </span>
        {node.qualified_name && node.qualified_name !== node.label && <span className="rm-focus-path">{node.qualified_name}</span>}
        <span className="rm-focus-path">{node.path}{node.line > 0 ? `:${node.line}${node.end_line > node.line ? `–${node.end_line}` : ""}` : ""}</span>
        <span className="rm-card-meta">
          {node.kind && <span>{symbolKindLabel(node.kind)}</span>}
          {calls !== null && <span>{plural(calls, "caller")} at capture</span>}
          {typeof node.centrality_rank === "number" && <span>centrality #{node.centrality_rank}</span>}
        </span>
      </Button>
    );
  }

  if (card.variant === "file-focus") {
    const symbols = card.rows ?? [];
    return (
      <div
        className="rm-card"
        data-node-id={card.id}
        data-state={state}
        data-variant={card.variant}
        style={{ ...style, justifyContent: "flex-start", padding: "0 6px 6px" }}
        onMouseEnter={() => onHover(card.id)}
        onMouseLeave={() => onHover(null)}
      >
        <Button unstyled {...common} onMouseEnter={undefined} onMouseLeave={undefined} className="rm-focus-header" aria-label={`${nodeName(node)}, focused file`}>
          <span className="rm-card-row">
            {isChangedStatus(node) ? <ChangeBadge node={node} /> : <FileTypeIcon path={node.path} size={15} />}
            <span className="rm-card-name" style={{ fontSize: 14 }}>{nodeName(node)}</span>
            <span className="rm-trailing"><NodeSignals node={node} /><ReviewMark node={node} /></span>
          </span>
          <span className="rm-focus-path">{node.path}</span>
          <span className="rm-card-meta">
            {(node.additions || node.deletions) ? <><span className="rm-add">+{node.additions ?? 0}</span><span className="rm-del">−{node.deletions ?? 0}</span></> : null}
            <span>
              {node.status === "subject"
                ? symbols.length ? plural(node.symbol_count ?? symbols.length, "symbol") : "No indexed symbols"
                : symbols.length ? plural(node.symbol_count ?? symbols.length, "changed symbol") : isChangedStatus(node) ? "No symbol boundary captured" : node.status === "impacted" ? "Affected by the change" : "Surrounding code"}
            </span>
          </span>
        </Button>
        {symbols.map(({ symbol }) => (
          <Button
            unstyled
            key={symbol.id}
            className="rm-symbol-row"
            data-active={activeSymbolId === symbol.id}
            onMouseEnter={() => onSymbolHover(symbol.id)}
            onMouseLeave={() => onSymbolHover(null)}
            onFocus={() => onSymbolHover(symbol.id)}
            onBlur={() => onSymbolHover(null)}
            onClick={() => {
              // Opening the symbol unmounts this row before it can report the
              // pointer leaving, so the hover would outlive it.
              onSymbolHover(null);
              onOpenSymbol(symbol);
            }}
            aria-label={`Open symbol ${symbol.label}`}
            title={symbol.qualified_name || symbol.label}
          >
            <span className="rm-kind-tag" title={symbol.kind || undefined}>{symbolKindLabel(symbol.kind)}</span>
            <span className="rm-symbol-name">{symbol.label}</span>
            <span className="rm-trailing">
              {symbol.change && symbol.change !== "modified" && node.status !== "new" && <span>{symbol.change}</span>}
              {symbol.caller_count > 0 && <span>{plural(symbol.caller_count, "caller")}</span>}
            </span>
          </Button>
        ))}
        {card.symbolFold && (
          <Button
            unstyled
            className="rm-symbol-more"
            onClick={() => onToggleFold(card.symbolFold!.key)}
            aria-expanded={card.symbolFold.expanded}
          >
            {card.symbolFold.expanded ? <ChevronUp size={12} aria-hidden="true" /> : <ChevronDown size={12} aria-hidden="true" />}
            {card.symbolFold.expanded
              ? `Show fewer${card.symbolFold.unlisted ? ` · ${card.symbolFold.unlisted} more only in the diff` : ""}`
              : `Show ${plural(card.symbolFold.hidden, "more symbol")}`}
          </Button>
        )}
      </div>
    );
  }

  const reviewed = isFullyReviewed(node);
  const symbolCount = node.symbol_count ?? node.symbols?.length ?? 0;
  return (
    <Button
      unstyled
      {...common}
      className="rm-card"
      style={style}
      data-reviewed={reviewed}
      data-deleted={changeLetter(node) === "D"}
      aria-label={`${nodeName(node)}, ${node.status === "subject" ? "in focus" : node.status === "new" ? "added" : changeLetter(node) === "D" ? "deleted" : "changed"}`}
      title={node.path}
    >
      <span className="rm-card-row">
        {node.status === "subject" ? <FileTypeIcon path={node.path} size={14} /> : <ChangeBadge node={node} />}
        <span className="rm-card-name">{nodeName(node)}</span>
        <span className="rm-trailing">
          <SessionMarks node={node} />
          <NodeSignals node={node} />
          <ReviewMark node={node} />
        </span>
      </span>
      <span className="rm-card-meta">
        {(node.additions || node.deletions) ? (
          <>
            <span className="rm-add">+{node.additions ?? 0}</span>
            <span className="rm-del">−{node.deletions ?? 0}</span>
            <DiffBar additions={node.additions ?? 0} deletions={node.deletions ?? 0} />
          </>
        ) : null}
        {symbolCount > 0 && <span>{plural(symbolCount, "symbol")}</span>}
        {test && <span>test</span>}
      </span>
    </Button>
  );
});

interface PlacedLabel {
  key: string;
  text: string;
  x: number;
  y: number;
  width: number;
}

function labelText(edge: MapEdgeLayout): string {
  return EDGE_KIND_LABEL[edge.kind] + (edge.edge.weight > 1 ? ` ×${edge.edge.weight}` : "");
}

/**
 * Greedy label placement: strongest links first, nudged vertically to avoid
 * each other, dropped when there is no free slot. Coupling arcs inside the
 * change are not labelled; their gutter is too narrow and color carries kind.
 */
// Label pills are sized from the rendered font, not a per-character guess, so
// collision checks match what is drawn. jsdom has no canvas; it estimates.
const LABEL_FALLBACK_FONT = 'ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
let labelContext: CanvasRenderingContext2D | null | undefined;
const labelWidths = new Map<string, number>();

function labelWidth(text: string): number {
  const cached = labelWidths.get(text);
  if (cached !== undefined) return cached;
  if (labelContext === undefined) {
    labelContext = null;
    if (typeof document !== "undefined" && !/jsdom/i.test(navigator.userAgent)) {
      const context = document.createElement("canvas").getContext("2d");
      if (context) {
        const scope = document.querySelector(".review-app") ?? document.documentElement;
        const family = getComputedStyle(scope).getPropertyValue("--review-ui-font").trim() || LABEL_FALLBACK_FONT;
        context.font = `10px ${family}`;
        labelContext = context;
      }
    }
  }
  const measured = labelContext ? labelContext.measureText(text).width : text.length * 5.6;
  const width = Math.ceil(measured) + 14;
  // Labels are a small vocabulary ("calls ×3", "imports"); the cap only guards
  // against unbounded growth over a very long session.
  if (labelWidths.size > 400) labelWidths.clear();
  labelWidths.set(text, width);
  return width;
}

function placeLabels(edges: MapEdgeLayout[], limit = 16): PlacedLabel[] {
  const placed: PlacedLabel[] = [];
  const seen = new Set<string>();
  const ordered = [...edges]
    .filter((edge) => !edge.internal)
    .sort((a, b) => (b.edge.weight || 0) - (a.edge.weight || 0));
  for (const edge of ordered) {
    if (placed.length >= limit || seen.has(edge.id)) continue;
    seen.add(edge.id);
    const text = labelText(edge);
    const width = labelWidth(text);
    for (const dy of [0, 20, -20, 40, -40]) {
      const x = edge.labelX - width / 2;
      const y = edge.labelY + dy - 9;
      const clash = placed.some((other) => x < other.x + other.width + 4 && other.x < x + width + 4 && y < other.y + 20 && other.y < y + 20);
      if (!clash) {
        placed.push({ key: edge.key, text, x, y, width });
        break;
      }
    }
  }
  return placed;
}

function EdgeLabel({ label }: { label: PlacedLabel }) {
  return (
    // Drawn inside the edge layer, which is already shifted into stage space.
    <g className="rm-edge-label" transform={`translate(${label.x},${label.y})`}>
      <rect width={label.width} height={18} rx={9} />
      <text x={label.width / 2} y={12.5} textAnchor="middle">{label.text}</text>
    </g>
  );
}

export interface MapCanvasProps {
  model: MapModel;
  selectedId: string | null;
  selectedEdgeId: string | null;
  onSelect(id: string | null): void;
  onSelectEdge(id: string): void;
  onOpen(id: string): void;
  onOpenSymbol(symbol: MapMemberSymbol): void;
  onToggleGroup(key: string): void;
  onWidth(width: number): void;
  /** Lane titles for this surface; defaults read as a review of a change. */
  laneTitles?: Partial<Record<MapLane, string>>;
  ariaLabel?: string;
}

export default function MapCanvas({
  model,
  selectedId,
  selectedEdgeId,
  onSelect,
  onSelectEdge,
  onOpen,
  onOpenSymbol,
  onToggleGroup,
  onWidth,
  laneTitles,
  ariaLabel = "Review map canvas",
}: MapCanvasProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const cardRefs = useRef(new Map<string, HTMLButtonElement>());
  const [hoveredCardId, setHoveredCardId] = useState<string | null>(null);
  const [hoverEdgeId, setHoverEdgeId] = useState<string | null>(null);
  const [hoveredSymbolId, setHoveredSymbolId] = useState<string | null>(null);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    onWidth(element.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => onWidth(element.clientWidth));
    });
    observer.observe(element);
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [onWidth]);

  // Keep the selection on screen when it changes from outside the canvas
  // (search, inspector, keyboard).
  useEffect(() => {
    if (!selectedId) return;
    const element = cardRefs.current.get(selectedId);
    if (element && typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [selectedId]);

  const registerRef = useCallback((id: string, element: HTMLButtonElement | null) => {
    if (element) cardRefs.current.set(id, element);
    else cardRefs.current.delete(id);
  }, []);

  // A card or symbol row can leave the map under the pointer (drill-in,
  // navigation), which never reports the pointer leaving: only what is still
  // on the map counts as hovered.
  const rowSymbolIds = useMemo(
    () => new Set(model.cards.flatMap((card) => (card.rows ?? []).map((row) => row.symbol.id))),
    [model.cards],
  );
  const activeSymbolId = hoveredSymbolId && rowSymbolIds.has(hoveredSymbolId) ? hoveredSymbolId : null;
  const hoverId = hoveredCardId && model.cardById.has(hoveredCardId) ? hoveredCardId : null;
  const selectedEdge = selectedEdgeId ? model.edges.find((edge) => edge.id === selectedEdgeId) ?? null : null;
  const hoverEdge = hoverEdgeId ? model.edges.find((edge) => edge.key === hoverEdgeId) ?? null : null;
  // The focused file or symbol relates to everything on its map, so treating
  // it as active would light every link. It rests; its neighbors and symbol
  // rows drive highlighting instead.
  const focusCardId = model.cards.find((card) => card.variant === "file-focus" || card.variant === "symbol-focus")?.id ?? null;
  const activeCandidate = hoverId ?? (hoverEdge || selectedEdge ? null : selectedId);
  const activeId = activeCandidate === focusCardId ? null : activeCandidate;
  const focusEdge = hoverEdge ?? (hoverId ? null : selectedEdge);

  const neighborIds = useMemo(() => {
    if (focusEdge) return new Set([focusEdge.from, focusEdge.to]);
    if (!activeId) return null;
    return new Set([activeId, ...(model.neighbors.get(activeId) ?? [])]);
  }, [activeId, focusEdge, model.neighbors]);

  const weights = useMemo(() => {
    const out = new Map<string, number>();
    for (const edge of model.edges) {
      if (!edge.resting || edge.symbolId) continue;
      const other = model.cardById.get(edge.from)?.lane === "subject" ? edge.to : edge.from;
      out.set(other, (out.get(other) ?? 0) + (edge.edge.weight || 1));
    }
    return out;
  }, [model]);

  const edgeState = (edge: MapEdgeLayout): "idle" | "active" | "dim" | "hidden" => {
    if (focusEdge) {
      if (edge.id === focusEdge.id) return "active";
      return edge.resting ? "dim" : "hidden";
    }
    if (activeSymbolId) {
      if (edge.symbolId === activeSymbolId) return "active";
      return edge.resting ? "dim" : "hidden";
    }
    if (activeId) {
      const touches = edge.from === activeId || edge.to === activeId;
      if (touches) return "active";
      return edge.resting ? "dim" : "hidden";
    }
    return edge.resting ? "idle" : "hidden";
  };

  const paintOrder = { hidden: 0, dim: 1, idle: 2, active: 3 } as const;
  const visibleEdges = model.edges
    .map((edge) => ({ edge, state: edgeState(edge) }))
    .filter((item) => item.state !== "hidden")
    .sort((a, b) => paintOrder[a.state] - paintOrder[b.state]);
  const labels = placeLabels(visibleEdges.filter((item) => item.state === "active").map((item) => item.edge));

  const cardState = (id: string): CardState => {
    if (id === selectedId && !selectedEdge) return "selected";
    if (!neighborIds) return "idle";
    return neighborIds.has(id) ? "neighbor" : "dim";
  };

  const moveSelection = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter"];
    if (!keys.includes(event.key)) return;
    if (event.target instanceof HTMLElement && event.target.closest(".rm-symbol-row") && event.key === "Enter") return;
    const current = selectedId ? model.cardById.get(selectedId) : undefined;
    if (event.key === "Enter") {
      if (current) {
        event.preventDefault();
        onOpen(current.id);
      }
      return;
    }
    event.preventDefault();
    const lanes = (["used_by", "subject", "uses"] as const).filter((lane) => model.laneOrder[lane].length);
    let target: string | undefined;
    if (!current) {
      target = model.laneOrder.subject[0] ?? model.laneOrder[lanes[0]]?.[0];
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      const order = model.laneOrder[current.lane];
      const index = order.indexOf(current.id) + (event.key === "ArrowDown" ? 1 : -1);
      target = order[Math.max(0, Math.min(order.length - 1, index))];
    } else {
      const laneIndex = lanes.indexOf(current.lane) + (event.key === "ArrowRight" ? 1 : -1);
      const lane = lanes[laneIndex];
      if (lane) {
        const centerY = current.y + current.h / 2;
        target = model.laneOrder[lane]
          .map((id) => model.cardById.get(id)!)
          .sort((a, b) => Math.abs(a.y + a.h / 2 - centerY) - Math.abs(b.y + b.h / 2 - centerY))[0]?.id;
      }
    }
    if (target) {
      onSelect(target);
      window.requestAnimationFrame(() => cardRefs.current.get(target!)?.focus({ preventScroll: true }));
    }
  };

  const stageHeight = stageY(model.height);
  const levelTitles = { ...LANE_TITLE[model.level], ...laneTitles };

  return (
    <div
      ref={scrollRef}
      className="rm-canvas h-full w-full"
      role="group"
      aria-label={ariaLabel}
      onKeyDown={moveSelection}
    >
      <div className="rm-lane-headers" style={{ width: model.width }}>
        {model.lanes.map((lane) => (
          <div key={lane.lane} className="rm-lane-title" style={{ left: lane.x + 4, width: lane.w }} title={LANE_HINT[lane.lane]}>
            {levelTitles[lane.lane]}
            <small>
              {lane.lane === "subject" && model.level === "architecture"
                ? plural(lane.count, "file")
                : lane.lane === "subject"
                  ? ""
                  : plural(lane.count, model.level !== "symbol" ? "file" : lane.lane === "uses" ? "symbol" : "site")}
            </small>
          </div>
        ))}
      </div>
      <div
        className="relative"
        style={{ width: model.width, height: stageHeight }}
        onClick={(event) => {
          if (event.target === event.currentTarget || (event.target as HTMLElement).classList?.contains("rm-lane-band")) onSelect(null);
        }}
      >
        {model.lanes.map((lane) => (
          <div
            key={lane.lane}
            className="rm-lane-band"
            data-lane={lane.lane}
            style={{ left: lane.x - 10, width: lane.w + 20, top: 0, bottom: 8 }}
          />
        ))}
        <svg className="rm-edges" width={model.width} height={stageHeight} aria-hidden="true">
          <g transform={`translate(0,${-LAYOUT.laneHeader})`}>
            {visibleEdges.map(({ edge, state }) => (
              <g key={edge.key}>
                <path className="rm-edge" d={edge.d} data-kind={edge.kind} data-state={state} data-internal={edge.internal} strokeWidth={state === "active" ? edge.width + 0.3 : edge.width} />
                <path className="rm-arrow" d={edge.arrow} data-kind={edge.kind} data-state={state} />
                <path
                  className="rm-hit"
                  d={edge.d}
                  fill="none"
                  stroke="transparent"
                  strokeWidth={12}
                  data-edge-id={edge.id}
                  onMouseEnter={() => setHoverEdgeId(edge.key)}
                  onMouseLeave={() => setHoverEdgeId(null)}
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelectEdge(edge.id);
                  }}
                />
              </g>
            ))}
            {labels.map((label) => <EdgeLabel key={label.key} label={label} />)}
          </g>
        </svg>
        {model.groups.map((group) => (
          <div key={group.key}>
            {group.folded ? (
              <Button
                unstyled
                className="rm-group rm-group-folded"
                style={{ left: group.x, top: stageY(group.y), width: group.w }}
                title={group.chapter ?? (group.module || "Repository root")}
                onClick={() => onToggleGroup(group.key)}
                aria-expanded={false}
                aria-label={`Show ${plural(group.total, "changed file")} in ${group.chapter ?? (group.module || "the repository root")}`}
              >
                <ChevronRight size={12} aria-hidden="true" className="shrink-0" />
                <span className="rm-group-label">{group.label}</span>
                <span className="rm-group-count">{plural(group.total, "file")}</span>
              </Button>
            ) : (
              <div className="rm-group" style={{ left: group.x, top: stageY(group.y), width: group.w }} title={group.chapter ?? (group.module || "Repository root")}>
                {group.chapter
                  ? <BookOpen size={12} aria-hidden="true" className="shrink-0 text-muted-foreground" />
                  : <Folder size={12} aria-hidden="true" className="shrink-0 text-muted-foreground" />}
                <span className="rm-group-label">{group.label}</span>
                {group.total > 1 && <span className="rm-group-count">{group.total}</span>}
              </div>
            )}
            {group.moreY !== null && (
              <Button
                unstyled
                className="rm-more"
                style={{ left: group.x, top: stageY(group.moreY), width: group.w }}
                onClick={() => onToggleGroup(group.key)}
                aria-expanded={group.expanded}
              >
                {group.expanded ? <ChevronUp size={12} aria-hidden="true" /> : <ChevronDown size={12} aria-hidden="true" />}
                {group.expanded
                  ? "Show fewer"
                  : `Show ${group.hidden} more in ${group.chapter ?? (baseName(group.module) || "root")}`}
              </Button>
            )}
          </div>
        ))}
        {model.cards.map((card) => (
          <MapCard
            key={card.id}
            card={card}
            state={cardState(card.id)}
            activeSymbolId={activeSymbolId}
            weight={weights.get(card.id) ?? 0}
            onSelect={onSelect}
            onOpen={onOpen}
            onHover={setHoveredCardId}
            onSymbolHover={setHoveredSymbolId}
            onOpenSymbol={onOpenSymbol}
            onToggleFold={onToggleGroup}
            registerRef={registerRef}
          />
        ))}
      </div>
    </div>
  );
}
