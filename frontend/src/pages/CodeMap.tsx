import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Boxes, Braces, FileCode2, Loader2, Network, Search, Share2, Sparkles, X } from "lucide-react";

import FileTypeIcon from "../components/FileTypeIcon";
import { selectedEditor } from "../lib/editorPreference";
import { setVisibleInterval } from "../lib/visibleInterval";
import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { currentReturnPath, returnPathFrom, withReturnPath } from "../lib/codeLinks";
import { useResolvedProductCapabilities } from "../lib/RuntimeCapabilities";
import type { ReviewAskSelection } from "../review/ReviewAskDrawer";
import { createCodeAskTransport } from "../code/codeAskTransport";
import CodeChangesMenu from "../code/CodeChangesMenu";
import CodeExplorer from "../code/CodeExplorer";
import CodeInspector, { type InspectorSubject, type ModuleLink, type ReviewScope } from "../code/CodeInspector";
import CodeMapView, { type Crumb } from "../code/CodeMapView";
import CodeSourceView from "../code/CodeSourceView";
import CodeTopologyView from "../code/CodeTopologyView";
import {
  formatMapSpec,
  liveByPath,
  liveMarksByPath,
  moduleOfPath,
  nodeAtLine,
  parseMapSpec,
  sessionMarks,
  type MapSpec,
} from "../code/codeModel";
import type { MapMemberSymbol, MapNode } from "../map/types";
import {
  api,
  type CodeChangedFile,
  type CodeChanges,
  type CodeFocusPayload,
  type CodeHotspots,
  type CodeMapActivityEvent,
  type CodeMapActivitySession,
  type CodeMapEditorCapability,
  type CodeMapNode,
  type CodeMapProject,
  type CodeMapReference,
  type CodeMapReferences,
  type CodeMapTextMatch,
  type CodeModulesOverview,
  type CodeRelationships,
} from "../api";

type CodeSearchItem = { type: "node"; id: string; node: CodeMapNode } | { type: "text"; id: string; match: CodeMapTextMatch };
type CodeSurface = "source" | "map" | "2d" | "3d";

/** The map does not move under the reader's hands: Follow waits this long after input. */
// The Ask drawer carries the markdown renderer (~350 KB); load it on first open.
const ReviewAskDrawer = lazy(() => import("../review/ReviewAskDrawer"));

const FOLLOW_QUIET_MS = 8_000;
const CHANGES_POLL_MS = 10_000;
const ACTIVITY_POLL_MS = 2_500;

function fileNodeFor(path: string): CodeMapNode {
  const slash = path.lastIndexOf("/");
  return {
    id: `file::${path}`,
    label: slash >= 0 ? path.slice(slash + 1) : path,
    qualified_name: path,
    path,
    kind: "source",
    node_type: "file",
    line: 1,
    end_line: 1,
  };
}

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

export default function CodeMap() {
  const navigate = useNavigate();
  const routerLocation = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedProject = searchParams.get("repo") || undefined;
  const requestedFile = searchParams.get("file") || undefined;
  const requestedSymbol = searchParams.get("symbol") || undefined;
  const requestedSurface = searchParams.get("view");
  const surface: CodeSurface = requestedSurface === "map" || requestedSurface === "2d" || requestedSurface === "3d" ? requestedSurface : "source";
  const mapSpecValue = searchParams.get("map");
  const mapSpec = useMemo(() => parseMapSpec(mapSpecValue), [mapSpecValue]);
  const selectedModuleId = searchParams.get("module");
  const sessionFilter = searchParams.get("session");
  const returnTo = returnPathFrom(searchParams);
  const returnLabel = /^\/(?:r|reviews?)(?:\/|$|\?)/.test(returnTo) ? "Review" : "Back";
  const requestedLineValue = Number(searchParams.get("line"));
  const requestedEndValue = Number(searchParams.get("end"));
  const requestedLine = Number.isInteger(requestedLineValue) && requestedLineValue > 0 ? requestedLineValue : null;
  const requestedEnd = Number.isInteger(requestedEndValue) && requestedEndValue >= (requestedLine ?? 1) ? requestedEndValue : requestedLine;
  const locationRange = useMemo(
    () => (requestedLine ? ([requestedLine, requestedEnd ?? requestedLine] as const) : null),
    [requestedEnd, requestedLine],
  );

  const [explorerWidth, setExplorerWidth] = useState(270);
  const [projects, setProjects] = useState<CodeMapProject[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [files, setFiles] = useState<CodeMapNode[]>([]);
  const [showIgnored, setShowIgnored] = useState(false);
  const [ignoredTruncated, setIgnoredTruncated] = useState(false);
  const [projectLabel, setProjectLabel] = useState<string>("");
  const [sourceProject, setSourceProject] = useState<{ reference: string; root: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFileSymbols, setSelectedFileSymbols] = useState<CodeMapNode[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [extraNodes, setExtraNodes] = useState<CodeMapNode[]>([]);
  const [relationships, setRelationships] = useState<CodeRelationships | null>(null);
  const [relationshipsLoading, setRelationshipsLoading] = useState(false);
  const [relationshipsError, setRelationshipsError] = useState<string | null>(null);
  const [references, setReferences] = useState<CodeMapReferences | null>(null);
  const [referencesLoading, setReferencesLoading] = useState(false);
  const [referencesError, setReferencesError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CodeMapNode[]>([]);
  const [textResults, setTextResults] = useState<CodeMapTextMatch[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeSearchIndex, setActiveSearchIndex] = useState(0);
  const [editorCapability, setEditorCapability] = useState<CodeMapEditorCapability | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [activityStatus, setActivityStatus] = useState("idle");
  const [activityHost, setActivityHost] = useState<string | null>(null);
  const [activitySessionId, setActivitySessionId] = useState<string | null>(null);
  const [followedSession, setFollowedSession] = useState<string | null>(null);
  const [activitySessions, setActivitySessions] = useState<CodeMapActivitySession[]>([]);
  const [askOpen, setAskOpen] = useState(false);
  const [askScope, setAskScope] = useState<ReviewAskSelection | null>(null);
  const [events, setEvents] = useState<CodeMapActivityEvent[]>([]);
  // Last edit in the first activity snapshot: that snapshot replays history,
  // so only edits after it should refetch changes and hotspots.
  const [snapshotEditId, setSnapshotEditId] = useState("");
  const [changes, setChanges] = useState<CodeChanges | null>(null);
  const [hotspots, setHotspots] = useState<CodeHotspots | null>(null);
  const [purposeBusy, setPurposeBusy] = useState<string | null>(null);
  const [purposeError, setPurposeError] = useState<{ module: string; message: string } | null>(null);
  const [overview, setOverview] = useState<CodeModulesOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [focus, setFocus] = useState<CodeFocusPayload | null>(null);
  const [focusLoading, setFocusLoading] = useState(false);
  const [focusError, setFocusError] = useState<string | null>(null);
  const [mapReload, setMapReload] = useState(0);
  const [selectedFocusId, setSelectedFocusId] = useState<string | null>(null);
  const [follow, setFollow] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const activityCursor = useRef<string | null>(null);
  const activitySession = useRef<string | null>(null);
  const lastInteraction = useRef(0);
  const fileSymbolsCache = useRef<Map<string, { editId: string; symbols: CodeMapNode[] }>>(new Map());
  const searchInput = useRef<HTMLInputElement | null>(null);
  const askReturnFocus = useRef<HTMLElement | null>(null);

  const requestedProjectEntry = requestedProject
    ? projects.find((project) => project.project_id === requestedProject || project.root === requestedProject)
    : undefined;
  const selectedProject =
    requestedProjectEntry ||
    (!requestedProject ? projects.find((project) => project.active && project.indexed) || projects.find((project) => project.indexed) : undefined);
  const selectedProjectReference = selectedProject?.project_id || requestedProject || selectedProject?.root;
  const projectRoot = selectedProject?.root ?? (sourceProject && sourceProject.reference === selectedProjectReference ? sourceProject.root : undefined);
  const projectUrlReference = selectedProject?.project_id || selectedProject?.root || requestedProject || projectRoot;
  // A local Dashboard serves only the deterministic map (runtime.code_map):
  // Code Ask, the editor, references, module purposes and Review capture stay
  // with the full product unless the host resolves them separately, and are
  // hidden, not shown broken.
  const productCapabilities = useResolvedProductCapabilities();
  const fullCode = productCapabilities?.features["runtime.code"] === true;
  const codeAskAvailable = productCapabilities?.features["runtime.code_ask"] === true;
  const localEditorAvailable = productCapabilities?.features["runtime.local_editor"] === true;
  const projectKnowledgeAvailable = productCapabilities?.features["runtime.project_knowledge"] === true;
  // Hosted Code reads an immutable tenant View: no Review from live changes
  // and no developer-local sessions or edit ledgers.
  const reviewCaptureAvailable =
    productCapabilities?.runtime.localReviewCapture === true &&
    (fullCode || Boolean(productCapabilities?.products.review.available));
  const liveWorkspace = productCapabilities?.features["runtime.live_workspace"] !== false;
  const codeAskTransport = useMemo(
    () => (codeAskAvailable && projectUrlReference ? createCodeAskTransport(projectUrlReference) : null),
    [codeAskAvailable, projectUrlReference],
  );
  useEffect(() => {
    setAskOpen(false);
    setAskScope(null);
  }, [projectUrlReference]);

  const nodesById = useMemo(() => {
    const byId = new Map<string, CodeMapNode>();
    for (const node of files) byId.set(node.id, node);
    for (const node of extraNodes) byId.set(node.id, node);
    for (const node of selectedFileSymbols) byId.set(node.id, node);
    return byId;
  }, [extraNodes, files, selectedFileSymbols]);
  const filesByPath = useMemo(() => new Map(files.map((node) => [node.path, node])), [files]);
  const selectedNode = selectedId ? nodesById.get(selectedId) ?? null : null;
  const symbolsByPath = useMemo(() => {
    const byPath = new Map<string, CodeMapNode[]>();
    for (const node of selectedFileSymbols) {
      const list = byPath.get(node.path) ?? [];
      list.push(node);
      byPath.set(node.path, list);
    }
    for (const list of byPath.values()) list.sort((a, b) => a.line - b.line || a.label.localeCompare(b.label));
    return byPath;
  }, [selectedFileSymbols]);
  const changedByPath = useMemo(() => new Map<string, CodeChangedFile>((changes?.files ?? []).map((row) => [row.path, row])), [changes]);
  const hotspotByPath = useMemo(() => new Map((hotspots?.files ?? []).map((row) => [row.path, row])), [hotspots]);
  const marks = useMemo(() => sessionMarks(changes, activitySessions), [activitySessions, changes]);
  const livePaths = useMemo(() => (liveEnabled ? liveByPath(events, now) : new Map()), [events, liveEnabled, now]);
  const liveMarks = useMemo(
    () => liveMarksByPath(livePaths, new Map(), marks, followedSession),
    [followedSession, livePaths, marks],
  );
  const overviewWithChanges = useMemo(() => {
    if (!overview) return null;
    const counts = new Map<string, number>();
    for (const path of changedByPath.keys()) {
      const module = moduleOfPath(path, overview.modules);
      if (module !== null) counts.set(module, (counts.get(module) ?? 0) + 1);
    }
    return { ...overview, modules: overview.modules.map((module) => ({ ...module, changed: counts.get(module.id) ?? 0 })) };
  }, [changedByPath, overview]);

  // ------------------------------------------------------------------ search
  const fileSearchResults = useMemo(() => {
    const clean = query.trim().toLowerCase();
    if (clean.length < 2) return [];
    return files.filter((node) => `${node.label} ${node.path}`.toLowerCase().includes(clean)).slice(0, 6);
  }, [files, query]);
  const searchResults = useMemo<CodeSearchItem[]>(() => {
    const seen = new Set<string>();
    const nodes = [...fileSearchResults, ...results]
      .filter((node) => {
        if (seen.has(node.id)) return false;
        seen.add(node.id);
        return true;
      })
      .map((node) => ({ type: "node" as const, id: node.id, node }));
    const text = textResults.map((match) => ({ type: "text" as const, id: match.id, match }));
    return [...nodes, ...text].slice(0, 30);
  }, [fileSearchResults, results, textResults]);

  useEffect(() => setActiveSearchIndex(0), [query, searchResults.length]);

  useEffect(() => {
    if (!localEditorAvailable) {
      setEditorCapability(null);
      return;
    }
    let cancelled = false;
    void api.codeMapEditorCapability()
      .then((capability) => {
        if (!cancelled) setEditorCapability(capability);
      })
      .catch(() => {
        if (!cancelled) setEditorCapability(null);
      });
    return () => {
      cancelled = true;
    };
  }, [localEditorAvailable]);

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
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  // ---------------------------------------------------------------- projects
  useEffect(() => {
    void api
      .codeMapProjects()
      .then((payload) => {
        setProjects(payload.projects);
        setProjectsError(null);
      })
      .catch(() => {
        setProjects([]);
        setProjectsError("Code index API unavailable. Restart the dashboard service, then reopen Code.");
      })
      .finally(() => setProjectsLoaded(true));
  }, []);

  useEffect(() => {
    if (!requestedProject || !selectedProject?.project_id || requestedProject === selectedProject.project_id) return;
    const next = new URLSearchParams(searchParams);
    next.set("repo", selectedProject.project_id);
    setSearchParams(next, { replace: true });
  }, [requestedProject, searchParams, selectedProject?.project_id, setSearchParams]);

  useEffect(() => {
    if (requestedProject || !projectsLoaded || selectedProjectReference) return;
    setLoading(false);
    setFiles([]);
    setError(projectsError || "No indexed project is available in this dashboard session.");
  }, [projectsError, projectsLoaded, requestedProject, selectedProjectReference]);

  useEffect(() => {
    if (!selectedProjectReference) return;
    setLoading(true);
    setSelectedId(null);
    setFiles([]);
    setIgnoredTruncated(false);
    setSelectedFileSymbols([]);
    setOverview(null);
    setFocus(null);
    setChanges(null);
    setHotspots(null);
    activityCursor.current = null;
    activitySession.current = null;
    setEvents([]);
    setActivityHost(null);
    setActivitySessionId(null);
  }, [selectedProjectReference]);

  // Source's first paint depends only on the file list.
  useEffect(() => {
    if (!selectedProjectReference) return;
    let cancelled = false;
    setError(null);
    void api
      .codeMapFiles(selectedProjectReference, showIgnored)
      .then((payload) => {
        if (cancelled) return;
        setFiles(payload.files);
        setIgnoredTruncated(Boolean(payload.ignored_truncated));
        setProjectLabel(payload.project.label);
        setSourceProject({ reference: selectedProjectReference, root: payload.project.root });
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(errorText(caught, "Could not list the project's files."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedProjectReference, showIgnored]);

  // --------------------------------------------------------------- selection
  const selectedPath = selectedNode?.path ?? requestedFile ?? null;
  useEffect(() => {
    if (!requestedFile) return;
    if (requestedSymbol) {
      const symbol = nodesById.get(requestedSymbol);
      if (symbol) {
        setSelectedId(symbol.id);
        return;
      }
    }
    const fileNode = filesByPath.get(requestedFile);
    if (fileNode) {
      setSelectedId(fileNode.id);
      return;
    }
    if (!loading) {
      // Outside the listing (ignored, generated, or new): still openable.
      const synthetic = fileNodeFor(requestedFile);
      setExtraNodes((current) => (current.some((node) => node.id === synthetic.id) ? current : [...current, synthetic]));
      setSelectedId(synthetic.id);
    }
  }, [filesByPath, loading, nodesById, requestedFile, requestedSymbol]);

  useEffect(() => {
    if (!projectRoot || !selectedPath) {
      setSelectedFileSymbols([]);
      return;
    }
    const editId = [...events].reverse().find((event) => event.kind === "edit" && event.path === selectedPath)?.id ?? "";
    const cacheKey = `${projectRoot}\n${selectedPath}`;
    const cached = fileSymbolsCache.current.get(cacheKey);
    if (cached?.editId === editId) {
      setSelectedFileSymbols(cached.symbols);
      return;
    }
    let cancelled = false;
    void api
      .codeMapFileSymbols(selectedPath, projectRoot)
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
  }, [events, projectRoot, selectedPath]);

  // Used by / Uses for the selected file or symbol, from the shared provider.
  const relationshipKey = selectedNode ? `${selectedNode.node_type === "file" ? "file" : "symbol"}:${selectedNode.node_type === "file" ? selectedNode.path : selectedNode.id}` : "";
  useEffect(() => {
    if (!projectRoot || !selectedNode) {
      setRelationships(null);
      setRelationshipsError(null);
      setRelationshipsLoading(false);
      return;
    }
    const controller = new AbortController();
    setRelationshipsLoading(true);
    setRelationshipsError(null);
    setRelationships(null);
    const target = selectedNode.node_type === "file" ? { path: selectedNode.path } : { symbolId: selectedNode.id };
    void api
      .codeMapRelationships(target, projectRoot, controller.signal)
      .then((payload) => setRelationships(payload))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setRelationshipsError(errorText(caught, "Could not read relationships for this selection."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setRelationshipsLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectRoot, relationshipKey]);

  useEffect(() => {
    if (!fullCode || !selectedNode || selectedNode.node_type === "file" || !projectRoot) {
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
        if (!cancelled) setReferencesError("Could not load references for this symbol.");
      })
      .finally(() => {
        if (!cancelled) setReferencesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fullCode, projectRoot, selectedNode]);

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
          if (cancelled) return;
          setResults(payload.results);
          setTextResults(payload.text_results);
          setSearchError(null);
        })
        .catch(() => {
          if (cancelled) return;
          setResults([]);
          setTextResults([]);
          setSearchError("Code search is temporarily unavailable.");
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

  // ----------------------------------------------------------------- location
  const updateLocation = useCallback(
    (mutate: (next: URLSearchParams) => void, replace = false) => {
      const next = new URLSearchParams(searchParams);
      if (projectUrlReference) next.set("repo", projectUrlReference);
      mutate(next);
      setSearchParams(next, { replace });
    },
    [projectUrlReference, searchParams, setSearchParams],
  );

  const locateNode = useCallback(
    (node: CodeMapNode, view: CodeSurface, replace = false, range?: { start: number; end?: number } | null) => {
      updateLocation((next) => {
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
        if (view === "source") next.delete("module");
      }, replace);
    },
    [updateLocation],
  );

  const remember = useCallback((node: CodeMapNode) => {
    setExtraNodes((current) => (current.some((item) => item.id === node.id) ? current : [...current.slice(-200), node]));
  }, []);

  const openNode = useCallback(
    (node: CodeMapNode, range?: { start: number; end?: number } | null) => {
      setQuery("");
      setResults([]);
      setTextResults([]);
      remember(node);
      setSelectedId(node.id);
      locateNode(node, "source", false, range);
    },
    [locateNode, remember],
  );

  const openPath = useCallback(
    (path: string, line?: number) => {
      const known = line ? nodeAtLine([...nodesById.values()], path, line) : undefined;
      const node = known ?? filesByPath.get(path) ?? fileNodeFor(path);
      openNode(node, line ? { start: line, end: line } : null);
    },
    [filesByPath, nodesById, openNode],
  );

  const openTextMatch = useCallback((match: CodeMapTextMatch) => openPath(match.path, match.line), [openPath]);
  const openSearchItem = useCallback(
    (item: CodeSearchItem) => {
      if (item.type === "text") openTextMatch(item.match);
      else openNode(item.node);
    },
    [openNode, openTextMatch],
  );

  const openReference = useCallback(
    (reference: CodeMapReference) => {
      const node = nodeAtLine([...nodesById.values()], reference.path, reference.line) ?? filesByPath.get(reference.path) ?? fileNodeFor(reference.path);
      openNode(node, { start: reference.line, end: Math.max(reference.line, reference.end_line) });
    },
    [filesByPath, nodesById, openNode],
  );

  const openActivity = useCallback(
    (event: CodeMapActivityEvent) => {
      const symbol = (event.symbol_ids ?? []).map((id) => nodesById.get(id)).find((node): node is CodeMapNode => Boolean(node && node.node_type !== "file"));
      if (symbol) openNode(symbol, event.line ? { start: event.line, end: event.line } : null);
      else if (event.path) openPath(event.path, event.line);
    },
    [nodesById, openNode, openPath],
  );

  const openLine = useCallback(
    (line: number) => {
      if (selectedNode && line > 0) openPath(selectedNode.path, line);
    },
    [openPath, selectedNode],
  );

  const changeSurface = useCallback(
    (view: CodeSurface) => {
      const enteringVisualFromSource = surface === "source" && view !== "source";
      if (enteringVisualFromSource) setSelectedFocusId(null);
      updateLocation((next) => {
        next.set("view", view);
        if (surface === "source" && view === "map" && selectedNode) {
          const spec: MapSpec = selectedNode.node_type === "file"
            ? { level: "file", id: selectedNode.path }
            : { level: "symbol", id: selectedNode.id };
          const value = formatMapSpec(spec);
          if (value) next.set("map", value);
        } else if (surface === "source" && (view === "2d" || view === "3d")) {
          next.delete("map");
          next.delete("module");
        }
      }, true);
    },
    [selectedNode, surface, updateLocation],
  );
  const setMapSpec = useCallback(
    (spec: MapSpec, replace = false) => {
      setSelectedFocusId(null);
      updateLocation((next) => {
        next.set("view", surface === "source" ? "map" : surface);
        const value = formatMapSpec(spec);
        if (value) next.set("map", value);
        else next.delete("map");
        if (spec?.level === "module") next.set("module", spec.id);
        else if (!spec) next.delete("module");
      }, replace);
    },
    [surface, updateLocation],
  );

  const selectModule = useCallback(
    (id: string | null) => {
      updateLocation((next) => {
        if (id === null) next.delete("module");
        else next.set("module", id);
      }, true);
    },
    [updateLocation],
  );

  const setSessionFilter = useCallback(
    (session: string | null) => {
      updateLocation((next) => {
        if (session) next.set("session", session);
        else next.delete("session");
      });
      if (session && session !== "unattributed") setFollowedSession(session);
    },
    [updateLocation],
  );

  // ------------------------------------------------------------ live activity
  const followTarget = sessionFilter && sessionFilter !== "unattributed" ? sessionFilter : null;
  useEffect(() => {
    if (!projectRoot || !liveEnabled) return;
    let cancelled = false;
    activityCursor.current = null;
    activitySession.current = null;
    const poll = async () => {
      try {
        const firstSnapshot = activityCursor.current === null;
        const payload = await api.codeMapActivity(projectRoot, activityCursor.current, 60, followTarget);
        if (cancelled) return;
        if (firstSnapshot) {
          setSnapshotEditId([...payload.events].reverse().find((event) => event.kind === "edit")?.id ?? "");
        }
        if (activitySession.current && payload.session_id && activitySession.current !== payload.session_id) {
          activityCursor.current = null;
          setEvents([]);
        }
        activitySession.current = payload.session_id;
        activityCursor.current = payload.cursor ?? activityCursor.current;
        setActivitySessionId(payload.session_id);
        setActivityHost(payload.host ?? null);

        setActivityStatus(payload.status);
        setActivitySessions(payload.sessions ?? []);
        setFollowedSession(payload.selected_session ?? null);
        if (!payload.events.length) return;
        setEvents((current) => {
          const merged = new Map(current.map((event) => [event.id, event]));
          for (const event of payload.events) merged.set(event.id, event);
          return [...merged.values()].slice(-40);
        });
      } catch {
        if (!cancelled) setActivityStatus("offline");
      }
    };
    void poll();
    const stopPolling = setVisibleInterval(poll, ACTIVITY_POLL_MS);
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [followTarget, liveEnabled, projectRoot]);

  // Uncommitted changes by session: polled, and refreshed on agent edits.
  const lastEditId = [...events].reverse().find((event) => event.kind === "edit")?.id ?? "";
  const freshEditId = lastEditId === snapshotEditId ? "" : lastEditId;
  useEffect(() => {
    if (!projectRoot) return;
    let cancelled = false;
    const load = () => {
      void api
        .codeMapChanges(projectRoot)
        .then((payload) => {
          if (!cancelled) setChanges(payload);
        })
        .catch(() => undefined);
    };
    load();
    const stopPolling = setVisibleInterval(load, CHANGES_POLL_MS);
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [freshEditId, projectRoot]);

  // Files agents keep re-editing: recorded edits over the last week, refreshed
  // when the followed session edits.
  useEffect(() => {
    if (!projectRoot) return;
    let cancelled = false;
    void api
      .codeMapHotspots(projectRoot)
      .then((payload) => {
        if (!cancelled && Array.isArray(payload.files)) setHotspots(payload);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [freshEditId, projectRoot]);

  // Follow: keep the map (or Source) on the file the followed session edits,
  // but never while the reader is using it.
  const lastFollowed = useRef<string>("");
  useEffect(() => {
    if (!follow || !liveEnabled) return;
    const edit = [...events].reverse().find((event) => event.kind === "edit" && event.path);
    if (!edit?.path || edit.id === lastFollowed.current) return;
    if (Date.now() - lastInteraction.current < FOLLOW_QUIET_MS) return;
    lastFollowed.current = edit.id;
    if (surface !== "source") setMapSpec({ level: "file", id: edit.path }, true);
    else {
      const node = filesByPath.get(edit.path) ?? fileNodeFor(edit.path);
      remember(node);
      setSelectedId(node.id);
      locateNode(node, "source", true, edit.line ? { start: edit.line } : null);
    }
  }, [events, filesByPath, follow, liveEnabled, locateNode, remember, setMapSpec, surface]);

  // --------------------------------------------------------------------- map
  useEffect(() => {
    if (surface === "source" || !projectRoot || (overview && !mapReload)) return;
    const controller = new AbortController();
    setOverviewLoading(true);
    setOverviewError(null);
    void api
      .codeMapModules(projectRoot, controller.signal)
      .then((payload) => setOverview(payload))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setOverviewError(errorText(caught, "Could not group the repository into modules."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setOverviewLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapReload, projectRoot, surface]);

  useEffect(() => {
    if (surface === "source" || !projectRoot || !mapSpec) {
      setFocus(null);
      setFocusError(null);
      return;
    }
    const controller = new AbortController();
    setFocusLoading(true);
    setFocusError(null);
    void api
      .codeMapFocus(mapSpec.level, mapSpec.id, projectRoot, controller.signal)
      .then((payload) => setFocus(payload))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setFocus(null);
          setFocusError(errorText(caught, "Could not map this subject."));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setFocusLoading(false);
      });
    return () => controller.abort();
  }, [mapReload, mapSpec, projectRoot, surface]);

  const crumbs = useMemo<Crumb[]>(() => {
    const root: Crumb = { label: selectedProject?.project || projectLabel || "Repository", spec: null };
    if (!mapSpec) return [root];
    const modules = overviewWithChanges?.modules ?? [];
    const moduleCrumb = (path: string): Crumb[] => {
      const module = moduleOfPath(path, modules);
      return module !== null && modules.some((item) => item.id === module && item.kind === "module")
        ? [{ label: module || "(root)", spec: { level: "module", id: module } }]
        : [];
    };
    if (mapSpec.level === "module") return [root, { label: mapSpec.id || "(root)", spec: mapSpec }];
    if (mapSpec.level === "file") {
      return [root, ...moduleCrumb(mapSpec.id), { label: mapSpec.id.split("/").pop() || mapSpec.id, spec: mapSpec, source: { path: mapSpec.id } }];
    }
    const path = focus?.subject.path || nodesById.get(mapSpec.id)?.path || "";
    const label = focus?.subject.label || nodesById.get(mapSpec.id)?.label || "symbol";
    const line = nodesById.get(mapSpec.id)?.line;
    return [
      root,
      ...(path ? moduleCrumb(path) : []),
      ...(path ? [{ label: path.split("/").pop() || path, spec: { level: "file" as const, id: path }, source: { path } }] : []),
      { label, spec: mapSpec, source: path ? { path, symbolId: mapSpec.id, line } : undefined },
    ];
  }, [focus, mapSpec, nodesById, overviewWithChanges, projectLabel, selectedProject?.project]);

  const pathOfFocusNode = (node: MapNode): string => node.path;
  const openFocusNode = (node: MapNode) => {
    if (mapSpec?.level === "symbol" && node.id === focus?.graph.focus) {
      openSource({ path: node.path, symbolId: mapSpec.id, line: node.line });
      return;
    }
    if (mapSpec?.level === "file" && node.id === focus?.graph.focus) {
      openSource({ path: node.path });
      return;
    }
    setMapSpec({ level: "file", id: pathOfFocusNode(node) });
  };
  const selectFocusNode = (node: MapNode | null) => {
    setSelectedFocusId(node?.id ?? null);
    if (!node) return;
    if (mapSpec?.level === "symbol" && node.id === focus?.graph.focus) {
      const symbol = nodesById.get(mapSpec.id);
      if (symbol) {
        setSelectedId(symbol.id);
        return;
      }
    }
    const fileNode = filesByPath.get(node.path) ?? fileNodeFor(node.path);
    remember(fileNode);
    setSelectedId(fileNode.id);
    updateLocation((next) => {
      next.set("file", fileNode.path);
      next.delete("symbol");
      next.delete("line");
      next.delete("end");
      next.delete("module");
    }, true);
  };
  const openFocusSymbol = (symbol: MapMemberSymbol) => {
    remember({
      id: symbol.id,
      label: symbol.label,
      qualified_name: symbol.qualified_name,
      path: symbol.path,
      kind: symbol.kind ?? "symbol",
      node_type: "symbol",
      line: symbol.line,
      end_line: symbol.end_line,
    });
    setMapSpec({ level: "symbol", id: symbol.id });
  };
  const openSource = (target: { path: string; symbolId?: string; line?: number }) => {
    const node = (target.symbolId && nodesById.get(target.symbolId)) || filesByPath.get(target.path) || fileNodeFor(target.path);
    openNode(node, target.line ? { start: target.line } : null);
  };

  // --------------------------------------------------------------- inspector
  const moduleSubject = useMemo<InspectorSubject | null>(() => {
    if (surface === "source" || selectedModuleId === null || !overviewWithChanges) return null;
    const module = overviewWithChanges.modules.find((item) => item.id === selectedModuleId);
    if (!module) return null;
    const label = (id: string) => overviewWithChanges.modules.find((item) => item.id === id)?.path || "(repository root)";
    const link = (id: string, edge: { weight: number; kinds: Record<string, number> }): ModuleLink => ({ id, label: label(id), weight: edge.weight, kinds: edge.kinds });
    return {
      kind: "module",
      module,
      usedBy: overviewWithChanges.edges.filter((edge) => edge.target === module.id).map((edge) => link(edge.source, edge)).sort((a, b) => b.weight - a.weight),
      uses: overviewWithChanges.edges.filter((edge) => edge.source === module.id).map((edge) => link(edge.target, edge)).sort((a, b) => b.weight - a.weight),
    };
  }, [overviewWithChanges, selectedModuleId, surface]);
  const inspectorSubject: InspectorSubject | null =
    (surface !== "source" && !mapSpec && moduleSubject) ||
    (selectedNode
      ? {
          kind: selectedNode.node_type === "file" ? "file" : "symbol",
          node: selectedNode,
          range: locationRange ?? (selectedNode.node_type !== "file" ? ([selectedNode.line, selectedNode.end_line] as const) : null),
        }
      : moduleSubject);

  // ------------------------------------------------------------------ actions
  const describeModule = useCallback(
    (id: string) => {
      if (!projectRoot) return;
      setPurposeBusy(id);
      setPurposeError(null);
      void api
        .codeMapModulePurpose(id, projectRoot)
        .then((result) => {
          const { purpose, purpose_kind, purpose_source, purpose_at, purpose_evidence } = result;
          const described = { purpose, purpose_kind, purpose_source, purpose_at, purpose_evidence };
          setOverview((current) => current && { ...current, modules: current.modules.map((row) => (row.id === id ? { ...row, ...described } : row)) });
        })
        .catch((caught: unknown) => setPurposeError({ module: id, message: errorText(caught, "Could not describe this module.") }))
        .finally(() => setPurposeBusy((current) => (current === id ? null : current)));
    },
    [projectRoot],
  );
  const openCodeAsk = useCallback((range?: readonly [number, number], selectedText?: string) => {
    if (!selectedNode || !codeAskTransport) return;
    askReturnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const selectedRange = range ?? locationRange ?? (selectedNode.node_type !== "file" ? ([selectedNode.line, selectedNode.end_line] as const) : null);
    setAskScope({
      path: selectedNode.path,
      symbol: selectedNode.node_type !== "file" ? selectedNode.qualified_name || selectedNode.label : "",
      start_line: selectedRange?.[0],
      end_line: selectedRange?.[1],
      selected_text: selectedText,
    });
    setAskOpen(true);
  }, [codeAskTransport, locationRange, selectedNode]);

  const closeCodeAsk = useCallback(() => {
    setAskOpen(false);
    const target = askReturnFocus.current;
    window.requestAnimationFrame(() => {
      if (target?.isConnected) target.focus();
      else document.querySelector<HTMLElement>('aside[aria-label="Inspector"] button[aria-label^="Ask about"]')?.focus();
    });
  }, []);

  useEffect(() => {
    if (!askOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) closeCodeAsk();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [askOpen, closeCodeAsk]);

  const openEditor = useCallback(() => {
    if (!selectedNode || !projectUrlReference) return Promise.reject(new Error("Open a file or symbol before launching the editor."));
    const line = requestedLine ?? (selectedNode.node_type !== "file" ? selectedNode.line : 1);
    return api.codeMapOpenEditor(projectUrlReference, selectedNode.path, line, 1, editorCapability ? selectedEditor(editorCapability)?.id : undefined);
  }, [editorCapability, projectUrlReference, requestedLine, selectedNode]);

  const openReview = useCallback(
    async (scope: ReviewScope | null) => {
      if (!projectUrlReference || reviewBusy) return;
      setReviewBusy(true);
      setReviewError(null);
      try {
        const review = await api.codeMapReview(projectUrlReference, scope ?? undefined);
        // The Review's Back returns to this exact Code location.
        navigate(withReturnPath(review.review_path, currentReturnPath(routerLocation)));
      } catch (caught: unknown) {
        setReviewError(errorText(caught, "Could not prepare the durable review."));
      } finally {
        setReviewBusy(false);
      }
    },
    [navigate, projectUrlReference, reviewBusy, routerLocation],
  );

  const followAvailable = Boolean(followedSession || activitySessionId);
  const liveSessionCount = activitySessions.filter((session) => session.live).length;
  const markInteraction = useCallback(() => {
    lastInteraction.current = Date.now();
  }, []);

  return (
    <section className="flex min-h-[calc(100vh-80px)] flex-col bg-surface-sunken text-neutral-200 xl:h-[calc(100dvh-41px)] xl:min-h-0 xl:overflow-hidden">
      <div className="border-b border-border bg-background/75 px-3 lg:px-4">
        <div className="flex h-10 items-center gap-1.5 overflow-x-auto whitespace-nowrap">
          {returnTo && (
            <UiButton type="button" size="xs" variant="ghost" className="shrink-0" onClick={() => navigate(returnTo)} title={returnLabel === "Review" ? "Back to the Review location you came from" : "Back to where you came from"}>
              <ArrowLeft size={11} /> {returnLabel}
            </UiButton>
          )}
          <div className="flex min-w-0 shrink items-center gap-2 text-caption">
            <Select
              className="max-w-72"
              contentClassName="min-w-72"
              uiSize="xs"
              value={selectedProject?.project_id || selectedProject?.root || ""}
              onValueChange={(value) => setSearchParams({ repo: value })}
              aria-label="Project and worktree"
              placeholder={projectLabel || "Select project and worktree"}
              searchable
              searchPlaceholder="Search projects"
              disabled={projects.length === 0}
            >
              {projects.map((project) => (
                <SelectItem key={project.project_id || project.root} value={project.project_id || project.root} title={project.root}>
                  {project.project || project.label} / {project.worktree || project.label} · {project.branch || "unknown branch"}
                  {!project.indexed ? " · index pending" : ""}
                </SelectItem>
              ))}
            </Select>
            <span className="shrink-0 text-muted-foreground">{files.length.toLocaleString()} files</span>
            {projectKnowledgeAvailable && (
              <UiButton type="button" size="xs" variant="ghost" className="shrink-0" onClick={() => navigate("/knowledge/project")}>
                Project Knowledge
              </UiButton>
            )}
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-1.5 text-caption">
            <div className="relative w-[280px] shrink-0">
              <Search size={12} className="pointer-events-none absolute left-2 top-2.5 text-muted-foreground" />
              <UiInput
                ref={searchInput}
                type="search"
                uiSize="xs"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (!searchResults.length) return;
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setActiveSearchIndex((index) => Math.min(index + 1, searchResults.length - 1));
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
                className="w-full pl-7 pr-12"
                aria-label="Find code"
                aria-controls={searchResults.length ? "code-search-results" : undefined}
              />
              {!query && (
                <span className="pointer-events-none absolute right-1.5 top-1.5 rounded border border-border px-1 text-micro text-muted-foreground">
                  {navigator.platform?.toLowerCase().includes("mac") ? "⌘K" : "Ctrl K"}
                </span>
              )}
              {searching && <Loader2 size={11} className="absolute right-2 top-2.5 animate-spin text-neutral-400" />}
              {query.trim().length >= 2 && !searching && searchError && (
                <div className="absolute right-0 top-9 z-30 w-full rounded-lg border border-red-900/60 bg-background px-3 py-2.5 text-label text-red-300 shadow-2xl">{searchError}</div>
              )}
              {query.trim().length >= 2 && !searching && !searchError && searchResults.length === 0 && (
                <div className="absolute right-0 top-9 z-30 w-full rounded-lg border border-border/80 bg-background px-3 py-2.5 text-label text-muted-foreground shadow-2xl">
                  No matching files, symbols, or text.
                </div>
              )}
              {searchResults.length > 0 && (
                <div id="code-search-results" className="absolute right-0 top-9 z-30 max-h-96 w-full overflow-y-auto rounded-lg border border-border/80 bg-background p-1 shadow-2xl">
                  {searchResults.map((result, index) => {
                    const label = result.type === "text" ? result.match.text || query.trim() : result.node.label;
                    const path = result.type === "text" ? result.match.path : result.node.path;
                    const line = result.type === "text" ? result.match.line : result.node.line;
                    const badge = result.type === "text" ? "text" : result.node.node_type === "file" ? "file" : result.node.kind;
                    return (
                      <UiButton
                        unstyled
                        key={result.id}
                        type="button"
                        className={`block w-full rounded-md px-2.5 py-2 text-left transition-colors hover:bg-muted ${index === activeSearchIndex ? "bg-muted" : ""}`}
                        onMouseEnter={() => setActiveSearchIndex(index)}
                        onClick={() => openSearchItem(result)}
                        aria-label={`${label} ${path}`}
                      >
                        <span className="flex items-center gap-2">
                          <FileTypeIcon path={path} size={13} />
                          <span className="min-w-0 flex-1 truncate text-xs text-foreground">{label}</span>
                          <span className="shrink-0 text-micro font-semibold uppercase tracking-wider text-muted-foreground">{badge}</span>
                        </span>
                        <span className="mt-0.5 block truncate pl-[21px] text-caption text-muted-foreground">
                          {path}
                          {result.type === "text" || result.node.node_type !== "file" ? `:${line}` : ""}
                        </span>
                      </UiButton>
                    );
                  })}
                </div>
              )}
            </div>

            <CodeChangesMenu
              changes={changes}
              marks={marks}
              reviewBusy={reviewBusy}
              reviewError={reviewError}
              onReview={reviewCaptureAvailable ? (scope) => void openReview(scope) : undefined}
              onFilter={(session) => setSessionFilter(session)}
            />
            <div className="flex h-7 shrink-0 overflow-hidden rounded-md border border-border/80 bg-background">
              <UiButton
                unstyled
                type="button"
                onClick={() => changeSurface("source")}
                aria-pressed={surface === "source"}
                style={{ borderRadius: 0 }}
                className={`inline-flex h-full items-center gap-1.5 border-0 px-2 ${surface === "source" ? "bg-accent text-foreground" : "text-neutral-400 hover:text-foreground"}`}
              >
                <FileCode2 size={11} /> Source
              </UiButton>
              <UiButton
                unstyled
                type="button"
                onClick={() => changeSurface("map")}
                aria-pressed={surface === "map"}
                style={{ borderRadius: 0 }}
                className={`inline-flex h-full items-center gap-1.5 border-0 border-l border-border px-2 ${surface === "map" ? "bg-accent text-foreground" : "text-neutral-400 hover:text-foreground"}`}
              >
                <Network size={11} /> Map
              </UiButton>
              <UiButton
                unstyled
                type="button"
                onClick={() => changeSurface("2d")}
                aria-pressed={surface === "2d"}
                style={{ borderRadius: 0 }}
                className={`inline-flex h-full items-center gap-1.5 border-0 border-l border-border px-2 ${surface === "2d" ? "bg-accent text-foreground" : "text-neutral-400 hover:text-foreground"}`}
              >
                <Share2 size={11} /> 2D
              </UiButton>
              <UiButton
                unstyled
                type="button"
                onClick={() => changeSurface("3d")}
                aria-pressed={surface === "3d"}
                style={{ borderRadius: 0 }}
                className={`inline-flex h-full items-center gap-1.5 border-0 border-l border-border px-2 ${surface === "3d" ? "bg-accent text-foreground" : "text-neutral-400 hover:text-foreground"}`}
              >
                <Boxes size={11} /> 3D
              </UiButton>
            </div>
            {codeAskTransport && (
              <UiButton
                type="button"
                size="xs"
                variant={askOpen ? "outline" : "ghost"}
                onClick={() => openCodeAsk()}
                disabled={!selectedNode}
                aria-pressed={askOpen}
                aria-label="Ask AI about code"
                tooltip={selectedNode ? "Ask AI about the selected file or symbol" : "Open a file or symbol to ask AI"}
                className="h-7 shrink-0"
              >
                <Sparkles size={12} /> Ask AI
              </UiButton>
            )}
            <UiButton
              unstyled
              type="button"
              onClick={() => setLiveEnabled((enabled) => !enabled)}
              className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border/80 bg-background px-2 text-caption tabular-nums text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-pressed={liveEnabled}
              aria-label={`Live activity: ${liveSessionCount} active ${liveSessionCount === 1 ? "session" : "sessions"}. Click to turn live overlay ${liveEnabled ? "off" : "on"}.`}
              title={activitySessions.length ? `${liveSessionCount} live of ${activitySessions.length} recent sessions` : "No agent session in this worktree yet"}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${liveEnabled && activityStatus !== "offline" && liveSessionCount > 0 ? "bg-emerald-400" : "bg-neutral-600"}`} />
              {liveSessionCount}
            </UiButton>
          </div>
        </div>
      </div>

      {error && (
        <div className="flex items-start justify-between border-b border-red-900/60 bg-red-950/20 px-4 py-2.5 text-label text-red-200">
          <span>{error}</span>
          <UiButton type="button" onClick={() => setError(null)} size="icon" variant="ghost" aria-label="Dismiss error">
            <X size={15} />
          </UiButton>
        </div>
      )}
      {loading ? (
        <div className="flex min-h-[560px] items-center justify-center gap-3 text-sm text-neutral-300">
          <Loader2 className="animate-spin text-brand-300" size={18} /> Listing files…
        </div>
      ) : !files.length && !error ? (
        <div className="flex min-h-[560px] items-center justify-center px-4">
          <div className="max-w-lg rounded-lg border border-border/70 bg-background/60 p-4 text-center">
            <Braces className="mx-auto text-brand-300" size={28} />
            <h3 className="mt-4 text-base font-semibold text-foreground">No files in this project yet</h3>
            <p className="mt-2 text-sm leading-6 text-neutral-300">Index the repository, then return here.</p>
            <UiButton type="button" size="xs" variant="outline" className="mt-3" onClick={() => setShowIgnored((value) => !value)}>{showIgnored ? "Hide Git ignored files" : "Show Git ignored files"}</UiButton>
            <code className="mt-4 inline-block rounded-md border border-border/70 bg-surface-code px-3 py-1.5 font-mono text-label text-emerald-300">{fullCode ? "lc code index" : "lc runtime index"}</code>
          </div>
        </div>
      ) : (
        <div style={{ "--explorer-width": `min(${explorerWidth}px, 45vw)` } as CSSProperties} className={`relative grid min-h-[650px] flex-1 grid-cols-1 xl:min-h-0 xl:overflow-hidden ${askOpen ? "xl:grid-cols-[var(--explorer-width)_minmax(0,1fr)_400px]" : "xl:grid-cols-[var(--explorer-width)_minmax(0,1fr)_320px]"}`}>
          <CodeExplorer
            key={projectUrlReference}
            files={files}
            symbolsByPath={symbolsByPath}
            selectedNode={selectedNode}
            changes={changes}
            changed={changedByPath}
            showIgnored={showIgnored}
            onShowIgnored={setShowIgnored}
            ignoredTruncated={ignoredTruncated}
            live={livePaths}
            activitySessions={activitySessions}
            marks={marks}
            sessionFilter={sessionFilter}
            onSessionFilter={setSessionFilter}
            onSelect={(node) => openNode(node)}
            hotspots={hotspots}
            liveWorkspace={liveWorkspace}
            onResizeWidth={setExplorerWidth}
          />
          {surface === "source" ? (
            <CodeSourceView
              projectRoot={projectRoot ?? ""}
              selectedNode={selectedNode}
              locationRange={locationRange}
              events={events}
              change={selectedNode ? changedByPath.get(selectedNode.path) : undefined}
              editorCapability={editorCapability}
              onOpenLine={openLine}
              onOpenEditor={openEditor}
              onAskSelection={codeAskAvailable ? (range, text) => openCodeAsk(range, text) : undefined}
            />
          ) : surface === "map" ? (
            <CodeMapView
              spec={mapSpec}
              crumbs={crumbs}
              overview={overviewWithChanges}
              overviewLoading={overviewLoading}
              overviewError={overviewError}
              focus={focus}
              focusLoading={focusLoading}
              focusError={focusError}
              selectedModuleId={selectedModuleId}
              selectedFocusId={selectedFocusId}
              live={liveMarks}
              follow={follow}
              followAvailable={followAvailable}
              onFollow={setFollow}
              onRetry={() => setMapReload((value) => value + 1)}
              onSpec={(spec) => setMapSpec(spec)}
              onSelectModule={(id) => {
                if (id && id.startsWith("fold:")) return;
                selectModule(id);
              }}
              onSelectFocusNode={selectFocusNode}
              onOpenFocusNode={openFocusNode}
              onOpenSymbol={openFocusSymbol}
              onOpenSource={openSource}
              onInteract={markInteraction}
            />
          ) : (
            <CodeTopologyView
              mode={surface}
              cacheKey={projectUrlReference ?? projectRoot ?? "repository"}
              spec={mapSpec}
              overview={overviewWithChanges}
              overviewLoading={overviewLoading}
              overviewError={overviewError}
              focus={focus}
              focusLoading={focusLoading}
              focusError={focusError}
              selectedModuleId={selectedModuleId}
              selectedFocusId={selectedFocusId}
              live={liveMarks}
              onRetry={() => setMapReload((value) => value + 1)}
              onSpec={(spec) => setMapSpec(spec)}
              onSelectModule={selectModule}
              onSelectFocusNode={selectFocusNode}
              onOpenFocusNode={openFocusNode}
              onInteract={markInteraction}
            />
          )}
          {askOpen && codeAskTransport ? (
            <div className="absolute inset-y-0 right-0 z-30 w-[min(400px,92vw)] min-h-0 border-l border-border bg-background shadow-[-18px_0_40px_rgba(0,0,0,0.3)] xl:static xl:w-auto xl:shadow-none">
              <Suspense fallback={null}>
                <ReviewAskDrawer
                  reviewId={projectUrlReference ?? ""}
                  revisionNumber={0}
                  latestRevisionNumber={0}
                  filePaths={files.map((file) => file.path)}
                  scope={askScope}
                  onClearScope={() => setAskScope((current) => current?.path ? { path: current.path } : null)}
                  onClose={closeCodeAsk}
                  onSwitchLatest={() => {}}
                  onOpenReference={(reference) => { if (reference.path) openPath(reference.path, reference.start_line); }}
                  transport={codeAskTransport}
                />
              </Suspense>
            </div>
          ) : <CodeInspector
            subject={inspectorSubject}
            relationships={inspectorSubject?.kind === "module" ? null : relationships}
            relationshipsLoading={relationshipsLoading}
            relationshipsError={relationshipsError}
            references={references}
            referencesAvailable={fullCode}
            referencesLoading={referencesLoading}
            referencesError={referencesError}
            events={events}
            activityHost={liveEnabled ? activityHost : null}
            change={selectedNode ? changedByPath.get(selectedNode.path) : undefined}
            changes={changes}
            marks={marks}
            reviewBusy={reviewBusy}
            reviewError={reviewError}
            onOpenPath={openPath}
            onOpenReference={openReference}
            onOpenActivity={openActivity}
            onSelectModule={(id) => selectModule(id)}
            onFocusModule={(id) => setMapSpec({ level: "module", id })}
            onReview={reviewCaptureAvailable ? (scope) => void openReview(scope) : undefined}
            hotspot={selectedNode ? hotspotByPath.get(selectedNode.path) : undefined}
            hotspotDays={hotspots?.days}
            purposeModel={fullCode && overview?.purpose_generation?.available ? overview.purpose_generation.model : ""}
            purposeBusy={inspectorSubject?.kind === "module" && purposeBusy === inspectorSubject.module.id}
            purposeError={inspectorSubject?.kind === "module" && purposeError?.module === inspectorSubject.module.id ? purposeError.message : null}
            onDescribeModule={fullCode ? describeModule : undefined}
            onAsk={codeAskAvailable ? () => openCodeAsk() : undefined}
          />}
        </div>
      )}
    </section>
  );
}
