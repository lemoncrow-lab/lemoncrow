/**
 * The architecture layout: lanes of cards and orthogonal arrows, computed from the document alone.
 *
 * - Lanes are fixed-width columns in document order, so a lane's x never depends on what is in another lane.
 * - Inside a lane, a card's row comes from its rank (the longest path over arrows inside that lane, cycles broken in
 *   document order) and then document order. Arrows between lanes never move a card, so adding a node to one lane
 *   cannot move cards in another.
 * - Arrows leave and enter cards on spread ports, run vertically in the gutters (one channel per overlapping run) and
 *   cross intermediate lanes only through gaps between cards.
 * - Labels are pills placed on the longest free stretch of their arrow, never over a card or another label.
 * - Text is measured from a width table and every coordinate is rounded, so the output is byte-identical across runs.
 */
import { boundsOf, inflate, overlapArea, overlaps, roundedPath, simplify, union, withArrowhead, type Box, type Point } from "./geometry";
import { fitText, textWidth } from "./textWidth";
import type { XRayEdge, XRayLane, XRayNode } from "./xrayGraphTypes";

export const ARCH = {
  pad: 24,
  laneW: 296,
  gutter: 112,
  cardW: 248,
  cardH: 60,
  rowGap: 44,
  headerH: 56,
  laneBottom: 28,
  slot: 8,
  innerSlot: 6,
  labelH: 20,
  labelPadX: 8,
  labelSize: 11,
  labelMax: 150,
  aiTagW: 22,
  corner: 10,
  laneTitleSize: 10,
  laneTitleTracking: 1.2,
} as const;

export interface ArchInput {
  lanes: readonly XRayLane[];
  nodes: readonly XRayNode[];
  edges: readonly XRayEdge[];
}

export interface ArchLane {
  id: string;
  lane: XRayLane;
  box: Box;
  title: string;
  platform: string;
}

export interface ArchCard {
  id: string;
  node: XRayNode;
  lane: string;
  rank: number;
  row: number;
  box: Box;
}

export interface ArchLabel {
  box: Box;
  /** The label as drawn, cut to fit; `full` is the label as written. */
  text: string;
  full: string;
  ai: boolean;
}

export type ArchRoute = "adjacent" | "lane-down" | "lane-up" | "cross" | "self";

export interface ArchArrow {
  id: string;
  edge: XRayEdge;
  route: ArchRoute;
  /** The route as orthogonal corners, ending at the arrow's tip. */
  points: Point[];
  /** The line (ending under the head) and the head. */
  d: string;
  head: string;
  label: ArchLabel | null;
  hero: boolean;
  /** Dropped from the lane's ranking to break a cycle. */
  back: boolean;
}

export interface Atlas {
  lanes: Record<string, Box>;
  nodes: Record<string, Box>;
  edges: Record<string, Box>;
  /** By `stepKey(flow, step)`. */
  steps: Record<string, Box>;
}

export interface ArchLayout {
  width: number;
  height: number;
  lanes: ArchLane[];
  cards: ArchCard[];
  arrows: ArchArrow[];
  atlas: Atlas;
}

/**
 * Ranks one lane: the longest path to each node over the lane's own arrows. Arrows are taken in document order and
 * one that would close a cycle is set aside as a back arrow, so the ranking is a pure function of document order.
 */
export function rankLane(nodeIds: readonly string[], edges: readonly { from: string; to: string }[]): { rank: Map<string, number>; back: Set<number> } {
  const ids = new Set(nodeIds);
  const order = new Map(nodeIds.map((id, index) => [id, index]));
  const out = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  const back = new Set<number>();
  const reaches = (from: string, to: string): boolean => {
    const seen = new Set<string>();
    const stack = [from];
    while (stack.length) {
      const at = stack.pop()!;
      if (at === to) return true;
      if (seen.has(at)) continue;
      seen.add(at);
      for (const next of out.get(at) ?? []) stack.push(next);
    }
    return false;
  };
  edges.forEach((edge, index) => {
    if (!ids.has(edge.from) || !ids.has(edge.to)) return;
    if (edge.from === edge.to || reaches(edge.to, edge.from)) {
      back.add(index);
      return;
    }
    out.get(edge.from)!.push(edge.to);
  });
  const indegree = new Map(nodeIds.map((id) => [id, 0]));
  for (const targets of out.values()) for (const to of targets) indegree.set(to, (indegree.get(to) ?? 0) + 1);
  const rank = new Map(nodeIds.map((id) => [id, 0]));
  // Kahn's order, always taking the earliest ready node in document order.
  const ready = nodeIds.filter((id) => indegree.get(id) === 0);
  while (ready.length) {
    ready.sort((a, b) => order.get(a)! - order.get(b)!);
    const at = ready.shift()!;
    for (const to of out.get(at) ?? []) {
      rank.set(to, Math.max(rank.get(to)!, rank.get(at)! + 1));
      indegree.set(to, indegree.get(to)! - 1);
      if (indegree.get(to) === 0) ready.push(to);
    }
  }
  return { rank, back };
}

type Side = "left" | "right" | "top" | "bottom";

interface Plan {
  index: number;
  edge: XRayEdge;
  from: ArchCard;
  to: ArchCard;
  route: ArchRoute;
  fromSide: Side;
  toSide: Side;
  back: boolean;
}

/** Slot offsets around a channel's centre: 0, +1, -1, +2, -2, ... */
function slotOffset(slot: number): number {
  return slot === 0 ? 0 : slot % 2 === 1 ? (slot + 1) / 2 : -slot / 2;
}

/**
 * Greedy interval colouring: each vertical run gets the lowest slot not used by a run it overlaps. Runs are taken in
 * order of their top, then bottom, then document order.
 */
function assignSlots(runs: { key: string; y1: number; y2: number; order: number }[], maxSlots: number): Map<string, number> {
  const sorted = [...runs].sort((a, b) => a.y1 - b.y1 || a.y2 - b.y2 || a.order - b.order);
  const taken: { y1: number; y2: number; slot: number }[] = [];
  const out = new Map<string, number>();
  for (const run of sorted) {
    const busy = new Set(taken.filter((t) => t.y1 < run.y2 + 6 && run.y1 < t.y2 + 6).map((t) => t.slot));
    let slot = 0;
    while (busy.has(slot) && slot < maxSlots - 1) slot += 1;
    // lc-debt: past maxSlots overlapping runs share the last channel; a wider gutter (which would move lanes) is the upgrade.
    taken.push({ y1: run.y1, y2: run.y2, slot });
    out.set(run.key, slot);
  }
  return out;
}

export function layoutArchitecture(input: ArchInput): ArchLayout {
  const A = ARCH;
  const laneIndex = new Map(input.lanes.map((lane, index) => [lane.id, index]));
  const laneX = (index: number) => A.pad + index * (A.laneW + A.gutter);

  // Cards: rows per lane from that lane's nodes and arrows only.
  const nodes = input.nodes.filter((node) => laneIndex.has(node.lane));
  const nodeOrder = new Map(nodes.map((node, index) => [node.id, index]));
  const edges = input.edges.filter((edge) => nodeOrder.has(edge.from) && nodeOrder.has(edge.to));
  const cards = new Map<string, ArchCard>();
  const backEdges = new Set<string>();
  const laneBottom = new Map<string, number>();
  for (const lane of input.lanes) {
    const members = nodes.filter((node) => node.lane === lane.id);
    const inside = edges.filter((edge) => members.some((n) => n.id === edge.from) && members.some((n) => n.id === edge.to));
    const { rank, back } = rankLane(members.map((n) => n.id), inside);
    back.forEach((index) => backEdges.add(inside[index].id));
    const rows = [...members].sort((a, b) => rank.get(a.id)! - rank.get(b.id)! || nodeOrder.get(a.id)! - nodeOrder.get(b.id)!);
    const x = laneX(laneIndex.get(lane.id)!) + (A.laneW - A.cardW) / 2;
    rows.forEach((node, row) => {
      const y = A.pad + A.headerH + row * (A.cardH + A.rowGap);
      cards.set(node.id, { id: node.id, node, lane: lane.id, rank: rank.get(node.id)!, row, box: { x, y, w: A.cardW, h: A.cardH } });
    });
    laneBottom.set(lane.id, rows.length ? A.pad + A.headerH + rows.length * (A.cardH + A.rowGap) - A.rowGap : A.pad + A.headerH);
  }

  // Which way each arrow goes, and the side of each card it uses.
  const plans: Plan[] = edges.map((edge, index) => {
    const from = cards.get(edge.from)!;
    const to = cards.get(edge.to)!;
    const back = backEdges.has(edge.id);
    if (from.id === to.id) return { index, edge, from, to, route: "self", fromSide: "right", toSide: "right", back };
    if (from.lane === to.lane) {
      if (!back && to.row === from.row + 1) return { index, edge, from, to, route: "adjacent", fromSide: "bottom", toSide: "top", back };
      if (to.row > from.row) return { index, edge, from, to, route: "lane-down", fromSide: "right", toSide: "right", back };
      return { index, edge, from, to, route: "lane-up", fromSide: "left", toSide: "left", back };
    }
    const rightward = laneIndex.get(to.lane)! > laneIndex.get(from.lane)!;
    return { index, edge, from, to, route: "cross", fromSide: rightward ? "right" : "left", toSide: rightward ? "left" : "right", back };
  });

  // Ports: the ends on one side of a card, spread and sorted by where the other end is.
  const portKey = (card: string, side: Side) => `${card}|${side}`;
  const sideEnds = new Map<string, { plan: number; end: "from" | "to"; other: number }[]>();
  const addEnd = (card: ArchCard, side: Side, plan: Plan, end: "from" | "to") => {
    const other = end === "from" ? plan.to : plan.from;
    const coordinate = side === "left" || side === "right" ? other.box.y + other.box.h / 2 : other.box.x + other.box.w / 2;
    const key = portKey(card.id, side);
    if (!sideEnds.has(key)) sideEnds.set(key, []);
    sideEnds.get(key)!.push({ plan: plan.index, end, other: coordinate });
  };
  for (const plan of plans) {
    if (plan.route === "self") continue;
    addEnd(plan.from, plan.fromSide, plan, "from");
    addEnd(plan.to, plan.toSide, plan, "to");
  }
  const ports = new Map<string, Point>();
  for (const [key, ends] of sideEnds) {
    const [cardId, side] = key.split("|") as [string, Side];
    const box = cards.get(cardId)!.box;
    ends.sort((a, b) => a.other - b.other || a.plan - b.plan || (a.end === "from" ? -1 : 1));
    const n = ends.length;
    ends.forEach((end, k) => {
      let point: Point;
      if (side === "left" || side === "right") {
        const spacing = n > 1 ? Math.min(12, (box.h - 20) / (n - 1)) : 0;
        const y = Math.round(box.y + box.h / 2 + (k - (n - 1) / 2) * spacing);
        point = { x: side === "left" ? box.x : box.x + box.w, y };
      } else {
        const spacing = n > 1 ? Math.min(16, (box.w - 40) / (n - 1)) : 0;
        const x = Math.round(box.x + box.w / 2 + (k - (n - 1) / 2) * spacing);
        point = { x, y: side === "top" ? box.y : box.y + box.h };
      }
      ports.set(`${end.plan}|${end.end}`, point);
    });
  }

  // Corridors: horizontal runs through the lanes between two far lanes, only where no card is.
  const lanesById = new Map(input.lanes.map((lane) => [lane.id, lane]));
  const blockedIn = (laneIdx: number): [number, number][] => {
    const lane = input.lanes[laneIdx];
    const blocks: [number, number][] = [[A.pad, A.pad + 34]];
    for (const card of cards.values()) if (card.lane === lane.id) blocks.push([card.box.y - 10, card.box.y + card.box.h + 10]);
    return blocks;
  };
  const freeAcross = (fromIdx: number, toIdx: number, y: number): boolean => {
    for (let i = Math.min(fromIdx, toIdx) + 1; i < Math.max(fromIdx, toIdx); i += 1) {
      if (blockedIn(i).some(([lo, hi]) => y > lo && y < hi)) return false;
    }
    return true;
  };
  const usedCorridors: { y: number; x1: number; x2: number }[] = [];
  const corridorY = (fromIdx: number, toIdx: number, y1: number, y2: number, x1: number, x2: number): number => {
    const blocks: [number, number][] = [];
    for (let i = Math.min(fromIdx, toIdx) + 1; i < Math.max(fromIdx, toIdx); i += 1) blocks.push(...blockedIn(i));
    blocks.sort((a, b) => a[0] - b[0]);
    const free: [number, number][] = [];
    let cursor = A.pad + 34;
    for (const [lo, hi] of blocks) {
      if (lo > cursor) free.push([cursor, lo]);
      cursor = Math.max(cursor, hi);
    }
    free.push([cursor, Infinity]);
    let best = { y: cursor + 16, cost: Infinity };
    for (const [lo, hi] of free) {
      const centre = hi === Infinity ? lo + 16 : Math.round((lo + hi) / 2);
      const cost = Math.abs(centre - y1) + Math.abs(centre - y2);
      if (cost < best.cost) best = { y: centre, cost };
    }
    // Two corridors in one gap sit a few pixels apart.
    let y = best.y;
    const left = Math.min(x1, x2);
    const right = Math.max(x1, x2);
    for (let attempt = 1; attempt <= 4; attempt += 1) {
      const clash = usedCorridors.some((c) => Math.abs(c.y - y) < 5 && c.x1 < right && left < c.x2);
      if (!clash) break;
      y = best.y + slotOffset(attempt) * 6;
    }
    usedCorridors.push({ y, x1: left, x2: right });
    return y;
  };

  // Route templates; vertical runs in gutters and lane margins get their channel afterwards.
  type Run = { key: string; channel: string; y1: number; y2: number; order: number };
  const runs: Run[] = [];
  type Template = (x: (key: string) => number) => Point[];
  const templates = new Map<number, Template>();
  const gutterCentre = (gutter: number) => laneX(gutter) + A.laneW + A.gutter / 2;
  for (const plan of plans) {
    const { from, to } = plan;
    const sp = ports.get(`${plan.index}|from`);
    const tp = ports.get(`${plan.index}|to`);
    if (plan.route === "self") {
      const right = from.box.x + from.box.w;
      const midY = from.box.y + from.box.h / 2;
      templates.set(plan.index, () => [
        { x: right - 24, y: from.box.y },
        { x: right - 24, y: from.box.y - 14 },
        { x: right + 14, y: from.box.y - 14 },
        { x: right + 14, y: midY },
        { x: right, y: midY },
      ]);
      continue;
    }
    if (!sp || !tp) continue;
    if (plan.route === "adjacent") {
      templates.set(plan.index, () => (sp.x === tp.x ? [sp, tp] : [sp, { x: sp.x, y: (sp.y + tp.y) / 2 }, { x: tp.x, y: (sp.y + tp.y) / 2 }, tp]));
      continue;
    }
    if (plan.route === "lane-down" || plan.route === "lane-up") {
      const channel = `${from.lane}|${plan.route === "lane-down" ? "right" : "left"}`;
      const key = `${plan.index}|inner`;
      runs.push({ key, channel, y1: Math.min(sp.y, tp.y), y2: Math.max(sp.y, tp.y), order: plan.index });
      templates.set(plan.index, (x) => [sp, { x: x(key), y: sp.y }, { x: x(key), y: tp.y }, tp]);
      continue;
    }
    const fromIdx = laneIndex.get(from.lane)!;
    const toIdx = laneIndex.get(to.lane)!;
    const rightward = toIdx > fromIdx;
    const g1 = rightward ? fromIdx : fromIdx - 1;
    const g2 = rightward ? toIdx - 1 : toIdx;
    const run = (gutter: number, tag: string, y1: number, y2: number) => {
      const key = `${plan.index}|${tag}`;
      runs.push({ key, channel: `g${gutter}`, y1: Math.min(y1, y2), y2: Math.max(y1, y2), order: plan.index });
      return key;
    };
    if (sp.y === tp.y && freeAcross(fromIdx, toIdx, sp.y)) {
      templates.set(plan.index, () => [sp, tp]);
    } else if (Math.abs(toIdx - fromIdx) === 1) {
      const key = run(g1, "g", sp.y, tp.y);
      templates.set(plan.index, (x) => [sp, { x: x(key), y: sp.y }, { x: x(key), y: tp.y }, tp]);
    } else if (freeAcross(fromIdx, toIdx, sp.y)) {
      const key = run(g2, "g2", sp.y, tp.y);
      templates.set(plan.index, (x) => [sp, { x: x(key), y: sp.y }, { x: x(key), y: tp.y }, tp]);
    } else if (freeAcross(fromIdx, toIdx, tp.y)) {
      const key = run(g1, "g1", sp.y, tp.y);
      templates.set(plan.index, (x) => [sp, { x: x(key), y: sp.y }, { x: x(key), y: tp.y }, tp]);
    } else {
      const cy = corridorY(fromIdx, toIdx, sp.y, tp.y, gutterCentre(g1), gutterCentre(g2));
      const k1 = run(g1, "g1", sp.y, cy);
      const k2 = run(g2, "g2", cy, tp.y);
      templates.set(plan.index, (x) => [sp, { x: x(k1), y: sp.y }, { x: x(k1), y: cy }, { x: x(k2), y: cy }, { x: x(k2), y: tp.y }, tp]);
    }
  }

  // Channels: slots per gutter or lane margin, in a fixed order.
  const channelX = new Map<string, number>();
  const byChannel = new Map<string, Run[]>();
  for (const run of runs) {
    if (!byChannel.has(run.channel)) byChannel.set(run.channel, []);
    byChannel.get(run.channel)!.push(run);
  }
  const gutterSlots = 2 * Math.floor((A.gutter / 2 - 14) / A.slot) + 1;
  for (const [channel, list] of byChannel) {
    if (channel.startsWith("g")) {
      const centre = gutterCentre(Number(channel.slice(1)));
      for (const [key, slot] of assignSlots(list, gutterSlots)) channelX.set(key, Math.round(centre + slotOffset(slot) * A.slot));
    } else {
      const [laneId, side] = channel.split("|");
      const x0 = laneX(laneIndex.get(laneId)!) + (A.laneW - A.cardW) / 2;
      for (const [key, slot] of assignSlots(list, 3)) {
        channelX.set(key, side === "right" ? x0 + A.cardW + 8 + slot * A.innerSlot : x0 - 8 - slot * A.innerSlot);
      }
    }
  }

  // Labels: hero first, then document order; each on the first free spot along its own route, preferring spots that
  // no other arrow runs through.
  // A card's pills (delta, badges, AI) sit on its top border, so a card keeps 12px clear above it.
  const cardBoxes = [...cards.values()].map(({ box }) => ({ x: box.x - 4, y: box.y - 12, w: box.w + 8, h: box.h + 16 }));
  const laneTitleBoxes = input.lanes.map((_lane, index) => ({ x: laneX(index) + 8, y: A.pad + 8, w: A.laneW - 16, h: 26 }));
  const routes = new Map<number, Point[]>();
  for (const plan of plans) {
    const template = templates.get(plan.index);
    if (template) routes.set(plan.index, simplify(template((key) => channelX.get(key) ?? 0).map((p) => ({ x: Math.round(p.x), y: Math.round(p.y) }))));
  }
  const placed: Box[] = [];
  const arrowsByIndex = new Map<number, ArchArrow>();
  const labelOrder = [...plans].sort((a, b) => Number(Boolean(b.edge.hero)) - Number(Boolean(a.edge.hero)) || a.index - b.index);
  for (const plan of labelOrder) {
    const raw = routes.get(plan.index);
    if (!raw) continue;
    const { points: line, head } = withArrowhead(raw);
    const others: [Point, Point][] = [];
    for (const [index, route] of routes) if (index !== plan.index) for (let i = 1; i < route.length; i += 1) others.push([route[i - 1], route[i]]);
    const label = placeLabel(plan.edge, raw, cardBoxes, laneTitleBoxes, placed, others);
    if (label) placed.push(label.box);
    arrowsByIndex.set(plan.index, {
      id: plan.edge.id,
      edge: plan.edge,
      route: plan.route,
      points: raw,
      d: roundedPath(line, A.corner),
      head,
      label,
      hero: Boolean(plan.edge.hero),
      back: plan.back,
    });
  }
  const arrows = plans.map((plan) => arrowsByIndex.get(plan.index)).filter((arrow): arrow is ArchArrow => Boolean(arrow));

  // Bounds: every lane panel runs the full height.
  const laneCount = Math.max(1, input.lanes.length);
  const width = A.pad * 2 + laneCount * A.laneW + (laneCount - 1) * A.gutter;
  let bottom = Math.max(A.pad + A.headerH + A.cardH, ...laneBottom.values());
  for (const arrow of arrows) {
    for (const point of arrow.points) bottom = Math.max(bottom, point.y);
    if (arrow.label) bottom = Math.max(bottom, arrow.label.box.y + arrow.label.box.h);
  }
  const height = Math.round(bottom + A.laneBottom + A.pad);
  const lanes: ArchLane[] = input.lanes.map((lane, index) => ({
    id: lane.id,
    lane: lanesById.get(lane.id)!,
    box: { x: laneX(index), y: A.pad, w: A.laneW, h: height - A.pad * 2 },
    ...laneTitle(lane),
  }));

  const atlas: Atlas = { lanes: {}, nodes: {}, edges: {}, steps: {} };
  for (const lane of lanes) atlas.lanes[lane.id] = lane.box;
  for (const card of cards.values()) atlas.nodes[card.id] = card.box;
  for (const arrow of arrows) atlas.edges[arrow.id] = union([boundsOf(arrow.points), ...(arrow.label ? [arrow.label.box] : [])])!;

  return { width, height, lanes, cards: [...cards.values()].sort((a, b) => nodeOrder.get(a.id)! - nodeOrder.get(b.id)!), arrows, atlas };
}

/** The lane heading as drawn: its label and platform in small capitals, cut to the lane. */
export function laneTitle(lane: XRayLane): { title: string; platform: string } {
  const A = ARCH;
  const room = A.laneW - 32;
  const title = fitText(lane.label.toUpperCase(), room, A.laneTitleSize, "sans-semibold", A.laneTitleTracking);
  const used = textWidth(title, A.laneTitleSize, "sans-semibold", A.laneTitleTracking);
  const platform = lane.platform && room - used > 40 ? fitText(` · ${lane.platform.toUpperCase()}`, room - used, A.laneTitleSize, "sans", A.laneTitleTracking) : "";
  return { title, platform };
}

/** The width of a label pill for `text`, with the AI tag when the arrow is a model's guess. */
export function labelPillWidth(text: string, ai: boolean): number {
  const A = ARCH;
  const textW = text ? textWidth(text, A.labelSize, "sans-semibold") + A.labelPadX * 2 : 0;
  return textW + (ai ? A.aiTagW + (text ? 2 : 0) : 0);
}

function placeLabel(
  edge: XRayEdge,
  points: readonly Point[],
  cards: readonly Box[],
  titles: readonly Box[],
  placed: readonly Box[],
  others: readonly (readonly [Point, Point])[],
): ArchLabel | null {
  const A = ARCH;
  const ai = edge.evidence === "inferred";
  const full = (edge.label ?? "").trim();
  if (!full && !ai) return null;
  const text = full ? fitText(full, A.labelMax, A.labelSize, "sans-semibold") : "";
  const w = labelPillWidth(text, ai);
  const h = A.labelH;
  const segments = points.slice(1).map((to, i) => ({ from: points[i], to, len: Math.abs(to.x - points[i].x) + Math.abs(to.y - points[i].y), i }));
  segments.sort((a, b) => b.len - a.len || a.i - b.i);
  // Centred on the line first; then, beside a vertical run (whose channel is narrower than a pill), touching it on
  // either side, so a pill in a lane's inner channel can sit out in the gutter instead of on a card.
  const centred: Box[] = [];
  const beside: Box[] = [];
  for (const seg of segments) {
    for (const t of [0.5, 0.32, 0.68, 0.18, 0.82, 0.42, 0.58, 0.25, 0.75, 0.1, 0.9]) {
      const cx = seg.from.x + (seg.to.x - seg.from.x) * t;
      const cy = seg.from.y + (seg.to.y - seg.from.y) * t;
      centred.push({ x: Math.round(cx - w / 2), y: Math.round(cy - h / 2), w, h });
      if (seg.from.x === seg.to.x) {
        beside.push({ x: Math.round(cx + 4), y: Math.round(cy - h / 2), w, h });
        beside.push({ x: Math.round(cx - 4 - w), y: Math.round(cy - h / 2), w, h });
      }
    }
  }
  const candidates = [...centred, ...beside];
  const score = (box: Box) => {
    let area = 0;
    for (const other of cards) area += overlapArea(box, other) * 4;
    for (const other of titles) area += overlapArea(box, other) * 2;
    for (const other of placed) area += overlapArea(box, inflate(other, 3));
    return area;
  };
  // How many other arrows a pill would sit on; a pill on a crossing line reads as that line's label.
  const crossings = (box: Box) => {
    let n = 0;
    const b = inflate(box, 2);
    for (const [p, q] of others) if (segmentHitsBox(p, q, b)) n += 1;
    return n;
  };
  const clear = candidates
    .map((box, order) => ({ box, order }))
    .filter(({ box }) => !cards.some((other) => overlaps(box, other)) && !titles.some((other) => overlaps(box, other)) && !placed.some((other) => overlaps(box, inflate(other, 3))));
  // lc-debt: every clear spot is checked against every other segment (labels × spots × segments); a segment grid is the upgrade past a few hundred arrows.
  const best = clear.length ? clear.map((c) => ({ ...c, n: crossings(c.box) })).sort((a, b) => a.n - b.n || a.order - b.order)[0].box : null;
  const box = best ?? [...candidates].sort((a, b) => score(a) - score(b))[0];
  return box ? { box, text, full, ai } : null;
}

/** Whether an axis-aligned segment passes through a box. */
function segmentHitsBox(p: Point, q: Point, box: Box): boolean {
  const x1 = Math.min(p.x, q.x);
  const x2 = Math.max(p.x, q.x);
  const y1 = Math.min(p.y, q.y);
  const y2 = Math.max(p.y, q.y);
  return x1 <= box.x + box.w && x2 >= box.x && y1 <= box.y + box.h && y2 >= box.y;
}
