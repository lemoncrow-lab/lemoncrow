import { Button as UiButton } from "../components/ui/button";
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

type GraphCameraState = { x: number; y: number; ratio: number; angle: number };

const POSITION_CACHE_BY_SCOPE = new Map<string, Map<string, { x: number; y: number }>>();
const CAMERA_CACHE_BY_SCOPE = new Map<string, GraphCameraState>();

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

function nodeSize(node: CodeMapNode, compact = false): number {
  const degree = Math.sqrt(node.degree ?? 0);
  const scale = compact ? 1.7 : 1;
  if (node.node_type === "community") return Math.min(compact ? 10 : 6, (2.4 + degree * 0.28) * scale);
  if (node.node_type === "file") return Math.min(compact ? 7 : 3.8, (1.25 + degree * 0.18) * scale);
  return Math.min(compact ? 5.8 : 3.3, (0.8 + degree * 0.21) * scale);
}

// Deterministic cluster seed: groups are dropped onto a golden-angle
// spiral, members scattered around their community centroid. ForceAtlas2 then
// refines this ONCE; the seed only makes that single settle converge fast and
// look the same on every load.
function seedPositions(
  nodes: CodeMapNode[],
  compact = false
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
      communityIndex === 0 ? 0 : (compact ? 6 : 18) * Math.sqrt(communityIndex);
    const cx = Math.cos(groupAngle) * groupRadius;
    const cy = Math.sin(groupAngle) * groupRadius;
    const spread = compact
      ? 1.7 + Math.sqrt(members.length) * 0.38
      : 3 + Math.sqrt(members.length) * 0.62;
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

function reviewSeedPositions(
  nodes: CodeMapNode[],
  edges: CodeMapEdge[],
  featured: ReadonlySet<string>,
): Map<string, { x: number; y: number }> {
  const anchors = nodes
    .filter((node) => featured.has(node.id))
    .sort((a, b) => (a.path || a.label).localeCompare(b.path || b.label));
  if (!anchors.length) return seedPositions(nodes, true);

  const positions = new Map<string, { x: number; y: number }>();
  const anchorIds = new Set(anchors.map((node) => node.id));
  const anchorCenter = new Map<string, { x: number; y: number }>();
  const columns = Math.max(1, Math.ceil(Math.sqrt(anchors.length)));
  const rows = Math.ceil(anchors.length / columns);
  const gapX = 14;
  const gapY = 11;

  anchors.forEach((node, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    const point = {
      x: (column - (columns - 1) / 2) * gapX,
      y: ((rows - 1) / 2 - row) * gapY,
    };
    anchorCenter.set(node.id, point);
    positions.set(node.id, point);
  });

  const relationWeight: Record<string, number> = {
    impacts: 10,
    calls: 7,
    imports: 5,
    references: 1,
  };
  const bestAnchor = new Map<string, { anchor: string; score: number }>();
  for (const edge of edges) {
    const sourceAnchor = anchorIds.has(edge.source);
    const targetAnchor = anchorIds.has(edge.target);
    if (sourceAnchor === targetAnchor) continue;
    const anchor = sourceAnchor ? edge.source : edge.target;
    const context = sourceAnchor ? edge.target : edge.source;
    const score =
      (relationWeight[edge.kind] ?? 1) * Math.max(1, Math.min(6, edge.weight ?? 1));
    const prior = bestAnchor.get(context);
    if (!prior || score > prior.score || (score === prior.score && anchor < prior.anchor)) {
      bestAnchor.set(context, { anchor, score });
    }
  }

  const groups = new Map<string, CodeMapNode[]>();
  for (const anchor of anchors) groups.set(anchor.id, []);
  const unassigned: CodeMapNode[] = [];
  for (const node of nodes) {
    if (anchorIds.has(node.id)) continue;
    const assignment = bestAnchor.get(node.id);
    if (assignment) groups.get(assignment.anchor)?.push(node);
    else unassigned.push(node);
  }

  for (const anchor of anchors) {
    const center = anchorCenter.get(anchor.id) ?? { x: 0, y: 0 };
    const members = (groups.get(anchor.id) ?? []).sort(
      (a, b) => stableUnit(a.id) - stableUnit(b.id)
    );
    members.forEach((node, index) => {
      const ring = Math.floor(index / 8);
      const ringIndex = index % 8;
      const ringCount = Math.min(8, members.length - ring * 8);
      const angle =
        (ringIndex / Math.max(1, ringCount)) * Math.PI * 2 +
        stableUnit(anchor.id) * 0.65;
      const radius = 4.1 + ring * 2.6;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius * 0.78,
      });
    });
  }

  const outerRadius = Math.max(12, Math.max(columns * gapX, rows * gapY) * 0.72);
  unassigned
    .sort((a, b) => stableUnit(a.id) - stableUnit(b.id))
    .forEach((node, index) => {
      const angle = (index / Math.max(1, unassigned.length)) * Math.PI * 2;
      positions.set(node.id, {
        x: Math.cos(angle) * outerRadius,
        y: Math.sin(angle) * outerRadius * 0.72,
      });
    });

  return positions;
}

export default function CodeGraph({
  cacheKey = "default",
  nodes,
  edges,
  selectedId,
  activityByNode,
  followNodeId,
  selectedEdgeId = null,
  edgeVisibility = "calls",
  directedKinds = ["calls"],
  presentation = "default",
  forceLabels = false,
  featuredNodeIds = [],
  dimUnrelatedSelection = true,
  onSelect,
  onSelectEdge,
  onExpand,
}: {
  cacheKey?: string;
  nodes: CodeMapNode[];
  edges: CodeMapEdge[];
  selectedId: string | null;
  activityByNode: Record<string, CodeMapActivityKind>;
  followNodeId: string | null;
  selectedEdgeId?: string | null;
  edgeVisibility?: "calls" | "all";
  directedKinds?: readonly string[];
  presentation?: "default" | "compact" | "review";
  forceLabels?: boolean;
  featuredNodeIds?: readonly string[];
  dimUnrelatedSelection?: boolean;
  onSelect: (nodeId: string | null) => void;
  onSelectEdge?: (edgeId: string | null) => void;
  onExpand: (nodeId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  // A pair of code entities can legitimately have several semantic links
  // (for example imports + calls + references). Preserve each keyed edge.
  const graphRef = useRef(new Graph({ type: "directed", multi: true }));
  const rendererRef = useRef<Sigma | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const settleTimerRef = useRef<number | null>(null);
  const followFrameRef = useRef<number | null>(null);
  const resizeFrameRef = useRef<number | null>(null);
  // Settled positions and camera survive unmount/remount per graph scope, so
  // switching 2D/3D or drilling in/out does not make familiar nodes "dance".
  const positionCacheRef = useRef(
    POSITION_CACHE_BY_SCOPE.get(cacheKey) ?? new Map<string, { x: number; y: number }>()
  );
  if (!POSITION_CACHE_BY_SCOPE.has(cacheKey)) {
    POSITION_CACHE_BY_SCOPE.set(cacheKey, positionCacheRef.current);
  }
  const hadSavedCameraRef = useRef(CAMERA_CACHE_BY_SCOPE.has(cacheKey));
  const selectedRef = useRef<string | null>(selectedId);
  const selectedEdgeRef = useRef<string | null>(selectedEdgeId);
  const hoveredRef = useRef<string | null>(null);
  const neighborsRef = useRef<Set<string>>(new Set());
  const activityRef = useRef(activityByNode);
  const edgeVisibilityRef = useRef(edgeVisibility);
  edgeVisibilityRef.current = edgeVisibility;
  const featuredNodeIdsRef = useRef(new Set(featuredNodeIds));
  featuredNodeIdsRef.current = new Set(featuredNodeIds);
  const directedKindsRef = useRef(new Set(directedKinds));
  directedKindsRef.current = new Set(directedKinds);
  const pulseRef = useRef(false);
  const onSelectRef = useRef(onSelect);
  const onSelectEdgeRef = useRef(onSelectEdge);
  const onExpandRef = useRef(onExpand);
  onSelectRef.current = onSelect;
  onSelectEdgeRef.current = onSelectEdge;
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
        labelDensity: presentation === "compact" ? 1.35 : presentation === "review" ? 1 : 1,
        labelGridCellSize: presentation === "compact" ? 70 : presentation === "review" ? 96 : 92,
        labelRenderedSizeThreshold: presentation === "compact" ? 0 : presentation === "review" ? 4.8 : 4.2,
        labelSize: presentation === "review" ? 11 : 12,
        stagePadding: presentation === "compact" ? 50 : presentation === "review" ? 72 : 42,
        minCameraRatio: 0.02,
        maxCameraRatio: 8,
        // Zoom should reveal topology, not inflate markers. Keep node/edge
        // sizes in screen space while graph positions spread under the camera.
        itemSizesReference: "screen",
        zoomToSizeRatioFunction: () => 1,
        renderEdgeLabels: false,
        zIndex: true,
        defaultDrawNodeHover: drawHoverBadge,
        nodeReducer: (nodeId, data) => {
          const selected = selectedRef.current;
          const hovered = hoveredRef.current;
          const activeKind = activityRef.current[nodeId];
          const dimmed =
            dimUnrelatedSelection &&
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
              ? base * (pulseRef.current ? 1.55 : 1.3)
              : nodeId === selected
                ? base * 1.28
                : nodeId === hovered
                  ? base * 1.4
                  : base,
            highlighted,
            forceLabel: Boolean(data.forceLabel) || highlighted,
            zIndex: highlighted ? 3 : dimmed ? 0 : 1,
          };
        },
        edgeReducer: (edgeId, data) => {
          const focus = selectedRef.current || hoveredRef.current;
          const [source, target] = graph.extremities(edgeId);
          if (selectedEdgeRef.current === edgeId) {
            return {
              ...data,
              hidden: false,
              color: lightThemeRef.current ? "#6d28d9" : "#c4b5fd",
              size: Math.max(2.4, Number(data.baseSize ?? 0.5) * 2.2),
              zIndex: 4,
            };
          }
          const activeKind =
            activityRef.current[source] || activityRef.current[target];
          if (activeKind) {
            return {
              ...data,
              hidden: false,
              color: ACTIVITY_COLORS[activeKind][lightThemeRef.current ? 1 : 0],
              size: pulseRef.current ? 1.35 : 1.05,
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
                  size: 1.25,
                  zIndex: 3,
                };
              }
              // Other incident relationships keep their semantic base color
              // when a specialized projection supplied one.
              return {
                ...data,
                hidden: false,
                color: String(data.semanticColor ?? data.baseColor ?? (lightThemeRef.current ? "#64748b" : "#94a3b8")),
                size: Math.max(0.78, Number(data.baseSize ?? 0.12) * 1.25),
                zIndex: 2,
              };
            }
            return {
              ...data,
              hidden: edgeVisibilityRef.current === "calls" && data.kind !== "calls",
              color: String(data.baseColor ?? "#3f3f46"),
              size: Number(data.baseSize ?? 0.2),
              zIndex: 0,
            };
          }
          // Resting view: show the graph faintly; callers/callees and other
          // incident relationships become prominent on selection.
          return {
            ...data,
            hidden: edgeVisibilityRef.current === "calls" && data.kind !== "calls",
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

    const camera = renderer.getCamera();
    const savedCamera = CAMERA_CACHE_BY_SCOPE.get(cacheKey);
    if (savedCamera) camera.setState(savedCamera);
    const saveCamera = () => CAMERA_CACHE_BY_SCOPE.set(cacheKey, camera.getState());
    camera.on("updated", saveCamera);

    // Keep Sigma synchronized with the available workbench size, but do not
    // move the camera on resize. The old graph's spatial memory is more useful
    // than repeatedly fitting it while sidebars/header widths change.
    const resizeObserver = containerRef.current
      ? new ResizeObserver(() => {
          if (resizeFrameRef.current) {
            window.cancelAnimationFrame(resizeFrameRef.current);
          }
          resizeFrameRef.current = window.requestAnimationFrame(() => {
            renderer.refresh();
          });
        })
      : null;
    if (resizeObserver && containerRef.current) {
      resizeObserver.observe(containerRef.current);
    }

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
        const rawColor = String(attr.rawColor ?? "");
        graph.setEdgeAttribute(
          id,
          "semanticColor",
          rawColor ? vivid(rawColor, light) : light ? "#64748b" : "#94a3b8"
        );
        graph.setEdgeAttribute(
          id,
          "baseColor",
          light ? "rgba(100,116,139,0.16)" : "rgba(148,163,184,0.18)"
        );
      });
      renderer.refresh({ skipIndexation: true });
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    renderer.on("clickNode", ({ node }) => {
      onSelectEdgeRef.current?.(null);
      onSelectRef.current(node);
    });
    renderer.on("clickEdge", ({ edge }) => {
      onSelectRef.current(null);
      onSelectEdgeRef.current?.(edge);
    });
    renderer.on("clickStage", () => {
      onSelectRef.current(null);
      onSelectEdgeRef.current?.(null);
    });
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
      if (resizeFrameRef.current)
        window.cancelAnimationFrame(resizeFrameRef.current);
      resizeObserver?.disconnect();
      layoutRef.current?.kill();
      layoutRef.current = null;
      themeObserver.disconnect();
      saveCamera();
      camera.off("updated", saveCamera);
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

    const compact = presentation !== "default";
    const review = presentation === "review";
    const cache = positionCacheRef.current;
    const featured = featuredNodeIdsRef.current;
    const seeds = nodes.every((node) => cache.has(node.id))
      ? cache
      : review
        ? reviewSeedPositions(nodes, edges, featured)
        : seedPositions(nodes, compact);
    let missing = false;
    for (const node of nodes) {
      if (!cache.has(node.id)) missing = true;
      const seed = cache.get(node.id) ?? seeds.get(node.id) ?? { x: 0, y: 0 };
      const featuredNode = featured.has(node.id);
      const size = nodeSize(node, compact) * (review ? (featuredNode ? 1.4 : 0.78) : 1);
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
        forceLabel: forceLabels || featuredNode || Boolean(node.focus),
        zIndex: featuredNode || node.focus ? 2 : 1,
      });
    }
    for (const edge of edges) {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
      if (graph.hasEdge(edge.id)) continue;
      const isCall = edge.kind === "calls";
      const directional = directedKindsRef.current.has(edge.kind);
      const compactEdgeBase = review
        ? edge.kind === "impacts"
          ? 0.82
          : isCall
            ? 0.58
            : edge.kind === "imports"
              ? 0.48
              : edge.kind === "references"
                ? 0.28
                : 0.36
        : edge.kind === "impacts"
          ? 1.05
          : isCall
            ? 0.9
            : edge.kind === "references"
              ? 0.72
              : 0.55;
      const weightBoost = Math.min(1.35, 0.82 + Math.log2(Math.max(1, edge.weight ?? 1) + 1) * 0.12);
      const restingEdgeBase =
        edge.kind === "impacts"
          ? 0.28
          : isCall
            ? 0.18
            : edge.kind === "imports"
              ? 0.13
              : edge.kind === "references"
                ? 0.07
                : 0.1;
      const edgeSize = compact ? compactEdgeBase * weightBoost : restingEdgeBase * weightBoost;
      const rawColor = edge.color ?? "";
      const semanticColor = rawColor
        ? vivid(rawColor, lightThemeRef.current)
        : lightThemeRef.current
          ? "#64748b"
          : "#94a3b8";
      // Resting topology should read as structure, not a saturated wiring
      // diagram. Semantic edge hue is reserved for hover/selection.
      const baseColor = lightThemeRef.current
        ? "rgba(100,116,139,0.16)"
        : "rgba(148,163,184,0.18)";
      graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
        type: directional ? "arrow" : "line",
        kind: edge.kind,
        color: baseColor,
        baseColor,
        semanticColor,
        rawColor,
        size: edgeSize,
        baseSize: edgeSize,
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

    if (!missing || (compact && graph.order <= 80)) {
      // Review maps are deliberately small and deterministic. Do not pay the
      // worker/layout startup cost for a handful of nodes: seeded positions
      // are already stable and readable. Also re-fit compact maps after
      // filtering/restoring nodes so a small subset never remains off-camera.
      if (missing) {
        graph.forEachNode((id, attr) =>
          cache.set(id, { x: Number(attr.x), y: Number(attr.y) })
        );
      }
      setComputing(false);
      renderer.refresh();
      if (compact && !hadSavedCameraRef.current) {
        window.requestAnimationFrame(() => {
          renderer.getCamera().animatedReset({ duration: 180 });
        });
      }
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
        gravity: compact ? 1.15 : 0.62,
        scalingRatio: compact ? (large ? 2.9 : 1.9) : large ? 5.4 : 3.45,
        slowDown: compact ? 14 : 10,
        linLogMode: true,
      },
    });
    layoutRef.current = layout;
    layout.start();
    const duration = Math.min(3600, 650 + graph.order * 0.06);
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
      if (!hadSavedCameraRef.current) {
        renderer.getCamera().animatedReset({ duration: 260 });
      }
    }, duration);
  }, [nodes, edges, presentation, forceLabels]);

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

  useEffect(() => {
    selectedEdgeRef.current = selectedEdgeId;
    rendererRef.current?.refresh({ skipIndexation: true });
  }, [selectedEdgeId]);

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
    }, 900);
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
      <div className="h-full min-h-[420px] overflow-y-auto bg-surface-sunken p-4">
        <div className="rounded-lg border border-neutral-800/70 border-l-2 border-l-amber-600/50 bg-neutral-950/60 p-4 text-sm text-neutral-300">
          WebGL is unavailable, so the graph is shown as a navigable list capped
          at 500 items.
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {nodes.slice(0, 500).map((node) => (
            <UiButton unstyled
              key={node.id}
              type="button"
              onClick={() => onSelect(node.id)}
              onDoubleClick={() => onExpand(node.id)}
              className="rounded-md border border-neutral-800/70 bg-neutral-950/60 p-3 text-left transition-colors hover:border-neutral-600"
            >
              <span className="block truncate text-sm text-neutral-100">
                {node.label}
              </span>
              <span className="mt-1 block truncate text-caption text-muted-foreground">
                {node.path}:{node.line}
              </span>
            </UiButton>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className={"relative h-full min-h-[420px] overflow-hidden " + (presentation === "review" ? "bg-surface-sunken bg-[radial-gradient(circle_at_50%_42%,rgba(99,102,241,0.06),transparent_36%)]" : "bg-surface-sunken")}>
      <div
        ref={containerRef}
        className="absolute inset-0"
        role="img"
        aria-label={`Interactive universe showing ${nodes.length} nodes and ${edges.length} relationships. Drag to pan, scroll to zoom, click a node to inspect, double-click to focus its callers and callees.`}
      />
      {computing && (
        <div className="pointer-events-none absolute left-3 top-3 z-20 flex items-center gap-2 rounded-md border border-border bg-background/90 px-2.5 py-1.5 text-caption text-muted-foreground shadow-sm backdrop-blur">
          <Loader2 className="animate-spin" size={12} />
          Settling layout…
        </div>
      )}
      <div className="absolute bottom-4 left-4 z-10 flex overflow-hidden rounded-lg border border-border bg-background/90 p-0.5 shadow-lg backdrop-blur-md">
        <UiButton
          type="button"
          size="icon"
          variant="ghost"
          onClick={() => void camera()?.animatedZoom({ duration: 180 })}
          aria-label="Zoom in"
        >
          <Plus size={15} />
        </UiButton>
        <UiButton
          type="button"
          size="icon"
          variant="ghost"
          onClick={() => void camera()?.animatedUnzoom({ duration: 180 })}
          aria-label="Zoom out"
        >
          <Minus size={15} />
        </UiButton>
        <UiButton
          type="button"
          size="icon"
          variant="ghost"
          onClick={() => void camera()?.animatedReset({ duration: 220 })}
          aria-label="Fit graph"
        >
          <Focus size={15} />
        </UiButton>
      </div>
      <div className="pointer-events-none absolute bottom-4 right-4 rounded-lg border border-border bg-background/85 px-2.5 py-1.5 text-micro font-medium text-muted-foreground shadow-lg backdrop-blur-md">
        {nodes.length.toLocaleString()} nodes <span className="mx-1 text-muted-foreground/50">·</span> {edges.length.toLocaleString()} relationships
      </div>
    </div>
  );
}
