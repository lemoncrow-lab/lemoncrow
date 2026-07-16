import {
  buildCodeMapHierarchy,
  buildCodeMapView,
  nextCodeMapDetailStep,
} from "./codeMapLod";
import type { CodeMapEdge, CodeMapNode } from "../api";

const nodes: CodeMapNode[] = [
  {
    id: "file-a",
    label: "a.ts",
    qualified_name: "src/a.ts",
    path: "src/a.ts",
    kind: "source",
    language: "TypeScript",
    line: 1,
    end_line: 20,
    node_type: "file",
    file_type: "source",
    community: "src/a",
    color: "#60a5fa",
  },
  {
    id: "file-b",
    label: "b.ts",
    qualified_name: "src/b.ts",
    path: "src/b.ts",
    kind: "source",
    language: "TypeScript",
    line: 1,
    end_line: 20,
    node_type: "file",
    file_type: "source",
    community: "src/b",
    color: "#f59e0b",
  },
  {
    id: "symbol-a",
    label: "alpha",
    qualified_name: "alpha",
    path: "src/a.ts",
    kind: "function",
    language: "TypeScript",
    line: 2,
    end_line: 5,
    node_type: "symbol",
    file_type: "source",
    community: "src/a",
    color: "#60a5fa",
  },
  {
    id: "symbol-b",
    label: "beta",
    qualified_name: "beta",
    path: "src/b.ts",
    kind: "function",
    language: "TypeScript",
    line: 2,
    end_line: 5,
    node_type: "symbol",
    file_type: "source",
    community: "src/b",
    color: "#f59e0b",
  },
];

const edges: CodeMapEdge[] = [
  {
    id: "call-1",
    source: "symbol-a",
    target: "symbol-b",
    kind: "calls",
    depth: 1,
    weight: 3,
  },
  {
    id: "contains-a",
    source: "file-a",
    target: "symbol-a",
    kind: "contains",
    depth: 0,
  },
  {
    id: "contains-b",
    source: "file-b",
    target: "symbol-b",
    kind: "contains",
    depth: 0,
  },
];

const onlyA = {
  communityIds: new Set(["community::src/a"]),
  fileIds: new Set(["file-a"]),
};

describe("code-map spatial detail", () => {
  it("unfolds only the community inside the current view", () => {
    const hierarchy = buildCodeMapHierarchy(nodes, edges);
    const overview = buildCodeMapView(hierarchy, 0, onlyA);
    const files = buildCodeMapView(hierarchy, 1, onlyA);

    expect(overview.nodes.map((node) => node.node_type)).toEqual([
      "community",
      "community",
    ]);
    expect(files.nodes.map((node) => node.id)).toContain("file-a");
    expect(files.nodes.map((node) => node.id)).not.toContain("file-b");
  });

  it("pins every direct caller, callee, parent file, and connecting edge", () => {
    const hierarchy = buildCodeMapHierarchy(nodes, edges);
    const view = buildCodeMapView(
      hierarchy,
      0,
      { communityIds: new Set(), fileIds: new Set() },
      [],
      "symbol-a"
    );

    expect(view.nodes.map((node) => node.id)).toEqual(
      expect.arrayContaining(["file-a", "file-b", "symbol-a", "symbol-b"])
    );
    expect(view.edges.map((edge) => edge.id)).toContain("call-1");
  });

  it("shows every point only through the explicit all-points view", () => {
    const hierarchy = buildCodeMapHierarchy(nodes, edges);
    const view = buildCodeMapView(
      hierarchy,
      0,
      { communityIds: new Set(), fileIds: new Set() },
      [],
      null,
      true
    );

    expect(view.detail).toBe("full");
    expect(view.hiddenCount).toBe(0);
  });

  it("uses bounded zoom steps with hysteresis", () => {
    expect(nextCodeMapDetailStep(0, 0.7)).toBe(1);
    expect(nextCodeMapDetailStep(1, 0.62)).toBe(1);
    expect(nextCodeMapDetailStep(1, 0.5)).toBe(2);
    expect(nextCodeMapDetailStep(2, 0.7)).toBe(1);
    expect(nextCodeMapDetailStep(8, 1)).toBe(0);
  });
});
