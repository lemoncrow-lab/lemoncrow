import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Boxes,
  Braces,
  FileCode2,
  GitPullRequest,
  Layers3,
  Loader2,
  Maximize2,
  Network,
  Search,
  X,
} from "lucide-react";
import FileTypeIcon from "../components/FileTypeIcon";
import CodeSourceWorkspace from "../components/code/CodeSourceWorkspace";
import CodeMapWorkspace from "../code/CodeMapWorkspace";
import { projectCodeMapGraph } from "../code/codeMapGraphModel";
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

const EMPTY_GRAPH: CodeMapGraph = {
  focus: null,
  nodes: [],
  edges: [],
  truncated: false,
};

type CodeSearchItem =
  | { type: "node"; id: string; node: CodeMapNode }
  | { type: "text"; id: string; match: CodeMapTextMatch };

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
  const [activityAgentRunSupported, setActivityAgentRunSupported] = useState(false);
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
  const graph = useMemo(
    () =>
      projectCodeMapGraph({
        baseGraph,
        groupCount: full?.groups.length ?? 0,
        fileTypeCount: full?.file_types.length ?? 0,
        selectedGroups,
        selectedFileTypes,
        selectedLanguage,
        activeNodeIds: activeIds,
        focused: viewMode === "focus",
      }),
    [
      activeIds,
      baseGraph,
      full?.groups.length,
      full?.file_types.length,
      selectedGroups,
      selectedFileTypes,
      selectedLanguage,
      viewMode,
    ]
  );

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
    (
      message: string,
      engine?: "auto" | "lemoncode" | "pi" | "codex" | "claude"
    ) => {
      if (!selectedNode || !projectUrlReference || (!engine && !activitySessionId)) {
        return Promise.reject(
          new Error(
            engine
              ? "No project is available for this agent run."
              : "No exact coding-agent session is available for this project."
          )
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
        expected_session_id: engine ? undefined : activitySessionId ?? undefined,
        engine,
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
        setActivityAgentRunSupported(Boolean(payload.agent_run_supported));
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
    <section className="min-h-[calc(100vh-80px)] bg-surface-sunken text-neutral-200">
      <div className="border-b border-neutral-800 bg-neutral-950/75 px-3 lg:px-4">
        <div className="flex h-8 items-center gap-1.5 overflow-x-auto whitespace-nowrap">
          <div
            className="flex shrink-0 items-center gap-1.5 text-[10px]"
            title={selectedProject?.worktree_root || selectedProject?.root}
          >
            <span className="text-neutral-600">dir</span>
            <span className="max-w-36 truncate font-medium text-neutral-300">
              {selectedProject?.directory?.split("/").filter(Boolean).pop() || "local"}
            </span>
            <span className="text-neutral-700">·</span>
            <span className="text-neutral-600">project</span>
            <span className="max-w-36 truncate font-medium text-neutral-300">
              {selectedProject?.project || full?.project.label || "local index"}
            </span>
            <span className="text-neutral-700">·</span>
            <span className="text-neutral-600">worktree</span>
            <span className="max-w-40 truncate font-medium text-neutral-300">
              {selectedProject?.worktree || full?.project.label || "local index"}
            </span>
            <span className="text-neutral-700">·</span>
            <span className="text-neutral-600">branch</span>
            <span className="max-w-40 truncate font-medium text-neutral-300">
              {selectedProject?.branch || "…"}
            </span>
            <span className="text-neutral-700">·</span>
            <span className="text-neutral-500">
              {(full?.total_symbols ?? 0).toLocaleString()} symbols
            </span>
            <span className="text-neutral-700">·</span>
            <span className="text-neutral-500">
              {(full?.total_files ?? 0).toLocaleString()} files
            </span>
            {surfaceMode === "map" && (
              <>
                <span className="text-neutral-700">·</span>
                <span className="text-neutral-500">
                  {graph.nodes.length.toLocaleString()} nodes
                </span>
                <span className="text-neutral-700">·</span>
                <span className="text-neutral-500">
                  {graphCallCount.toLocaleString()} calls
                </span>
              </>
            )}
            {full?.truncated && (
              <>
                <span className="text-neutral-700">·</span>
                <span className="text-amber-300">bounded</span>
              </>
            )}
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px]">
            {projects.length > 1 && (
              <select
                className="h-6 max-w-44 border border-neutral-700 bg-neutral-900 px-2 text-[10px] text-neutral-200"
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
            <div className="relative w-[280px] shrink-0">
              <Search
                size={12}
                className="pointer-events-none absolute left-2 top-1.5 text-neutral-500"
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
                className="h-6 w-full border border-neutral-700 bg-neutral-900 pl-7 pr-12 text-[10px] text-neutral-100 outline-none placeholder:text-neutral-600 focus:border-brand-500/60"
                aria-label="Find code"
                aria-controls={searchResults.length ? "code-search-results" : undefined}
              />
              {!query && (
                <span className="pointer-events-none absolute right-1.5 top-1 border border-neutral-800 px-1 text-[9px] text-neutral-600">
                  {navigator.platform?.toLowerCase().includes("mac") ? "⌘K" : "Ctrl K"}
                </span>
              )}
              {searching && (
                <Loader2
                  size={11}
                  className="absolute right-2 top-1.5 animate-spin text-brand-300"
                />
              )}
              {query.trim().length >= 2 && !searching && searchError && (
                <div className="absolute right-0 top-8 z-30 w-full border border-red-900/70 bg-neutral-950 px-3 py-3 text-[11px] text-red-300 shadow-2xl">
                  {searchError}
                </div>
              )}
              {query.trim().length >= 2 &&
                !searching &&
                !searchError &&
                searchResults.length === 0 && (
                  <div className="absolute right-0 top-8 z-30 w-full border border-neutral-700 bg-neutral-950 px-3 py-3 text-[11px] text-neutral-500 shadow-2xl">
                    No matching files, symbols, or text.
                  </div>
                )}
              {searchResults.length > 0 && (
                <div
                  id="code-search-results"
                  className="absolute right-0 top-8 z-30 max-h-96 w-full overflow-y-auto border border-neutral-700 bg-neutral-950 shadow-2xl"
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
                          <FileTypeIcon path={path} size={13} />
                          <span className="min-w-0 flex-1 truncate text-xs text-neutral-100">
                            {label}
                          </span>
                          <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wider text-neutral-600">
                            {badge}
                          </span>
                        </span>
                        <span className="mt-0.5 block truncate pl-[21px] text-[10px] text-neutral-500">
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

            <button
              type="button"
              onClick={() => void openReview()}
              disabled={!projectUrlReference || reviewBusy}
              className="inline-flex h-6 shrink-0 items-center gap-1.5 border border-neutral-700 bg-neutral-900/70 px-2 text-neutral-300 hover:border-neutral-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              {reviewBusy ? (
                <Loader2 size={11} className="animate-spin" />
              ) : (
                <GitPullRequest size={11} />
              )}
              {reviewBusy ? "Preparing…" : "Review changes"}
            </button>
            {reviewError && (
              <span className="max-w-40 truncate text-[10px] text-red-300/80" title={reviewError}>
                {reviewError}
              </span>
            )}
            <div className="flex h-7 shrink-0 border border-neutral-700 bg-neutral-900/70">
              <button
                type="button"
                onClick={() => changeSurface("source")}
                aria-pressed={surfaceMode === "source"}
                className={`inline-flex h-full items-center gap-1.5 px-2 ${surfaceMode === "source" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
              >
                <FileCode2 size={11} /> Source
              </button>
              <button
                type="button"
                onClick={() => changeSurface("map")}
                aria-pressed={surfaceMode === "map"}
                className={`inline-flex h-full items-center gap-1.5 border-l border-neutral-700 px-2 ${surfaceMode === "map" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
              >
                <Network size={11} /> Map
              </button>
            </div>
            {surfaceMode === "map" && (
              <>
                <div className="flex h-7 shrink-0 border border-neutral-700 bg-neutral-900/70">
                  <button
                    type="button"
                    onClick={() => setViewMode("full")}
                    aria-pressed={viewMode === "full"}
                    className={`inline-flex h-full items-center gap-1.5 px-2 ${viewMode === "full" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                  >
                    <Maximize2 size={11} /> Full
                  </button>
                  <button
                    type="button"
                    disabled={!focusGraph.nodes.length}
                    onClick={() => setViewMode("focus")}
                    aria-pressed={viewMode === "focus"}
                    className={`inline-flex h-full items-center gap-1.5 border-l border-neutral-700 px-2 disabled:opacity-40 ${viewMode === "focus" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                  >
                    <Layers3 size={11} /> Focus
                  </button>
                </div>
                <div className="flex h-7 shrink-0 border border-neutral-700 bg-neutral-900/70">
                  <button
                    type="button"
                    onClick={() => setDimension("2d")}
                    aria-pressed={dimension === "2d"}
                    className={`inline-flex h-full items-center px-2 ${dimension === "2d" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                  >
                    2D
                  </button>
                  <button
                    type="button"
                    onClick={() => setDimension("3d")}
                    aria-pressed={dimension === "3d"}
                    className={`inline-flex h-full items-center gap-1.5 border-l border-neutral-700 px-2 ${dimension === "3d" ? "bg-neutral-700 text-white" : "text-neutral-400"}`}
                  >
                    <Boxes size={11} /> 3D
                  </button>
                </div>
              </>
            )}
            <button
              type="button"
              onClick={() => setLiveEnabled((enabled) => !enabled)}
              className="inline-flex h-6 shrink-0 items-center gap-1.5 border border-neutral-700 bg-neutral-900/70 px-2 text-neutral-400 hover:text-neutral-200"
              aria-pressed={liveEnabled}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${liveEnabled && activityStatus !== "offline" ? "bg-emerald-400" : "bg-neutral-600"}`}
              />
              Live {liveEnabled ? "on" : "off"}
            </button>
          </div>
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
          agentRunSupported={activityAgentRunSupported}
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
        <CodeMapWorkspace
          graph={graph}
          groups={full.groups}
          fileTypes={full.file_types}
          languages={full.languages}
          selectedGroups={selectedGroups}
          selectedFileTypes={selectedFileTypes}
          selectedLanguage={selectedLanguage}
          dimension={dimension}
          selectedId={selectedId}
          selectedNode={selectedNode}
          detail={detail}
          callers={callers}
          callees={callees}
          incomingCount={incomingCount}
          outgoingCount={outgoingCount}
          expandingId={expandingId}
          activityByNode={activityByNode}
          followNodeId={followNodeId}
          liveEnabled={liveEnabled}
          events={events}
          onToggleGroup={(id) => toggleFacet(setSelectedGroups, id)}
          onToggleAllGroups={() => toggleAll(setSelectedGroups, full.groups, selectedGroups)}
          onToggleFileType={(id) => toggleFacet(setSelectedFileTypes, id)}
          onToggleAllFileTypes={() => toggleAll(setSelectedFileTypes, full.file_types, selectedFileTypes)}
          onLanguageChange={setSelectedLanguage}
          onSelect={selectNode}
          onFocus={focusNode}
          onPick={openSearchResult}
          onOpenActivity={openActivity}
        />
      )}
    </section>
  );
}
