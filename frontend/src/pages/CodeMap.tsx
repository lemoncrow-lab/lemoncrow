import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Braces,
  Check,
  CircleDot,
  Expand,
  FileCode2,
  GitFork,
  Layers3,
  Loader2,
  Maximize2,
  Search,
  X,
} from "lucide-react";
import CodeGraph from "../components/CodeGraph";
import {
  api,
  type CodeMapActivityEvent,
  type CodeMapActivityKind,
  type CodeMapFacet,
  type CodeMapFull,
  type CodeMapGraph,
  type CodeMapNode,
  type CodeMapProject,
  type CodeMapSymbol,
} from "../api";

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

export default function CodeMap() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedProject = searchParams.get("repo") || undefined;
  const [projects, setProjects] = useState<CodeMapProject[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [full, setFull] = useState<CodeMapFull | null>(null);
  const [focusGraph, setFocusGraph] = useState<CodeMapGraph>(EMPTY_GRAPH);
  const [viewMode, setViewMode] = useState<"full" | "focus">("full");
  const [selectedCommunities, setSelectedCommunities] = useState<Set<string>>(
    new Set()
  );
  const [selectedFileTypes, setSelectedFileTypes] = useState<Set<string>>(
    new Set()
  );
  const [selectedLanguage, setSelectedLanguage] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CodeMapSymbol | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CodeMapNode[]>([]);
  const [searching, setSearching] = useState(false);
  const [loading, setLoading] = useState(true);
  const [expandingId, setExpandingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [activityStatus, setActivityStatus] = useState("idle");
  const [events, setEvents] = useState<CodeMapActivityEvent[]>([]);
  const [activityByNode, setActivityByNode] = useState<
    Record<string, CodeMapActivityKind>
  >({});
  const [followNodeId, setFollowNodeId] = useState<string | null>(null);
  const activityCursor = useRef<string | null>(null);
  const activitySession = useRef<string | null>(null);
  const lastActiveNode = useRef<string | null>(null);
  const graphNodes = useRef<Map<string, CodeMapNode>>(new Map());
  const highlightTimers = useRef<number[]>([]);

  const selectedProjectRoot =
    requestedProject ||
    projects.find((project) => project.active && project.indexed)?.root ||
    projects.find((project) => project.indexed)?.root;
  const projectRoot = full?.project.root || selectedProjectRoot;
  const baseGraph =
    viewMode === "full" ? (full?.graph ?? EMPTY_GRAPH) : focusGraph;
  const activeIds = useMemo(
    () => new Set(Object.keys(activityByNode)),
    [activityByNode]
  );
  const graph = useMemo<CodeMapGraph>(() => {
    if (viewMode === "focus") return baseGraph;
    const allCommunitiesSelected =
      !full?.communities.length ||
      selectedCommunities.size === full.communities.length;
    const allFileTypesSelected =
      !full?.file_types.length ||
      selectedFileTypes.size === full.file_types.length;
    if (
      allCommunitiesSelected &&
      allFileTypesSelected &&
      selectedLanguage === "all"
    ) {
      return baseGraph;
    }
    const nodes = baseGraph.nodes.filter((node) => {
      if (activeIds.has(node.id)) return true;
      const communityMatch =
        !full?.communities.length ||
        selectedCommunities.has(node.community || "root");
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
    full?.communities.length,
    full?.file_types.length,
    selectedCommunities,
    selectedFileTypes,
    selectedLanguage,
    viewMode,
  ]);

  const selectedNode = useMemo(
    () =>
      baseGraph.nodes.find((node) => node.id === selectedId) ??
      full?.graph.nodes.find((node) => node.id === selectedId) ??
      null,
    [baseGraph.nodes, full?.graph.nodes, selectedId]
  );

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
          "Source-map API unavailable. Restart the dashboard service, then reopen Map."
        );
      })
      .finally(() => setProjectsLoaded(true));
  }, []);

  useEffect(() => {
    if (requestedProject || !projectsLoaded || selectedProjectRoot) return;
    setLoading(false);
    setFull(null);
    setError(
      projectsError ||
        "No indexed project is available in this dashboard session."
    );
  }, [projectsError, projectsLoaded, requestedProject, selectedProjectRoot]);

  useEffect(() => {
    if (!selectedProjectRoot) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    setSelectedId(null);
    setViewMode("full");
    activityCursor.current = null;
    activitySession.current = null;
    setEvents([]);
    void api
      .codeMapFull(selectedProjectRoot)
      .then((payload) => {
        if (cancelled) return;
        setFull(payload);
        setFocusGraph(EMPTY_GRAPH);
        setSelectedCommunities(
          new Set(payload.communities.map((facet) => facet.id))
        );
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
  }, [selectedProjectRoot]);

  useEffect(() => {
    graphNodes.current = new Map(
      [...(full?.graph.nodes ?? []), ...focusGraph.nodes].map((node) => [
        node.id,
        node,
      ])
    );
  }, [focusGraph.nodes, full?.graph.nodes]);

  useEffect(() => {
    const clean = query.trim();
    if (clean.length < 2 || !projectRoot) {
      setResults([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      void api
        .codeMapSearch(clean, projectRoot, 20)
        .then((payload) => {
          if (!cancelled) setResults(payload.results);
        })
        .catch(() => {
          if (!cancelled) setResults([]);
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

  const selectNode = useCallback((nodeId: string) => {
    setSelectedId((current) => {
      if (current !== nodeId) setDetail(null);
      return nodeId;
    });
  }, []);

  useEffect(() => {
    if (!selectedNode || selectedNode.node_type === "file" || !projectRoot) {
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
  }, [projectRoot, selectedNode]);

  const focusNode = useCallback(
    (nodeId: string) => {
      const node = graphNodes.current.get(nodeId);
      setSelectedId(nodeId);
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
    [projectRoot]
  );

  const openSearchResult = useCallback(
    (node: CodeMapNode) => {
      setQuery("");
      setResults([]);
      setSelectedId(node.id);
      setDetail(null);
      if (full?.graph.nodes.some((candidate) => candidate.id === node.id)) {
        setViewMode("full");
        setFollowNodeId(node.id);
      } else {
        focusNode(node.id);
      }
    },
    [focusNode, full?.graph.nodes]
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
          if (!targets.length) {
            targets = [...graphNodes.current.values()]
              .filter(
                (node) =>
                  Boolean(event.path && event.path === node.path) ||
                  Boolean(
                    event.query &&
                    `${node.label} ${node.qualified_name}`
                      .toLowerCase()
                      .includes(event.query.toLowerCase())
                  )
              )
              .slice(0, 6)
              .map((node) => node.id);
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
  const callEdges = baseGraph.edges.filter((edge) => edge.kind === "calls");
  const incomingCount = selectedId
    ? callEdges.filter((edge) => edge.target === selectedId).length
    : 0;
  const outgoingCount = selectedId
    ? callEdges.filter((edge) => edge.source === selectedId).length
    : 0;

  return (
    <section className="min-h-[calc(100vh-150px)] bg-surface-sunken text-neutral-200">
      <div className="border-b border-neutral-800 bg-neutral-950/75 px-4 py-4 md:px-6">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="border border-brand-500/40 bg-brand-500/10 p-2 text-brand-300">
              <GitFork size={18} />
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="text-lg font-semibold text-neutral-100">
                  Source map
                </h2>
                <span className="truncate text-xs text-neutral-400">
                  {full?.project.label ?? "local index"}
                </span>
              </div>
              <p className="mt-1 text-xs text-neutral-300">
                Every tracked file, indexed symbol, and uniquely resolved call.
                Source stays local.
              </p>
            </div>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            {projects.length > 1 && (
              <select
                className="h-10 max-w-64 border border-neutral-700 bg-neutral-900 px-3 text-xs text-neutral-200"
                value={projectRoot ?? ""}
                onChange={(event) =>
                  setSearchParams({ repo: event.target.value })
                }
                aria-label="Indexed project"
              >
                {projects.map((project) => (
                  <option key={project.root} value={project.root}>
                    {project.label}
                    {project.active ? " · active" : ""}
                  </option>
                ))}
              </select>
            )}
            <div className="relative w-full sm:w-[360px]">
              <Search
                size={15}
                className="pointer-events-none absolute left-3 top-3 text-neutral-400"
              />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Find a symbol…"
                className="h-10 w-full border border-neutral-700 bg-neutral-900 pl-9 pr-9 text-sm text-neutral-100 placeholder:text-neutral-400 focus:border-brand-500"
                aria-label="Find a symbol"
              />
              {searching && (
                <Loader2
                  size={14}
                  className="absolute right-3 top-3 animate-spin text-brand-300"
                />
              )}
              {results.length > 0 && (
                <div className="absolute right-0 top-11 z-30 max-h-80 w-full overflow-y-auto border border-neutral-700 bg-neutral-950 shadow-2xl">
                  {results.map((result) => (
                    <button
                      key={result.id}
                      type="button"
                      className="block w-full border-b border-neutral-800 px-3 py-3 text-left transition last:border-0 hover:bg-neutral-900"
                      onClick={() => openSearchResult(result)}
                      aria-label={`${result.label} ${result.path}`}
                    >
                      <span className="block truncate text-sm text-neutral-100">
                        {result.label}
                      </span>
                      <span className="mt-1 block truncate text-[11px] text-neutral-400">
                        {result.path}:{result.line} · {result.kind}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 text-[11px] text-neutral-300">
          <span>
            {(full?.total_symbols ?? 0).toLocaleString()} indexed symbols
          </span>
          <span>·</span>
          <span>{(full?.total_files ?? 0).toLocaleString()} tracked files</span>
          <span>·</span>
          <span>{graph.nodes.length.toLocaleString()} map nodes</span>
          <span>·</span>
          <span>
            {graph.edges
              .filter((edge) => edge.kind === "calls")
              .length.toLocaleString()}{" "}
            calls
          </span>
          {full?.truncated && (
            <span className="text-amber-200">bounded by safety limit</span>
          )}
          <div className="ml-auto flex border border-neutral-700 bg-neutral-900/70">
            <button
              type="button"
              onClick={() => setViewMode("full")}
              aria-pressed={viewMode === "full"}
              className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 ${viewMode === "full" ? "bg-neutral-700 text-white" : "text-neutral-300"}`}
            >
              <Maximize2 size={12} /> Full
            </button>
            <button
              type="button"
              disabled={!focusGraph.nodes.length}
              onClick={() => setViewMode("focus")}
              aria-pressed={viewMode === "focus"}
              className={`inline-flex items-center gap-1.5 border-l border-neutral-700 px-2.5 py-1.5 disabled:opacity-40 ${viewMode === "focus" ? "bg-neutral-700 text-white" : "text-neutral-300"}`}
            >
              <Layers3 size={12} /> Focus
            </button>
          </div>
          <button
            type="button"
            onClick={() => setLiveEnabled((enabled) => !enabled)}
            className="inline-flex items-center gap-2 border border-neutral-700 bg-neutral-900/70 px-2.5 py-1.5 text-neutral-200 hover:border-neutral-500"
            aria-pressed={liveEnabled}
          >
            <span
              className={`h-2 w-2 rounded-full ${liveEnabled && activityStatus !== "offline" ? "bg-emerald-400 shadow-[0_0_10px_#4ade80]" : "bg-neutral-500"}`}
            />{" "}
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
          <Loader2 className="animate-spin text-brand-300" size={18} /> Building
          the local source map…
        </div>
      ) : !full?.graph.nodes.length ? (
        <div className="flex min-h-[560px] items-center justify-center px-6">
          <div className="max-w-lg border border-neutral-800 bg-neutral-950/70 p-8 text-center">
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
      ) : (
        <div className="grid min-h-[650px] grid-cols-1 xl:grid-cols-[270px_minmax(0,1fr)_340px]">
          <aside className="order-2 border-t border-neutral-800 bg-neutral-950/55 xl:order-1 xl:border-r xl:border-t-0">
            <FacetList
              title="Communities"
              facets={full.communities}
              selected={selectedCommunities}
              onToggle={(id) => toggleFacet(setSelectedCommunities, id)}
              onAll={() =>
                toggleAll(
                  setSelectedCommunities,
                  full.communities,
                  selectedCommunities
                )
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
                    return (
                      <li
                        key={event.id}
                        className="border border-neutral-800 bg-neutral-900/35 p-2"
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
            <CodeGraph
              nodes={graph.nodes}
              edges={graph.edges}
              selectedId={selectedId}
              activityByNode={activityByNode}
              followNodeId={liveEnabled ? followNodeId : null}
              onSelect={selectNode}
              onExpand={focusNode}
            />
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
