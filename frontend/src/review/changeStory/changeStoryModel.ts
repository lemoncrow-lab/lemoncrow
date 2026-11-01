// Change Story: an ordered, step-by-step walk over one Snapshot's changed path,
// built only from the Review's own Map graph, architecture flow, findings,
// review targets and recorded checks. Pure and deterministic; nothing here
// fetches, asks a model, or guesses an association the data does not prove.
import { EDGE_KIND_LABEL, edgeKind, isChangedStatus, nodeName, visualEnds } from "../reviewMapModel";
import type { MapChapter } from "../reviewMapModel";
import type {
  Annotation,
  ReviewArchitectureFlowStep,
  ReviewArchitectureObservedFlow,
  ReviewMapEdge,
  ReviewMapGraph,
  ReviewMapNode,
  ReviewMapSelection,
  ReviewTarget,
} from "../types";

export type StoryExactness = "snapshot" | "observed" | "current_index" | "inferred" | "unknown";
export type StoryChange = "added" | "removed" | "changed" | "unchanged" | "context_only" | "unknown";
export type StoryRole = "entry" | "changed" | "dependency" | "affected";
export type StoryReveal = "path" | "neighbors" | "all";

/** The Brief's check wording: a check that did not run or has no result is never a pass. */
export const CHECK_TEXT: Record<string, { label: string; tone: string }> = {
  PASS: { label: "passed", tone: "text-emerald-300/90" },
  FAIL: { label: "failed", tone: "text-red-300" },
  NOT_RUN: { label: "did not run", tone: "text-amber-300/90" },
  UNKNOWN: { label: "no result recorded", tone: "text-amber-300/90" },
};

export function checkText(status: string): { label: string; tone: string } {
  return CHECK_TEXT[status] ?? CHECK_TEXT.UNKNOWN;
}

const MOVED: ReadonlySet<string> = new Set(["added", "removed", "changed"]);
const CLOSED_DISPOSITIONS: ReadonlySet<string> = new Set(["dismissed", "fixed", "superseded_stale"]);

export const EXACTNESS_LABEL: Record<StoryExactness, string> = {
  snapshot: "Snapshot-exact",
  observed: "observed in a recorded run",
  current_index: "from the current index, not this Snapshot",
  inferred: "inferred",
  unknown: "exactness unavailable",
};

export const CHANGE_LABEL: Record<StoryChange, string> = {
  added: "added",
  removed: "removed",
  changed: "changed",
  unchanged: "unchanged",
  context_only: "context only",
  unknown: "change unknown",
};

const ROLE_LABEL: Record<StoryRole, string> = {
  entry: "Entry point",
  changed: "Changed component",
  dependency: "Relies on, unchanged",
  affected: "Affected caller",
};

export interface StoryFinding {
  id: string;
  severity: "p0" | "p1";
  title: string;
  path: string;
  line: number;
  side: "base" | "head";
  /** Where the anchor proves the link: the step's file, or the relationship's recorded site. */
  on: "node" | "relationship";
}

export interface StoryChecks {
  /** Review targets of this step that the server links checks to. */
  targets: number;
  pass: number;
  fail: number;
  unknown: number;
  /** Plain words, worst first; never "passed" unless a pass was recorded. */
  words: string[];
  tone: string;
}

export interface StoryReceipt {
  label: string;
  ref: string;
  recordId: string;
}

export interface StoryHuman {
  targets: number;
  reviewed: number;
  needsChanges: number;
  openThreads: number;
  /** Human review is still needed on this step's targets. */
  needed: boolean;
}

export interface StoryVia {
  edgeId: string;
  /** Visual ends: dependent → what it depends on. */
  fromId: string;
  toId: string;
  sentence: string;
  relation: string;
  change: StoryChange;
  changeLabel: string;
  exactness: StoryExactness;
  exactnessLabel: string;
  /** Only Snapshot-exact or observed relationships move on screen. */
  animate: boolean;
  /** Runtime order, in words: observed only when a recorded trace matched. */
  runtime: string;
  site: { path: string; line: number } | null;
  selection: ReviewMapSelection;
}

export interface ChangeStoryStep {
  index: number;
  nodeId: string;
  role: StoryRole;
  roleLabel: string;
  title: string;
  path: string;
  line: number;
  endLine: number;
  side: "base" | "head";
  locationLabel: string;
  change: StoryChange;
  changeLabel: string;
  exactness: StoryExactness;
  exactnessLabel: string;
  boundaries: string[];
  via: StoryVia | null;
  selection: ReviewMapSelection;
  /** Source jumps open Snapshot source only; current-index context is not this Snapshot. */
  canOpenSource: boolean;
  evidence: {
    findings: StoryFinding[];
    checks: StoryChecks | null;
    receipts: StoryReceipt[];
    human: StoryHuman | null;
  };
}

export interface ChangeStory {
  steps: ChangeStoryStep[];
  ordering: "flow" | "dependencies";
  orderNote: string;
  changedTotal: number;
  omittedChanged: number;
  affectedTotal: number;
  omittedAffected: number;
  /** False when findings were not supplied: absence of a finding then proves nothing. */
  findingsKnown: boolean;
  targetsKnown: boolean;
}

export interface ChangeStoryInput {
  graph: ReviewMapGraph;
  flow?: { applicable: boolean; steps: ReviewArchitectureFlowStep[]; observed?: ReviewArchitectureObservedFlow } | null;
  findings?: readonly Annotation[] | null;
  targets?: readonly ReviewTarget[] | null;
  /** Changed files walked before the rest are counted as omitted. */
  maxChanged?: number;
  maxAffected?: number;
}

export const DEFAULT_MAX_CHANGED = 20;
export const DEFAULT_MAX_AFFECTED = 6;
const NEIGHBOR_LIMIT = 12;
const RECEIPT_LIMIT = 3;

function plural(count: number, one: string, many = one + "s"): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function storyExactness(value: string | undefined): StoryExactness {
  return value === "snapshot" || value === "current_index" || value === "inferred" || value === "observed" ? value : "unknown";
}

export function nodeChange(node: ReviewMapNode): StoryChange {
  const delta = String(node.delta_status ?? "");
  if (delta === "added" || delta === "removed" || delta === "changed" || delta === "unchanged" || delta === "context_only") return delta;
  if (delta === "unknown") return "unknown";
  if (node.status === "new") return "added";
  if (node.status === "changed") return /^(d|deleted|removed)$/i.test(node.change ?? "") ? "removed" : "changed";
  return node.status === "context" ? "context_only" : "unchanged";
}

export function edgeChange(edge: ReviewMapEdge): StoryChange {
  const delta = String(edge.delta_status ?? "");
  if (delta === "added" || delta === "removed" || delta === "changed" || delta === "unchanged" || delta === "context_only") return delta;
  return "unknown";
}

export function isChangedNode(node: ReviewMapNode): boolean {
  return MOVED.has(String(node.delta_status ?? "")) || isChangedStatus(node);
}

function nodeSide(node: ReviewMapNode): "base" | "head" {
  if (node.selection?.side === "old") return "base";
  return nodeChange(node) === "removed" ? "base" : "head";
}

function rankOf(node: ReviewMapNode): number {
  return typeof node.rank === "number" && node.rank > 0 ? node.rank : Number.POSITIVE_INFINITY;
}

function nodePaths(node: ReviewMapNode): string[] {
  const paths = node.paths?.length ? node.paths : node.path ? [node.path] : [];
  return node.old_path && !paths.includes(node.old_path) ? [...paths, node.old_path] : paths;
}

function openFinding(annotation: Annotation): annotation is Annotation & { severity: "p0" | "p1" } {
  return (annotation.severity === "p0" || annotation.severity === "p1")
    && annotation.state === "open"
    && annotation.anchored
    && !CLOSED_DISPOSITIONS.has(annotation.finding_disposition ?? "")
    && Boolean(annotation.path);
}

function toFinding(annotation: Annotation & { severity: "p0" | "p1" }, on: StoryFinding["on"]): StoryFinding {
  return {
    id: annotation.id,
    severity: annotation.severity,
    title: annotation.title || annotation.body.split("\n")[0] || "Finding",
    path: annotation.path,
    line: annotation.start_line,
    side: annotation.side === "old" ? "base" : "head",
    on,
  };
}

export function checksFor(node: ReviewMapNode, targets: ReadonlyMap<string, ReviewTarget> | null): StoryChecks | null {
  if (!targets) return null;
  const linked = (node.target_ids ?? []).map((id) => targets.get(id)).filter((target): target is ReviewTarget => Boolean(target));
  if (!linked.length) return null;
  const pass = linked.reduce((sum, target) => sum + (target.verification?.pass ?? 0), 0);
  const fail = linked.reduce((sum, target) => sum + (target.verification?.fail ?? 0), 0);
  const unknown = linked.reduce((sum, target) => sum + (target.verification?.unknown ?? 0), 0);
  const words = [
    fail ? `${fail} ${CHECK_TEXT.FAIL.label}` : "",
    pass ? `${pass} ${CHECK_TEXT.PASS.label}` : "",
    unknown ? `${unknown} ${CHECK_TEXT.UNKNOWN.label}` : "",
  ].filter(Boolean);
  const tone = fail ? CHECK_TEXT.FAIL.tone : pass && !unknown ? CHECK_TEXT.PASS.tone : CHECK_TEXT.UNKNOWN.tone;
  return { targets: linked.length, pass, fail, unknown, words: words.length ? words : ["no check recorded"], tone };
}

function humanFor(node: ReviewMapNode): StoryHuman | null {
  const review = node.review;
  if (!review || !review.targets) return null;
  const outstanding = typeof review.outstanding === "number" ? review.outstanding : review.targets - review.reviewed;
  return {
    targets: review.targets,
    reviewed: review.reviewed,
    needsChanges: review.needs_changes,
    openThreads: review.open_threads,
    needed: outstanding > 0 || review.needs_changes > 0 || review.changed_since_review > 0,
  };
}

interface Link {
  edge: ReviewMapEdge;
  other: string;
}

/**
 * Builds the walkthrough: the changed entry point first, then the changed
 * files it reaches through recorded relationships (dependents before what they
 * depend on), then unchanged callers the change reaches. The order follows the
 * architecture flow when the Snapshot comparison provides one, else recorded
 * dependencies; it is never presented as a runtime order unless a recorded
 * trace matched the step.
 */
export function buildChangeStory(input: ChangeStoryInput): ChangeStory {
  const { graph } = input;
  const maxChanged = input.maxChanged ?? DEFAULT_MAX_CHANGED;
  const maxAffected = input.maxAffected ?? DEFAULT_MAX_AFFECTED;
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const byPath = new Map<string, ReviewMapNode>();
  for (const node of graph.nodes) if (node.path && !byPath.has(node.path)) byPath.set(node.path, node);
  const index = new Map(graph.nodes.map((node, position) => [node.id, position]));
  const order = (a: ReviewMapNode, b: ReviewMapNode) =>
    rankOf(a) - rankOf(b) || (index.get(a.id) ?? 0) - (index.get(b.id) ?? 0);

  const changed = graph.nodes.filter(isChangedNode).sort(order);
  const changedIds = new Set(changed.map((node) => node.id));
  const dependencies = new Map<string, Link[]>();
  const dependents = new Map<string, Link[]>();
  for (const edge of graph.edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target) || edge.source === edge.target) continue;
    const { from, to } = visualEnds(edge);
    (dependencies.get(from) ?? dependencies.set(from, []).get(from)!).push({ edge, other: to });
    (dependents.get(to) ?? dependents.set(to, []).get(to)!).push({ edge, other: from });
  }
  const linkOrder = (a: Link, b: Link) =>
    Number(b.edge.exactness === "snapshot") - Number(a.edge.exactness === "snapshot")
    || (b.edge.weight || 0) - (a.edge.weight || 0)
    || order(byId.get(a.other)!, byId.get(b.other)!);
  for (const list of dependencies.values()) list.sort(linkOrder);
  for (const list of dependents.values()) list.sort(linkOrder);

  const flowSteps = input.flow?.applicable ? input.flow.steps : [];
  const flowByEdge = new Map(flowSteps.map((step) => [step.edge_id, step]));
  const visited: { node: ReviewMapNode; via: ReviewMapEdge | null }[] = [];
  const seen = new Set<string>();
  const emit = (node: ReviewMapNode, via: ReviewMapEdge | null) => {
    if (seen.has(node.id)) return false;
    seen.add(node.id);
    visited.push({ node, via });
    return true;
  };
  const cursor = new Map<string, number>();
  // An unchanged file is walked to only through a relationship the comparison
  // says moved (an added import of a storage module, say), and never past it.
  const walkable = (link: Link) => !seen.has(link.other)
    && (changedIds.has(link.other) || MOVED.has(String(link.edge.delta_status ?? "")));
  // Iterative depth-first walk (preorder) so a long chain of changed files cannot overflow the stack.
  const walk = (start: ReviewMapNode, via: ReviewMapEdge | null) => {
    emit(start, via);
    const path: ReviewMapNode[] = changedIds.has(start.id) ? [start] : [];
    while (path.length) {
      const node = path[path.length - 1];
      const links = dependencies.get(node.id) ?? [];
      let at = cursor.get(node.id) ?? 0;
      while (at < links.length && !walkable(links[at])) at += 1;
      cursor.set(node.id, at + 1);
      const link = links[at];
      if (!link) {
        path.pop();
        continue;
      }
      const child = byId.get(link.other)!;
      emit(child, link.edge);
      if (changedIds.has(child.id)) path.push(child);
    }
  };

  let ordering: ChangeStory["ordering"] = "dependencies";
  if (flowSteps.length) {
    const edgeById = new Map(graph.edges.map((edge) => [edge.id, edge]));
    for (const step of flowSteps) {
      const edge = edgeById.get(step.edge_id);
      const source = edge ? byId.get(edge.source) : byPath.get(step.from);
      const target = edge ? byId.get(edge.target) : byPath.get(step.to);
      if (!source || !target) continue;
      ordering = "flow";
      const ends = edge ? visualEnds(edge) : { from: source.id, to: target.id };
      emit(byId.get(ends.from)!, null);
      emit(byId.get(ends.to)!, edge ?? null);
    }
    // Continue each flow file's own changed dependencies before the rest.
    for (const item of [...visited]) walk(item.node, null);
  }
  // Tests depend on the code they cover, but they are not where a change starts:
  // they neither make an entry point nor stop one, and are walked last.
  const isTest = (id: string) => byId.get(id)?.category === "test";
  const entries = changed.filter((node) => !isTest(node.id)
    && !(dependents.get(node.id) ?? []).some((link) => changedIds.has(link.other) && !isTest(link.other)));
  for (const node of entries) if (!seen.has(node.id)) walk(node, null);
  // Cycles and files without a recorded relationship to the rest, then tests.
  for (const node of changed) if (!seen.has(node.id) && !isTest(node.id)) walk(node, null);
  for (const node of changed) if (!seen.has(node.id)) walk(node, null);

  const shownPath = visited.slice(0, Math.max(0, maxChanged));
  const shownIds = new Set(shownPath.map((item) => item.node.id));
  const shownChanged = shownPath.filter((item) => changedIds.has(item.node.id));
  // A step whose arriving edge starts at a file left out of the walk keeps no arrow.
  const chained = shownPath.map((item) => {
    if (!item.via) return item;
    const { from, to } = visualEnds(item.via);
    const other = from === item.node.id ? to : from;
    return shownIds.has(other) ? item : { node: item.node, via: null };
  });

  // Unchanged code that depends on a walked file: the callers the change reaches.
  const affectedLinks = new Map<string, Link & { score: number }>();
  for (const item of shownChanged) {
    for (const link of dependents.get(item.node.id) ?? []) {
      if (changedIds.has(link.other) || shownIds.has(link.other)) continue;
      const score = (link.edge.exactness === "snapshot" ? 1000 : 0) + (link.edge.weight || 1);
      const prior = affectedLinks.get(link.other);
      if (!prior || score > prior.score) affectedLinks.set(link.other, { edge: link.edge, other: item.node.id, score });
    }
  }
  const affectedAll = [...affectedLinks.entries()]
    .map(([id, link]) => ({ node: byId.get(id)!, link }))
    .sort((a, b) => b.link.score - a.link.score || order(a.node, b.node));
  const affected = affectedAll.slice(0, Math.max(0, maxAffected));

  const targets = input.targets ? new Map(input.targets.map((target) => [target.target_id, target])) : null;
  const findings = (input.findings ?? []).filter(openFinding);

  const viaFor = (edge: ReviewMapEdge): StoryVia => {
    const { from, to } = visualEnds(edge);
    const fromNode = byId.get(from);
    const toNode = byId.get(to);
    const kind = edgeKind(edge.kind);
    const fromName = fromNode ? nodeName(fromNode) : from;
    const toName = toNode ? nodeName(toNode) : to;
    const flow = flowByEdge.get(edge.id);
    const observed = flow?.grounding === "observed" && Boolean(flow.trace);
    const exactness: StoryExactness = observed ? "observed" : storyExactness(edge.exactness);
    const site = edge.evidence?.find((item) => item.path && item.line > 0) ?? null;
    return {
      edgeId: edge.id,
      fromId: from,
      toId: to,
      sentence: `${fromName} ${EDGE_KIND_LABEL[kind]} ${toName}`,
      relation: edge.kind,
      change: edgeChange(edge),
      changeLabel: CHANGE_LABEL[edgeChange(edge)],
      exactness,
      exactnessLabel: EXACTNESS_LABEL[exactness],
      animate: exactness === "snapshot" || exactness === "observed",
      runtime: observed && flow?.trace
        ? `Observed on ${flow.trace.side} in ${flow.trace.recipe}: ${flow.trace.caller_fn} → ${flow.trace.callee_fn}`
        : "Static relationship; no recorded run observed this order.",
      site: site ? { path: site.path, line: site.line } : null,
      selection: edge.selection,
    };
  };

  const receiptsFor = (node: ReviewMapNode, via: ReviewMapEdge | null): StoryReceipt[] => {
    const out: StoryReceipt[] = [];
    const trace = via ? flowByEdge.get(via.id) : undefined;
    if (trace?.grounding === "observed" && trace.trace) {
      out.push({
        label: `${trace.trace.recipe}: ${trace.trace.caller_fn} → ${trace.trace.callee_fn} (${trace.trace.side})`,
        ref: trace.trace.evidence_ref,
        recordId: trace.trace.record_id,
      });
    }
    const observed = input.flow?.observed;
    if (observed?.available) {
      for (const call of observed.head) {
        if (out.length >= RECEIPT_LIMIT) break;
        if (call.from !== node.path && call.to !== node.path) continue;
        out.push({ label: `${call.from_fn} → ${call.to_fn} (head run)`, ref: call.evidence_ref, recordId: "" });
      }
    }
    return out.slice(0, RECEIPT_LIMIT);
  };

  const findingsFor = (node: ReviewMapNode, via: StoryVia | null): StoryFinding[] => {
    const paths = new Set(nodePaths(node));
    const out: StoryFinding[] = [];
    for (const finding of findings) {
      if (paths.has(finding.path)) {
        out.push(toFinding(finding, "node"));
        continue;
      }
      const site = via?.site;
      if (site && finding.path === site.path && finding.start_line <= site.line && site.line <= Math.max(finding.start_line, finding.end_line)) {
        out.push(toFinding(finding, "relationship"));
      }
    }
    return out.sort((a, b) => a.severity.localeCompare(b.severity) || a.line - b.line);
  };

  const stepOf = (node: ReviewMapNode, via: ReviewMapEdge | null, role: StoryRole, position: number): ChangeStoryStep => {
    const exactness = storyExactness(node.exactness);
    const change = nodeChange(node);
    const side = nodeSide(node);
    const line = node.selection?.line ?? node.line ?? 0;
    const viaStep = via ? viaFor(via) : null;
    const kind = via ? edgeKind(via.kind) : "other";
    return {
      index: position,
      nodeId: node.id,
      role,
      roleLabel: role === "affected" && kind === "impacts" ? "Affected site" : ROLE_LABEL[role],
      title: nodeName(node),
      path: node.path,
      line,
      endLine: node.end_line ?? line,
      side,
      locationLabel: `${node.path}${line > 0 ? `:${line}` : ""} · ${side}`,
      change,
      changeLabel: CHANGE_LABEL[change],
      exactness,
      exactnessLabel: EXACTNESS_LABEL[exactness],
      boundaries: [...(node.boundaries ?? [])],
      via: viaStep,
      selection: node.selection,
      canOpenSource: exactness === "snapshot" && Boolean(node.path),
      evidence: {
        findings: findingsFor(node, viaStep),
        checks: checksFor(node, targets),
        receipts: receiptsFor(node, via),
        human: humanFor(node),
      },
    };
  };

  const steps: ChangeStoryStep[] = [];
  chained.forEach((item, position) => {
    const role: StoryRole = !changedIds.has(item.node.id) ? "dependency" : position === 0 || !item.via ? "entry" : "changed";
    steps.push(stepOf(item.node, item.via, role, steps.length));
  });
  for (const item of affected) steps.push(stepOf(item.node, item.link.edge, "affected", steps.length));

  const observedSteps = steps.filter((step) => step.via?.exactness === "observed").length;
  const orderNote = ordering === "flow"
    ? `Ordered by the changed-relationship flow between the base and this Snapshot. ${observedSteps ? `${plural(observedSteps, "step")} matched a recorded run; the rest are not a runtime order.` : "This is not a runtime order."}`
    : "Ordered by recorded dependencies between changed files, dependents first. This is not a runtime order.";

  return {
    steps,
    ordering,
    orderNote,
    changedTotal: changed.length,
    omittedChanged: Math.max(0, changed.length - shownChanged.length),
    affectedTotal: affectedAll.length,
    omittedAffected: Math.max(0, affectedAll.length - affected.length),
    findingsKnown: Array.isArray(input.findings),
    targetsKnown: Array.isArray(input.targets),
  };
}

export interface StoryStage {
  graph: ReviewMapGraph;
  /** One reading-order chapter per walked changed file, numbered by step. */
  chapters: MapChapter[];
  hiddenNodes: number;
}

/**
 * The focused sub-map a step is drawn on. By default only the walked path and
 * the current step's nearest neighbours, so a 400-file Snapshot stays legible;
 * "all" hands the whole graph to the Map's own folding.
 */
export function storyStage(story: ChangeStory, graph: ReviewMapGraph, current: number, reveal: StoryReveal): StoryStage {
  const stepIds = story.steps.map((step) => step.nodeId);
  const keep = new Set(stepIds);
  const step = story.steps[current];
  if (reveal === "all") {
    for (const node of graph.nodes) keep.add(node.id);
  } else if (reveal === "neighbors" && step) {
    const near = graph.edges
      .filter((edge) => edge.source === step.nodeId || edge.target === step.nodeId)
      .map((edge) => ({ id: edge.source === step.nodeId ? edge.target : edge.source, edge }))
      .filter((item) => !keep.has(item.id))
      .sort((a, b) => Number(b.edge.exactness === "snapshot") - Number(a.edge.exactness === "snapshot") || (b.edge.weight || 0) - (a.edge.weight || 0));
    for (const item of near) {
      if (keep.size >= stepIds.length + NEIGHBOR_LIMIT) break;
      keep.add(item.id);
    }
  }
  const position = new Map(stepIds.map((id, index) => [id, index]));
  const nodes = graph.nodes
    .filter((node) => keep.has(node.id))
    .sort((a, b) => (position.get(a.id) ?? Number.POSITIVE_INFINITY) - (position.get(b.id) ?? Number.POSITIVE_INFINITY));
  const edges = graph.edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target));
  const chapters = story.steps
    .filter((item) => item.role === "entry" || item.role === "changed")
    .map((item) => ({ id: `story-${item.index}`, label: `${item.index + 1} · ${item.roleLabel}`, paths: [item.path] }));
  return {
    graph: { ...graph, nodes, edges, truncated: graph.truncated || nodes.length < graph.nodes.length },
    chapters: chapters.length >= 2 ? chapters : [],
    hiddenNodes: graph.nodes.length - nodes.length,
  };
}
