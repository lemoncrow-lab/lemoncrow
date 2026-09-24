import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Boxes,
  Braces,
  Check,
  CircleDot,
  Expand,
  FileCode2,
  GitFork,
  GitPullRequest,
  Layers3,
  Loader2,
  Maximize2,
  Network,
  Search,
  X,
} from "lucide-react";
import CodeSourceWorkspace from "../components/code/CodeSourceWorkspace";
import {
  api,
  type CodeMapActivityEvent,
  type CodeMapActivityKind,
  type CodeMapFacet,
  type CodeMapFull,
  type CodeMapGraph,
  type CodeMapNode,
  type CodeMapEditorCapability,
  type CodeMapProject,
  type CodeMapReference,
  type CodeMapReferences,
  type CodeMapSymbol,
  type CodeMapTextMatch,
} from "../api";

const CodeGraph = lazy(() => import("../components/CodeGraph"));
const CodeGraph3D = lazy(() => import("../components/CodeGraph3D"));

const ACTIVITY_TONE: Record<
  CodeMapActivityKind,
  { dot: string; text: string; label: string }
> = {
  search: { dot: "bg-violet-300", text: "text-violet-200", label: "Search" },
  read: { dot: "bg-cyan-300", text: "text-cyan-200", label: "Read" },
  edit: { dot: "bg-amber-300", text: "text-amber-200", label: "Edit" },
  verify: { dot: "bg-emerald-300", text: "text-emerald-200", label: "Verify" },
};

const EMPTY_GRAPH: CodeMapGraph = {
  focus: null,
  nodes: [],
  edges: [],
  truncated: false,
};

type CodeSearchItem =
  | { type: "node"; id: string; node: CodeMapNode }
  | { type: "text"; id: string; match: CodeMapTextMatch };

function relativeTime(value: string): string {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "now";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 10) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function FacetList({
  title,
  facets,
  selected,
  onToggle,
  onAll,
}: {
  title: string;
  facets: CodeMapFacet[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onAll: () => void;
}) {
  const allSelected = facets.length > 0 && selected.size === facets.length;
  return (
    <div className="border-b border-neutral-800 p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[10px] font-semibold uppercase tracking-widest text-neutral-200">
          {title}
        </h3>
        <button
          type="button"
          onClick={onAll}
          className="text-[10px] text-neutral-300 hover:text-white"
        >
          {allSelected ? "Clear" : "All"}
        </button>
      </div>
      <div className="max-h-52 space-y-0.5 overflow-y-auto pr-1">
        {facets.map((facet) => {
          const active = selected.has(facet.id);
          return (
            <button
              key={facet.id}
              type="button"
              onClick={() => onToggle(facet.id)}
              aria-pressed={active}
              className="flex w-full items-center gap-2 px-1.5 py-1.5 text-left text-[11px] text-neutral-300 transition hover:bg-neutral-900 hover:text-white"
            >
              <span
                className={`flex h-3.5 w-3.5 items-center justify-center border ${active ? "border-neutral-500 bg-neutral-700" : "border-neutral-700"}`}
              >
                {active && <Check size={10} />}
              </span>
              <span
                className="h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: facet.color || "#737373" }}
              />
              <span className="min-w-0 flex-1 truncate">{facet.label}</span>
              <span className="text-neutral-400">
                {facet.count.toLocaleString()}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CallList({
  title,
  icon,
  nodes,
  onPick,
}: {
  title: string;
  icon: React.ReactNode;
  nodes: CodeMapNode[];
  onPick: (node: CodeMapNode) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
        {icon} {title} · {nodes.length}
      </div>
      <ul className="max-h-40 space-y-0.5 overflow-y-auto pr-1">
        {nodes.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              onClick={() => onPick(node)}
              title={`${node.qualified_name} — ${node.path}:${node.line}`}
              className="flex w-full items-baseline gap-1.5 px-1.5 py-1 text-left text-[11px] text-neutral-300 transition hover:bg-neutral-900 hover:text-white"
            >
              <span className="truncate">{node.label}</span>
              <span className="ml-auto shrink-0 text-[10px] text-neutral-500">
                {node.path.split("/").pop()}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function CodeMap() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedProject = searchParams.get("repo") || undefined;
  const requestedFile = searchParams.get("file") || undefined;
  const requestedSymbol = searchParams.get("symbol") || undefined;
  const requestedView = searchParams.get("view");
  const requestedLineValue = Number(searchParams.get("line"));
  const requestedEndValue = Number(searchParams.get("end"));
  const requestedLine =
    Number.isInteger(requestedLineValue) && requestedLineValue > 0
      ? requestedLineValue
      : null;
  const requestedEnd =
    Number.isInteger(requestedEndValue) && requestedEndValue >= (requestedLine ?? 1)
      ? requestedEndValue
      : requestedLine;
  const [projects, setProjects] = useState<CodeMapProject[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [full, setFull] = useState<CodeMapFull | null>(null);
  const [sourceFiles, setSourceFiles] = useState<CodeMapNode[]>([]);
  const [selectedFileSymbols, setSelectedFileSymbols] = useState<CodeMapNode[]>([]);
  const [focusGraph, setFocusGraph] = useState<CodeMapGraph>(EMPTY_GRAPH);
  const [surfaceMode, setSurfaceMode] = useState<"source" | "map">(
    requestedView === "map" ? "map" : "source"
  );
  const [viewMode, setViewMode] = useState<"full" | "focus">("full");
  const [dimension, setDimension] = useState<"2d" | "3d">("2d");
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [selectedFileTypes, setSelectedFileTypes] = useState<Set<string>>(
    new Set()
  );
  const [selectedLanguage, setSelectedLanguage] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CodeMapSymbol | null>(null);
  const [references, setReferences] = useState<CodeMapReferences | null>(null);
  const [referencesLoading, setReferencesLoading] = useState(false);
  const [referencesError, setReferencesError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CodeMapNode[]>([]);
  const [textResults, setTextResults] = useState<CodeMapTextMatch[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeSearchIndex, setActiveSearchIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [editorCapability, setEditorCapability] = useState<CodeMapEditorCapability | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [expandingId, setExpandingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [activityStatus, setActivityStatus] = useState("idle");
  const [activityHost, setActivityHost] = useState<string | null>(null);
  const [activitySessionId, setActivitySessionId] = useState<string | null>(null);
  const [activityHandoffSupported, setActivityHandoffSupported] = useState(false);
  const [events, setEvents] = useState<CodeMapActivityEvent[]>([]);
  const [activityByNode, setActivityByNode] = useState<
    Record<string, CodeMapActivityKind>
  >({});
  const [followNodeId, setFollowNodeId] = useState<string | null>(null);
  const activityCursor = useRef<string | null>(null);
  const activitySession = useRef<string | null>(null);
  const lastActiveNode = useRef<string | null>(null);
  const graphNodes = useRef<Map<string, CodeMapNode>>(new Map());
  const graphNodesByPath = useRef<Map<string, CodeMapNode[]>>(new Map());
  const fileSymbolsCache = useRef<
    Map<string, { editId: string; symbols: CodeMapNode[] }>
  >(new Map());
  const highlightTimers = useRef<number[]>([]);
  const searchInput = useRef<HTMLInputElement | null>(null);

  const requestedProjectEntry = requestedProject
    ? projects.find(
        (project) =>
          project.project_id === requestedProject || project.root === requestedProject
      )
    : undefined;
  const selectedProject =
    requestedProjectEntry ||
    (!requestedProject
      ? projects.find((project) => project.active && project.indexed) ||
        projects.find((project) => project.indexed)
      : undefined);
  const selectedProjectReference =
    selectedProject?.project_id || requestedProject || selectedProject?.root;
  const projectRoot = full?.project.root || selectedProject?.root;
  const projectUrlReference =
    selectedProject?.project_id || selectedProject?.root || requestedProject || projectRoot;
  const baseGraph =
    viewMode === "full" ? (full?.graph ?? EMPTY_GRAPH) : focusGraph;
  const sourceNodes = useMemo(() => {
    const byId = new Map<string, CodeMapNode>();
    for (const node of sourceFiles) byId.set(node.id, node);
    for (const node of full?.graph.nodes ?? []) {
      if (node.node_type !== "file") byId.set(node.id, node);
    }
    for (const node of selectedFileSymbols) byId.set(node.id, node);
    return [...byId.values()];
  }, [full?.graph.nodes, selectedFileSymbols, sourceFiles]);
  const activeIds = useMemo(
    () => new Set(Object.keys(activityByNode)),
    [activityByNode]
  );
  const graph = useMemo<CodeMapGraph>(() => {
    if (viewMode === "focus") return baseGraph;
    const allGroupsSelected =
      !full?.groups.length || selectedGroups.size === full.groups.length;
    const allFileTypesSelected =
      !full?.file_types.length ||
      selectedFileTypes.size === full.file_types.length;
    if (
      allGroupsSelected &&
      allFileTypesSelected &&
      selectedLanguage === "all"
    ) {
      return baseGraph;
    }
    const nodes = baseGraph.nodes.filter((node) => {
      if (activeIds.has(node.id)) return true;
      const communityMatch =
        !full?.groups.length || selectedGroups.has(node.community || "root");
      const typeMatch =
        !full?.file_types.length ||
        selectedFileTypes.has(node.file_type || "other");
      const languageMatch =
        selectedLanguage === "all" || node.language === selectedLanguage;
      return communityMatch && typeMatch && languageMatch;
    });
    const ids = new Set(nodes.map((node) => node.id));
    return {
      ...baseGraph,
      nodes,
      edges: baseGraph.edges.filter(
        (edge) => ids.has(edge.source) && ids.has(edge.target)
      ),
    };
  }, [
    activeIds,
    baseGraph,
    full?.groups.length,
    full?.file_types.length,
    selectedGroups,
    selectedFileTypes,
    selectedLanguage,
    viewMode,
  ]);

  const selectedNode = useMemo(
    () =>
      baseGraph.nodes.find((node) => node.id === selectedId) ??
      sourceNodes.find((node) => node.id === selectedId) ??
      null,
    [baseGraph.nodes, selectedId, sourceNodes]
  );

  const fileSearchResults = useMemo(() => {
    const clean = query.trim().toLowerCase();
    if (clean.length < 2) return [];
    return sourceFiles
      .filter((node) =>
        `${node.label} ${node.path}`.toLowerCase().includes(clean)
      )
      .slice(0, 6);
  }, [query, sourceFiles]);

  const searchResults = useMemo<CodeSearchItem[]>(() => {
    const seen = new Set<string>();
    const nodes = [...fileSearchResults, ...results]
      .filter((node) => {
        if (seen.has(node.id)) return false;
        seen.add(node.id);
        return true;
      })
      .map((node) => ({ type: "node" as const, id: node.id, node }));
    const text = textResults.map((match) => ({
      type: "text" as const,
      id: match.id,
      match,
    }));
    return [...nodes, ...text].slice(0, 30);
  }, [fileSearchResults, results, textResults]);

  useEffect(() => {
    setActiveSearchIndex(0);
  }, [query, searchResults.length]);

  useEffect(() => {
    let cancelled = false;
    void api
      .codeMapEditorCapability()
      .then((capability) => {
        if (!cancelled) setEditorCapability(capability);
      })
      .catch(() => {
        if (!cancelled) setEditorCapability(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchInput.current?.focus();
        searchInput.current?.select();
        return;
      }
      if (event.key === "Escape" && document.activeElement === searchInput.current) {
        setQuery("");
        searchInput.current?.blur();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    setSurfaceMode(requestedView === "map" ? "map" : "source");
  }, [requestedView]);

  useEffect(() => {
    if (!full) return;
    const requestedNode = requestedSymbol
      ? full.graph.nodes.find((node) => node.id === requestedSymbol)
      : requestedFile
        ? sourceFiles.find((node) => node.path === requestedFile) ??
          full.graph.nodes.find((node) => node.path === requestedFile)
        : null;
    if (requestedNode) {
      setSelectedId((current) => {
        if (current === requestedNode.id) return current;
        setDetail(null);
        return requestedNode.id;
      });
      return;
    }
    if (!requestedSymbol || !projectRoot) return;

    let cancelled = false;
    void api
      .codeMapNeighborhood(requestedSymbol, projectRoot, 1, 160)
      .then((incoming) => {
        if (cancelled) return;
        const node = incoming.nodes.find(
          (candidate) => candidate.id === requestedSymbol
        );
        if (!node) return;
        setFocusGraph(incoming);
        setViewMode("focus");
        setSelectedId(node.id);
        setDetail(null);
      })
      .catch(() => {
        // A stale/invalid deep link should not make the whole Code page fail.
      });
    return () => {
      cancelled = true;
    };
  }, [full, projectRoot, requestedFile, requestedSymbol, sourceFiles]);

  useEffect(() => {
    void api
      .codeMapProjects()
      .then((payload) => {
        setProjects(payload.projects);
        setProjectsError(null);
      })
      .catch(() => {
        setProjects([]);
        setProjectsError(
          "Code index API unavailable. Restart the dashboard service, then reopen Code."
        );
      })
      .finally(() => setProjectsLoaded(true));
  }, []);

  useEffect(() => {
    if (
      !requestedProject ||
      !selectedProject?.project_id ||
      requestedProject === selectedProject.project_id
    ) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.set("repo", selectedProject.project_id);
    setSearchParams(next, { replace: true });
  }, [requestedProject, searchParams, selectedProject?.project_id, setSearchParams]);

  useEffect(() => {
    if (requestedProject || !projectsLoaded || selectedProjectReference) return;
    setLoading(false);
    setFull(null);
    setSourceFiles([]);
    setSelectedFileSymbols([]);
    setError(
      projectsError ||
        "No indexed project is available in this dashboard session."
    );
  }, [projectsError, projectsLoaded, requestedProject, selectedProjectReference]);

  useEffect(() => {
    if (!selectedProjectReference) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    setSelectedId(null);
    setSourceFiles([]);
    setSelectedFileSymbols([]);
    setViewMode("full");
    activityCursor.current = null;
    activitySession.current = null;
    setActivityHost(null);
    setActivitySessionId(null);
    setActivityHandoffSupported(false);
    setEvents([]);
    void Promise.all([
      api.codeMapFull(selectedProjectReference),
      api.codeMapFiles(selectedProjectReference).catch(() => null),
    ])
      .then(([payload, filePayload]) => {
        if (cancelled) return;
        setFull(payload);
        setSourceFiles(
          filePayload?.files ??
            payload.graph.nodes.filter((node) => node.node_type === "file")
        );
        setFocusGraph(EMPTY_GRAPH);
        setSelectedGroups(new Set(payload.groups.map((facet) => facet.id)));
        setSelectedFileTypes(
          new Set(payload.file_types.map((facet) => facet.id))
        );
      })
      .catch((caught: unknown) => {
        if (!cancelled)
          setError(
            caught instanceof Error
              ? caught.message
              : "Could not load the code map."
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedProjectReference]);

  useEffect(() => {
    const path = selectedNode?.path;
    if (surfaceMode !== "source" || !projectRoot || !path) {
      setSelectedFileSymbols([]);
      return;
    }
    const editId =
      [...events].reverse().find((event) => event.kind === "edit" && event.path === path)
        ?.id ?? "";
    const cacheKey = `${projectRoot}\n${path}`;
    const cached = fileSymbolsCache.current.get(cacheKey);
    if (cached?.editId === editId) {
      setSelectedFileSymbols(cached.symbols);
      return;
    }

    let cancelled = false;
    setSelectedFileSymbols([]);
    void api
      .codeMapFileSymbols(path, projectRoot)
      .then((payload) => {
        if (cancelled) return;
        const cache = fileSymbolsCache.current;
        cache.set(cacheKey, { editId, symbols: payload.symbols });
        if (cache.size > 80) {
          const oldest = cache.keys().next().value;
          if (oldest) cache.delete(oldest);
        }
        setSelectedFileSymbols(payload.symbols);
      })
      .catch(() => {
        if (!cancelled) setSelectedFileSymbols([]);
      });
    return () => {
      cancelled = true;
    };
  }, [events, projectRoot, selectedNode?.path, surfaceMode]);

  useEffect(() => {
    const allNodes = [...sourceNodes, ...focusGraph.nodes];
    graphNodes.current = new Map(allNodes.map((node) => [node.id, node]));
    const byPath = new Map<string, CodeMapNode[]>();
    for (const node of allNodes) {
      if (!node.path) continue;
      const bucket = byPath.get(node.path);
      if (bucket) bucket.push(node);
      else byPath.set(node.path, [node]);
    }
    graphNodesByPath.current = byPath;
  }, [focusGraph.nodes, sourceNodes]);

  useEffect(() => {
    const clean = query.trim();
    if (clean.length < 2 || !projectRoot) {
      setResults([]);
      setTextResults([]);
      setSearchError(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    setSearchError(null);
    const timer = window.setTimeout(() => {
      void api
        .codeMapSearch(clean, projectRoot, 12)
        .then((payload) => {
          if (!cancelled) {
            setResults(payload.results);
            setTextResults(payload.text_results);
            setSearchError(null);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setResults([]);
            setTextResults([]);
            setSearchError("Code search is temporarily unavailable.");
          }
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [projectRoot, query]);

  const updateNodeLocation = useCallback(
    (
      node: CodeMapNode,
      view: "source" | "map",
      replace = false,
      range?: { start: number; end?: number } | null
    ) => {
      const next = new URLSearchParams(searchParams);
      if (projectUrlReference) next.set("repo", projectUrlReference);
      next.set("file", node.path);
      if (node.node_type === "file") next.delete("symbol");
      else next.set("symbol", node.id);
      if (range && range.start > 0) {
        next.set("line", String(range.start));
        const end = range.end ?? range.start;
        if (end > range.start) next.set("end", String(end));
        else next.delete("end");
      } else {
        next.delete("line");
        next.delete("end");
      }
      next.set("view", view);
      setSearchParams(next, { replace });
    },
    [projectUrlReference, searchParams, setSearchParams]
  );

  const changeSurface = useCallback(
    (view: "source" | "map") => {
      setSurfaceMode(view);
      if (selectedNode) {
        updateNodeLocation(selectedNode, view, true);
        return;
      }
      const next = new URLSearchParams(searchParams);
      if (projectUrlReference) next.set("repo", projectUrlReference);
      next.set("view", view);
      setSearchParams(next, { replace: true });
    },
    [projectUrlReference, searchParams, selectedNode, setSearchParams, updateNodeLocation]
  );

  const selectNode = useCallback(
    (nodeId: string | null) => {
      setSelectedId((current) => {
        if (current !== nodeId) setDetail(null);
        return nodeId;
      });
      if (!nodeId) return;
      const node = graphNodes.current.get(nodeId);
      if (node) updateNodeLocation(node, surfaceMode, false);
    },
    [surfaceMode, updateNodeLocation]
  );

  useEffect(() => {
    if (
      surfaceMode !== "map" ||
      !selectedNode ||
      selectedNode.node_type === "file" ||
      !projectRoot
    ) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    void api
      .codeMapSymbol(selectedNode.id, projectRoot)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectRoot, selectedNode, surfaceMode]);

  useEffect(() => {
    if (
      surfaceMode !== "source" ||
      !selectedNode ||
      selectedNode.node_type === "file" ||
      !projectRoot
    ) {
      setReferences(null);
      setReferencesLoading(false);
      setReferencesError(null);
      return;
    }
    let cancelled = false;
    setReferences(null);
    setReferencesLoading(true);
    setReferencesError(null);
    void api
      .codeMapReferences(selectedNode.id, projectRoot, 60)
      .then((payload) => {
        if (!cancelled) setReferences(payload);
      })
      .catch(() => {
        if (!cancelled) {
          setReferences(null);
          setReferencesError("Could not load references for this symbol.");
        }
      })
      .finally(() => {
        if (!cancelled) setReferencesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectRoot, selectedNode, surfaceMode]);

  const focusNode = useCallback(
    (
      nodeId: string,
      locationView: "source" | "map" = "map",
      range?: { start: number; end?: number } | null,
      replaceLocation = false
    ) => {
      const node = graphNodes.current.get(nodeId);
      setSelectedId(nodeId);
      if (node) updateNodeLocation(node, locationView, replaceLocation, range);
      if (!projectRoot || node?.node_type === "file") return;
      setExpandingId(nodeId);
      void api
        .codeMapNeighborhood(nodeId, projectRoot, 1, 160)
        .then((incoming) => {
          setFocusGraph(incoming);
          setViewMode("focus");
          setFollowNodeId(nodeId);
        })
        .catch((caught: unknown) =>
          setError(
            caught instanceof Error
              ? caught.message
              : "Could not focus this symbol."
          )
        )
        .finally(() => setExpandingId(null));
    },
    [projectRoot, updateNodeLocation]
  );

  const openSearchResult = useCallback(
    (
      node: CodeMapNode,
      range?: { start: number; end?: number } | null
    ) => {
      setQuery("");
      setResults([]);
      setTextResults([]);
      setSurfaceMode("source");
      setSelectedId(node.id);
      setDetail(null);
      updateNodeLocation(node, "source", false, range);
      if (full?.graph.nodes.some((candidate) => candidate.id === node.id)) {
        setViewMode("full");
        setFollowNodeId(node.id);
      } else if (node.node_type !== "file") {
        focusNode(node.id, "source", range, true);
      }
    },
    [focusNode, full?.graph.nodes, updateNodeLocation]
  );

  const openTextMatch = useCallback(
    (match: CodeMapTextMatch) => {
      const pathTargets = graphNodesByPath.current.get(match.path) ?? [];
      const containingSymbol = pathTargets
        .filter(
          (node) =>
            node.node_type !== "file" &&
            node.line <= match.line &&
            node.end_line >= match.line
        )
        .sort((a, b) => a.end_line - a.line - (b.end_line - b.line))[0];
      const fileTarget = pathTargets.find((node) => node.node_type === "file");
      const target = containingSymbol ?? fileTarget ?? pathTargets[0];
      if (target) {
        openSearchResult(target, { start: match.line, end: match.line });
      }
    },
    [openSearchResult]
  );

  const openSearchItem = useCallback(
    (item: CodeSearchItem) => {
      if (item.type === "text") openTextMatch(item.match);
      else openSearchResult(item.node);
    },
    [openSearchResult, openTextMatch]
  );

  const askAgent = useCallback(
    (message: string) => {
      if (!selectedNode || !projectUrlReference || !activitySessionId) {
        return Promise.reject(
          new Error("No exact coding-agent session is available for this project.")
        );
      }
      const line =
        requestedLine ??
        (selectedNode.node_type !== "file" ? selectedNode.line : undefined);
      const endLine = requestedLine
        ? requestedEnd ?? requestedLine
        : selectedNode.node_type !== "file"
          ? selectedNode.end_line
          : undefined;
      return api.codeMapAgentHandoff({
        project_root: projectUrlReference,
        expected_session_id: activitySessionId,
        path: selectedNode.path,
        line: line ?? undefined,
        end_line: endLine ?? undefined,
        symbol:
          selectedNode.node_type !== "file"
            ? selectedNode.qualified_name || selectedNode.label
            : undefined,
        message,
      });
    },
    [
      activitySessionId,
      projectUrlReference,
      requestedEnd,
      requestedLine,
      selectedNode,
    ]
  );

  const openLine = useCallback(
    (line: number) => {
      if (!selectedNode || line < 1) return;
      const pathTargets = graphNodesByPath.current.get(selectedNode.path) ?? [];
      const containingSymbol = pathTargets
        .filter(
          (node) =>
            node.node_type !== "file" &&
            node.line <= line &&
            node.end_line >= line
        )
        .sort((a, b) => a.end_line - a.line - (b.end_line - b.line))[0];
      const fileTarget = pathTargets.find((node) => node.node_type === "file");
      const target = containingSymbol ?? fileTarget ?? selectedNode;
      openSearchResult(target, { start: line, end: line });
    },
    [openSearchResult, selectedNode]
  );

  const openReference = useCallback(
    (reference: CodeMapReference) => {
      const pathTargets = graphNodesByPath.current.get(reference.path) ?? [];
      const containingSymbol = pathTargets
        .filter(
          (node) =>
            node.node_type !== "file" &&
            node.line <= reference.line &&
            node.end_line >= reference.line
        )
        .sort(
          (a, b) =>
            a.end_line - a.line - (b.end_line - b.line)
        )[0];
      const fileTarget = pathTargets.find((node) => node.node_type === "file");
      const target = containingSymbol ?? fileTarget ?? pathTargets[0];
      if (target) {
        openSearchResult(target, {
          start: reference.line,
          end: Math.max(reference.line, reference.end_line),
        });
      }
    },
    [openSearchResult]
  );

  const openActivity = useCallback(
    (event: CodeMapActivityEvent) => {
      const symbolTarget = (event.symbol_ids ?? [])
        .map((id) => graphNodes.current.get(id))
        .find((node): node is CodeMapNode => !!node);
      const pathTargets = event.path
        ? graphNodesByPath.current.get(event.path) ?? []
        : [];
      const lineTarget =
        event.line !== undefined
          ? pathTargets.find(
              (node) =>
                node.node_type !== "file" &&
                node.line <= event.line! &&
                node.end_line >= event.line!
            )
          : undefined;
      const fileTarget = pathTargets.find((node) => node.node_type === "file");
      const target = symbolTarget ?? lineTarget ?? fileTarget ?? pathTargets[0];
      if (target) {
        openSearchResult(
          target,
          event.line ? { start: event.line, end: event.line } : null
        );
      }
    },
    [openSearchResult]
  );

  useEffect(() => {
    if (!projectRoot || !liveEnabled) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const payload = await api.codeMapActivity(
          projectRoot,
          activityCursor.current,
          60
        );
        if (cancelled) return;
        if (
          activitySession.current &&
          payload.session_id &&
          activitySession.current !== payload.session_id
        ) {
          activityCursor.current = null;
          setEvents([]);
        }
        activitySession.current = payload.session_id;
        activityCursor.current = payload.cursor ?? activityCursor.current;
        setActivitySessionId(payload.session_id);
        setActivityHost(payload.host ?? null);
        setActivityHandoffSupported(Boolean(payload.handoff_supported));
        setActivityStatus(payload.status);
        if (!payload.events.length) return;
        setEvents((current) => {
          const merged = new Map(current.map((event) => [event.id, event]));
          for (const event of payload.events) merged.set(event.id, event);
          return [...merged.values()].slice(-18);
        });

        const highlights: Record<string, CodeMapActivityKind> = {};
        let newestTarget: string | null = null;
        for (const event of payload.events) {
          let targets = (event.symbol_ids ?? []).filter((id) =>
            graphNodes.current.has(id)
          );
          if (!targets.length && event.path) {
            targets = (graphNodesByPath.current.get(event.path) ?? [])
              .slice(0, 6)
              .map((node) => node.id);
          }
          if (!targets.length && event.query) {
            const needle = event.query.toLowerCase();
            const matched: string[] = [];
            for (const node of graphNodes.current.values()) {
              if (
                `${node.label} ${node.qualified_name}`
                  .toLowerCase()
                  .includes(needle)
              ) {
                matched.push(node.id);
                if (matched.length >= 6) break;
              }
            }
            targets = matched;
          }
          if (event.kind === "verify" && !targets.length) {
            const fallback =
              lastActiveNode.current || selectedId || baseGraph.focus;
            if (fallback) targets = [fallback];
          }
          for (const id of targets) highlights[id] = event.kind;
          if (targets[0]) newestTarget = targets[0];
        }
        if (newestTarget) {
          lastActiveNode.current = newestTarget;
          setFollowNodeId(newestTarget);
        }
        if (Object.keys(highlights).length) {
          setActivityByNode((current) => ({ ...current, ...highlights }));
          const timer = window.setTimeout(() => {
            setActivityByNode((current) => {
              const next = { ...current };
              for (const id of Object.keys(highlights)) delete next[id];
              return next;
            });
          }, 3600);
          highlightTimers.current.push(timer);
        }
      } catch {
        if (!cancelled) setActivityStatus("offline");
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [baseGraph.focus, liveEnabled, projectRoot, selectedId]);

  useEffect(() => {
    if (!liveEnabled) {
      setActivityByNode({});
      setFollowNodeId(null);
    }
  }, [liveEnabled]);

  useEffect(
    () => () => {
      for (const timer of highlightTimers.current) window.clearTimeout(timer);
    },
    []
  );

  const openEditor = useCallback(() => {
    if (!selectedNode || !projectUrlReference) {
      return Promise.reject(new Error("Open a file or symbol before launching the editor."));
    }
    const line =
      requestedLine ??
      (selectedNode.node_type !== "file" ? selectedNode.line : 1);
    return api.codeMapOpenEditor(
      projectUrlReference,
      selectedNode.path,
      line,
      1
    );
  }, [projectUrlReference, requestedLine, selectedNode]);

  const openReview = useCallback(async () => {
    if (!projectUrlReference || reviewBusy) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const review = await api.codeMapReview(projectUrlReference);
      navigate(review.review_path);
    } catch (caught: unknown) {
      setReviewError(
        caught instanceof Error
          ? caught.message
          : "Could not prepare the durable review."
      );
    } finally {
      setReviewBusy(false);
    }
  }, [navigate, projectUrlReference, reviewBusy]);

  const toggleFacet = (
    setter: React.Dispatch<React.SetStateAction<Set<string>>>,
    id: string
  ) => {
    setter((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const toggleAll = (
    setter: React.Dispatch<React.SetStateAction<Set<string>>>,
    facets: CodeMapFacet[],
    current: Set<string>
  ) => {
    setter(
      current.size === facets.length
        ? new Set()
        : new Set(facets.map((facet) => facet.id))
    );
  };
  const { incomingCount, outgoingCount, callers, callees } = useMemo(() => {
    const empty = {
      incomingCount: 0,
      outgoingCount: 0,
      callers: [] as CodeMapNode[],
      callees: [] as CodeMapNode[],
    };
    if (!selectedId) return empty;
    const byId = new Map(baseGraph.nodes.map((node) => [node.id, node]));
    const callerMap = new Map<string, CodeMapNode>();
    const calleeMap = new Map<string, CodeMapNode>();
    for (const edge of baseGraph.edges) {
      if (edge.kind !== "calls") continue;
      if (edge.target === selectedId) {
        const node = byId.get(edge.source);
        if (node) callerMap.set(node.id, node);
      }
      if (edge.source === selectedId) {
        const node = byId.get(edge.target);
        if (node) calleeMap.set(node.id, node);
      }
    }
    const byLabel = (a: CodeMapNode, b: CodeMapNode) =>
      a.label.localeCompare(b.label);
    const callerList = [...callerMap.values()].sort(byLabel);
    const calleeList = [...calleeMap.values()].sort(byLabel);
    return {
      incomingCount: callerList.length,
      outgoingCount: calleeList.length,
      callers: callerList,
      callees: calleeList,
    };
  }, [baseGraph.edges, baseGraph.nodes, selectedId]);
  const graphCallCount = useMemo(
    () =>
      graph.edges.reduce(
        (total, edge) => total + (edge.kind === "calls" ? 1 : 0),
        0
      ),
    [graph.edges]
  );

  return (
    <section className="min-h-[calc(100vh-150px)] bg-surface-sunken text-neutral-200">
      <div className="border-b border-neutral-800 bg-neutral-950/75 px-5 py-3 lg:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-xs font-medium text-neutral-300">
            {full?.project.label ?? "local index"}
          </span>
          <span className="text-neutral-700">·</span>
          <span className="text-[10px] text-neutral-500">
            {(full?.total_symbols ?? 0).toLocaleString()} symbols
          </span>
          <span className="text-neutral-700">·</span>
          <span className="text-[10px] text-neutral-500">
            {(full?.total_files ?? 0).toLocaleString()} files
          </span>
          {surfaceMode === "map" && (
            <>
              <span className="text-neutral-700">·</span>
              <span className="text-[10px] text-neutral-500">
                {graph.nodes.length.toLocaleString()} nodes
              </span>
              <span className="text-neutral-700">·</span>
              <span className="text-[10px] text-neutral-500">
                {graphCallCount.toLocaleString()} calls
              </span>
            </>
          )}
          {full?.truncated && (
            <span className="text-[10px] text-amber-300">bounded</span>
          )}
          <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
            {projects.length > 1 && (
              <select
                className="h-8 max-w-56 border border-neutral-700 bg-neutral-900 px-2 text-[10px] text-neutral-200"
                value={selectedProject?.project_id || selectedProject?.root || ""}
                onChange={(event) =>
                  setSearchParams({ repo: event.target.value })
                }
                aria-label="Indexed project"
              >
                {projects.map((project) => (
                  <option
                    key={project.project_id || project.root}
                    value={project.project_id || project.root}
                  >
                    {project.label}
                    {project.active ? " · active" : ""}
                  </option>
                ))}
              </select>
            )}
            <div className="relative w-full sm:w-[320px]">
              <Search
                size={13}
                className="pointer-events-none absolute left-2.5 top-2.5 text-neutral-500"
              />
              <input
                ref={searchInput}
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (!searchResults.length) return;
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setActiveSearchIndex((index) =>
                      Math.min(index + 1, searchResults.length - 1)
                    );
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setActiveSearchIndex((index) => Math.max(index - 1, 0));
                  } else if (event.key === "Enter") {
                    event.preventDefault();
                    const result = searchResults[activeSearchIndex];
                    if (result) openSearchItem(result);
                  }
                }}
                placeholder="Find files, symbols, or text…"
                className="h-8 w-full border border-neutral-700 bg-neutral-900 pl-8 pr-14 text-[11px] text-neutral-100 outline-none placeholder:text-neutral-600 focus:border-brand-500/60"
                aria-label="Find code"
                aria-controls={searchResults.length ? "code-search-results" : undefined}
              />
              {!query && (
                <span className="pointer-events-none absolute right-2 top-2 border border-neutral-800 px-1 text-[9px] text-neutral-600">
                  {navigator.platform?.toLowerCase().includes("mac") ? "⌘K" : "Ctrl K"}
                </span>
              )}
              {searching && (
                <Loader2
                  size={12}
                  className="absolute right-2.5 top-2.5 animate-spin text-brand-300"
                />
              )}
              {query.trim().length >= 2 && !searching && searchError && (
                <div className="absolute right-0 top-9 z-30 w-full border border-red-900/70 bg-neutral-950 px-3 py-3 text-[11px] text-red-300 shadow-2xl">
                  {searchError}
                </div>
              )}
              {query.trim().length >= 2 &&
                !searching &&
                !searchError &&
                searchResults.length === 0 && (
                  <div className="absolute right-0 top-9 z-30 w-full border border-neutral-700 bg-neutral-950 px-3 py-3 text-[11px] text-neutral-500 shadow-2xl">
                    No matching files, symbols, or text.
                  </div>
                )}
              {searchResults.length > 0 && (
                <div
                  id="code-search-results"
                  className="absolute right-0 top-9 z-30 max-h-96 w-full overflow-y-auto border border-neutral-700 bg-neutral-950 shadow-2xl"
                >
                  {searchResults.map((result, index) => {
                    const label =
                      result.type === "text"
                        ? result.match.text || query.trim()
                        : result.node.label;
                    const path =
                      result.type === "text" ? result.match.path : result.node.path;
                    const line =
                      result.type === "text" ? result.match.line : result.node.line;
                    const badge =
                      result.type === "text"
                        ? "text"
                        : result.node.node_type === "file"
                          ? "file"
                          : result.node.kind;
                    return (
                      <button
                        key={result.id}
                        type="button"
                        className={`block w-full border-b border-neutral-800 px-3 py-2 text-left transition last:border-0 hover:bg-neutral-900 ${
                          index === activeSearchIndex ? "bg-neutral-900" : ""
                        }`}
                        onMouseEnter={() => setActiveSearchIndex(index)}
                        onClick={() => openSearchItem(result)}
                        aria-label={`${label} ${path}`}
                      >
                        <span className="flex items-center gap-2">
                          <span className="min-w-0 flex-1 truncate text-xs text-neutral-100">
                            {label}
                          </span>
                          <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wider text-neutral-600">
                            {badge}
                          </span>
                        </span>
                        <span className="mt-0.5 block truncate text-[10px] text-neutral-500">
                          {path}
                          {result.type === "text" || result.node.node_type !== "file"
                            ? `:${line}`
                            : ""}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-end gap-2 text-[10px]">
          <button
            type="button"
            onClick={() => void openReview()}
            disabled={!projectUrlReference || reviewBusy}
            className="inline-flex items-center gap-1.5 border border-neutral-700 bg-neutral-900/70 px-2 py-1 text-neutral-300 hover:border-neutral-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {reviewBusy ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <GitPullRequest size={11} />
            )}
            {reviewBusy ? "Preparing review…" : "Review changes"}
          </button>
          {reviewError && (
            <span className="max-w-72 truncate text-[10px] text-red-300/80" title={reviewError}>
              {reviewError}
            </span>
          )}
          <div className="flex border border-neutral-700 bg-neutral-900/70">
            <button
              type="button"
              onClick={() => changeSurface("source")}
              aria-pressed={surfaceMode === "source"}
              className={`inline-flex items-center gap-1.5 px-2 py-1 ${surfaceMode === "source" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
            >
              <FileCode2 size={11} /> Source
            </button>
            <button
              type="button"
              onClick={() => changeSurface("map")}
              aria-pressed={surfaceMode === "map"}
              className={`inline-flex items-center gap-1.5 border-l border-neutral-700 px-2 py-1 ${surfaceMode === "map" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
            >
              <Network size={11} /> Map
            </button>
          </div>
          {surfaceMode === "map" && (
            <>
              <div className="flex border border-neutral-700 bg-neutral-900/70">
                <button
                  type="button"
                  onClick={() => setViewMode("full")}
                  aria-pressed={viewMode === "full"}
                  className={`inline-flex items-center gap-1.5 px-2 py-1 ${viewMode === "full" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                >
                  <Maximize2 size={11} /> Full
                </button>
                <button
                  type="button"
                  disabled={!focusGraph.nodes.length}
                  onClick={() => setViewMode("focus")}
                  aria-pressed={viewMode === "focus"}
                  className={`inline-flex items-center gap-1.5 border-l border-neutral-700 px-2 py-1 disabled:opacity-40 ${viewMode === "focus" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                >
                  <Layers3 size={11} /> Focus
                </button>
              </div>
              <div className="flex border border-neutral-700 bg-neutral-900/70">
                <button
                  type="button"
                  onClick={() => setDimension("2d")}
                  aria-pressed={dimension === "2d"}
                  className={`inline-flex items-center px-2 py-1 ${dimension === "2d" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                >
                  2D
                </button>
                <button
                  type="button"
                  onClick={() => setDimension("3d")}
                  aria-pressed={dimension === "3d"}
                  className={`inline-flex items-center gap-1.5 border-l border-neutral-700 px-2 py-1 ${dimension === "3d" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                >
                  <Boxes size={11} /> 3D
                </button>
              </div>
            </>
          )}
          <button
            type="button"
            onClick={() => setLiveEnabled((enabled) => !enabled)}
            className="inline-flex items-center gap-1.5 border border-neutral-700 bg-neutral-900/70 px-2 py-1 text-neutral-400 hover:text-neutral-200"
            aria-pressed={liveEnabled}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${liveEnabled && activityStatus !== "offline" ? "bg-emerald-400" : "bg-neutral-600"}`}
            />
            Live {liveEnabled ? "on" : "off"}
          </button>
        </div>
      </div>

      {error && (
        <div className="flex items-start justify-between border-b border-red-900/60 bg-red-950/30 px-6 py-3 text-sm text-red-200">
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss error"
          >
            <X size={15} />
          </button>
        </div>
      )}
      {loading ? (
        <div className="flex min-h-[560px] items-center justify-center gap-3 text-sm text-neutral-300">
          <Loader2 className="animate-spin text-brand-300" size={18} /> Loading
          code index…
        </div>
      ) : !full?.graph.nodes.length ? (
        <div className="flex min-h-[560px] items-center justify-center px-6">
          <div className="max-w-lg rounded-sm border border-neutral-800 bg-neutral-950/70 p-6 text-center">
            <Braces className="mx-auto text-brand-300" size={28} />
            <h3 className="mt-4 text-base font-semibold text-neutral-100">
              No indexed symbols yet
            </h3>
            <p className="mt-2 text-sm leading-6 text-neutral-300">
              Build this repository&apos;s local graph, then return here.
            </p>
            <code className="mt-5 inline-block border border-neutral-700 bg-black px-4 py-2 text-sm text-emerald-300">
              lc code index
            </code>
          </div>
        </div>
      ) : surfaceMode === "source" ? (
        <CodeSourceWorkspace
          projectRoot={projectRoot ?? ""}
          allNodes={sourceNodes}
          selectedNode={selectedNode}
          callers={callers}
          callees={callees}
          references={references}
          referencesLoading={referencesLoading}
          referencesError={referencesError}
          locationRange={
            requestedLine
              ? ([requestedLine, requestedEnd ?? requestedLine] as const)
              : null
          }
          events={events}
          liveEnabled={liveEnabled}
          agentHost={liveEnabled ? activityHost : null}
          agentSessionId={liveEnabled ? activitySessionId : null}
          agentHandoffSupported={liveEnabled && activityHandoffSupported}
          editorCapability={editorCapability}
          onSelect={openSearchResult}
          onOpenMap={(node) => {
            setSurfaceMode("map");
            focusNode(node.id, "map");
          }}
          onOpenActivity={openActivity}
          onOpenReference={openReference}
          onOpenLine={openLine}
          onOpenEditor={openEditor}
          onAskAgent={askAgent}
        />
      ) : (
        <div className="grid min-h-[650px] grid-cols-1 xl:grid-cols-[270px_minmax(0,1fr)_340px]">
          <aside className="order-2 border-t border-neutral-800 bg-neutral-950/55 xl:order-1 xl:border-r xl:border-t-0">
            <FacetList
              title="Groups"
              facets={full.groups}
              selected={selectedGroups}
              onToggle={(id) => toggleFacet(setSelectedGroups, id)}
              onAll={() =>
                toggleAll(setSelectedGroups, full.groups, selectedGroups)
              }
            />
            <FacetList
              title="File types"
              facets={full.file_types}
              selected={selectedFileTypes}
              onToggle={(id) => toggleFacet(setSelectedFileTypes, id)}
              onAll={() =>
                toggleAll(
                  setSelectedFileTypes,
                  full.file_types,
                  selectedFileTypes
                )
              }
            />
            <div className="border-b border-neutral-800 p-3">
              <label
                className="text-[10px] font-semibold uppercase tracking-widest text-neutral-200"
                htmlFor="map-language"
              >
                Language
              </label>
              <select
                id="map-language"
                value={selectedLanguage}
                onChange={(event) => setSelectedLanguage(event.target.value)}
                className="mt-2 h-9 w-full border border-neutral-700 bg-neutral-900 px-2 text-xs text-neutral-200"
              >
                <option value="all">All languages</option>
                {full.languages.map((language) => (
                  <option key={language.id} value={language.id}>
                    {language.label} · {language.count}
                  </option>
                ))}
              </select>
            </div>
            <div className="border-b border-neutral-800 p-3">
              <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-200">
                <Activity size={13} className="text-emerald-300" /> Live run
              </div>
              <p className="mt-2 text-[11px] leading-5 text-neutral-400">
                Run-ledger events only. No source, diffs, stdout, or stderr.
              </p>
            </div>
            <div className="max-h-72 overflow-y-auto p-2">
              {events.length ? (
                <ol className="space-y-1" aria-label="Recent run activity">
                  {[...events].reverse().map((event) => {
                    const tone = ACTIVITY_TONE[event.kind];
                    const actionable =
                      !!event.path || !!event.symbol_ids?.length;
                    return (
                      <li key={event.id}>
                        <button
                          type="button"
                          disabled={!actionable}
                          onClick={() => openActivity(event)}
                          className="w-full border border-neutral-800 bg-neutral-900/35 p-2 text-left transition enabled:hover:border-neutral-700 enabled:hover:bg-neutral-900/70 disabled:cursor-default"
                        >
                          <div className="flex items-center gap-2">
                            <span
                              className={`h-2 w-2 rounded-full ${tone.dot}`}
                            />
                            <span
                              className={`text-[10px] font-semibold uppercase tracking-wider ${tone.text}`}
                            >
                              {tone.label}
                            </span>
                            <time
                              className="ml-auto text-[10px] text-neutral-400"
                              dateTime={event.at}
                            >
                              {relativeTime(event.at)}
                            </time>
                          </div>
                          <p className="mt-1.5 break-words text-[11px] leading-4 text-neutral-300">
                            {event.label}
                          </p>
                          {event.path && (
                            <p className="mt-1 truncate text-[9px] text-neutral-600">
                              {event.path}
                              {event.line ? `:${event.line}` : ""}
                            </p>
                          )}
                        </button>
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <p className="p-2 text-xs leading-5 text-neutral-400">
                  {liveEnabled
                    ? "Waiting for local activity."
                    : "Live follow is paused."}
                </p>
              )}
            </div>
          </aside>

          <div className="order-1 h-[60vh] min-h-[520px] xl:order-2 xl:h-[calc(100vh-250px)] xl:min-h-[650px]">
            <Suspense
              fallback={
                <div className="flex h-full items-center justify-center gap-2 text-xs text-neutral-500">
                  <Loader2 size={14} className="animate-spin" /> Loading map…
                </div>
              }
            >
              {dimension === "3d" ? (
                <CodeGraph3D
                  nodes={graph.nodes}
                  edges={graph.edges}
                  selectedId={selectedId}
                  activityByNode={activityByNode}
                  followNodeId={liveEnabled ? followNodeId : null}
                  onSelect={selectNode}
                  onExpand={focusNode}
                />
              ) : (
                <CodeGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  selectedId={selectedId}
                  activityByNode={activityByNode}
                  followNodeId={liveEnabled ? followNodeId : null}
                  onSelect={selectNode}
                  onExpand={focusNode}
                />
              )}
            </Suspense>
          </div>

          <aside className="order-3 border-t border-neutral-800 bg-neutral-950/60 xl:border-l xl:border-t-0">
            {selectedNode ? (
              <div>
                <div className="border-b border-neutral-800 p-5">
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5 border border-cyan-500/30 bg-cyan-500/10 p-2 text-cyan-200">
                      <CircleDot size={15} />
                    </div>
                    <div className="min-w-0">
                      <div className="text-[10px] uppercase tracking-widest text-neutral-400">
                        {selectedNode.node_type || "symbol"} ·{" "}
                        {selectedNode.kind}
                      </div>
                      <h3 className="mt-1 break-words text-sm font-semibold text-neutral-100">
                        {selectedNode.qualified_name || selectedNode.label}
                      </h3>
                    </div>
                  </div>
                  {selectedNode.node_type !== "file" && (
                    <>
                      <div className="mt-4 flex items-center gap-3 text-[11px] text-neutral-300">
                        <span className="inline-flex items-center gap-1.5">
                          <ArrowDownLeft
                            size={13}
                            className="text-violet-300"
                          />
                          {incomingCount} callers
                        </span>
                        <span className="inline-flex items-center gap-1.5">
                          <ArrowUpRight size={13} className="text-cyan-300" />
                          {outgoingCount} callees
                        </span>
                      </div>
                      {(callers.length > 0 || callees.length > 0) && (
                        <div className="mt-3 space-y-3">
                          {callers.length > 0 && (
                            <CallList
                              title="Callers"
                              icon={
                                <ArrowDownLeft
                                  size={11}
                                  className="text-violet-300"
                                />
                              }
                              nodes={callers}
                              onPick={openSearchResult}
                            />
                          )}
                          {callees.length > 0 && (
                            <CallList
                              title="Callees"
                              icon={
                                <ArrowUpRight
                                  size={11}
                                  className="text-cyan-300"
                                />
                              }
                              nodes={callees}
                              onPick={openSearchResult}
                            />
                          )}
                        </div>
                      )}
                      <button
                        type="button"
                        onClick={() => focusNode(selectedNode.id)}
                        disabled={expandingId !== null}
                        className="mt-4 inline-flex items-center gap-2 border border-brand-500/50 bg-brand-500/10 px-3 py-2 text-xs text-brand-300 hover:bg-brand-500/20 disabled:opacity-60"
                      >
                        {expandingId === selectedNode.id ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <Expand size={13} />
                        )}{" "}
                        Focus callers + callees
                      </button>
                    </>
                  )}
                </div>
                <div className="border-b border-neutral-800 p-5">
                  <div className="flex items-start gap-2 text-xs text-neutral-300">
                    <FileCode2
                      size={14}
                      className="mt-0.5 shrink-0 text-neutral-400"
                    />
                    <span className="break-all">
                      {selectedNode.path}
                      {selectedNode.node_type !== "file"
                        ? `:${selectedNode.line}–${selectedNode.end_line}`
                        : ""}
                    </span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-[10px] text-neutral-300">
                    <span className="border border-neutral-700 px-2 py-1">
                      {selectedNode.language || "Other"}
                    </span>
                    <span className="border border-neutral-700 px-2 py-1">
                      {selectedNode.community || "root"}
                    </span>
                  </div>
                </div>
                <div className="p-4">
                  <div className="mb-3 text-[10px] font-semibold uppercase tracking-widest text-neutral-300">
                    {selectedNode.node_type === "file"
                      ? "File node"
                      : "Exact symbol source"}
                  </div>
                  {selectedNode.node_type === "file" ? (
                    <p className="text-xs leading-5 text-neutral-300">
                      File metadata is visible in the full map. Select one of
                      its symbol nodes to open an exact source range.
                    </p>
                  ) : detail ? (
                    <pre className="max-h-[430px] overflow-auto whitespace-pre border border-neutral-800 bg-surface-code p-3 text-[11px] leading-5 text-neutral-200">
                      <code className="border-0 bg-transparent p-0 text-inherit">
                        {detail.source ||
                          detail.signature ||
                          "Source is unavailable."}
                      </code>
                    </pre>
                  ) : (
                    <div className="flex items-center gap-2 text-xs text-neutral-400">
                      <Loader2 size={13} className="animate-spin" /> Loading
                      exact range…
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="p-6">
                <GitFork size={22} className="text-brand-300" />
                <h3 className="mt-4 text-sm font-semibold text-neutral-100">
                  Explore the map
                </h3>
                <p className="mt-2 text-xs leading-5 text-neutral-300">
                  Click any file or symbol. Double-click a symbol to focus its
                  callers and callees.
                </p>
              </div>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
