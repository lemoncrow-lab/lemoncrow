// Review Map graphs (and architecture flows) for the investigation fixtures, so
// path-shaped work (N01 X-Ray) runs over the same pinned changes as N00/N05.
// Built from the real Map types; deterministic, nothing random or time-bound.
import type {
  ReviewArchitectureFlowStep,
  ReviewArchitectureObservedFlow,
  ReviewMapEdge,
  ReviewMapGraph,
  ReviewMapNode,
} from "../types";
import { INVESTIGATION_FIXTURES, type InvestigationFixture } from "./fixtures";

export interface InvestigationMapFixture {
  fixture: InvestigationFixture;
  graph: ReviewMapGraph;
  flow: { applicable: boolean; kind: string; reason: string; steps: ReviewArchitectureFlowStep[]; observed?: ReviewArchitectureObservedFlow };
}

interface Ids {
  review: string;
  revision: string;
}

export function fixtureMapNode(ids: Ids, id: string, path: string, overrides: Partial<ReviewMapNode> = {}): ReviewMapNode {
  const status = overrides.status ?? "changed";
  const exactness = overrides.exactness ?? (status === "context" ? "current_index" : "snapshot");
  return {
    id,
    label: path.split("/").pop() ?? path,
    kind: "file",
    path,
    line: 0,
    end_line: 0,
    status,
    exactness,
    target_ids: [],
    important_public_contract: false,
    boundaries: [],
    boundary_basis: "",
    selection: {
      kind: "review_map_node",
      review_id: ids.review,
      revision_id: ids.revision,
      node_id: id,
      path,
      line: 1,
      side: overrides.delta_status === "removed" ? "old" : "new",
      exactness,
    },
    ...overrides,
  };
}

export function fixtureMapEdge(ids: Ids, source: string, target: string, overrides: Partial<ReviewMapEdge> = {}): ReviewMapEdge {
  const id = overrides.id ?? `${source}->${target}`;
  const exactness = overrides.exactness ?? "snapshot";
  return {
    id,
    source,
    target,
    kind: "calls",
    exactness,
    weight: 1,
    uncertain: false,
    selection: { kind: "review_map_edge", review_id: ids.review, revision_id: ids.revision, edge_id: id, exactness },
    ...overrides,
  };
}

function symbol(path: string, label: string, line: number) {
  return { id: `${path}::${label}`, label, qualified_name: label, path, line, end_line: line + 12, change: "modified", caller_count: 1, usage_count: 1, centrality_rank: null, centrality_percentile: null };
}

function graph(nodes: ReviewMapNode[], edges: ReviewMapEdge[]): ReviewMapGraph {
  return { level: "architecture", focus: null, nodes, edges, truncated: false };
}

/*
 * (a) Payment retry: checkout -> retry -> idempotency key (added, observed),
 * provider client (remote, observed), ledger store (changed weight), a
 * settlement queue (added async handoff); the old backoff module is base only.
 */
function paymentRetryMap(): InvestigationMapFixture {
  const fixture = INVESTIGATION_FIXTURES.paymentRetry();
  const ids = { review: fixture.projection.review_id, revision: fixture.revision.id };
  const node = (id: string, path: string, overrides: Partial<ReviewMapNode> = {}) => fixtureMapNode(ids, id, path, overrides);
  const edge = (source: string, target: string, overrides: Partial<ReviewMapEdge> = {}) => fixtureMapEdge(ids, source, target, overrides);
  const nodes = [
    node("checkout", "src/checkout/charge_route.py", { status: "context", exactness: "snapshot", delta_status: "unchanged" }),
    node("retry", "src/payments/retry.py", { delta_status: "changed", target_ids: ["pay-retry"], symbols: [symbol("src/payments/retry.py", "charge_with_retry", 10)] }),
    node("key", "src/payments/idempotency.py", { delta_status: "changed", target_ids: ["pay-key"] }),
    node("provider", "src/payments/provider_client.py", { status: "context", exactness: "snapshot", delta_status: "unchanged" }),
    node("ledger", "src/payments/ledger_store.py", { status: "context", exactness: "snapshot", delta_status: "unchanged", boundaries: ["persistence"], boundary_basis: "path" }),
    node("settle", "src/payments/settlement_queue.py", { status: "new", delta_status: "added" }),
    node("backoff", "src/payments/legacy_backoff.py", { delta_status: "removed", change: "D" }),
    node("test", "tests/payments/test_retry.py", { category: "test", delta_status: "changed", target_ids: ["pay-test"] }),
  ];
  const edges = [
    edge("checkout", "retry", { delta_status: "unchanged", base_weight: 1, snapshot_weight: 1, symbols: [{ name: "charge_with_retry", count: 1 }] }),
    edge("retry", "key", { delta_status: "added", base_weight: 0, snapshot_weight: 2, evidence: [{ kind: "call", path: "src/payments/retry.py", line: 14, uncertainty: "", symbol: "idempotency_key" }] }),
    edge("retry", "provider", { delta_status: "unchanged", base_weight: 1, snapshot_weight: 1, symbols: [{ name: "post_charge", count: 1 }] }),
    edge("retry", "ledger", { delta_status: "changed", base_weight: 1, snapshot_weight: 2 }),
    edge("retry", "settle", { delta_status: "added", base_weight: 0, snapshot_weight: 1, symbols: [{ name: "enqueue_settlement", count: 1 }] }),
    edge("retry", "backoff", { delta_status: "removed", base_weight: 1, snapshot_weight: 0 }),
    edge("test", "retry", { delta_status: "changed", base_weight: 1, snapshot_weight: 2 }),
  ];
  const flowStep = (e: ReviewMapEdge, from: string, to: string, trace: ReviewArchitectureFlowStep["trace"]): ReviewArchitectureFlowStep => ({
    edge_id: e.id,
    from,
    to,
    relationship: e.kind,
    delta_status: String(e.delta_status),
    base_weight: e.base_weight ?? 0,
    snapshot_weight: e.snapshot_weight ?? 0,
    grounding: trace ? "observed" : "inferred",
    trace,
  });
  const call = (from: string, fromFn: string, to: string, toFn: string) => ({ from, from_fn: fromFn, to, to_fn: toFn, evidence_ref: `evidence:${fromFn}->${toFn}` });
  const head = [
    call("src/checkout/charge_route.py", "charge", "src/payments/retry.py", "charge_with_retry"),
    call("src/payments/retry.py", "charge_with_retry", "src/payments/idempotency.py", "idempotency_key"),
    call("src/payments/retry.py", "charge_with_retry", "src/payments/provider_client.py", "post_charge"),
  ];
  const base = [
    call("src/checkout/charge_route.py", "charge", "src/payments/retry.py", "charge_with_retry"),
    call("src/payments/retry.py", "charge_with_retry", "src/payments/legacy_backoff.py", "sleep_for"),
    call("src/payments/retry.py", "charge_with_retry", "src/payments/provider_client.py", "post_charge"),
  ];
  return {
    fixture,
    graph: graph(nodes, edges),
    flow: {
      applicable: true,
      kind: "changed_relationship_flow",
      reason: "",
      steps: [
        flowStep(edges[1], "src/payments/retry.py", "src/payments/idempotency.py", {
          side: "head", caller_fn: "charge_with_retry", callee_fn: "idempotency_key", recipe: "pytest tests/payments", evidence_ref: "evidence:retry-key", record_id: "record-pay-1",
        }),
        flowStep(edges[4], "src/payments/retry.py", "src/payments/settlement_queue.py", null),
        flowStep(edges[5], "src/payments/retry.py", "src/payments/legacy_backoff.py", null),
      ],
      observed: { available: true, comparable: true, grounded_steps: 1, inferred_steps: 2, base, head, added: [head[1]], removed: [base[1]], note: "" },
    },
  };
}

/*
 * (b) Auth refresh with no recorded run: the http client uses refresh, which
 * reads the token store, calls the identity provider client and, new in this
 * Snapshot, a session-error handler.
 */
function authRefreshMap(): InvestigationMapFixture {
  const fixture = INVESTIGATION_FIXTURES.authRefresh();
  const ids = { review: fixture.projection.review_id, revision: fixture.revision.id };
  const node = (id: string, path: string, overrides: Partial<ReviewMapNode> = {}) => fixtureMapNode(ids, id, path, overrides);
  const edge = (source: string, target: string, overrides: Partial<ReviewMapEdge> = {}) => fixtureMapEdge(ids, source, target, overrides);
  const nodes = [
    node("client", "src/http/client.ts", { delta_status: "changed", target_ids: ["auth-client"] }),
    node("refresh", "src/auth/refresh.ts", {
      delta_status: "changed",
      target_ids: ["auth-refresh", "auth-error"],
      boundaries: ["security"],
      boundary_basis: "path",
      symbols: [symbol("src/auth/refresh.ts", "refreshToken", 10), symbol("src/auth/refresh.ts", "handleRefreshError", 40)],
    }),
    node("tokens", "src/auth/token_store.ts", { status: "context", exactness: "snapshot", delta_status: "unchanged", boundaries: ["security", "persistence"], boundary_basis: "path" }),
    node("idp", "src/auth/identity_client.ts", { status: "context", exactness: "current_index", delta_status: "unchanged" }),
    node("errors", "src/auth/session_errors.ts", { status: "new", delta_status: "added" }),
  ];
  const edges = [
    edge("client", "refresh", { delta_status: "changed", base_weight: 1, snapshot_weight: 2, symbols: [{ name: "refreshToken", count: 2 }] }),
    edge("refresh", "tokens", { delta_status: "unchanged", base_weight: 2, snapshot_weight: 2 }),
    // A current-index relationship: context for this Snapshot, never a base comparison.
    edge("refresh", "idp", { delta_status: "context_only", base_weight: 0, snapshot_weight: 0, exactness: "current_index" }),
    edge("refresh", "errors", { delta_status: "added", base_weight: 0, snapshot_weight: 1, symbols: [{ name: "handleRefreshError", count: 1 }] }),
  ];
  return {
    fixture,
    graph: graph(nodes, edges),
    flow: { applicable: false, kind: "none", reason: "The exact relationship delta is too small for a useful sequence/flow explanation.", steps: [] },
  };
}

export const INVESTIGATION_MAP_FIXTURES = { paymentRetryMap, authRefreshMap } as const;
