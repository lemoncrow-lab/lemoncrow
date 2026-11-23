// The X-Ray picture: lanes, cards and arrows (canvas), or the same lanes stacked as lists on a narrow screen.
// Nothing here decides what is observed or changed; it draws what `xrayLens` says, in shape as well as colour.
import { useId, useMemo, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Activity } from "lucide-react";

import { Button } from "../../components/ui/button";
import { edgePaths, layoutLens, type LensGeometry } from "./xrayLayout";
import { LANE_LABEL, MAIN_LANES, edgeStyle, toneOf, type LensCard, type LensDiagram, type LensEdge, type LensView } from "./xrayLens";
import type { XRaySide, XRayTone } from "./xrayModel";

export type LensSelection = { kind: "card" | "edge"; id: string } | null;

export interface LensDrawProps {
  diagram: LensDiagram;
  view: LensView;
  side: XRaySide;
  selection: LensSelection;
  /** The walkthrough's card and arrow, when it is on. */
  active: { cardId: string; edgeKey: string | null } | null;
  /** The arrow the moving dot runs along now; null when nothing moves (reduced motion, or nothing observed in order). */
  pulse: { key: string; tick: number } | null;
  behaviorCount?(path: string): number;
  onCard(card: LensCard): void;
  onEdge(edge: LensEdge): void;
}

const TONES: XRayTone[] = ["added", "changed", "removed", "unchanged", "unknown"];

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

export function cardGlyph(card: LensCard): string {
  if (card.kind === "fold") return "+";
  switch (card.change) {
    case "added": return "+";
    case "removed": return "−";
    case "changed": return "~";
    case "unchanged": return "=";
    default: return "?";
  }
}

export function cardChangeLabel(card: LensCard, side: XRaySide): string {
  if (card.kind === "fold") {
    const changed = card.folded?.changed ?? 0;
    const count = card.folded?.count ?? 0;
    return changed ? `${changed} changed · ${count - changed} unchanged` : `${plural(count, "unchanged neighbour")}`;
  }
  switch (card.change) {
    case "added": return "new file";
    case "removed": return side === "before" ? "removed by this change" : "removed";
    case "changed": return "changed";
    case "unchanged": return "unchanged";
    case "context_only": return "not compared";
    default: return "change unknown";
  }
}

function cardTone(card: LensCard): XRayTone | "fold" {
  return card.kind === "fold" ? "fold" : card.context && card.change !== "removed" && card.change !== "added" ? (card.change === "unchanged" ? "unchanged" : "unknown") : toneOf(card.change);
}

function cardLabel(card: LensCard, side: XRaySide, mode: LensDiagram["mode"], behavior: number): string {
  if (card.kind === "fold") return `${card.title} files folded in ${LANE_LABEL[card.lane]}: ${cardChangeLabel(card, side)}. Show the list.`;
  const action = mode === "change" ? "Open its path" : "Show details";
  return [card.title, cardChangeLabel(card, side), card.roleLabel, behavior ? plural(behavior, "behavior change") : "", action].filter(Boolean).join(", ");
}

/** The few facts a card carries under its name: change in words, a boundary, its checks, where a path stops. */
function CardFace({ card, side, mode, behavior }: { card: LensCard; side: XRaySide; mode: LensDiagram["mode"]; behavior: number }) {
  const step = card.steps?.[side] ?? card.steps?.after ?? card.steps?.before;
  const boundary = card.boundaries[0];
  const checks = card.checks;
  return (
    <>
      <span className="xr-card-top">
        <span className="xr-glyph" data-tone={cardTone(card)} aria-hidden="true">{cardGlyph(card)}</span>
        <span className="xr-card-title">{card.title}</span>
        {behavior > 0 && (
          <span className="xr-card-behavior" aria-hidden="true">
            <Activity size={14} />
            {behavior}
          </span>
        )}
      </span>
      <span className="xr-card-folder">{card.kind === "fold" ? cardChangeLabel(card, side) : card.folder || " "}</span>
      <span className="xr-card-meta">
        {card.kind === "fold" ? (
          <span>folded past the bound</span>
        ) : (
          <>
            <span data-change={card.change}>{mode === "focus" && step ? step.roleLabel : cardChangeLabel(card, side)}</span>
            {boundary && <span className="xr-card-chip" data-boundary={boundary.kind}>{boundary.kind === "storage" ? "storage" : boundary.kind === "async" ? "runs later" : boundary.kind === "remote" ? "other service" : boundary.kind}</span>}
            {checks && checks.fail > 0 && <span className="xr-card-chip" data-check="fail">{`✕ ${checks.fail} failed`}</span>}
            {checks && !checks.fail && checks.pass > 0 && <span className="xr-card-chip" data-check="pass">{`✓ ${checks.pass} passed`}</span>}
            {mode === "focus" && step?.exit === "depth" && <span className="xr-card-chip">past depth</span>}
          </>
        )}
      </span>
    </>
  );
}

function activate(handler: () => void) {
  return (event: ReactKeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handler();
    }
  };
}

/** The canvas: lane bands, cards placed by `layoutLens`, and an SVG layer of arrows under them. */
export function LensCanvas(props: LensDrawProps & { width: number }) {
  const { diagram, view, side, selection, active, pulse, behaviorCount, onCard, onEdge, width } = props;
  const geometry: LensGeometry = useMemo(() => layoutLens(diagram, width), [diagram, width]);
  const paths = useMemo(() => edgePaths(view.edges, geometry, view.cards), [view.edges, geometry, view.cards]);
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const shown = new Set(view.cards.map((card) => card.id));
  const pulsePath = pulse ? paths.get(pulse.key) : undefined;
  const reachesActive = (edge: LensEdge) => active && (edge.key === active.edgeKey);

  return (
    <div
      className="xr-canvas"
      data-testid="xray-canvas"
      data-walk={active ? "on" : "off"}
      style={{ width: geometry.width, height: geometry.height }}
    >
      {geometry.lanes.map((lane) => (
        <div key={lane.id} className="xr-lane" data-testid="xray-lane" data-lane={lane.id} style={{ left: lane.x, top: lane.y, width: lane.w, height: lane.h }}>
          <span className="xr-lane-label">{lane.label}</span>
          <span className="xr-lane-count">{lane.count}</span>
        </div>
      ))}
      {geometry.band && (
        <div className="xr-lane xr-band" data-testid="xray-lane" data-lane="tests" style={{ left: geometry.band.x, top: geometry.band.y, width: geometry.band.w, height: geometry.band.h }}>
          <span className="xr-lane-label">{geometry.band.label}</span>
          <span className="xr-lane-count">{geometry.band.count}</span>
        </div>
      )}
      <svg className="xr-edges" width={geometry.width} height={geometry.height} aria-label="Arrows between files" role="group">
        <defs>
          {TONES.map((tone) => (
            <marker key={tone} id={`${uid}-head-${tone}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="xr-head" data-tone={tone} />
            </marker>
          ))}
        </defs>
        {view.edges.map((edge) => {
          const path = paths.get(edge.key);
          if (!path) return null;
          const style = edgeStyle(edge);
          const selected = selection?.kind === "edge" && selection.id === edge.pair;
          const isActive = Boolean(reachesActive(edge));
          const badge = [style.glyph, edge.observed && edge.sequence !== null ? `#${edge.sequence}` : ""].filter(Boolean).join(" ");
          return (
            <g
              key={edge.key}
              className="xr-edge"
              role="button"
              tabIndex={0}
              aria-label={style.label}
              aria-pressed={selected}
              data-testid="xray-edge"
              data-edge-key={edge.key}
              data-line={style.line}
              data-tone={style.tone}
              data-observed={edge.observed ? "true" : "false"}
              data-sequence={edge.sequence ?? ""}
              data-fold={edge.fold ? "true" : "false"}
              data-selected={selected ? "true" : "false"}
              data-current={isActive ? "true" : "false"}
              onClick={() => onEdge(edge)}
              onKeyDown={activate(() => onEdge(edge))}
            >
              <title>{style.label}</title>
              <path className="xr-edge-hit" d={path.d} />
              <path className="xr-edge-line" d={path.d} markerEnd={`url(#${uid}-head-${style.tone})`} />
              {badge && (
                <g className="xr-edge-badge" transform={`translate(${path.mid.x},${path.mid.y})`}>
                  <rect x={-(6 + badge.length * 3.4)} y={-9} width={12 + badge.length * 6.8} height={18} rx={9} />
                  <text textAnchor="middle" dominantBaseline="central">{badge}</text>
                </g>
              )}
            </g>
          );
        })}
        {pulse && pulsePath && (
          <circle key={`${pulse.key}:${pulse.tick}`} className="xr-dot" r={4.5} data-testid="xray-dot">
            <animateMotion dur="1.2s" fill="freeze" path={pulsePath.d} />
          </circle>
        )}
      </svg>
      {diagram.cards.map((card) => {
        const box = geometry.boxes.get(card.id);
        // Before shows no added file and After no removed one; the others keep their places.
        if (!box || !shown.has(card.id)) return null;
        const behavior = card.kind === "node" && behaviorCount ? behaviorCount(card.path) : 0;
        return (
          <Button
            key={card.id}
            unstyled
            className="xr-card"
            data-testid="xray-card"
            data-card-id={card.id}
            data-kind={card.kind}
            data-tone={cardTone(card)}
            data-context={card.context ? "true" : "false"}
            data-root={diagram.mode === "focus" && card.nodeId === diagram.rootId ? "true" : "false"}
            data-selected={selection?.kind === "card" && selection.id === card.id ? "true" : "false"}
            data-current={active?.cardId === card.id ? "true" : "false"}
            aria-label={cardLabel(card, side, diagram.mode, behavior)}
            tooltip={card.kind === "node" ? card.path : `${card.folded?.count ?? 0} files folded past the lane bound`}
            style={{ left: box.x, top: box.y, width: box.w, height: box.h }}
            onClick={() => onCard(card)}
          >
            <CardFace card={card} side={side} mode={diagram.mode} behavior={behavior} />
          </Button>
        );
      })}
    </div>
  );
}

/** A short sample of an arrow's line, for lists and the legend. */
export function LineSample({ line, tone }: { line: "solid" | "dashed"; tone: string }) {
  return (
    <svg className="xr-line-sample" width="22" height="8" viewBox="0 0 22 8" aria-hidden="true" data-line={line} data-tone={tone}>
      <path d="M1,4 H17" />
      <path d="M16,1 L21,4 L16,7 z" className="xr-line-sample-head" />
    </svg>
  );
}

/** Narrow screens: the same lanes, top to bottom; each card lists the arrows that leave it. */
export function LensStack(props: LensDrawProps) {
  const { diagram, view, side, selection, active, behaviorCount, onCard, onEdge } = props;
  const byId = new Map(view.cards.map((card) => [card.id, card]));
  const groups = diagram.mode === "change"
    ? [...MAIN_LANES, "tests" as const].map((lane) => ({ id: lane as string, label: LANE_LABEL[lane], cards: view.cards.filter((card) => card.lane === lane && card.place !== "other").sort((a, b) => a.column - b.column || a.row - b.row) }))
    : diagram.columns.map((column, index) => ({ id: column.id, label: column.label, cards: view.cards.filter((card) => card.column === index).sort((a, b) => a.row - b.row) }));
  return (
    <div className="xr-stack" data-testid="xray-canvas" data-walk={active ? "on" : "off"}>
      {groups.filter((group) => group.cards.length).map((group) => (
        <section key={group.id} className="xr-stack-lane" data-testid="xray-lane" data-lane={group.id} aria-label={group.label}>
          <h4 className="xr-lane-label">{`${group.label} · ${group.cards.length}`}</h4>
          <ul className="xr-stack-list">
            {group.cards.map((card) => {
              const out = view.edges.filter((edge) => edge.from === card.id);
              const behavior = card.kind === "node" && behaviorCount ? behaviorCount(card.path) : 0;
              return (
                <li key={card.id} className="xr-stack-item">
                  <Button
                    unstyled
                    className="xr-card xr-card-stacked"
                    data-testid="xray-card"
                    data-card-id={card.id}
                    data-kind={card.kind}
                    data-tone={cardTone(card)}
                    data-context={card.context ? "true" : "false"}
                    data-root={diagram.mode === "focus" && card.nodeId === diagram.rootId ? "true" : "false"}
                    data-selected={selection?.kind === "card" && selection.id === card.id ? "true" : "false"}
                    data-current={active?.cardId === card.id ? "true" : "false"}
                    aria-label={cardLabel(card, side, diagram.mode, behavior)}
                    onClick={() => onCard(card)}
                  >
                    <CardFace card={card} side={side} mode={diagram.mode} behavior={behavior} />
                  </Button>
                  {out.length > 0 && (
                    <ul className="xr-stack-edges" aria-label={`Arrows from ${card.title}`}>
                      {out.map((edge) => {
                        const style = edgeStyle(edge);
                        return (
                          <li key={edge.key}>
                            <Button
                              unstyled
                              className="xr-stack-edge"
                              data-testid="xray-edge"
                              data-edge-key={edge.key}
                              data-line={style.line}
                              data-tone={style.tone}
                              data-observed={edge.observed ? "true" : "false"}
                              data-sequence={edge.sequence ?? ""}
                              data-selected={selection?.kind === "edge" && selection.id === edge.pair ? "true" : "false"}
                              data-current={active?.edgeKey === edge.key ? "true" : "false"}
                              aria-label={style.label}
                              onClick={() => onEdge(edge)}
                            >
                              <LineSample line={style.line} tone={style.tone} />
                              {style.glyph && <span className="xr-glyph" data-tone={style.tone} aria-hidden="true">{style.glyph}</span>}
                              <span className="xr-stack-edge-name">{byId.get(edge.to)?.title ?? edge.to}</span>
                              <span className="xr-stack-edge-basis">{edge.observed ? (edge.sequence !== null ? `observed #${edge.sequence}` : "observed") : "static"}</span>
                            </Button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
