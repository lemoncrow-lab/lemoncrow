import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewMapPayload } from "./types";

const api = vi.hoisted(() => ({
  fetchHistoricalReviewMap: vi.fn(),
  explainHistoricalReviewArchitecture: vi.fn(),
  fetchHistoricalAnnotations: vi.fn(async () => ({ annotations: [] })),
  fetchHistoricalTargets: vi.fn(async () => ({ targets: [] })),
}));

vi.mock("./reviewApi", () => api);

import ReviewMapSheet from "./ReviewMapSheet";
import ReviewMapInspector from "./ReviewMapInspector";
import { REVIEW_MAP_OVERVIEW, reviewMapLocationFromSelection } from "./reviewMapLocation";

const SNAPSHOT: ReviewMapPayload["snapshot"] = {
  exactness: "snapshot",
  tree_fingerprint: "tree",
  source_fingerprint: "source",
  base_sha: "a",
  head_sha: "b",
  degraded: [],
  provenance: { host: "codex", model: "gpt-5.6", certainty: "exact", method: "recorded_edit" },
  verification: { pass: 3, fail: 1, not_run: 0, unknown: 1, checks: [] },
};

const CONTEXT: ReviewMapPayload["context"] = {
  source: "current_index",
  exact_for_snapshot: false,
  snapshot_head_matches_checkout: false,
  index: {},
  readiness: { graph: { state: "ready" } },
  note: "Surrounding calls come from the current Code Map index.",
};

function selection(nodeId: string, level: "architecture" | "file" | "symbol" = "architecture", extra = {}) {
  return { kind: "review_map_node", review_id: "review-1", revision_id: "rrv-1", level, node_id: nodeId, exactness: "snapshot", ...extra };
}

const SECURITY = {
  id: "file:security",
  label: "auth/security.py",
  kind: "file",
  component: "src",
  module: "src/auth",
  path: "src/auth/security.py",
  line: 0,
  end_line: 0,
  status: "changed" as const,
  change: "modified",
  exactness: "snapshot" as const,
  additions: 42,
  deletions: 7,
  rank: 1,
  reasons: ["public contract changed", "7 known callers"],
  review: { targets: 2, reviewed: 1, needs_changes: 0, changed_since_review: 0, open_threads: 1, attention: "high" as const },
  target_ids: ["target-authorize"],
  important_public_contract: true,
  boundaries: ["security"],
  boundary_basis: "path",
  symbol_count: 1,
  symbols: [
    {
      id: "symbol:authorize",
      label: "authorize",
      qualified_name: "app.auth.authorize",
      kind: "function",
      path: "src/auth/security.py",
      line: 30,
      end_line: 55,
      change: "modified",
      caller_count: 7,
      usage_count: 9,
      centrality_rank: 4,
      centrality_percentile: 0.97,
    },
  ],
  selection: selection("file:security", "architecture", { path: "src/auth/security.py", target_ids: ["target-authorize"] }),
};

const TEST_FILE = {
  ...SECURITY,
  id: "file:test",
  label: "test_security.py",
  module: "tests",
  path: "tests/test_security.py",
  category: "test",
  rank: 2,
  reasons: [],
  review: null,
  important_public_contract: false,
  boundaries: [],
  symbols: [],
  symbol_count: 0,
  selection: selection("file:test"),
};

const ROUTES = {
  ...SECURITY,
  id: "file:routes",
  label: "api/routes.py",
  module: "src/api",
  path: "src/api/routes.py",
  status: "impacted" as const,
  change: "",
  lane: "used_by" as const,
  additions: 0,
  deletions: 0,
  rank: 0,
  reasons: [],
  review: null,
  important_public_contract: false,
  boundaries: [],
  symbols: [],
  symbol_count: 0,
  target_ids: [],
  selection: selection("file:routes", "architecture", { path: "src/api/routes.py" }),
};

const CACHE = {
  ...ROUTES,
  id: "file:cache",
  label: "cache.py",
  module: "src/infra",
  path: "src/infra/cache.py",
  status: "context" as const,
  exactness: "current_index" as const,
  lane: "uses" as const,
  selection: { ...selection("file:cache"), exactness: "current_index" },
};

function edgeSelection(id: string, source: string, target: string, relation: string, exactness = "snapshot") {
  return { kind: "review_map_edge", review_id: "review-1", revision_id: "rrv-1", level: "architecture" as const, focus_node_id: "", edge_id: id, source_id: source, target_id: target, relation, exactness };
}

const ARCHITECTURE: ReviewMapPayload = {
  review_id: "review-1",
  revision_id: "rrv-1",
  revision_number: 4,
  level: "architecture",
  snapshot: SNAPSHOT,
  context: CONTEXT,
  graph: {
    level: "architecture",
    focus: "file:security",
    truncated: false,
    omitted_node_count: 0,
    stats: { changed_files: 2, new_files: 0, impacted_files: 1, filtered_relationships: 3 },
    nodes: [SECURITY, TEST_FILE, ROUTES, CACHE],
    edges: [
      {
        id: "edge:impact",
        source: "file:security",
        target: "file:routes",
        kind: "impacts",
        exactness: "snapshot",
        weight: 1,
        uncertain: false,
        evidence: [{ kind: "untouched_caller", path: "src/api/routes.py", line: 88, uncertainty: "", symbol: "authorize", snippet: "authorize(request)" }],
        symbols: [{ name: "authorize", count: 1 }],
        selection: edgeSelection("edge:impact", "file:security", "file:routes", "impacts"),
      },
      {
        id: "edge:cache",
        source: "file:security",
        target: "file:cache",
        kind: "calls",
        exactness: "current_index",
        weight: 3,
        uncertain: false,
        evidence: [{ kind: "calls", path: "src/auth/security.py", line: 40, uncertainty: "", symbol: "get" }],
        selection: edgeSelection("edge:cache", "file:security", "file:cache", "calls", "current_index"),
      },
      {
        id: "edge:test",
        source: "file:test",
        target: "file:security",
        kind: "calls",
        exactness: "current_index",
        weight: 5,
        uncertain: false,
        selection: edgeSelection("edge:test", "file:test", "file:security", "calls", "current_index"),
      },
    ],
  },
};

const ARCHITECTURE_DELTA: ReviewMapPayload = {
  ...ARCHITECTURE,
  projection: "architecture_delta",
  architecture_delta: {
    version: 1,
    digest: "delta-digest",
    status: "ready",
    reason: "",
    base_identity: "base-exact",
    snapshot_identity: "snapshot-exact",
    grouping: {
      strategy: "directory",
      version: 1,
      stable_for_identical_input: true,
      components: [
        { id: "component:auth", label: "src/auth", delta_status: "changed", base_file_count: 1, snapshot_file_count: 1, paths: ["src/auth/security.py"], paths_truncated: false },
        { id: "component:api", label: "src/api", delta_status: "changed", base_file_count: 1, snapshot_file_count: 1, paths: ["src/api/routes.py"], paths_truncated: false },
      ],
    },
    summary: {
      material: true,
      status: "ready",
      changed_components: 2,
      changed_nodes: 0,
      changed_edges: 1,
      added_edges: 1,
      removed_edges: 0,
      modified_edges: 0,
      context_only_edges: 1,
      unknown_edges: 0,
      important_concerns: ["Security boundary touched"],
      exactness: "snapshot",
      degraded: [],
    },
    flow: {
      applicable: false,
      kind: "none",
      reason: "The exact relationship delta is too small for a useful sequence/flow explanation.",
      steps: [],
    },
    graph: {} as never,
  },
  graph: {
    level: "architecture",
    focus: "file:security",
    truncated: false,
    omitted_node_count: 0,
    stats: { changed_files: 1, impacted_files: 1, architecture_changed_edges: 1, architecture_context_edges: 1 },
    nodes: [
      {
        ...SECURITY,
        delta_status: "changed",
        architecture_provenance: { source: "snapshot_comparison", exactness: "snapshot", comparison: "base_vs_snapshot", note: "Compared from immutable base to Snapshot." },
        selection: {
          ...SECURITY.selection,
          projection: "architecture_delta",
          delta_status: "changed",
          architecture_source: "snapshot_comparison",
          architecture_comparison: "base_vs_snapshot",
        },
      },
      {
        ...ROUTES,
        delta_status: "unchanged",
        architecture_provenance: { source: "snapshot_comparison", exactness: "snapshot", comparison: "base_vs_snapshot", note: "Compared from immutable base to Snapshot." },
        selection: {
          ...ROUTES.selection,
          projection: "architecture_delta",
          delta_status: "unchanged",
          architecture_source: "snapshot_comparison",
          architecture_comparison: "base_vs_snapshot",
        },
      },
      {
        ...CACHE,
        delta_status: "context_only",
        architecture_provenance: { source: "current_index", exactness: "current_index", comparison: "none", note: "Current-index relationship is context only." },
        selection: {
          ...CACHE.selection,
          projection: "architecture_delta",
          delta_status: "context_only",
          architecture_source: "current_index",
          architecture_comparison: "none",
        },
      },
    ],
    edges: [
      {
        id: "architecture-edge:added",
        source: "file:security",
        target: "file:routes",
        kind: "references",
        exactness: "snapshot",
        delta_status: "added",
        base_weight: 0,
        snapshot_weight: 1,
        weight: 1,
        uncertain: false,
        evidence: [{ kind: "reference", path: "src/auth/security.py", line: 40, uncertainty: "", symbol: "route" }],
        architecture_provenance: { source: "snapshot_comparison", exactness: "snapshot", comparison: "base_vs_snapshot", note: "Derived from exact immutable side comparison." },
        selection: {
          ...edgeSelection("architecture-edge:added", "file:security", "file:routes", "references"),
          projection: "architecture_delta",
          delta_status: "added",
          architecture_source: "snapshot_comparison",
          architecture_comparison: "base_vs_snapshot",
          path: "src/auth/security.py",
          line: 40,
        },
      },
      {
        id: "architecture-context-edge:cache",
        source: "file:security",
        target: "file:cache",
        kind: "calls",
        exactness: "current_index",
        delta_status: "context_only",
        base_weight: 0,
        snapshot_weight: 0,
        weight: 1,
        uncertain: false,
        architecture_provenance: { source: "current_index", exactness: "current_index", comparison: "none", note: "Current-index relationship is context only." },
        selection: {
          ...edgeSelection("architecture-context-edge:cache", "file:security", "file:cache", "calls", "current_index"),
          projection: "architecture_delta",
          delta_status: "context_only",
          architecture_source: "current_index",
          architecture_comparison: "none",
        },
      },
    ],
  },
};
ARCHITECTURE_DELTA.architecture_delta!.graph = ARCHITECTURE_DELTA.graph;

describe("Map source-relationship truth and missing-impact limits", () => {
  const props = {
    overview: null, selected: null, onSelectNode: vi.fn(), onSelectEdge: vi.fn(), onClear: vi.fn(),
    onFocusFile: vi.fn(), onOpenSymbol: vi.fn(), onOpenSelection: vi.fn(), onAskSelection: vi.fn(),
  };

  it("does not infer no impact when available static relationships are empty", () => {
    render(<ReviewMapInspector {...props} payload={{
      ...ARCHITECTURE,
      graph: { ...ARCHITECTURE.graph, nodes: [SECURITY], edges: [], stats: { changed_files: 1, new_files: 0, impacted_files: 0, filtered_relationships: 0 } },
    }} selectedEdge={null} />);
    expect(screen.getByText(/No unchanged file is shown by the available relationships/)).toBeInTheDocument();
    expect(screen.getByText(/not an observed runtime execution path/)).toBeInTheDocument();
    expect(screen.queryByText(/No unchanged file was found to be reached/)).toBeNull();
  });

  it("identifies current-index call links as non-Snapshot, non-runtime evidence", () => {
    render(<ReviewMapInspector {...props} payload={ARCHITECTURE} selectedEdge={ARCHITECTURE.graph.edges[1]} />);
    expect(screen.getByText(/current index \(may differ from Snapshot\)/)).toBeInTheDocument();
    expect(screen.getByText(/not runtime proof/)).toBeInTheDocument();
    expect(screen.queryByText(/Snapshot fact/)).toBeNull();
  });
});

const FILE_LEVEL: ReviewMapPayload = {
  ...ARCHITECTURE,
  level: "file",
  graph: {
    level: "file",
    focus: "file:security",
    truncated: false,
    nodes: [
      { ...SECURITY, selection: selection("file:security", "file", { focus_node_id: "file:security" }) },
      { ...ROUTES, selection: selection("file:routes", "file", { focus_node_id: "file:security" }) },
    ],
    edges: [{ ...ARCHITECTURE.graph.edges[0], selection: { ...ARCHITECTURE.graph.edges[0].selection, level: "file", focus_node_id: "file:security" } }],
  },
};

const SYMBOL: ReviewMapPayload = {
  ...ARCHITECTURE,
  level: "symbol",
  graph: {
    level: "symbol",
    focus: "symbol:authorize",
    truncated: false,
    nodes: [
      {
        id: "symbol:authorize",
        label: "authorize",
        qualified_name: "app.auth.authorize",
        kind: "function",
        path: "src/auth/security.py",
        line: 30,
        end_line: 55,
        status: "changed",
        change: "modified",
        exactness: "snapshot",
        target_ids: ["target-authorize"],
        important_public_contract: true,
        boundaries: ["security"],
        boundary_basis: "path",
        caller_count: 7,
        centrality_rank: 4,
        selection: selection("symbol:authorize", "symbol", { focus_node_id: "symbol:authorize", path: "src/auth/security.py", line: 30, target_ids: ["target-authorize"] }),
      },
      {
        id: "current:caller",
        label: "handle_request",
        qualified_name: "app.api.handle_request",
        kind: "function",
        path: "src/api/routes.py",
        line: 70,
        end_line: 105,
        status: "context",
        exactness: "current_index",
        target_ids: [],
        important_public_contract: false,
        boundaries: [],
        boundary_basis: "",
        selection: { ...selection("current:caller", "symbol", { focus_node_id: "symbol:authorize" }), exactness: "current_index" },
      },
    ],
    edges: [
      {
        id: "edge:caller",
        source: "current:caller",
        target: "symbol:authorize",
        kind: "call",
        exactness: "current_index",
        weight: 1,
        uncertain: false,
        selection: { ...edgeSelection("edge:caller", "current:caller", "symbol:authorize", "call", "current_index"), level: "symbol" as const },
      },
    ],
  },
};

function renderSheet(props: Partial<Parameters<typeof ReviewMapSheet>[0]> = {}) {
  const handlers = { onClose: vi.fn(), onOpenSelection: vi.fn(), onAskSelection: vi.fn() };
  render(<ReviewMapSheet reviewId="review-1" revisionNumber={4} historical={false} {...handlers} {...props} />);
  return handlers;
}

const card = (name: string) => screen.findByRole("button", { name: new RegExp("^" + name.replace(".", "\\.") + ",") });
const inspector = () => screen.getByRole("complementary", { name: "Review map inspector" });

describe("ReviewMapSheet", () => {
  const width = window.innerWidth;
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    api.fetchHistoricalReviewMap.mockReset().mockImplementation(async (_id: string, _revision: number, level = "architecture", nodeId = "", projection = "change_map") => {
      if (projection === "architecture_delta") return ARCHITECTURE_DELTA;
      if (level === "file") return FILE_LEVEL;
      if (level === "symbol" && nodeId === "symbol:authorize") return SYMBOL;
      return ARCHITECTURE;
    });
    api.explainHistoricalReviewArchitecture.mockReset().mockResolvedValue({
      status: "ready",
      architecture_delta_digest: "delta-digest",
      result: {
        summary: "The exact added reference broadens the dependency shape.",
        why_it_matters: ["Auth now reaches API code through a new exact relationship."],
        grounded_edge_ids: ["architecture-edge:added"],
        flow: [],
        limitations: ["A reference edge does not prove runtime order."],
        provenance: "inferred_explanation",
        exactness: "inferred",
      },
      intelligence: { cache_hit: false },
    });
  });
  afterEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  });

  it("drops old Snapshot nodes during render when switching revisions, before fetch can resolve", async () => {
    const never = new Promise<ReviewMapPayload>(() => undefined);
    api.fetchHistoricalReviewMap.mockImplementation(async (_id: string, revision: number) => revision === 4 ? ARCHITECTURE : never);
    const seenOnRefresh: boolean[] = [];
    function Probe({ revision }: { revision: number }) {
      useLayoutEffect(() => {
        if (revision === 5) seenOnRefresh.push(Boolean(document.body.textContent?.includes("auth/security.py")));
      }, [revision]);
      return <ReviewMapSheet reviewId="review-1" revisionNumber={revision} historical={false}
        onClose={vi.fn()} onOpenSelection={vi.fn()} onAskSelection={vi.fn()} />;
    }
    const view = render(<Probe revision={4} />);
    await card("security.py");
    view.rerender(<Probe revision={5} />);
    expect(seenOnRefresh).toEqual([false]);
    expect(screen.queryByText("auth/security.py")).toBeNull();
    expect(screen.queryByRole("button", { name: "security.py, changed file" })).toBeNull();
  });

  it("explains folded changed files in a real 401-file Map and lets the reviewer expand them", async () => {
    const nodes = Array.from({ length: 401 }, (_, index) => ({
      ...SECURITY,
      id: `file:large-${index}`,
      label: `large-${index}.py`,
      path: `src/group-${Math.floor(index / 11)}/large-${index}.py`,
      module: `src/group-${Math.floor(index / 11)}`,
      target_ids: [`target-${index}`],
      selection: selection(`file:large-${index}`, "architecture", { path: `src/group-${Math.floor(index / 11)}/large-${index}.py` }),
    }));
    api.fetchHistoricalReviewMap.mockResolvedValue({
      ...ARCHITECTURE,
      graph: { ...ARCHITECTURE.graph, focus: null, nodes, edges: [], stats: {
        changed_files: 401, new_files: 0, impacted_files: 0, filtered_relationships: 0,
      } },
    });
    renderSheet();
    const summary = await screen.findByTestId("map-folded-summary");
    expect(summary).toHaveTextContent(/Showing \d+ of 401 mapped changed files/);
    const before = summary.textContent ?? "";
    const folds = screen.getAllByRole("button", { name: /Show \d+ changed files in src\/group-/ });
    expect(folds.length).toBeGreaterThan(0);
    await userEvent.click(folds[0]);
    expect(summary).not.toHaveTextContent(before);
    expect(summary).toHaveTextContent(/folded into expandable groups/);
  });

  it("opens on the change: changed files in the middle, dependents and dependencies around them, and where to start", async () => {
    renderSheet();
    // A view of the Review shell, not a modal.
    expect(await screen.findByRole("region", { name: "Review Map" })).toBeTruthy();
    expect(document.querySelector('[aria-modal="true"]')).toBeNull();
    const canvas = await screen.findByRole("group", { name: "Review map canvas" });
    expect(within(canvas).getByText("Changed")).toBeTruthy();
    expect(within(canvas).getByText("Used by")).toBeTruthy();
    expect(within(canvas).getByText("Uses")).toBeTruthy();
    expect(await card("security.py")).toBeTruthy();
    expect(within(canvas).getByRole("button", { name: "routes.py, unchanged source linked to the change" })).toBeTruthy();
    expect(within(canvas).getByRole("button", { name: "cache.py, surrounding code" })).toBeTruthy();
    expect(screen.getByText(/Snapshot 4 · 2 files changed · 1 affected outside the change/)).toBeTruthy();

    const guide = inspector();
    expect(within(guide).getByText("Start here")).toBeTruthy();
    expect(within(guide).getByRole("button", { name: "Select security.py" })).toBeTruthy();
    expect(within(guide).getByText("public contract changed")).toBeTruthy();
    expect(within(guide).getByText(/3 name-collision links/)).toBeTruthy();
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 4);
  });

  it("projects Architecture Delta through the existing map and preserves provenance into Ask", async () => {
    const selectionHref = (item: { path?: string }) => item.path ? `/review-target/${item.path}` : null;
    const { onAskSelection } = renderSheet({ selectionHref });
    await card("security.py");

    await userEvent.click(screen.getByRole("radio", { name: "Architecture Δ" }));
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith(
      "review-1",
      4,
      "architecture",
      "",
      "architecture_delta",
    ));

    const guide = inspector();
    expect(within(guide).getByRole("heading", { name: "Architecture delta" })).toBeTruthy();
    expect(within(guide).getByText(/2 components and 1 relationship changed/)).toBeTruthy();
    expect(within(guide).getByText(/too small for a useful sequence\/flow explanation/)).toBeTruthy();
    expect(screen.getByText(/Snapshot 4 · 2 components changed · 1 relationship changes · base ↔ Snapshot/)).toBeTruthy();

    await userEvent.click(await card("security.py"));
    await userEvent.click(within(inspector()).getByRole("button", { name: "Ask about this file" }));
    expect(onAskSelection).toHaveBeenLastCalledWith(
      expect.objectContaining({
        kind: "review_map_node",
        projection: "architecture_delta",
        delta_status: "changed",
        architecture_source: "snapshot_comparison",
        architecture_comparison: "base_vs_snapshot",
      }),
      "auth/security.py",
    );

    const exactEdge = document.querySelector('[data-edge-id="architecture-edge:added"]');
    expect(exactEdge).toBeTruthy();
    fireEvent.click(exactEdge!);
    expect(within(inspector()).getByText(/Relationship weight 0 → 1/)).toBeTruthy();
    // The in-Review jump is primary; the dedicated tab stays a secondary link.
    expect(within(inspector()).getByRole("button", { name: "Open Review target" })).toBeTruthy();
    const reviewTarget = within(inspector()).getByRole("link", { name: "Open Review target in a new tab" });
    expect(reviewTarget.getAttribute("href")).toBe("/review-target/src/auth/security.py");
    expect(reviewTarget.getAttribute("target")).toBe("_blank");
    await userEvent.click(within(inspector()).getByRole("button", { name: "Ask about this link" }));
    expect(onAskSelection).toHaveBeenLastCalledWith(
      expect.objectContaining({
        kind: "review_map_edge",
        edge_id: "architecture-edge:added",
        exactness: "snapshot",
        delta_status: "added",
        architecture_source: "snapshot_comparison",
      }),
      expect.any(String),
    );

    await userEvent.click(within(inspector()).getByRole("button", { name: "Close map inspector" }));
    await userEvent.click(within(inspector()).getByRole("button", { name: "Explain exact delta" }));
    await waitFor(() => expect(api.explainHistoricalReviewArchitecture).toHaveBeenCalledWith("review-1", 4));
    expect(await within(inspector()).findByText("The exact added reference broadens the dependency shape.")).toBeTruthy();
    expect(within(inspector()).getByText(/A reference edge does not prove runtime order/)).toBeTruthy();

    const restored = reviewMapLocationFromSelection(ARCHITECTURE_DELTA.graph.edges[0].selection);
    expect(restored.projection).toBe("architecture_delta");
    expect(restored.edge).toBe("architecture-edge:added");
  });

  it("inspects a changed file with why it matters, review progress, symbols, and relationships", async () => {
    const { onAskSelection, onOpenSelection } = renderSheet();
    await userEvent.click(await card("security.py"));
    const panel = inspector();
    expect(within(panel).getByRole("heading", { name: "security.py" })).toBeTruthy();
    expect(within(panel).getByText("Modified in this change")).toBeTruthy();
    expect(within(panel).getByText("public contract changed")).toBeTruthy();
    expect(within(panel).getByText("Touches a security-sensitive path")).toBeTruthy();
    expect(within(panel).getByText(/1 of 2 targets reviewed · 1 open thread/)).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Open symbol authorize" })).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Select routes.py" })).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Select cache.py" })).toBeTruthy();

    await userEvent.click(within(panel).getByRole("button", { name: "Open in diff" }));
    expect(onOpenSelection).toHaveBeenCalledWith(expect.objectContaining({ node_id: "file:security", target_ids: ["target-authorize"] }));
    await userEvent.click(within(panel).getByRole("button", { name: "Ask about this file" }));
    expect(onAskSelection).toHaveBeenCalledWith(expect.objectContaining({ kind: "review_map_node", node_id: "file:security" }), "auth/security.py");
  });

  it("opens files and symbols in Code at their location when a local workspace is bound", async () => {
    const onOpenInCode = vi.fn();
    renderSheet({ onOpenInCode });
    await userEvent.click(await card("security.py"));
    await userEvent.click(within(inspector()).getByRole("button", { name: "Open in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/auth/security.py", line: 0, endLine: 0 });

    await userEvent.dblClick(await card("security.py"));
    const canvas = await screen.findByRole("group", { name: "Review map canvas" });
    await userEvent.click(await within(canvas).findByRole("button", { name: "Open symbol authorize" }));
    expect(await screen.findByRole("button", { name: "authorize, focused symbol" })).toBeTruthy();
    await userEvent.click(within(inspector()).getByRole("button", { name: "Open in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/auth/security.py", line: 30, endLine: 55 });
  });

  it("marks current-index context as navigation only and offers no Snapshot source", async () => {
    renderSheet();
    await userEvent.click(await screen.findByRole("button", { name: "cache.py, surrounding code" }));
    const panel = inspector();
    expect(within(panel).getByText(/comes from the current code index, not the reviewed Snapshot/)).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: /Open/ })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "Map this file" })).toBeNull();
  });

  it("inspects a relationship with its evidence and asks about it", async () => {
    const { onAskSelection } = renderSheet();
    await userEvent.click(await card("security.py"));
    await userEvent.click(within(inspector()).getByRole("button", { name: "Inspect affects link with routes.py" }));
    const panel = inspector();
    expect(within(panel).getByRole("heading", { name: "Snapshot evidence links security.py with unchanged routes.py" })).toBeTruthy();
    expect(within(panel).getByText("Why connected")).toBeTruthy();
    expect(within(panel).getByText("src/api/routes.py:88")).toBeTruthy();
    expect(within(panel).getByText("authorize(request)")).toBeTruthy();
    await userEvent.click(within(panel).getByRole("button", { name: "Ask about this link" }));
    expect(onAskSelection).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "review_map_edge", edge_id: "edge:impact", relation: "impacts" }),
      "routes.py → security.py · impacts",
    );
  });

  it("selects a link from the canvas", async () => {
    renderSheet();
    await card("security.py");
    const hit = document.querySelector('[data-edge-id="edge:cache"]');
    expect(hit).toBeTruthy();
    fireEvent.click(hit!);
    expect(within(inspector()).getByRole("heading", { name: "security.py calls cache.py" })).toBeTruthy();
  });

  it("drills change → file → symbol and walks back with the breadcrumb", async () => {
    const { onOpenSelection } = renderSheet();
    await userEvent.dblClick(await card("security.py"));
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 4, "file", "file:security"));
    expect(await screen.findByRole("button", { name: "security.py, focused file" })).toBeTruthy();
    const location = screen.getByRole("navigation", { name: "Review map location" });
    expect(within(location).getByText("security.py")).toBeTruthy();

    const canvas = screen.getByRole("group", { name: "Review map canvas" });
    await userEvent.click(within(canvas).getByRole("button", { name: "Open symbol authorize" }));
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 4, "symbol", "symbol:authorize"));
    expect(await screen.findByRole("button", { name: "authorize, focused symbol" })).toBeTruthy();
    const panel = inspector();
    expect(within(panel).getByText("7 at capture")).toBeTruthy();
    expect(within(panel).getByText("#4 in the repository")).toBeTruthy();
    await userEvent.click(within(panel).getByRole("button", { name: "Open in diff" }));
    expect(onOpenSelection).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: "symbol:authorize", target_ids: ["target-authorize"] }));

    await userEvent.click(within(screen.getByRole("navigation", { name: "Review map location" })).getByRole("button", { name: "security.py" }));
    expect(await screen.findByRole("button", { name: "security.py, focused file" })).toBeTruthy();
    await userEvent.click(within(screen.getByRole("navigation", { name: "Review map location" })).getByRole("button", { name: "Change" }));
    expect(await card("security.py")).toBeTruthy();
    // The overview comes back as the reviewer left it.
    expect(within(inspector()).getByRole("heading", { name: "security.py" })).toBeTruthy();
  });

  it("keeps a drilled-into symbol's links lit after the symbol row it was opened from leaves the map", async () => {
    renderSheet();
    await userEvent.dblClick(await card("security.py"));
    await screen.findByRole("button", { name: "security.py, focused file" });
    const canvas = screen.getByRole("group", { name: "Review map canvas" });
    // Hovering the row lights its links; opening it unmounts the row under the
    // pointer, which never reports the pointer leaving.
    await userEvent.click(within(canvas).getByRole("button", { name: "Open symbol authorize" }));
    await screen.findByRole("button", { name: "authorize, focused symbol" });
    const edge = document.querySelector('[data-edge-id="edge:caller"]')?.parentElement?.querySelector(".rm-edge");
    expect(edge?.getAttribute("data-state")).toBe("idle");
  });

  it("falls back to a file focus derived from the overview on servers without the file level", async () => {
    api.fetchHistoricalReviewMap.mockImplementation(async (_id: string, _revision: number, level = "architecture") => {
      if (level === "file") throw new Error("level must be 'architecture' or 'symbol'");
      return ARCHITECTURE;
    });
    renderSheet();
    await userEvent.dblClick(await card("security.py"));
    expect(await screen.findByRole("button", { name: "security.py, focused file" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "cache.py, surrounding code" })).toBeTruthy();
  });

  it("reopens the drill-down an Ask reference points at", async () => {
    renderSheet({
      historical: true,
      location: reviewMapLocationFromSelection(selection("current:caller", "symbol", { focus_node_id: "symbol:authorize", exactness: "current_index" })),
    });
    expect(await screen.findByRole("button", { name: "authorize, focused symbol" })).toBeTruthy();
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 4);
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 4, "symbol", "symbol:authorize");
    expect(within(inspector()).getByRole("heading", { name: "handle_request" })).toBeTruthy();
  });

  it("jumps to a mapped diff inside the Review and keeps the new tab secondary", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const selectionHref = (item: { node_id?: string; target_ids?: string[] }) => item.target_ids?.length ? `/r/review-1?review-item=file&node=${item.node_id}` : null;
    const { onClose, onOpenSelection } = renderSheet({ selectionHref });
    await userEvent.click(await card("security.py"));

    const link = within(inspector()).getByRole("link", { name: "Open in diff in a new tab" });
    expect(link.getAttribute("href")).toBe("/r/review-1?review-item=file&node=file:security");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");

    // Keyboard and the primary button both stay in this Review (the Reader
    // records the Map on its backtrail); nothing opens a window.
    fireEvent.keyDown(screen.getByRole("group", { name: "Review map canvas" }), { key: "o" });
    expect(onOpenSelection).toHaveBeenCalledTimes(1);
    expect(onOpenSelection).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: "file:security" }));
    await userEvent.click(within(inspector()).getByRole("button", { name: "Open in diff" }));
    expect(onOpenSelection).toHaveBeenCalledTimes(2);
    expect(open).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it("shows Ask in the Map side panel and restores the selected inspector", async () => {
    const onClose = vi.fn();
    const onCloseAsk = vi.fn();
    const props = { reviewId: "review-1", revisionNumber: 4, historical: false, onClose, onCloseAsk, onOpenSelection: vi.fn() };
    const { rerender } = render(<ReviewMapSheet {...props} />);
    await userEvent.click(await card("security.py"));
    expect(within(inspector()).getByRole("heading", { name: "security.py" })).toBeTruthy();

    rerender(<ReviewMapSheet {...props} askPanel={<div role="region" aria-label="Map Ask">Ask conversation</div>} />);
    expect(screen.getByRole("region", { name: "Map Ask" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Review map inspector" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("region", { name: "Map Ask" }), { key: "Escape" });
    expect(onCloseAsk).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    rerender(<ReviewMapSheet {...props} />);
    expect(within(inspector()).getByRole("heading", { name: "security.py" })).toBeTruthy();
  });

  it("reports its selection and steps out with Escape: selection first, then the view", async () => {
    const onSelectionChange = vi.fn();
    const { onClose } = renderSheet({ onSelectionChange });
    await userEvent.click(await card("security.py"));
    expect(onSelectionChange).toHaveBeenLastCalledWith(expect.objectContaining({ path: "src/auth/security.py", exactness: "snapshot" }));

    const view = screen.getByRole("region", { name: "Review Map" });
    fireEvent.keyDown(view, { key: "Escape" });
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(view, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("walks through the change from the Map header, keeps the step in its location, and jumps to source", async () => {
    const onLocationChange = vi.fn();
    const { onClose, onOpenSelection } = renderSheet({ location: REVIEW_MAP_OVERVIEW, onLocationChange });
    await card("security.py");
    await userEvent.click(screen.getByRole("button", { name: "Walk through the change" }));
    const story = await screen.findByTestId("change-story");
    expect(within(story).getByTestId("change-story-step")).toHaveTextContent("security.py");
    await waitFor(() => expect(onLocationChange).toHaveBeenLastCalledWith(expect.objectContaining({ level: "architecture", story: "file:security" }), expect.any(String)));
    expect(onLocationChange).toHaveBeenCalledWith(expect.objectContaining({ story: expect.any(String) }), "push");
    // Only local Review reads back the overlays.
    expect(api.fetchHistoricalAnnotations).toHaveBeenCalledWith("review-1", 4);
    expect(api.explainHistoricalReviewArchitecture).not.toHaveBeenCalled();

    await userEvent.click(within(story).getByRole("button", { name: "Open source" }));
    expect(onOpenSelection).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: "file:security" }));

    fireEvent.keyDown(story, { key: "Escape" });
    expect(screen.queryByTestId("change-story")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(onLocationChange.mock.lastCall![0].story).toBeUndefined();
  });

  it("mirrors the drill-down into its location and walks it back from outside", async () => {
    const onLocationChange = vi.fn();
    const props = { reviewId: "review-1", revisionNumber: 4, historical: false, onClose: vi.fn(), onOpenSelection: vi.fn(), onLocationChange };
    const { rerender } = render(<ReviewMapSheet {...props} location={REVIEW_MAP_OVERVIEW} />);
    await userEvent.click(await card("security.py"));
    expect(onLocationChange).toHaveBeenLastCalledWith(expect.objectContaining({ level: "architecture", node: "file:security" }), "replace");
    const overviewLocation = onLocationChange.mock.lastCall![0];

    await userEvent.dblClick(await card("security.py"));
    expect(await screen.findByRole("button", { name: "security.py, focused file" })).toBeTruthy();
    expect(onLocationChange).toHaveBeenLastCalledWith(expect.objectContaining({ level: "file", focus: "file:security" }), "push");
    const fileLocation = onLocationChange.mock.lastCall![0];
    rerender(<ReviewMapSheet {...props} location={fileLocation} />);

    // Back: the overview returns from the drill-down stack without a refetch.
    const fetches = api.fetchHistoricalReviewMap.mock.calls.length;
    const reports = onLocationChange.mock.calls.length;
    rerender(<ReviewMapSheet {...props} location={overviewLocation} />);
    expect(await card("security.py")).toBeTruthy();
    expect(within(inspector()).getByRole("heading", { name: "security.py" })).toBeTruthy();
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledTimes(fetches);
    expect(onLocationChange).toHaveBeenCalledTimes(reports);

    // Forward: a step no longer on the stack loads again.
    rerender(<ReviewMapSheet {...props} location={fileLocation} />);
    expect(await screen.findByRole("button", { name: "security.py, focused file" })).toBeTruthy();
    expect(api.fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 4, "file", "file:security");
  });

  it("unfolds the chapter holding a selected file that its chapter folded away", async () => {
    const changed = (index: number) => {
      const path = `src/pkg/f${String(index).padStart(2, "0")}.py`;
      const id = `file:f${index}`;
      return { ...SECURITY, id, label: path, module: "src/pkg", path, rank: index + 1, symbols: [], symbol_count: 0, target_ids: [], selection: selection(id, "architecture", { path }) };
    };
    const nodes = Array.from({ length: 14 }, (_, index) => changed(index));
    const chaptered: ReviewMapPayload = { ...ARCHITECTURE, graph: { ...ARCHITECTURE.graph, focus: null, nodes, edges: [] } };
    api.fetchHistoricalReviewMap.mockImplementation(async () => chaptered);
    const chapters = [
      { label: "Most of it", paths: nodes.slice(0, 13).map((node) => node.path) },
      { label: "The rest", paths: [nodes[13].path] },
    ];
    // The last file of a thirteen-file chapter starts folded away; selecting it
    // has to unfold its chapter, not the folder the chapter replaced.
    renderSheet({ chapters, location: { ...REVIEW_MAP_OVERVIEW, node: "file:f12" } });
    expect(await card("f12.py")).toBeTruthy();
  });

  it("hides test files on request and finds files and symbols from search", async () => {
    renderSheet();
    expect(await card("test_security.py")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Tests" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^test_security\.py,/ })).toBeNull());

    await userEvent.type(screen.getByRole("combobox", { name: "Search review map" }), "test_sec");
    await userEvent.click(screen.getByRole("option", { name: "Select test_security.py on map" }));
    // Selecting a hidden file brings it back.
    expect(await card("test_security.py")).toBeTruthy();
    expect(within(inspector()).getByRole("heading", { name: "test_security.py" })).toBeTruthy();

    await userEvent.type(screen.getByRole("combobox", { name: "Search review map" }), "authorize{Enter}");
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 4, "symbol", "symbol:authorize"));
  });

  it("moves selection with the keyboard and clears it before closing on Escape", async () => {
    const { onClose, onOpenSelection } = renderSheet();
    const canvas = await screen.findByRole("group", { name: "Review map canvas" });
    await card("security.py");
    fireEvent.keyDown(canvas, { key: "ArrowDown" });
    expect(within(inspector()).getByRole("heading", { name: "security.py" })).toBeTruthy();
    fireEvent.keyDown(canvas, { key: "o" });
    expect(onOpenSelection).toHaveBeenCalledWith(expect.objectContaining({ node_id: "file:security" }));
    fireEvent.keyDown(canvas, { key: "ArrowDown" });
    expect(within(inspector()).getByRole("heading", { name: "test_security.py" })).toBeTruthy();
    fireEvent.keyDown(canvas, { key: "ArrowLeft" });
    expect(within(inspector()).getByRole("heading", { name: "routes.py" })).toBeTruthy();
    fireEvent.keyDown(canvas, { key: "Escape" });
    expect(within(inspector()).getByText("Start here")).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(canvas, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("explains an empty Snapshot instead of drawing nothing", async () => {
    api.fetchHistoricalReviewMap.mockResolvedValue({
      ...ARCHITECTURE,
      graph: { ...ARCHITECTURE.graph, nodes: [], edges: [], reason: "This Snapshot contains no changed files." },
    });
    renderSheet();
    expect(await screen.findByText("No map facts are available for this Snapshot")).toBeTruthy();
    expect(screen.getByText("This Snapshot contains no changed files.")).toBeTruthy();
  });
});
