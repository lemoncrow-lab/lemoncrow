import type { CodeMapGraph } from "../api";

export interface CodeMapGraphProjectionInput {
  baseGraph: CodeMapGraph;
  groupCount: number;
  fileTypeCount: number;
  selectedGroups: ReadonlySet<string>;
  selectedFileTypes: ReadonlySet<string>;
  selectedLanguage: string;
  activeNodeIds: ReadonlySet<string>;
  focused: boolean;
}

/**
 * Project the generic Code Map graph through user-selected filters.
 *
 * Activity nodes stay visible even when their facet is filtered out so live
 * execution never disappears from the map. Focus graphs are already bounded
 * server-side and therefore bypass full-map facet filtering.
 */
export function projectCodeMapGraph({
  baseGraph,
  groupCount,
  fileTypeCount,
  selectedGroups,
  selectedFileTypes,
  selectedLanguage,
  activeNodeIds,
  focused,
}: CodeMapGraphProjectionInput): CodeMapGraph {
  if (focused) return baseGraph;

  const allGroupsSelected = groupCount === 0 || selectedGroups.size === groupCount;
  const allFileTypesSelected = fileTypeCount === 0 || selectedFileTypes.size === fileTypeCount;
  if (allGroupsSelected && allFileTypesSelected && selectedLanguage === "all") {
    return baseGraph;
  }

  const nodes = baseGraph.nodes.filter((node) => {
    if (activeNodeIds.has(node.id)) return true;
    const communityMatch = groupCount === 0 || selectedGroups.has(node.community || "root");
    const typeMatch = fileTypeCount === 0 || selectedFileTypes.has(node.file_type || "other");
    const languageMatch = selectedLanguage === "all" || node.language === selectedLanguage;
    return communityMatch && typeMatch && languageMatch;
  });
  const ids = new Set(nodes.map((node) => node.id));
  return {
    ...baseGraph,
    nodes,
    edges: baseGraph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)),
  };
}
