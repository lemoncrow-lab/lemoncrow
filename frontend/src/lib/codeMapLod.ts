import type { CodeMapEdge, CodeMapNode } from "../api";

export type CodeMapDetail = "overview" | "files" | "symbols" | "full";

export interface CodeMapExpansion {
  communityIds: ReadonlySet<string>;
  fileIds: ReadonlySet<string>;
}

export interface CodeMapHierarchy {
  communityNodes: CodeMapNode[];
  fileNodes: CodeMapNode[];
  symbolNodes: CodeMapNode[];
  communityEdges: CodeMapEdge[];
  fileEdges: CodeMapEdge[];
  sourceEdges: CodeMapEdge[];
  filesByCommunity: Map<string, CodeMapNode[]>;
  symbolsByFile: Map<string, CodeMapNode[]>;
  fileByPath: Map<string, CodeMapNode>;
  nodeById: Map<string, CodeMapNode>;
}

export interface CodeMapView {
  nodes: CodeMapNode[];
  edges: CodeMapEdge[];
  detail: CodeMapDetail;
  hiddenCount: number;
}

const FILES_PER_COMMUNITY = [0, 4, 12, 32, 96, 240, 500, 1_000_000, 1_000_000];
const SYMBOLS_PER_FILE = [0, 0, 0, 0, 0, 4, 12, 50, 1_000_000];
const ENTER_RATIOS = [0.78, 0.58, 0.44, 0.33, 0.245, 0.18, 0.132, 0.096];
const EXIT_RATIOS = [0.88, 0.66, 0.5, 0.38, 0.285, 0.21, 0.153, 0.112];

function aggregateEdge(
  edges: Map<string, CodeMapEdge>,
  source: string,
  target: string,
  prefix: string,
  weight: number
) {
  if (!source || !target || source === target) return;
  const key = `${prefix}::${source}::${target}`;
  const current = edges.get(key);
  if (current) {
    current.weight = (current.weight ?? 1) + weight;
    return;
  }
  edges.set(key, {
    id: key,
    source,
    target,
    kind: "aggregate",
    depth: 0,
    weight,
  });
}

function byWeight(edges: Iterable<CodeMapEdge>): CodeMapEdge[] {
  return [...edges].sort(
    (left, right) => (right.weight ?? 1) - (left.weight ?? 1)
  );
}

function compareNodes(left: CodeMapNode, right: CodeMapNode): number {
  if (Boolean(left.focus) !== Boolean(right.focus)) return left.focus ? -1 : 1;
  const degree = (right.degree ?? 0) - (left.degree ?? 0);
  if (degree) return degree;
  return `${left.path}:${left.line}:${left.id}`.localeCompare(
    `${right.path}:${right.line}:${right.id}`
  );
}

export function buildCodeMapHierarchy(
  nodes: CodeMapNode[],
  edges: CodeMapEdge[]
): CodeMapHierarchy {
  const originalFiles = nodes.filter((node) => node.node_type === "file");
  const symbolNodes = nodes.filter((node) => node.node_type === "symbol");
  const fileNodes = originalFiles.map((node) => ({ ...node, degree: 0 }));
  const fileByPath = new Map(fileNodes.map((node) => [node.path, node]));
  const nodeById = new Map(
    [...fileNodes, ...symbolNodes].map((node) => [node.id, node])
  );
  const fileNodeById = new Map(fileNodes.map((node) => [node.id, node]));
  const fileEdges = new Map<string, CodeMapEdge>();

  for (const edge of edges) {
    if (edge.kind !== "calls") continue;
    const sourceNode = nodeById.get(edge.source);
    const targetNode = nodeById.get(edge.target);
    const sourceFile = sourceNode ? fileByPath.get(sourceNode.path) : undefined;
    const targetFile = targetNode ? fileByPath.get(targetNode.path) : undefined;
    if (!sourceFile || !targetFile) continue;
    aggregateEdge(
      fileEdges,
      sourceFile.id,
      targetFile.id,
      "file-call",
      edge.weight ?? 1
    );
  }

  for (const edge of fileEdges.values()) {
    const source = fileNodeById.get(edge.source);
    const target = fileNodeById.get(edge.target);
    if (source) source.degree = (source.degree ?? 0) + 1;
    if (target) target.degree = (target.degree ?? 0) + 1;
  }

  const symbolColorByCommunity = new Map<string, string>();
  for (const node of symbolNodes) {
    if (node.community && node.color)
      symbolColorByCommunity.set(node.community, node.color);
  }

  const filesByCommunityName = new Map<string, CodeMapNode[]>();
  for (const node of fileNodes) {
    const community = node.community || "root";
    const bucket = filesByCommunityName.get(community) ?? [];
    bucket.push(node);
    filesByCommunityName.set(community, bucket);
  }

  const communityNodes = [...filesByCommunityName.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([community, files]) => ({
      id: `community::${community}`,
      label: community,
      qualified_name: community,
      path: "",
      kind: "community",
      language: "",
      line: 0,
      end_line: 0,
      degree: 0,
      node_type: "community" as const,
      file_type: "community",
      community,
      color:
        symbolColorByCommunity.get(community) || files[0]?.color || "#737373",
      aggregate_count: files.length,
    }));

  const filesByCommunity = new Map<string, CodeMapNode[]>();
  for (const community of communityNodes) {
    filesByCommunity.set(
      community.id,
      [...(filesByCommunityName.get(community.community || "root") ?? [])].sort(
        compareNodes
      )
    );
  }

  const symbolsByFile = new Map<string, CodeMapNode[]>();
  for (const symbol of symbolNodes) {
    const file = fileByPath.get(symbol.path);
    if (!file) continue;
    const bucket = symbolsByFile.get(file.id) ?? [];
    bucket.push(symbol);
    symbolsByFile.set(file.id, bucket);
  }
  for (const bucket of symbolsByFile.values()) bucket.sort(compareNodes);

  const communityNodeByName = new Map(
    communityNodes.map((node) => [node.community || "root", node])
  );
  const communityEdges = new Map<string, CodeMapEdge>();
  for (const edge of fileEdges.values()) {
    const sourceFile = fileNodeById.get(edge.source);
    const targetFile = fileNodeById.get(edge.target);
    if (!sourceFile || !targetFile) continue;
    const source = communityNodeByName.get(sourceFile.community || "root");
    const target = communityNodeByName.get(targetFile.community || "root");
    if (!source || !target) continue;
    aggregateEdge(
      communityEdges,
      source.id,
      target.id,
      "community-call",
      edge.weight ?? 1
    );
  }

  const communityById = new Map(communityNodes.map((node) => [node.id, node]));
  for (const edge of communityEdges.values()) {
    const source = communityById.get(edge.source);
    const target = communityById.get(edge.target);
    if (source) source.degree = (source.degree ?? 0) + 1;
    if (target) target.degree = (target.degree ?? 0) + 1;
  }

  return {
    communityNodes,
    fileNodes,
    symbolNodes,
    communityEdges: byWeight(communityEdges.values()),
    fileEdges: byWeight(fileEdges.values()),
    sourceEdges: edges,
    filesByCommunity,
    symbolsByFile,
    fileByPath,
    nodeById,
  };
}

export function buildCodeMapView(
  hierarchy: CodeMapHierarchy,
  detailStep: number,
  expansion: CodeMapExpansion,
  forcedNodeIds: Iterable<string> = [],
  focusNodeId: string | null = null,
  showAll = false
): CodeMapView {
  const step = Math.max(
    0,
    Math.min(FILES_PER_COMMUNITY.length - 1, detailStep)
  );
  const visible = new Map(
    hierarchy.communityNodes.map((node) => [node.id, node])
  );

  if (showAll) {
    for (const node of hierarchy.fileNodes) visible.set(node.id, node);
    for (const node of hierarchy.symbolNodes) visible.set(node.id, node);
  } else {
    const fileLimit = FILES_PER_COMMUNITY[step];
    for (const communityId of expansion.communityIds) {
      const files = hierarchy.filesByCommunity.get(communityId) ?? [];
      for (const file of files.slice(0, fileLimit)) visible.set(file.id, file);
    }

    const symbolLimit = SYMBOLS_PER_FILE[step];
    if (symbolLimit) {
      for (const fileId of expansion.fileIds) {
        const file = hierarchy.nodeById.get(fileId);
        if (file?.node_type === "file") visible.set(file.id, file);
        const symbols = hierarchy.symbolsByFile.get(fileId) ?? [];
        for (const symbol of symbols.slice(0, symbolLimit))
          visible.set(symbol.id, symbol);
      }
    }
  }

  const forceNode = (nodeId: string) => {
    const node = hierarchy.nodeById.get(nodeId);
    if (!node) return;
    if (node.node_type === "symbol") {
      const parent = hierarchy.fileByPath.get(node.path);
      if (parent) visible.set(parent.id, parent);
    }
    visible.set(node.id, node);
  };
  for (const nodeId of forcedNodeIds) forceNode(nodeId);

  if (focusNodeId) {
    forceNode(focusNodeId);
    for (const edge of hierarchy.sourceEdges) {
      if (edge.source !== focusNodeId && edge.target !== focusNodeId) continue;
      forceNode(edge.source);
      forceNode(edge.target);
    }
  }

  const visibleIds = new Set(visible.keys());
  const visibleFiles = [...visible.values()].filter(
    (node) => node.node_type === "file"
  );
  const visibleSymbols = [...visible.values()].filter(
    (node) => node.node_type === "symbol"
  );
  const detail: CodeMapDetail = showAll
    ? "full"
    : visibleSymbols.length
      ? "symbols"
      : visibleFiles.length
        ? "files"
        : "overview";

  const collected = new Map<string, CodeMapEdge>();
  const add = (edge: CodeMapEdge) => {
    if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) return;
    if (!collected.has(edge.id)) collected.set(edge.id, edge);
  };

  if (focusNodeId) {
    for (const edge of hierarchy.sourceEdges) {
      if (edge.source === focusNodeId || edge.target === focusNodeId) add(edge);
    }
  }

  const communityEdgeLimit = showAll
    ? 0
    : detail === "overview"
      ? 60
      : detail === "files"
        ? 40
        : 20;
  for (const edge of hierarchy.communityEdges.slice(0, communityEdgeLimit))
    add(edge);

  const membershipLimit = showAll ? 0 : Math.min(180, visibleFiles.length);
  for (const file of visibleFiles.slice(0, membershipLimit)) {
    add({
      id: `community-member::${file.id}`,
      source: `community::${file.community || "root"}`,
      target: file.id,
      kind: "contains",
      depth: 0,
      weight: 1,
    });
  }

  const fileEdgeLimit =
    detail === "full"
      ? 0
      : detail === "symbols"
        ? 220
        : Math.min(700, Math.max(50, Math.round(visibleFiles.length * 0.15)));
  let fileEdgesAdded = 0;
  for (const edge of hierarchy.fileEdges) {
    if (fileEdgesAdded >= fileEdgeLimit) break;
    if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
    add(edge);
    fileEdgesAdded += 1;
  }

  if (visibleSymbols.length && !showAll) {
    const containsLimit = showAll
      ? 2_500
      : Math.min(700, visibleSymbols.length);
    let containsAdded = 0;
    for (const edge of hierarchy.sourceEdges) {
      if (containsAdded >= containsLimit) break;
      if (edge.kind !== "contains") continue;
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target))
        continue;
      add(edge);
      containsAdded += 1;
    }

    const callLimit = showAll
      ? 3_000
      : Math.min(
          1_200,
          Math.max(200, Math.round(visibleSymbols.length * 0.22))
        );
    let callsAdded = 0;
    for (const edge of hierarchy.sourceEdges) {
      if (callsAdded >= callLimit) break;
      if (edge.kind !== "calls") continue;
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target))
        continue;
      add(edge);
      callsAdded += 1;
    }
  }

  return {
    nodes: [...visible.values()],
    edges: [...collected.values()],
    detail,
    hiddenCount:
      hierarchy.fileNodes.length +
      hierarchy.symbolNodes.length -
      visibleFiles.length -
      visibleSymbols.length,
  };
}

export function nextCodeMapDetailStep(current: number, ratio: number): number {
  const last = FILES_PER_COMMUNITY.length - 1;
  let next = Math.max(0, Math.min(last, current));
  while (next < last && ratio < ENTER_RATIOS[next]) next += 1;
  while (next > 0 && ratio > EXIT_RATIOS[next - 1]) next -= 1;
  return next;
}
