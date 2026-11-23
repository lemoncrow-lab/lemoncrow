/** P3-3 Review Map. Source: stored, revision-pinned Review graph; no AI-created edges. */
import { assignLanes, nodeName } from "../reviewMapModel";
import type { ReviewDataPathReport, ReviewMapEdge, ReviewMapNode, ReviewMapPayload, ReviewTarget } from "../types";
import type { MapLens, MapView } from "../reviewRoutes";

export type MapState = "added" | "removed" | "changed" | "unchanged" | "unverified";
export type MapStep = 0 | 1 | 2;
/** The diagram's three lanes, in reading order: what uses the change, the changed code, what it uses. */
export type MapLane = "used_by" | "subject" | "uses";
export const MAP_LANES: readonly MapLane[] = ["used_by", "subject", "uses"];
export const MAP_LANE_TITLES: Record<MapLane, string> = { used_by: "USED BY", subject: "CHANGED CODE", uses: "USES" };
export interface MapNodeLayout {
  id: string;
  node: ReviewMapNode;
  state: MapState;
  lane: MapLane;
  x: number;
  y: number;
  width: number;
  height: number;
  depth: number;
  visible: boolean;
  active: boolean;
  target?: ReviewTarget;
}
/** How a connection was established. Only "checked" is a Checked in code fact. */
export type EdgeBasis = "checked" | "name" | "index";
export interface MapEdgeLayout {
  id: string;
  edge: ReviewMapEdge;
  basis: EdgeBasis;
  state: MapState;
  from: MapNodeLayout;
  to: MapNodeLayout;
  visible: boolean;
  active: boolean;
}
export interface MapFoldLayout { lane: string; x: number; y: number; width: number; height: number; count: number }
/** A lane drawn as a band across the phone diagram; its title sits at `y`. */
export interface MapBandLayout { lane: MapLane; y: number }
export interface MapFlowLayout {
  nodes: MapNodeLayout[];
  folds: MapFoldLayout[];
  edges: MapEdgeLayout[];
  width: number;
  height: number;
  /** Set only on the compact (phone) layout, whose lanes stack as bands instead of standing side by side. */
  bands?: MapBandLayout[];
  folded: number;
  summary: { changed: number; added: number; removed: number; newConnections: number; notVerified: number };
  comparable: boolean;
  truncated: boolean;
}

export function statusOf(node: ReviewMapNode | ReviewMapEdge): MapState {
  switch (node.delta_status) {
    case "added": return "added";
    case "removed": return "removed";
    case "changed": return "changed";
    case "unchanged": case "context_only": return "unchanged";
    default: break;
  }
  // The plain change map carries no comparison, but its own snapshot nodes say which files this change touched.
  if (node.delta_status === undefined && node.exactness === "snapshot" && "status" in node) {
    if (node.status === "new") return "added";
    if (node.status === "changed") return "changed";
  }
  return "unverified";
}

export function mapStateSymbol(state: MapState): string {
  return state === "added" ? "+" : state === "removed" ? "−" : state === "changed" ? "~" : state === "unverified" ? "?" : "·";
}
export function mapStateText(state: MapState): string {
  return state === "added" ? "Added" : state === "removed" ? "Removed" : state === "changed" ? "Changed" : state === "unverified" ? "Not verified" : "Unchanged";
}
export function survivesView(state: MapState, view: MapView): boolean {
  return view === "changes" || (view === "before" ? state !== "added" : state !== "removed");
}
export function graphComparable(payload: ReviewMapPayload): boolean {
  return payload.projection === "architecture_delta"
    && payload.architecture_delta?.status === "ready"
    && !payload.graph.truncated;
}

/**
 * The edge kinds the server really produces for the map: "references" (architecture_delta.py, matched by a bare
 * symbol name), "imports" and "calls" (the current code index) and "impacts" (review_map.py). None of them says
 * how data moves, so no graph edge belongs to the Data path lens: it shows the stored data path report instead.
 */
export const DATA_PATH_KINDS: readonly string[] = [];
export const DATA_PATH_UNAVAILABLE = "Data path is not available here: LemonCrow records which files use which, not how data moves between them.";
export const DEPENDENCY_KINDS: readonly string[] = ["references", "imports", "calls", "impacts"];

/** Only relationships returned by the server, never inferred from a matching name. */
export function lensEdges(payload: ReviewMapPayload, lens: MapLens): ReviewMapEdge[] {
  const kinds = lens === "data-path" ? DATA_PATH_KINDS : DEPENDENCY_KINDS;
  return payload.graph.edges.filter((e) => kinds.includes(e.kind));
}
/** The data path ran on this Snapshot: its report holds flows, or says none were found in what it covered. */
/**
 * The current code index is still building its link graph (`context.readiness.graph.state`, review_map.py), so the Map's
 * surrounding connections may be missing. Only "building" counts: "degraded" has symbols and says so in its own note.
 */
export function relationshipsIndexing(payload: Pick<ReviewMapPayload, "context">): boolean {
  return payload.context.source === "current_index" && payload.context.readiness?.graph?.state === "building";
}

/** How many times the Map reads itself again while the index builds: about a minute in all, then it stops and offers Check again. */
export const INDEX_REFRESH_LIMIT = 14;

/** The wait before refresh `attempt` (1-based): 0.5 s doubling to one read every 5 s; null past the limit, so polling ends. */
export function indexRefreshDelay(attempt: number): number | null {
  if (!Number.isInteger(attempt) || attempt < 1 || attempt > INDEX_REFRESH_LIMIT) return null;
  return Math.min(5000, 500 * 2 ** (attempt - 1));
}

export function dataPathAvailable(payload: ReviewMapPayload): boolean {
  const state = payload.data_path?.state;
  return state === "ran" || state === "partial";
}
function sentence(text: string): string {
  const trimmed = text.trim();
  return !trimmed || /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}
/** Why the Data path lens is off: the stored report's own words, and a `not_available` report's setup line. */
export function dataPathUnavailableText(report: ReviewDataPathReport | undefined): string {
  if (!report) return DATA_PATH_UNAVAILABLE;
  if (report.state === "not_run") return "Data path has not run for this revision.";
  if (report.state === "not_available" && report.setup) return [sentence(report.summary), sentence(report.setup)].filter(Boolean).join(" ");
  return sentence(report.summary) || DATA_PATH_UNAVAILABLE;
}
/** A run request the server refused or could not answer, in plain words. */
export function dataPathRunFailure(error: unknown): string {
  const message = error instanceof Error ? error.message.trim() : "";
  if (!message || /^\d+$/.test(message)) return `The data path did not run${message ? ` (the server answered ${message})` : ""}. Your review is unchanged.`;
  return `The data path did not run: ${sentence(message)} Your review is unchanged.`;
}
/**
 * The server stamps architecture-delta "references" edges exactness "snapshot" and uncertain false, yet it links
 * two files whenever one uses a name the other defines (architecture_delta.py `definitions.get(tag.name)`). That is
 * a name match, not a checked fact, so it is never drawn as checked.
 */
export function edgeBasis(edge: ReviewMapEdge): EdgeBasis {
  if (edge.exactness !== "snapshot" || edge.uncertain) return "index";
  return edge.kind === "references" ? "name" : "checked";
}
export function edgeBasisText(basis: EdgeBasis): string {
  return basis === "checked" ? "Checked in code"
    : basis === "name" ? "Not verified · matched by name only"
      : "Not verified · from the current code index";
}
export function nodeBasisText(node: ReviewMapNode): string {
  if (nodeChecked(node)) return "Checked in code";
  return node.exactness !== "snapshot" ? "Not verified · from the current code index" : "Not verified · no recorded comparison for this file";
}
export function nodeChecked(node: ReviewMapNode): boolean {
  return node.exactness === "snapshot" && !node.uncertain && statusOf(node) !== "unverified";
}
function depthById(nodes: readonly ReviewMapNode[], edges: readonly ReviewMapEdge[]): Map<string, number> {
  const starts = nodes.filter((node) => ["added", "removed", "changed"].includes(statusOf(node)));
  // If a comparison is unavailable, use the explicitly changed file statuses
  // for context selection, not for a before/after claim.
  const seeds = starts.length ? starts : nodes.filter((node) => ["changed", "new"].includes(node.status));
  const depth = new Map(seeds.map((node) => [node.id, 0]));
  for (let hop = 1; hop <= 2; hop++) {
    for (const edge of edges) {
      const a = depth.get(edge.source);
      const b = depth.get(edge.target);
      if (a === hop - 1 && !depth.has(edge.target)) depth.set(edge.target, hop);
      if (b === hop - 1 && !depth.has(edge.source)) depth.set(edge.source, hop);
    }
  }
  return depth;
}

export function buildFlowLayout(
  payload: ReviewMapPayload,
  targets: readonly ReviewTarget[],
  lens: MapLens,
  view: MapView,
  steps: MapStep,
): MapFlowLayout {
  const nodes = payload.graph.nodes;
  const edges = lensEdges(payload, lens);
  const depth = depthById(nodes, edges);
  const lanes = assignLanes(payload);
  const laneX: Record<string, number> = { used_by: 25, subject: 360, uses: 695 };
  const tops: Record<string, number> = { used_by: 48, subject: 48, uses: 48 };
  const byPath = new Map<string, ReviewTarget[]>();
  for (const t of targets) { const list = byPath.get(t.path); if (list) list.push(t); else byPath.set(t.path, [t]); }
  const ordered = [...nodes].sort((a, b) => {
    const la = lanes.get(a.id) ?? "subject", lb = lanes.get(b.id) ?? "subject";
    const side = ["used_by", "subject", "uses"].indexOf(la) - ["used_by", "subject", "uses"].indexOf(lb);
    return side || (a.rank ?? 9999) - (b.rank ?? 9999) || a.path.localeCompare(b.path) || a.id.localeCompare(b.id);
  });
  const comparable = graphComparable(payload);
  const placed = ordered.map((node) => {
    const lane = laneOf(lanes.get(node.id));
    const state = statusOf(node);
    const y = tops[lane];
    tops[lane] += 84;
    const target = (byPath.get(node.path) ?? []).find((t) => t.target_id === node.target_ids?.[0]) ?? byPath.get(node.path)?.[0];
    const distance = depth.get(node.id) ?? 99;
    return {
      id: node.id, node, state, lane, x: laneX[lane] ?? 360, y, width: 280, height: 67, depth: distance,
      visible: distance <= steps || (distance === 99 && statusOf(node) !== "unchanged" && statusOf(node) !== "unverified"),
      active: comparable ? survivesView(state, view) : view === "changes",
      target,
    };
  });
  const byId = new Map(placed.map((row) => [row.id, row]));
  const placedEdges = edges.flatMap((edge) => {
    const from = byId.get(edge.source), to = byId.get(edge.target);
    if (!from || !to) return [];
    const state = statusOf(edge);
    return [{ id: edge.id, edge, basis: edgeBasis(edge), state, from, to,
      visible: from.visible && to.visible,
      active: comparable ? survivesView(state, view) && from.active && to.active : view === "changes",
    }];
  });
  const folds: MapFoldLayout[] = ["used_by", "subject", "uses"].flatMap((lane) => {
    const count = placed.filter((row) => !row.visible && (lanes.get(row.id) ?? "subject") === lane).length;
    return count ? [{ lane, x: laneX[lane], y: tops[lane], width: 280, height: 40, count }] : [];
  });
  const changed = placed.filter((r) => r.state === "changed").length;
  const added = placed.filter((r) => r.state === "added").length;
  const removed = placed.filter((r) => r.state === "removed").length;
  return {
    nodes: placed, folds, edges: placedEdges, width: 1005,
    // Room for a fold node under the longest lane keeps the height the same at every context step.
    height: Math.max(355, ...Object.values(tops).map((n) => n + 56)),
    folded: placed.filter((row) => !row.visible).length,
    summary: {
      changed, added, removed,
      newConnections: placedEdges.filter((r) => r.state === "added").length,
      notVerified: placed.filter((r) => r.state === "unverified").length,
    },
    comparable,
    truncated: Boolean(payload.graph.truncated || payload.graph.omitted_node_count),
  };
}
function laneOf(lane: string | undefined): MapLane {
  return lane === "used_by" || lane === "uses" ? lane : "subject";
}

const COMPACT_PAD = 8;
const COMPACT_GAP = 6;
const COMPACT_BAND_TITLE = 28;
const COMPACT_BAND_GAP = 12;
const COMPACT_NODE_HEIGHT = 52;
const COMPACT_FOLD_HEIGHT = 36;
/** The strip right of the nodes where connections run, so a line never crosses a node. */
const COMPACT_ARC_ROOM = 48;

/**
 * The phone diagram (review-12.4): the same nodes, connections and states as `layout`, with its lanes stacked as bands
 * (Used by, Changed code, Uses) in one column that fits `width`, so neither the diagram nor the page scrolls sideways.
 * Connections run as arcs in a strip right of the column (see `edgePath`). Only shown nodes are placed and a lane with
 * nothing shown is left out; positions depend on the shown nodes alone, so they stay put across Changes, Before and After.
 */
export function compactFlowLayout(layout: MapFlowLayout, width: number): MapFlowLayout {
  const nodeWidth = Math.max(0, width - COMPACT_PAD * 2 - COMPACT_ARC_ROOM);
  const moved = new Map<string, MapNodeLayout>();
  const folds: MapFoldLayout[] = [];
  const bands: MapBandLayout[] = [];
  let y = COMPACT_PAD;
  for (const lane of MAP_LANES) {
    const rows = layout.nodes.filter((row) => row.visible && row.lane === lane);
    const fold = layout.folds.find((item) => item.lane === lane);
    if (!rows.length && !fold) continue;
    if (bands.length) y += COMPACT_BAND_GAP;
    bands.push({ lane, y });
    y += COMPACT_BAND_TITLE;
    for (const row of rows) {
      moved.set(row.id, { ...row, x: COMPACT_PAD, y, width: nodeWidth, height: COMPACT_NODE_HEIGHT });
      y += COMPACT_NODE_HEIGHT + COMPACT_GAP;
    }
    if (fold) {
      folds.push({ ...fold, x: COMPACT_PAD, y, width: nodeWidth, height: COMPACT_FOLD_HEIGHT });
      y += COMPACT_FOLD_HEIGHT + COMPACT_GAP;
    }
  }
  const nodes = layout.nodes.map((row) => moved.get(row.id) ?? row);
  const edges = layout.edges.map((edge) => ({ ...edge, from: moved.get(edge.from.id) ?? edge.from, to: moved.get(edge.to.id) ?? edge.to }));
  return { ...layout, nodes, edges, folds, bands, width, height: Math.max(y - COMPACT_GAP + COMPACT_PAD, COMPACT_PAD * 2) };
}

/**
 * The drawn path of a connection. In the wide layout it leaves the facing sides of two lanes at mid-height. On the phone
 * it is an arc from the right side of one node to the right side of the other, through the strip beside the column; a
 * longer arc bulges further, so arcs nest instead of running on top of each other.
 */
export function edgePath(from: MapNodeLayout, to: MapNodeLayout, compact: boolean): string {
  if (!compact) {
    const sx = from.x + (from.x < to.x ? from.width : 0);
    const tx = to.x + (from.x < to.x ? 0 : to.width);
    const sy = from.y + 33, ty = to.y + 33;
    const mid = (sx + tx) / 2;
    return `M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ty}, ${tx} ${ty}`;
  }
  const x = Math.max(from.x + from.width, to.x + to.width);
  const sy = from.y + from.height / 2, ty = to.y + to.height / 2;
  const bulge = Math.round(Math.min(COMPACT_ARC_ROOM - 4, 12 + Math.abs(ty - sy) / 10));
  return `M ${x} ${sy} C ${x + bulge} ${sy}, ${x + bulge} ${ty}, ${x} ${ty}`;
}

/** D5 footer counts over what is on screen: every shown node plus every shown connection, by how it was matched. */
export function provenanceCounts(layout: MapFlowLayout, showNameMatched: boolean): { checked: number; notVerified: number } {
  let checked = 0, notVerified = 0;
  for (const row of layout.nodes) {
    if (!row.visible || !row.active) continue;
    if (nodeChecked(row.node)) checked++; else notVerified++;
  }
  for (const edge of layout.edges) {
    if (!edge.visible || !edge.active) continue;
    if (edge.basis === "checked") checked++;
    else if (showNameMatched) notVerified++;
  }
  return { checked, notVerified };
}
export function relatedTarget(node: ReviewMapNode, targets: readonly ReviewTarget[]): ReviewTarget | undefined {
  return targets.find((target) => node.target_ids?.includes(target.target_id))
    ?? targets.find((target) => target.path === node.path && (node.line === 0 || target.start_line <= node.line && node.line <= target.end_line));
}
/** "Changed 3 · Added 1 · Removed 1 · New connections 0" (SPEC 4.7); "Not compared" when no comparison exists. */
export function summaryLine(layout: MapFlowLayout, example: boolean): string {
  const { changed, added, removed, newConnections } = layout.summary;
  const text = layout.comparable ? `Changed ${changed} · Added ${added} · Removed ${removed} · New connections ${newConnections}` : "Not compared · no verified before and after for this change";
  return example ? `Example data · ${text}` : text;
}
/** The footer's "Checked in the code at <7-char sha>" (D5). */
export function footerText(payload: ReviewMapPayload, counts: { checked: number; notVerified: number }): string {
  const sha = payload.snapshot.head_sha.slice(0, 7);
  return `Checked in the code${sha ? ` at ${sha}` : ""} · ${counts.checked} checked in code · ${counts.notVerified} not verified`;
}
export function mapRowLabel(node: ReviewMapNode): string {
  return nodeName(node) || node.path || node.id;
}

/** Honest, explicitly illustrative larger example. Never sent to server or signed. */
export function exampleMap(): ReviewMapPayload {
  const spec = [
    ["entry", "api/orders.py", "changed"], ["guard", "auth/access.py", "removed"],
    ["discount", "pricing/discount.py", "changed"], ["limit", "api/rate_limit.py", "added"],
    ["total", "orders/total.py", "changed"], ["db", "storage/orders.py", "unchanged"],
    ["metrics", "ops/metrics.py", "unchanged"], ["identity", "auth/identity.py", "unchanged"],
  ] as const;
  const nodes: ReviewMapNode[] = spec.map(([id, path, status]) => ({
    id, label: path.split("/").pop() ?? path, kind: "file", path, line: 1, end_line: 20,
    status: status === "added" ? "new" : status === "unchanged" ? "context" : "changed",
    exactness: "snapshot", delta_status: status, target_ids: [], important_public_contract: false, boundaries: [],
    boundary_basis: "", selection: { kind: "review_map_node", review_id: "illustrative", revision_id: "illustrative", node_id: id, exactness: "snapshot" },
  }));
  const edges: ReviewMapEdge[] = [
    ["entry", "guard", "removed"], ["entry", "discount", "changed"], ["entry", "limit", "added"],
    ["discount", "total", "unchanged"], ["total", "db", "unchanged"], ["entry", "metrics", "unchanged"], ["guard", "identity", "removed"],
  ].map(([source, target, status], i) => ({
    id: String(i), source, target, kind: "calls", exactness: "snapshot", weight: 1, uncertain: false,
    delta_status: status, selection: { kind: "review_map_edge", review_id: "illustrative", revision_id: "illustrative", edge_id: String(i), exactness: "snapshot" },
  }));
  return {
    review_id: "illustrative", revision_id: "illustrative", revision_number: 1, level: "architecture", projection: "architecture_delta",
    architecture_delta: { status: "ready" } as ReviewMapPayload["architecture_delta"],
    graph: { level: "architecture", focus: null, nodes, edges, truncated: false },
    context: { source: "unavailable", exact_for_snapshot: false, snapshot_head_matches_checkout: false, index: {}, note: "Illustrative example only" },
    snapshot: { exactness: "snapshot", tree_fingerprint: "illustrative", source_fingerprint: "illustrative", base_sha: "",
      head_sha: "", degraded: [], provenance: { host: "", model: "", certainty: "", method: "" },
      verification: { pass: 0, fail: 0, not_run: 0, unknown: 0, checks: [] } },
  };
}
