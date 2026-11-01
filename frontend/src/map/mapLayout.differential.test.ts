// Differential oracle for the shared map engine.
//
// The snapshot was recorded from the Review Map's layout before the engine was
// extracted into `map/`; the extracted engine must reproduce it exactly on the
// same inputs. Payloads are generated from a seeded PRNG so the corpus covers
// folding, collapse, file focus and symbol focus without hand-written fixtures.
import { describe, expect, it } from "vitest";

import { buildMapModel, type LayoutOptions, type MapModel } from "./mapModel";
import type { ReviewMapEdge, ReviewMapMemberSymbol, ReviewMapNode, ReviewMapPayload } from "../review/types";

function prng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

function fileNode(id: string, path: string, status: ReviewMapNode["status"], extra: Partial<ReviewMapNode> = {}): ReviewMapNode {
  return {
    id,
    label: path.split("/").pop() ?? path,
    kind: "file",
    path,
    line: 0,
    end_line: 0,
    status,
    exactness: status === "context" ? "current_index" : "snapshot",
    target_ids: [],
    important_public_contract: false,
    boundaries: [],
    boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: id, exactness: "snapshot" },
    ...extra,
  };
}

function link(source: string, target: string, kind: string, weight: number, extra: Partial<ReviewMapEdge> = {}): ReviewMapEdge {
  const id = `${source}->${target}:${kind}`;
  return {
    id,
    source,
    target,
    kind,
    exactness: kind === "impacts" ? "snapshot" : "current_index",
    weight,
    uncertain: false,
    selection: { kind: "review_map_edge", review_id: "r", revision_id: "v", edge_id: id, exactness: "current_index" },
    ...extra,
  };
}

function payload(level: ReviewMapPayload["level"], focus: string | null, nodes: ReviewMapNode[], edges: ReviewMapEdge[]): ReviewMapPayload {
  return {
    review_id: "r",
    revision_id: "v",
    revision_number: 1,
    level,
    snapshot: {
      exactness: "snapshot",
      tree_fingerprint: "",
      source_fingerprint: "",
      base_sha: "",
      head_sha: "",
      degraded: [],
      provenance: { host: "", model: "", certainty: "", method: "" },
      verification: { pass: 0, fail: 0, not_run: 0, unknown: 0, checks: [] },
    },
    context: { source: "current_index", exact_for_snapshot: false, snapshot_head_matches_checkout: false, index: {}, note: "" },
    graph: { level, focus, nodes, edges, truncated: false },
  };
}

const KINDS = ["calls", "imports", "references", "impacts", "calls"];

function architecture(seed: number, changedCount: number, folders: number, contextCount: number): ReviewMapPayload {
  const random = prng(seed);
  const nodes: ReviewMapNode[] = [];
  for (let index = 0; index < changedCount; index += 1) {
    const folder = `src/area${index % folders}/sub${Math.floor(random() * 2)}`;
    const test = random() < 0.15;
    nodes.push(fileNode(`c${index}`, `${test ? "tests/" : ""}${folder}/file${index}.${test ? "test.ts" : "py"}`, random() < 0.2 ? "new" : "changed", {
      rank: 1 + Math.floor(random() * changedCount),
      additions: Math.floor(random() * 200),
      deletions: Math.floor(random() * 80),
    }));
  }
  for (let index = 0; index < contextCount; index += 1) {
    const status = random() < 0.3 ? "impacted" : "context";
    nodes.push(fileNode(`x${index}`, `lib/ctx${index % 7}/dep${index}.py`, status, random() < 0.3 ? { lane: random() < 0.5 ? "uses" : "used_by" } : {}));
  }
  const edges: ReviewMapEdge[] = [];
  const ids = nodes.map((node) => node.id);
  for (let index = 0; index < changedCount * 3; index += 1) {
    const source = ids[Math.floor(random() * ids.length)];
    const target = ids[Math.floor(random() * ids.length)];
    if (source === target) continue;
    edges.push(link(source, target, KINDS[Math.floor(random() * KINDS.length)], 1 + Math.floor(random() * 5)));
  }
  return payload("architecture", null, nodes, edges);
}

function symbol(path: string, index: number, kind: string, linked: boolean): ReviewMapMemberSymbol {
  return {
    id: `${path}#${index}`,
    label: linked ? `linked_${index}` : `sym_${index}`,
    qualified_name: `mod.sym_${index}`,
    kind,
    path,
    line: index * 10 + 1,
    end_line: index * 10 + 8,
    change: index % 3 === 0 ? "added" : "modified",
    caller_count: index % 4,
    usage_count: 0,
    centrality_rank: null,
    centrality_percentile: null,
  };
}

function fileFocus(seed: number, symbolCount: number): ReviewMapPayload {
  const random = prng(seed);
  const path = "src/core/engine.py";
  const kinds = ["function_definition", "class_definition", "method", "lexical_declaration", "property"];
  const symbols = Array.from({ length: symbolCount }, (_, index) => symbol(path, index, kinds[index % kinds.length], index % 5 === 2));
  const nodes = [fileNode("focus", path, "changed", { symbols, symbol_count: symbolCount + 3, additions: 40, deletions: 12 })];
  const edges: ReviewMapEdge[] = [];
  for (let index = 0; index < 18; index += 1) {
    const lane = random() < 0.5 ? "used_by" : "uses";
    nodes.push(fileNode(`n${index}`, `src/n${index % 4}/other${index}.py`, random() < 0.2 ? "changed" : "context", { lane }));
    const names = [{ name: `linked_${2 + 5 * (index % 3)}`, count: 2 }];
    edges.push(lane === "used_by"
      ? link(`n${index}`, "focus", "calls", 1 + (index % 4), { symbols: names })
      : link("focus", `n${index}`, index % 2 ? "imports" : "calls", 1 + (index % 3)));
  }
  return payload("file", "focus", nodes, edges);
}

function symbolFocus(): ReviewMapPayload {
  const nodes = [fileNode("s", "src/core/engine.py", "changed", { kind: "function_definition", label: "run", line: 10, end_line: 40, caller_count: 3 })];
  const edges: ReviewMapEdge[] = [];
  for (let index = 0; index < 9; index += 1) {
    nodes.push(fileNode(`caller${index}`, `src/callers/c${index}.py`, "context", { kind: "caller", lane: "used_by" }));
    edges.push(link(`caller${index}`, "s", "calls", 1 + (index % 2)));
  }
  for (let index = 0; index < 5; index += 1) {
    nodes.push(fileNode(`callee${index}`, `src/lib/l${index}.py`, "context", { kind: "callee", lane: "uses", label: `helper${index}` }));
    edges.push(link("s", `callee${index}`, "calls", 1));
  }
  return payload("symbol", "s", nodes, edges);
}

function digest(model: MapModel) {
  return {
    size: [model.width, model.height],
    lanes: model.lanes.map((lane) => [lane.lane, lane.x, lane.w, lane.count]),
    counts: model.counts,
    hidden: [model.hiddenTests, model.hiddenContext],
    laneOrder: model.laneOrder,
    groups: model.groups.map((group) => [group.key, group.x, group.y, group.w, group.total, group.hidden, group.collapsible, group.expanded, group.folded, group.moreY]),
    cards: model.cards.map((card) => [
      card.id,
      card.lane,
      card.variant,
      card.groupKey,
      card.x,
      card.y,
      card.w,
      card.h,
      card.sharedBy ?? null,
      (card.rows ?? []).map((row) => [row.symbol.id, row.y]),
      card.symbolFold ?? null,
    ]),
    edges: model.edges.map((edge) => [edge.key, edge.id, edge.kind, edge.from, edge.to, edge.d, edge.arrow, edge.labelX, edge.labelY, edge.width, edge.resting, edge.internal, edge.symbolId ?? null]),
    neighbors: [...model.neighbors.entries()].map(([id, set]) => [id, [...set].sort()]),
  };
}

const BASE: LayoutOptions = { width: 1280, linkMode: "key", hideTests: false, expanded: new Set() };

const CASES: [string, ReviewMapPayload, LayoutOptions][] = [
  ["small architecture", architecture(1, 6, 2, 8), BASE],
  ["medium architecture, all links", architecture(2, 24, 5, 20), { ...BASE, linkMode: "all" }],
  ["large architecture, folded", architecture(3, 72, 11, 30), BASE],
  ["large architecture, tests hidden, narrow", architecture(4, 45, 9, 26), { ...BASE, width: 760, hideTests: true }],
  ["large architecture, expanded groups", architecture(3, 72, 11, 30), { ...BASE, expanded: new Set(["subject::src/area9/sub0", "subject::src/area10/sub1", "used_by::lib/ctx1"]) }],
  ["file focus", fileFocus(5, 30), BASE],
  ["file focus, symbols expanded", fileFocus(5, 30), { ...BASE, expanded: new Set(["symbols::focus"]) }],
  ["symbol focus", symbolFocus(), BASE],
];

describe("map engine differential layout", () => {
  for (const [name, input, options] of CASES) {
    it(`reproduces the recorded layout: ${name}`, () => {
      expect(digest(buildMapModel(input, options))).toMatchSnapshot();
    });
  }

  it("keeps a 401-file change navigable without inventing relationships", () => {
    // An actual 401-changed-file graph is intentionally larger than the
    // 72-file differential fixtures. The source graph, not a guessed edge,
    // defines what the reviewer may inspect. No network or model is involved.
    const input = architecture(17, 401, 31, 90);
    const started = performance.now();
    const model = buildMapModel(input, BASE);
    const elapsedMs = performance.now() - started;
    const originalEdges = new Set(input.graph.edges.map((edge) => edge.id));
    expect(input.graph.nodes.filter((node) => node.status === "changed" || node.status === "new")).toHaveLength(401);
    const visibleChanged = model.cards.filter((card) => card.node.status === "changed" || card.node.status === "new").length;
    const subjectGroups = model.groups.filter((group) => group.lane === "subject");
    const hiddenChanged = subjectGroups.reduce((sum, group) => sum + group.hidden, 0);
    expect(visibleChanged + hiddenChanged).toBe(401);
    // Folding is intentional, but every hidden changed file must belong to a
    // discoverable and expandable group, never silently disappear from Map.
    expect(subjectGroups.filter((group) => group.hidden > 0).every((group) => group.folded || group.moreY !== null)).toBe(true);
    const expanded = buildMapModel(input, {
      ...BASE,
      expanded: new Set(subjectGroups.filter((group) => group.hidden > 0).map((group) => group.key)),
    });
    expect(expanded.cards.filter((card) => card.node.status === "changed" || card.node.status === "new")).toHaveLength(401);
    expect(model.edges.every((edge) => originalEdges.has(edge.id))).toBe(true);
    expect(expanded.edges.every((edge) => originalEdges.has(edge.id))).toBe(true);
    expect(model.cards.every((card) => Number.isFinite(card.x) && Number.isFinite(card.y)
      && Number.isFinite(card.w) && Number.isFinite(card.h))).toBe(true);
    expect(Number.isFinite(model.width) && Number.isFinite(model.height)).toBe(true);
    // A generous non-regression ceiling for pure layout, not a browser FPS
    // or customer first-render SLO. Browser responsiveness is qualified apart.
    expect(elapsedMs).toBeLessThan(5000);
  });
});
