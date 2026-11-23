/**
 * The flow layout: one sequence diagram. Participants are columns in their given order, steps are rows in their given
 * order. Column gaps grow only as far as the widest label between them needs; activation bars open on a call and
 * close on the matching return. Text comes from the width table and coordinates are rounded, like the architecture.
 */
import { union, withArrowhead, type Box, type Point } from "./geometry";
import { fitText, textWidth, wrapText } from "./textWidth";
import type { Atlas } from "./archLayout";
import { stepKey } from "./graphModel";
import type { XRayFlowStep, XRayGraphFlow, XRayNode } from "./xrayGraphTypes";

export const FLOW = {
  pad: 24,
  indexW: 30,
  headW: 208,
  headH: 60,
  minGap: 236,
  firstRow: 44,
  rowH: 50,
  selfW: 34,
  selfH: 22,
  noteSize: 11,
  noteLine: 15,
  labelSize: 11,
  labelH: 20,
  labelPadX: 8,
  labelMax: 280,
  repeatW: 26,
  aiTagW: 22,
  actW: 10,
  nest: 5,
  bottom: 28,
} as const;

export interface FlowColumn {
  node: string;
  label: string;
  x: number;
  head: Box;
  /** The column band, header to the end of the lifeline. */
  band: Box;
  lifeline: { x: number; y1: number; y2: number };
}

export interface FlowActivation {
  node: string;
  box: Box;
}

export interface FlowMessage {
  /** The step id, unique only within its flow. */
  id: string;
  flow: string;
  /** `stepKey(flow, id)`: what highlights, the atlas and the pulse go by. */
  key: string;
  index: number;
  step: XRayFlowStep;
  points: Point[];
  /** The line (ending under a filled head) and the head; async and return heads are open. */
  line: Point[];
  head: string;
  open: boolean;
  label: { box: Box; text: string; full: string; repeat: number; ai: boolean };
  note: { x: number; y: number; lines: string[]; anchor: "middle" | "start" } | null;
  /** The step's row: its full span plus label and note. */
  box: Box;
  y: number;
}

export interface FlowLayout {
  width: number;
  height: number;
  columns: FlowColumn[];
  activations: FlowActivation[];
  messages: FlowMessage[];
  atlas: Atlas;
}

export function stepPillWidth(text: string, repeat: number, ai: boolean): number {
  const F = FLOW;
  return textWidth(text, F.labelSize, "sans-semibold") + F.labelPadX * 2 + (repeat >= 2 ? F.repeatW : 0) + (ai ? F.aiTagW : 0);
}

export function layoutFlow(flow: XRayGraphFlow, nodes: ReadonlyMap<string, XRayNode>): FlowLayout {
  const F = FLOW;
  const participants = flow.participants.filter((p, index, all) => all.findIndex((q) => q.node === p.node) === index);
  const col = new Map(participants.map((p, index) => [p.node, index]));
  const steps = flow.steps.filter((step) => col.has(step.from) && col.has(step.to));
  const labels = steps.map((step) => fitText(step.label, F.labelMax, F.labelSize, "sans-semibold"));
  const pillW = steps.map((step, i) => stepPillWidth(labels[i], step.repeat ?? 0, step.evidence === "inferred"));

  // Column gaps: wide enough for the header cards, then for each label between them (short spans first).
  const n = participants.length;
  const gaps: number[] = Array.from({ length: Math.max(0, n - 1) }, () => F.minGap);
  let rightRoom = F.headW / 2;
  const order = steps.map((step, i) => ({ i, a: Math.min(col.get(step.from)!, col.get(step.to)!), b: Math.max(col.get(step.from)!, col.get(step.to)!) }));
  order.sort((p, q) => p.b - p.a - (q.b - q.a) || p.i - q.i);
  for (const { i, a, b } of order) {
    if (a === b) {
      const need = F.selfW + 10 + pillW[i] + 16;
      if (a < n - 1) gaps[a] = Math.max(gaps[a], need + F.actW);
      else rightRoom = Math.max(rightRoom, need + F.actW);
      continue;
    }
    const need = pillW[i] + 56;
    let span = 0;
    for (let g = a; g < b; g += 1) span += gaps[g];
    if (span < need) {
      const extra = Math.ceil((need - span) / (b - a));
      for (let g = a; g < b; g += 1) gaps[g] += extra;
    }
  }

  const top = F.pad;
  const xs: number[] = [];
  let x = F.pad + F.indexW + F.headW / 2;
  for (let i = 0; i < n; i += 1) {
    xs.push(Math.round(x));
    if (i < n - 1) x += gaps[i];
  }

  // Rows, then activations from the call and return structure.
  const stacks = new Map<string, { start: number; level: number }[]>(participants.map((p) => [p.node, []]));
  const activations: FlowActivation[] = [];
  const openBar = (node: string, start: number) => {
    const stack = stacks.get(node)!;
    stack.push({ start, level: stack.length });
  };
  const closeBar = (node: string, end: number) => {
    const bar = stacks.get(node)!.pop();
    if (bar) activations.push({ node, box: barBox(node, bar.level, bar.start, end) });
  };
  const barBox = (node: string, level: number, start: number, end: number): Box => ({
    x: xs[col.get(node)!] - F.actW / 2 + level * F.nest,
    y: start - 6,
    w: F.actW,
    h: Math.max(14, end - start + 12),
  });
  const level = (node: string) => stacks.get(node)!.length;
  const edgeX = (node: string, toward: number) => {
    const depth = level(node);
    const base = xs[col.get(node)!];
    if (!depth) return base;
    const offset = (depth - 1) * F.nest;
    return base + offset + (toward > 0 ? F.actW / 2 : -F.actW / 2);
  };

  const messages: FlowMessage[] = [];
  let y = top + F.headH + F.firstRow;
  steps.forEach((step, index) => {
    const a = col.get(step.from)!;
    const b = col.get(step.to)!;
    const repeat = step.repeat && step.repeat >= 2 ? step.repeat : 0;
    const ai = step.evidence === "inferred";
    const w = pillW[index];
    let points: Point[];
    let labelBox: Box;
    let note: FlowMessage["note"] = null;
    let rowH: number = F.rowH;
    const open = step.kind === "async" || step.kind === "return";
    if (a === b) {
      const x1 = edgeX(step.from, 1) || xs[a];
      const start = level(step.from) ? x1 : xs[a] + F.actW / 2;
      points = [
        { x: start, y },
        { x: start + F.selfW, y },
        { x: start + F.selfW, y: y + F.selfH },
        { x: start + 2, y: y + F.selfH },
      ];
      activations.push({ node: step.from, box: barBox(step.from, level(step.from), y + F.selfH - 4, y + F.selfH + 6) });
      labelBox = { x: start + F.selfW + 10, y: Math.round(y + F.selfH / 2 - F.labelH / 2), w, h: F.labelH };
      rowH += F.selfH;
      if (step.note) {
        const lines = wrapText(step.note, Math.max(160, (a < n - 1 ? gaps[a] : rightRoom) - F.selfW - 30), F.noteSize, 3);
        note = { x: labelBox.x, y: y + F.selfH + 18, lines, anchor: "start" };
        rowH += lines.length * F.noteLine;
      }
    } else {
      const dir = b > a ? 1 : -1;
      if (step.kind === "return") {
        const x1 = edgeX(step.from, dir);
        closeBar(step.from, y);
        const x2 = edgeX(step.to, -dir);
        points = [{ x: x1, y }, { x: x2, y }];
      } else {
        const x1 = edgeX(step.from, dir);
        if (step.kind === "sync") openBar(step.to, y);
        else activations.push({ node: step.to, box: barBox(step.to, level(step.to), y, y + 10) });
        const depth = level(step.to);
        const base = xs[b] + (step.kind === "sync" ? (depth - 1) * F.nest : level(step.to) * F.nest);
        const x2 = base - dir * (F.actW / 2);
        points = [{ x: x1, y }, { x: x2, y }];
      }
      const mid = (points[0].x + points[1].x) / 2;
      labelBox = { x: Math.round(mid - w / 2), y: Math.round(y - F.labelH / 2), w, h: F.labelH };
      if (step.note) {
        const span = Math.abs(points[1].x - points[0].x);
        const lines = wrapText(step.note, Math.max(160, span - 32), F.noteSize, 3);
        note = { x: Math.round(mid), y: y + 26, lines, anchor: "middle" };
        rowH += lines.length * F.noteLine + 4;
      }
    }
    points = points.map((p) => ({ x: Math.round(p.x), y: Math.round(p.y) }));
    const { points: line, head } = withArrowhead(points, open);
    const noteBox = note ? noteBounds(note) : null;
    const box = union([
      { x: Math.min(...points.map((p) => p.x)), y: y - F.labelH / 2 - 4, w: Math.max(1, Math.max(...points.map((p) => p.x)) - Math.min(...points.map((p) => p.x))), h: Math.max(...points.map((p) => p.y)) - y + F.labelH },
      labelBox,
      ...(noteBox ? [noteBox] : []),
    ])!;
    messages.push({ id: step.id, flow: flow.id, key: stepKey(flow.id, step.id), index, step, points, line, head, open, label: { box: labelBox, text: labels[index], full: step.label, repeat, ai }, note, box: roundBox(box), y });
    y += rowH;
  });
  const end = y - F.rowH / 2 + 8;
  for (const node of [...stacks.keys()]) while (stacks.get(node)!.length) closeBar(node, end);

  const bottom = Math.round(end + F.bottom);
  const columns: FlowColumn[] = participants.map((p, index) => {
    const node = nodes.get(p.node);
    const cx = xs[index];
    const head = { x: Math.round(cx - F.headW / 2), y: top, w: F.headW, h: F.headH };
    return {
      node: p.node,
      label: p.label || node?.label || p.node,
      x: cx,
      head,
      band: { x: head.x - 8, y: top - 8, w: F.headW + 16, h: bottom - top + 8 },
      lifeline: { x: cx, y1: top + F.headH, y2: bottom - 8 },
    };
  });
  const lastX = xs.length ? xs[xs.length - 1] : F.pad;
  let width = Math.round(lastX + Math.max(F.headW / 2 + 8, rightRoom) + F.pad);
  for (const message of messages) width = Math.max(width, message.box.x + message.box.w + F.pad);
  const height = bottom + F.pad;

  const atlas: Atlas = { lanes: {}, nodes: {}, edges: {}, steps: {} };
  for (const column of columns) atlas.nodes[column.node] = column.head;
  for (const message of messages) atlas.steps[message.key] = message.box;
  return { width, height, columns, activations: activations.map((a) => ({ ...a, box: roundBox(a.box) })), messages, atlas };
}

function noteBounds(note: NonNullable<FlowMessage["note"]>): Box {
  const F = FLOW;
  const w = Math.max(...note.lines.map((line) => textWidth(line, F.noteSize)));
  const x = note.anchor === "middle" ? note.x - w / 2 : note.x;
  return { x, y: note.y - F.noteSize, w, h: note.lines.length * F.noteLine };
}

function roundBox(box: Box): Box {
  return { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.w), h: Math.round(box.h) };
}
