/**
 * What the X-Ray graph screen shows, as pure functions of the document: the Before/After filter, how an arrow or step
 * is drawn, which nodes a view covers, where the walkthrough looks, the order recorded calls pulse in, and hit-testing
 * against a layout's atlas.
 */
import { contains, distanceToPolyline, inflate, union, type Box, type Point } from "./geometry";
import type { ArchLayout, Atlas } from "./archLayout";
import type { FlowLayout } from "./flowLayout";
import type {
  XRayDelta,
  XRayEdge,
  XRayEvidence,
  XRayFlowStep,
  XRayGraph,
  XRayGraphFlow,
  XRayNode,
  XRayPayload,
  XRayTourStep,
  XRayView,
} from "./xrayGraphTypes";

/** Both sides (the change), or one side of it. */
export type GraphSide = "both" | "before" | "after";

/** A flow step's key. Step ids are unique only within their flow, so every step lookup goes by flow and step. */
export function stepKey(flow: string, step: string): string {
  return JSON.stringify([flow, step]);
}

/** Before shows removed, unchanged and modified; After shows added, unchanged and modified (the contract's rule). */
export function onSide(delta: XRayDelta, side: GraphSide): boolean {
  if (side === "before") return delta !== "added";
  if (side === "after") return delta !== "removed";
  return true;
}

/**
 * The document as one side sees it: nodes by their own delta, arrows by theirs and only between kept nodes, flow
 * participants by their node and steps by their own delta between kept participants. Views and the tour are kept.
 */
export function filterGraph(graph: XRayGraph, side: GraphSide): XRayGraph {
  if (side === "both") return graph;
  const nodes = graph.nodes.filter((node) => onSide(node.delta, side));
  const kept = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => onSide(edge.delta, side) && kept.has(edge.from) && kept.has(edge.to));
  const flows = graph.flows.map((flow) => {
    const participants = flow.participants.filter((p) => kept.has(p.node));
    const present = new Set(participants.map((p) => p.node));
    return { ...flow, participants, steps: flow.steps.filter((step) => onSide(step.delta, side) && present.has(step.from) && present.has(step.to)) };
  });
  return { ...graph, nodes, edges, flows };
}

export type Tone = "added" | "modified" | "removed" | "unchanged";
export type LineStyle = "solid" | "dashed" | "return";

export interface LineLook {
  tone: Tone;
  line: LineStyle;
  /** A model proposed it: drawn with an "AI" tag and never as fact. */
  ai: boolean;
  /** A recorded run observed it: a pulse travels along it in order. */
  pulse: boolean;
  /** Removed: drawn faded. */
  ghost: boolean;
  hero: boolean;
}

/** An arrow's look: colour from its delta, line from its evidence (recorded solid, otherwise dashed). */
export function edgeLook(edge: Pick<XRayEdge, "delta" | "evidence" | "hero">): LineLook {
  return {
    tone: edge.delta,
    line: edge.evidence === "recorded" ? "solid" : "dashed",
    ai: edge.evidence === "inferred",
    pulse: edge.evidence === "recorded",
    ghost: edge.delta === "removed",
    hero: Boolean(edge.hero),
  };
}

/** A step's look: like an arrow, except a return is always drawn as a return line (dashed back to the caller). */
export function stepLook(step: Pick<XRayFlowStep, "delta" | "evidence" | "kind">): LineLook {
  const base = edgeLook({ delta: step.delta, evidence: step.evidence });
  return step.kind === "return" ? { ...base, line: "return" } : base;
}

export function deltaWord(delta: XRayDelta): string {
  return delta === "added" ? "New" : delta === "modified" ? "Changed" : delta === "removed" ? "Removed" : "Unchanged";
}

export function deltaGlyph(delta: XRayDelta): string {
  return delta === "added" ? "+" : delta === "modified" ? "~" : delta === "removed" ? "−" : "=";
}

export function evidenceWord(evidence: XRayEvidence): string {
  return evidence === "recorded" ? "Recorded in a run" : evidence === "static" ? "Checked in code" : "AI guess";
}

/** Every view, depth first, with its parents. */
export function flattenViews(views: readonly XRayView[]): { view: XRayView; path: XRayView[] }[] {
  const out: { view: XRayView; path: XRayView[] }[] = [];
  const walk = (list: readonly XRayView[], path: XRayView[]) => {
    for (const view of list) {
      out.push({ view, path: [...path, view] });
      walk(view.children ?? [], [...path, view]);
    }
  };
  walk(views, []);
  return out;
}

export function findView(views: readonly XRayView[], id: string): { view: XRayView; path: XRayView[] } | null {
  return flattenViews(views).find((entry) => entry.view.id === id) ?? null;
}

/** The tabs: the overview first, then its drill-downs, then any other top-level view. */
export function viewTabs(views: readonly XRayView[]): XRayView[] {
  if (!views.length) return [];
  const [overview, ...rest] = views;
  return [overview, ...(overview.children ?? []), ...rest];
}

/** A flow's view: the first view scoped to it, or a plain one made up for the walkthrough. */
export function viewForFlow(graph: XRayGraph, flowId: string): XRayView {
  const found = flattenViews(graph.views).find((entry) => entry.view.scope.kind === "flow" && entry.view.scope.flow === flowId);
  if (found) return found.view;
  const flow = graph.flows.find((f) => f.id === flowId);
  return { id: `flow:${flowId}`, title: flow?.title ?? "Flow", lens: "flow", scope: { kind: "flow", flow: flowId }, children: [] };
}

export type ViewContent =
  | { lens: "architecture"; nodes: XRayNode[]; edges: XRayEdge[]; lanes: XRayGraph["lanes"] }
  | { lens: "flow"; flow: XRayGraphFlow | null };

/**
 * What a view draws from a (possibly filtered) document. An architecture view keeps the lanes its scope touches in
 * the unfiltered document, so switching Before and After never moves a lane.
 */
export function viewContent(view: XRayView, graph: XRayGraph, full: XRayGraph = graph): ViewContent {
  if (view.scope.kind === "flow" || view.lens === "flow") {
    const flowId = view.scope.kind === "flow" ? view.scope.flow : "";
    return { lens: "flow", flow: graph.flows.find((flow) => flow.id === flowId) ?? null };
  }
  const wanted = view.scope.kind === "nodes" ? new Set(view.scope.nodes) : null;
  const inScope = (node: XRayNode) => !wanted || wanted.has(node.id);
  const nodes = graph.nodes.filter(inScope);
  const ids = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => ids.has(edge.from) && ids.has(edge.to));
  const laneIds = new Set(full.nodes.filter(inScope).map((node) => node.lane));
  const lanes = wanted ? full.lanes.filter((lane) => laneIds.has(lane.id)) : full.lanes;
  return { lens: "architecture", nodes, edges, lanes };
}

/**
 * The order recorded arrows pulse in: the order recorded flow steps walk them (either direction), then document
 * order for the rest. Removed arrows pulse only on the Before side.
 */
export function pulseOrder(graph: XRayGraph, edges: readonly XRayEdge[], side: GraphSide): string[] {
  const recorded = edges.filter((edge) => edge.evidence === "recorded" && (side === "before" || edge.delta !== "removed"));
  const rank = new Map<string, number>();
  let position = 0;
  for (const flow of graph.flows) {
    for (const step of flow.steps) {
      if (step.evidence !== "recorded" || step.kind === "return") continue;
      const edge = recorded.find((e) => !rank.has(e.id) && ((e.from === step.from && e.to === step.to) || (e.from === step.to && e.to === step.from)));
      if (edge) rank.set(edge.id, position++);
    }
  }
  return recorded
    .map((edge, index) => ({ id: edge.id, key: rank.get(edge.id) ?? 1_000 + index }))
    .sort((a, b) => a.key - b.key)
    .map((entry) => entry.id);
}

/** The flow steps that carry this arrow's data: same two nodes, in either direction, with a payload. */
export function payloadStepsForEdge(graph: XRayGraph, edge: XRayEdge): { flow: XRayGraphFlow; step: XRayFlowStep }[] {
  const out: { flow: XRayGraphFlow; step: XRayFlowStep }[] = [];
  for (const flow of graph.flows) {
    for (const step of flow.steps) {
      if (!step.payload || (!step.payload.request && !step.payload.response)) continue;
      if ((step.from === edge.from && step.to === edge.to) || (step.from === edge.to && step.to === edge.from)) out.push({ flow, step });
    }
  }
  // The call itself first, then its return.
  return out.sort((a, b) => Number(a.step.kind === "return") - Number(b.step.kind === "return"));
}

/** The request and response an arrow carries, merged from its steps (the call's own payload wins). */
export function payloadForEdge(
  graph: XRayGraph,
  edge: XRayEdge,
): { request?: XRayPayload; response?: XRayPayload; steps: { flow: XRayGraphFlow; step: XRayFlowStep }[] } | null {
  const steps = payloadStepsForEdge(graph, edge);
  if (!steps.length) return null;
  let request: XRayPayload | undefined;
  let response: XRayPayload | undefined;
  for (const { step } of steps) {
    request ??= step.payload?.request;
    response ??= step.payload?.response;
  }
  return { request, response, steps };
}

export type Stage = { kind: "view"; view: XRayView } | { kind: "flow"; view: XRayView; flow: string };

export interface TourFocus {
  stage: Stage;
  /** null: everything is in focus. Steps are by `stepKey`. */
  lit: { nodes: Set<string>; edges: Set<string>; steps: Set<string> } | null;
}

/**
 * Where a walkthrough step looks: the view or flow it stages, and what it lights. An arrow in focus lights its two
 * ends; a message lights its two participants. Ids that the staged picture does not draw are ignored.
 */
export function resolveTourStep(graph: XRayGraph, step: XRayTourStep): TourFocus {
  const stage: Stage = step.stage.kind === "flow"
    ? { kind: "flow", view: viewForFlow(graph, step.stage.flow), flow: step.stage.flow }
    : { kind: "view", view: findView(graph.views, step.stage.view)?.view ?? graph.views[0] };
  if (step.focus.kind === "all") return { stage, lit: null };
  const nodes = new Set(step.focus.nodes ?? []);
  const edges = new Set(step.focus.edges ?? []);
  const steps = new Set<string>();
  for (const edge of graph.edges) if (edges.has(edge.id)) nodes.add(edge.from).add(edge.to);
  if (stage.kind === "flow") {
    const flow = graph.flows.find((f) => f.id === stage.flow);
    for (const message of flow?.steps ?? []) {
      if ((step.focus.messages ?? []).includes(message.id)) {
        steps.add(stepKey(stage.flow, message.id));
        nodes.add(message.from).add(message.to);
      }
    }
  }
  return { stage, lit: { nodes, edges, steps } };
}

/** The box a focus zooms to in a drawn layout's atlas, padded; null when nothing lit is drawn (zoom to fit). */
export function focusBox(atlas: Atlas, lit: TourFocus["lit"], pad = 32): Box | null {
  if (!lit) return null;
  const boxes: Box[] = [];
  for (const id of lit.nodes) if (atlas.nodes[id]) boxes.push(atlas.nodes[id]);
  for (const id of lit.edges) if (atlas.edges[id]) boxes.push(atlas.edges[id]);
  for (const id of lit.steps) if (atlas.steps[id]) boxes.push(atlas.steps[id]);
  const box = union(boxes);
  return box ? inflate(box, pad) : null;
}

/** What is selected. A step names its flow too, since step ids repeat across flows. */
export type Hit = { kind: "node" | "edge" | "lane"; id: string } | { kind: "step"; flow: string; id: string };

export function sameHit(a: Hit | null, b: Hit | null): boolean {
  if (!a || !b || a.kind !== b.kind || a.id !== b.id) return false;
  return a.kind !== "step" || (b.kind === "step" && a.flow === b.flow);
}

/** What sits under a point of an architecture layout: a label or card first, then an arrow within `tolerance`, then a lane. */
export function hitArchitecture(layout: ArchLayout, point: Point, tolerance = 6): Hit | null {
  for (const arrow of layout.arrows) if (arrow.label && contains(arrow.label.box, point)) return { kind: "edge", id: arrow.id };
  for (const card of layout.cards) if (contains(card.box, point)) return { kind: "node", id: card.id };
  let best: { id: string; d: number } | null = null;
  for (const arrow of layout.arrows) {
    const d = distanceToPolyline(point, arrow.points);
    if (d <= tolerance && (!best || d < best.d)) best = { id: arrow.id, d };
  }
  if (best) return { kind: "edge", id: best.id };
  for (const lane of layout.lanes) if (contains(lane.box, point)) return { kind: "lane", id: lane.id };
  return null;
}

/** What sits under a point of a flow layout: a participant header, then a step row. */
export function hitFlow(layout: FlowLayout, point: Point): Hit | null {
  for (const column of layout.columns) if (contains(column.head, point)) return { kind: "node", id: column.node };
  for (const message of layout.messages) if (contains(message.box, point)) return { kind: "step", flow: message.flow, id: message.id };
  return null;
}

/** The one caveat line the screen may show, most important first; null when there is nothing to warn about. */
export function caveatLine(graph: XRayGraph, pending: boolean): string | null {
  if (pending) return "AI is still adding names, flows and the walkthrough; this picture is from the code index.";
  if (!graph.provenance.recorded_runs.length) return "No run was recorded on this revision; every arrow here comes from the code.";
  return graph.degraded[0] ?? null;
}
