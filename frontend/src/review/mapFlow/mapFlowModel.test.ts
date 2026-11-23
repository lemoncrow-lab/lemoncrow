import { describe, expect, it } from "vitest";
import type { ReviewDataPathReport, ReviewMapEdge, ReviewMapNode, ReviewMapPayload, ReviewTarget } from "../types";
import {
  DATA_PATH_UNAVAILABLE, INDEX_REFRESH_LIMIT, dataPathRunFailure, dataPathUnavailableText, indexRefreshDelay, relationshipsIndexing,
  buildFlowLayout, compactFlowLayout, dataPathAvailable, edgeBasis, edgePath, exampleMap, footerText, graphComparable, lensEdges, mapRowLabel, mapStateSymbol,
  provenanceCounts, relatedTarget, statusOf, summaryLine, survivesView,
} from "./mapFlowModel";

function node(id: string, delta: string, extra: Partial<ReviewMapNode> = {}): ReviewMapNode {
  return {
    id, label: id, kind: "file", path: `src/${id}.py`, line: 5, end_line: 10, status: delta === "added" ? "new" : "changed",
    exactness: "snapshot", delta_status: delta, target_ids: [], important_public_contract: false,
    boundaries: [], boundary_basis: "", selection: { kind: "review_map_node", review_id: "r", revision_id: "rev", node_id: id, exactness: "snapshot" },
    ...extra,
  };
}
function edge(source: string, target: string, kind = "references", delta = "unchanged"): ReviewMapEdge {
  return { id: `${source}>${target}`, source, target, kind, exactness: "snapshot", uncertain: false, weight: 1,
    delta_status: delta, selection: { kind: "review_map_edge", review_id: "r", revision_id: "rev", edge_id: `${source}>${target}`, exactness: "snapshot" } };
}
function payload(nodes: ReviewMapNode[], edges: ReviewMapEdge[], comparable = true): ReviewMapPayload {
  return {
    review_id: "r", revision_id: "rev", revision_number: 2, level: "architecture",
    projection: comparable ? "architecture_delta" : "change_map",
    ...(comparable ? { architecture_delta: { status: "ready" } as ReviewMapPayload["architecture_delta"] } : {}),
    graph: { nodes, edges, level: "architecture", focus: null, truncated: false },
    context: { source: "current_index", exact_for_snapshot: false, snapshot_head_matches_checkout: false, index: {}, note: "" },
    snapshot: { exactness: "snapshot", tree_fingerprint: "t", source_fingerprint: "s", head_sha: "head", base_sha: "base",
      degraded: [], provenance: { host: "test", model: "", certainty: "exact", method: "" },
      verification: { pass: 0, fail: 0, not_run: 0, unknown: 0, checks: [] } },
  };
}
const GRAPH = payload(
  [node("changed", "changed"), node("added", "added"), node("removed", "removed"), node("near", "unchanged", { status: "context" }),
   node("far", "unchanged", { status: "context" }), node("isolated", "unchanged", { status: "context" })],
  [edge("near", "changed"), edge("near", "far"), edge("changed", "added", "references", "added"), edge("removed", "changed", "references", "removed")],
);
describe("P3-3 MAP-1: comparison and stable positions", () => {
  it("uses the exact same coordinates for every node in Changes, Before and After", () => {
    const views = ["changes", "before", "after"] as const;
    const results = views.map((v) => buildFlowLayout(GRAPH, [], "dependencies", v, 2));
    const boxes = (i: number) => results[i].nodes.map(({ id, x, y, width, height }) => ({ id, x, y, width, height }));
    expect(boxes(0)).toEqual(boxes(1));
    expect(boxes(0)).toEqual(boxes(2));
    expect(results[0].nodes.find((r) => r.id === "added")?.active).toBe(true);
    expect(results[1].nodes.find((r) => r.id === "added")?.active).toBe(false);
    expect(results[2].nodes.find((r) => r.id === "removed")?.active).toBe(false);
    expect(results[1].nodes.find((r) => r.id === "removed")?.active).toBe(true);
  });
  it("keeps How far to follow 0/1/2 stable and folds out-of-depth unchanged nodes", () => {
    const lay0 = buildFlowLayout(GRAPH, [], "dependencies", "changes", 0);
    const lay1 = buildFlowLayout(GRAPH, [], "dependencies", "changes", 1);
    const lay2 = buildFlowLayout(GRAPH, [], "dependencies", "changes", 2);
    expect(lay0.nodes.find((r) => r.id === "near")?.visible).toBe(false);
    expect(lay1.nodes.find((r) => r.id === "near")?.visible).toBe(true);
    expect(lay1.nodes.find((r) => r.id === "far")?.visible).toBe(false);
    expect(lay2.nodes.find((r) => r.id === "far")?.visible).toBe(true);
    expect(lay2.nodes.find((r) => r.id === "isolated")?.visible).toBe(false);
    const coords = (layout: typeof lay0) => layout.nodes.map(({ id, x, y }) => ({ id, x, y }));
    expect(coords(lay0)).toEqual(coords(lay1));
    expect(coords(lay1)).toEqual(coords(lay2));
  });
  it("only uses server-returned edges for either lens, and marks uncertain facts", () => {
    expect(lensEdges(GRAPH, "dependencies").every((e) => GRAPH.graph.edges.includes(e))).toBe(true);
    expect(lensEdges(GRAPH, "dependencies")).toHaveLength(GRAPH.graph.edges.length);
    expect(statusOf(node("unknown", "unknown"))).toBe("unverified");
    expect(mapStateSymbol("removed")).toBe("−");
    expect(survivesView("removed", "after")).toBe(false);
  });
  it("refuses to invent Before/After when comparison facts are unavailable", () => {
    const noDelta = payload([node("a", "unknown")], [], false);
    expect(graphComparable(noDelta)).toBe(false);
    const model = buildFlowLayout(noDelta, [], "dependencies", "before", 1);
    expect(model.comparable).toBe(false);
    expect(model.nodes.every((r) => !r.active)).toBe(true);
    expect(model.summary.notVerified).toBe(1);
  });
  it("gets the exact part from server IDs, not from a guessed same-name path", () => {
    const target = { target_id: "t1", path: "src/changed.py", start_line: 1, end_line: 30 } as ReviewTarget;
    const n = node("changed", "changed", { target_ids: ["t1"] });
    expect(relatedTarget(n, [target])).toBe(target);
    expect(relatedTarget(node("other", "unchanged"), [target])).toBeUndefined();
    expect(mapRowLabel(n)).toBeTruthy();
  });
  it("the bigger-change example is visibly synthetic and cannot be mistaken for a store revision", () => {
    const demo = exampleMap();
    expect(demo.review_id).toBe("illustrative");
    expect(demo.graph.nodes.length).toBeGreaterThan(GRAPH.graph.nodes.length);
    expect(buildFlowLayout(demo, [], "dependencies", "before", 2).comparable).toBe(true);
  });
});

/** The shape architecture_delta.py really returns: name-matched "references" edges stamped snapshot, a revision-exact
 *  "impacts" fact, and current-index "imports" and "calls" context. */
const REAL = payload(
  [node("changed", "changed"), node("user", "unchanged", { status: "impacted" }), node("fact", "unknown", { status: "impacted" }),
   node("ctx", "context_only", { status: "context", exactness: "current_index" })],
  [
    { ...edge("user", "changed", "references", "added"), symbols: [{ name: "apply_discount", count: 2 }],
      evidence: [{ kind: "reference", path: "src/user.py", line: 4, uncertainty: "", symbol: "apply_discount" }] },
    { ...edge("fact", "changed", "impacts", "unknown"), selection: { ...edge("a", "b").selection } },
    { ...edge("ctx", "changed", "imports", "context_only"), exactness: "current_index" },
    { ...edge("ctx", "user", "calls", "context_only"), exactness: "current_index", uncertain: true },
  ],
);
describe("P3-3 MAP-2: what counts as checked in code", () => {
  it("never calls a name-matched references edge checked, even though the server stamps it snapshot", () => {
    const [references, impacts, imports, calls] = REAL.graph.edges;
    expect(references.exactness).toBe("snapshot");
    expect(references.uncertain).toBe(false);
    expect(edgeBasis(references)).toBe("name");
    expect(edgeBasis(impacts)).toBe("checked");
    expect(edgeBasis(imports)).toBe("index");
    expect(edgeBasis(calls)).toBe("index");
  });
  it("counts the footer from how each shown node and connection was matched", () => {
    const layout = buildFlowLayout(REAL, [], "dependencies", "changes", 2);
    // Nodes: changed (snapshot, changed) and user (snapshot, unchanged) are checked; fact has no recorded comparison and
    // ctx is current-index, so neither is. Connections: only the impacts fact is checked.
    expect(provenanceCounts(layout, true)).toEqual({ checked: 3, notVerified: 5 });
    // Hiding the dashed connections removes them from the count too.
    expect(provenanceCounts(layout, false)).toEqual({ checked: 3, notVerified: 2 });
    expect(layout.edges.filter((e) => e.basis !== "checked")).toHaveLength(3);
  });
  it("words the footer with a 7-character sha", () => {
    const layout = buildFlowLayout(REAL, [], "dependencies", "changes", 2);
    expect(footerText({ ...REAL, snapshot: { ...REAL.snapshot, head_sha: "4e1b9c2a77d0e" } }, provenanceCounts(layout, true)))
      .toBe("Checked in the code at 4e1b9c2 · 3 checked in code · 5 not verified");
    expect(footerText(REAL, { checked: 1, notVerified: 0 })).toBe("Checked in the code at head · 1 checked in code · 0 not verified");
    expect(footerText({ ...REAL, snapshot: { ...REAL.snapshot, head_sha: "" } }, { checked: 1, notVerified: 0 })).toBe("Checked in the code · 1 checked in code · 0 not verified");
  });
});
describe("P3-3 lenses use the kinds the server really produces", () => {
  it("has no Data path: references, imports, calls and impacts say who uses whom, not how data moves", () => {
    expect(REAL.graph.edges.map((e) => e.kind)).toEqual(["references", "impacts", "imports", "calls"]);
    expect(dataPathAvailable(REAL)).toBe(false);
    expect(lensEdges(REAL, "data-path")).toHaveLength(0);
    expect(lensEdges(REAL, "dependencies")).toHaveLength(4);
  });
  it("shows the list summary line, or says nothing was compared", () => {
    const comparable = buildFlowLayout(GRAPH, [], "dependencies", "changes", 1);
    expect(summaryLine(comparable, false)).toBe("Changed 1 · Added 1 · Removed 1 · New connections 1");
    expect(summaryLine(comparable, true)).toMatch(/^Example data · Changed 1/);
    expect(summaryLine(buildFlowLayout(payload([node("a", "unknown")], [], false), [], "dependencies", "changes", 1), false)).toMatch(/^Not compared/);
  });
  it("turns the Data path lens on only for a stored report that ran", () => {
    const report = (extra: Partial<ReviewDataPathReport>): ReviewDataPathReport =>
      ({ schema: 1, entry: "data_path.diff", experimental: true, summary: "s", disclosure: "d", state: "not_run", ...extra });
    const counts = { added: 0, removed: 0, rerouted: 0, unchanged: 3 };
    for (const state of ["not_run", "not_available", "not_measured"]) expect(dataPathAvailable({ ...REAL, data_path: report({ state }) })).toBe(false);
    expect(dataPathAvailable({ ...REAL, data_path: report({ state: "ran", counts }) })).toBe(true);
    expect(dataPathAvailable({ ...REAL, data_path: report({ state: "partial", counts }) })).toBe(true);
  });
  it("words why the Data path lens is off from the stored report, with a not_available report's setup line", () => {
    const report = (extra: Partial<ReviewDataPathReport>): ReviewDataPathReport =>
      ({ schema: 1, entry: "data_path.diff", experimental: true, summary: "", disclosure: "d", state: "not_run", ...extra });
    expect(dataPathUnavailableText(undefined)).toBe(DATA_PATH_UNAVAILABLE);
    expect(dataPathUnavailableText(report({ summary: "Not run: the data path runs only when requested" }))).toBe("Data path has not run for this revision.");
    expect(dataPathUnavailableText(report({ state: "not_available", summary: "Data path not available: no opengrep", setup: "Download it and set LEMONCROW_DATA_PATH_OPENGREP_BIN." })))
      .toBe("Data path not available: no opengrep. Download it and set LEMONCROW_DATA_PATH_OPENGREP_BIN.");
    expect(dataPathUnavailableText(report({ state: "not_available", summary: "Data path not available: no sandbox" }))).toBe("Data path not available: no sandbox.");
    expect(dataPathUnavailableText(report({ state: "not_measured", summary: "Data path not measured: the run stopped on an unexpected error (OSError)" })))
      .toBe("Data path not measured: the run stopped on an unexpected error (OSError).");
  });
  it("says a run the server refused or could not answer in plain words", () => {
    expect(dataPathRunFailure(new Error("data_path.diff is not in the effective run plan: planned as skipped")))
      .toBe("The data path did not run: data_path.diff is not in the effective run plan: planned as skipped. Your review is unchanged.");
    expect(dataPathRunFailure(new Error("500"))).toBe("The data path did not run (the server answered 500). Your review is unchanged.");
    expect(dataPathRunFailure("offline")).toBe("The data path did not run. Your review is unchanged.");
  });
  it("places one fold node per lane for the unchanged nodes beyond how far the map follows", () => {
    const one = buildFlowLayout(GRAPH, [], "dependencies", "changes", 1);
    expect(one.folds.reduce((sum, f) => sum + f.count, 0)).toBe(one.folded);
    expect(buildFlowLayout(GRAPH, [], "dependencies", "changes", 2).height).toBe(one.height);
  });
});

describe("review-12.4: the compact phone diagram", () => {
  const overlaps = (a: { x: number; y: number; width: number; height: number }, b: typeof a) =>
    a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
  it("fits the width, stacks the lanes in reading order and keeps every view's boxes", () => {
    for (const width of [356, 300]) {
      const views = (["changes", "before", "after"] as const).map((v) => compactFlowLayout(buildFlowLayout(exampleMap(), [], "dependencies", v, 2), width));
      const [changes] = views;
      expect(changes.width).toBe(width);
      const shown = changes.nodes.filter((n) => n.visible);
      expect(shown.length).toBeGreaterThan(4);
      for (const n of shown) {
        expect(n.x).toBeGreaterThanOrEqual(0);
        expect(n.x + n.width).toBeLessThanOrEqual(width);
        expect(n.y + n.height).toBeLessThanOrEqual(changes.height);
      }
      for (const [i, a] of shown.entries()) for (const b of shown.slice(i + 1)) expect(overlaps(a, b), `${a.id} / ${b.id}`).toBe(false);
      const bands = changes.bands ?? [];
      expect(bands.map((band) => band.lane)).toEqual(["used_by", "subject", "uses"].filter((lane) => bands.some((band) => band.lane === lane)));
      for (const n of shown) {
        const band = bands.findIndex((b) => b.lane === n.lane);
        expect(n.y).toBeGreaterThan(bands[band].y);
        if (bands[band + 1]) expect(n.y + n.height).toBeLessThan(bands[band + 1].y);
      }
      const boxes = (i: number) => views[i].nodes.map(({ id, x, y, width: w, height }) => ({ id, x, y, w, height }));
      expect(boxes(1)).toEqual(boxes(0));
      expect(boxes(2)).toEqual(boxes(0));
      for (const edge of changes.edges) {
        expect(edge.from).toBe(changes.nodes.find((n) => n.id === edge.from.id));
        expect(edge.to).toBe(changes.nodes.find((n) => n.id === edge.to.id));
        if (!edge.visible) continue;
        // A cubic stays inside its control points: every arc's farthest point is inside the width, right of every node.
        const xs = [...edgePath(edge.from, edge.to, true).matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map((m) => Number(m[1]));
        expect(Math.max(...xs)).toBeLessThanOrEqual(width);
        expect(Math.min(...xs)).toBeGreaterThanOrEqual(Math.max(...shown.map((n) => n.x + n.width)));
      }
    }
  });
  it("puts each fold across the width under its lane", () => {
    const wide = buildFlowLayout(GRAPH, [], "dependencies", "changes", 0);
    expect(wide.folds.length).toBeGreaterThan(0);
    const compact = compactFlowLayout(wide, 340);
    expect(compact.folds.map((f) => f.lane)).toEqual(wide.folds.map((f) => f.lane).sort((a, b) => ["used_by", "subject", "uses"].indexOf(a) - ["used_by", "subject", "uses"].indexOf(b)));
    for (const fold of compact.folds) {
      expect(fold.x + fold.width).toBeLessThanOrEqual(340);
      expect(fold.y + fold.height).toBeLessThanOrEqual(compact.height);
      for (const n of compact.nodes.filter((row) => row.visible)) expect(overlaps(fold, n)).toBe(false);
    }
  });
  it("draws phone connections as arcs right of the column, longer ones bulging further, and the wide layout as before", () => {
    const box = (x: number, y: number) => ({ x, y, width: 100, height: 56 }) as Parameters<typeof edgePath>[0];
    expect(edgePath(box(8, 8), box(8, 100), true)).toBe("M 108 36 C 129 36, 129 128, 108 128");
    expect(edgePath(box(8, 600), box(8, 8), true)).toBe("M 108 628 C 152 628, 152 36, 108 36");
    expect(edgePath({ ...box(25, 48), width: 280 }, { ...box(360, 48), width: 280 }, false)).toBe("M 305 81 C 332.5 81, 332.5 81, 360 81");
  });
});

describe("statusOf on the plain change map", () => {
  const node = (fields: Record<string, unknown>) => fields as unknown as ReviewMapNode;
  it("reads a snapshot node's own change status when no comparison has set delta_status", () => {
    expect(statusOf(node({ status: "changed", exactness: "snapshot" }))).toBe("changed");
    expect(statusOf(node({ status: "new", exactness: "snapshot" }))).toBe("added");
  });
  it("never reads a context node, or a node outside the snapshot, as changed", () => {
    expect(statusOf(node({ status: "context", exactness: "current_index" }))).toBe("unverified");
    expect(statusOf(node({ status: "changed", exactness: "current_index" }))).toBe("unverified");
  });
  it("keeps the comparison's answer when there is one", () => {
    expect(statusOf(node({ status: "changed", exactness: "snapshot", delta_status: "unchanged" }))).toBe("unchanged");
  });
});

describe("the Map's index refresh", () => {
  const context = (source: string, state?: string) => ({ context: { source, exact_for_snapshot: false as const, snapshot_head_matches_checkout: false, index: {}, note: "", readiness: state ? { graph: { state } } : undefined } });
  it("counts only a current index whose link graph is building", () => {
    expect(relationshipsIndexing(context("current_index", "building"))).toBe(true);
    expect(relationshipsIndexing(context("current_index", "ready"))).toBe(false);
    expect(relationshipsIndexing(context("current_index", "degraded"))).toBe(false);
    expect(relationshipsIndexing(context("current_index"))).toBe(false);
    expect(relationshipsIndexing(context("unavailable", "building"))).toBe(false);
  });
  it("backs off to 5 s and ends at the limit", () => {
    expect(INDEX_REFRESH_LIMIT).toBe(14);
    expect([1, 2, 3, 4, 5, 6].map(indexRefreshDelay)).toEqual([500, 1000, 2000, 4000, 5000, 5000]);
    expect(indexRefreshDelay(INDEX_REFRESH_LIMIT)).toBe(5000);
    expect(indexRefreshDelay(INDEX_REFRESH_LIMIT + 1)).toBeNull();
    expect(indexRefreshDelay(1000)).toBeNull();
    expect(indexRefreshDelay(0)).toBeNull();
    expect(indexRefreshDelay(1.5)).toBeNull();
  });
});
