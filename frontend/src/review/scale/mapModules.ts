/**
 * The Map of a large change lists modules, not files (SPEC 4.15, 7.1 P5-3 B). A module is the parent directory (the server
 * stamps it on every file node as `module`; for a part's path, and for an older server, it is derived from `path`); a file at
 * the repository root is the module "Repository root".
 *
 * The server's map keeps at most 300 changed files (`_MAX_OVERVIEW_CHANGED`), so the map nodes alone can miss files. The
 * members of a module are therefore built from the complete parts list AND the nodes, merged by path: every file that has a
 * part is reachable, and a module made only of such files still appears (its row says the map left it out). One pass each over
 * the nodes, the edges and the parts, with Map lookups: linear, no cap.
 */
import { statusOf, type MapState } from "../mapFlow/mapFlowModel";
import { answerCounts } from "../answerModel";
import type { ReviewMapEdge, ReviewMapNode, ReviewMapPayload, ReviewTarget } from "../types";

export const ROOT_MODULE = "Repository root";
const ID_PREFIX = "module:";

/** The module of a path: the server's `module` when it sent one, else the parent directory. */
export function moduleOf(path: string, module?: string): string {
  const name = module ?? (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
  return name || ROOT_MODULE;
}

export interface ModuleStats { parts: number; decisions: number; quickChecks: number; acceptedByRule: number; files: number; changedFiles: number }
/** One file of a module: the map node when the server sent one, and the part "Open part" goes to. */
export interface ModuleFile { path: string; node: ReviewMapNode | null; part: ReviewTarget | null }
export interface ModuleAggregate {
  /** A map payload whose nodes are modules. It feeds the same layout as the file map. */
  payload: ReviewMapPayload;
  /** Every file of a module that has a map node or a part. */
  members: Map<string, ModuleFile[]>;
  stats: Map<string, ModuleStats>;
  /** Modules with no map node at all: the server's map left them out. */
  beyondMap: Set<string>;
  /** Per module, how many of its files the server's map left out (only modules where it is above zero). */
  leftOut: Map<string, number>;
}

/** One state for a group: all added, all removed, any change at all, any not verified, else unchanged. */
function groupState(states: Iterable<MapState>): MapState {
  const seen = new Set(states);
  const moved = ["added", "removed", "changed"].filter((state) => seen.has(state as MapState));
  if (moved.length === 1 && seen.size === 1) return moved[0] as MapState;
  if (moved.length > 0) return "changed";
  return seen.has("unverified") ? "unverified" : "unchanged";
}
const DELTA: Record<MapState, string> = { added: "added", removed: "removed", changed: "changed", unchanged: "unchanged", unverified: "unverified" };

export function moduleId(name: string): string {
  return `${ID_PREFIX}${name}`;
}

export function aggregateModules(payload: ReviewMapPayload, targets: readonly ReviewTarget[]): ModuleAggregate {
  const index = indexTargets(targets);
  const members = new Map<string, ModuleFile[]>();
  const byPath = new Map<string, ModuleFile>();
  const moduleByNode = new Map<string, string>();
  const add = (name: string, file: ModuleFile) => {
    byPath.set(file.path, file);
    const list = members.get(name);
    if (list) list.push(file); else members.set(name, [file]);
  };
  for (const node of payload.graph.nodes) {
    const name = moduleOf(node.path, node.module);
    moduleByNode.set(node.id, name);
    if (byPath.has(node.path)) continue;
    add(name, { path: node.path, node, part: partFor(node, index) ?? index.byPath.get(node.path)?.[0] ?? null });
  }
  // A file the map left out (past the server's cap) is still a file with parts.
  for (const [path, parts] of index.byPath) {
    if (!byPath.has(path)) add(moduleOf(path), { path, node: null, part: parts[0] });
  }

  const stats = new Map<string, ModuleStats>();
  const statsOf = (name: string): ModuleStats => {
    let row = stats.get(name);
    if (!row) { row = { parts: 0, decisions: 0, quickChecks: 0, acceptedByRule: 0, files: 0, changedFiles: 0 }; stats.set(name, row); }
    return row;
  };
  const byModule = new Map<string, ReviewTarget[]>();
  for (const target of targets) {
    const name = moduleOf(target.path);
    const list = byModule.get(name);
    if (list) list.push(target); else byModule.set(name, [target]);
  }
  for (const [name, parts] of byModule) {
    const counts = answerCounts(parts);
    const row = statsOf(name);
    row.parts = parts.length;
    row.decisions = counts.decision;
    row.quickChecks = counts.quick_check;
    row.acceptedByRule = counts.accepted_by_rule;
  }

  const template = payload.graph.nodes[0];
  const beyondMap = new Set<string>();
  const leftOut = new Map<string, number>();
  const nodes: ReviewMapNode[] = [];
  for (const [name, files] of members) {
    const mapped = files.filter((file) => file.node !== null).map((file) => file.node as ReviewMapNode);
    const unmapped = files.length - mapped.length;
    // A file with a part but no map node is changed code the map did not carry: it counts as changed, never as unchanged.
    const states: MapState[] = [...mapped.map(statusOf), ...Array.from({ length: unmapped }, () => "changed" as const)];
    const state = groupState(states);
    const row = statsOf(name);
    row.files = files.length;
    row.changedFiles = states.filter((s) => s === "added" || s === "removed" || s === "changed").length;
    if (mapped.length === 0) beyondMap.add(name);
    if (unmapped > 0) leftOut.set(name, unmapped);
    const first = mapped[0] ?? template;
    const lanes = new Set(mapped.map((file) => file.lane ?? ""));
    let rank = 0;
    for (const file of mapped) if ((file.rank ?? 0) > 0 && (rank === 0 || (file.rank as number) < rank)) rank = file.rank as number;
    const targetIds = new Set<string>();
    for (const file of files) for (const id of file.node?.target_ids ?? []) targetIds.add(id);
    nodes.push({
      ...first,
      id: moduleId(name),
      label: name,
      kind: "module",
      module: name,
      path: name === ROOT_MODULE ? "" : name,
      line: 0,
      end_line: 0,
      status: state === "added" ? "new" : state === "unchanged" || state === "unverified" ? "context" : "changed",
      delta_status: DELTA[state],
      exactness: unmapped === 0 && mapped.every((file) => file.exactness === "snapshot") ? "snapshot" : unmapped > 0 ? "unknown" : mapped.find((file) => file.exactness !== "snapshot")?.exactness ?? "unknown",
      uncertain: mapped.some((file) => file.uncertain),
      lane: mapped.length > 0 && lanes.size === 1 ? first.lane : undefined,
      rank,
      changed_file_count: row.changedFiles,
      target_ids: [...targetIds],
      paths: files.map((file) => file.path),
      selection: first ? { ...first.selection, node_id: moduleId(name) } : undefined,
    } as unknown as ReviewMapNode);
  }

  const grouped = new Map<string, ReviewMapEdge[]>();
  for (const edge of payload.graph.edges) {
    const from = moduleByNode.get(edge.source), to = moduleByNode.get(edge.target);
    if (from === undefined || to === undefined || from === to) continue;
    const key = `${from}\u0000${to}`;
    const list = grouped.get(key);
    if (list) list.push(edge); else grouped.set(key, [edge]);
  }
  const edges: ReviewMapEdge[] = [];
  for (const [key, list] of grouped) {
    const [from, to] = key.split("\u0000");
    const first = list[0];
    const state = groupState(list.map(statusOf));
    edges.push({
      ...first,
      id: `${moduleId(from)}>${moduleId(to)}`,
      source: moduleId(from),
      target: moduleId(to),
      // A connection between modules is checked only when every connection under it is; one name match makes it a name match.
      kind: list.some((edge) => edge.kind === "references") ? "references" : first.kind,
      exactness: list.every((edge) => edge.exactness === "snapshot") ? "snapshot" : list.find((edge) => edge.exactness !== "snapshot")?.exactness ?? first.exactness,
      uncertain: list.some((edge) => edge.uncertain),
      weight: list.reduce((sum, edge) => sum + (edge.weight || 1), 0),
      delta_status: DELTA[state],
      selection: { ...first.selection, edge_id: `${moduleId(from)}>${moduleId(to)}` },
    } as ReviewMapEdge);
  }

  return { payload: { ...payload, graph: { ...payload.graph, nodes, edges } }, members, stats, beyondMap, leftOut };
}

/**
 * "3 changed files · 12 parts · 2 decisions · 9 quick checks · 1 accepted by rule": real counts from the module's files and parts.
 * A zero count is left out ("0 decisions" is noise); the parts number includes parts accepted by rule, so that count is
 * named whenever it is not zero. A module with nothing to count reads "No changed files".
 */
export function moduleCountsText(row: ModuleStats): string {
  const count = (n: number, one: string, many: string) => (n ? `${n} ${n === 1 ? one : many}` : "");
  const text = [
    count(row.changedFiles, "changed file", "changed files"),
    count(row.parts, "part", "parts"),
    count(row.decisions, "decision", "decisions"),
    count(row.quickChecks, "quick check", "quick checks"),
    row.acceptedByRule ? `${row.acceptedByRule} accepted by rule` : "",
  ].filter(Boolean).join(" · ");
  return text || "No changed files";
}

export interface TargetIndex { byId: Map<string, ReviewTarget>; byPath: Map<string, ReviewTarget[]> }
export function indexTargets(targets: readonly ReviewTarget[]): TargetIndex {
  const byId = new Map<string, ReviewTarget>();
  const byPath = new Map<string, ReviewTarget[]>();
  for (const target of targets) {
    byId.set(target.target_id, target);
    const list = byPath.get(target.path);
    if (list) list.push(target); else byPath.set(target.path, [target]);
  }
  return { byId, byPath };
}
/** The part a file node opens: the target the server named, else the part of that file at the node's line. */
export function partFor(node: ReviewMapNode, index: TargetIndex): ReviewTarget | undefined {
  for (const id of node.target_ids ?? []) {
    const hit = index.byId.get(id);
    if (hit) return hit;
  }
  return index.byPath.get(node.path)?.find((target) => node.line === 0 || (target.start_line <= node.line && node.line <= target.end_line));
}
