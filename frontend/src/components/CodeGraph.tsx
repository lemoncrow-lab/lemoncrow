import { useEffect, useMemo, useRef, useState } from "react";
import Graph from "graphology";
import forceAtlas2, { inferSettings } from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import Sigma from "sigma";
import { EdgeArrowProgram } from "sigma/rendering";
import { Focus, Minus, Plus } from "lucide-react";
import type { CodeMapActivityKind, CodeMapEdge, CodeMapNode } from "../api";
import {
  buildCodeMapHierarchy,
  buildCodeMapView,
  nextCodeMapDetailStep,
  type CodeMapDetail,
  type CodeMapExpansion,
} from "../lib/codeMapLod";

const KIND_COLORS: Record<string, [string, string]> = {
  class: ["#fbbf24", "#b45309"],
  method: ["#67e8f9", "#0e7490"],
  function: ["#67e8f9", "#0e7490"],
  async_function: ["#a5b4fc", "#4f46e5"],
  interface: ["#c4b5fd", "#7c3aed"],
  type: ["#c4b5fd", "#7c3aed"],
  module: ["#86efac", "#15803d"],
  reference: ["#a3a3a3", "#525252"],
};

const ACTIVITY_COLORS: Record<CodeMapActivityKind, [string, string]> = {
  search: ["#c4b5fd", "#7c3aed"],
  read: ["#67e8f9", "#0e7490"],
  edit: ["#fbbf24", "#b45309"],
  verify: ["#4ade80", "#15803d"],
};

function stableUnit(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

function nodeSize(node: CodeMapNode): number {
  const degree = Math.sqrt(node.degree ?? 0);
  if (node.node_type === "community") {
    return Math.min(
      6.5,
      2.4 + Math.sqrt(node.aggregate_count ?? 1) * 0.06 + degree * 0.1
    );
  }
  if (node.node_type === "file") return Math.min(2.3, 0.8 + degree * 0.12);
  return Math.min(2.8, 0.65 + degree * 0.16);
}

function initialPosition(
  node: CodeMapNode,
  index: number,
  count: number,
  communityIndex: number,
  localIndex: number
) {
  if (node.focus && count < 500) return { x: 0, y: 0 };
  const localAngle = stableUnit(node.id) * Math.PI * 2;
  if (count > 500) {
    const groupAngle = communityIndex * 2.399963229728653;
    const groupRadius =
      communityIndex === 0 ? 0 : 24 * Math.sqrt(communityIndex);
    const localRadius =
      0.5 + 0.2 * Math.sqrt(localIndex) + stableUnit(`${node.id}:${index}`);
    return {
      x:
        Math.cos(groupAngle) * groupRadius + Math.cos(localAngle) * localRadius,
      y:
        Math.sin(groupAngle) * groupRadius + Math.sin(localAngle) * localRadius,
    };
  }
  const ring = 1 + (index % Math.max(1, Math.ceil(Math.sqrt(count)))) * 0.12;
  return { x: Math.cos(localAngle) * ring, y: Math.sin(localAngle) * ring };
}

function sameSet(left: ReadonlySet<string>, right: ReadonlySet<string>) {
  if (left.size !== right.size) return false;
  for (const value of left) if (!right.has(value)) return false;
  return true;
}

function viewportExpansion(renderer: Sigma, graph: Graph): CodeMapExpansion {
  const { width, height } = renderer.getDimensions();
  const margin = 80;
  const communityIds = new Set<string>();
  const fileIds = new Set<string>();
  graph.forEachNode((nodeId, attributes) => {
    if (attributes.nodeType !== "community" && attributes.nodeType !== "file")
      return;
    const point = renderer.graphToViewport({
      x: Number(attributes.x),
      y: Number(attributes.y),
    });
    if (
      point.x < -margin ||
      point.x > width + margin ||
      point.y < -margin ||
      point.y > height + margin
    )
      return;
    if (attributes.nodeType === "community") communityIds.add(nodeId);
    else {
      fileIds.add(nodeId);
      communityIds.add(`community::${attributes.community || "root"}`);
    }
  });
  return { communityIds, fileIds };
}

export default function CodeGraph({
  nodes,
  edges,
  selectedId,
  activityByNode,
  followNodeId,
  onSelect,
  onExpand,
}: {
  nodes: CodeMapNode[];
  edges: CodeMapEdge[];
  selectedId: string | null;
  activityByNode: Record<string, CodeMapActivityKind>;
  followNodeId: string | null;
  onSelect: (nodeId: string) => void;
  onExpand: (nodeId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef(new Graph({ type: "directed", multi: false }));
  const rendererRef = useRef<Sigma | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const layoutTimerRef = useRef<number | null>(null);
  const rebuildFrameRef = useRef<number | null>(null);
  const revealFrameRef = useRef<number | null>(null);
  const scopeTimerRef = useRef<number | null>(null);
  const scopeUpdateRef = useRef<() => void>(() => undefined);
  const rebuildingRef = useRef(false);
  const positionCacheRef = useRef(new Map<string, { x: number; y: number }>());
  const detailStepRef = useRef(0);
  const showAllRef = useRef(false);
  const detailRef = useRef<CodeMapDetail>("overview");
  const selectedRef = useRef<string | null>(selectedId);
  const hoveredRef = useRef<string | null>(null);
  const activityRef = useRef(activityByNode);
  const pulseRef = useRef(false);
  const onSelectRef = useRef(onSelect);
  const onExpandRef = useRef(onExpand);
  onSelectRef.current = onSelect;
  onExpandRef.current = onExpand;
  const lightThemeRef = useRef(
    document.documentElement.classList.contains("light")
  );
  const [renderingUnavailable, setRenderingUnavailable] = useState(false);
  const [detailStep, setDetailStep] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [expansion, setExpansion] = useState<CodeMapExpansion>({
    communityIds: new Set(),
    fileIds: new Set(),
  });
  showAllRef.current = showAll;

  const hierarchy = useMemo(
    () => buildCodeMapHierarchy(nodes, edges),
    [edges, nodes]
  );
  const forcedNodeIds = useMemo(
    () => [
      ...(followNodeId ? [followNodeId] : []),
      ...Object.keys(activityByNode),
    ],
    [activityByNode, followNodeId]
  );
  const view = useMemo(
    () =>
      buildCodeMapView(
        hierarchy,
        detailStep,
        expansion,
        forcedNodeIds,
        selectedId,
        showAll
      ),
    [detailStep, expansion, forcedNodeIds, hierarchy, selectedId, showAll]
  );
  detailRef.current = view.detail;
  const displayNodes = view.nodes;
  const displayEdges = view.edges;
  const sourceNodeById = hierarchy.nodeById;
  const fileIdByPath = useMemo(
    () => new Map(hierarchy.fileNodes.map((node) => [node.path, node.id])),
    [hierarchy.fileNodes]
  );
  const nodeIds = useMemo(
    () => new Set(displayNodes.map((node) => node.id)),
    [displayNodes]
  );
  const displaySelectedId = useMemo(() => {
    if (!selectedId) return null;
    if (nodeIds.has(selectedId)) return selectedId;
    const source = sourceNodeById.get(selectedId);
    if (!source) return null;
    const fileId = fileIdByPath.get(source.path);
    if (fileId && nodeIds.has(fileId)) return fileId;
    return `community::${source.community || "root"}`;
  }, [fileIdByPath, nodeIds, selectedId, sourceNodeById]);
  const displayActivity = useMemo(() => {
    const projected: Record<string, CodeMapActivityKind> = {};
    for (const [nodeId, kind] of Object.entries(activityByNode)) {
      if (nodeIds.has(nodeId)) {
        projected[nodeId] = kind;
        continue;
      }
      const source = sourceNodeById.get(nodeId);
      if (!source) continue;
      const fileId = fileIdByPath.get(source.path);
      const target =
        fileId && nodeIds.has(fileId)
          ? fileId
          : `community::${source.community || "root"}`;
      if (nodeIds.has(target)) projected[target] = kind;
    }
    return projected;
  }, [activityByNode, fileIdByPath, nodeIds, sourceNodeById]);

  useEffect(() => {
    if (!containerRef.current) return;
    const graph = graphRef.current;
    let renderer: Sigma;
    try {
      renderer = new Sigma(graph, containerRef.current, {
        allowInvalidContainer: true,
        defaultNodeColor: "#67e8f9",
        defaultEdgeColor: "#525252",
        edgeProgramClasses: { arrow: EdgeArrowProgram },
        labelColor: {
          color: lightThemeRef.current ? "#262626" : "#e5e5e5",
        },
        labelDensity: 0.8,
        labelGridCellSize: 90,
        labelRenderedSizeThreshold: 7,
        minCameraRatio: 0.08,
        maxCameraRatio: 5,
        renderEdgeLabels: false,
        zIndex: true,
        nodeReducer: (nodeId, data) => {
          const selected = selectedRef.current;
          const hovered = hoveredRef.current;
          const activeKind = activityRef.current[nodeId];
          const related =
            !selected ||
            nodeId === selected ||
            (graph.hasNode(selected) && graph.areNeighbors(nodeId, selected));
          return {
            ...data,
            color: activeKind
              ? ACTIVITY_COLORS[activeKind][lightThemeRef.current ? 1 : 0]
              : nodeId === selected
                ? lightThemeRef.current
                  ? "#7c3aed"
                  : "#ddd6fe"
                : related
                  ? String(
                      data.baseColor ||
                        (KIND_COLORS[String(data.kind)] ?? ["#a3a3a3"])[0]
                    )
                  : lightThemeRef.current
                    ? "#a3a3a3"
                    : "#525252",
            size: activeKind
              ? Number(data.baseSize) * (pulseRef.current ? 2 : 1.45)
              : nodeId === hovered
                ? Number(data.baseSize) * 1.3
                : Number(data.baseSize),
            highlighted:
              nodeId === selected || nodeId === hovered || Boolean(activeKind),
            forceLabel:
              nodeId === selected || nodeId === hovered || Boolean(activeKind),
            zIndex:
              nodeId === selected || nodeId === hovered || activeKind
                ? 3
                : related
                  ? 2
                  : 1,
          };
        },
        edgeReducer: (edgeId, data) => {
          const selected = selectedRef.current;
          const focus = selected || hoveredRef.current;
          const extremities = graph.extremities(edgeId);
          const related = !focus || extremities.includes(focus);
          const overviewEdge =
            detailRef.current === "overview" &&
            edgeId.startsWith("community-call::");
          const activeKind =
            activityRef.current[extremities[0]] ||
            activityRef.current[extremities[1]];
          const aggregate = data.kind === "aggregate";
          return {
            ...data,
            color: activeKind
              ? ACTIVITY_COLORS[activeKind][lightThemeRef.current ? 1 : 0]
              : focus && related
                ? lightThemeRef.current
                  ? "#7c3aed"
                  : "#c4b5fd"
                : aggregate
                  ? lightThemeRef.current
                    ? "#a3a3a3"
                    : "#737373"
                  : data.kind === "contains"
                    ? lightThemeRef.current
                      ? "#e5e5e5"
                      : "#303030"
                    : lightThemeRef.current
                      ? "#d4d4d4"
                      : "#525252",
            size: activeKind
              ? pulseRef.current
                ? 2.4
                : 1.7
              : focus && related
                ? 1.1
                : aggregate
                  ? Number(data.baseSize ?? 0.4)
                  : data.kind === "contains"
                    ? 0.08
                    : 0.18,
            hidden: !activeKind && (focus ? !related : !overviewEdge),
          };
        },
      });
    } catch {
      setRenderingUnavailable(true);
      return;
    }
    rendererRef.current = renderer;

    const themeObserver = new MutationObserver(() => {
      lightThemeRef.current =
        document.documentElement.classList.contains("light");
      renderer.setSetting("labelColor", {
        color: lightThemeRef.current ? "#262626" : "#e5e5e5",
      });
      renderer.refresh();
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    const openNode = (node: string, expand: boolean) => {
      if (graph.getNodeAttribute(node, "nodeType") === "community") {
        const position = renderer.getNodeDisplayData(node);
        const nextStep = Math.max(detailStepRef.current, 2);
        setExpansion((current) => {
          const communityIds = new Set(current.communityIds);
          communityIds.add(node);
          return { communityIds, fileIds: current.fileIds };
        });
        detailStepRef.current = nextStep;
        setDetailStep(nextStep);
        if (position) {
          renderer
            .getCamera()
            .animate(
              { x: position.x, y: position.y, ratio: 0.5 },
              { duration: 420 }
            );
        }
        return;
      }
      if (expand) onExpandRef.current(node);
      else onSelectRef.current(node);
    };
    renderer.on("clickNode", ({ node }) => openNode(node, false));
    renderer.on("doubleClickNode", ({ node, preventSigmaDefault }) => {
      preventSigmaDefault();
      openNode(node, true);
    });
    const updateScope = () => {
      if (scopeTimerRef.current) window.clearTimeout(scopeTimerRef.current);
      scopeTimerRef.current = window.setTimeout(() => {
        scopeTimerRef.current = null;
        if (showAllRef.current) return;
        const next = viewportExpansion(renderer, graph);
        setExpansion((current) =>
          sameSet(current.communityIds, next.communityIds) &&
          sameSet(current.fileIds, next.fileIds)
            ? current
            : next
        );
      }, 120);
    };
    scopeUpdateRef.current = updateScope;
    const camera = renderer.getCamera();
    const handleCameraUpdate = ({ ratio }: { ratio: number }) => {
      if (rebuildingRef.current || showAllRef.current) return;
      const next = nextCodeMapDetailStep(detailStepRef.current, ratio);
      if (next !== detailStepRef.current) {
        detailStepRef.current = next;
        setDetailStep(next);
      }
      updateScope();
    };
    camera.on("updated", handleCameraUpdate);
    renderer.on("enterNode", ({ node }) => {
      hoveredRef.current = node;
      containerRef.current?.classList.add("cursor-pointer");
      renderer.refresh();
    });
    renderer.on("leaveNode", () => {
      hoveredRef.current = null;
      containerRef.current?.classList.remove("cursor-pointer");
      renderer.refresh();
    });

    return () => {
      if (layoutTimerRef.current) window.clearTimeout(layoutTimerRef.current);
      if (rebuildFrameRef.current)
        window.cancelAnimationFrame(rebuildFrameRef.current);
      if (revealFrameRef.current)
        window.cancelAnimationFrame(revealFrameRef.current);
      if (scopeTimerRef.current) window.clearTimeout(scopeTimerRef.current);
      scopeUpdateRef.current = () => undefined;
      layoutRef.current?.kill();
      camera.off("updated", handleCameraUpdate);
      themeObserver.disconnect();
      renderer.kill();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    const graph = graphRef.current;
    rebuildingRef.current = true;
    if (rebuildFrameRef.current)
      window.cancelAnimationFrame(rebuildFrameRef.current);
    graph.forEachNode((id, attributes) => {
      positionCacheRef.current.set(id, { x: attributes.x, y: attributes.y });
    });
    layoutRef.current?.kill();
    layoutRef.current = null;
    if (layoutTimerRef.current) window.clearTimeout(layoutTimerRef.current);
    if (revealFrameRef.current)
      window.cancelAnimationFrame(revealFrameRef.current);
    const nextNodeIds = new Set(displayNodes.map((node) => node.id));
    const removalCount = graph
      .nodes()
      .reduce((count, nodeId) => count + (nextNodeIds.has(nodeId) ? 0 : 1), 0);
    if (removalCount > 5_000) {
      graph.clear();
    } else {
      graph.clearEdges();
      for (const nodeId of graph.nodes()) {
        if (!nextNodeIds.has(nodeId)) graph.dropNode(nodeId);
      }
    }
    if (hoveredRef.current && !nextNodeIds.has(hoveredRef.current))
      hoveredRef.current = null;

    const communityOrder = [
      ...new Set(displayNodes.map((node) => node.community || "root")),
    ].sort();
    const communityIndexes = new Map(
      communityOrder.map((community, index) => [community, index])
    );
    const localIndexes = new Map<string, number>();
    const revealTargets = new Map<string, number>();
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    const animateReveal = !reducedMotion && displayNodes.length <= 5_000;

    displayNodes.forEach((node, index) => {
      const community = node.community || "root";
      const parentId =
        node.node_type === "symbol"
          ? fileIdByPath.get(node.path)
          : node.node_type === "file"
            ? `community::${community}`
            : undefined;
      const clusterKey = parentId || community;
      const localIndex = localIndexes.get(clusterKey) ?? 0;
      localIndexes.set(clusterKey, localIndex + 1);
      const parentPosition =
        parentId && graph.hasNode(parentId)
          ? {
              x: Number(graph.getNodeAttribute(parentId, "x")),
              y: Number(graph.getNodeAttribute(parentId, "y")),
            }
          : parentId
            ? positionCacheRef.current.get(parentId)
            : undefined;
      const angle = stableUnit(node.id) * Math.PI * 2;
      const spread =
        node.node_type === "symbol"
          ? 0.08 + Math.sqrt(localIndex) * 0.025
          : 0.35 + Math.sqrt(localIndex) * 0.11;
      const position =
        positionCacheRef.current.get(node.id) ??
        (parentPosition
          ? {
              x: parentPosition.x + Math.cos(angle) * spread,
              y: parentPosition.y + Math.sin(angle) * spread,
            }
          : initialPosition(
              node,
              index,
              displayNodes.length,
              communityIndexes.get(community) ?? 0,
              localIndex
            ));
      const size = nodeSize(node);
      const color =
        node.color ||
        (node.focus ? "#c4b5fd" : (KIND_COLORS[node.kind] ?? ["#a3a3a3"])[0]);
      const attributes = {
        ...position,
        label: node.label,
        color,
        baseColor: color,
        size,
        baseSize: size,
        kind: node.kind,
        nodeType: node.node_type,
        community: node.community,
        path: node.path,
        forceLabel:
          (node.node_type === "community" &&
            (node.aggregate_count ?? 0) >= 10) ||
          Boolean(node.focus),
        zIndex: node.focus ? 2 : 1,
      };
      if (graph.hasNode(node.id)) {
        graph.mergeNodeAttributes(node.id, attributes);
      } else {
        const startSize = animateReveal ? Math.max(0.15, size * 0.16) : size;
        graph.addNode(node.id, {
          ...attributes,
          size: startSize,
          baseSize: startSize,
        });
        if (animateReveal) revealTargets.set(node.id, size);
      }
    });
    displayEdges.forEach((edge) => {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) return;
      const key = graph.hasEdge(edge.source, edge.target)
        ? `${edge.id}:${edge.depth}`
        : edge.id;
      if (!graph.hasEdge(key)) {
        graph.addDirectedEdgeWithKey(key, edge.source, edge.target, {
          type:
            (view.detail === "symbols" || view.detail === "full") &&
            edge.kind === "calls"
              ? "arrow"
              : "line",
          kind: edge.kind,
          color: edge.kind === "contains" ? "#404040" : "#737373",
          size:
            edge.kind === "contains"
              ? 0.08
              : edge.kind === "aggregate"
                ? Math.min(0.65, 0.28 + Math.log2(edge.weight ?? 1) * 0.08)
                : 0.18,
          baseSize:
            edge.kind === "aggregate"
              ? Math.min(0.65, 0.28 + Math.log2(edge.weight ?? 1) * 0.08)
              : edge.kind === "contains"
                ? 0.08
                : 0.18,
        });
      }
    });

    rendererRef.current?.setSetting("labelRenderedSizeThreshold", 4.5);
    if (graph.order > 1 && graph.order <= 1_200) {
      if (reducedMotion) {
        if (graph.order <= 500) {
          forceAtlas2.assign(graph, {
            iterations: 24,
            settings: { ...inferSettings(graph), gravity: 1.4, slowDown: 8 },
          });
        }
      } else {
        const layout = new FA2Layout(graph, {
          settings: {
            ...inferSettings(graph),
            gravity: graph.order > 500 ? 0.5 : 1.4,
            scalingRatio: graph.order > 500 ? 7 : 2,
            linLogMode: graph.order > 500,
            slowDown: graph.order > 500 ? 12 : 8,
            barnesHutOptimize: graph.order > 80,
          },
        });
        layoutRef.current = layout;
        layout.start();
        layoutTimerRef.current = window.setTimeout(
          () => layout.stop(),
          detailStep === 0 ? 450 : 320
        );
      }
    }
    rendererRef.current?.refresh();
    if (revealTargets.size) {
      const startedAt = performance.now();
      const reveal = (now: number) => {
        const progress = Math.min(1, (now - startedAt) / 180);
        const eased = 1 - Math.pow(1 - progress, 3);
        for (const [nodeId, target] of revealTargets) {
          if (!graph.hasNode(nodeId)) continue;
          const size = Math.max(0.15, target * (0.16 + eased * 0.84));
          graph.setNodeAttribute(nodeId, "size", size);
          graph.setNodeAttribute(nodeId, "baseSize", size);
        }
        rendererRef.current?.refresh();
        if (progress < 1) {
          revealFrameRef.current = window.requestAnimationFrame(reveal);
        } else {
          revealFrameRef.current = null;
        }
      };
      revealFrameRef.current = window.requestAnimationFrame(reveal);
    }
    rebuildFrameRef.current = window.requestAnimationFrame(() => {
      rebuildingRef.current = false;
      rebuildFrameRef.current = null;
      scopeUpdateRef.current();
    });
  }, [detailStep, displayEdges, displayNodes, fileIdByPath, view.detail]);

  useEffect(() => {
    selectedRef.current = displaySelectedId;
    activityRef.current = displayActivity;
    rendererRef.current?.refresh();
  }, [displayActivity, displaySelectedId]);

  useEffect(() => {
    if (!Object.keys(displayActivity).length) return;
    const timer = window.setInterval(() => {
      pulseRef.current = !pulseRef.current;
      rendererRef.current?.refresh();
    }, 620);
    return () => window.clearInterval(timer);
  }, [displayActivity]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const source = followNodeId ? sourceNodeById.get(followNodeId) : undefined;
    if (!renderer || !followNodeId || !source) return;
    const requiredStep = source.node_type === "symbol" ? 6 : 3;
    if (!graphRef.current.hasNode(followNodeId)) {
      const nextStep = Math.max(detailStepRef.current, requiredStep);
      detailStepRef.current = nextStep;
      setDetailStep(nextStep);
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      const position = renderer.getNodeDisplayData(followNodeId);
      if (!position) return;
      const state = renderer.getCamera().getState();
      renderer.getCamera().animate(
        {
          x: position.x,
          y: position.y,
          ratio: Math.min(
            state.ratio,
            source.node_type === "symbol" ? 0.17 : 0.42
          ),
        },
        { duration: 520 }
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [displayNodes, followNodeId, sourceNodeById]);

  const camera = () => rendererRef.current?.getCamera();

  if (renderingUnavailable) {
    return (
      <div className="h-full min-h-[420px] overflow-y-auto bg-surface-sunken p-5">
        <div className="border border-amber-700/50 bg-amber-950/20 p-4 text-sm text-neutral-200">
          WebGL is unavailable, so the graph is shown as a navigable list capped
          at 500 items.
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {nodes.slice(0, 500).map((node) => (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelect(node.id)}
              onDoubleClick={() => onExpand(node.id)}
              className="border border-neutral-700 bg-neutral-900/70 p-3 text-left transition hover:border-brand-500"
            >
              <span className="block truncate text-sm text-neutral-100">
                {node.label}
              </span>
              <span className="mt-1 block truncate text-[11px] text-neutral-400">
                {node.path}:{node.line}
              </span>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full min-h-[420px] overflow-hidden bg-surface-sunken">
      <div
        ref={containerRef}
        className="absolute inset-0"
        role="img"
        aria-label={`Interactive source map showing ${displayNodes.length} visible nodes and ${displayEdges.length} relationships; ${view.hiddenCount} nodes remain grouped. Zoom in to unfold them progressively.`}
      />
      <div className="absolute bottom-4 left-4 flex border border-neutral-700 bg-neutral-950/90 shadow-lg">
        <button
          type="button"
          className="border-r border-neutral-700 p-2 text-neutral-300 transition hover:bg-neutral-800 hover:text-white"
          onClick={() => void camera()?.animatedZoom({ duration: 180 })}
          aria-label="Zoom in"
        >
          <Plus size={15} />
        </button>
        <button
          type="button"
          className="border-r border-neutral-700 p-2 text-neutral-300 transition hover:bg-neutral-800 hover:text-white"
          onClick={() => void camera()?.animatedUnzoom({ duration: 180 })}
          aria-label="Zoom out"
        >
          <Minus size={15} />
        </button>
        <button
          type="button"
          className="border-r border-neutral-700 p-2 text-neutral-300 transition hover:bg-neutral-800 hover:text-white"
          onClick={() => {
            showAllRef.current = false;
            setShowAll(false);
            detailStepRef.current = 0;
            setDetailStep(0);
            void camera()?.animatedReset({ duration: 260 });
          }}
          aria-label="Fit graph"
        >
          <Focus size={15} />
        </button>
        <button
          type="button"
          aria-pressed={showAll}
          onClick={() => {
            const next = !showAllRef.current;
            showAllRef.current = next;
            setShowAll(next);
            if (!next) scopeUpdateRef.current();
          }}
          className={
            showAll
              ? "bg-brand-500/20 px-3 text-[10px] font-semibold uppercase tracking-wider text-brand-200"
              : "px-3 text-[10px] font-semibold uppercase tracking-wider text-neutral-300 transition hover:bg-neutral-800 hover:text-white"
          }
        >
          {showAll ? "Grouped" : "All points"}
        </button>
      </div>
      <div className="absolute bottom-4 right-4 border border-neutral-800 bg-neutral-950/85 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300">
        {nodeIds.size.toLocaleString()} visible ·{" "}
        {showAll
          ? "all points"
          : view.detail === "overview"
            ? "grouped overview"
            : view.detail === "files"
              ? "local files"
              : "local symbols"}
        {view.hiddenCount > 0
          ? ` · ${view.hiddenCount.toLocaleString()} grouped`
          : ""}
      </div>
    </div>
  );
}
