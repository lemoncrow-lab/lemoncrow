// Map engine: view model and deterministic lane layout, shared by the Review
// Map (a Snapshot of one Change) and the `/code` focus lanes (the current
// index). Adapters turn their data into a MapPayload; nothing here knows which.
//
// The map answers one question: what is the subject, what uses it, and what it
// uses.
// Instead of a force-directed hairball, every node is placed in one of three
// lanes relative to the subject (the changed files, or the focused file or
// symbol):
//
//   used by  ->  subject  ->  uses
//
// Lanes read left to right as "depends on", so every edge has the same visual
// grammar. Files are grouped by folder, the subject lane is ordered by review
// priority, and the side lanes are ordered by the barycenter of the subject
// cards they connect to, which keeps most edges short and nearly horizontal.
// Everything here is pure and deterministic so it can be unit tested.

import type { MapEdge, MapLevel, MapMemberSymbol, MapNode, MapPayload } from "./types";

export type MapLane = "used_by" | "subject" | "uses";
export type LinkMode = "key" | "all";
export type EdgeKind = "impacts" | "calls" | "imports" | "references" | "other";

export const EDGE_KIND_LABEL: Record<EdgeKind, string> = {
  impacts: "affects",
  calls: "calls",
  imports: "imports",
  references: "references",
  other: "relates to",
};

const KIND_FACTOR: Record<EdgeKind, number> = { impacts: 10, calls: 7, imports: 5, references: 1, other: 2 };
const TEST_PATH = /(^|\/)(tests?|__tests__|specs?)\/|(^|\/)test_[^/]+\.py$|_test\.(py|go|rb)$|\.(test|spec)\.[cm]?[jt]sx?$/i;

export function edgeKind(kind: string): EdgeKind {
  if (kind === "impacts") return "impacts";
  if (kind === "calls" || kind === "call") return "calls";
  if (kind === "imports" || kind === "import") return "imports";
  if (kind === "references" || kind === "reference") return "references";
  return "other";
}

export function isKeyKind(kind: EdgeKind): boolean {
  return kind !== "references";
}

export function isChangedStatus(node: Pick<MapNode, "status">): boolean {
  return node.status === "changed" || node.status === "new";
}

export function isTestNode(node: Pick<MapNode, "category" | "path">): boolean {
  return node.category === "test" || TEST_PATH.test(node.path);
}

/**
 * Whether hiding tests hides *node*. A drilled-into test file or symbol stays;
 * the overview's focus only suggests where to start, so it hides like the rest.
 */
export function hiddenAsTest(payload: Pick<MapPayload, "level" | "graph">, node: MapNode): boolean {
  return isTestNode(node) && !(payload.level !== "architecture" && node.id === payload.graph.focus);
}

export function moduleOf(node: Pick<MapNode, "module" | "path">): string {
  if (typeof node.module === "string") return node.module;
  const slash = node.path.lastIndexOf("/");
  return slash > 0 ? node.path.slice(0, slash) : "";
}

export function baseName(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash >= 0 ? path.slice(slash + 1) : path;
}

/** Folder label that keeps the most specific segments visible. */
export function shortModule(module: string, maxChars = 34): string {
  if (!module) return "(repository root)";
  if (module.length <= maxChars) return module;
  const parts = module.split("/");
  let out = parts[parts.length - 1] ?? module;
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    const next = parts[index] + "/" + out;
    if (next.length + 2 > maxChars) break;
    out = next;
  }
  return "…/" + out;
}

/** Display name for a node: files show their basename, symbols their label. */
export function nodeName(node: MapNode): string {
  if (node.kind === "file") return baseName(node.path) || node.label;
  return node.label;
}

export function changeLetter(node: MapNode): string {
  const change = (node.change || node.change_kinds?.[0] || "").toLowerCase();
  if (node.status === "new" || change === "added") return "A";
  if (change === "deleted" || change === "removed") return "D";
  if (change === "renamed") return "R";
  if (change === "copied") return "C";
  return "M";
}

export function isFullyReviewed(node: MapNode): boolean {
  const review = node.review;
  return Boolean(review && review.targets > 0 && review.reviewed >= review.targets && !review.needs_changes);
}

// Parser node kinds ("lexical_declaration", "property_signature") mean nothing
// to a reviewer; the first matching word wins, so specific kinds come first.
const SYMBOL_KIND_WORDS: [RegExp, string][] = [
  [/interface|protocol/, "interface"],
  [/method|constructor/, "method"],
  [/property|field|member|attribute/, "field"],
  [/class/, "class"],
  [/struct/, "struct"],
  [/enum/, "enum"],
  [/trait/, "trait"],
  [/type/, "type"],
  [/function|arrow|lambda|^def$|^fn$/, "function"],
  [/module|namespace|package/, "module"],
  [/macro/, "macro"],
  [/const/, "const"],
  [/lexical|variable|^let$|^var$|assignment/, "variable"],
];

export function symbolKindLabel(kind: string | undefined): string {
  const value = (kind ?? "").toLowerCase();
  if (!value) return "symbol";
  for (const [pattern, word] of SYMBOL_KIND_WORDS) if (pattern.test(value)) return word;
  return value.replace(/_/g, " ");
}

const DECLARATION_WORDS = new Set(["class", "interface", "struct", "enum", "trait", "type", "function", "module", "macro"]);

/** 0 for top-level declarations, rising for members and plain values. */
export function symbolTier(symbol: Pick<MapMemberSymbol, "kind">): number {
  const word = symbolKindLabel(symbol.kind);
  if (DECLARATION_WORDS.has(word)) return 0;
  if (word === "method") return 1;
  if (word === "const" || word === "variable") return 2;
  return 3;
}

// Control keywords are never declarations; they only appear when a parser
// mistakes a statement for a member.
const NOT_A_NAME = new Set(["if", "else", "for", "while", "switch", "case", "return", "catch", "try", "do", "with", "match"]);

export function reviewableSymbols<T extends Pick<MapMemberSymbol, "label">>(symbols: readonly T[]): T[] {
  return symbols.filter((symbol) => !NOT_A_NAME.has(symbol.label));
}

const SIZE_REASON = /^\+(\d+) -(\d+)$/;
/** Changed lines at which review ordering's size score saturates (ordering.py `_SIZE_CAP * _SIZE_DIVISOR`). */
const LARGE_CHANGE_LINES = 400;

/**
 * Ranking reasons worth reading. The size signal arrives as a bare diffstat
 * on every changed file, which the map already shows next to the file, so it
 * is dropped, and reads as "large change" only when the file really is large.
 */
export function reasonsOf(node: Pick<MapNode, "reasons">): string[] {
  const reasons = node.reasons ?? [];
  const out = reasons.filter((reason) => !SIZE_REASON.test(reason));
  const large = reasons.some((reason) => {
    const match = SIZE_REASON.exec(reason);
    return match !== null && Number(match[1]) + Number(match[2]) >= LARGE_CHANGE_LINES;
  });
  if (large) out.push("large change");
  return out;
}

/** The current-index relationship graph is still being built and has produced nothing yet. */
export function relationshipsPending(payload: Pick<MapPayload, "context" | "graph">): boolean {
  return payload.context.source === "current_index"
    && payload.context.readiness?.graph?.state === "building"
    && !payload.graph.edges.some((edge) => edge.exactness === "current_index");
}

function edgeWeight(edge: MapEdge): number {
  return KIND_FACTOR[edgeKind(edge.kind)] * Math.max(1, Math.min(6, edge.weight || 1));
}

/** Visual direction: from the dependent to what it depends on. */
export function visualEnds(edge: MapEdge): { from: string; to: string } {
  // An impact edge points from the changed code to the site it reaches; the
  // site *uses* the changed code, so it is drawn the other way round.
  return edgeKind(edge.kind) === "impacts"
    ? { from: edge.target, to: edge.source }
    : { from: edge.source, to: edge.target };
}

export function subjectIds(payload: MapPayload): Set<string> {
  if (payload.level === "architecture") {
    // A Review's subject is its changed files. Code's module focus sends the module's members too, as
    // status "subject" when they have no uncommitted change: they are the subject, not used-by context.
    return new Set(payload.graph.nodes.filter((node) => isChangedStatus(node) || node.status === "subject").map((node) => node.id));
  }
  return new Set(payload.graph.focus ? [payload.graph.focus] : []);
}

function countOf(count: number, one: string): string {
  return `${count} ${count === 1 ? one : `${one}s`}`;
}

/**
 * The symbol line of a focused file's header. A row counts as changed only when the server says so:
 * a Review lists the changed symbols, while Code's file focus lists every symbol with an empty `change`.
 */
export function focusSymbolNote(node: Pick<MapNode, "status" | "symbol_count">, symbols: readonly Pick<MapMemberSymbol, "change">[]): string {
  if (!symbols.length) {
    if (node.status === "subject") return "No indexed symbols";
    if (isChangedStatus(node)) return "No symbol boundary captured";
    return node.status === "impacted" ? "Affected by the change" : "Surrounding code";
  }
  const changed = symbols.filter((symbol) => symbol.change && symbol.change !== "unchanged").length;
  if (node.status === "subject" || changed === 0) return countOf(node.symbol_count ?? symbols.length, "symbol");
  return countOf(changed === symbols.length ? node.symbol_count ?? changed : changed, "changed symbol");
}

/** Lane per node. The server's lane hint wins; older servers are inferred. */
export function assignLanes(payload: MapPayload): Map<string, MapLane> {
  const subject = subjectIds(payload);
  const usedBy = new Map<string, number>();
  const uses = new Map<string, number>();
  for (const edge of payload.graph.edges) {
    const { from, to } = visualEnds(edge);
    const fromSubject = subject.has(from);
    const toSubject = subject.has(to);
    if (fromSubject === toSubject) continue;
    const weight = edgeWeight(edge);
    if (toSubject) usedBy.set(from, (usedBy.get(from) ?? 0) + weight);
    else uses.set(to, (uses.get(to) ?? 0) + weight);
  }
  const lanes = new Map<string, MapLane>();
  for (const node of payload.graph.nodes) {
    if (subject.has(node.id)) {
      lanes.set(node.id, "subject");
    } else if (node.lane === "used_by" || node.lane === "uses") {
      lanes.set(node.id, node.lane);
    } else if (node.status === "impacted" && payload.level === "architecture") {
      lanes.set(node.id, "used_by");
    } else {
      const left = usedBy.get(node.id) ?? 0;
      const right = uses.get(node.id) ?? 0;
      lanes.set(node.id, right > left ? "uses" : "used_by");
    }
  }
  return lanes;
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export const LAYOUT = {
  pad: 20,
  laneHeader: 40,
  groupHeader: 24,
  groupGap: 18,
  subjectCard: 46,
  subjectGap: 6,
  contextCard: 30,
  contextGap: 4,
  moreRow: 26,
  fileFocusHeader: 72,
  symbolRow: 30,
  symbolFocus: 112,
  arcGutter: 56,
  focusSymbolsShow: 12,
  contextCollapseAt: 7,
  contextCollapsedShow: 5,
} as const;

/**
 * Folder folding for the subject lane scales with the size of the change so a
 * large review still opens on a first frame that shows every folder. Folders
 * are visited in review-priority order; once `budget` cards are on the map,
 * later folders fold down to their header row.
 */
export function subjectFolding(changedCount: number): { at: number; show: number; budget: number } {
  if (changedCount > 60) return { at: 6, show: 4, budget: 24 };
  if (changedCount > 30) return { at: 8, show: 5, budget: 36 };
  return { at: 12, show: 8, budget: Number.POSITIVE_INFINITY };
}

export interface MapCardLayout<N extends MapNode = MapNode> {
  id: string;
  node: N;
  lane: MapLane;
  variant: "subject" | "file-focus" | "symbol-focus" | "context";
  groupKey: string;
  x: number;
  y: number;
  w: number;
  h: number;
  /** File focus only: the changed symbol rows edges can land on. */
  rows?: { symbol: NonNullable<N["symbols"]>[number]; y: number }[];
  /** File focus only: a fold for symbols beyond the first screenful. */
  symbolFold?: { key: string; hidden: number; expanded: boolean; unlisted: number };
  /** A shared dependency or dependent: how many changed files it links to. */
  sharedBy?: number;
}

export interface MapGroupLayout {
  key: string;
  lane: MapLane;
  module: string;
  label: string;
  x: number;
  y: number;
  w: number;
  total: number;
  hidden: number;
  collapsible: boolean;
  expanded: boolean;
  /** Folded down to its header row: the header itself unfolds it. */
  folded: boolean;
  moreY: number | null;
  /** Set when the group is a reading-order chapter rather than a folder. */
  chapter?: string;
}

export interface MapEdgeLayout<E extends MapEdge = MapEdge> {
  key: string;
  id: string;
  edge: E;
  kind: EdgeKind;
  from: string;
  to: string;
  d: string;
  arrow: string;
  labelX: number;
  labelY: number;
  width: number;
  /** Drawn at rest; otherwise only while an endpoint is active. */
  resting: boolean;
  internal: boolean;
  symbolId?: string;
}

export interface MapLaneLayout {
  lane: MapLane;
  x: number;
  w: number;
  count: number;
}

export interface MapModel<N extends MapNode = MapNode, E extends MapEdge = MapEdge> {
  level: MapLevel;
  width: number;
  height: number;
  lanes: MapLaneLayout[];
  groups: MapGroupLayout[];
  cards: MapCardLayout<N>[];
  cardById: Map<string, MapCardLayout<N>>;
  edges: MapEdgeLayout<E>[];
  neighbors: Map<string, Set<string>>;
  /** Node id → the group holding it, whether its card is shown or folded away. */
  groupKeyOf: Map<string, string>;
  /** Visual reading order for keyboard navigation, per lane. */
  laneOrder: Record<MapLane, string[]>;
  hiddenTests: number;
  hiddenContext: number;
  counts: Record<MapLane, number>;
}

export interface MapChapter {
  /** Stable Review cohort identity when chapters come from a Review projection. */
  id?: string;
  label: string;
  paths: readonly string[];
}

export interface LayoutOptions {
  width: number;
  linkMode: LinkMode;
  hideTests: boolean;
  expanded: ReadonlySet<string>;
  /** Reading-order chapters: the overview's changed lane groups by chapter first, folder second. */
  chapters?: readonly MapChapter[];
}

interface Placed {
  node: MapNode;
  lane: MapLane;
  index: number;
}

function groupKey(lane: MapLane, module: string): string {
  return lane + "::" + module;
}

/** Path → first chapter holding it; one chapter says nothing about order, so it is ignored. */
function chapterIndex(chapters: readonly MapChapter[] | undefined): Map<string, { label: string; order: number }> {
  const byPath = new Map<string, { label: string; order: number }>();
  if (!chapters || chapters.length < 2) return byPath;
  chapters.forEach((chapter, order) => {
    for (const path of chapter.paths) if (path && !byPath.has(path)) byPath.set(path, { label: chapter.label, order });
  });
  return byPath;
}

/** Keeps each folder's files together, folders in the order their first file ranks. */
function byFirstFolder(members: Placed[]): Placed[] {
  const first = new Map<string, number>();
  members.forEach((placed, rank) => {
    const module = moduleOf(placed.node);
    if (!first.has(module)) first.set(module, rank);
  });
  return members
    .map((placed, rank) => ({ placed, rank, folder: first.get(moduleOf(placed.node)) ?? rank }))
    .sort((a, b) => a.folder - b.folder || a.rank - b.rank)
    .map((item) => item.placed);
}

function shortText(text: string, maxChars: number): string {
  return text.length <= maxChars ? text : text.slice(0, Math.max(1, maxChars - 1)).trimEnd() + "…";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function strokeWidth(edge: MapEdge): number {
  return 1 + Math.min(0.8, Math.log2(1 + Math.max(1, edge.weight || 1)) * 0.2);
}

/** Left-to-right curve with horizontal tangents, after an optional straight lead. */
export function horizontalPath(xStart: number, xCurve: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(24, Math.abs(x2 - xCurve) * 0.5);
  const lead = xCurve > xStart ? ` H${xCurve}` : "";
  return `M${xStart},${y1}${lead} C${xCurve + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

/** Arrowhead length; edge paths stop this far short of a card so the head meets the line. */
export const ARROW_LEN = 4.5;

export function arrowHead(x: number, y: number, pointing: "left" | "right"): string {
  const back = pointing === "right" ? -ARROW_LEN : ARROW_LEN;
  return `M${x},${y} L${x + back},${y - 2.5} L${x + back},${y + 2.5} Z`;
}

export function buildMapModel<N extends MapNode, E extends MapEdge>(payload: MapPayload<N, E>, options: LayoutOptions): MapModel<N, E> {
  return layoutMap(payload as unknown as MapPayload, options) as unknown as MapModel<N, E>;
}

function layoutMap(payload: MapPayload, options: LayoutOptions): MapModel {
  const level = payload.level;
  const lanes = assignLanes(payload);
  const allNodes = payload.graph.nodes;

  // 1. Which nodes are in play.
  let hiddenTests = 0;
  const present = new Map<string, Placed>();
  allNodes.forEach((node, index) => {
    if (options.hideTests && hiddenAsTest(payload, node)) {
      hiddenTests += 1;
      return;
    }
    present.set(node.id, { node, lane: lanes.get(node.id) ?? "used_by", index });
  });

  // 2. Candidate edges between present nodes. Key links drive layout; weak
  //    references are only drawn on demand in "key" mode.
  const candidateEdges = payload.graph.edges.filter((edge) => {
    if (!present.has(edge.source) || !present.has(edge.target) || edge.source === edge.target) return false;
    const source = present.get(edge.source)!;
    const target = present.get(edge.target)!;
    // Side-to-side links do not explain the subject.
    return source.lane === "subject" || target.lane === "subject";
  });
  const restingEdge = (edge: MapEdge) => options.linkMode === "all" || isKeyKind(edgeKind(edge.kind));

  // 3. Surrounding code with no resting link is noise in the first frame.
  const linked = new Set<string>();
  for (const edge of candidateEdges) {
    if (!restingEdge(edge)) continue;
    linked.add(edge.source);
    linked.add(edge.target);
  }
  let hiddenContext = 0;
  for (const [id, placed] of [...present]) {
    if (placed.lane === "subject") continue;
    if (placed.node.status === "context" && !linked.has(id)) {
      present.delete(id);
      hiddenContext += 1;
    }
  }

  // Strength of each side node's connection to the subject, for collapse
  // priority, and the subject cards it touches, for barycenter ordering.
  const strength = new Map<string, number>();
  const touches = new Map<string, Map<string, number>>();
  const sideEdges = new Map<string, MapEdge[]>();
  for (const edge of candidateEdges) {
    if (!present.has(edge.source) || !present.has(edge.target)) continue;
    const source = present.get(edge.source)!;
    const target = present.get(edge.target)!;
    if (source.lane === "subject" && target.lane === "subject") continue;
    const side = source.lane === "subject" ? target : source;
    const anchor = source.lane === "subject" ? source : target;
    const weight = edgeWeight(edge);
    strength.set(side.node.id, (strength.get(side.node.id) ?? 0) + weight);
    const row = touches.get(side.node.id) ?? new Map<string, number>();
    row.set(anchor.node.id, (row.get(anchor.node.id) ?? 0) + weight);
    touches.set(side.node.id, row);
    const list = sideEdges.get(side.node.id) ?? [];
    list.push(edge);
    sideEdges.set(side.node.id, list);
  }

  const byLane: Record<MapLane, Placed[]> = { used_by: [], subject: [], uses: [] };
  for (const placed of present.values()) byLane[placed.lane].push(placed);
  const cards: MapCardLayout[] = [];
  const cardById = new Map<string, MapCardLayout>();
  const groupKeyOf = new Map<string, string>();
  const groups: MapGroupLayout[] = [];
  const top = LAYOUT.laneHeader + 8;

  // 4. Subject lane, vertically: groups in review-priority order (the server
  //    already ranks changed files), cards within a group likewise. Horizontal
  //    placement waits until the side lanes are known.
  //    With reading-order chapters, a changed file sits in its chapter (in
  //    chapter order, folders kept together inside it); files outside every
  //    chapter keep their folder groups after the chapters.
  const chapterOf = chapterIndex(level === "architecture" ? options.chapters : undefined);
  const subjectGroups = new Map<string, { module: string; chapter?: string; order: number; members: Placed[] }>();
  for (const placed of [...byLane.subject].sort((a, b) => a.index - b.index)) {
    const module = moduleOf(placed.node);
    const chapter = chapterOf.get(placed.node.path);
    const id = chapter ? "chapter:" + chapter.label : module;
    let bucket = subjectGroups.get(id);
    if (!bucket) {
      bucket = { module: chapter ? "" : module, chapter: chapter?.label, order: chapter?.order ?? Number.POSITIVE_INFINITY, members: [] };
      subjectGroups.set(id, bucket);
    }
    bucket.members.push(placed);
  }
  const orderedGroups = [...subjectGroups.entries()].sort((a, b) => a[1].order - b[1].order);
  for (const [, bucket] of orderedGroups) if (bucket.chapter) bucket.members = byFirstFolder(bucket.members);
  let y = top;
  const folding = subjectFolding(byLane.subject.length);
  let budget = folding.budget;
  for (const [id, { module, chapter, members }] of orderedGroups) {
    const key = groupKey("subject", id);
    for (const placed of members) groupKeyOf.set(placed.node.id, key);
    const expanded = options.expanded.has(key);
    // The budget is spent as if nothing were expanded, so opening one folder
    // never folds another.
    let restShow = members.length;
    if (level === "architecture") {
      if (members.length >= folding.at) restShow = folding.show;
      // A folder of one or two files costs no more than its fold row would.
      if (members.length > 2) restShow = Math.min(restShow, Math.max(0, budget));
      budget -= restShow;
    }
    const collapsible = restShow < members.length;
    const shown = collapsible && !expanded ? members.slice(0, restShow) : members;
    const folded = collapsible && !expanded && shown.length === 0;
    const group: MapGroupLayout = {
      key,
      lane: "subject",
      module,
      label: module,
      x: 0,
      y,
      w: 0,
      total: members.length,
      hidden: members.length - shown.length,
      collapsible,
      expanded,
      folded,
      moreY: null,
      ...(chapter ? { chapter } : {}),
    };
    groups.push(group);
    y += LAYOUT.groupHeader;
    for (const placed of shown) {
      const card = subjectCard(placed, level, y, key);
      cards.push(card);
      cardById.set(card.id, card);
      y += card.h + LAYOUT.subjectGap;
    }
    if (collapsible && !folded) {
      group.moreY = y;
      y += LAYOUT.moreRow + LAYOUT.subjectGap;
    }
    y += LAYOUT.groupGap - LAYOUT.subjectGap;
  }
  let bottom = y;

  // Where a side file's links land on the subject: the card's header, or the
  // symbol rows of a focused file that the link reaches.
  const barycenter = (id: string): number => {
    let total = 0;
    let weight = 0;
    for (const edge of sideEdges.get(id) ?? []) {
      const anchorId = edge.source === id ? edge.target : edge.source;
      const card = cardById.get(anchorId);
      if (!card) continue;
      const value = edgeWeight(edge);
      const rows = card.rows ? matchRows(edge, card.rows) : [];
      const y = rows.length
        ? rows.reduce((sum, row) => sum + row.y, 0) / rows.length
        : card.y + Math.min(card.h, card.variant === "file-focus" ? LAYOUT.fileFocusHeader : card.h) / 2;
      total += y * value;
      weight += value;
    }
    return weight ? total / weight : Number.POSITIVE_INFINITY;
  };
  // A surrounding file whose only subject links sit in a collapsed group has
  // nothing to point at; it returns when that group is expanded.
  for (const lane of ["used_by", "uses"] as const) {
    byLane[lane] = byLane[lane].filter((placed) =>
      placed.node.exactness === "snapshot" || Number.isFinite(barycenter(placed.node.id)));
  }

  // 5. Horizontal geometry. Lanes fill the canvas up to comfortable maxima and
  //    the whole diagram centers; a very narrow canvas scrolls instead of
  //    squeezing cards below legibility.
  const hasLeft = byLane.used_by.length > 0;
  const hasRight = byLane.uses.length > 0;
  const hasInternal = level === "architecture"
    && candidateEdges.some((edge) => cardById.get(edge.source)?.lane === "subject" && cardById.get(edge.target)?.lane === "subject");
  const arc = hasInternal ? LAYOUT.arcGutter : 0;
  const pad = LAYOUT.pad;
  const width = Math.max(options.width, 320);
  const sideCount = Number(hasLeft) + Number(hasRight);
  const subjectMax = level === "architecture" ? 380 : 420;
  let sideW = clamp(width * 0.22, 176, 264);
  let gap = clamp(width * 0.08, 56, 132);
  const subjectW = clamp(width - pad * 2 - sideCount * (sideW + gap) - arc, 280, subjectMax);
  let used = pad * 2 + sideCount * (sideW + gap) + subjectW + arc;
  if (used > width && sideCount) {
    const over = used - width;
    gap -= Math.max(0, Math.min(over / sideCount, gap - 44));
    used = pad * 2 + sideCount * (sideW + gap) + subjectW + arc;
    if (used > width) {
      sideW = Math.max(160, sideW - (used - width) / sideCount);
      used = pad * 2 + sideCount * (sideW + gap) + subjectW + arc;
    }
  }
  const totalW = Math.max(width, used);
  let cursor = pad + Math.max(0, (totalW - used) / 2);
  const laneX: Record<MapLane, number> = { used_by: 0, subject: 0, uses: 0 };
  if (hasLeft) {
    laneX.used_by = cursor;
    cursor += sideW + gap;
  }
  laneX.subject = cursor;
  cursor += subjectW + arc;
  if (hasRight) laneX.uses = cursor + gap;
  const laneW: Record<MapLane, number> = { used_by: sideW, subject: subjectW, uses: sideW };
  for (const card of cards) {
    card.x = laneX.subject;
    card.w = subjectW;
  }
  for (const group of groups) {
    group.x = laneX.subject;
    group.w = subjectW;
    group.label = group.chapter ? shortText(group.chapter, Math.floor(subjectW / 7.2)) : shortModule(group.module, Math.floor(subjectW / 7.2));
  }

  // High-degree surrounding files (a shared types module, a db session) link
  // to most of the change. Drawn like any other node they pull long bundles
  // through every gutter, so they sit at the top of their lane, say how much
  // of the change they touch, and their links appear on demand.
  const sharedBy = new Map<string, number>();
  for (const [id, row] of touches) {
    let count = 0;
    for (const anchor of row.keys()) if (cardById.has(anchor)) count += 1;
    sharedBy.set(id, count);
  }
  const hubThreshold = Math.max(6, Math.ceil(cards.length * 0.3));
  const hubs = new Set([...sharedBy].filter(([, count]) => count >= hubThreshold).map(([id]) => id));
  const orderKey = (id: string) => (hubs.has(id) ? -1 : barycenter(id));

  // 6. Side lanes, ordered by the barycenter of the subject cards they touch.
  for (const lane of ["used_by", "uses"] as const) {
    if (!byLane[lane].length) continue;
    const byModule = new Map<string, Placed[]>();
    for (const placed of byLane[lane]) {
      const module = moduleOf(placed.node);
      const bucket = byModule.get(module) ?? [];
      bucket.push(placed);
      byModule.set(module, bucket);
    }
    const blocks = [...byModule].map(([module, members]) => {
      const key = groupKey(lane, module);
      for (const placed of members) groupKeyOf.set(placed.node.id, key);
      const collapsible = members.length >= LAYOUT.contextCollapseAt;
      const expanded = options.expanded.has(key);
      // Snapshot facts first, then the strongest links, when space is short.
      const byStrength = [...members].sort((a, b) =>
        Number(b.node.exactness === "snapshot") - Number(a.node.exactness === "snapshot")
        || (strength.get(b.node.id) ?? 0) - (strength.get(a.node.id) ?? 0)
        || a.index - b.index);
      const shown = (collapsible && !expanded ? byStrength.slice(0, LAYOUT.contextCollapsedShow) : byStrength)
        .map((placed) => ({ placed, bary: orderKey(placed.node.id) }))
        .sort((a, b) => a.bary - b.bary || a.placed.index - b.placed.index);
      const finite = shown.map((item) => item.bary).filter(Number.isFinite);
      const bary = shown.some((item) => hubs.has(item.placed.node.id))
        ? -1
        : finite.length ? finite.reduce((sum, value) => sum + value, 0) / finite.length : Number.POSITIVE_INFINITY;
      const height = LAYOUT.groupHeader
        + shown.length * (LAYOUT.contextCard + LAYOUT.contextGap)
        + (collapsible ? LAYOUT.moreRow + LAYOUT.contextGap : 0);
      return { module, key, members, shown, collapsible, expanded, bary, height };
    }).sort((a, b) => a.bary - b.bary || a.module.localeCompare(b.module));

    let cursorY = top;
    for (const block of blocks) {
      const desired = Number.isFinite(block.bary) && block.bary >= 0 ? block.bary - block.height / 2 : cursorY;
      let blockY = Math.max(cursorY, desired, top);
      const group: MapGroupLayout = {
        key: block.key,
        lane,
        module: block.module,
        label: shortModule(block.module, Math.floor(sideW / 6.6)),
        x: laneX[lane],
        y: blockY,
        w: sideW,
        total: block.members.length,
        hidden: block.members.length - block.shown.length,
        collapsible: block.collapsible,
        expanded: block.expanded,
        folded: false,
        moreY: null,
      };
      groups.push(group);
      blockY += LAYOUT.groupHeader;
      for (const { placed } of block.shown) {
        const card: MapCardLayout = {
          id: placed.node.id,
          node: placed.node,
          lane,
          variant: "context",
          groupKey: block.key,
          x: laneX[lane],
          y: blockY,
          w: sideW,
          h: LAYOUT.contextCard,
          sharedBy: hubs.has(placed.node.id) ? sharedBy.get(placed.node.id) : undefined,
        };
        cards.push(card);
        cardById.set(card.id, card);
        blockY += LAYOUT.contextCard + LAYOUT.contextGap;
      }
      if (block.collapsible) {
        group.moreY = blockY;
        blockY += LAYOUT.moreRow + LAYOUT.contextGap;
      }
      cursorY = blockY + LAYOUT.groupGap;
    }
    bottom = Math.max(bottom, cursorY);
  }

  // 7. Edges between laid-out cards. Endpoints are first collected per card
  //    side, then spread along that side in the order of their far ends, so a
  //    busy file fans links out instead of stacking them on one point.
  const edges: MapEdgeLayout[] = [];
  const neighbors = new Map<string, Set<string>>();
  const link = (a: string, b: string) => {
    const setA = neighbors.get(a) ?? new Set<string>();
    setA.add(b);
    neighbors.set(a, setA);
    const setB = neighbors.get(b) ?? new Set<string>();
    setB.add(a);
    neighbors.set(b, setB);
  };
  interface Port { card: MapCardLayout; side: "left" | "right"; y: number; fixed: boolean; farY: number }
  interface Pending {
    edge: MapEdge;
    kind: EdgeKind;
    from: string;
    to: string;
    internal: boolean;
    resting: boolean;
    width: number;
    symbolId?: string;
    a: Port;
    b: Port;
  }
  const pending: Pending[] = [];
  // At rest a surrounding file shows its strongest link plus any nearby ones;
  // long-distance links wait for hover so the gutters do not fill with long
  // vertical strokes.
  const strongestLink = new Map<string, { id: string; score: number }>();
  for (const edge of candidateEdges) {
    if (!restingEdge(edge)) continue;
    const sides = [edge.source, edge.target].filter((id) => cardById.get(id)?.lane !== "subject");
    for (const id of sides) {
      const score = edgeWeight(edge);
      const prior = strongestLink.get(id);
      if (!prior || score > prior.score) strongestLink.set(id, { id: edge.id, score });
    }
  }
  const middle = (card: MapCardLayout) => card.y + Math.min(card.h, card.variant === "file-focus" ? LAYOUT.fileFocusHeader : 46) / 2;
  for (const edge of candidateEdges) {
    const { from, to } = visualEnds(edge);
    const fromCard = cardById.get(from);
    const toCard = cardById.get(to);
    if (!fromCard || !toCard) continue;
    link(from, to);
    const kind = edgeKind(edge.kind);
    const base = { edge, kind, from, to, width: strokeWidth(edge) };
    const hubLink = hubs.has(from) || hubs.has(to);
    if (fromCard.lane === "subject" && toCard.lane === "subject") {
      pending.push({
        ...base,
        internal: true,
        // Cross-folder coupling arcs are long and cross everything; they
        // appear when either end is hovered or selected. Exact impacts show.
        resting: restingEdge(edge) && (fromCard.groupKey === toCard.groupKey || edge.exactness === "snapshot"),
        a: { card: fromCard, side: "right", y: middle(fromCard), fixed: false, farY: middle(toCard) },
        b: { card: toCard, side: "right", y: middle(toCard), fixed: false, farY: middle(fromCard) },
      });
      continue;
    }
    const leftCard = fromCard.x <= toCard.x ? fromCard : toCard;
    const sideCard = fromCard.lane === "subject" ? toCard : fromCard;
    const subjectCard = sideCard === fromCard ? toCard : fromCard;
    const near = Math.abs(middle(sideCard) - middle(subjectCard)) <= 240;
    const strongest = strongestLink.get(sideCard.id)?.id === edge.id;
    const rows = toCard.rows ? matchRows(edge, toCard.rows) : [];
    const targets = rows.length ? rows.map((row) => ({ y: row.y, symbolId: row.symbol.id as string | undefined })) : [{ y: middle(toCard), symbolId: undefined }];
    for (const target of targets) {
      const fromPort: Port = { card: fromCard, side: fromCard === leftCard ? "right" : "left", y: middle(fromCard), fixed: false, farY: target.y };
      const toPort: Port = { card: toCard, side: toCard === leftCard ? "right" : "left", y: target.y, fixed: Boolean(target.symbolId), farY: middle(fromCard) };
      pending.push({ ...base, internal: false, resting: restingEdge(edge) && !hubLink && (strongest || near), symbolId: target.symbolId, a: fromPort, b: toPort });
    }
  }
  const sides = new Map<string, Port[]>();
  for (const item of pending) {
    for (const port of [item.a, item.b]) {
      if (port.fixed) continue;
      const key = port.card.id + ":" + port.side;
      const list = sides.get(key) ?? [];
      list.push(port);
      sides.set(key, list);
    }
  }
  for (const ports of sides.values()) {
    if (ports.length < 2) continue;
    const card = ports[0].card;
    const span = Math.min(card.h, card.variant === "file-focus" ? LAYOUT.fileFocusHeader : 46);
    const top = card.y + Math.min(8, span * 0.2);
    const usable = span - 2 * Math.min(8, span * 0.2);
    ports.sort((a, b) => a.farY - b.farY);
    ports.forEach((port, index) => {
      port.y = top + (usable * (index + 1)) / (ports.length + 1);
    });
  }
  for (const item of pending) {
    const key = item.symbolId ? item.edge.id + "::" + item.symbolId : item.edge.id;
    if (item.internal) {
      const xr = item.a.card.x + item.a.card.w;
      const y1 = item.a.y;
      const y2 = item.b.y;
      const bulge = Math.min(arc - 8, 12 + Math.abs(y2 - y1) * 0.16);
      edges.push({
        key,
        id: item.edge.id,
        edge: item.edge,
        kind: item.kind,
        from: item.from,
        to: item.to,
        d: `M${xr},${y1} C${xr + bulge},${y1} ${xr + bulge},${y2} ${xr + ARROW_LEN},${y2}`,
        arrow: arrowHead(xr + 0.5, y2, "left"),
        labelX: xr + bulge * 0.75,
        labelY: (y1 + y2) / 2,
        width: item.width,
        resting: item.resting,
        internal: true,
      });
      continue;
    }
    // Ports face each other; a link leaving the subject to the right runs
    // straight through the coupling gutter before it curves.
    const leftPort = item.a.side === "right" ? item.a : item.b;
    const rightPort = leftPort === item.a ? item.b : item.a;
    const x1 = leftPort.card.x + leftPort.card.w;
    const xCurve = x1 + (leftPort.card.lane === "subject" ? arc : 0);
    const x2 = rightPort.card.x;
    const pointsRight = item.b === rightPort;
    const startX = pointsRight ? x1 : x1 + ARROW_LEN;
    const endX = pointsRight ? x2 - ARROW_LEN : x2;
    edges.push({
      key,
      id: item.edge.id,
      edge: item.edge,
      kind: item.kind,
      from: item.from,
      to: item.to,
      d: horizontalPath(startX, Math.max(startX, xCurve), leftPort.y, endX, rightPort.y),
      arrow: arrowHead(pointsRight ? x2 : x1, pointsRight ? rightPort.y : leftPort.y, pointsRight ? "right" : "left"),
      labelX: (xCurve + x2) / 2,
      labelY: (leftPort.y + rightPort.y) / 2,
      width: item.width,
      resting: item.resting,
      internal: false,
      symbolId: item.symbolId,
    });
  }
  // Heavier and exact edges paint last so they stay on top.
  edges.sort((a, b) => Number(a.edge.exactness === "snapshot") - Number(b.edge.exactness === "snapshot") || a.width - b.width);

  const laneOrder: Record<MapLane, string[]> = { used_by: [], subject: [], uses: [] };
  for (const card of [...cards].sort((a, b) => a.y - b.y)) laneOrder[card.lane].push(card.id);

  const laneLayouts: MapLaneLayout[] = (["used_by", "subject", "uses"] as const)
    .filter((lane) => lane === "subject" || byLane[lane].length > 0)
    .map((lane) => ({ lane, x: laneX[lane], w: laneW[lane], count: byLane[lane].length }));

  return {
    level,
    width: totalW,
    height: Math.max(bottom + pad, 240),
    lanes: laneLayouts,
    groups,
    cards,
    cardById,
    groupKeyOf,
    edges,
    neighbors,
    laneOrder,
    hiddenTests,
    hiddenContext,
    counts: { used_by: byLane.used_by.length, subject: byLane.subject.length, uses: byLane.uses.length },
  };

  function subjectCard(placed: Placed, mapLevel: MapLevel, cardY: number, key: string): MapCardLayout {
    const x = 0;
    const w = 0;
    const node = placed.node;
    if (mapLevel === "file") {
      const all = reviewableSymbols(node.symbols ?? []);
      const foldKey = "symbols::" + node.id;
      const foldable = all.length > LAYOUT.focusSymbolsShow + 2;
      const unfolded = options.expanded.has(foldKey);
      let symbols = all;
      if (foldable && !unfolded) {
        // Keep the rows links land on, then declarations over members, and
        // show them in file order so the card still reads top to bottom.
        const linked = new Set<string>();
        for (const edge of candidateEdges) {
          if (edge.source !== node.id && edge.target !== node.id) continue;
          for (const item of edge.symbols ?? []) linked.add(item.name);
          for (const evidence of edge.evidence ?? []) if (evidence.symbol) linked.add(evidence.symbol);
        }
        const keep = new Set(
          all
            .map((symbol, index) => ({
              index,
              score: (linked.has(symbol.label) || linked.has(symbol.qualified_name) ? 0 : 10) + symbolTier(symbol),
            }))
            .sort((a, b) => a.score - b.score || a.index - b.index)
            .slice(0, LAYOUT.focusSymbolsShow)
            .map((item) => item.index),
        );
        symbols = all.filter((_, index) => keep.has(index));
      }
      const rows = symbols.map((symbol, index) => ({
        symbol,
        y: cardY + LAYOUT.fileFocusHeader + index * LAYOUT.symbolRow + LAYOUT.symbolRow / 2,
      }));
      const listed = node.symbols?.length ?? 0;
      const unlisted = Math.max(0, (node.symbol_count ?? listed) - listed);
      const symbolFold = foldable
        ? { key: foldKey, hidden: all.length - symbols.length, expanded: unfolded, unlisted }
        : undefined;
      const h = LAYOUT.fileFocusHeader
        + symbols.length * LAYOUT.symbolRow
        + (symbolFold ? LAYOUT.moreRow + 4 : 0)
        + (symbols.length ? 8 : 0);
      return { id: node.id, node, lane: "subject", variant: "file-focus", groupKey: key, x, y: cardY, w, h, rows, symbolFold };
    }
    if (mapLevel === "symbol") {
      return { id: node.id, node, lane: "subject", variant: "symbol-focus", groupKey: key, x, y: cardY, w, h: LAYOUT.symbolFocus };
    }
    return { id: node.id, node, lane: "subject", variant: "subject", groupKey: key, x, y: cardY, w, h: LAYOUT.subjectCard };
  }
}

function matchRows(
  edge: MapEdge,
  rows: NonNullable<MapCardLayout["rows"]>,
): NonNullable<MapCardLayout["rows"]> {
  const names = new Set<string>();
  for (const item of edge.symbols ?? []) names.add(item.name);
  for (const evidence of edge.evidence ?? []) if (evidence.symbol) names.add(evidence.symbol);
  if (!names.size) return [];
  return rows.filter((row) => names.has(row.symbol.label) || names.has(row.symbol.qualified_name)).slice(0, 4);
}

// ---------------------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------------------

export interface Relationship<N extends MapNode = MapNode, E extends MapEdge = MapEdge> {
  edge: E;
  kind: EdgeKind;
  other: N | undefined;
  otherId: string;
}

/** Relationships of one node, split by direction relative to it. */
export function relationshipsOf<N extends MapNode, E extends MapEdge>(
  payload: MapPayload<N, E>,
  nodeId: string,
): { usedBy: Relationship<N, E>[]; uses: Relationship<N, E>[] } {
  const byId = new Map(payload.graph.nodes.map((node) => [node.id, node]));
  const usedBy: Relationship<N, E>[] = [];
  const uses: Relationship<N, E>[] = [];
  for (const edge of payload.graph.edges) {
    const { from, to } = visualEnds(edge);
    if (from !== nodeId && to !== nodeId) continue;
    const otherId = from === nodeId ? to : from;
    const row = { edge, kind: edgeKind(edge.kind), other: byId.get(otherId), otherId };
    if (to === nodeId) usedBy.push(row);
    else uses.push(row);
  }
  const order = (a: Relationship<N, E>, b: Relationship<N, E>) =>
    KIND_FACTOR[b.kind] - KIND_FACTOR[a.kind] || (b.edge.weight || 0) - (a.edge.weight || 0);
  return { usedBy: usedBy.sort(order), uses: uses.sort(order) };
}

function hasOutstandingWork(node: MapNode): boolean {
  const outstanding = node.review?.outstanding;
  return outstanding !== undefined ? outstanding > 0 : !isFullyReviewed(node);
}

/**
 * Where to look next: changed files with outstanding work, in the server's
 * canonical queue order (the same queue as the Reader's Next card). The
 * ranker's scalar rank only orders files the queue did not place.
 */
export function reviewQueue<N extends MapNode>(payload: MapPayload<N>, limit = 5): N[] {
  const placed = (node: MapNode) => {
    const position = node.review?.next_position ?? -1;
    return position >= 0 ? position : Number.MAX_SAFE_INTEGER;
  };
  return payload.graph.nodes
    .filter((node) => isChangedStatus(node) && hasOutstandingWork(node))
    .map((node, index) => ({ node, index }))
    .sort((a, b) => {
      const rankA = a.node.rank && a.node.rank > 0 ? a.node.rank : Number.MAX_SAFE_INTEGER;
      const rankB = b.node.rank && b.node.rank > 0 ? b.node.rank : Number.MAX_SAFE_INTEGER;
      return placed(a.node) - placed(b.node) || rankA - rankB || a.index - b.index;
    })
    .slice(0, limit)
    .map((item) => item.node);
}

export interface SearchHit<N extends MapNode = MapNode> {
  kind: "file" | "symbol";
  id: string;
  label: string;
  detail: string;
  node: N;
  symbol?: NonNullable<N["symbols"]>[number];
}

export function searchMap<N extends MapNode>(payload: MapPayload<N>, query: string, limit = 8): SearchHit<N>[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const hits: { hit: SearchHit<N>; score: number }[] = [];
  const score = (label: string, haystack: string) => {
    const lower = label.toLowerCase();
    if (lower === needle) return 0;
    if (lower.startsWith(needle)) return 1;
    if (lower.includes(needle)) return 2;
    return haystack.toLowerCase().includes(needle) ? 3 : -1;
  };
  for (const node of payload.graph.nodes) {
    const label = nodeName(node);
    const value = score(label, node.path + " " + (node.qualified_name ?? ""));
    if (value >= 0) {
      hits.push({
        hit: { kind: node.kind === "file" ? "file" : "symbol", id: node.id, label, detail: node.path, node },
        score: value + (isChangedStatus(node) ? 0 : 0.5),
      });
    }
    for (const symbol of node.symbols ?? []) {
      const symbolScore = score(symbol.label, symbol.qualified_name);
      if (symbolScore >= 0) {
        hits.push({
          hit: { kind: "symbol", id: symbol.id, label: symbol.label, detail: baseName(symbol.path) + ":" + symbol.line, node, symbol },
          score: symbolScore + 0.25,
        });
      }
    }
  }
  return hits
    .sort((a, b) => a.score - b.score || a.hit.label.localeCompare(b.hit.label))
    .slice(0, limit)
    .map((item) => item.hit);
}

/**
 * File focus derived from an overview payload, for servers that predate the
 * `file` map level. Only relationships already in the overview are known, so
 * the result is marked truncated when the overview itself was.
 */
export function localFileFocus<P extends MapPayload>(overview: P, nodeId: string): P | null {
  const focus = overview.graph.nodes.find((node) => node.id === nodeId);
  if (!focus) return null;
  const edges = overview.graph.edges.filter((edge) => edge.source === nodeId || edge.target === nodeId);
  const neighborIds = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  neighborIds.delete(nodeId);
  const nodes = [
    { ...focus, lane: "" as const },
    ...overview.graph.nodes
      .filter((node) => neighborIds.has(node.id))
      .map((node) => {
        const lane = edges.some((edge) => {
          const ends = visualEnds(edge);
          return ends.from === node.id && ends.to === nodeId;
        }) ? "used_by" as const : "uses" as const;
        return { ...node, lane };
      }),
  ];
  return {
    ...overview,
    level: "file",
    graph: {
      level: "file",
      focus: nodeId,
      nodes,
      edges,
      truncated: overview.graph.truncated,
      omitted_node_count: 0,
      reason: "",
    },
  } as P;
}
