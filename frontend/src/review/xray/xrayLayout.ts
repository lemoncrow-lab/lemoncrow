// Pixel geometry for the X-Ray picture: where each card sits and how each arrow bends. Pure: it takes the diagram and
// the width on offer, and returns the same numbers for the same inputs. Positions come from the whole diagram (both
// sides), so the Before/After toggle redraws arrows over cards that stay where they were.
import type { LensCard, LensColumn, LensDiagram, LensEdge } from "./xrayLens";

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LaneBox extends Box {
  id: string;
  label: string;
  count: number;
}

export interface LensGeometry {
  width: number;
  height: number;
  card: { w: number; h: number };
  boxes: Map<string, Box>;
  lanes: LaneBox[];
  /** The tests band under the lanes, when the change has tests. */
  band: LaneBox | null;
}

export interface EdgePath {
  d: string;
  mid: { x: number; y: number };
}

const PAD = 20;
const LANE_PAD = 12;
const LANE_HEAD = 32;
const ROW_GAP = 14;
const INNER_GAP = 44;
const LANE_GAP = 64;
const BAND_GAP = 44;
const CARD_H = 70;
const CARD_W = 200;
const CARD_MIN_W = 160;
const MAX_LANE_GAP = 150;

/** Lays the diagram out for a canvas *available* pixels wide (0 when not measured: the natural width). */
export function layoutLens(diagram: Pick<LensDiagram, "columns" | "cards">, available: number): LensGeometry {
  const main = diagram.cards.filter((card) => card.place === "main");
  const tests = diagram.cards.filter((card) => card.place === "tests");
  const columns = diagram.columns;
  const groups = columns.reduce<string[]>((out, column) => (out.includes(column.group) ? out : [...out, column.group]), []);
  const innerGaps = columns.filter((column, index) => index > 0 && columns[index - 1].group === column.group).length;
  const laneGaps = Math.max(0, groups.length - 1);
  const natural = (cardW: number, laneGap: number) => 2 * PAD + 2 * LANE_PAD * groups.length + columns.length * cardW + innerGaps * INNER_GAP + laneGaps * laneGap;

  let cardW = CARD_W;
  let laneGap = LANE_GAP;
  if (available > 0 && columns.length) {
    if (natural(cardW, laneGap) > available) {
      const room = available - natural(0, laneGap);
      cardW = Math.max(CARD_MIN_W, Math.floor(room / columns.length));
    } else if (laneGaps) {
      laneGap = Math.min(MAX_LANE_GAP, laneGap + Math.floor((available - natural(cardW, laneGap)) / laneGaps));
    }
  }
  const contentW = columns.length ? natural(cardW, laneGap) : 0;
  const bandW = tests.length * (cardW + ROW_GAP) - ROW_GAP + 2 * PAD + 2 * LANE_PAD;
  const width = Math.max(contentW, available > 0 ? available : 0, tests.length ? Math.min(bandW, Math.max(contentW, available)) : 0);
  const offset = Math.max(0, Math.floor((width - contentW) / 2));

  // Column x positions, left to right.
  const columnX: number[] = [];
  let x = offset + PAD;
  columns.forEach((column: LensColumn, index) => {
    const previous = columns[index - 1];
    if (!previous) x += LANE_PAD;
    else if (previous.group === column.group) x += cardW + INNER_GAP;
    else x += cardW + LANE_PAD + laneGap + LANE_PAD;
    columnX.push(x);
  });

  const rowsIn = columns.map((_, index) => main.filter((card) => card.column === index).length);
  const maxRows = Math.max(0, ...rowsIn);
  const stride = CARD_H + ROW_GAP;
  const top = PAD;
  const boxes = new Map<string, Box>();
  for (const card of main) {
    const shift = ((maxRows - (rowsIn[card.column] ?? 0)) * stride) / 2;
    boxes.set(card.id, { x: columnX[card.column] ?? PAD, y: top + LANE_HEAD + shift + card.row * stride, w: cardW, h: CARD_H });
  }
  const laneH = maxRows ? LANE_HEAD + maxRows * stride - ROW_GAP + LANE_PAD : 0;
  const lanes: LaneBox[] = groups.map((group) => {
    const indexes = columns.map((column, index) => (column.group === group ? index : -1)).filter((index) => index >= 0);
    const first = columnX[indexes[0]];
    const last = columnX[indexes[indexes.length - 1]];
    return {
      id: group,
      label: columns[indexes[0]].groupLabel,
      count: main.filter((card) => indexes.includes(card.column)).reduce((sum, card) => sum + (card.folded ? card.folded.count : 1), 0),
      x: first - LANE_PAD,
      y: top,
      w: last - first + cardW + 2 * LANE_PAD,
      h: laneH,
    };
  });

  let height = maxRows ? top + laneH + PAD : PAD;
  let band: LaneBox | null = null;
  if (tests.length) {
    const bandTop = maxRows ? height - PAD + BAND_GAP : PAD;
    const perRow = Math.max(1, Math.floor((width - 2 * PAD - 2 * LANE_PAD + ROW_GAP) / (cardW + ROW_GAP)));
    const rows = Math.ceil(tests.length / perRow);
    const bandX = PAD;
    const innerW = width - 2 * PAD - 2 * LANE_PAD;
    // Tests come ordered by the lanes they reach (left to right), centred in rows under the lanes.
    const ordered = [...tests].sort((a, b) => a.column - b.column);
    ordered.forEach((card, index) => {
      const row = Math.floor(index / perRow);
      const col = index % perRow;
      const inRow = Math.min(perRow, tests.length - row * perRow);
      const rowW = inRow * (cardW + ROW_GAP) - ROW_GAP;
      const start = bandX + LANE_PAD + Math.max(0, Math.floor((innerW - rowW) / 2));
      boxes.set(card.id, { x: start + col * (cardW + ROW_GAP), y: bandTop + LANE_HEAD + row * stride, w: cardW, h: CARD_H });
    });
    const bandH = LANE_HEAD + rows * stride - ROW_GAP + LANE_PAD;
    band = { id: "tests", label: "Tests", count: tests.reduce((sum, card) => sum + (card.folded ? card.folded.count : 1), 0), x: bandX, y: bandTop, w: width - 2 * PAD, h: bandH };
    height = bandTop + bandH + PAD;
  }
  return { width, height, card: { w: cardW, h: CARD_H }, boxes, lanes, band };
}

type Side = "L" | "R" | "T" | "B";

interface End {
  key: string;
  card: string;
  side: Side;
  /** Where the other end is, to order ports along the side. */
  toward: number;
  start: boolean;
}

function anchor(box: Box, side: Side, offset: number): { x: number; y: number } {
  switch (side) {
    case "L": return { x: box.x, y: box.y + box.h / 2 + offset };
    case "R": return { x: box.x + box.w, y: box.y + box.h / 2 + offset };
    case "T": return { x: box.x + box.w / 2 + offset, y: box.y };
    default: return { x: box.x + box.w / 2 + offset, y: box.y + box.h };
  }
}

const round = (value: number) => Math.round(value * 10) / 10;

/**
 * The curve of each arrow: forward arrows leave a card's right edge for the next card's left edge, backward ones run
 * right to left, arrows inside one column bow out to the right, and tests reach up into the lanes. Several arrows on
 * one side of a card spread along it in the order of their other ends, so they do not start on one point.
 */
export function edgePaths(edges: readonly LensEdge[], geometry: LensGeometry, cards: readonly LensCard[]): Map<string, EdgePath> {
  const byId = new Map(cards.map((card) => [card.id, card]));
  const out = new Map<string, EdgePath>();
  const plans: { edge: LensEdge; a: Box; b: Box; sa: Side; sb: Side; kind: "h" | "back" | "same" | "up" | "down" | "band" }[] = [];
  for (const edge of edges) {
    const a = geometry.boxes.get(edge.from);
    const b = geometry.boxes.get(edge.to);
    const ca = byId.get(edge.from);
    const cb = byId.get(edge.to);
    if (!a || !b || !ca || !cb) continue;
    const mainA = ca.place === "main";
    const mainB = cb.place === "main";
    if (mainA && mainB) {
      if (a.x < b.x) plans.push({ edge, a, b, sa: "R", sb: "L", kind: "h" });
      else if (a.x > b.x) plans.push({ edge, a, b, sa: "L", sb: "R", kind: "back" });
      else plans.push({ edge, a, b, sa: "R", sb: "R", kind: "same" });
    } else if (!mainA && mainB) plans.push({ edge, a, b, sa: "T", sb: "B", kind: "up" });
    else if (mainA && !mainB) plans.push({ edge, a, b, sa: "B", sb: "T", kind: "down" });
    else plans.push({ edge, a, b, sa: "B", sb: "B", kind: "band" });
  }
  const ends: End[] = [];
  for (const plan of plans) {
    const along = (side: Side, other: Box) => (side === "L" || side === "R" ? other.y + other.h / 2 : other.x + other.w / 2);
    ends.push({ key: plan.edge.key, card: plan.edge.from, side: plan.sa, toward: along(plan.sa, plan.b), start: true });
    ends.push({ key: plan.edge.key, card: plan.edge.to, side: plan.sb, toward: along(plan.sb, plan.a), start: false });
  }
  const offsets = new Map<string, number>();
  const bySlot = new Map<string, End[]>();
  for (const end of ends) {
    const slot = `${end.card}\u0000${end.side}`;
    (bySlot.get(slot) ?? bySlot.set(slot, []).get(slot)!).push(end);
  }
  for (const list of bySlot.values()) {
    list.sort((p, q) => p.toward - q.toward || p.key.localeCompare(q.key));
    const vertical = list[0].side === "L" || list[0].side === "R";
    const step = vertical ? 9 : 16;
    const limit = vertical ? geometry.card.h / 2 - 10 : geometry.card.w / 2 - 16;
    const spread = Math.min(step, list.length > 1 ? (2 * limit) / (list.length - 1) : step);
    list.forEach((end, index) => offsets.set(`${end.key}\u0000${end.start ? "s" : "e"}`, (index - (list.length - 1) / 2) * spread));
  }
  for (const plan of plans) {
    const s = anchor(plan.a, plan.sa, offsets.get(`${plan.edge.key}\u0000s`) ?? 0);
    const e = anchor(plan.b, plan.sb, offsets.get(`${plan.edge.key}\u0000e`) ?? 0);
    let c1: { x: number; y: number };
    let c2: { x: number; y: number };
    if (plan.kind === "h") {
      const dx = Math.max(28, (e.x - s.x) * 0.45);
      c1 = { x: s.x + dx, y: s.y };
      c2 = { x: e.x - dx, y: e.y };
    } else if (plan.kind === "back") {
      const dx = Math.max(28, (s.x - e.x) * 0.45);
      c1 = { x: s.x - dx, y: s.y };
      c2 = { x: e.x + dx, y: e.y };
    } else if (plan.kind === "same") {
      const bow = 30 + Math.min(40, Math.abs(e.y - s.y) * 0.15);
      c1 = { x: s.x + bow, y: s.y };
      c2 = { x: e.x + bow, y: e.y };
    } else if (plan.kind === "up") {
      const dy = Math.max(30, (s.y - e.y) * 0.4);
      c1 = { x: s.x, y: s.y - dy };
      c2 = { x: e.x, y: e.y + dy };
    } else if (plan.kind === "down") {
      const dy = Math.max(30, (e.y - s.y) * 0.4);
      c1 = { x: s.x, y: s.y + dy };
      c2 = { x: e.x, y: e.y - dy };
    } else {
      c1 = { x: s.x, y: s.y + 34 };
      c2 = { x: e.x, y: e.y + 34 };
    }
    const mid = {
      x: (s.x + 3 * c1.x + 3 * c2.x + e.x) / 8,
      y: (s.y + 3 * c1.y + 3 * c2.y + e.y) / 8,
    };
    out.set(plan.edge.key, {
      d: `M${round(s.x)},${round(s.y)} C${round(c1.x)},${round(c1.y)} ${round(c2.x)},${round(c2.y)} ${round(e.x)},${round(e.y)}`,
      mid: { x: round(mid.x), y: round(mid.y) },
    });
  }
  return out;
}
