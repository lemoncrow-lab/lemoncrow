import { lazy, Suspense, useMemo } from "react";
import { ChevronRight, Loader2, RotateCw, X } from "lucide-react";

import type {
  CodeFocusPayload,
  CodeMapActivityKind,
  CodeMapEdge,
  CodeMapNode,
  CodeModulesOverview,
} from "../api";
import { Button as UiButton } from "../components/ui/button";
import type { MapEdge, MapNode } from "../map/types";
import { communityColor, relationColor } from "../lib/graphColors";
import { moduleOfPath, type LiveMarks, type MapSpec } from "./codeModel";

const CodeGraph2D = lazy(() => import("./CodeGraph2D"));
const CodeGraph3D = lazy(() => import("./CodeGraph3D"));

export interface CodeTopologyViewProps {
  mode: "2d" | "3d";
  cacheKey: string;
  spec: MapSpec;
  overview: CodeModulesOverview | null;
  overviewLoading: boolean;
  overviewError: string | null;
  focus: CodeFocusPayload | null;
  focusLoading: boolean;
  focusError: string | null;
  selectedModuleId: string | null;
  selectedFocusId: string | null;
  live: ReadonlyMap<string, LiveMarks>;
  onRetry: () => void;
  onSpec: (spec: MapSpec) => void;
  onSelectModule: (id: string | null) => void;
  onSelectFocusNode: (node: MapNode | null) => void;
  onOpenFocusNode: (node: MapNode) => void;
  onInteract: () => void;
}

function Status({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-[420px] items-center justify-center gap-2 p-6 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}

function parentPath(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? "" : path.slice(0, slash);
}

function legacyNodeType(node: MapNode): CodeMapNode["node_type"] {
  if (node.kind === "module" || node.kind === "community") return "community";
  if (node.kind === "file" || node.symbols) return "file";
  return "symbol";
}

function graphCommunity(node: MapNode): string {
  if (node.status === "subject") return "focus:subject";
  if (node.lane === "used_by") return "focus:used_by";
  if (node.lane === "uses") return "focus:uses";
  const path = node.module || parentPath(node.path) || node.path || "root";
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[0] : "root";
}

function nodeGraphColor(node: MapNode, community: string): string {
  if (node.status === "subject") return "#f59e0b";
  if (node.lane === "used_by") return "#8b5cf6";
  if (node.lane === "uses") return "#06b6d4";
  if (node.status === "changed" || node.status === "new") return "#3b82f6";
  return communityColor(community);
}

function toLegacyGraph(nodes: readonly MapNode[], edges: readonly MapEdge[]) {
  const degree = new Map<string, number>();
  for (const node of nodes) degree.set(node.id, 0);
  for (const edge of edges) {
    const weight = Math.max(1, edge.weight || 1);
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + weight);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + weight);
  }

  const legacyNodes: CodeMapNode[] = nodes.map((node) => {
    const community = graphCommunity(node);
    return {
      id: node.id,
      label: node.label,
      qualified_name: node.qualified_name || node.label,
      path: node.path,
      kind: node.kind,
      line: node.line,
      end_line: node.end_line,
      degree: degree.get(node.id) ?? 0,
      focus: node.status === "subject",
      node_type: legacyNodeType(node),
      file_type: node.kind === "file" ? "source" : undefined,
      community,
      color: nodeGraphColor(node, community),
    };
  });

  const legacyEdges: CodeMapEdge[] = edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    kind: edge.kind,
    depth: 0,
    weight: edge.weight,
    color: relationColor(edge.kind),
  }));

  return { nodes: legacyNodes, edges: legacyEdges };
}

export default function CodeTopologyView({
  mode,
  cacheKey,
  spec,
  overview,
  overviewLoading,
  overviewError,
  focus,
  focusLoading,
  focusError,
  selectedModuleId,
  selectedFocusId,
  live,
  onRetry,
  onSpec,
  onSelectModule,
  onSelectFocusNode,
  onOpenFocusNode,
  onInteract,
}: CodeTopologyViewProps) {
  const rootGraph = useMemo(() => {
    if (!overview) return null;

    const nodes: MapNode[] = overview.modules.map((module) => ({
      id: module.id,
      label: module.label || module.path || "(root)",
      qualified_name: module.path,
      kind: "module",
      module: module.parent,
      lane: "",
      path: module.path || module.id,
      line: 1,
      end_line: 1,
      status: module.changed > 0 ? "changed" : "context",
      exactness: "current_index",
      symbol_count: module.symbols,
    }));
    const edges: MapEdge[] = overview.edges.map((edge, index) => ({
      id: `module:${edge.source}->${edge.target}:${index}`,
      source: edge.source,
      target: edge.target,
      kind: Object.entries(edge.kinds).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "dependency",
      exactness: "current_index",
      weight: edge.weight,
    }));
    return { graph: { nodes, edges }, legacy: toLegacyGraph(nodes, edges) };
  }, [overview]);

  const focusGraph = useMemo(() => {
    if (!focus) return null;
    const graph = focus.graph;
    return { graph, legacy: toLegacyGraph(graph.nodes, graph.edges) };
  }, [focus]);

  const current = spec ? focusGraph : rootGraph;
  const loading = spec ? focusLoading : overviewLoading;
  const error = spec ? focusError : overviewError;
  const selectedId = spec ? selectedFocusId : selectedModuleId;

  const activityByNode = useMemo(() => {
    const result: Record<string, CodeMapActivityKind> = {};
    if (!current) return result;
    const priority: Record<CodeMapActivityKind, number> = {
      search: 1,
      read: 2,
      verify: 3,
      edit: 4,
    };
    const setActivity = (id: string, kind: CodeMapActivityKind) => {
      const prior = result[id];
      if (!prior || priority[kind] >= priority[prior]) result[id] = kind;
    };

    if (!spec && overview) {
      const ids = new Set(current.legacy.nodes.map((node) => node.id));
      for (const [path, marks] of live) {
        const module = moduleOfPath(path, overview.modules);
        if (module && ids.has(module)) setActivity(module, marks.kind);
      }
      return result;
    }

    const idsByPath = new Map<string, string[]>();
    for (const node of current.graph.nodes) {
      if (!node.path) continue;
      const ids = idsByPath.get(node.path) ?? [];
      ids.push(node.id);
      idsByPath.set(node.path, ids);
    }
    for (const [path, marks] of live) {
      for (const id of idsByPath.get(path) ?? []) setActivity(id, marks.kind);
    }
    return result;
  }, [current, live, overview, spec]);

  if (error) {
    return (
      <Status>
        <span className="text-red-300/80">{error}</span>
        <UiButton type="button" size="xs" variant="outline" onClick={onRetry}>
          <RotateCw size={10} /> Retry
        </UiButton>
      </Status>
    );
  }
  if (loading && !current) {
    return <Status><Loader2 size={14} className="animate-spin" /> Building topology…</Status>;
  }
  if (!current || current.legacy.nodes.length === 0) {
    return <Status>No topology is available for this scope.</Status>;
  }

  const mapNodeById = new Map(current.graph.nodes.map((node) => [node.id, node]));
  const select = (nodeId: string | null) => {
    onInteract();
    if (spec) {
      onSelectFocusNode(nodeId ? mapNodeById.get(nodeId) ?? null : null);
      return;
    }
    onSelectModule(nodeId);
  };
  const expand = (nodeId: string) => {
    onInteract();
    const node = mapNodeById.get(nodeId);
    if (!node) return;
    if (spec) {
      onOpenFocusNode(node);
      return;
    }
    onSpec({ level: "module", id: node.id });
  };
  const clearScope = () => {
    onInteract();
    onSpec(null);
  };
  const scopedKey = `${cacheKey}:legacy-graph:${spec ? `${spec.level}:${spec.id}` : "modules"}:${mode}`;

  return (
    <div className="relative flex h-full min-h-[500px] flex-col overflow-hidden">
      <div className="flex min-h-9 shrink-0 items-center gap-2 border-b border-border bg-background/70 px-3 text-caption text-muted-foreground">
        <span className="font-medium text-foreground">{mode.toUpperCase()} topology</span>
        <span>{current.legacy.nodes.length.toLocaleString()} nodes</span>
        <span>·</span>
        <span>{current.legacy.edges.length.toLocaleString()} relationships</span>
        <div className="ml-auto flex min-w-0 items-center gap-1.5">
          {spec ? (
            <>
              <UiButton
                unstyled
                type="button"
                onClick={clearScope}
                className="shrink-0 rounded px-1.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                title="Show repository modules"
              >
                Repository
              </UiButton>
              <ChevronRight size={11} className="shrink-0 text-muted-foreground/60" />
              <div className="flex min-w-0 items-center gap-1 rounded-md border border-border bg-muted/45 pl-2 pr-1">
                <span className="max-w-[320px] truncate py-1 text-foreground" title={`${spec.level}: ${spec.id}`}>
                  {spec.level}: {spec.id}
                </span>
                <UiButton
                  unstyled
                  type="button"
                  onClick={clearScope}
                  className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted-foreground hover:bg-background hover:text-foreground"
                  aria-label="Clear topology scope"
                  title="Clear scope"
                >
                  <X size={11} />
                </UiButton>
              </div>
            </>
          ) : (
            <span className="rounded-md border border-border bg-muted/35 px-2 py-1 text-foreground">
              Repository modules
            </span>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <Suspense fallback={<Status><Loader2 size={14} className="animate-spin" /> Loading {mode.toUpperCase()} renderer…</Status>}>
          {mode === "2d" ? (
            <CodeGraph2D
              key={scopedKey}
              cacheKey={scopedKey}
              nodes={current.legacy.nodes}
              edges={current.legacy.edges}
              selectedId={selectedId}
              activityByNode={activityByNode}
              followNodeId={null}
              edgeVisibility="all"
              directedKinds={["calls", "imports", "references"]}
              presentation="default"
              onSelect={select}
              onExpand={expand}
            />
          ) : (
            <CodeGraph3D
              key={scopedKey}
              cacheKey={scopedKey}
              nodes={current.legacy.nodes}
              edges={current.legacy.edges}
              selectedId={selectedId}
              activityByNode={activityByNode}
              followNodeId={null}
              onSelect={select}
              onExpand={expand}
            />
          )}
        </Suspense>
      </div>
    </div>
  );
}
