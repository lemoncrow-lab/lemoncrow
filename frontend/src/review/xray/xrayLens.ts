// The X-Ray as a picture (PR Lens style): the whole change in lanes by layer, cards per file and arrows between them,
// and the focused path of one file drawn the same way. Pure and deterministic over the same Map graph, flow and
// targets the walk uses: nothing here measures the DOM, fetches or asks a model. Honesty lives in the drawing: an
// arrow is solid only when this Snapshot's recorded run traced it on the side shown, dashed otherwise; lanes are read
// from paths and the Map's boundary tags, never from a run.
import {
  EXACTNESS_LABEL,
  checksFor,
  isChangedNode,
  nodeChange,
  storyExactness,
  type StoryChange,
  type StoryChecks,
  type StoryExactness,
} from "../changeStory/changeStoryModel";
import { EDGE_KIND_LABEL, baseName, edgeKind, isTestNode, moduleOf, nodeName, visualEnds } from "../reviewMapModel";
import type { ReviewMapGraph, ReviewMapNode, ReviewMapSelection, ReviewTarget } from "../types";
import {
  buildXRay,
  comparisonMeasured,
  edgeOnSide,
  graphSnapshot,
  handoffFor,
  mapBoundaries,
  nodeOnSide,
  words,
  xrayLinkContext,
  type XRay,
  type XRayBoundary,
  type XRayFlow,
  type XRayLink,
  type XRayNotMeasured,
  type XRaySide,
  type XRayStatus,
  type XRayStep,
  type XRayTone,
} from "./xrayModel";

export type LensLaneId = "tests" | "entry" | "api" | "domain" | "storage" | "other";
export type LensMode = "change" | "focus";
export type LensPlace = "main" | "tests" | "other";

/** Left to right: what a person or a schedule reaches first, then what it calls, then where data rests or leaves. */
export const MAIN_LANES: readonly LensLaneId[] = ["entry", "api", "domain", "storage"];
export const LANE_LABEL: Record<LensLaneId, string> = {
  tests: "Tests",
  entry: "Entry & UI",
  api: "API & handlers",
  domain: "Domain & services",
  storage: "Storage & external",
  other: "Docs, config & assets",
};

// lc-debt: whole-change bounds (8 cards per lane, 3 unchanged neighbours per lane, 2 sub-columns per lane, 6 tests,
// 12 docs/assets, 150 arrows); past them cards fold into "+N more". Page or expand a fold on demand when reviewers need it.
export const LENS_LANE_CAP = 8;
export const LENS_CONTEXT_CAP = 3;
export const LENS_SUBCOLUMNS = 2;
export const LENS_TESTS_CAP = 6;
export const LENS_OTHER_CAP = 12;
export const LENS_EDGE_CAP = 150;

// lc-debt: lanes are recognised by path and file-name words only; replace with typed layer facts from the index when it records them.
const UI_EXT = /\.(jsx|tsx|vue|svelte|astro|html?|css|scss|sass|less)$/i;
const NON_CODE_EXT = /\.(md|mdx|markdown|rst|txt|adoc|png|jpe?g|gif|svg|webp|ico|bmp|mp4|webm|mov|wav|mp3|ogg|pdf|ya?ml|toml|ini|cfg|conf|lock|env|csv|tsv|bru|http|rest)$/i;
const NON_CODE_NAME = /^(dockerfile|makefile|procfile|license|notice|codeowners|\.gitignore|\.dockerignore|\.editorconfig)$/i;
const STORAGE_EXT = /\.(sql|csv|tsv|parquet|db|sqlite3?)$/i;
const ENTRY_WORDS: ReadonlySet<string> = new Set([
  "frontend", "web", "ui", "pages", "page", "components", "component", "screens", "screen", "layouts", "widgets",
  "cli", "bin", "cmd", "commands", "jobs", "job", "workers", "worker", "cron", "scheduler", "consumers", "subscriber",
]);
const API_WORDS: ReadonlySet<string> = new Set([
  "api", "apis", "routes", "route", "router", "routers", "handlers", "handler", "controllers", "controller",
  "endpoints", "endpoint", "server", "rest", "graphql", "resolvers", "rpc", "webhooks", "webhook", "middleware", "urls",
  "wsgi", "asgi", "app",
]);
const STORAGE_WORDS: ReadonlySet<string> = new Set([
  "db", "database", "databases", "store", "stores", "storage", "repository", "repositories", "models", "model",
  "migrations", "migration", "schema", "schemas", "sql", "dao", "persistence", "cache", "redis", "ledger", "orm",
  "queue", "queues", "kafka", "pubsub", "sqs", "sns", "rabbitmq", "amqp", "nats", "broker", "outbox", "publisher",
  "client", "clients", "sdk", "gateway", "proxy", "grpc",
]);

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** Folder words plus the file's own name, split on case and punctuation. */
function pathWords(path: string): { dirs: string[]; stem: string[] } {
  const parts = path.split("/");
  const file = parts.pop() ?? "";
  return { dirs: parts.flatMap(words), stem: words(file.replace(/\.[^.]+$/, "")) };
}

function isNonCode(node: Pick<ReviewMapNode, "path" | "category">): boolean {
  const file = baseName(node.path);
  return NON_CODE_EXT.test(file) || NON_CODE_NAME.test(file) || /(^|\/)(docs?|media|assets|\.github)\//.test(node.path)
    || node.category === "docs" || node.category === "config";
}

/**
 * The lane a file sits in, from its path, its file name and the Map's boundary tags. A test has its own band; a doc,
 * config or asset with no code relationship sits in the strip under the picture. Recognised from names only.
 */
export function laneFor(node: Pick<ReviewMapNode, "path" | "category" | "boundaries" | "label">, connected: boolean): LensLaneId {
  if (isTestNode(node)) return "tests";
  if (!connected && isNonCode(node)) return "other";
  const file = baseName(node.path) || node.label;
  if (UI_EXT.test(file)) return "entry";
  const { dirs, stem } = pathWords(node.path);
  if ((node.boundaries ?? []).includes("persistence") || STORAGE_EXT.test(file) || stem.some((word) => STORAGE_WORDS.has(word))) return "storage";
  if (stem.join("_") === "main" || (stem.join("_") === "index" && dirs.length === 0)) return "entry";
  if (stem.some((word) => API_WORDS.has(word))) return "api";
  // The nearest folder decides: `frontend/src/api/` is the API client of a UI, `server/db/` storage under a server.
  for (let index = dirs.length - 1; index >= 0; index -= 1) {
    const word = dirs[index];
    if (STORAGE_WORDS.has(word) && word !== "client" && word !== "clients") return "storage";
    if (API_WORDS.has(word) && word !== "app") return "api";
    if (ENTRY_WORDS.has(word)) return "entry";
  }
  if (isNonCode(node)) return "other";
  return "domain";
}

export interface LensColumn {
  id: string;
  label: string;
  /** Columns of one lane share a group; the header spans them. */
  group: string;
  groupLabel: string;
}

export interface LensFold {
  count: number;
  /** The folded files' paths, and their Map node ids in the same order. */
  names: string[];
  ids: string[];
  changed: number;
}

export interface LensCard {
  id: string;
  nodeId: string;
  kind: "node" | "fold";
  place: LensPlace;
  /** Main: column index. Tests and other: position in the band. */
  column: number;
  row: number;
  lane: LensLaneId;
  title: string;
  path: string;
  folder: string;
  change: StoryChange;
  /** An unchanged neighbour drawn for context, not part of the change. */
  context: boolean;
  present: Record<XRaySide, boolean>;
  exactness: StoryExactness;
  exactnessLabel: string;
  boundaries: XRayBoundary[];
  canOpenSource: boolean;
  selection: ReviewMapSelection | null;
  checks: StoryChecks | null;
  roleLabel: string;
  folded: LensFold | null;
  /** Focus view: the walk's own step on each side. */
  steps?: Partial<Record<XRaySide, XRayStep>>;
}

export interface LensRelationship {
  kind: string;
  kindLabel: string;
  change: StoryChange;
  exactness: StoryExactness;
  exactnessLabel: string;
  observed: boolean;
}

export interface LensEdge {
  /** Unique per side: `side:from\0to`. */
  key: string;
  /** The two cards, the same on both sides: a selection survives the Before/After toggle by it. */
  pair: string;
  from: string;
  to: string;
  /** The strongest relationship between the two cards; the panel lists all of them. */
  link: XRayLink;
  members: LensRelationship[];
  change: StoryChange;
  observed: boolean;
  sequence: number | null;
  /** How many relationships into a folded card this arrow stands for; 0 for a plain arrow. */
  fold: number;
}

export interface LensDiagram {
  mode: LensMode;
  status: XRayStatus;
  statusNote: string;
  rootId: string;
  rootTitle: string;
  snapshot: string;
  walkSnapshot: string;
  columns: LensColumn[];
  cards: LensCard[];
  edges: Record<XRaySide, LensEdge[]>;
  measured: boolean;
  notMeasured: XRayNotMeasured[];
  degraded: string[];
  /** Cards and arrows left out by the bounds, as words. */
  bounded: string[];
  totals: { files: number; changed: number; context: number };
  walks?: Record<XRaySide, XRay>;
}

export interface LensStep {
  cardId: string;
  edgeKey: string | null;
  basis: "observed" | "reading";
  label: string;
}

export interface LensCaveat {
  text: string;
  action: "data_path" | null;
}

export interface LensView {
  side: XRaySide;
  status: XRayStatus;
  statusNote: string;
  cards: LensCard[];
  edges: LensEdge[];
  observed: number;
  caveat: LensCaveat | null;
  steps: LensStep[];
}

export interface LensEdgeStyle {
  line: "solid" | "dashed";
  tone: XRayTone;
  /** The non-colour mark on the arrow: + added, ~ changed, − removed; none for an unchanged or unmeasured arrow. */
  glyph: "+" | "~" | "−" | null;
  animate: boolean;
  label: string;
}

const MOVED: ReadonlySet<StoryChange> = new Set(["added", "removed", "changed"]);
const KIND_SCORE: Record<string, number> = { impacts: 6, calls: 5, imports: 4, other: 2, references: 1 };

export function toneOf(change: StoryChange): XRayTone {
  return change === "added" || change === "removed" || change === "changed" || change === "unchanged" ? change : "unknown";
}

/** Solid only when observed in a recorded run on this side; colour and a glyph say how the change moved it. */
export function edgeStyle(edge: LensEdge, reducedMotion = false): LensEdgeStyle {
  const tone = toneOf(edge.change);
  const glyph = edge.change === "added" ? "+" : edge.change === "removed" ? "−" : edge.change === "changed" ? "~" : null;
  const basis = edge.observed
    ? edge.sequence !== null ? `observed #${edge.sequence} in a recorded run` : "observed in a recorded run"
    : "static relationship, not observed";
  const what = edge.fold ? `${plural(edge.fold, "relationship")} into folded files` : edge.link.sentence;
  return {
    line: edge.observed ? "solid" : "dashed",
    tone,
    glyph,
    animate: edge.observed && edge.sequence !== null && !reducedMotion,
    label: `${what} · ${tone === "unknown" ? "change unknown" : edge.link.mark.label} · ${basis}`,
  };
}

/** Several relationships between two cards read as one arrow: a moved one decides its change, any observed one its line. */
function combinedChange(changes: StoryChange[]): StoryChange {
  const has = (change: StoryChange) => changes.includes(change);
  if (has("changed") || (has("added") && has("removed"))) return "changed";
  if (has("added")) return "added";
  if (has("removed")) return "removed";
  if (has("unchanged")) return "unchanged";
  if (has("context_only")) return "context_only";
  return "unknown";
}

function linkScore(link: XRayLink): number {
  return (link.order === "observed" ? 1e6 : 0) + (MOVED.has(link.change) ? 1e5 : 0) + (link.exactness === "snapshot" ? 1e4 : 0)
    + (KIND_SCORE[edgeKind(link.kind)] ?? 0) * 100;
}

function relationshipOf(link: XRayLink): LensRelationship {
  return {
    kind: link.kind,
    kindLabel: EDGE_KIND_LABEL[edgeKind(link.kind)],
    change: link.change,
    exactness: link.exactness,
    exactnessLabel: link.exactnessLabel,
    observed: link.order === "observed",
  };
}

function mergeEdges(side: XRaySide, from: string, to: string, links: XRayLink[], fold: number): LensEdge {
  const ordered = [...links].sort((a, b) => linkScore(b) - linkScore(a) || a.edgeId.localeCompare(b.edgeId));
  const observed = ordered.filter((link) => link.order === "observed");
  const sequences = observed.map((link) => link.sequence).filter((value): value is number => value !== null);
  const pair = `${from}\u0000${to}`;
  return {
    key: `${side}:${pair}`,
    pair,
    from,
    to,
    link: ordered[0],
    members: ordered.map(relationshipOf),
    change: combinedChange(ordered.map((link) => link.change)),
    // An arrow into a fold stands for several relationships: solid only when every one of them was observed.
    observed: fold ? observed.length === ordered.length : observed.length > 0,
    sequence: fold ? null : sequences.length ? Math.min(...sequences) : null,
    fold,
  };
}

function emptyDiagram(mode: LensMode, status: XRayStatus, statusNote: string, graph: ReviewMapGraph, walkSnapshot: string, rootId = "", rootTitle = ""): LensDiagram {
  return {
    mode,
    status,
    statusNote,
    rootId,
    rootTitle,
    snapshot: graphSnapshot(graph),
    walkSnapshot,
    columns: [],
    cards: [],
    edges: { before: [], after: [] },
    measured: comparisonMeasured(graph),
    notMeasured: [],
    degraded: [],
    bounded: [],
    totals: { files: 0, changed: 0, context: 0 },
  };
}

const BASE_NOT_MEASURED: XRayNotMeasured = {
  key: "base_side",
  label: "Before (base) relationships",
  reason: "None of this Map's relationships comes from a base-to-revision comparison, so Before draws the files the diff names and no arrows.",
  needs: "delta_status, or base_weight and snapshot_weight, on the Map's edges",
};

function observedNotMeasured(flow: XRayFlow | null | undefined): XRayNotMeasured | null {
  const observed = flow?.observed;
  if (observed?.available && (observed.base.length || observed.head.length)) return null;
  return {
    key: "observed_order",
    label: "Observed order",
    reason: "No recorded run of this revision traced these files, so no arrow is a runtime order. Dashed arrows are relationships in the code.",
    needs: "a runtime-observation run whose call trace lands on these files (flow.observed.base / flow.observed.head)",
  };
}

const PAYLOAD_NOT_MEASURED: XRayNotMeasured = {
  key: "payloads",
  label: "What travels on an arrow",
  reason: "A recorded call names its caller and callee functions and an evidence reference, not the arguments or the result it carried.",
  needs: "per-call request and response digests on the observed calls (ReviewObservedCall)",
};

const LANE_NOTE = "Lanes are read from file paths, file names and the Map's boundary tags, not from a run.";

function degradedOf(graph: ReviewMapGraph): string[] {
  const out: string[] = [];
  if (graph.truncated) out.push(`The Map is bounded${graph.omitted_node_count ? `: ${plural(graph.omitted_node_count, "file")} left out` : ""}, so arrows to files outside it are not drawn.`);
  if (graph.reason) out.push(graph.reason);
  return out;
}

interface Candidate {
  node: ReviewMapNode;
  lane: LensLaneId;
  changed: boolean;
  score: number;
}

export interface ChangeLensInput {
  graph: ReviewMapGraph;
  flow?: XRayFlow | null;
  targets?: readonly ReviewTarget[] | null;
  walkSnapshot?: string;
  laneCap?: number;
  contextCap?: number;
  testsCap?: number;
  otherCap?: number;
  edgeCap?: number;
}

/**
 * The whole change: every changed file and the unchanged files right next to it, in lanes by layer, with the arrows
 * between them on both sides. Positions are shared by Before and After, so the toggle redraws the same picture.
 */
export function buildChangeLens(input: ChangeLensInput): LensDiagram {
  const { graph } = input;
  const snapshot = graphSnapshot(graph);
  const walkSnapshot = input.walkSnapshot ?? snapshot;
  if (walkSnapshot && snapshot && walkSnapshot !== snapshot) {
    return emptyDiagram("change", "stale", "A newer revision replaced the one this X-Ray was opened on. Rebuild it on the revision shown now.", graph, walkSnapshot);
  }
  const laneCap = Math.max(1, input.laneCap ?? LENS_LANE_CAP);
  const contextCap = Math.max(0, input.contextCap ?? LENS_CONTEXT_CAP);
  const testsCap = Math.max(1, input.testsCap ?? LENS_TESTS_CAP);
  const otherCap = Math.max(1, input.otherCap ?? LENS_OTHER_CAP);
  const edgeCap = Math.max(1, input.edgeCap ?? LENS_EDGE_CAP);
  const measured = comparisonMeasured(graph);
  const contexts: Record<XRaySide, ReturnType<typeof xrayLinkContext>> = {
    before: xrayLinkContext(graph, input.flow, "before"),
    after: xrayLinkContext(graph, input.flow, "after"),
  };
  const byId = contexts.after.byId;
  const edges = graph.edges.filter((edge) => edge.source !== edge.target && byId.has(edge.source) && byId.has(edge.target));
  const changedIds = new Set(graph.nodes.filter(isChangedNode).map((node) => node.id));
  if (!changedIds.size) return emptyDiagram("change", "missing_root", "This revision's Map lists no changed file, so there is no change to draw.", graph, walkSnapshot);

  // Unchanged files count only when they touch the change; each is ranked by how much of it they touch.
  const touches = new Map<string, number>();
  const degree = new Map<string, number>();
  const observedIds = new Set<string>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    for (const [id, other] of [[edge.source, edge.target], [edge.target, edge.source]] as const) {
      if (!changedIds.has(id) && changedIds.has(other)) touches.set(id, (touches.get(id) ?? 0) + 1);
    }
    for (const side of ["before", "after"] as const) {
      const { from, to } = visualEnds(edge);
      if (edgeOnSide(edge, side) && contexts[side].observedFor(edge, byId.get(from)!, byId.get(to)!)) {
        observedIds.add(from);
        observedIds.add(to);
      }
    }
  }
  const candidates: Candidate[] = graph.nodes
    .filter((node) => changedIds.has(node.id) || touches.has(node.id))
    .map((node) => {
      const changed = changedIds.has(node.id);
      const lane = laneFor(node, (degree.get(node.id) ?? 0) > 0);
      const score = (changed ? 1e6 : 0) + (observedIds.has(node.id) ? 1e5 : 0) + (MOVED.has(nodeChange(node)) ? 1e4 : 0)
        + Math.min(99, touches.get(node.id) ?? 0) * 100 + Math.min(99, degree.get(node.id) ?? 0);
      return { node, lane, changed, score };
    });
  const byScore = (a: Candidate, b: Candidate) => b.score - a.score || a.node.path.localeCompare(b.node.path) || a.node.id.localeCompare(b.node.id);

  const targets = input.targets ? new Map(input.targets.map((target) => [target.target_id, target])) : null;
  const cardOf = (node: ReviewMapNode, lane: LensLaneId, place: LensPlace, changed: boolean): LensCard => {
    const exactness = storyExactness(node.exactness);
    const boundaries = mapBoundaries(node);
    const handoff = handoffFor(undefined, node);
    if (handoff && place === "main") boundaries.push(handoff);
    return {
      id: node.id,
      nodeId: node.id,
      kind: "node",
      place,
      column: 0,
      row: 0,
      lane,
      title: nodeName(node),
      path: node.path,
      folder: moduleOf(node),
      change: nodeChange(node),
      context: !changed,
      present: { before: nodeOnSide(node, "before"), after: nodeOnSide(node, "after") },
      exactness,
      exactnessLabel: EXACTNESS_LABEL[exactness],
      boundaries,
      canOpenSource: exactness === "snapshot" && Boolean(node.path),
      selection: node.selection ?? null,
      checks: checksFor(node, targets),
      roleLabel: changed ? LANE_LABEL[lane] : `${LANE_LABEL[lane]} · unchanged neighbour`,
      folded: null,
    };
  };
  const foldOf = (lane: LensLaneId, place: LensPlace, rest: Candidate[]): LensCard => ({
    id: `fold:${lane}`,
    nodeId: "",
    kind: "fold",
    place,
    column: 0,
    row: 0,
    lane,
    title: `+${rest.length} more`,
    path: "",
    folder: "",
    change: "unknown",
    context: rest.every((item) => !item.changed),
    present: {
      before: rest.some((item) => nodeOnSide(item.node, "before")),
      after: rest.some((item) => nodeOnSide(item.node, "after")),
    },
    exactness: "unknown",
    exactnessLabel: EXACTNESS_LABEL.unknown,
    boundaries: [],
    canOpenSource: false,
    selection: null,
    checks: null,
    roleLabel: `${LANE_LABEL[lane]} · folded`,
    folded: { count: rest.length, names: rest.map((item) => item.node.path), ids: rest.map((item) => item.node.id), changed: rest.filter((item) => item.changed).length },
  });

  // Which card a file is drawn as: its own, or its lane's fold.
  const cardFor = new Map<string, string>();
  const cards: LensCard[] = [];
  const bounded: string[] = [];
  const place = (lane: LensLaneId, where: LensPlace, cap: number, ctxCap: number) => {
    const all = candidates.filter((item) => item.lane === lane).sort(byScore);
    if (!all.length) return [] as LensCard[];
    const changed = all.filter((item) => item.changed);
    const context = all.filter((item) => !item.changed);
    const keep = [...changed.slice(0, cap), ...context.slice(0, Math.max(0, Math.min(ctxCap, cap - changed.length)))];
    const kept = new Set(keep.map((item) => item.node.id));
    const rest = all.filter((item) => !kept.has(item.node.id));
    const out = keep.map((item) => cardOf(item.node, lane, where, item.changed));
    for (const card of out) cardFor.set(card.nodeId, card.id);
    if (rest.length) {
      const fold = foldOf(lane, where, rest);
      for (const item of rest) cardFor.set(item.node.id, fold.id);
      out.push(fold);
      bounded.push(`${plural(rest.length, "file")} folded into “+${rest.length} more” in ${LANE_LABEL[lane]}`);
    }
    return out;
  };
  const laneCards = new Map<LensLaneId, LensCard[]>();
  for (const lane of MAIN_LANES) laneCards.set(lane, place(lane, "main", laneCap, contextCap));
  const testCards = place("tests", "tests", testsCap, Math.min(contextCap, 2));
  const otherCards = place("other", "other", otherCap, 0);

  // Arrows per side, one per pair of cards; the cap keeps the moved and observed ones first.
  const sideEdges = (side: XRaySide): LensEdge[] => {
    const groups = new Map<string, { from: string; to: string; links: XRayLink[] }>();
    for (const edge of edges) {
      if (!edgeOnSide(edge, side)) continue;
      const { from, to } = visualEnds(edge);
      const fromNode = byId.get(from)!;
      const toNode = byId.get(to)!;
      if (!nodeOnSide(fromNode, side) || !nodeOnSide(toNode, side)) continue;
      const a = cardFor.get(from);
      const b = cardFor.get(to);
      if (!a || !b || a === b) continue;
      const key = `${a}\u0000${b}`;
      const group = groups.get(key) ?? groups.set(key, { from: a, to: b, links: [] }).get(key)!;
      group.links.push(contexts[side].linkOf(edge));
    }
    const merged = [...groups.values()].map((group) => {
      const folds = group.from.startsWith("fold:") || group.to.startsWith("fold:");
      return mergeEdges(side, group.from, group.to, group.links, folds ? group.links.length : 0);
    });
    merged.sort((a, b) => linkScore(b.link) - linkScore(a.link) || a.key.localeCompare(b.key));
    return merged;
  };
  const allEdges = { before: measured ? sideEdges("before") : [], after: sideEdges("after") };
  let dropped = 0;
  for (const side of ["before", "after"] as const) {
    if (allEdges[side].length > edgeCap) {
      dropped = Math.max(dropped, allEdges[side].length - edgeCap);
      allEdges[side] = allEdges[side].slice(0, edgeCap);
    }
  }
  if (dropped) bounded.push(`${plural(dropped, "arrow")} past the ${edgeCap}-arrow bound not drawn`);

  // Columns: each lane splits into up to three sub-columns by who uses whom inside it, then rows reduce crossings.
  const union = [...allEdges.after, ...allEdges.before];
  const columns: LensColumn[] = [];
  const columnOf = new Map<string, number>();
  for (const lane of MAIN_LANES) {
    const inLane = laneCards.get(lane) ?? [];
    if (!inLane.length) continue;
    const ids = new Set(inLane.map((card) => card.id));
    const depth = new Map(inLane.map((card) => [card.id, 0]));
    const inner = union.filter((edge) => ids.has(edge.from) && ids.has(edge.to) && edge.from !== edge.to);
    // Longest path inside the lane, bounded: a cycle cannot push a card past the last sub-column.
    for (let pass = 0; pass < LENS_SUBCOLUMNS; pass += 1) {
      for (const edge of inner) {
        const next = Math.min(LENS_SUBCOLUMNS - 1, (depth.get(edge.from) ?? 0) + 1);
        if (next > (depth.get(edge.to) ?? 0)) depth.set(edge.to, next);
      }
    }
    for (const card of inLane) if (card.kind === "fold") depth.set(card.id, Math.max(...depth.values()));
    const used = [...new Set(depth.values())].sort((a, b) => a - b);
    used.forEach((level, index) => {
      columns.push({ id: `${lane}:${index}`, label: index === 0 ? LANE_LABEL[lane] : "", group: lane, groupLabel: LANE_LABEL[lane] });
      for (const card of inLane) if (depth.get(card.id) === level) columnOf.set(card.id, columns.length - 1);
    });
  }
  const mainCards = MAIN_LANES.flatMap((lane) => laneCards.get(lane) ?? []);
  for (const card of mainCards) card.column = columnOf.get(card.id) ?? 0;
  orderRows(mainCards, columns.length, union);
  orderBand(testCards, mainCards, union);
  otherCards.forEach((card, index) => { card.column = index; card.row = 0; });
  cards.push(...mainCards, ...testCards, ...otherCards);

  const notMeasured: XRayNotMeasured[] = [];
  if (!measured) notMeasured.push(BASE_NOT_MEASURED);
  const observedGap = observedNotMeasured(input.flow);
  if (observedGap) notMeasured.push(observedGap);
  notMeasured.push(PAYLOAD_NOT_MEASURED);
  const contextCount = cards.filter((card) => card.kind === "node" && card.context).length;
  return {
    mode: "change",
    status: "ready",
    statusNote: "",
    rootId: "",
    rootTitle: "",
    snapshot,
    walkSnapshot,
    columns,
    cards,
    edges: allEdges,
    measured,
    notMeasured,
    degraded: [...degradedOf(graph), LANE_NOTE],
    bounded,
    totals: { files: candidates.length, changed: changedIds.size, context: contextCount },
  };
}

/**
 * Rows inside each column by the barycentre of their neighbours: a sweep left to right, then right to left. A fold
 * stays last in its column. Deterministic: ties keep the previous order.
 */
function orderRows(cards: LensCard[], columnCount: number, edges: LensEdge[]): void {
  const byColumn = Array.from({ length: columnCount }, () => [] as LensCard[]);
  for (const card of cards) byColumn[card.column]?.push(card);
  const rowOf = new Map<string, number>();
  const columnOf = new Map(cards.map((card) => [card.id, card.column]));
  byColumn.forEach((column) => column.forEach((card, index) => rowOf.set(card.id, index)));
  const neighbours = new Map<string, string[]>();
  for (const edge of edges) {
    (neighbours.get(edge.from) ?? neighbours.set(edge.from, []).get(edge.from)!).push(edge.to);
    (neighbours.get(edge.to) ?? neighbours.set(edge.to, []).get(edge.to)!).push(edge.from);
  }
  const sweep = (index: number, toward: (column: number) => boolean) => {
    const column = byColumn[index];
    const keyed = column.map((card, position) => {
      const near = (neighbours.get(card.id) ?? []).filter((id) => columnOf.has(id) && toward(columnOf.get(id)!));
      const center = near.length ? near.reduce((sum, id) => sum + (rowOf.get(id) ?? 0), 0) / near.length : position;
      return { card, center: card.kind === "fold" ? Number.POSITIVE_INFINITY : center, position };
    });
    keyed.sort((a, b) => a.center - b.center || a.position - b.position);
    byColumn[index] = keyed.map((item) => item.card);
    byColumn[index].forEach((card, row) => rowOf.set(card.id, row));
  };
  for (let index = 1; index < columnCount; index += 1) sweep(index, (column) => column < index);
  for (let index = columnCount - 2; index >= 0; index -= 1) sweep(index, (column) => column > index);
  for (const card of cards) card.row = rowOf.get(card.id) ?? 0;
}

/** The tests band under the lanes: each test sits under the columns of the code it reaches, left to right. */
function orderBand(band: LensCard[], main: LensCard[], edges: LensEdge[]): void {
  const columnOf = new Map(main.map((card) => [card.id, card.column + card.row / 100]));
  const keyed = band.map((card, position) => {
    const near = edges.filter((edge) => edge.from === card.id || edge.to === card.id)
      .map((edge) => columnOf.get(edge.from === card.id ? edge.to : edge.from))
      .filter((value): value is number => value !== undefined);
    const center = card.kind === "fold" ? Number.POSITIVE_INFINITY : near.length ? near.reduce((a, b) => a + b, 0) / near.length : 1e3 + position;
    return { card, center, position };
  });
  keyed.sort((a, b) => a.center - b.center || a.position - b.position);
  keyed.forEach((item, index) => { item.card.column = index; item.card.row = 0; });
}

export interface FocusLensInput {
  graph: ReviewMapGraph;
  flow?: XRayFlow | null;
  targets?: readonly ReviewTarget[] | null;
  rootId: string;
  walkSnapshot?: string;
  maxDepth?: number;
  maxBranching?: number;
  maxSteps?: number;
  maxConsumers?: number;
}

const FOCUS_ROLE_COLUMN: Record<string, string> = { consumer: "Reached from", entry: "This file" };

/**
 * One file's path, zoomed in: its callers, then what it calls, depth by depth, on both sides of the change. The walks
 * are `buildXRay`'s own; this only lays them out on one shared grid so Before and After keep their places.
 */
export function buildFocusLens(input: FocusLensInput): LensDiagram {
  const walkSnapshot = input.walkSnapshot ?? graphSnapshot(input.graph);
  const walk = (side: XRaySide) => buildXRay({ ...input, side, walkSnapshot });
  const walks: Record<XRaySide, XRay> = { before: walk("before"), after: walk("after") };
  const ready = (["after", "before"] as const).filter((side) => walks[side].status === "ready");
  const lead = walks[ready[0] ?? "after"];
  if (!ready.length) {
    return { ...emptyDiagram("focus", lead.status, lead.statusNote, input.graph, walkSnapshot, lead.rootId, lead.rootTitle), walks, notMeasured: lead.notMeasured };
  }
  const hasCallers = ready.some((side) => walks[side].consumers.length > 0 || walks[side].truncation.consumers > 0);
  const offset = hasCallers ? 1 : 0;
  const maxDepth = Math.max(0, ...ready.flatMap((side) => walks[side].steps.map((step) => step.depth + (step.hiddenLinks ? 1 : 0))));
  const columns: LensColumn[] = [];
  if (hasCallers) columns.push({ id: "callers", label: FOCUS_ROLE_COLUMN.consumer, group: "callers", groupLabel: FOCUS_ROLE_COLUMN.consumer });
  for (let depth = 0; depth <= maxDepth; depth += 1) {
    const label = depth === 0 ? FOCUS_ROLE_COLUMN.entry : depth === 1 ? "Calls / uses" : `Then · step ${depth}`;
    columns.push({ id: `depth:${depth}`, label, group: `depth:${depth}`, groupLabel: label });
  }
  const byNode = new Map<string, LensCard>();
  const rows = columns.map(() => 0);
  const byId = new Map(input.graph.nodes.map((node) => [node.id, node]));
  const addCard = (id: string, column: number, make: () => LensCard): LensCard => {
    const known = byNode.get(id);
    if (known) return known;
    const card = make();
    card.column = column;
    card.row = rows[column]++;
    byNode.set(id, card);
    return card;
  };
  const stepCard = (step: XRayStep, side: XRaySide, column: number) => {
    const node = byId.get(step.nodeId);
    const card = addCard(step.nodeId, column, () => ({
      id: step.nodeId,
      nodeId: step.nodeId,
      kind: "node",
      place: "main",
      column,
      row: 0,
      lane: node ? laneFor(node, true) : "domain",
      title: step.title,
      path: step.path,
      folder: node ? moduleOf(node) : "",
      change: step.change,
      context: !MOVED.has(step.change),
      present: { before: false, after: false },
      exactness: step.exactness,
      exactnessLabel: step.exactnessLabel,
      boundaries: step.boundaries,
      canOpenSource: step.canOpenSource,
      selection: step.selection,
      checks: step.checks,
      roleLabel: step.roleLabel,
      folded: null,
      steps: {},
    }));
    card.present[side] = true;
    card.steps = { ...card.steps, [side]: step };
    return card;
  };
  const foldCard = (id: string, column: number, side: XRaySide, count: number, label: string) => {
    const card = addCard(id, column, () => ({
      id, nodeId: "", kind: "fold", place: "main", column, row: 0, lane: "domain", title: `+${count} more`, path: "", folder: "",
      change: "unknown", context: true, present: { before: false, after: false }, exactness: "unknown", exactnessLabel: EXACTNESS_LABEL.unknown,
      boundaries: [], canOpenSource: false, selection: null, checks: null, roleLabel: label, folded: { count, names: [], ids: [], changed: 0 },
    }));
    card.present[side] = true;
    if (card.folded && count > card.folded.count) {
      card.folded.count = count;
      card.title = `+${count} more`;
    }
    return card;
  };
  const edges: Record<XRaySide, LensEdge[]> = { before: [], after: [] };
  for (const side of ["after", "before"] as const) {
    const xray = walks[side];
    if (xray.status !== "ready") continue;
    const byKey = new Map(xray.steps.map((step) => [step.key, step]));
    for (const step of xray.steps) stepCard(step, side, offset + Math.max(0, step.depth));
    for (const step of xray.consumers) stepCard(step, side, 0);
    const out: LensEdge[] = [];
    const link = (from: string, to: string, value: XRayLink) => out.push(mergeEdges(side, from, to, [value], 0));
    for (const step of xray.consumers) if (step.link) link(step.nodeId, xray.rootId, step.link);
    for (const step of xray.steps) {
      const parent = step.parentKey ? byKey.get(step.parentKey) : null;
      if (parent && step.link) link(parent.nodeId, step.nodeId, step.link);
      if (step.hiddenLinks > 0 && step.link !== undefined) {
        const fold = foldCard(`fold:${step.nodeId}`, offset + step.depth + 1, side, step.hiddenLinks, step.exit === "depth" ? "Past the depth bound" : "More links from here");
        const sample = step.link ?? xray.steps[0].link;
        if (sample) out.push({ ...mergeEdges(side, step.nodeId, fold.id, [sample], step.hiddenLinks), change: "unknown", observed: false });
      }
    }
    if (xray.truncation.consumers > 0) {
      const fold = foldCard("fold:callers", 0, side, xray.truncation.consumers, "More callers");
      const sample = xray.consumers[0]?.link;
      if (sample) out.push({ ...mergeEdges(side, fold.id, xray.rootId, [sample], xray.truncation.consumers), change: "unknown", observed: false });
    }
    edges[side] = out;
  }
  const cards = [...byNode.values()];
  const bounded = ready.flatMap((side) => {
    const { truncation, bounds } = walks[side];
    const sideWord = side === "before" ? "Before" : "After";
    return [
      truncation.depth ? `${sideWord}: ${plural(truncation.depth, "link")} past depth ${bounds.depth}` : "",
      truncation.branching ? `${sideWord}: ${plural(truncation.branching, "sibling link")} over ${bounds.branching} per step` : "",
      truncation.total ? `${sideWord}: ${plural(truncation.total, "step")} over the ${bounds.steps}-step bound` : "",
      truncation.consumers ? `${sideWord}: ${plural(truncation.consumers, "more caller")}` : "",
    ];
  }).filter(Boolean);
  const notMeasured = lead.notMeasured.filter((item, index, all) => all.findIndex((other) => other.key === item.key) === index);
  return {
    mode: "focus",
    status: "ready",
    statusNote: "",
    rootId: lead.rootId,
    rootTitle: lead.rootTitle,
    snapshot: lead.snapshot,
    walkSnapshot,
    columns,
    cards,
    edges,
    measured: lead.comparison === "measured",
    notMeasured: [...notMeasured, PAYLOAD_NOT_MEASURED],
    degraded: [...new Set([...walks.after.degraded, ...walks.before.degraded])],
    bounded: [...new Set(bounded)],
    totals: { files: cards.filter((card) => card.kind === "node").length, changed: cards.filter((card) => !card.context).length, context: cards.filter((card) => card.kind === "node" && card.context).length },
    walks,
  };
}

function readingOrder(cards: LensCard[]): LensCard[] {
  const placeRank: Record<LensPlace, number> = { main: 0, tests: 1, other: 2 };
  return [...cards].sort((a, b) => placeRank[a.place] - placeRank[b.place] || a.column - b.column || a.row - b.row || a.id.localeCompare(b.id));
}

/**
 * The walkthrough: arrows observed in a recorded run first, in their recorded order, then the rest in reading order
 * (lane by lane, top to bottom; the tests after the code). One card, and the arrow that reaches it, per step.
 * On the whole change it steps through changed files only; unchanged neighbours are there to be seen, not walked.
 */
export function walkthroughOrder(view: Pick<LensView, "cards" | "edges">, mode: LensMode): LensStep[] {
  const byId = new Map(view.cards.map((card) => [card.id, card]));
  const visited = new Set<string>();
  const steps: LensStep[] = [];
  const name = (id: string) => byId.get(id)?.title ?? id;
  const visit = (cardId: string, edge: LensEdge | null, basis: LensStep["basis"]) => {
    visited.add(cardId);
    const label = edge
      ? `${basis === "observed" ? `Observed #${edge.sequence ?? "?"}` : edge.observed ? "Observed" : "Static"} · ${name(edge.from)} → ${name(edge.to)}`
      : name(cardId);
    steps.push({ cardId, edgeKey: edge?.key ?? null, basis, label });
  };
  const observed = view.edges
    .filter((edge) => edge.observed && !edge.fold && byId.has(edge.from) && byId.has(edge.to))
    .sort((a, b) => (a.sequence ?? Number.MAX_SAFE_INTEGER) - (b.sequence ?? Number.MAX_SAFE_INTEGER) || a.key.localeCompare(b.key));
  for (const edge of observed) {
    if (!visited.has(edge.from)) visit(edge.from, null, "observed");
    visit(edge.to, edge, "observed");
  }
  for (const card of readingOrder(view.cards)) {
    if (visited.has(card.id) || card.kind === "fold" || card.place === "other") continue;
    if (mode === "change" && card.context) continue;
    const incoming = view.edges
      .filter((edge) => edge.to === card.id && visited.has(edge.from) && !edge.fold)
      .sort((a, b) => linkScore(b.link) - linkScore(a.link) || a.key.localeCompare(b.key))[0] ?? null;
    visit(card.id, incoming, "reading");
  }
  return steps;
}

/**
 * What one side shows: removed files only on Before, added only on After, changed on both, the side's own arrows,
 * its walkthrough, and at most one caveat line.
 */
export function lensView(diagram: LensDiagram, side: XRaySide): LensView {
  const sideWord = side === "before" ? "Before" : "After";
  let status = diagram.status;
  let statusNote = diagram.statusNote;
  if (diagram.mode === "focus" && diagram.walks && diagram.walks[side].status !== "ready") {
    status = diagram.walks[side].status;
    statusNote = diagram.walks[side].statusNote;
  }
  if (status !== "ready") return { side, status, statusNote, cards: [], edges: [], observed: 0, caveat: null, steps: [] };
  const cards = diagram.cards.filter((card) => card.present[side]);
  const shown = new Set(cards.map((card) => card.id));
  const edges = diagram.edges[side].filter((edge) => shown.has(edge.from) && shown.has(edge.to));
  const observed = edges.filter((edge) => edge.observed).length;
  const caveat: LensCaveat | null = side === "before" && !diagram.measured
    ? { text: "Before has no base comparison on this Map, so only the files the diff names are drawn.", action: null }
    : !edges.length
      ? cards.length > 1 ? { text: `No code relationship between these files is recorded on ${sideWord}.`, action: null } : null
      : observed === 0
        ? { text: `No recorded run on ${sideWord}: every arrow is a static code relationship.`, action: "data_path" }
        : observed < edges.length
          ? { text: `${observed} of ${plural(edges.length, "arrow")} observed in a recorded run; dashed ones are static.`, action: null }
          : null;
  return { side, status, statusNote, cards, edges, observed, caveat, steps: walkthroughOrder({ cards, edges }, diagram.mode) };
}

/** The paths an arrow touches, for the behavior changes recorded on either end. */
export function edgePaths(view: Pick<LensView, "cards">, edge: LensEdge): string[] {
  const byId = new Map(view.cards.map((card) => [card.id, card]));
  return [byId.get(edge.from), byId.get(edge.to)].flatMap((card) => (card?.kind === "node" ? [card.path] : card?.folded?.names ?? [])).filter(Boolean);
}

/** Words for a card's change on the side shown. */
export function cardChangeWords(card: LensCard, side: XRaySide): string {
  if (card.kind === "fold") return card.folded?.changed ? `${plural(card.folded.changed, "changed file")} folded` : "unchanged neighbours folded";
  if (card.change === "removed") return side === "before" ? "removed by this change" : "removed";
  if (card.change === "context_only") return "context only, not compared";
  if (card.change === "unknown") return "change unknown";
  return card.context ? "unchanged neighbour" : card.change;
}

