import { useEffect, useRef, useState } from "react";
import Graph from "graphology";
import { inferSettings } from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import Sigma from "sigma";
import { EdgeArrowProgram } from "sigma/rendering";
import { Focus, Loader2, Minus, Plus } from "lucide-react";
import type { CodeMapActivityKind, CodeMapEdge, CodeMapNode } from "../api";
import {
  ACTIVITY_COLORS,
  KIND_COLORS,
  stableUnit,
  vivid,
} from "../lib/graphColors";

// Theme-aware hover label. Sigma's default hover badge is hard-coded white, so
// in dark mode the light label text rendered white-on-white. Draw our own
// readable badge with theme-matched background and text.
function drawHoverBadge(
  context: CanvasRenderingContext2D,
  data: { x: number; y: number; size: number; label?: string | null },
  settings: { labelSize: number; labelFont: string; labelWeight: string }
): void {
  const label = typeof data.label === "string" ? data.label : "";
  if (!label) return;
  const light = document.documentElement.classList.contains("light");
  const size = settings.labelSize;
  context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
  const padX = 7;
  const padY = 5;
  const width = context.measureText(label).width + padX * 2;
  const height = size + padY * 2;
  const x = data.x + data.size + 3;
  const y = data.y - height / 2;
  context.fillStyle = light ? "#ffffff" : "#171717";
  context.strokeStyle = light ? "#e5e5e5" : "#3f3f46";
  context.lineWidth = 1;
  context.beginPath();
  if (context.roundRect) context.roundRect(x, y, width, height, 4);
  else context.rect(x, y, width, height);
  context.fill();
  context.stroke();
  context.fillStyle = light ? "#171717" : "#f5f5f5";
  context.textAlign = "left";
  context.textBaseline = "middle";
  context.fillText(label, x + padX, data.y);
}

function nodeSize(node: CodeMapNode): number {
  const degree = Math.sqrt(node.degree ?? 0);
  if (node.node_type === "file") return Math.min(3.4, 1.1 + degree * 0.16);
  return Math.min(3, 0.7 + degree * 0.2);
}

// Deterministic cluster seed: groups are dropped onto a golden-angle
// spiral, members scattered around their community centroid. ForceAtlas2 then
// refines this ONCE; the seed only makes that single settle converge fast and
// look the same on every load.
function seedPositions(
  nodes: CodeMapNode[]
): Map<string, { x: number; y: number }> {
  const groups = new Map<string, CodeMapNode[]>();
  for (const node of nodes) {
    const key = node.community || "root";
    const bucket = groups.get(key);
    if (bucket) bucket.push(node);
    else groups.set(key, [node]);
  }
  const names = [...groups.keys()].sort();
  const positions = new Map<string, { x: number; y: number }>();
  names.forEach((name, communityIndex) => {
    const members = groups.get(name) ?? [];
    // Spread community centroids far apart on a golden-angle spiral, and give
    // each cluster a footprint proportional to how many nodes it holds so big
    // packages get their own breathing room instead of one shared blob.
    const groupAngle = communityIndex * 2.399963229728653;
    const groupRadius =
      communityIndex === 0 ? 0 : 24 * Math.sqrt(communityIndex);
    const cx = Math.cos(groupAngle) * groupRadius;
    const cy = Math.sin(groupAngle) * groupRadius;
    const spread = 2 + Math.sqrt(members.length) * 0.45;
    const count = Math.max(1, members.length);
    members.forEach((node, localIndex) => {
      const angle = stableUnit(node.id) * Math.PI * 2;
      const radius = spread * (0.3 + 0.7 * Math.sqrt(localIndex / count));
      positions.set(node.id, {
        x: cx + Math.cos(angle) * radius,
        y: cy + Math.sin(angle) * radius,
      });
    });
  });
  return positions;
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
  onSelect: (nodeId: string | null) => void;
  onExpand: (nodeId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef(new Graph({ type: "directed", multi: false }));
  const rendererRef = useRef<Sigma | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const settleTimerRef = useRef<number | null>(null);
  const followFrameRef = useRef<number | null>(null);
  // Settled positions survive rebuilds: switching Full <-> Focus and back, or
  // toggling a facet filter off and on, reuses the frozen layout instantly
  // instead of re-running physics (which is what looked like "dancing").
  const positionCacheRef = useRef(new Map<string, { x: number; y: number }>());
  const selectedRef = useRef<string | null>(selectedId);
  const hoveredRef = useRef<string | null>(null);
  const neighborsRef = useRef<Set<string>>(new Set());
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
  const [computing, setComputing] = useState(false);

  // ---- create the renderer once -------------------------------------- //
  useEffect(() => {
    if (!containerRef.current) return;
    const graph = graphRef.current;
    let renderer: Sigma;
    try {
      renderer = new Sigma(graph, containerRef.current, {
        allowInvalidContainer: true,
        defaultNodeColor: "#67e8f9",
        defaultEdgeColor: "#3f3f46",
        edgeProgramClasses: { arrow: EdgeArrowProgram },
        labelColor: { color: lightThemeRef.current ? "#262626" : "#e5e5e5" },
        labelDensity: 0.7,
        labelGridCellSize: 120,
        labelRenderedSizeThreshold: 7,
        minCameraRatio: 0.02,
        maxCameraRatio: 8,
        renderEdgeLabels: false,
        zIndex: true,
        defaultDrawNodeHover: drawHoverBadge,
        nodeReducer: (nodeId, data) => {
          const selected = selectedRef.current;
          const hovered = hoveredRef.current;
          const activeKind = activityRef.current[nodeId];
          const dimmed =
            Boolean(selected) &&
            !activeKind &&
            !neighborsRef.current.has(nodeId);
          const color = activeKind
            ? ACTIVITY_COLORS[activeKind][lightThemeRef.current ? 1 : 0]
            : nodeId === selected
              ? lightThemeRef.current
                ? "#7c3aed"
                : "#ddd6fe"
              : dimmed
                ? lightThemeRef.current
                  ? "#d4d4d4"
                  : "#303030"
                : String(data.baseColor ?? "#a3a3a3");
          const base = Number(data.baseSize ?? 1);
          const highlighted =
            nodeId === selected || nodeId === hovered || Boolean(activeKind);
          return {
            ...data,
            color,
            size: activeKind
              ? base * (pulseRef.current ? 1.9 : 1.5)
              : nodeId === hovered
                ? base * 1.4
                : base,
            highlighted,
            forceLabel: highlighted,
            zIndex: highlighted ? 3 : dimmed ? 0 : 1,
          };
        },
        edgeReducer: (edgeId, data) => {
          const focus = selectedRef.current || hoveredRef.current;
          const [source, target] = graph.extremities(edgeId);
          const activeKind =
            activityRef.current[source] || activityRef.current[target];
          if (activeKind) {
            return {
              ...data,
              hidden: false,
              color: ACTIVITY_COLORS[activeKind][lightThemeRef.current ? 1 : 0],
              size: pulseRef.current ? 2.2 : 1.6,
              zIndex: 3,
            };
          }
          if (focus) {
            // Callers (target === focus) AND callees (source === focus) both
            // light up; every other call edge stays faint grey for context.
            const related = source === focus || target === focus;
            if (related) {
              if (data.kind === "calls") {
                // Direction-coded to match the detail panel legend:
                // callee (focus -> target) = cyan, caller (source -> focus) =
                // violet.
                const callee = source === focus;
                return {
                  ...data,
                  hidden: false,
                  color: callee
                    ? lightThemeRef.current
                      ? "#0e7490"
                      : "#22d3ee"
                    : lightThemeRef.current
                      ? "#6d28d9"
                      : "#a78bfa",
                  size: 1.6,
                  zIndex: 3,
                };
              }
              // contains / other incident edges: neutral highlight
              return {
                ...data,
                hidden: false,
                color: lightThemeRef.current ? "#7c3aed" : "#c4b5fd",
                size: 1,
                zIndex: 2,
              };
            }
            return {
              ...data,
              hidden: data.kind !== "calls",
              color: String(data.baseColor ?? "#3f3f46"),
              size: Number(data.baseSize ?? 0.2),
              zIndex: 0,
            };
          }
          // Resting view: show the call graph faintly, hide file->symbol
          // membership clutter until something is selected.
          return {
            ...data,
            hidden: data.kind !== "calls",
            color: String(data.baseColor ?? "#3f3f46"),
            size: Number(data.baseSize ?? 0.2),
            zIndex: 0,
          };
        },
      });
    } catch {
      setRenderingUnavailable(true);
      return;
    }
    rendererRef.current = renderer;

    const themeObserver = new MutationObserver(() => {
      const light = document.documentElement.classList.contains("light");
      lightThemeRef.current = light;
      renderer.setSetting("labelColor", {
        color: light ? "#262626" : "#e5e5e5",
      });
      // Re-theme resting base colors in place (no relayout) so a live theme
      // toggle recolors every node/edge, not just labels and the selection.
      graph.forEachNode((id, attr) => {
        const themed = vivid(
          String(attr.rawColor ?? attr.baseColor ?? "#a3a3a3"),
          light
        );
        graph.setNodeAttribute(id, "baseColor", themed);
      });
      graph.forEachEdge((id, attr) => {
        const isCall = attr.kind === "calls";
        graph.setEdgeAttribute(
          id,
          "baseColor",
          isCall
            ? light
              ? "#cbd5e1"
              : "#3f3f46"
            : light
              ? "#e5e5e5"
              : "#262626"
        );
      });
      renderer.refresh({ skipIndexation: true });
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    renderer.on("clickNode", ({ node }) => onSelectRef.current(node));
    renderer.on("clickStage", () => onSelectRef.current(null));
    renderer.on("doubleClickNode", ({ node, preventSigmaDefault }) => {
      preventSigmaDefault();
      onExpandRef.current(node);
    });
    renderer.on("enterNode", ({ node }) => {
      hoveredRef.current = node;
      containerRef.current?.classList.add("cursor-pointer");
      renderer.refresh({ skipIndexation: true, schedule: true });
    });
    renderer.on("leaveNode", () => {
      hoveredRef.current = null;
      containerRef.current?.classList.remove("cursor-pointer");
      renderer.refresh({ skipIndexation: true, schedule: true });
    });

    return () => {
      if (settleTimerRef.current) window.clearTimeout(settleTimerRef.current);
      if (followFrameRef.current)
        window.cancelAnimationFrame(followFrameRef.current);
      layoutRef.current?.kill();
      layoutRef.current = null;
      themeObserver.disconnect();
      renderer.kill();
      rendererRef.current = null;
    };
  }, []);

  // ---- build the whole graph, settle ONCE, then freeze --------------- //
  useEffect(() => {
    const renderer = rendererRef.current;
    const graph = graphRef.current;
    if (!renderer) return;

    layoutRef.current?.kill();
    layoutRef.current = null;
    if (settleTimerRef.current) {
      window.clearTimeout(settleTimerRef.current);
      settleTimerRef.current = null;
    }

    graph.clear();
    if (!nodes.length) {
      renderer.refresh();
      return;
    }

    const cache = positionCacheRef.current;
    const seeds = nodes.every((node) => cache.has(node.id))
      ? cache
      : seedPositions(nodes);
    let missing = false;
    for (const node of nodes) {
      if (!cache.has(node.id)) missing = true;
      const seed = cache.get(node.id) ?? seeds.get(node.id) ?? { x: 0, y: 0 };
      const size = nodeSize(node);
      const rawColor =
        node.color ||
        (node.focus ? "#c4b5fd" : (KIND_COLORS[node.kind] ?? "#a3a3a3"));
      const color = vivid(rawColor, lightThemeRef.current);
      graph.addNode(node.id, {
        x: seed.x,
        y: seed.y,
        size,
        baseSize: size,
        label: node.label,
        color,
        baseColor: color,
        rawColor,
        kind: node.kind,
        nodeType: node.node_type,
        community: node.community,
        path: node.path,
        forceLabel: Boolean(node.focus),
        zIndex: node.focus ? 2 : 1,
      });
    }
    for (const edge of edges) {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
      if (graph.hasEdge(edge.source, edge.target)) continue;
      const isCall = edge.kind === "calls";
      const baseColor = isCall
        ? lightThemeRef.current
          ? "#cbd5e1"
          : "#3f3f46"
        : lightThemeRef.current
          ? "#e5e5e5"
          : "#262626";
      graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
        type: isCall ? "arrow" : "line",
        kind: edge.kind,
        color: baseColor,
        baseColor,
        size: isCall ? 0.35 : 0.12,
        baseSize: isCall ? 0.35 : 0.12,
      });
    }

    // recompute neighbours of the current selection against the new graph
    const set = new Set<string>();
    const sel = selectedRef.current;
    if (sel && graph.hasNode(sel)) {
      set.add(sel);
      graph.forEachNeighbor(sel, (neighbor) => set.add(neighbor));
    }
    neighborsRef.current = set;

    if (!missing) {
      // Every node already has a frozen position -> no physics, instant.
      setComputing(false);
      renderer.refresh();
      return;
    }

    // One-shot settle behind an opaque overlay, then stop forever.
    setComputing(true);
    renderer.refresh();
    const large = graph.order > 2000;
    const layout = new FA2Layout(graph, {
      settings: {
        ...inferSettings(graph),
        barnesHutOptimize: graph.order > 500,
        // Gentle separation: moderate gravity keeps the graph cohesive and the
        // gaps between groups filled, with only slight repulsion so
        // clusters are distinguishable without flying apart into islands.
        gravity: 0.45,
        scalingRatio: large ? 5 : 3,
        slowDown: 10,
        linLogMode: true,
      },
    });
    layoutRef.current = layout;
    layout.start();
    const duration = Math.min(6000, 900 + graph.order * 0.08);
    settleTimerRef.current = window.setTimeout(() => {
      layout.stop();
      layout.kill();
      layoutRef.current = null;
      settleTimerRef.current = null;
      graph.forEachNode((id, attr) =>
        cache.set(id, { x: Number(attr.x), y: Number(attr.y) })
      );
      // refresh neighbour set once positions are final (selection unchanged)
      const nset = new Set<string>();
      const s = selectedRef.current;
      if (s && graph.hasNode(s)) {
        nset.add(s);
        graph.forEachNeighbor(s, (neighbor) => nset.add(neighbor));
      }
      neighborsRef.current = nset;
      setComputing(false);
      renderer.refresh();
      renderer.getCamera().animatedReset({ duration: 320 });
    }, duration);
  }, [nodes, edges]);

  // ---- selection: recolor only (no relayout) ------------------------- //
  useEffect(() => {
    selectedRef.current = selectedId;
    const graph = graphRef.current;
    const set = new Set<string>();
    if (selectedId && graph.hasNode(selectedId)) {
      set.add(selectedId);
      graph.forEachNeighbor(selectedId, (neighbor) => set.add(neighbor));
    }
    neighborsRef.current = set;
    rendererRef.current?.refresh({ skipIndexation: true });
  }, [selectedId]);

  // ---- activity highlight: recolor only ------------------------------ //
  useEffect(() => {
    activityRef.current = activityByNode;
    rendererRef.current?.refresh({ skipIndexation: true });
  }, [activityByNode]);

  useEffect(() => {
    if (!Object.keys(activityByNode).length) return;
    const timer = window.setInterval(() => {
      pulseRef.current = !pulseRef.current;
      rendererRef.current?.refresh({ skipIndexation: true, schedule: true });
    }, 620);
    return () => window.clearInterval(timer);
  }, [activityByNode]);

  // ---- follow the live cursor without moving any node ---------------- //
  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer || !followNodeId || computing) return;
    if (!graphRef.current.hasNode(followNodeId)) return;
    followFrameRef.current = window.requestAnimationFrame(() => {
      const position = renderer.getNodeDisplayData(followNodeId);
      if (!position) return;
      const state = renderer.getCamera().getState();
      renderer
        .getCamera()
        .animate(
          { x: position.x, y: position.y, ratio: Math.min(state.ratio, 0.35) },
          { duration: 480 }
        );
    });
    return () => {
      if (followFrameRef.current)
        window.cancelAnimationFrame(followFrameRef.current);
    };
  }, [followNodeId, computing]);

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
        aria-label={`Interactive universe showing ${nodes.length} nodes and ${edges.length} relationships. Drag to pan, scroll to zoom, click a node to inspect, double-click to focus its callers and callees.`}
      />
      {computing && (
        <div className="absolute inset-0 z-20 flex items-center justify-center gap-3 bg-surface-sunken text-sm text-neutral-300">
          <Loader2 className="animate-spin text-brand-300" size={18} />
          Computing layout…
        </div>
      )}
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
          className="p-2 text-neutral-300 transition hover:bg-neutral-800 hover:text-white"
          onClick={() => void camera()?.animatedReset({ duration: 260 })}
          aria-label="Fit graph"
        >
          <Focus size={15} />
        </button>
      </div>
      <div className="absolute bottom-4 right-4 border border-neutral-800 bg-neutral-950/85 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300">
        {nodes.length.toLocaleString()} nodes · {edges.length.toLocaleString()}{" "}
        edges
      </div>
    </div>
  );
}
