/**
 * One flow as a sequence diagram: participant cards over dashed lifelines, activation bars, and numbered steps. A sync
 * call has a filled head, an async call an open one, a return runs dashed back to its caller, and a self step loops.
 * Repeats carry a ×N badge; notes sit under their step. A dot walks the recorded steps in order.
 */
import { useMemo } from "react";

import { FLOW, type FlowLayout, type FlowMessage } from "./flowLayout";
import { GraphCard } from "./GraphCard";
import { deltaWord, evidenceWord, sameHit, stepLook, type Hit } from "./graphModel";
import type { Highlight } from "./ArchitectureView";
import { Pulse, usePulseCycle } from "./motion";
import type { XRayNode } from "./xrayGraphTypes";

const KIND_WORD = { sync: "call", async: "async call", return: "return", self: "step inside" } as const;

export interface FlowViewProps {
  layout: FlowLayout;
  nodes: ReadonlyMap<string, XRayNode>;
  selected: Hit | null;
  highlight: Highlight | null;
  reduced: boolean;
  onSelect(hit: Hit): void;
}

export function FlowView({ layout, nodes, selected, highlight, reduced, onSelect }: FlowViewProps) {
  const pulseIds = useMemo(
    () => layout.messages.filter((m) => m.step.evidence === "recorded" && (!highlight || !highlight.steps.size || highlight.steps.has(m.key))).map((m) => m.key),
    [layout.messages, highlight],
  );
  const cycle = usePulseCycle(pulseIds, !reduced);
  const pulsing = cycle.id ? layout.messages.find((m) => m.key === cycle.id) : undefined;
  const stepDim = (m: FlowMessage) => (highlight && !highlight.steps.has(m.key) ? highlight.strength : undefined);
  const hitOf = (m: FlowMessage): Hit => ({ kind: "step", flow: m.flow, id: m.id });
  const nodeDim = (id: string) => (highlight && !highlight.nodes.has(id) ? highlight.strength : undefined);
  const name = (id: string) => layout.columns.find((c) => c.node === id)?.label ?? nodes.get(id)?.label ?? id;
  const right = layout.width - FLOW.pad;

  return (
    <g className="xg-flow" data-testid="xg-flow">
      <g className="xg-columns">
        {layout.columns.map((column) => (
          <g key={column.node} data-dim={nodeDim(column.node)}>
            <rect className="xg-flow-band" x={column.band.x} y={column.band.y} width={column.band.w} height={column.band.h} rx={14} />
            <line className="xg-lifeline" x1={column.lifeline.x} x2={column.lifeline.x} y1={column.lifeline.y1} y2={column.lifeline.y2} />
          </g>
        ))}
      </g>
      <g className="xg-steps">
        {layout.messages.map((message) => (
          <Step
            key={message.key}
            message={message}
            right={right}
            selected={sameHit(selected, hitOf(message))}
            dim={stepDim(message)}
            name={`${message.index + 1}. ${message.step.label}: ${name(message.step.from)}${message.step.kind === "self" ? "" : ` to ${name(message.step.to)}`}, ${KIND_WORD[message.step.kind]}, ${deltaWord(message.step.delta).toLowerCase()}, ${evidenceWord(message.step.evidence).toLowerCase()}`}
            onSelect={() => onSelect(hitOf(message))}
          />
        ))}
      </g>
      <g className="xg-activations" aria-hidden="true">
        {layout.activations.map((bar, index) => (
          <rect key={`${bar.node}:${index}`} className="xg-activation" data-dim={nodeDim(bar.node)} x={bar.box.x} y={bar.box.y} width={bar.box.w} height={bar.box.h} rx={2} />
        ))}
      </g>
      {pulsing ? <Pulse key={`${pulsing.key}:${cycle.run}`} points={pulsing.points} tone={pulsing.step.delta} onDone={cycle.done} /> : null}
      <g className="xg-step-labels" aria-hidden="true">
        {layout.messages.map((message) => (
          <StepLabel key={message.key} message={message} selected={sameHit(selected, hitOf(message))} dim={stepDim(message)} onSelect={() => onSelect(hitOf(message))} />
        ))}
      </g>
      <g className="xg-heads">
        {layout.columns.map((column) => {
          const node = nodes.get(column.node);
          if (!node) return null;
          return (
            <GraphCard
              key={column.node}
              node={node}
              box={column.head}
              label={column.label}
              selected={selected?.kind === "node" && selected.id === column.node}
              dim={nodeDim(column.node)}
              onSelect={() => onSelect({ kind: "node", id: column.node })}
            />
          );
        })}
      </g>
    </g>
  );
}

function Step({ message, right, selected, dim, name, onSelect }: { message: FlowMessage; right: number; selected: boolean; dim: Highlight["strength"] | undefined; name: string; onSelect(): void }) {
  const look = stepLook(message.step);
  const row = message.box;
  const line = message.line.map((p, i) => `${i ? "L" : "M"}${p.x} ${p.y}`).join(" ");
  return (
    <g
      className="xg-step xg-edge"
      data-testid="xg-step"
      data-step-id={message.id}
      data-flow-id={message.flow}
      data-tone={look.tone}
      data-line={look.line}
      data-open={message.open ? "true" : undefined}
      data-ghost={look.ghost ? "true" : undefined}
      data-selected={selected ? "true" : undefined}
      data-dim={dim}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={name}
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <rect className="xg-step-row" x={FLOW.pad - 6} y={row.y - 4} width={right - FLOW.pad + 12} height={row.h + 8} rx={8} />
      <g className="xg-step-index" transform={`translate(${FLOW.pad + 9} ${message.y})`}>
        <circle r={9} />
        <text y={3.5} textAnchor="middle">{message.index + 1}</text>
      </g>
      <path className="xg-edge-line" d={line} />
      <path className="xg-edge-head" d={message.head} />
      {message.note ? (
        <text className="xg-step-note" x={message.note.x} y={message.note.y} textAnchor={message.note.anchor}>
          {message.note.lines.map((text, i) => (
            <tspan key={i} x={message.note!.x} dy={i ? FLOW.noteLine : 0}>{text}</tspan>
          ))}
        </text>
      ) : null}
    </g>
  );
}

function StepLabel({ message, selected, dim, onSelect }: { message: FlowMessage; selected: boolean; dim: Highlight["strength"] | undefined; onSelect(): void }) {
  const { box, text, full, repeat, ai } = message.label;
  const textW = box.w - (repeat ? FLOW.repeatW : 0) - (ai ? FLOW.aiTagW : 0);
  return (
    <g
      className="xg-label"
      data-tone={message.step.delta}
      data-selected={selected ? "true" : undefined}
      data-dim={dim}
      transform={`translate(${box.x} ${box.y})`}
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
    >
      <title>{full}</title>
      <rect className="xg-label-bg" width={box.w} height={box.h} rx={box.h / 2} />
      <text className="xg-label-text" x={textW / 2} y={box.h / 2 + 4} textAnchor="middle">{text}</text>
      {repeat ? (
        <g className="xg-label-repeat" transform={`translate(${textW} 2)`}>
          <rect width={FLOW.repeatW - 2} height={box.h - 4} rx={(box.h - 4) / 2} />
          <text x={(FLOW.repeatW - 2) / 2} y={(box.h - 4) / 2 + 3.5} textAnchor="middle">{`×${repeat}`}</text>
        </g>
      ) : null}
      {ai ? (
        <g className="xg-label-ai" transform={`translate(${box.w - FLOW.aiTagW} 2)`}>
          <rect width={FLOW.aiTagW - 2} height={box.h - 4} rx={(box.h - 4) / 2} />
          <text x={(FLOW.aiTagW - 2) / 2} y={(box.h - 4) / 2 + 3.5} textAnchor="middle">AI</text>
        </g>
      ) : null}
    </g>
  );
}
