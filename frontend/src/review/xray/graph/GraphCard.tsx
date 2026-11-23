/**
 * One component card, drawn in SVG: a kind icon, the label, a subtitle and the badges riding the top border. The
 * delta shows in colour and in shape: a + / ~ / − pill, a solid or dashed outline, and a struck-through label when
 * the component is removed.
 */
import {
  AppWindow,
  Box as BoxIcon,
  Braces,
  Clock,
  Database,
  FileJson,
  FileSpreadsheet,
  FlaskConical,
  Globe,
  Layers,
  Package,
  Route,
  Settings2,
  SquareFunction,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import { fitText, textWidth } from "./textWidth";
import type { Box } from "./geometry";
import type { XRayNode } from "./xrayGraphTypes";

const KIND_ICONS: Record<XRayNode["kind"], LucideIcon> = {
  ui: AppWindow,
  route: Route,
  handler: Workflow,
  function: SquareFunction,
  class: Braces,
  module: Package,
  datastore: Database,
  queue: Layers,
  external: Globe,
  job: Clock,
  config: Settings2,
  test: FlaskConical,
};

/** The icon for a node: by kind, except data files, which show what kind of file they are. */
export function kindIcon(node: Pick<XRayNode, "kind" | "label">): LucideIcon {
  if (node.kind === "datastore" || node.kind === "config") {
    if (/\.(csv|tsv|xlsx?)$/i.test(node.label)) return FileSpreadsheet;
    if (/\.(json|ya?ml|toml)$/i.test(node.label)) return FileJson;
  }
  return KIND_ICONS[node.kind] ?? BoxIcon;
}

export const KIND_WORDS: Record<XRayNode["kind"], string> = {
  ui: "UI component",
  route: "Route",
  handler: "Handler",
  function: "Function",
  class: "Class",
  module: "Module",
  datastore: "Data store",
  queue: "Queue",
  external: "External service",
  job: "Job",
  config: "Config",
  test: "Test",
};

const DELTA_PILL: Record<XRayNode["delta"], string> = { added: "+ NEW", modified: "~ CHANGED", removed: "− REMOVED", unchanged: "" };
const PILL_SIZE = 9.5;
const PILL_TRACKING = 0.6;
const BADGE_SIZE = 10;

export interface GraphCardProps {
  node: XRayNode;
  box: Box;
  /** Overrides the node's label (a flow participant's own label). */
  label?: string;
  selected?: boolean;
  /** Faded: outside the walkthrough's focus ("true") or outside a selection's neighbourhood ("soft"). */
  dim?: "true" | "soft";
  onSelect?(): void;
}

export function GraphCard({ node, box, label, selected, dim, onSelect }: GraphCardProps) {
  const Icon = kindIcon(node);
  const h = box.h;
  const textX = 50;
  const room = box.w - textX - 12;
  const title = fitText(label ?? node.label, room, 13, "sans-semibold");
  const subtitle = node.subtitle ? fitText(node.subtitle, room, 10.5, "mono") : "";
  const titleY = subtitle ? Math.round(h / 2 - 3) : Math.round(h / 2 + 4);
  const pills: { text: string; kind: "delta" | "badge" | "ai"; w: number }[] = [];
  if (DELTA_PILL[node.delta]) pills.push({ text: DELTA_PILL[node.delta], kind: "delta", w: textWidth(DELTA_PILL[node.delta], PILL_SIZE, "sans-semibold", PILL_TRACKING) + 14 });
  for (const badge of node.badges.slice(0, 3)) {
    if (badge.toLowerCase() === "new" && node.delta === "added") continue;
    const text = fitText(badge, 72, BADGE_SIZE, "mono");
    pills.push({ text, kind: "badge", w: textWidth(text, BADGE_SIZE, "mono") + 12 });
  }
  if (node.source === "inferred") pills.push({ text: "AI", kind: "ai", w: 24 });
  let cursor = box.w - 12;
  const placed = pills.map((pill) => {
    cursor -= pill.w;
    const x = cursor;
    cursor -= 4;
    return { ...pill, x };
  });
  const name = `${label ?? node.label}, ${KIND_WORDS[node.kind]}, ${node.delta === "unchanged" ? "unchanged" : node.delta === "modified" ? "changed" : node.delta}`;
  return (
    <g
      className="xg-card"
      data-testid="xg-card"
      data-node-id={node.id}
      data-tone={node.delta}
      data-selected={selected ? "true" : undefined}
      data-dim={dim}
      transform={`translate(${box.x} ${box.y})`}
      role="button"
      tabIndex={0}
      aria-label={name}
      aria-pressed={selected ? true : false}
      onClick={(event) => {
        event.stopPropagation();
        onSelect?.();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect?.();
        }
      }}
    >
      <title>{node.subtitle ? `${node.label} · ${node.subtitle}` : node.label}</title>
      {selected ? <rect className="xg-card-ring" x={-4} y={-4} width={box.w + 8} height={h + 8} rx={13} /> : null}
      <rect className="xg-card-bg" width={box.w} height={h} rx={10} />
      <rect className="xg-card-icon" x={12} y={Math.round(h / 2 - 14)} width={28} height={28} rx={7} />
      <Icon x={19} y={Math.round(h / 2 - 7)} size={14} className="xg-card-glyph" aria-hidden="true" />
      <text className="xg-card-title" x={textX} y={titleY}>{title}</text>
      {subtitle ? <text className="xg-card-sub" x={textX} y={Math.round(h / 2 + 13)}>{subtitle}</text> : null}
      {placed.map((pill) => (
        <g key={`${pill.kind}:${pill.text}`} className="xg-pill" data-kind={pill.kind} transform={`translate(${pill.x} -9)`}>
          <rect width={pill.w} height={18} rx={9} />
          <text x={pill.w / 2} y={12.5} textAnchor="middle">{pill.text}</text>
        </g>
      ))}
    </g>
  );
}
