import { describe, expect, it } from "vitest";

import { ARROW_LEN } from "../map/mapModel";

import {
  assignLanes,
  buildMapModel,
  localFileFocus,
  reasonsOf,
  relationshipsOf,
  reviewQueue,
  searchMap,
  shortModule,
  subjectFolding,
  symbolKindLabel,
  testReach,
  type LayoutOptions,
} from "./reviewMapModel";
import type { ReviewMapEdge, ReviewMapNode, ReviewMapPayload } from "./types";

function node(id: string, path: string, status: ReviewMapNode["status"], extra: Partial<ReviewMapNode> = {}): ReviewMapNode {
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

function edge(source: string, target: string, kind: string, weight = 1, extra: Partial<ReviewMapEdge> = {}): ReviewMapEdge {
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

function payload(nodes: ReviewMapNode[], edges: ReviewMapEdge[], level: ReviewMapPayload["level"] = "architecture", focus: string | null = null): ReviewMapPayload {
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

const OPTIONS: LayoutOptions = { width: 1200, linkMode: "key", hideTests: false, expanded: new Set() };

function overlapsInLane(model: ReturnType<typeof buildMapModel>): boolean {
  const byLane = new Map<string, { y: number; h: number }[]>();
  for (const card of model.cards) {
    const rows = byLane.get(card.lane) ?? [];
    rows.push({ y: card.y, h: card.h });
    byLane.set(card.lane, rows);
  }
  for (const rows of byLane.values()) {
    rows.sort((a, b) => a.y - b.y);
    for (let index = 1; index < rows.length; index += 1) {
      if (rows[index].y < rows[index - 1].y + rows[index - 1].h) return true;
    }
  }
  return false;
}

describe("assignLanes", () => {
  it("puts dependents left, dependencies right, and trusts the server's hint", () => {
    const lanes = assignLanes(payload(
      [
        node("core", "src/core.py", "changed"),
        node("caller", "src/api.py", "context"),
        node("callee", "src/util.py", "context"),
        node("site", "src/jobs.py", "impacted"),
        node("hinted", "src/hinted.py", "context", { lane: "uses" }),
      ],
      [
        edge("caller", "core", "calls"),
        edge("core", "callee", "imports"),
        edge("core", "site", "impacts"),
        edge("hinted", "core", "calls", 9),
      ],
    ));
    expect(lanes.get("core")).toBe("subject");
    expect(lanes.get("caller")).toBe("used_by");
    expect(lanes.get("callee")).toBe("uses");
    // An impact site uses the changed code: it is a dependent.
    expect(lanes.get("site")).toBe("used_by");
    expect(lanes.get("hinted")).toBe("uses");
  });
});

describe("buildMapModel", () => {
  const nodes = [
    node("a", "src/billing/invoice.py", "changed", { rank: 1 }),
    node("b", "src/billing/tax.py", "changed", { rank: 3 }),
    node("c", "src/api/routes.py", "new", { rank: 2 }),
    node("t", "tests/test_invoice.py", "changed", { category: "test" }),
    node("x", "src/web/page.py", "context"),
    node("y", "src/lib/money.py", "context"),
    node("z", "src/lib/strings.py", "context"),
    node("s", "src/jobs/reminders.py", "impacted"),
  ];
  const edges = [
    edge("x", "c", "calls", 4),
    edge("a", "y", "calls", 2),
    edge("b", "z", "references", 3),
    edge("a", "s", "impacts"),
    edge("t", "a", "calls", 5),
    edge("c", "a", "calls", 1),
  ];

  it("lays changed files out by folder in server order with side lanes around them", () => {
    const model = buildMapModel(payload(nodes, edges), OPTIONS);
    expect(model.laneOrder.subject).toEqual(["a", "b", "c", "t"]);
    expect(model.groups.filter((group) => group.lane === "subject").map((group) => group.module)).toEqual(["src/billing", "src/api", "tests"]);
    expect(model.laneOrder.used_by.sort()).toEqual(["s", "x"]);
    expect(model.laneOrder.uses).toEqual(["y"]);
    // Reference-only surrounding code stays out of the first frame.
    expect(model.cardById.has("z")).toBe(false);
    expect(model.hiddenContext).toBe(1);
    expect(overlapsInLane(model)).toBe(false);
    const lanes = model.lanes.map((lane) => lane.lane);
    expect(lanes).toEqual(["used_by", "subject", "uses"]);
    const [left, middle, right] = model.lanes;
    expect(left.x + left.w).toBeLessThan(middle.x);
    expect(middle.x + middle.w).toBeLessThan(right.x);
  });

  it("groups changed files by reading-order chapter first and folder second", () => {
    const chapters = [
      { label: "Expose the endpoint", paths: ["src/api/routes.py", "tests/test_invoice.py"] },
      { label: "Bill correctly", paths: ["src/billing/tax.py", "src/billing/invoice.py"] },
    ];
    const model = buildMapModel(payload(nodes, edges), { ...OPTIONS, chapters });
    const subject = model.groups.filter((group) => group.lane === "subject");
    expect(subject.map((group) => group.chapter)).toEqual(["Expose the endpoint", "Bill correctly"]);
    expect(subject.map((group) => group.label)).toEqual(["Expose the endpoint", "Bill correctly"]);
    expect(model.laneOrder.subject).toEqual(["c", "t", "a", "b"]);
    expect(overlapsInLane(model)).toBe(false);

    // A file outside every chapter keeps its folder group, after the chapters.
    const partial = buildMapModel(payload(nodes, edges), { ...OPTIONS, chapters: [chapters[0], { label: "Tax", paths: ["src/billing/tax.py"] }] });
    expect(partial.groups.filter((group) => group.lane === "subject").map((group) => group.chapter ?? group.module)).toEqual(["Expose the endpoint", "Tax", "src/billing"]);

    // One chapter says nothing about order: folders stay.
    const single = buildMapModel(payload(nodes, edges), { ...OPTIONS, chapters: [chapters[0]] });
    expect(single.groups.filter((group) => group.lane === "subject").map((group) => group.module)).toEqual(["src/billing", "src/api", "tests"]);
  });

  it("draws every link from the dependent to what it depends on", () => {
    const model = buildMapModel(payload(nodes, edges), OPTIONS);
    const impact = model.edges.find((item) => item.kind === "impacts")!;
    expect([impact.from, impact.to]).toEqual(["s", "a"]);
    const caller = model.edges.find((item) => item.id === "x->c:calls")!;
    const callerCard = model.cardById.get("x")!;
    const calleeCard = model.cardById.get("c")!;
    expect(caller.d.startsWith(`M${callerCard.x + callerCard.w},`)).toBe(true);
    expect(caller.d.endsWith(`${calleeCard.x - ARROW_LEN},${calleeCard.y + 23}`)).toBe(true);
    // Coupling inside the change is an arc; only same-folder arcs rest visible.
    const internal = model.edges.filter((item) => item.internal);
    expect(internal.map((item) => item.id).sort()).toEqual(["c->a:calls", "t->a:calls"]);
    expect(internal.every((item) => !item.resting)).toBe(true);
  });

  it("shows references on request and hides tests without losing the focus", () => {
    const all = buildMapModel(payload(nodes, edges), { ...OPTIONS, linkMode: "all" });
    expect(all.cardById.has("z")).toBe(true);
    const noTests = buildMapModel(payload(nodes, edges), { ...OPTIONS, hideTests: true });
    expect(noTests.cardById.has("t")).toBe(false);
    expect(noTests.hiddenTests).toBe(1);
    const focused = buildMapModel(payload(nodes, edges, "file", "t"), { ...OPTIONS, hideTests: true });
    expect(focused.cardById.has("t")).toBe(true);
    // The overview's focus is only where to start: hiding tests hides it too.
    const overview = buildMapModel(payload(nodes, edges, "architecture", "t"), { ...OPTIONS, hideTests: true });
    expect(overview.cardById.has("t")).toBe(false);
  });

  it("folds large folders and unfolds them on request", () => {
    const many = Array.from({ length: 14 }, (_, index) => node(`f${index}`, `src/big/file_${index}.py`, "changed"));
    const folded = buildMapModel(payload(many, []), OPTIONS);
    expect(subjectFolding(14)).toEqual({ at: 12, show: 8, budget: Number.POSITIVE_INFINITY });
    expect(folded.cards).toHaveLength(8);
    expect(folded.groups[0]).toMatchObject({ total: 14, hidden: 6, collapsible: true });
    const open = buildMapModel(payload(many, []), { ...OPTIONS, expanded: new Set([folded.groups[0].key]) });
    expect(open.cards).toHaveLength(14);
    expect(open.groups[0].hidden).toBe(0);
  });

  it("folds later folders of a large change to their header once the card budget is spent", () => {
    const many = Array.from({ length: 70 }, (_, index) =>
      node(`f${index}`, `src/pkg${Math.floor(index / 7)}/file_${index}.py`, "changed"),
    );
    const model = buildMapModel(payload(many, []), OPTIONS);
    expect(model.cards).toHaveLength(24);
    expect(model.groups.map((group) => group.folded)).toEqual([false, false, false, false, false, false, true, true, true, true]);
    expect(model.groups[6]).toMatchObject({ total: 7, hidden: 7, moreY: null });
    const folded = buildMapModel(payload(many, []), { ...OPTIONS, expanded: new Set([model.groups[7].key]) });
    expect(folded.cards).toHaveLength(31);
    expect(folded.groups.map((group) => group.folded)).toEqual([false, false, false, false, false, false, true, false, true, true]);
    expect(folded.groups[7].moreY).not.toBeNull();
    expect(model.height).toBeLessThan(2200);
  });

  it("lands incoming links on the changed symbol rows of a focused file", () => {
    const focus = node("f", "src/core.py", "changed", {
      symbols: [
        { id: "s1", label: "load", qualified_name: "core.load", path: "src/core.py", line: 10, end_line: 20, change: "modified", caller_count: 3, usage_count: 3, centrality_rank: null, centrality_percentile: null },
        { id: "s2", label: "save", qualified_name: "core.save", path: "src/core.py", line: 30, end_line: 40, change: "modified", caller_count: 1, usage_count: 1, centrality_rank: null, centrality_percentile: null },
      ],
    });
    const model = buildMapModel(
      payload(
        [focus, node("c", "src/api.py", "context", { lane: "used_by" })],
        [edge("c", "f", "calls", 5, { symbols: [{ name: "load", count: 4 }, { name: "save", count: 1 }] })],
        "file",
        "f",
      ),
      OPTIONS,
    );
    const card = model.cardById.get("f")!;
    expect(card.variant).toBe("file-focus");
    const branches = model.edges.filter((item) => item.id === "c->f:calls");
    expect(branches.map((item) => item.symbolId)).toEqual(["s1", "s2"]);
    expect(branches[0].d.endsWith(`${card.x - ARROW_LEN},${card.rows![0].y}`)).toBe(true);
  });

  it("folds a long symbol list but keeps the rows links land on", () => {
    const symbols = Array.from({ length: 30 }, (_, index) => ({
      id: `s${index}`,
      label: index === 29 ? "late" : index === 3 ? "if" : `field${index}`,
      qualified_name: "",
      kind: index === 29 ? "property_signature" : index < 5 ? "function_declaration" : "property_signature",
      path: "src/core.ts",
      line: index + 1,
      end_line: index + 1,
      change: "added",
      caller_count: -1,
      usage_count: -1,
      centrality_rank: null,
      centrality_percentile: null,
    }));
    const focus = node("f", "src/core.ts", "new", { symbols, symbol_count: 40 });
    const data = payload(
      [focus, node("c", "src/api.ts", "context", { lane: "used_by" })],
      [edge("c", "f", "calls", 2, { symbols: [{ name: "late", count: 2 }] })],
      "file",
      "f",
    );
    const folded = buildMapModel(data, OPTIONS).cardById.get("f")!;
    const labels = folded.rows!.map((row) => row.symbol.label);
    expect(labels).toHaveLength(12);
    expect(labels).not.toContain("if");
    expect(labels[labels.length - 1]).toBe("late");
    expect(labels.slice(0, 4)).toEqual(["field0", "field1", "field2", "field4"]);
    expect(folded.symbolFold).toEqual({ key: "symbols::f", hidden: 17, expanded: false, unlisted: 10 });
    const open = buildMapModel(data, { ...OPTIONS, expanded: new Set(["symbols::f"]) }).cardById.get("f")!;
    expect(open.rows).toHaveLength(29);
    expect(open.h).toBeGreaterThan(folded.h);
  });

  it("stays fast and overlap-free on a relationship-heavy review", () => {
    const heavyNodes: ReviewMapNode[] = [];
    const heavyEdges: ReviewMapEdge[] = [];
    for (let index = 0; index < 120; index += 1) heavyNodes.push(node(`c${index}`, `pkg/m${index % 15}/file_${index}.py`, "changed"));
    for (let index = 0; index < 200; index += 1) {
      heavyNodes.push(node(`x${index}`, `lib/n${index % 20}/ctx_${index}.py`, "context", { lane: index % 2 ? "used_by" : "uses" }));
    }
    for (let index = 0; index < 2000; index += 1) {
      const changed = `c${(index * 7) % 120}`;
      const other = index % 3 ? `x${(index * 13) % 200}` : `c${(index * 11) % 120}`;
      if (changed === other) continue;
      heavyEdges.push(edge(index % 2 ? changed : other, index % 2 ? other : changed, index % 5 ? "calls" : "references", 1 + (index % 7)));
    }
    const started = performance.now();
    const model = buildMapModel(payload(heavyNodes, heavyEdges), OPTIONS);
    const elapsed = performance.now() - started;
    expect(elapsed).toBeLessThan(500);
    expect(overlapsInLane(model)).toBe(false);
    // Folding keeps the map bounded: 15 folders of 8 files, 4 shown each until
    // the 24-card budget is spent, then the other nine fold to their headers.
    expect(model.laneOrder.subject.length).toBe(24);
    expect(model.groups.filter((group) => group.lane === "subject" && group.folded)).toHaveLength(9);
  });
});

describe("derived views", () => {
  const nodes = [
    node("a", "src/a.py", "changed", { rank: 2, review: { targets: 2, reviewed: 2, needs_changes: 0, changed_since_review: 0, open_threads: 0, attention: "normal" } }),
    node("b", "src/b.py", "changed", { rank: 1, symbols: [{ id: "sym", label: "parseInvoice", qualified_name: "b.parseInvoice", path: "src/b.py", line: 4, end_line: 9, change: "added", caller_count: 0, usage_count: 0, centrality_rank: null, centrality_percentile: null }] }),
    node("c", "src/c.py", "new", { rank: 3 }),
    node("s", "src/site.py", "impacted"),
  ];
  const edges = [edge("b", "s", "impacts"), edge("b", "c", "calls", 2)];

  it("queues unreviewed changed files by review priority", () => {
    expect(reviewQueue(payload(nodes, edges)).map((item) => item.id)).toEqual(["b", "c"]);
  });

  it("follows the server's queue position over the ranker's rank", () => {
    const progress = (outstanding: number, next_position: number) => ({
      targets: 2, reviewed: 2 - outstanding, needs_changes: 0, changed_since_review: 0, open_threads: 0,
      attention: "normal" as const, outstanding, next_position,
    });
    const queued = [
      node("loud", "src/loud.py", "changed", { rank: 1, review: progress(1, 4) }),
      node("asked", "src/asked.py", "changed", { rank: 9, review: progress(1, 0) }),
      node("done", "src/done.py", "changed", { rank: 2, review: progress(0, -1) }),
    ];
    expect(reviewQueue(payload(queued, [])).map((item) => item.id)).toEqual(["asked", "loud"]);
  });

  it("splits relationships by direction", () => {
    const { usedBy, uses } = relationshipsOf(payload(nodes, edges), "b");
    expect(usedBy.map((row) => row.otherId)).toEqual(["s"]);
    expect(uses.map((row) => row.otherId)).toEqual(["c"]);
  });

  it("finds files and changed symbols", () => {
    const hits = searchMap(payload(nodes, edges), "parse");
    expect(hits[0]).toMatchObject({ kind: "symbol", id: "sym", label: "parseInvoice" });
    expect(searchMap(payload(nodes, edges), "c.py")[0]).toMatchObject({ kind: "file", id: "c" });
  });

  it("derives a file focus from the overview for older servers", () => {
    const local = localFileFocus(payload(nodes, edges), "b")!;
    expect(local.level).toBe("file");
    expect(local.graph.focus).toBe("b");
    const lanes = Object.fromEntries(local.graph.nodes.map((item) => [item.id, item.lane]));
    expect(lanes).toEqual({ b: "", c: "uses", s: "used_by" });
  });

  it("speaks a reviewer's vocabulary for symbol kinds and ranking reasons", () => {
    expect(symbolKindLabel("lexical_declaration")).toBe("variable");
    expect(symbolKindLabel("property_signature")).toBe("field");
    expect(symbolKindLabel("method_signature")).toBe("method");
    expect(symbolKindLabel("type_alias_declaration")).toBe("type");
    expect(symbolKindLabel("interface_declaration")).toBe("interface");
    expect(symbolKindLabel("function")).toBe("function");
    expect(symbolKindLabel("")).toBe("symbol");
    expect(reasonsOf({ reasons: ["+578 -0", "substantial new production surface"] })).toEqual([
      "substantial new production surface",
      "large change",
    ]);
    // A 13-line new file is not a large change: its diffstat alone gives no reason.
    expect(reasonsOf({ reasons: ["+13 -0"] })).toEqual([]);
    expect(reasonsOf({ reasons: ["+399 -0"] })).toEqual([]);
    expect(reasonsOf({ reasons: ["+200 -200"] })).toEqual(["large change"]);
  });

  it("keeps the specific end of long folder paths", () => {
    expect(shortModule("src/lemoncrow/pro/capabilities/review", 24)).toBe("…/capabilities/review");
    expect(shortModule("")).toBe("(repository root)");
  });
});

describe("testReach", () => {
  const shop = () => [
    node("auth", "app/auth.py", "changed"),
    node("orders", "app/orders.py", "changed"),
    node("gone", "app/legacy.py", "changed", { change: "deleted" }),
    node("views", "app/views.py", "context"),
    node("test", "tests/test_orders.py", "context", { category: "test" }),
  ];
  const links = () => [
    edge("views", "auth", "calls", 2),
    edge("views", "orders", "calls", 3),
    edge("test", "orders", "calls", 2),
  ];

  it("names the tests that reach a changed file and marks the one none reaches", () => {
    const reach = testReach(payload(shop(), links()));
    expect(reach.get("orders")).toEqual({ state: "reached", tests: [expect.objectContaining({ id: "test" })] });
    expect(reach.get("auth")?.state).toBe("none");
    // Neither surrounding code, tests, nor deleted files get a verdict.
    expect(reach.has("views")).toBe(false);
    expect(reach.has("test")).toBe(false);
    expect(reach.has("gone")).toBe(false);
  });

  it("an impact edge into a test site counts as reach", () => {
    const reach = testReach(payload(shop(), [edge("auth", "test", "impacts")]));
    expect(reach.get("auth")?.state).toBe("reached");
  });

  it("never claims no test when reach was not measured or the map was cut short", () => {
    const unavailable = payload(shop(), links());
    unavailable.context = { ...unavailable.context, source: "unavailable" };
    expect(testReach(unavailable).get("auth")?.state).toBe("unmeasured");
    const truncated = payload(shop(), links());
    truncated.graph = { ...truncated.graph, truncated: true };
    expect(testReach(truncated).get("auth")?.state).toBe("unmeasured");
    const indexing = payload(shop(), []);
    indexing.context = { ...indexing.context, readiness: { graph: { state: "building" } } };
    expect(testReach(indexing).get("auth")?.state).toBe("unmeasured");
    // What was found is still found.
    expect(testReach(truncated).get("orders")?.state).toBe("reached");
  });
});
