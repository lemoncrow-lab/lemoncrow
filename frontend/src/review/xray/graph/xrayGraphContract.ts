/**
 * The contract's producer rules (docs/reference/xray-graph.md, "Rules the producer enforces") as a checker. The
 * renderer does not need a valid document to draw, but fixtures and tests use this to stay contract-valid.
 */
import type { XRayAnchor, XRayGraph, XRayPayload, XRayView } from "./xrayGraphTypes";

const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value ?? null)).length;

export function contractProblems(graph: XRayGraph): string[] {
  const problems: string[] = [];
  const fail = (text: string) => problems.push(text);
  const max = (what: string, text: string | undefined, limit: number) => {
    if (text !== undefined && [...text].length > limit) fail(`${what} is longer than ${limit}: ${JSON.stringify(text)}`);
  };
  if (graph.version !== 1) fail("version must be 1");
  max("title", graph.title, 80);
  max("summary", graph.summary, 240);
  const runs = new Set(graph.provenance.recorded_runs.map((run) => run.ref));
  if (graph.provenance.producer === "index" && graph.provenance.model) fail("an index-only graph names no model");

  if (graph.lanes.length < 1 || graph.lanes.length > 8) fail("lanes must be 1..8");
  const lanes = new Set(graph.lanes.map((lane) => lane.id));
  if (lanes.size !== graph.lanes.length) fail("lane ids must be unique");

  const anchorsOk = (where: string, anchors: XRayAnchor[]) => {
    for (const anchor of anchors) {
      if (!anchor.path) fail(`${where}: an anchor has no path`);
      if (anchor.side !== "base" && anchor.side !== "head") fail(`${where}: anchor side must be base or head`);
      if (anchor.start_line !== undefined && anchor.end_line !== undefined && anchor.end_line < anchor.start_line) fail(`${where}: anchor lines run backwards`);
    }
  };

  if (graph.nodes.length > 60) fail("at most 60 nodes");
  const nodes = new Set<string>();
  for (const node of graph.nodes) {
    if (nodes.has(node.id)) fail(`node id ${node.id} repeats`);
    nodes.add(node.id);
    if (!lanes.has(node.lane)) fail(`node ${node.id} names lane ${node.lane}, which does not exist`);
    max(`node ${node.id} subtitle`, node.subtitle, 60);
    if (node.badges.length > 3) fail(`node ${node.id} has more than 3 badges`);
    if (node.kind !== "external" && node.anchors.length === 0) fail(`node ${node.id} has no anchor`);
    if (node.source === "recorded" && runs.size === 0) fail(`node ${node.id} is recorded but no run is`);
    anchorsOk(`node ${node.id}`, node.anchors);
  }

  if (graph.edges.length > 150) fail("at most 150 edges");
  const edges = new Set<string>();
  let heroes = 0;
  for (const edge of graph.edges) {
    if (edges.has(edge.id)) fail(`edge id ${edge.id} repeats`);
    edges.add(edge.id);
    if (!nodes.has(edge.from) || !nodes.has(edge.to)) fail(`edge ${edge.id} names a node that does not exist`);
    max(`edge ${edge.id} label`, edge.label, 40);
    if (edge.hero) heroes += 1;
    if (edge.evidence === "recorded" && (!edge.recorded || !runs.has(edge.recorded.ref))) fail(`edge ${edge.id} is recorded without a run from provenance`);
    anchorsOk(`edge ${edge.id}`, edge.anchors);
  }
  if (heroes > 1) fail("at most one hero edge");

  const payloadOk = (where: string, payload: XRayPayload | undefined) => {
    if (!payload) return;
    max(`${where} type`, payload.type, 60);
    if (payload.shape !== undefined && new TextEncoder().encode(payload.shape).length > 2048) fail(`${where} shape is over 2048 bytes`);
    if (bytes(payload.before) > 4096 || bytes(payload.after) > 4096) fail(`${where} sample is over 4096 bytes`);
    if (payload.evidence === "recorded" && !payload.evidence_ref) fail(`${where} is recorded without evidence_ref`);
  };
  if (graph.flows.length > 6) fail("at most 6 flows");
  const flows = new Map<string, Set<string>>();
  for (const flow of graph.flows) {
    max(`flow ${flow.id} title`, flow.title, 60);
    max(`flow ${flow.id} summary`, flow.summary, 140);
    if (flow.participants.length < 2 || flow.participants.length > 8) fail(`flow ${flow.id} needs 2..8 participants`);
    if (flow.steps.length < 1 || flow.steps.length > 24) fail(`flow ${flow.id} needs 1..24 steps`);
    const members = new Set(flow.participants.map((p) => p.node));
    for (const p of flow.participants) if (!nodes.has(p.node)) fail(`flow ${flow.id} participant ${p.node} does not exist`);
    const ids = new Set<string>();
    for (const step of flow.steps) {
      if (ids.has(step.id)) fail(`flow ${flow.id} step id ${step.id} repeats`);
      ids.add(step.id);
      if (!members.has(step.from) || !members.has(step.to)) fail(`flow ${flow.id} step ${step.id} names a non-participant`);
      if ((step.kind === "self") !== (step.from === step.to)) fail(`flow ${flow.id} step ${step.id}: a self step goes from a node to itself`);
      max(`step ${step.id} label`, step.label, 48);
      max(`step ${step.id} note`, step.note, 140);
      if (step.repeat !== undefined && step.repeat < 2) fail(`step ${step.id} repeat must be >= 2`);
      if (step.evidence === "recorded" && runs.size === 0) fail(`step ${step.id} is recorded but no run is`);
      if (step.evidence === "recorded" && step.recorded && !runs.has(step.recorded.ref)) fail(`flow ${flow.id} step ${step.id} names run ${step.recorded.ref}, which is not in provenance`);
      anchorsOk(`step ${step.id}`, step.anchors);
      payloadOk(`step ${step.id} request`, step.payload?.request);
      payloadOk(`step ${step.id} response`, step.payload?.response);
    }
    flows.set(flow.id, ids);
  }

  const views = new Map<string, XRayView>();
  const walk = (list: XRayView[]) => {
    for (const view of list) {
      if (views.has(view.id)) fail(`view id ${view.id} repeats`);
      views.set(view.id, view);
      max(`view ${view.id} title`, view.title, 60);
      max(`view ${view.id} summary`, view.summary, 140);
      if (view.scope.kind === "nodes") for (const id of view.scope.nodes) if (!nodes.has(id)) fail(`view ${view.id} names node ${id}, which does not exist`);
      if (view.scope.kind === "flow" && !flows.has(view.scope.flow)) fail(`view ${view.id} names flow ${view.scope.flow}, which does not exist`);
      walk(view.children ?? []);
    }
  };
  walk(graph.views);
  if (!graph.views.length) fail("at least one view");
  else if (graph.views[0].scope.kind !== "all") fail("views[0] is the overview, scope all");
  if (views.size > 32) fail("at most 32 views");

  const tour = graph.walkthrough?.steps;
  if (tour) {
    if (tour.length < 2 || tour.length > 12) fail("a walkthrough has 2..12 steps");
    for (const step of tour) {
      max(`tour ${step.id} heading`, step.heading, 48);
      max(`tour ${step.id} body`, step.body, 140);
      let flow: string | null = null;
      if (step.stage.kind === "view") {
        const view = views.get(step.stage.view);
        if (!view) fail(`tour ${step.id} stages view ${step.stage.view}, which does not exist`);
        else if (view.scope.kind === "flow") flow = view.scope.flow;
      } else {
        flow = step.stage.flow;
        if (!flows.has(flow)) fail(`tour ${step.id} stages flow ${flow}, which does not exist`);
      }
      if (step.focus.kind === "selection") {
        for (const id of step.focus.nodes ?? []) if (!nodes.has(id)) fail(`tour ${step.id} focuses node ${id}, which does not exist`);
        for (const id of step.focus.edges ?? []) if (!edges.has(id)) fail(`tour ${step.id} focuses edge ${id}, which does not exist`);
        for (const id of step.focus.messages ?? []) if (!flow || !flows.get(flow)?.has(id)) fail(`tour ${step.id} focuses message ${id}, which is not in the flow it stages`);
      }
    }
  }
  if (graph.degraded.length > 5) fail("at most 5 degraded notes");
  return problems;
}
