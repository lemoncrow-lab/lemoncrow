import { describe, expect, it } from "vitest";

import { buildColumnModel, fitZoom, foldId, foldModules, stronglyConnected, type ColumnNode } from "./columnModel";

function module(id: string, extra: Partial<ColumnNode> = {}): ColumnNode {
  const slash = id.lastIndexOf("/");
  return { id, label: slash >= 0 ? id.slice(slash + 1) : id, parent: slash > 0 ? id.slice(0, slash) : "", files: 4, ...extra };
}

const OPTIONS = { width: 1200, maxCards: 40, expanded: new Set<string>() };

describe("buildColumnModel", () => {
  it("places every module left of what it depends on", () => {
    const model = buildColumnModel(
      [module("cli"), module("api"), module("core"), module("util")],
      [
        { source: "cli", target: "api", weight: 3 },
        { source: "api", target: "core", weight: 5 },
        { source: "cli", target: "core", weight: 1 },
        { source: "core", target: "util", weight: 2 },
      ],
      OPTIONS,
    );
    const column = (id: string) => model.cardById.get(id)!.column;
    expect([column("cli"), column("api"), column("core"), column("util")]).toEqual([0, 1, 2, 3]);
    for (const edge of model.edges) {
      expect(model.cardById.get(edge.from)!.x).toBeLessThan(model.cardById.get(edge.to)!.x);
    }
    expect(model.cycles).toEqual([]);
  });

  it("fits five dependency columns into the Code map pane at 1440 px without sideways scrolling", () => {
    // The centre pane between the 270 px explorer and the 320 px inspector is about 850 px wide.
    const chain = ["pages", "components", "hooks", "lib", "billing"];
    const model = buildColumnModel(
      chain.map((id) => module(`src/${id}`)),
      chain.slice(1).map((id, index) => ({ source: `src/${chain[index]}`, target: `src/${id}`, weight: 2 })),
      { ...OPTIONS, width: 850 },
    );
    expect(new Set(model.cards.map((card) => card.column)).size).toBe(5);
    expect(model.width).toBeLessThanOrEqual(850);
    for (const card of model.cards) expect(card.x + card.w).toBeLessThanOrEqual(850);
    expect(fitZoom(model.width, 850)).toBe(1);
    // A roomy pane keeps the full-size cards.
    const roomy = buildColumnModel(chain.map((id) => module(`src/${id}`)), [], { ...OPTIONS, width: 1600 });
    expect(Math.min(...roomy.cards.map((card) => card.w))).toBeGreaterThanOrEqual(168);
  });

  it("draws a map wider than its pane smaller, down to a readable floor", () => {
    expect(fitZoom(1000, 0)).toBe(1);
    expect(fitZoom(800, 850)).toBe(1);
    expect(fitZoom(1000, 850)).toBe(0.85);
    expect(fitZoom(3000, 850)).toBe(0.7);
  });

  it("keeps every edge label in the gap beside its left card, never over a card", () => {
    const model = buildColumnModel(
      [module("cli"), module("api"), module("core"), module("util")],
      [
        { source: "cli", target: "api", weight: 3 },
        { source: "api", target: "core", weight: 5 },
        { source: "cli", target: "util", weight: 1 },
        { source: "core", target: "util", weight: 2 },
      ],
      OPTIONS,
    );
    for (const edge of model.edges) {
      const left = [model.cardById.get(edge.from)!, model.cardById.get(edge.to)!].sort((p, q) => p.x - q.x)[0];
      expect(edge.labelMin).toBeGreaterThanOrEqual(left.x + left.w);
      expect(edge.labelX).toBeGreaterThanOrEqual(edge.labelMin);
      expect(edge.labelX).toBeLessThanOrEqual(edge.labelMax);
      // No card overlaps the gap, so a label inside it covers none.
      for (const card of model.cards) {
        expect(card.x >= edge.labelMax || card.x + card.w <= edge.labelMin).toBe(true);
      }
    }
    // The edge spanning three columns is labelled in its first gap, on its own row.
    const span = model.edges.find((edge) => edge.from === "cli" && edge.to === "util")!;
    const cli = model.cardById.get("cli")!;
    expect(span.labelY).toBeGreaterThanOrEqual(cli.y - 20);
    expect(span.labelY).toBeLessThanOrEqual(cli.y + cli.h + 20);
  });

  it("condenses a dependency cycle into one marked group in one column", () => {
    const model = buildColumnModel(
      [module("app"), module("a"), module("b"), module("base")],
      [
        { source: "app", target: "a", weight: 1 },
        { source: "a", target: "b", weight: 1 },
        { source: "b", target: "a", weight: 1 },
        { source: "b", target: "base", weight: 1 },
      ],
      OPTIONS,
    );
    expect(model.cycles).toHaveLength(1);
    expect(model.cycles[0].members.sort()).toEqual(["a", "b"]);
    expect(model.cardById.get("a")!.column).toBe(model.cardById.get("b")!.column);
    expect(model.cardById.get("a")!.cycle).not.toBeNull();
    expect(model.cardById.get("base")!.column).toBeGreaterThan(model.cardById.get("a")!.column);
    // Inside the cycle, cards are stacked without overlap.
    const [first, second] = model.cycles[0].members.map((id) => model.cardById.get(id)!).sort((p, q) => p.y - q.y);
    expect(second.y).toBeGreaterThanOrEqual(first.y + first.h);
  });

  it("puts modules with no links in their own trailing column", () => {
    const model = buildColumnModel([module("a"), module("b"), module("docs")], [{ source: "a", target: "b", weight: 1 }], OPTIONS);
    const unlinked = model.columns.find((column) => column.unlinked)!;
    expect(unlinked.index).toBe(model.cardById.get("docs")!.column);
    expect(unlinked.index).toBeGreaterThan(model.cardById.get("b")!.column);
  });

  it("folds sibling modules into their parent to keep the first frame within budget", () => {
    const nodes = [
      ...Array.from({ length: 30 }, (_, index) => module(`src/pkg/m${index}`, { changed: index === 3 ? 2 : 0 })),
      ...Array.from({ length: 20 }, (_, index) => module(`tests/t${index}`)),
      module("docs"),
    ];
    const edges = [{ source: "tests/t0", target: "src/pkg/m3", weight: 4 }];
    const model = buildColumnModel(nodes, edges, OPTIONS);
    expect(model.cards.length).toBeLessThanOrEqual(40);
    const fold = model.cardById.get(foldId("src/pkg"))!;
    expect(fold.fold).toBe(true);
    expect(fold.members).toHaveLength(30);
    expect(fold.changed).toBe(2);
    expect(model.cardOf.get("src/pkg/m3")).toBe(foldId("src/pkg"));
    // Links are re-pointed at the fold.
    expect(model.edges.some((edge) => edge.to === foldId("src/pkg"))).toBe(true);

    const opened = buildColumnModel(nodes, edges, { ...OPTIONS, expanded: new Set([foldId("src/pkg")]) });
    expect(opened.cardById.has("src/pkg/m3")).toBe(true);
    expect(opened.cardById.has(foldId("src/pkg"))).toBe(false);
  });

  it("is deterministic", () => {
    const nodes = Array.from({ length: 12 }, (_, index) => module(`m${index}`));
    const edges = Array.from({ length: 20 }, (_, index) => ({ source: `m${index % 12}`, target: `m${(index * 5 + 3) % 12}`, weight: 1 + (index % 3) }));
    const a = buildColumnModel(nodes, edges, OPTIONS);
    const b = buildColumnModel([...nodes].reverse(), [...edges].reverse(), OPTIONS);
    expect(a.cards.map((card) => [card.id, card.x, card.y])).toEqual(b.cards.map((card) => [card.id, card.x, card.y]));
  });
});

describe("helpers", () => {
  it("finds strongly connected components", () => {
    const out = new Map([
      ["a", new Set(["b"])],
      ["b", new Set(["c"])],
      ["c", new Set(["a"])],
      ["d", new Set(["a"])],
    ]);
    const components = stronglyConnected(["a", "b", "c", "d"], out).map((component) => component.join(","));
    expect(components.sort()).toEqual(["a,b,c", "d"]);
  });

  it("keeps a fold the reader opened", () => {
    const nodes = Array.from({ length: 6 }, (_, index) => module(`x/m${index}`));
    expect(new Set(foldModules(nodes, 2, new Set()).values())).toEqual(new Set([foldId("x")]));
    expect(new Set(foldModules(nodes, 2, new Set([foldId("x")])).values()).size).toBe(6);
  });
});
