/**
 * The narrow-screen picture (below 720px): the same view as a stacked list. Lanes become sections, cards become rows
 * with their outgoing arrows under them, and a flow becomes its numbered steps. Selecting anything opens the same
 * inspector as the wide picture.
 */
import { ArrowRight, CornerDownLeft, Repeat } from "lucide-react";

import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import type { ArchLayout } from "./archLayout";
import type { Highlight } from "./ArchitectureView";
import type { FlowLayout } from "./flowLayout";
import { kindIcon } from "./GraphCard";
import { deltaGlyph, sameHit, type Hit } from "./graphModel";
import { DeltaBadge, EvidenceBadge } from "./Inspector";
import type { XRayNode } from "./xrayGraphTypes";

export interface StackedGraphProps {
  arch: ArchLayout | null;
  flow: FlowLayout | null;
  nodes: ReadonlyMap<string, XRayNode>;
  selected: Hit | null;
  highlight: Highlight | null;
  onSelect(hit: Hit): void;
}

export function StackedGraph({ arch, flow, nodes, selected, highlight, onSelect }: StackedGraphProps) {
  const isSelected = (hit: Hit) => (sameHit(selected, hit) ? "true" : undefined);
  const name = (id: string) => nodes.get(id)?.label ?? id;
  if (flow) {
    const dim = (key: string) => (highlight && highlight.steps.size && !highlight.steps.has(key) ? highlight.strength : undefined);
    return (
      <ol className="xg-stack m-0 list-none" data-testid="xg-stack" aria-label="Steps">
        {flow.messages.map((message) => {
          const step = message.step;
          const Icon = step.kind === "return" ? CornerDownLeft : step.kind === "self" ? Repeat : ArrowRight;
          return (
            <li key={message.key} data-dim={dim(message.key)}>
              <Button unstyled className="xg-stack-step" data-selected={isSelected({ kind: "step", flow: message.flow, id: message.id })} data-testid="xg-stack-step" onClick={() => onSelect({ kind: "step", flow: message.flow, id: message.id })}>
                <span className="xg-stack-step-index">{message.index + 1}</span>
                <span className="flex min-w-0 flex-1 flex-col gap-1">
                  <span className="flex min-w-0 items-center gap-1.5 text-caption text-muted-foreground">
                    <span className="truncate">{name(step.from)}</span>
                    {step.kind === "self" ? null : (
                      <>
                        <Icon size={14} aria-hidden="true" className="shrink-0" />
                        <span className="truncate">{name(step.to)}</span>
                      </>
                    )}
                  </span>
                  <span className="text-label font-semibold">
                    {step.label}
                    {step.repeat && step.repeat >= 2 ? <span className="xg-mono ml-1.5 text-caption text-muted-foreground">{`×${step.repeat}`}</span> : null}
                  </span>
                  <span className="flex flex-wrap gap-1">
                    <DeltaBadge delta={step.delta} />
                    <EvidenceBadge evidence={step.evidence} />
                    {step.payload?.request || step.payload?.response ? <Badge tone="secondary">Data</Badge> : null}
                  </span>
                  {step.note ? <span className="text-caption text-subtle">{step.note}</span> : null}
                </span>
              </Button>
            </li>
          );
        })}
      </ol>
    );
  }
  if (!arch) return null;
  const nodeDim = (id: string) => (highlight && !highlight.nodes.has(id) ? highlight.strength : undefined);
  return (
    <div className="xg-stack" data-testid="xg-stack">
      {arch.lanes.map((lane) => {
        const cards = arch.cards.filter((card) => card.lane === lane.id).sort((a, b) => a.row - b.row);
        return (
          <section key={lane.id} className="xg-stack-lane" aria-label={lane.lane.label}>
            <h4 className="xg-stack-lane-head m-0">
              {lane.lane.label}
              {lane.lane.platform ? <span>{lane.lane.platform}</span> : null}
            </h4>
            {cards.length === 0 ? <p className="m-0 text-caption text-muted-foreground">Nothing here on this side.</p> : null}
            {cards.map((card) => {
              const node = card.node;
              const Icon = kindIcon(node);
              const out = arch.arrows.filter((arrow) => arrow.edge.from === node.id);
              return (
                <div key={card.id} className="flex flex-col gap-1" data-dim={nodeDim(card.id)}>
                  <Button unstyled className="xg-stack-card" data-tone={node.delta} data-selected={isSelected({ kind: "node", id: card.id })} data-testid="xg-stack-card" onClick={() => onSelect({ kind: "node", id: card.id })}>
                    <span className="xg-stack-title">
                      <Icon size={14} aria-hidden="true" className="shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate">{node.label}</span>
                      {node.delta !== "unchanged" ? <DeltaBadge delta={node.delta} /> : null}
                    </span>
                    {node.subtitle ? <span className="xg-stack-sub truncate">{node.subtitle}</span> : null}
                  </Button>
                  {out.length ? (
                    <div className="xg-stack-edges">
                      {out.map((arrow) => (
                        <Button key={arrow.id} unstyled className="xg-stack-edge" data-selected={isSelected({ kind: "edge", id: arrow.id })} data-testid="xg-stack-edge" onClick={() => onSelect({ kind: "edge", id: arrow.id })}>
                          <span className="xg-tone-dot" data-tone={arrow.edge.delta} aria-hidden="true" />
                          <span className="xg-mono w-3 shrink-0 text-micro text-muted-foreground" aria-hidden="true">{arrow.edge.delta === "unchanged" ? "" : deltaGlyph(arrow.edge.delta)}</span>
                          <ArrowRight size={14} aria-hidden="true" className="shrink-0" />
                          <span className="min-w-0 truncate font-medium text-foreground">{name(arrow.edge.to)}</span>
                          {arrow.edge.label ? <span className="min-w-0 truncate text-micro text-muted-foreground">{arrow.edge.label}</span> : null}
                          {arrow.edge.evidence === "inferred" ? <Badge tone="ai">AI</Badge> : null}
                        </Button>
                      ))}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}
