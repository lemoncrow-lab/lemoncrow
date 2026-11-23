/** Plain geometry for the X-Ray renderer: boxes, orthogonal polylines, rounded paths, arrowheads. All integer output. */

export interface Point {
  x: number;
  y: number;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const round = (value: number) => Math.round(value);

export function roundBox(box: Box): Box {
  return { x: round(box.x), y: round(box.y), w: round(box.w), h: round(box.h) };
}

export function inflate(box: Box, by: number): Box {
  return { x: box.x - by, y: box.y - by, w: box.w + by * 2, h: box.h + by * 2 };
}

export function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

export function overlapArea(a: Box, b: Box): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

export function contains(box: Box, point: Point): boolean {
  return point.x >= box.x && point.x <= box.x + box.w && point.y >= box.y && point.y <= box.y + box.h;
}

export function union(boxes: readonly Box[]): Box | null {
  if (!boxes.length) return null;
  let x1 = Infinity;
  let y1 = Infinity;
  let x2 = -Infinity;
  let y2 = -Infinity;
  for (const box of boxes) {
    x1 = Math.min(x1, box.x);
    y1 = Math.min(y1, box.y);
    x2 = Math.max(x2, box.x + box.w);
    y2 = Math.max(y2, box.y + box.h);
  }
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

export function boundsOf(points: readonly Point[]): Box {
  return union(points.map((p) => ({ x: p.x, y: p.y, w: 0, h: 0 }))) ?? { x: 0, y: 0, w: 0, h: 0 };
}

/** Drops repeated points and the middle of straight runs, so every remaining point is a real corner. */
export function simplify(points: readonly Point[]): Point[] {
  const out: Point[] = [];
  for (const point of points) {
    const last = out[out.length - 1];
    if (last && last.x === point.x && last.y === point.y) continue;
    if (out.length >= 2) {
      const prev = out[out.length - 2];
      const straight = (prev.x === last.x && last.x === point.x) || (prev.y === last.y && last.y === point.y);
      if (straight) out[out.length - 1] = point;
      else out.push(point);
    } else out.push(point);
  }
  return out;
}

function length(a: Point, b: Point): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function polylineLength(points: readonly Point[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i += 1) total += length(points[i - 1], points[i]);
  return total;
}

/** The point a fraction `t` (0..1) of the way along a polyline. */
export function pointAlong(points: readonly Point[], t: number): Point {
  if (!points.length) return { x: 0, y: 0 };
  const total = polylineLength(points);
  let left = Math.max(0, Math.min(1, t)) * total;
  for (let i = 1; i < points.length; i += 1) {
    const seg = length(points[i - 1], points[i]);
    if (left <= seg && seg > 0) {
      const k = left / seg;
      return { x: points[i - 1].x + (points[i].x - points[i - 1].x) * k, y: points[i - 1].y + (points[i].y - points[i - 1].y) * k };
    }
    left -= seg;
  }
  return points[points.length - 1];
}

export function distanceToSegment(p: Point, a: Point, b: Point): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(p.x - (a.x + dx * t), p.y - (a.y + dy * t));
}

export function distanceToPolyline(p: Point, points: readonly Point[]): number {
  let best = Infinity;
  for (let i = 1; i < points.length; i += 1) best = Math.min(best, distanceToSegment(p, points[i - 1], points[i]));
  return best;
}

/** An SVG path through orthogonal points with each corner rounded to at most `radius`. */
export function roundedPath(points: readonly Point[], radius: number): string {
  if (!points.length) return "";
  const parts = [`M${round(points[0].x)} ${round(points[0].y)}`];
  for (let i = 1; i < points.length - 1; i += 1) {
    const prev = points[i - 1];
    const at = points[i];
    const next = points[i + 1];
    const r = Math.floor(Math.min(radius, length(prev, at) / 2, length(at, next) / 2));
    if (r < 1) {
      parts.push(`L${round(at.x)} ${round(at.y)}`);
      continue;
    }
    const inX = at.x - Math.sign(at.x - prev.x) * r;
    const inY = at.y - Math.sign(at.y - prev.y) * r;
    const outX = at.x + Math.sign(next.x - at.x) * r;
    const outY = at.y + Math.sign(next.y - at.y) * r;
    parts.push(`L${round(inX)} ${round(inY)}`, `Q${round(at.x)} ${round(at.y)} ${round(outX)} ${round(outY)}`);
  }
  const last = points[points.length - 1];
  if (points.length > 1) parts.push(`L${round(last.x)} ${round(last.y)}`);
  return parts.join(" ");
}

export interface Arrowhead {
  /** The polyline, shortened so its stroke ends under the head instead of poking through the tip. */
  points: Point[];
  /** The head itself as an SVG path: a filled triangle, or an open chevron. */
  head: string;
}

/** Ends `points` in an arrowhead whose tip is the last point. */
export function withArrowhead(points: readonly Point[], open = false, size = 8): Arrowhead {
  const out = points.map((p) => ({ ...p }));
  if (out.length < 2) return { points: out, head: "" };
  const tip = out[out.length - 1];
  const from = out[out.length - 2];
  const len = length(from, tip) || 1;
  const ux = (tip.x - from.x) / len;
  const uy = (tip.y - from.y) / len;
  const half = size * 0.56;
  const baseX = tip.x - ux * size;
  const baseY = tip.y - uy * size;
  const left = { x: round(baseX - uy * half), y: round(baseY + ux * half) };
  const right = { x: round(baseX + uy * half), y: round(baseY - ux * half) };
  const head = open
    ? `M${left.x} ${left.y} L${round(tip.x)} ${round(tip.y)} L${right.x} ${right.y}`
    : `M${left.x} ${left.y} L${round(tip.x)} ${round(tip.y)} L${right.x} ${right.y} Z`;
  // A filled head covers the line's end; an open head needs the line to reach the tip.
  if (!open && len > size) out[out.length - 1] = { x: round(tip.x - ux * (size - 2)), y: round(tip.y - uy * (size - 2)) };
  return { points: out, head };
}
