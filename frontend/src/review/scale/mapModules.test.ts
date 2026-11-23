import { describe, expect, it } from "vitest";

import { edgeBasis } from "../mapFlow/mapFlowModel";
import type { ReviewMapEdge, ReviewMapNode, ReviewMapPayload, ReviewTarget } from "../types";
import { ROOT_MODULE, aggregateModules, moduleCountsText, moduleId, moduleOf } from "./mapModules";

function node(path: string, delta: string, extra: Partial<ReviewMapNode> = {}): ReviewMapNode {
  return {
    id: `n:${path}`, label: path.split("/").pop() ?? path, kind: "file", path, module: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "",
    line: 1, end_line: 9, status: delta === "added" ? "new" : delta === "unchanged" ? "context" : "changed", exactness: "snapshot", delta_status: delta,
    target_ids: [`t:${path}`], important_public_contract: false, boundaries: [], boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: `n:${path}`, exactness: "snapshot" },
    ...extra,
  } as ReviewMapNode;
}
function edge(a: string, b: string, extra: Partial<ReviewMapEdge> = {}): ReviewMapEdge {
  return { id: `${a}>${b}`, source: `n:${a}`, target: `n:${b}`, kind: "calls", exactness: "snapshot", weight: 1, uncertain: false, delta_status: "unchanged",
    selection: { kind: "review_map_edge", review_id: "r", revision_id: "v", edge_id: `${a}>${b}`, exactness: "snapshot" }, ...extra } as ReviewMapEdge;
}
function target(path: string, category: "decision" | "quick_check" | "accepted_by_rule", id = `${path}#${category}`): ReviewTarget {
  return { target_id: id, path, answer: category === "accepted_by_rule" ? { category, rule: "lockfiles", rule_label: "Lockfiles" } : { category, rule: "" } } as unknown as ReviewTarget;
}
function payload(nodes: ReviewMapNode[], edges: ReviewMapEdge[]): ReviewMapPayload {
  return { review_id: "r", revision_id: "v", revision_number: 1, level: "architecture", projection: "architecture_delta", architecture_delta: { status: "ready" },
    graph: { level: "architecture", focus: null, nodes, edges, truncated: false } } as unknown as ReviewMapPayload;
}

describe("moduleOf", () => {
  it("uses the server's module, else the parent directory, else the root", () => {
    expect(moduleOf("src/core/a.py", "src/core")).toBe("src/core");
    expect(moduleOf("src/core/a.py")).toBe("src/core");
    expect(moduleOf("a.py")).toBe(ROOT_MODULE);
    expect(moduleOf("a.py", "")).toBe(ROOT_MODULE);
  });
});

describe("aggregateModules", () => {
  const p = payload(
    [node("src/core/a.py", "changed"), node("src/core/b.py", "unchanged"), node("src/api/c.py", "added"), node("src/api/d.py", "removed"), node("README.md", "unchanged"), node("lib/x.py", "unchanged", { exactness: "current_index" })],
    [edge("src/core/a.py", "src/core/b.py"), edge("src/core/a.py", "src/api/c.py"), edge("src/core/b.py", "src/api/d.py", { delta_status: "added" }), edge("src/api/c.py", "lib/x.py", { kind: "references" })],
  );
  const t = [target("src/core/a.py", "decision"), target("src/core/a.py", "quick_check"), target("src/api/c.py", "quick_check"), target("package-lock.json", "accepted_by_rule")];
  const agg = aggregateModules(p, t);
  const byName = (name: string) => agg.payload.graph.nodes.find((n) => n.id === moduleId(name))!;

  it("keeps every file reachable under exactly one module", () => {
    const all = [...agg.members.values()].flat().map((f) => f.node?.id).filter(Boolean).sort();
    expect(all).toEqual(p.graph.nodes.map((n) => n.id).sort());
    expect(agg.members.get("src/core")).toHaveLength(2);
    expect(agg.members.get(ROOT_MODULE)).toHaveLength(2); // README.md (map node) and package-lock.json (a part the map lacks)
  });

  it("states each module from its files, never calling a mixed module unchanged", () => {
    expect(byName("src/core").delta_status).toBe("changed");
    expect(byName("src/api").delta_status).toBe("changed"); // one added, one removed
    expect(byName(ROOT_MODULE).delta_status).toBe("changed"); // README is unchanged, package-lock.json has a part the map lacks
    expect(byName("lib").delta_status).toBe("unchanged");
    expect(byName("lib").exactness).toBe("current_index");
  });

  it("counts real parts per module and keeps parts accepted by rule apart", () => {
    expect(agg.stats.get("src/core")).toMatchObject({ parts: 2, decisions: 1, quickChecks: 1, acceptedByRule: 0, files: 2, changedFiles: 1 });
    expect(agg.stats.get(ROOT_MODULE)).toMatchObject({ parts: 1, acceptedByRule: 1 });
    expect(moduleCountsText(agg.stats.get(ROOT_MODULE)!)).toBe("1 changed file · 1 part · 1 accepted by rule");
    expect(moduleCountsText(agg.stats.get("src/core")!)).toBe("1 changed file · 2 parts · 1 decision · 1 quick check");
  });

  it("leaves zero counts out and names an empty module plainly", () => {
    const row = { parts: 0, decisions: 0, quickChecks: 0, acceptedByRule: 0, files: 3, changedFiles: 0 };
    expect(moduleCountsText(row)).toBe("No changed files");
    expect(moduleCountsText({ ...row, changedFiles: 2, parts: 4, quickChecks: 4 })).toBe("2 changed files · 4 parts · 4 quick checks");
    expect(moduleCountsText({ ...row, changedFiles: 1, parts: 1, decisions: 1 })).toBe("1 changed file · 1 part · 1 decision");
  });

  it("joins edges between modules, drops edges inside one, and never upgrades a name match to checked", () => {
    const edges = agg.payload.graph.edges;
    expect(edges.map((e) => e.id).sort()).toEqual([`${moduleId("src/api")}>${moduleId("lib")}`, `${moduleId("src/core")}>${moduleId("src/api")}`].sort());
    const joined = edges.find((e) => e.source === moduleId("src/core"))!;
    expect(joined.weight).toBe(2);
    expect(edgeBasis(joined)).toBe("checked");
    expect(edgeBasis(edges.find((e) => e.target === moduleId("lib"))!)).toBe("name");
  });
});

describe("a change the server's map cannot carry whole", () => {
  // The server keeps at most 300 changed files in the map (review_map.py _MAX_OVERVIEW_CHANGED) and says so with
  // `truncated` and `omitted_node_count`; the parts list still holds every part.
  const CAP = 300, FILES = 1200, MODULES = 300;
  const path = (i: number) => `pkg/m${String(i % MODULES).padStart(3, "0")}/f${i}.py`;
  const nodes: ReviewMapNode[] = [], targets: ReviewTarget[] = [];
  for (let i = 0; i < FILES; i++) {
    if (i < CAP) nodes.push(node(path(i), "changed"));
    targets.push(target(path(i), i % 3 === 0 ? "decision" : "quick_check", `t${i}`));
  }
  const p = payload(nodes, []);
  p.graph.truncated = true;
  p.graph.omitted_node_count = FILES - CAP;

  it("reaches every part: each file with a part is a member of its module with an Open part target", () => {
    const agg = aggregateModules(p, targets);
    const files = [...agg.members.values()].flat();
    expect(files).toHaveLength(FILES);
    const reachable = new Set(files.map((f) => f.part?.target_id));
    expect(targets.filter((t) => !reachable.has(t.target_id))).toEqual([]);
    expect([...agg.stats.values()].reduce((n, row) => n + row.parts, 0)).toBe(FILES);
  });

  it("shows a module made only of files past the cap, as changed and not verified", () => {
    const only = [target("late/dir/new.py", "quick_check", "late")];
    const agg = aggregateModules(payload(nodes.slice(0, 3), []), [...targets.slice(0, 3), ...only]);
    const mod = agg.payload.graph.nodes.find((n) => n.id === moduleId("late/dir"))!;
    expect(mod.delta_status).toBe("changed");
    expect(mod.exactness).toBe("unknown");
    expect(agg.beyondMap.has("late/dir")).toBe(true);
    expect(agg.stats.get("late/dir")).toMatchObject({ files: 1, changedFiles: 1, parts: 1 });
  });

  it("counts a module's changed files from its files, not only from the map's nodes", () => {
    const agg = aggregateModules(p, targets);
    const row = agg.stats.get("pkg/m000")!;
    expect(row.files).toBe(4);
    expect(row.changedFiles).toBe(4);
    expect(row.parts).toBe(4);
  });

  it("reads each node and each part a bounded number of times (linear work)", () => {
    for (const scale of [1, 10]) {
      let reads = 0;
      const counted = <T,>(rows: T[]) => new Proxy(rows, { get(target, key, receiver) { if (typeof key === "string" && /^\d+$/.test(key)) reads++; return Reflect.get(target, key, receiver); } });
      const ns: ReviewMapNode[] = [], ts: ReviewTarget[] = [];
      for (let i = 0; i < FILES * scale; i++) {
        ns.push(node(path(i), "changed")); ts.push(target(path(i), "quick_check", `t${i}`));
      }
      const big = payload(ns, []);
      big.graph.nodes = counted(ns);
      aggregateModules(big, counted(ts));
      expect(reads).toBeLessThanOrEqual(8 * FILES * scale);
    }
  });
});
