/**
 * The architecture picture: lane columns with their platform, component cards, and arrows coloured by change and drawn
 * by evidence, each with its label pill. A dot travels the recorded arrows in order unless motion is reduced. Drawn
 * inside the Viewport's SVG from a precomputed layout, so this component only paints.
 */
import { useMemo } from "react";

import { ARCH, laneTitle, type ArchArrow, type ArchLayout } from "./archLayout";
import { GraphCard } from "./GraphCard";
import { edgeLook, evidenceWord, deltaWord, type Hit, type TourFocus } from "./graphModel";
import { roundedPath } from "./geometry";
import { Pulse, usePulseCycle } from "./motion";
import type { XRayNode } from "./xrayGraphTypes";

export type Dim = "true" | "soft" | undefined;

/** What a highlight leaves bright: the walkthrough's focus (strong), or a selection and its neighbours (soft). */
export interface Highlight {
  nodes: Set<string>;
  edges: Set<string>;
  /** By `stepKey(flow, step)`. */
  steps: Set<string>;
  strength: "true" | "soft";
}

export function highlightFromFocus(lit: TourFocus["lit"]): Highlight | null {
  return lit ? { ...lit, strength: "true" } : null;
}

/** A selection keeps its neighbourhood bright: a card with its arrows and the cards at their other ends; an arrow with its two ends. */
export function highlightFromSelection(layout: ArchLayout, hit: Hit | null): Highlight | null {
  if (!hit || (hit.kind !== "node" && hit.kind !== "edge")) return null;
  const nodes = new Set<string>();
  const edges = new Set<string>();
  for (const arrow of layout.arrows) {
    const { from, to } = arrow.edge;
    if ((hit.kind === "node" && (from === hit.id || to === hit.id)) || (hit.kind === "edge" && arrow.id === hit.id)) {
      edges.add(arrow.id);
      nodes.add(from).add(to);
    }
  }
  if (hit.kind === "node") nodes.add(hit.id);
  return { nodes, edges, steps: new Set(), strength: "soft" };
}

function edgeDim(highlight: Highlight | null, arrow: ArchArrow): Dim {
  if (!highlight) return undefined;
  const lit = highlight.edges.size ? highlight.edges.has(arrow.id) : highlight.nodes.has(arrow.edge.from) && highlight.nodes.has(arrow.edge.to);
  return lit ? undefined : highlight.strength;
}

const DRAW_ORDER = { unchanged: 0, removed: 1, modified: 2, added: 3 } as const;

export interface ArchitectureViewProps {
  layout: ArchLayout;
  /** All nodes by id, for the arrow names a screen reader hears. */
  nodes: ReadonlyMap<string, XRayNode>;
  selected: Hit | null;
  highlight: Highlight | null;
  /** Recorded arrows in the order the pulse walks them. */
  pulse: readonly string[];
  reduced: boolean;
  onSelect(hit: Hit): void;
}

export function ArchitectureView({ layout, nodes, selected, highlight, pulse, reduced, onSelect }: ArchitectureViewProps) {
  const arrows = useMemo(
    () => [...layout.arrows].sort((a, b) => Number(a.hero) - Number(b.hero) || DRAW_ORDER[a.edge.delta] - DRAW_ORDER[b.edge.delta]),
    [layout.arrows],
  );
  const byId = useMemo(() => new Map(layout.arrows.map((arrow) => [arrow.id, arrow])), [layout.arrows]);
  const pulseIds = useMemo(() => pulse.filter((id) => byId.has(id) && !edgeDim(highlight, byId.get(id)!)), [pulse, byId, highlight]);
  const cycle = usePulseCycle(pulseIds, !reduced);
  const pulsing = cycle.id ? byId.get(cycle.id) : undefined;
  const selectedEdge = selected?.kind === "edge" ? selected.id : null;
  const selectedNode = selected?.kind === "node" ? selected.id : null;
  const name = (id: string) => nodes.get(id)?.label ?? id;

  return (
    <g className="xg-arch" data-testid="xg-architecture">
      <g className="xg-lanes">
        {layout.lanes.map((lane) => {
          const { title, platform } = laneTitle(lane.lane);
          const count = layout.cards.filter((card) => card.lane === lane.id).length;
          return (
            <g key={lane.id} className="xg-lane" data-lane-id={lane.id}>
              <rect className="xg-lane-bg" x={lane.box.x} y={lane.box.y} width={lane.box.w} height={lane.box.h} rx={14} />
              <text className="xg-lane-title" x={lane.box.x + 16} y={lane.box.y + 24}>
                {title}
                {platform ? <tspan className="xg-lane-platform">{platform}</tspan> : null}
              </text>
              <text className="xg-lane-count" x={lane.box.x + lane.box.w - 16} y={lane.box.y + 24} textAnchor="end">{count}</text>
            </g>
          );
        })}
      </g>
      <g className="xg-edges">
        {arrows.map((arrow) => {
          const look = edgeLook(arrow.edge);
          const label = arrow.edge.label ? `${arrow.edge.label}: ` : "";
          return (
            <g
              key={arrow.id}
              className="xg-edge"
              data-testid="xg-edge"
              data-edge-id={arrow.id}
              data-tone={look.tone}
              data-line={look.line}
              data-ghost={look.ghost ? "true" : undefined}
              data-hero={look.hero ? "true" : undefined}
              data-selected={selectedEdge === arrow.id ? "true" : undefined}
              data-dim={edgeDim(highlight, arrow)}
              role="button"
              tabIndex={0}
              aria-pressed={selectedEdge === arrow.id}
              aria-label={`${label}${name(arrow.edge.from)} to ${name(arrow.edge.to)}, ${deltaWord(arrow.edge.delta).toLowerCase()}, ${evidenceWord(arrow.edge.evidence).toLowerCase()}`}
              onClick={(event) => {
                event.stopPropagation();
                onSelect({ kind: "edge", id: arrow.id });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect({ kind: "edge", id: arrow.id });
                }
              }}
            >
              <path className="xg-edge-halo" d={roundedPath(arrow.points, ARCH.corner)} />
              <path className="xg-edge-line" d={arrow.d} />
              <path className="xg-edge-head" d={arrow.head} />
              <path className="xg-edge-hit" d={roundedPath(arrow.points, ARCH.corner)} />
            </g>
          );
        })}
      </g>
      {/* The pulse rides above the arrows and under the cards and labels, so it never covers text. */}
      {pulsing ? <Pulse key={`${pulsing.id}:${cycle.run}`} points={pulsing.points} tone={pulsing.edge.delta} onDone={cycle.done} /> : null}
      <g className="xg-cards">
        {layout.cards.map((card) => (
          <GraphCard
            key={card.id}
            node={card.node}
            box={card.box}
            selected={selectedNode === card.id}
            dim={highlight && !highlight.nodes.has(card.id) ? highlight.strength : undefined}
            onSelect={() => onSelect({ kind: "node", id: card.id })}
          />
        ))}
      </g>
      <g className="xg-labels" aria-hidden="true">
        {layout.arrows.map((arrow) => (arrow.label ? (
          <ArrowLabel
            key={arrow.id}
            arrow={arrow}
            selected={selectedEdge === arrow.id}
            dim={edgeDim(highlight, arrow)}
            onSelect={() => onSelect({ kind: "edge", id: arrow.id })}
          />
        ) : null))}
      </g>
    </g>
  );
}

function ArrowLabel({ arrow, selected, dim, onSelect }: { arrow: ArchArrow; selected: boolean; dim: Dim; onSelect(): void }) {
  const label = arrow.label!;
  const { box } = label;
  const textW = label.text ? box.w - (label.ai ? ARCH.aiTagW + 2 : 0) : 0;
  return (
    <g
      className="xg-label"
      data-tone={arrow.edge.delta}
      data-selected={selected ? "true" : undefined}
      data-dim={dim}
      transform={`translate(${box.x} ${box.y})`}
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
    >
      <title>{label.full}</title>
      {label.text ? (
        <>
          <rect className="xg-label-bg" width={textW} height={box.h} rx={box.h / 2} />
          <text className="xg-label-text" x={textW / 2} y={box.h / 2 + 4} textAnchor="middle">{label.text}</text>
        </>
      ) : null}
      {label.ai ? (
        <g className="xg-label-ai" transform={`translate(${box.w - ARCH.aiTagW} 1)`}>
          <rect width={ARCH.aiTagW} height={box.h - 2} rx={(box.h - 2) / 2} />
          <text x={ARCH.aiTagW / 2} y={(box.h - 2) / 2 + 3.5} textAnchor="middle">AI</text>
        </g>
      ) : null}
    </g>
  );
}
