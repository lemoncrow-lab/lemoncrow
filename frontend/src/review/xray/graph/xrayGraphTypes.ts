/**
 * The X-Ray graph document, contract v1 (docs/reference/xray-graph.md). The backend produces one per Review revision;
 * this renderer only reads it. Keep these in step with the contract: change both sides together.
 */

export type XRayDelta = "added" | "modified" | "removed" | "unchanged";
export type XRayEvidence = "recorded" | "static" | "inferred";
export type XRayGraphSide = "base" | "head";

export interface XRayAnchor {
  path: string;
  start_line?: number;
  end_line?: number;
  side: XRayGraphSide;
}

export interface XRayRecordedRun {
  ref: string;
  recipe: string;
  side: XRayGraphSide;
  at: string;
}

export interface XRayLane {
  id: string;
  label: string;
  platform?: string;
}

export type XRayNodeKind =
  | "ui"
  | "route"
  | "handler"
  | "function"
  | "class"
  | "module"
  | "datastore"
  | "queue"
  | "external"
  | "job"
  | "config"
  | "test";

export interface XRayNode {
  id: string;
  label: string;
  kind: XRayNodeKind;
  lane: string;
  delta: XRayDelta;
  subtitle?: string;
  badges: string[];
  anchors: XRayAnchor[];
  source: XRayEvidence;
}

export type XRayEdgeKind = "http" | "call" | "data" | "event" | "dependency" | "render";

export interface XRayEdge {
  id: string;
  from: string;
  to: string;
  kind: XRayEdgeKind;
  label?: string;
  delta: XRayDelta;
  evidence: XRayEvidence;
  hero?: boolean;
  anchors: XRayAnchor[];
  recorded?: { ref: string; count: number };
}

export interface XRayPayload {
  type?: string;
  shape?: string;
  before?: unknown;
  after?: unknown;
  status?: { before?: number; after?: number };
  evidence: "recorded" | "inferred";
  evidence_ref?: string;
  behavior_key?: string;
  redacted: boolean;
}

export type XRayStepKind = "sync" | "async" | "return" | "self";

export interface XRayFlowStep {
  id: string;
  from: string;
  to: string;
  label: string;
  kind: XRayStepKind;
  delta: XRayDelta;
  evidence: XRayEvidence;
  repeat?: number;
  note?: string;
  anchors: XRayAnchor[];
  payload?: { request?: XRayPayload; response?: XRayPayload };
  /** The run that observed this step, and how many times; a recorded step may name it. */
  recorded?: { ref: string; count: number };
}

export interface XRayGraphFlow {
  id: string;
  title: string;
  summary?: string;
  participants: { node: string; label?: string }[];
  steps: XRayFlowStep[];
}

export type XRayViewScope = { kind: "all" } | { kind: "nodes"; nodes: string[] } | { kind: "flow"; flow: string };

export interface XRayView {
  id: string;
  title: string;
  lens: "architecture" | "flow";
  summary?: string;
  scope: XRayViewScope;
  children: XRayView[];
}

export type XRayTourFocus =
  | { kind: "all" }
  | { kind: "selection"; nodes?: string[]; edges?: string[]; messages?: string[] };

export interface XRayTourStep {
  id: string;
  heading: string;
  body: string;
  stage: { kind: "view"; view: string } | { kind: "flow"; flow: string };
  focus: XRayTourFocus;
}

export interface XRayGraph {
  version: 1;
  review_id: string;
  revision_id: string;
  digest: string;
  title: string;
  summary: string;
  provenance: {
    producer: "index" | "index+ai";
    model?: string;
    generated_at: string;
    recorded_runs: XRayRecordedRun[];
  };
  lanes: XRayLane[];
  nodes: XRayNode[];
  edges: XRayEdge[];
  flows: XRayGraphFlow[];
  views: XRayView[];
  walkthrough?: { steps: XRayTourStep[] };
  degraded: string[];
}

/** `GET /api/reviews/{review_id}/xray-graph?revision_id=<id>`. */
export interface XRayGraphResponse {
  state: "ready" | "pending" | "unavailable";
  graph?: XRayGraph;
  reason?: string;
}
