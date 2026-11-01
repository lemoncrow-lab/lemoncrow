// Behavioral X-Ray (N01): one bounded, focused path from a Review Map node --
// the callers that reach it, then entry point -> what it calls -> the storage
// or service boundary, with its exits and the error-handling sites it names --
// on either side of the change. Pure and deterministic over the same Map graph,
// architecture flow and code anchors the Change Story walks: nothing here parses
// source, fetches, or asks a model. A link is "observed" only when this
// Snapshot's recorded run traced it on the side shown; every other link is a
// possible/reachable relationship, never an execution order.
import {
  CHANGE_LABEL,
  EXACTNESS_LABEL,
  checksFor,
  edgeChange,
  nodeChange,
  storyExactness,
  type StoryChange,
  type StoryChecks,
  type StoryExactness,
} from "../changeStory/changeStoryModel";
import { snapshotIdentity } from "../investigation/investigationContext";
import { EDGE_KIND_LABEL, baseName, edgeKind, isTestNode, nodeName, visualEnds, type EdgeKind } from "../reviewMapModel";
import type {
  ReviewArchitectureFlowStep,
  ReviewArchitectureObservedFlow,
  ReviewMapEdge,
  ReviewMapGraph,
  ReviewMapNode,
  ReviewMapSelection,
  ReviewObservedCall,
  ReviewTarget,
} from "../types";

export type XRaySide = "before" | "after";
export type XRayRole = "entry" | "consumer" | "call" | "boundary" | "handoff" | "error";
export type XRayOrder = "observed" | "reachable";
export type XRayBoundaryKind = "storage" | "security" | "async" | "remote" | "other";
export type XRayExit = "boundary" | "handoff" | "end" | "depth" | "joins" | null;
export type XRayStatus = "ready" | "stale" | "missing_root" | "absent" | "not_measured";
export type XRayTone = "added" | "removed" | "changed" | "unchanged" | "unknown";

/** A change in shape and words as well as colour: + added, − removed, ~ changed, = unchanged, ? not measured. */
export interface XRayMark {
  glyph: "+" | "−" | "~" | "=" | "·" | "?";
  label: string;
  tone: XRayTone;
}

export interface XRayBoundary {
  kind: XRayBoundaryKind;
  label: string;
  /** "map": the Review Map's own boundary tag; "name": recognised from a path or symbol name only. */
  basis: "map" | "name";
  /** Async and remote handoffs: what runs after them is not traced, so the path stops. */
  handoff: boolean;
}

export interface XRayErrorHint {
  name: string;
  /** "trace": a recorded run called it; "name": recognised from its name, branch not analysed. */
  basis: "trace" | "name";
}

export interface XRayTrace {
  side: "base" | "head";
  recipe: string;
  callerFn: string;
  calleeFn: string;
  evidenceRef: string;
  recordId: string;
}

export interface XRayLink {
  edgeId: string;
  kind: string;
  sentence: string;
  change: StoryChange;
  mark: XRayMark;
  weights: { base: number; head: number } | null;
  exactness: StoryExactness;
  exactnessLabel: string;
  order: XRayOrder;
  orderLabel: string;
  /** First-seen position in this Snapshot's recorded run on the side shown; null unless observed in order. */
  sequence: number | null;
  /** Only a link observed in order on this Snapshot moves on screen. */
  animate: boolean;
  trace: XRayTrace | null;
  /** Set when the link hands off to a queue or another service instead of calling it directly. */
  handoff: XRayBoundary | null;
  /** Names the relationship lands on, from the Map's own anchors. */
  landsOn: string[];
  site: { path: string; line: number } | null;
  selection: ReviewMapSelection;
  uncertain: boolean;
}

export interface XRayStep {
  /** Unique per position on the path (a node can be reached once). */
  key: string;
  nodeId: string;
  parentKey: string | null;
  /** 0 for the entry point, 1+ downstream, -1 for callers that reach it. */
  depth: number;
  role: XRayRole;
  roleLabel: string;
  title: string;
  path: string;
  line: number;
  locationLabel: string;
  change: StoryChange;
  mark: XRayMark;
  exactness: StoryExactness;
  exactnessLabel: string;
  /** How this step is reached from its parent (or, for a caller, how it reaches the entry). */
  link: XRayLink | null;
  boundaries: XRayBoundary[];
  errorHints: XRayErrorHint[];
  exit: XRayExit;
  exitLabel: string;
  /** Links from this step left out by the depth or branching bound. */
  hiddenLinks: number;
  canOpenSource: boolean;
  selection: ReviewMapSelection;
  checks: StoryChecks | null;
}

export interface XRayNotMeasured {
  key: string;
  label: string;
  reason: string;
  /** The field the data would need for this to be measured. */
  needs: string;
}

export interface XRayTruncation {
  depth: number;
  branching: number;
  total: number;
  consumers: number;
  joins: number;
}

export interface XRay {
  status: XRayStatus;
  statusNote: string;
  side: XRaySide;
  rootId: string;
  rootTitle: string;
  /** Identity of the Snapshot the graph shows, and of the one the walk was opened on. */
  snapshot: string;
  walkSnapshot: string;
  consumers: XRayStep[];
  /** The entry point first, then the downstream path in reading (preorder) order. */
  steps: XRayStep[];
  comparison: "measured" | "not_measured";
  orderNote: string;
  observedLinks: number;
  reachableLinks: number;
  diff: { added: number; removed: number; changed: number; otherSideOnly: number; notCompared: number };
  truncation: XRayTruncation;
  degraded: string[];
  notMeasured: XRayNotMeasured[];
  errorBranches: number;
  handoffs: number;
  bounds: { depth: number; branching: number; steps: number; consumers: number };
}

export interface XRayFlow {
  applicable: boolean;
  steps: ReviewArchitectureFlowStep[];
  observed?: ReviewArchitectureObservedFlow;
}

export interface XRayInput {
  graph: ReviewMapGraph;
  flow?: XRayFlow | null;
  rootId: string;
  side: XRaySide;
  /** The Snapshot the walk was opened on; when the graph now shows another, the walk is stale. */
  walkSnapshot?: string;
  targets?: readonly ReviewTarget[] | null;
  maxDepth?: number;
  maxBranching?: number;
  maxSteps?: number;
  maxConsumers?: number;
}

// lc-debt: fixed path bounds (depth 5, 4 links per step, 30 steps, 4 callers); page or expand a branch on demand when reviewers need more.
export const XRAY_MAX_DEPTH = 5;
export const XRAY_MAX_BRANCHING = 4;
export const XRAY_MAX_STEPS = 30;
export const XRAY_MAX_CONSUMERS = 4;

// Only these come from the base-to-Snapshot comparison. "context_only" (current index) and
// "unknown" (a Snapshot fact) are this Snapshot's relationships with no base established.
const COMPARED: ReadonlySet<string> = new Set(["added", "removed", "changed", "unchanged"]);
const MOVED: ReadonlySet<StoryChange> = new Set(["added", "removed", "changed"]);
const KIND_FACTOR: Record<EdgeKind, number> = { impacts: 6, calls: 5, imports: 4, other: 2, references: 1 };

// lc-debt: async, remote and error-handling sites are recognised by path and symbol words only; replace with typed boundary and branch facts from the index when it records them.
const ASYNC_WORDS: ReadonlySet<string> = new Set([
  "queue", "queues", "worker", "workers", "job", "jobs", "celery", "kafka", "pubsub", "sqs", "sns", "rabbitmq",
  "amqp", "nats", "broker", "outbox", "webhook", "webhooks", "scheduler", "cron", "enqueue", "publisher", "subscriber",
]);
const REMOTE_WORDS: ReadonlySet<string> = new Set(["client", "clients", "http", "grpc", "rpc", "sdk", "remote", "gateway", "proxy"]);
const ERROR_WORDS: ReadonlySet<string> = new Set([
  "error", "errors", "err", "exception", "exceptions", "fail", "failed", "failure", "failures", "fallback", "rollback",
  "abort", "reject", "rejected", "panic", "raise", "recover",
]);
const SERVICE_ROOT = /^(services|apps)\/([^/]+)\//;

const ROLE_LABEL: Record<XRayRole, string> = {
  entry: "Entry point",
  consumer: "Reached from",
  call: "Calls / uses",
  boundary: "Storage boundary",
  handoff: "Handoff",
  error: "Error branch",
};

export const SIDE_LABEL: Record<XRaySide, string> = { before: "Before (base)", after: "After (this Snapshot)" };

function plural(count: number, one: string, many = one + "s"): string {
  return `${count} ${count === 1 ? one : many}`;
}

function words(text: string): string[] {
  return text.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}

function hasWord(text: string, set: ReadonlySet<string>): boolean {
  return words(text).some((word) => set.has(word));
}

/** The Snapshot a Map graph belongs to, from its own selections; "" when it names none. */
export function graphSnapshot(graph: ReviewMapGraph): string {
  const selection = graph.nodes.find((node) => node.selection?.review_id && node.selection?.revision_id)?.selection;
  return selection ? snapshotIdentity(selection.review_id, selection.revision_id) : "";
}

/** Whether the Map's relationships carry a base-to-Snapshot comparison at all. */
export function comparisonMeasured(graph: ReviewMapGraph): boolean {
  return graph.edges.some(edgeCompared);
}

/** Whether this relationship's base presence comes from the base-to-Snapshot comparison. */
function edgeCompared(edge: ReviewMapEdge): boolean {
  const status = edge.delta_status;
  if (status !== undefined && status !== "") return COMPARED.has(String(status));
  return typeof edge.base_weight === "number" && typeof edge.snapshot_weight === "number";
}

function edgeOnSide(edge: ReviewMapEdge, side: XRaySide): boolean {
  const status = String(edge.delta_status ?? "");
  if (status === "added") return side === "after";
  if (status === "removed") return side === "before";
  if (status === "changed" || status === "unchanged") return true;
  if (!status && typeof edge.base_weight === "number" && typeof edge.snapshot_weight === "number") {
    return (side === "before" ? edge.base_weight : edge.snapshot_weight) > 0;
  }
  // Context and Snapshot facts without a comparison are this Snapshot's only; their base is not measured.
  return side === "after";
}

function nodeOnSide(node: ReviewMapNode, side: XRaySide): boolean {
  const change = nodeChange(node);
  if (change === "added") return side === "after";
  if (change === "removed") return side === "before";
  return true;
}

export function changeMark(change: StoryChange, side: XRaySide): XRayMark {
  switch (change) {
    case "added":
      return { glyph: "+", label: "added", tone: "added" };
    case "removed":
      return { glyph: "−", label: side === "before" ? "removed by this change" : "removed", tone: "removed" };
    case "changed":
      return { glyph: "~", label: "changed", tone: "changed" };
    case "unchanged":
      return { glyph: "=", label: "unchanged", tone: "unchanged" };
    case "context_only":
      return { glyph: "?", label: `${CHANGE_LABEL.context_only}, not compared with the base`, tone: "unknown" };
    default:
      return { glyph: "?", label: "change not measured", tone: "unknown" };
  }
}

function mapBoundaries(node: ReviewMapNode): XRayBoundary[] {
  return (node.boundaries ?? []).map((value): XRayBoundary => {
    if (value === "persistence") return { kind: "storage", label: "storage boundary", basis: "map", handoff: false };
    if (value === "security") return { kind: "security", label: "security boundary", basis: "map", handoff: false };
    return { kind: "other", label: `${value} boundary`, basis: "map", handoff: false };
  });
}

/** An async or remote handoff on the link into *to*, recognised from names only. */
function handoffFor(from: ReviewMapNode | undefined, to: ReviewMapNode): XRayBoundary | null {
  const text = `${to.path} ${to.label}`;
  if (hasWord(text, ASYNC_WORDS)) {
    return { kind: "async", label: "possible async/queue handoff", basis: "name", handoff: true };
  }
  const fromService = from?.path.match(SERVICE_ROOT)?.[2];
  const toService = to.path.match(SERVICE_ROOT)?.[2];
  if (fromService && toService && fromService !== toService) {
    return { kind: "remote", label: `possible remote handoff · ${fromService} → ${toService} service`, basis: "name", handoff: true };
  }
  if (hasWord(text, REMOTE_WORDS)) {
    return { kind: "remote", label: "possible remote-service handoff", basis: "name", handoff: true };
  }
  return null;
}

interface Link {
  edge: ReviewMapEdge;
  other: string;
  score: number;
}

function emptyXRay(input: XRayInput, status: XRayStatus, statusNote: string, extra: Partial<XRay> = {}): XRay {
  const root = input.graph.nodes.find((node) => node.id === input.rootId);
  return {
    status,
    statusNote,
    side: input.side,
    rootId: input.rootId,
    rootTitle: root ? nodeName(root) : input.rootId,
    snapshot: graphSnapshot(input.graph),
    walkSnapshot: input.walkSnapshot ?? graphSnapshot(input.graph),
    consumers: [],
    steps: [],
    comparison: comparisonMeasured(input.graph) ? "measured" : "not_measured",
    orderNote: "",
    observedLinks: 0,
    reachableLinks: 0,
    diff: { added: 0, removed: 0, changed: 0, otherSideOnly: 0, notCompared: 0 },
    truncation: { depth: 0, branching: 0, total: 0, consumers: 0, joins: 0 },
    degraded: [],
    notMeasured: [],
    errorBranches: 0,
    handoffs: 0,
    bounds: {
      depth: input.maxDepth ?? XRAY_MAX_DEPTH,
      branching: input.maxBranching ?? XRAY_MAX_BRANCHING,
      steps: input.maxSteps ?? XRAY_MAX_STEPS,
      consumers: input.maxConsumers ?? XRAY_MAX_CONSUMERS,
    },
    ...extra,
  };
}

const BASE_NOT_MEASURED: XRayNotMeasured = {
  key: "base_side",
  label: "Before (base) relationships",
  reason: "None of this Map's relationships comes from a base-to-Snapshot comparison (they are current-index context or Snapshot facts only), so the base path cannot be drawn.",
  needs: "delta_status, or base_weight and snapshot_weight, on the Map's edges",
};

/** Builds the X-Ray for one side of the change; bounded, and stale when the Snapshot moved on. */
export function buildXRay(input: XRayInput): XRay {
  const { graph, side } = input;
  const maxDepth = Math.max(0, Math.min(12, input.maxDepth ?? XRAY_MAX_DEPTH));
  const maxBranching = Math.max(1, input.maxBranching ?? XRAY_MAX_BRANCHING);
  const maxSteps = Math.max(1, input.maxSteps ?? XRAY_MAX_STEPS);
  const maxConsumers = Math.max(0, input.maxConsumers ?? XRAY_MAX_CONSUMERS);
  const snapshot = graphSnapshot(graph);
  const walkSnapshot = input.walkSnapshot ?? snapshot;
  if (walkSnapshot && snapshot && walkSnapshot !== snapshot) {
    return emptyXRay(input, "stale", "A newer Snapshot replaced the one this X-Ray was built on, so its path is not shown. Rebuild it on the Snapshot shown now.");
  }
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const root = byId.get(input.rootId);
  if (!root) return emptyXRay(input, "missing_root", "This file is not on the Map shown now, so there is no path to draw from it.");
  const measured = comparisonMeasured(graph);
  if (side === "before" && !measured) {
    return emptyXRay(input, "not_measured", "Before is not measured on this Map: its relationships carry no base-to-Snapshot comparison.", { notMeasured: [BASE_NOT_MEASURED] });
  }
  if (!nodeOnSide(root, side)) {
    return emptyXRay(input, "absent", side === "before"
      ? "This file is new in this Snapshot, so it has no Before path. Switch to After."
      : "This file is removed by this change, so it has no After path. Switch to Before.");
  }

  const traceSide = side === "before" ? "base" : "head";
  const flowSteps = input.flow?.applicable ? input.flow.steps : [];
  const flowByEdge = new Map(flowSteps.map((step) => [step.edge_id, step]));
  const observedCalls: ReviewObservedCall[] = input.flow?.observed?.available ? input.flow.observed[traceSide] ?? [] : [];
  const callAt = new Map<string, number>();
  observedCalls.forEach((call, at) => {
    const key = `${call.from}\u0000${call.to}`;
    if (!callAt.has(key)) callAt.set(key, at);
  });
  const observedFor = (edge: ReviewMapEdge, from: ReviewMapNode, to: ReviewMapNode): { sequence: number | null; trace: XRayTrace } | null => {
    const flow = flowByEdge.get(edge.id);
    const stepTrace = flow?.grounding === "observed" && flow.trace && flow.trace.side === traceSide ? flow.trace : null;
    const at = callAt.get(`${from.path}\u0000${to.path}`);
    const call = at === undefined ? null : observedCalls[at];
    if (!stepTrace && !call) return null;
    return {
      sequence: at === undefined ? null : at + 1,
      trace: {
        side: traceSide,
        recipe: stepTrace?.recipe ?? "",
        callerFn: stepTrace?.caller_fn ?? call?.from_fn ?? "",
        calleeFn: stepTrace?.callee_fn ?? call?.to_fn ?? "",
        evidenceRef: stepTrace?.evidence_ref ?? call?.evidence_ref ?? "",
        recordId: stepTrace?.record_id ?? "",
      },
    };
  };

  const present = (id: string) => {
    const node = byId.get(id);
    return Boolean(node && nodeOnSide(node, side));
  };
  const dependencies = new Map<string, Link[]>();
  const dependents = new Map<string, Link[]>();
  for (const edge of graph.edges) {
    if (edge.source === edge.target || !present(edge.source) || !present(edge.target) || !edgeOnSide(edge, side)) continue;
    const { from, to } = visualEnds(edge);
    const fromNode = byId.get(from)!;
    const toNode = byId.get(to)!;
    const score = (MOVED.has(edgeChange(edge)) ? 1e6 : 0)
      + (observedFor(edge, fromNode, toNode) ? 1e5 : 0)
      + (edge.exactness === "snapshot" ? 1e4 : 0)
      + KIND_FACTOR[edgeKind(edge.kind)] * 100
      + Math.min(99, edge.weight || 0);
    // Tests use the code they cover but are not how behaviour is reached: they rank after real callers.
    (dependencies.get(from) ?? dependencies.set(from, []).get(from)!).push({ edge, other: to, score: score + (isTestNode(toNode) ? 0 : 1e7) });
    (dependents.get(to) ?? dependents.set(to, []).get(to)!).push({ edge, other: from, score: score + (isTestNode(fromNode) ? 0 : 1e7) });
  }
  const byScore = (a: Link, b: Link) => b.score - a.score || a.other.localeCompare(b.other);
  for (const list of dependencies.values()) list.sort(byScore);
  for (const list of dependents.values()) list.sort(byScore);

  const targets = input.targets ? new Map(input.targets.map((target) => [target.target_id, target])) : null;

  const linkOf = (edge: ReviewMapEdge): XRayLink => {
    const { from, to } = visualEnds(edge);
    const fromNode = byId.get(from)!;
    const toNode = byId.get(to)!;
    const kind = edgeKind(edge.kind);
    const change = edgeChange(edge);
    const observed = observedFor(edge, fromNode, toNode);
    const exactness: StoryExactness = observed ? "observed" : storyExactness(edge.exactness);
    const handoff = handoffFor(fromNode, toNode);
    const site = edge.evidence?.find((item) => item.path && item.line > 0) ?? null;
    const landsOn = [
      ...(edge.symbols ?? []).map((symbol) => symbol.name),
      ...(edge.evidence ?? []).map((item) => item.symbol ?? ""),
    ].filter((name, index, all) => name && all.indexOf(name) === index).slice(0, 4);
    return {
      edgeId: edge.id,
      kind: edge.kind,
      sentence: `${nodeName(fromNode)} ${EDGE_KIND_LABEL[kind]} ${nodeName(toNode)}`,
      change,
      mark: changeMark(change, side),
      weights: typeof edge.base_weight === "number" && typeof edge.snapshot_weight === "number"
        ? { base: edge.base_weight, head: edge.snapshot_weight }
        : null,
      exactness,
      exactnessLabel: EXACTNESS_LABEL[exactness],
      order: observed ? "observed" : "reachable",
      orderLabel: observed
        ? observed.sequence !== null
          ? `Observed: call #${observed.sequence} first seen in this Snapshot's recorded ${traceSide} runs`
          : `Observed in this Snapshot's recorded ${traceSide} run; its position in the run is not recorded`
        : "Possible/reachable: a recorded relationship, not an execution order",
      sequence: observed?.sequence ?? null,
      animate: Boolean(observed && observed.sequence !== null),
      trace: observed?.trace ?? null,
      handoff,
      landsOn,
      site: site ? { path: site.path, line: site.line } : null,
      selection: edge.selection,
      uncertain: edge.uncertain || Boolean(edge.evidence?.some((item) => item.uncertainty)),
    };
  };

  const errorHintsFor = (node: ReviewMapNode, link: XRayLink | null): XRayErrorHint[] => {
    const out: XRayErrorHint[] = [];
    const add = (name: string, basis: XRayErrorHint["basis"]) => {
      if (name && hasWord(name, ERROR_WORDS) && !out.some((hint) => hint.name === name)) out.push({ name, basis });
    };
    if (link?.trace) add(link.trace.calleeFn, "trace");
    for (const name of link?.landsOn ?? []) add(name, "name");
    for (const symbol of node.symbols ?? []) add(symbol.label, "name");
    return out.slice(0, 4);
  };

  const stepOf = (node: ReviewMapNode, edge: ReviewMapEdge | null, depth: number, parentKey: string | null, consumer = false): XRayStep => {
    const link = edge ? linkOf(edge) : null;
    const boundaries = mapBoundaries(node);
    if (link?.handoff && !consumer) boundaries.push(link.handoff);
    const storage = boundaries.some((item) => item.kind === "storage");
    const errorNamed = hasWord(`${baseName(node.path)} ${node.label}`, ERROR_WORDS);
    const role: XRayRole = consumer ? "consumer"
      : depth === 0 ? "entry"
        : link?.handoff ? "handoff"
          : storage ? "boundary"
            : errorNamed ? "error"
              : "call";
    const change = nodeChange(node);
    const exactness = storyExactness(node.exactness);
    const line = node.selection?.line ?? node.line ?? 0;
    const exit: XRayExit = consumer || depth === 0 ? null : role === "handoff" ? "handoff" : role === "boundary" ? "boundary" : null;
    return {
      key: `${parentKey ?? (consumer ? "in" : "")}>${node.id}`,
      nodeId: node.id,
      parentKey,
      depth,
      role,
      roleLabel: role === "handoff" && link?.handoff ? (link.handoff.kind === "async" ? "Async handoff" : "Remote handoff") : ROLE_LABEL[role],
      title: nodeName(node),
      path: node.path,
      line,
      locationLabel: `${node.path}${line > 0 ? `:${line}` : ""}`,
      change,
      mark: changeMark(change, side),
      exactness,
      exactnessLabel: EXACTNESS_LABEL[exactness],
      link,
      boundaries,
      errorHints: errorHintsFor(node, link),
      exit,
      exitLabel: exit === "handoff"
        ? link?.handoff?.kind === "async"
          ? "Async handoff: delivery, timing and what runs next are not traced, so the path stops here."
          : "Remote handoff: the other service's behaviour is not traced here, so the path stops."
        : exit === "boundary" ? "Reaches a storage boundary; the path stops here." : "",
      hiddenLinks: 0,
      canOpenSource: exactness === "snapshot" && Boolean(node.path),
      selection: node.selection,
      checks: checksFor(node, targets),
    };
  };

  const truncation: XRayTruncation = { depth: 0, branching: 0, total: 0, consumers: 0, joins: 0 };
  const shown = new Set<string>([root.id]);
  const entry = stepOf(root, null, 0, null);
  const steps: XRayStep[] = [entry];
  const expand = (step: XRayStep) => {
    if (step.exit === "handoff" || step.exit === "boundary") return;
    const links = dependencies.get(step.nodeId) ?? [];
    const fresh = links.filter((link) => !shown.has(link.other));
    truncation.joins += links.length - fresh.length;
    if (!links.length) {
      step.exit = "end";
      step.exitLabel = `Path ends: no further recorded relationship on the ${side === "before" ? "Before" : "After"} side.`;
      return;
    }
    if (!fresh.length) {
      step.exit = "joins";
      step.exitLabel = "Continues only into steps already shown.";
      return;
    }
    if (step.depth >= maxDepth) {
      truncation.depth += fresh.length;
      step.hiddenLinks = fresh.length;
      step.exit = "depth";
      step.exitLabel = `${plural(fresh.length, "more link")} past the depth bound of ${maxDepth}, not shown.`;
      return;
    }
    const taken = fresh.slice(0, maxBranching);
    step.hiddenLinks = fresh.length - taken.length;
    truncation.branching += step.hiddenLinks;
    for (const link of taken) {
      if (shown.has(link.other)) {
        truncation.joins += 1;
        continue;
      }
      if (steps.length >= maxSteps) {
        truncation.total += 1;
        continue;
      }
      shown.add(link.other);
      const child = stepOf(byId.get(link.other)!, link.edge, step.depth + 1, step.key);
      steps.push(child);
      expand(child);
    }
  };
  expand(entry);

  const callers = (dependents.get(root.id) ?? []).filter((link) => !shown.has(link.other));
  const consumers = callers.slice(0, maxConsumers).map((link) => stepOf(byId.get(link.other)!, link.edge, -1, null, true));
  truncation.consumers = callers.length - consumers.length;

  const links = [...consumers, ...steps].map((step) => step.link).filter((link): link is XRayLink => Boolean(link));
  const observedLinks = links.filter((link) => link.order === "observed").length;
  const reachableLinks = links.length - observedLinks;
  const shownNodes = new Set(steps.map((step) => step.nodeId));
  const other: XRaySide = side === "before" ? "after" : "before";
  const outgoing = graph.edges.filter((edge) => edge.source !== edge.target && shownNodes.has(visualEnds(edge).from));
  const otherSideOnly = outgoing.filter((edge) => edgeCompared(edge) && edgeOnSide(edge, other) && !edgeOnSide(edge, side)).length;
  const pathLinks = steps.map((step) => step.link).filter((link): link is XRayLink => Boolean(link));
  const diff = {
    added: pathLinks.filter((link) => link.change === "added").length,
    removed: pathLinks.filter((link) => link.change === "removed").length,
    changed: pathLinks.filter((link) => link.change === "changed").length,
    otherSideOnly,
    // This Snapshot's links from these files whose base presence the comparison does not establish.
    notCompared: outgoing.filter((edge) => !edgeCompared(edge)).length,
  };

  const sideWord = side === "before" ? "Before" : "After";
  const orderNote = observedLinks === 0
    ? `No recorded run traced this path on the ${sideWord} side. Every link is a possible/reachable relationship, not an execution order.`
    : `${observedLinks} of ${plural(links.length, "link")} observed in this Snapshot's recorded ${traceSide} runs; the rest are possible/reachable relationships, not an execution order.`;

  const degraded: string[] = [];
  if (graph.truncated) {
    degraded.push(`The Map is bounded${graph.omitted_node_count ? `: ${plural(graph.omitted_node_count, "file")} left out` : ""}, so links to files outside it are not shown.`);
  }
  const inexact = [...consumers, ...steps].filter((step) => step.exactness !== "snapshot" || (step.link && step.link.exactness !== "snapshot" && step.link.exactness !== "observed")).length;
  if (inexact) degraded.push(`${plural(inexact, "step")} rest on the current index or inference, not this Snapshot.`);
  const uncertain = links.filter((link) => link.uncertain).length;
  if (uncertain) degraded.push(`${plural(uncertain, "link")} the index itself marks uncertain.`);
  if (graph.reason) degraded.push(graph.reason);

  const errorBranches = steps.filter((step) => step.role === "error" || step.errorHints.length > 0).length;
  const handoffs = steps.filter((step) => step.role === "handoff").length;
  const notMeasured: XRayNotMeasured[] = [];
  if (!measured) notMeasured.push(BASE_NOT_MEASURED);
  if (!observedLinks) {
    notMeasured.push({
      key: "observed_order",
      label: "Observed order",
      reason: `No recorded run of this Snapshot traced the ${sideWord} side of this path, so nothing here is a runtime order.`,
      needs: `a runtime-observation run whose trace lands on these files (flow.observed.${traceSide})`,
    });
  }
  notMeasured.push({
    key: "error_branches",
    label: "Error branches",
    reason: errorBranches
      ? "Error-handling sites here are recognised by name or a recorded call; which branch runs, and when, is not analysed."
      : "No error-handling site is named on this path, and the Map records relationships, not control flow.",
    needs: "branch facts (raise / catch / error-return sites) on Map edges",
  });
  if (graph.level !== "symbol") {
    notMeasured.push({
      key: "call_order",
      label: "Function-level calls inside each step",
      reason: "This Map relates files; which function calls which, on each side, is not recorded at this level.",
      needs: "symbol-level edges with delta_status for both sides",
    });
  }

  return {
    status: "ready",
    statusNote: "",
    side,
    rootId: root.id,
    rootTitle: nodeName(root),
    snapshot,
    walkSnapshot,
    consumers,
    steps,
    comparison: measured ? "measured" : "not_measured",
    orderNote,
    observedLinks,
    reachableLinks,
    diff,
    truncation,
    degraded,
    notMeasured,
    errorBranches,
    handoffs,
    bounds: { depth: maxDepth, branching: maxBranching, steps: maxSteps, consumers: maxConsumers },
  };
}

/** Where an X-Ray stands, as one URL value: `side~snapshot~node`. */
export interface XRayLocation {
  side: XRaySide;
  snapshot: string;
  rootId: string;
}

export function encodeXRayLocation(location: XRayLocation): string {
  return `${location.side}~${location.snapshot}~${location.rootId}`;
}

export function decodeXRayLocation(value: string | null | undefined): XRayLocation | null {
  if (!value) return null;
  const first = value.indexOf("~");
  const second = first < 0 ? -1 : value.indexOf("~", first + 1);
  if (second < 0) return null;
  const side = value.slice(0, first);
  const rootId = value.slice(second + 1);
  if ((side !== "before" && side !== "after") || !rootId) return null;
  return { side, snapshot: value.slice(first + 1, second), rootId };
}
