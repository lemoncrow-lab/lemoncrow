import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewMapPayload } from "./types";

const api = vi.hoisted(() => ({
  fetchHistoricalReviewMap: vi.fn(),
}));
const canvas = vi.hoisted(() => ({ lastProps: null as any }));

vi.mock("./reviewApi", () => api);
vi.mock("../components/CodeGraph", () => ({
  default: (props: {
    nodes: { id: string; label: string }[];
    edges: { id: string; kind: string }[];
    onSelect(id: string | null): void;
    onSelectEdge?(id: string | null): void;
  }) => {
    canvas.lastProps = props;
    const { nodes, edges, onSelect, onSelectEdge } = props;
    return (
      <div data-testid="shared-code-graph">
        {nodes.map((node) => (
          <button key={node.id} type="button" onClick={() => onSelect(node.id)} aria-label={`Map node ${node.label}`}>
            {node.label}
          </button>
        ))}
        {edges.map((edge) => (
          <button key={edge.id} type="button" onClick={() => onSelectEdge?.(edge.id)} aria-label={`Map edge ${edge.kind}`}>
            {edge.kind}
          </button>
        ))}
      </div>
    );
  },
}));

import ReviewMapSheet from "./ReviewMapSheet";

const ARCHITECTURE: ReviewMapPayload = {
  review_id: "review-1",
  revision_id: "rrv-1",
  revision_number: 4,
  level: "architecture",
  snapshot: {
    exactness: "snapshot",
    tree_fingerprint: "tree",
    source_fingerprint: "source",
    base_sha: "a",
    head_sha: "b",
    degraded: [],
    provenance: { host: "codex", model: "gpt-5.6", certainty: "exact", method: "recorded_edit" },
    verification: { pass: 3, fail: 1, not_run: 0, unknown: 1, checks: [] },
  },
  context: {
    source: "current_index",
    exact_for_snapshot: false,
    snapshot_head_matches_checkout: false,
    index: { symbols_indexed: 100 },
    note: "Surrounding calls come from the current Code Map index.",
  },
  graph: {
    level: "architecture",
    focus: "file:security",
    truncated: false,
    omitted_node_count: 0,
    nodes: [
      {
        id: "file:security",
        label: "auth/security.py",
        kind: "file",
        component: "src",
        path: "src/auth/security.py",
        line: 0,
        end_line: 0,
        status: "changed",
        exactness: "snapshot",
        changed_file_count: 1,
        impacted_file_count: 0,
        relationship_count: 1,
        paths: ["src/auth/security.py"],
        target_ids: ["target-authorize"],
        current_symbol_ids: ["sym-current-authorize"],
        important_public_contract: true,
        boundaries: ["security"],
        boundary_basis: "path",
        change_kinds: ["modified"],
        symbols: [
          {
            id: "symbol:authorize",
            label: "authorize",
            qualified_name: "app.auth.authorize",
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
        selection: {
          kind: "review_map_node",
          review_id: "review-1",
          revision_id: "rrv-1",
          level: "architecture",
          node_id: "file:security",
          exactness: "snapshot",
          path: "src/auth/security.py",
          target_ids: ["target-authorize"],
          current_symbol_id: "sym-current-authorize",
        },
      },
      {
        id: "file:routes",
        label: "api/routes.py",
        kind: "file",
        component: "src",
        path: "src/api/routes.py",
        line: 0,
        end_line: 0,
        status: "impacted",
        exactness: "snapshot",
        changed_file_count: 0,
        impacted_file_count: 1,
        relationship_count: 1,
        paths: ["src/api/routes.py"],
        target_ids: [],
        important_public_contract: false,
        boundaries: [],
        boundary_basis: "",
        selection: {
          kind: "review_map_node",
          review_id: "review-1",
          revision_id: "rrv-1",
          level: "architecture",
          node_id: "file:routes",
          exactness: "snapshot",
          path: "src/api/routes.py",
          target_ids: [],
        },
      },
    ],
    edges: [
      {
        id: "edge:impact",
        source: "file:security",
        target: "file:routes",
        kind: "impacts",
        exactness: "snapshot",
        weight: 1,
        uncertain: false,
        evidence: [{ kind: "callsite", path: "src/api/routes.py", line: 88, uncertainty: "" }],
        selection: {
          kind: "review_map_edge",
          review_id: "review-1",
          revision_id: "rrv-1",
          level: "architecture",
          focus_node_id: "",
          edge_id: "edge:impact",
          source_id: "file:security",
          target_id: "file:routes",
          relation: "impacts",
          exactness: "snapshot",
        },
      },
    ],
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
        current_symbol_id: "sym-current-authorize",
        important_public_contract: true,
        boundaries: ["security"],
        boundary_basis: "path",
        caller_count: 7,
        usage_count: 9,
        centrality_rank: 4,
        centrality_percentile: 0.97,
        selection: {
          kind: "review_map_node",
          review_id: "review-1",
          revision_id: "rrv-1",
          level: "symbol",
          focus_node_id: "symbol:authorize",
          node_id: "symbol:authorize",
          exactness: "snapshot",
          path: "src/auth/security.py",
          line: 30,
          end_line: 55,
          symbol: "app.auth.authorize",
          target_ids: ["target-authorize"],
          current_symbol_id: "sym-current-authorize",
        },
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
        change: "unknown",
        exactness: "current_index",
        target_ids: [],
        current_symbol_id: "caller",
        important_public_contract: false,
        boundaries: [],
        boundary_basis: "",
        selection: {
          kind: "review_map_node",
          review_id: "review-1",
          revision_id: "rrv-1",
          level: "symbol",
          focus_node_id: "symbol:authorize",
          node_id: "current:caller",
          exactness: "current_index",
          path: "src/api/routes.py",
          line: 70,
          end_line: 105,
          target_ids: [],
          current_symbol_id: "caller",
        },
      },
    ],
    edges: [],
  },
};

describe("ReviewMapSheet", () => {
  beforeEach(() => {
    canvas.lastProps = null;
    api.fetchHistoricalReviewMap.mockReset().mockResolvedValue(ARCHITECTURE);
  });

  it("starts with bounded architecture context on the shared Code Map canvas pinned to the displayed Snapshot", async () => {
    render(<ReviewMapSheet reviewId="review-1" revisionNumber={4} historical={false} onClose={vi.fn()} onOpenSelection={vi.fn()} />);

    expect(await screen.findByRole("dialog", { name: "Review Map" })).toBeTruthy();
    expect(await screen.findByTestId("shared-code-graph")).toBeTruthy();
    expect(canvas.lastProps.edgeVisibility).toBe("all");
    expect(canvas.lastProps.directedKinds).toEqual(["calls", "impacts", "references"]);
    expect(canvas.lastProps.presentation).toBe("compact");
    expect(canvas.lastProps.forceLabels).toBe(true);
    expect(canvas.lastProps.dimUnrelatedSelection).toBe(false);
    expect(canvas.lastProps.followNodeId).toBeNull();
    expect(screen.getByText("Snapshot facts exact")).toBeTruthy();
    expect(screen.getByText("Surrounding graph · current index")).toBeTruthy();
    expect(screen.getByText(/Surrounding calls come from the current Code Map index/)).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Review map inspector" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Map node auth/security.py" }));
    expect(screen.getByText("Public contract")).toBeTruthy();
    expect(screen.getByText("security boundary signal · path")).toBeTruthy();
    expect(screen.getByText("Component")).toBeTruthy();
    expect(screen.getByText("src")).toBeTruthy();
    expect(screen.getByText("changed symbols")).toBeTruthy();
    expect(screen.getByText("relationships")).toBeTruthy();
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 4);
  });

  it("shows native graph readiness when surrounding context is incomplete", async () => {
    api.fetchHistoricalReviewMap.mockResolvedValueOnce({
      ...ARCHITECTURE,
      context: {
        ...ARCHITECTURE.context,
        readiness: {
          backend: "native",
          graph: { state: "building", reason: "link_rebuild_deferred" },
          semantic: { state: "degraded", reason: "semantic_content_incomplete" },
        },
        note: "Graph intelligence is building (link_rebuild_deferred).",
      },
    });
    render(<ReviewMapSheet reviewId="review-1" revisionNumber={4} historical={false} onClose={vi.fn()} onOpenSelection={vi.fn()} />);

    expect(await screen.findByText("Surrounding graph · current index · graph building")).toBeTruthy();
    expect(screen.getByText(/Graph intelligence is building/)).toBeTruthy();
  });

  it("drills from a frozen changed symbol into exact plus current-index context", async () => {
    api.fetchHistoricalReviewMap.mockResolvedValueOnce(ARCHITECTURE).mockResolvedValueOnce(SYMBOL);
    render(<ReviewMapSheet reviewId="review-1" revisionNumber={4} historical={false} onClose={vi.fn()} onOpenSelection={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Map node auth/security.py" }));
    await userEvent.click(screen.getByRole("button", { name: "Drill into authorize" }));
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 4, "symbol", "symbol:authorize"));
    expect(await screen.findByText("frozen callers", { exact: false })).toBeTruthy();
    expect(screen.getByText("frozen centrality", { exact: false })).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Map node handle_request" }));
    expect(screen.getByText("Current index context")).toBeTruthy();
    expect(screen.getByText(/not a claim about the reviewed Snapshot/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Open Snapshot source/ })).toBeNull();
  });

  it("asks from both a selected node and a selected relationship without creating another chat surface", async () => {
    const onAskSelection = vi.fn();
    render(
      <ReviewMapSheet
        reviewId="review-1"
        revisionNumber={4}
        historical={false}
        onClose={vi.fn()}
        onOpenSelection={vi.fn()}
        onAskSelection={onAskSelection}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Map node auth/security.py" }));
    await userEvent.click(screen.getByRole("button", { name: "Ask about this node" }));
    expect(onAskSelection).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "review_map_node", node_id: "file:security", revision_id: "rrv-1" }),
      "auth/security.py",
    );
    await userEvent.click(screen.getByRole("button", { name: "Map edge impacts" }));
    expect(canvas.lastProps.selectedEdgeId).toBe("edge:impact");
    expect(await screen.findByText("Why connected")).toBeTruthy();
    expect(screen.getByText("src/api/routes.py:88")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Ask about this relationship" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Ask about this relationship" }));
    expect(onAskSelection).toHaveBeenLastCalledWith(
      expect.objectContaining({ kind: "review_map_edge", edge_id: "edge:impact", relation: "impacts" }),
      expect.stringContaining("auth/security.py → api/routes.py · impacts"),
    );
    expect(onAskSelection).toHaveBeenCalledTimes(2);
  });

  it("opens source only for Snapshot-exact selections and uses historical map routes", async () => {
    const onOpenSelection = vi.fn();
    api.fetchHistoricalReviewMap.mockResolvedValueOnce(SYMBOL);
    render(<ReviewMapSheet reviewId="review-1" revisionNumber={4} historical onClose={vi.fn()} onOpenSelection={onOpenSelection} />);

    expect((await screen.findAllByText("authorize")).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /Open Snapshot source/ }));
    expect(onOpenSelection).toHaveBeenCalledWith(expect.objectContaining({
      revision_id: "rrv-1",
      node_id: "symbol:authorize",
      exactness: "snapshot",
      target_ids: ["target-authorize"],
    }));
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 4);
  });
});
