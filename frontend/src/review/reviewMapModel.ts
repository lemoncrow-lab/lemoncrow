// The Review Map is the shared map engine (map/) applied to one Snapshot.
// Review-typed views of the engine's generic layout types live here so Review
// code reads Snapshot fields (selection, provenance) without casts.
import type * as Engine from "../map/mapModel";
import { changeLetter, isChangedStatus, isTestNode, relationshipsPending, visualEnds } from "../map/mapModel";
import type { ReviewMapEdge, ReviewMapNode, ReviewMapPayload } from "./types";

export * from "../map/mapModel";

export type MapCardLayout = Engine.MapCardLayout<ReviewMapNode>;
export type MapEdgeLayout = Engine.MapEdgeLayout<ReviewMapEdge>;
export type MapModel = Engine.MapModel<ReviewMapNode, ReviewMapEdge>;
export type Relationship = Engine.Relationship<ReviewMapNode, ReviewMapEdge>;
export type SearchHit = Engine.SearchHit<ReviewMapNode>;

/**
 * Whether a test on this map reaches a changed file. `none` is claimed only
 * when the map's relationships were measured and nothing was left off it;
 * otherwise a missing link says nothing, and the answer is `unmeasured`.
 */
export interface TestReach {
  state: "reached" | "none" | "unmeasured";
  tests: ReviewMapNode[];
}

/** How the Map words test reach; "not measured" never reads as "no tests". */
export function testReachText(state: TestReach["state"]): string {
  if (state === "none") return "No test on this map reaches it";
  if (state === "unmeasured") return "Test reach not measured";
  return "Reached by a test";
}

export function testReach(payload: Pick<ReviewMapPayload, "context" | "graph" | "snapshot">): Map<string, TestReach> {
  const byId = new Map(payload.graph.nodes.map((node) => [node.id, node]));
  const reached = new Map<string, Map<string, ReviewMapNode>>();
  for (const edge of payload.graph.edges) {
    const { from, to } = visualEnds(edge);
    const test = byId.get(from);
    if (!test || !isTestNode(test) || from === to) continue;
    const tests = reached.get(to) ?? new Map<string, ReviewMapNode>();
    tests.set(test.id, test);
    reached.set(to, tests);
  }
  const measured = payload.context.source === "current_index"
    && !relationshipsPending(payload)
    && !payload.graph.truncated
    && !(payload.graph.omitted_node_count ?? 0);
  const out = new Map<string, TestReach>();
  for (const node of payload.graph.nodes) {
    // A deleted file has nothing left to test.
    if (node.kind !== "file" || !isChangedStatus(node) || isTestNode(node) || changeLetter(node) === "D") continue;
    const tests = [...(reached.get(node.id)?.values() ?? [])];
    out.set(node.id, { state: tests.length ? "reached" : measured ? "none" : "unmeasured", tests });
  }
  return out;
}
