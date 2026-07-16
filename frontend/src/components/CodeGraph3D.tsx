import { useEffect, useRef, useState } from "react";
import ForceGraph3D, { type ForceGraph3DInstance } from "3d-force-graph";
import { Focus, Loader2 } from "lucide-react";
import type { CodeMapActivityKind, CodeMapEdge, CodeMapNode } from "../api";
import {
  ACTIVITY_COLORS,
  KIND_COLORS,
  stableUnit,
  vivid,
} from "../lib/graphColors";

type FgNode = {
  id: string;
  label: string;
  qualified_name: string;
  path: string;
  kind: string;
  community: string;
  line: number;
  degree: number;
  val: number;
  raw: string;
  base: string;
  x?: number;
  y?: number;
  z?: number;
};
type FgLink = { source: string | FgNode; target: string | FgNode };

function endId(end: string | FgNode): string {
  return typeof end === "object" ? end.id : end;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Deterministic 3D seed: community centroids spread over a Fibonacci sphere,
// members jittered around them, so the single force settle converges fast and
// looks the same each load.
function seed3d(nodes: FgNode[]): void {
  const groups = new Map<string, FgNode[]>();
  for (const node of nodes) {
    const key = node.community || "root";
    const bucket = groups.get(key);
    if (bucket) bucket.push(node);
    else groups.set(key, [node]);
  }
  const names = [...groups.keys()].sort();
  const count = names.length;
  const golden = Math.PI * (3 - Math.sqrt(5));
  const radius = 170;
  names.forEach((name, index) => {
    const t = count <= 1 ? 0 : index / (count - 1);
    const y = 1 - t * 2;
    const ring = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * index;
    const cx = Math.cos(theta) * ring * radius;
    const cy = y * radius;
    const cz = Math.sin(theta) * ring * radius;
    const members = groups.get(name) ?? [];
    const spread = 6 + Math.sqrt(members.length) * 1.6;
    for (const node of members) {
      node.x = cx + (stableUnit(node.id + "x") - 0.5) * spread;
      node.y = cy + (stableUnit(node.id + "y") - 0.5) * spread;
      node.z = cz + (stableUnit(node.id + "z") - 0.5) * spread;
    }
  });
}

function buildData(nodes: CodeMapNode[], edges: CodeMapEdge[], light: boolean) {
  const chosen = nodes;
  const idset = new Set(chosen.map((node) => node.id));
  const fgNodes: FgNode[] = chosen.map((node) => {
    const raw =
      node.color ||
      (node.focus ? "#c4b5fd" : (KIND_COLORS[node.kind] ?? "#a3a3a3"));
    const degree = node.degree ?? 0;
    return {
      id: node.id,
      label: node.label,
      qualified_name: node.qualified_name,
      path: node.path,
      kind: node.kind,
      community: node.community || "root",
      line: node.line,
      degree,
      val: 1 + Math.sqrt(degree) * 0.8,
      raw,
      base: vivid(raw, light),
    };
  });
  const links: FgLink[] = [];
  for (const edge of edges) {
    if (edge.kind !== "calls") continue;
    if (idset.has(edge.source) && idset.has(edge.target)) {
      links.push({ source: edge.source, target: edge.target });
    }
  }
  seed3d(fgNodes);
  return { nodes: fgNodes, links, total: nodes.length };
}

export default function CodeGraph3D({
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
  const fgRef = useRef<ForceGraph3DInstance | null>(null);
  const nodesRef = useRef<FgNode[]>([]);
  const selectedRef = useRef<string | null>(selectedId);
  const neighborsRef = useRef<Set<string>>(new Set());
  const activityRef = useRef(activityByNode);
  const onSelectRef = useRef(onSelect);
  const onExpandRef = useRef(onExpand);
  onSelectRef.current = onSelect;
  onExpandRef.current = onExpand;
  const lightThemeRef = useRef(
    document.documentElement.classList.contains("light")
  );
  const [computing, setComputing] = useState(false);
  const [shown, setShown] = useState({ shown: 0, total: 0 });

  const nodeColorFor = (node: FgNode): string => {
    const light = lightThemeRef.current;
    const kind = activityRef.current[node.id];
    if (kind) return ACTIVITY_COLORS[kind][light ? 1 : 0];
    const selected = selectedRef.current;
    if (selected) {
      if (node.id === selected) return light ? "#7c3aed" : "#ddd6fe";
      if (!neighborsRef.current.has(node.id))
        return light ? "#d4d4d4" : "#2f2f2f";
    }
    return node.base;
  };
  const linkColorFor = (link: FgLink): string => {
    const light = lightThemeRef.current;
    const source = endId(link.source);
    const target = endId(link.target);
    const kind = activityRef.current[source] || activityRef.current[target];
    if (kind) return ACTIVITY_COLORS[kind][light ? 1 : 0];
    const selected = selectedRef.current;
    if (selected) {
      if (source === selected) return light ? "#0e7490" : "#22d3ee"; // callee
      if (target === selected) return light ? "#6d28d9" : "#a78bfa"; // caller
      return light ? "#ececec" : "#242424";
    }
    return light ? "#cbd5e1" : "#3f3f46";
  };
  const linkWidthFor = (link: FgLink): number => {
    const selected = selectedRef.current;
    if (!selected) return 0.4;
    return endId(link.source) === selected || endId(link.target) === selected
      ? 1.4
      : 0.3;
  };

  const applyStyles = () => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.nodeColor((node) => nodeColorFor(node as unknown as FgNode))
      .linkColor((link) => linkColorFor(link as FgLink))
      .linkWidth((link) => linkWidthFor(link as FgLink))
      .linkDirectionalArrowColor((link) => linkColorFor(link as FgLink));
  };

  // create the renderer once
  useEffect(() => {
    if (!containerRef.current) return;
    const fg = new ForceGraph3D(containerRef.current)
      .backgroundColor(lightThemeRef.current ? "#f4f4f5" : "#0a0a0a")
      .showNavInfo(false)
      .nodeId("id")
      .nodeRelSize(3)
      .nodeResolution(6)
      .nodeOpacity(0.92)
      .nodeVal((node) => (node as unknown as FgNode).val)
      .nodeColor((node) => nodeColorFor(node as unknown as FgNode))
      .nodeLabel((node) => {
        const n = node as unknown as FgNode;
        const light = lightThemeRef.current;
        return `<div style="background:${light ? "#ffffff" : "#171717"};color:${light ? "#171717" : "#f5f5f5"};border:1px solid ${light ? "#e5e5e5" : "#3f3f46"};padding:4px 8px;border-radius:4px;font:12px system-ui,sans-serif;max-width:340px"><b>${escapeHtml(n.label)}</b><br><span style="opacity:.65">${escapeHtml(n.path)}:${n.line}</span></div>`;
      })
      .linkColor((link) => linkColorFor(link as FgLink))
      .linkWidth((link) => linkWidthFor(link as FgLink))
      .linkOpacity(0.4)
      .linkDirectionalArrowLength(2.4)
      .linkDirectionalArrowRelPos(1)
      .linkDirectionalArrowColor((link) => linkColorFor(link as FgLink))
      .enableNodeDrag(false)
      .warmupTicks(0)
      .cooldownTicks(120)
      .onEngineStop(() => {
        setComputing(false);
        fg.zoomToFit(600, 60);
      })
      .onNodeClick((node) =>
        onSelectRef.current((node as unknown as FgNode).id)
      )
      .onNodeRightClick((node) =>
        onExpandRef.current((node as unknown as FgNode).id)
      )
      .onBackgroundClick(() => onSelectRef.current(null));
    fgRef.current = fg;

    const resize = () => {
      const el = containerRef.current;
      if (!el) return;
      fg.width(el.clientWidth).height(el.clientHeight);
    };
    resize();
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(containerRef.current);

    const themeObserver = new MutationObserver(() => {
      const light = document.documentElement.classList.contains("light");
      lightThemeRef.current = light;
      fg.backgroundColor(light ? "#f4f4f5" : "#0a0a0a");
      for (const node of nodesRef.current) node.base = vivid(node.raw, light);
      applyStyles();
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    return () => {
      resizeObserver.disconnect();
      themeObserver.disconnect();
      fg._destructor();
      fgRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // (re)build the graph data, settle once, then freeze
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const data = buildData(nodes, edges, lightThemeRef.current);
    nodesRef.current = data.nodes;
    setShown({ shown: data.nodes.length, total: data.total });
    const selected = selectedRef.current;
    const set = new Set<string>();
    if (selected) set.add(selected);
    neighborsRef.current = set;
    setComputing(true);
    // Larger graphs get fewer settle ticks and lower-poly nodes so the
    // single settle stays quick and rendering stays smooth.
    const heavy = data.nodes.length > 8000;
    fg.cooldownTicks(heavy ? 30 : 120).nodeResolution(heavy ? 3 : 6);
    fg.graphData({ nodes: data.nodes, links: data.links });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  // selection -> recolor only
  useEffect(() => {
    selectedRef.current = selectedId;
    const set = new Set<string>();
    if (selectedId) {
      set.add(selectedId);
      for (const link of edges) {
        if (link.kind !== "calls") continue;
        if (link.source === selectedId) set.add(link.target);
        if (link.target === selectedId) set.add(link.source);
      }
    }
    neighborsRef.current = set;
    applyStyles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, edges]);

  // activity highlight -> recolor only
  useEffect(() => {
    activityRef.current = activityByNode;
    applyStyles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityByNode]);

  // follow the live cursor by flying the camera to the node
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || !followNodeId || computing) return;
    const node = nodesRef.current.find(
      (candidate) => candidate.id === followNodeId
    );
    if (!node || node.x === undefined) return;
    const distance = 90;
    const distRatio =
      1 + distance / Math.hypot(node.x, node.y ?? 0, (node.z ?? 0) || 1);
    fg.cameraPosition(
      {
        x: (node.x ?? 0) * distRatio,
        y: (node.y ?? 0) * distRatio,
        z: (node.z ?? 0) * distRatio,
      },
      { x: node.x ?? 0, y: node.y ?? 0, z: node.z ?? 0 },
      800
    );
  }, [followNodeId, computing]);

  return (
    <div className="relative h-full min-h-[420px] overflow-hidden bg-surface-sunken">
      <div ref={containerRef} className="absolute inset-0" />
      {computing && (
        <div className="absolute inset-0 z-20 flex items-center justify-center gap-3 bg-surface-sunken text-sm text-neutral-300">
          <Loader2 className="animate-spin text-brand-300" size={18} />
          Building 3D layout…
        </div>
      )}
      <button
        type="button"
        onClick={() => fgRef.current?.zoomToFit(500, 60)}
        className="absolute bottom-4 left-4 border border-neutral-700 bg-neutral-950/90 p-2 text-neutral-300 shadow-lg transition hover:bg-neutral-800 hover:text-white"
        aria-label="Fit graph"
      >
        <Focus size={15} />
      </button>
      <div className="absolute bottom-4 right-4 border border-neutral-800 bg-neutral-950/85 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300">
        {shown.shown.toLocaleString()} nodes · 3D
      </div>
    </div>
  );
}
