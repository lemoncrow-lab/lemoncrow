// Column layout: the lane grammar generalized from three fixed lanes to N
// ordered columns, for a repository's module overview.
//
// Every arrow reads "depends on" and points right: a module sits left of the
// modules it uses. Dependency cycles are condensed into one group so the rest
// can be ordered; beyond a card budget, sibling modules fold into their parent
// folder. Pure and deterministic so it can be unit tested.

import { horizontalPath, arrowHead, ARROW_LEN } from "./mapModel";

export interface ColumnNode {
  id: string;
  label: string;
  /** Parent folder, for folding siblings together; "" at the top. */
  parent: string;
  files?: number;
  symbols?: number;
  changed?: number;
  purpose?: string;
  /** The purpose line came from a model, not the code. */
  purposeGenerated?: boolean;
}

export interface ColumnEdgeInput {
  source: string;
  target: string;
  weight: number;
  kinds?: Record<string, number>;
}

export interface ColumnOptions {
  width: number;
  /** Cards on the first frame; siblings fold into their parent beyond it. */
  maxCards: number;
  /** Fold ids the reader opened. */
  expanded: ReadonlySet<string>;
  maxColumns?: number;
}

export interface ColumnCard {
  id: string;
  label: string;
  /** Real modules this card stands for (one, or a fold's members). */
  members: string[];
  fold: boolean;
  column: number;
  cycle: string | null;
  files: number;
  symbols: number;
  changed: number;
  purpose: string;
  purposeGenerated?: boolean;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ColumnEdge {
  key: string;
  from: string;
  to: string;
  weight: number;
  kinds: Record<string, number>;
  d: string;
  arrow: string;
  width: number;
  /** Drawn at rest; otherwise only while an endpoint is active. */
  resting: boolean;
  labelX: number;
  labelY: number;
  /** The free gap beside the edge's left card: a label kept inside it never covers a card. */
  labelMin: number;
  labelMax: number;
}

export interface ColumnGroup {
  id: string;
  members: string[];
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ColumnLayout {
  index: number;
  x: number;
  w: number;
  count: number;
  unlinked: boolean;
}

export interface ColumnModel {
  width: number;
  height: number;
  columns: ColumnLayout[];
  cards: ColumnCard[];
  cardById: Map<string, ColumnCard>;
  /** Real module id -> the card showing it. */
  cardOf: Map<string, string>;
  edges: ColumnEdge[];
  cycles: ColumnGroup[];
  neighbors: Map<string, Set<string>>;
  /** Visual reading order per column, for keyboard navigation. */
  columnOrder: string[][];
  folded: number;
}

export const COLUMN_LAYOUT = {
  pad: 20,
  header: 40,
  card: 46,
  cardWithPurpose: 62,
  gap: 8,
  cycleHeader: 20,
  cyclePad: 6,
  maxCardW: 236,
  minCardW: 168,
  /** Narrowest card and gap, used only when the roomy minimums would make the map wider than its pane. */
  compactCardW: 132,
  compactGap: 36,
  restingEdges: 60,
} as const;

/** Smallest scale the map is drawn at to fit its pane; a map wider than that still scrolls sideways. */
const MIN_FIT_ZOOM = 0.7;

/** The scale that fits a map `mapWidth` wide into a pane `paneWidth` wide (1 when it fits or the pane is unmeasured). */
export function fitZoom(mapWidth: number, paneWidth: number): number {
  if (paneWidth <= 0 || mapWidth <= paneWidth) return 1;
  return Math.max(MIN_FIT_ZOOM, Math.floor((paneWidth / mapWidth) * 100) / 100);
}

const FOLD_PREFIX = "fold:";

/** The point at x on a `horizontalPath` curve from (x0, y0) to (x3, y3). */
function gapLabel(x0: number, y0: number, x3: number, y3: number, x: number): { labelX: number; labelY: number } {
  const length = x3 - x0;
  if (length <= 0) return { labelX: (x0 + x3) / 2, labelY: (y0 + y3) / 2 };
  // horizontalPath's control points: (x0 + dx, y0) and (x3 - dx, y3); x(t) rises monotonically with t.
  const dx = Math.max(24, length * 0.5);
  const xAt = (t: number) => x0 + 3 * (1 - t) ** 2 * t * dx + 3 * (1 - t) * t * t * (length - dx) + t ** 3 * length;
  const target = Math.min(Math.max(x, x0), x3);
  let lo = 0;
  let hi = 1;
  for (let step = 0; step < 24; step += 1) {
    const t = (lo + hi) / 2;
    if (xAt(t) < target) lo = t;
    else hi = t;
  }
  const t = (lo + hi) / 2;
  return { labelX: target, labelY: y0 + (y3 - y0) * (3 * t * t - 2 * t ** 3) };
}

export function foldId(parent: string): string {
  return FOLD_PREFIX + parent;
}

export function isFoldId(id: string): boolean {
  return id.startsWith(FOLD_PREFIX);
}

function baseLabel(path: string): string {
  if (!path) return "(repository root)";
  const slash = path.lastIndexOf("/");
  return slash >= 0 ? path.slice(slash + 1) : path;
}

function parentOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash > 0 ? path.slice(0, slash) : "";
}

/**
 * Which card shows each module. Siblings fold into their parent folder, the
 * largest sibling sets first, until the budget holds; a fold the reader opened
 * stays open.
 */
export function foldModules(nodes: readonly ColumnNode[], maxCards: number, expanded: ReadonlySet<string>): Map<string, string> {
  const shown = new Map<string, string>(nodes.map((node) => [node.id, node.id]));
  const parentOfCard = new Map<string, string>(nodes.map((node) => [node.id, node.parent]));
  let visible = new Set(shown.values());
  for (let round = 0; round < 8 && visible.size > maxCards; round += 1) {
    const siblings = new Map<string, string[]>();
    for (const card of visible) {
      const parent = parentOfCard.get(card) ?? "";
      if (expanded.has(foldId(parent))) continue;
      const list = siblings.get(parent) ?? [];
      list.push(card);
      siblings.set(parent, list);
    }
    const candidates = [...siblings]
      .filter(([parent, cards]) => cards.length >= 2 && parent !== "")
      .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
    if (!candidates.length) break;
    let progressed = false;
    for (const [parent, cards] of candidates) {
      if (visible.size <= maxCards) break;
      const fold = foldId(parent);
      const members = new Set(cards);
      for (const [module, card] of shown) if (members.has(card)) shown.set(module, fold);
      parentOfCard.set(fold, parentOf(parent));
      visible = new Set(shown.values());
      progressed = true;
    }
    if (!progressed) break;
  }
  return shown;
}

/** Strongly connected components (Tarjan), in a stable order. */
export function stronglyConnected(ids: readonly string[], out: Map<string, Set<string>>): string[][] {
  let index = 0;
  const indices = new Map<string, number>();
  const low = new Map<string, number>();
  const stack: string[] = [];
  const onStack = new Set<string>();
  const components: string[][] = [];
  const visit = (start: string) => {
    // Iterative DFS: module graphs can be deep enough to overflow recursion.
    const work: { id: string; next: string[]; i: number }[] = [];
    const enter = (id: string) => {
      indices.set(id, index);
      low.set(id, index);
      index += 1;
      stack.push(id);
      onStack.add(id);
      work.push({ id, next: [...(out.get(id) ?? [])].sort(), i: 0 });
    };
    enter(start);
    while (work.length) {
      const frame = work[work.length - 1];
      if (frame.i < frame.next.length) {
        const next = frame.next[frame.i];
        frame.i += 1;
        if (!indices.has(next)) enter(next);
        else if (onStack.has(next)) low.set(frame.id, Math.min(low.get(frame.id)!, indices.get(next)!));
        continue;
      }
      work.pop();
      if (work.length) {
        const parent = work[work.length - 1].id;
        low.set(parent, Math.min(low.get(parent)!, low.get(frame.id)!));
      }
      if (low.get(frame.id) === indices.get(frame.id)) {
        const component: string[] = [];
        let member: string | undefined;
        do {
          member = stack.pop()!;
          onStack.delete(member);
          component.push(member);
        } while (member !== frame.id);
        components.push(component.sort());
      }
    }
  };
  for (const id of [...ids].sort()) if (!indices.has(id)) visit(id);
  return components;
}

export function buildColumnModel(
  nodes: readonly ColumnNode[],
  inputEdges: readonly ColumnEdgeInput[],
  options: ColumnOptions,
): ColumnModel {
  const L = COLUMN_LAYOUT;
  const maxColumns = Math.max(2, options.maxColumns ?? 6);
  const byId = new Map(nodes.map((node) => [node.id, node]));

  // 1. Cards: modules, or folds standing for several siblings.
  const cardOf = foldModules(nodes, options.maxCards, options.expanded);
  const cardMembers = new Map<string, string[]>();
  for (const node of nodes) {
    const card = cardOf.get(node.id)!;
    const list = cardMembers.get(card) ?? [];
    list.push(node.id);
    cardMembers.set(card, list);
  }
  const cardIds = [...cardMembers.keys()].sort();

  // 2. Card-level edges, weights summed; self links inside a fold dropped.
  const edgeMap = new Map<string, { from: string; to: string; weight: number; kinds: Record<string, number> }>();
  for (const edge of inputEdges) {
    const from = cardOf.get(edge.source);
    const to = cardOf.get(edge.target);
    if (!from || !to || from === to) continue;
    const key = from + "\u0000" + to;
    const row = edgeMap.get(key) ?? { from, to, weight: 0, kinds: {} };
    row.weight += Math.max(1, edge.weight);
    for (const [kind, count] of Object.entries(edge.kinds ?? {})) row.kinds[kind] = (row.kinds[kind] ?? 0) + count;
    edgeMap.set(key, row);
  }
  const out = new Map<string, Set<string>>();
  const linked = new Set<string>();
  for (const edge of edgeMap.values()) {
    const set = out.get(edge.from) ?? new Set<string>();
    set.add(edge.to);
    out.set(edge.from, set);
    linked.add(edge.from);
    linked.add(edge.to);
  }

  // 3. Condense cycles, then layer: a component sits one column right of the
  //    rightmost component that depends on it.
  const components = stronglyConnected(cardIds, out);
  const componentOf = new Map<string, number>();
  components.forEach((members, index) => members.forEach((id) => componentOf.set(id, index)));
  const componentOut = components.map(() => new Set<number>());
  const indegree = components.map(() => 0);
  for (const edge of edgeMap.values()) {
    const a = componentOf.get(edge.from)!;
    const b = componentOf.get(edge.to)!;
    if (a === b || componentOut[a].has(b)) continue;
    componentOut[a].add(b);
    indegree[b] += 1;
  }
  const depth = components.map(() => 0);
  const queue = components.map((_, index) => index).filter((index) => indegree[index] === 0);
  const remaining = [...indegree];
  for (let head = 0; head < queue.length; head += 1) {
    const current = queue[head];
    for (const next of componentOut[current]) {
      depth[next] = Math.max(depth[next], depth[current] + 1);
      remaining[next] -= 1;
      if (remaining[next] === 0) queue.push(next);
    }
  }
  const deepest = Math.max(0, ...depth);
  const linkedColumns = Math.min(maxColumns, deepest + 1);
  const columnOfComponent = depth.map((value) => (deepest === 0 ? 0 : Math.round((value * (linkedColumns - 1)) / deepest)));
  const hasUnlinked = cardIds.some((id) => !linked.has(id));
  const unlinkedColumn = linkedColumns;
  const columnOf = new Map<string, number>();
  for (const id of cardIds) {
    columnOf.set(id, linked.has(id) ? columnOfComponent[componentOf.get(id)!] : unlinkedColumn);
  }
  const columnCount = linkedColumns + (hasUnlinked ? 1 : 0);

  // 4. Order inside columns: two barycenter sweeps against linked neighbors,
  //    cycle members kept together.
  const weightOf = (id: string) => {
    let total = 0;
    for (const edge of edgeMap.values()) if (edge.from === id || edge.to === id) total += edge.weight;
    return total;
  };
  const strength = new Map(cardIds.map((id) => [id, weightOf(id)]));
  const columns: string[][] = Array.from({ length: columnCount }, () => []);
  for (const id of cardIds) columns[columnOf.get(id)!].push(id);
  const position = new Map<string, number>();
  const unitOf = (id: string) => {
    const members = components[componentOf.get(id)!];
    return members.length > 1 ? "cycle:" + members[0] : id;
  };
  const orderColumn = (column: string[], reference: (id: string) => number) => {
    const units = new Map<string, string[]>();
    for (const id of column) {
      const unit = unitOf(id);
      const list = units.get(unit) ?? [];
      list.push(id);
      units.set(unit, list);
    }
    const scored = [...units].map(([unit, members]) => {
      const values = members.map(reference).filter(Number.isFinite);
      const bary = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : Number.POSITIVE_INFINITY;
      const weight = members.reduce((sum, id) => sum + (strength.get(id) ?? 0), 0);
      return { unit, members: members.sort((a, b) => (strength.get(b) ?? 0) - (strength.get(a) ?? 0) || a.localeCompare(b)), bary, weight };
    });
    scored.sort((a, b) => a.bary - b.bary || b.weight - a.weight || a.unit.localeCompare(b.unit));
    return scored.flatMap((item) => item.members);
  };
  const neighborsOf = new Map<string, string[]>();
  for (const edge of edgeMap.values()) {
    neighborsOf.set(edge.from, [...(neighborsOf.get(edge.from) ?? []), edge.to]);
    neighborsOf.set(edge.to, [...(neighborsOf.get(edge.to) ?? []), edge.from]);
  }
  const referenceFrom = (id: string) => {
    const values = (neighborsOf.get(id) ?? []).map((other) => position.get(other)).filter((value): value is number => value !== undefined);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : Number.POSITIVE_INFINITY;
  };
  columns.forEach((column, index) => {
    columns[index] = orderColumn(column, () => Number.POSITIVE_INFINITY);
    columns[index].forEach((id, rank) => position.set(id, rank));
  });
  for (let sweep = 0; sweep < 2; sweep += 1) {
    for (let index = 1; index < linkedColumns; index += 1) {
      columns[index] = orderColumn(columns[index], referenceFrom);
      columns[index].forEach((id, rank) => position.set(id, rank));
    }
    for (let index = linkedColumns - 2; index >= 0; index -= 1) {
      columns[index] = orderColumn(columns[index], referenceFrom);
      columns[index].forEach((id, rank) => position.set(id, rank));
    }
  }

  // 5. Geometry.
  const width = Math.max(options.width, 360);
  // Cards and gaps tighten before the map outgrows its pane; past the compact minimums ColumnCanvas draws it smaller.
  const roomy = L.pad * 2 + L.minCardW * columnCount + 52 * Math.max(0, columnCount - 1) <= width;
  const gapMin = roomy ? 52 : L.compactGap;
  const minCardW = roomy ? L.minCardW : L.compactCardW;
  const cardW = Math.max(minCardW, Math.min(L.maxCardW, (width - L.pad * 2 - gapMin * (columnCount - 1)) / Math.max(1, columnCount)));
  const gap = columnCount > 1 ? Math.max(gapMin, (width - L.pad * 2 - cardW * columnCount) / (columnCount - 1)) : 0;
  const totalW = Math.max(width, L.pad * 2 + cardW * columnCount + gap * Math.max(0, columnCount - 1));
  const top = L.header + 10;
  const cards: ColumnCard[] = [];
  const cardById = new Map<string, ColumnCard>();
  const cycles: ColumnGroup[] = [];
  let bottom = top;
  columns.forEach((column, index) => {
    const x = L.pad + index * (cardW + gap);
    let y = top;
    let openCycle: { unit: string; group: ColumnGroup } | null = null;
    for (const id of column) {
      const unit = unitOf(id);
      if (openCycle && openCycle.unit !== unit) {
        openCycle.group.h = y - openCycle.group.y + L.cyclePad - L.gap;
        y += L.cyclePad * 2;
        openCycle = null;
      }
      if (unit !== id && !openCycle) {
        const group: ColumnGroup = { id: unit, members: [], x: x - L.cyclePad, y, w: cardW + L.cyclePad * 2, h: 0 };
        cycles.push(group);
        openCycle = { unit, group };
        y += L.cycleHeader;
      }
      const members = cardMembers.get(id) ?? [id];
      const fold = isFoldId(id);
      const real = members.map((member) => byId.get(member)).filter((node): node is ColumnNode => Boolean(node));
      const purpose = fold ? "" : real[0]?.purpose ?? "";
      const h = purpose ? L.cardWithPurpose : L.card;
      const card: ColumnCard = {
        id,
        label: fold ? baseLabel(id.slice(FOLD_PREFIX.length)) : real[0]?.label ?? baseLabel(id),
        members,
        fold,
        column: index,
        cycle: unit !== id ? unit : null,
        files: real.reduce((sum, node) => sum + (node.files ?? 0), 0),
        symbols: real.reduce((sum, node) => sum + (node.symbols ?? 0), 0),
        changed: real.reduce((sum, node) => sum + (node.changed ?? 0), 0),
        purpose,
        ...(purpose && real[0]?.purposeGenerated ? { purposeGenerated: true } : {}),
        x,
        y,
        w: cardW,
        h,
      };
      if (openCycle) openCycle.group.members.push(id);
      cards.push(card);
      cardById.set(id, card);
      y += h + L.gap;
    }
    if (openCycle) {
      openCycle.group.h = y - openCycle.group.y + L.cyclePad - L.gap;
      y += L.cyclePad * 2;
    }
    bottom = Math.max(bottom, y);
  });

  // 6. Edges. Ports spread along each card side in the order of their far
  //    ends; the strongest links rest, the rest appear on hover.
  const restingKeys = new Set(
    [...edgeMap.values()]
      .sort((a, b) => b.weight - a.weight || a.from.localeCompare(b.from) || a.to.localeCompare(b.to))
      .slice(0, L.restingEdges)
      .map((edge) => edge.from + "\u0000" + edge.to),
  );
  interface Port { card: ColumnCard; side: "left" | "right"; y: number; farY: number }
  const middle = (card: ColumnCard) => card.y + card.h / 2;
  const pending: { key: string; edge: { from: string; to: string; weight: number; kinds: Record<string, number> }; a: Port; b: Port; sameColumn: boolean }[] = [];
  for (const [key, edge] of edgeMap) {
    const a = cardById.get(edge.from);
    const b = cardById.get(edge.to);
    if (!a || !b) continue;
    const sameColumn = a.column === b.column;
    const forward = a.column < b.column;
    pending.push({
      key,
      edge,
      sameColumn,
      a: { card: a, side: sameColumn || forward ? "right" : "left", y: middle(a), farY: middle(b) },
      b: { card: b, side: sameColumn ? "right" : forward ? "left" : "right", y: middle(b), farY: middle(a) },
    });
  }
  const sides = new Map<string, Port[]>();
  for (const item of pending) {
    for (const port of [item.a, item.b]) {
      const sideKey = port.card.id + ":" + port.side;
      const list = sides.get(sideKey) ?? [];
      list.push(port);
      sides.set(sideKey, list);
    }
  }
  for (const ports of sides.values()) {
    if (ports.length < 2) continue;
    const card = ports[0].card;
    const inset = Math.min(8, card.h * 0.2);
    ports.sort((p, q) => p.farY - q.farY);
    ports.forEach((port, rank) => {
      port.y = card.y + inset + ((card.h - inset * 2) * (rank + 1)) / (ports.length + 1);
    });
  }
  const edges: ColumnEdge[] = [];
  const neighbors = new Map<string, Set<string>>();
  const connect = (a: string, b: string) => {
    neighbors.set(a, (neighbors.get(a) ?? new Set<string>()).add(b));
    neighbors.set(b, (neighbors.get(b) ?? new Set<string>()).add(a));
  };
  const maxWeight = Math.max(1, ...[...edgeMap.values()].map((edge) => edge.weight));
  for (const item of pending) {
    connect(item.edge.from, item.edge.to);
    const strokeWidth = 1 + 0.8 * Math.sqrt(item.edge.weight / maxWeight);
    const resting = restingKeys.has(item.key);
    if (item.sameColumn) {
      const xr = item.a.card.x + item.a.card.w;
      const y1 = item.a.y;
      const y2 = item.b.y;
      const bulge = Math.min(gap - 10, 14 + Math.abs(y2 - y1) * 0.18);
      edges.push({
        key: item.key,
        from: item.edge.from,
        to: item.edge.to,
        weight: item.edge.weight,
        kinds: item.edge.kinds,
        d: `M${xr},${y1} C${xr + bulge},${y1} ${xr + bulge},${y2} ${xr + ARROW_LEN},${y2}`,
        arrow: arrowHead(xr + 0.5, y2, "left"),
        width: strokeWidth,
        resting,
        labelX: xr + bulge * 0.75,
        labelY: (y1 + y2) / 2,
        labelMin: xr + 2,
        labelMax: xr + gap - 2,
      });
      continue;
    }
    const forward = item.a.card.column < item.b.card.column;
    const left = forward ? item.a : item.b;
    const right = forward ? item.b : item.a;
    const x1 = left.card.x + left.card.w;
    const x2 = right.card.x;
    const startX = forward ? x1 : x1 + ARROW_LEN;
    const endX = forward ? x2 - ARROW_LEN : x2;
    edges.push({
      key: item.key,
      from: item.edge.from,
      to: item.edge.to,
      weight: item.edge.weight,
      kinds: item.edge.kinds,
      d: horizontalPath(startX, startX, left.y, endX, right.y),
      arrow: arrowHead(forward ? x2 : x1, forward ? right.y : left.y, forward ? "right" : "left"),
      width: strokeWidth,
      resting,
      // The label sits in the first gap the edge crosses, on the curve there, so
      // an edge that spans columns doesn't put its label on a card in between.
      ...gapLabel(startX, left.y, endX, right.y, x1 + gap / 2),
      labelMin: x1 + 2,
      labelMax: x1 + gap - 2,
    });
  }
  edges.sort((a, b) => a.width - b.width);

  const columnLayouts: ColumnLayout[] = columns.map((column, index) => ({
    index,
    x: L.pad + index * (cardW + gap),
    w: cardW,
    count: column.length,
    unlinked: hasUnlinked && index === unlinkedColumn,
  }));
  const columnOrder = columns.map((column) => [...column].sort((a, b) => cardById.get(a)!.y - cardById.get(b)!.y));
  const folded = nodes.length - [...cardOf.entries()].filter(([module, card]) => module === card).length;

  return {
    width: totalW,
    height: Math.max(bottom + L.pad, 240),
    columns: columnLayouts,
    cards,
    cardById,
    cardOf,
    edges,
    cycles: cycles.filter((group) => group.members.length > 1),
    neighbors,
    columnOrder,
    folded,
  };
}
