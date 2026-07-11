import {
  useEffect,
  useState,
  useRef,
  useCallback,
  useMemo,
  type ReactNode,
} from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import {
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  Terminal,
  X,
} from "lucide-react";
import { api, type Trace, type SessionSummary } from "../api";
import { MetricCard, SectionHeader, cx } from "../components/WorkbenchUI";
import { fmtUsd, fmtDate, fmtDuration, fmtRelativeTime } from "../lib/format";
import { extractHost, HOST_COLORS } from "./sessions/helpers";
import { StatusDot } from "./sessions/StatusBadge";
import { SessionExplorerDetail } from "./sessions/SessionDetail";

// ---------------------------------------------------------------------------
// Highlight search terms in text (JSX — lives here, not in helpers.ts)
// ---------------------------------------------------------------------------

function highlightSearchText(value: string, query: string): ReactNode {
  if (!value || !query.trim()) return value;
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return value;
  const pattern = terms
    .slice()
    .sort((l, r) => r.length - l.length)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  if (!pattern) return value;
  const matcher = new RegExp(`(${pattern})`, "gi");
  return value.split(matcher).map((part, i) =>
    terms.includes(part.toLowerCase()) ? (
      <mark
        key={i}
        className="bg-brand-500/30 text-brand-200 rounded-[1px] px-0.5 border border-brand-500/20"
      >
        {part}
      </mark>
    ) : (
      <span key={i}>{part}</span>
    )
  );
}

function firstModelLabel(models?: Record<string, number>): string | null {
  if (!models) return null;
  const [firstModel] = Object.keys(models);
  return firstModel || null;
}

function resolveSessionModel(
  summary?: SessionSummary | null,
  trace?: Trace | null
): string | null {
  return (
    summary?.started_model ||
    firstModelLabel(summary?.models_used) ||
    trace?.model ||
    null
  );
}

// Latest activity timestamp for a session — prefers session-summary fields
// (ended_at → started_at) and falls back to the head-trace created_at.
function latestActivityMs(
  trace: Trace,
  summary?: SessionSummary | null
): number {
  const candidate =
    summary?.ended_at ||
    summary?.updated_at ||
    summary?.started_at ||
    trace.created_at;
  const ts = candidate ? Date.parse(candidate) : NaN;
  return Number.isFinite(ts) ? ts : 0;
}

// ---------------------------------------------------------------------------
// Main Sessions page — sidebar list + master-detail routing
// ---------------------------------------------------------------------------

// History sidebar is intentionally independent of the global time range —
// show everything we have, newest activity first.
const SESSIONS_SINCE_ALL = "36500d";

export default function Sessions() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [traces, setTraces] = useState<Trace[] | null>(null);
  const [loadingTraces, setLoadingTraces] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState(searchParams.get("q") ?? "");
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const tracesRequestSeq = useRef(0);
  const [summaries, setSummaries] = useState<SessionSummary[] | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [hostFilter, setHostFilter] = useState("all");
  const [workspaceFilter, setWorkspaceFilter] = useState("all");
  const [availableHosts, setAvailableHosts] = useState<string[]>([]);
  const [availableWorkspaces, setAvailableWorkspaces] = useState<string[]>([]);

  // Pre-compute a summary lookup map to eliminate repeated .find() calls
  // in the sort comparator and inside each map callback — O(m + n) instead of
  // O(m · n).
  const sessionsMap = useMemo(() => {
    if (!summaries) return null;
    const m = new Map<string, SessionSummary>();
    for (const s of summaries) m.set(s.session_id, s);
    return m;
  }, [summaries]);

  // Deduplicate traces by session_id — one entry per real session. The
  // workspace filter is now applied server-side (api.traces()'s workspace
  // param), covering full history instead of just this loaded page.
  // Prefer traces that have a task description when multiple share the same session_id.
  const displayTraces = useMemo(() => {
    if (!traces) return null;
    const seen = new Map<string, Trace>();
    for (const t of traces) {
      const sid = t.session_id || t.id;
      const existing = seen.get(sid);
      if (!existing || (!existing.task && t.task)) seen.set(sid, t);
    }
    return Array.from(seen.values());
  }, [traces]);

  const fetchTracesPage = useCallback(
    (offset: number) => {
      const requestSeq = ++tracesRequestSeq.current;
      setLoadingTraces(true);
      setErr(null);
      api
        .traces(50, offset, "all", hostFilter, workspaceFilter, query)
        .then((res) => {
          if (requestSeq !== tracesRequestSeq.current) return;
          // Defensive: older/mismatched backends may omit metrics facets —
          // never let a missing wire field crash the render.
          setAvailableHosts(res.metrics?.hosts ?? []);
          setAvailableWorkspaces(res.metrics?.workspaces ?? []);
          const items = res.items ?? [];
          if (offset === 0) {
            setTraces(items);
            setHasMore(items.length >= 50);
            setPage(0);
          } else {
            setTraces((prev) => (prev ? [...prev, ...items] : items));
            setHasMore(items.length >= 50);
            setPage(offset / 50);
          }
          setLoadingTraces(false);
        })
        .catch((e) => {
          if (requestSeq !== tracesRequestSeq.current) return;
          setErr(String(e));
          setLoadingTraces(false);
        });
    },
    [query, hostFilter, workspaceFilter]
  );

  const fetchSummaries = useCallback(() => {
    api
      .sessions(SESSIONS_SINCE_ALL)
      .then(setSummaries)
      .catch(() => null);
  }, []);

  // Debounce search input → query
  useEffect(() => {
    const timer = setTimeout(() => {
      const nextQuery = searchInput.trim();
      setQuery(nextQuery);
      const next = new URLSearchParams(searchParams);
      if (nextQuery) next.set("q", nextQuery);
      else next.delete("q");
      setSearchParams(next, { replace: true });
    }, 300);
    return () => clearTimeout(timer);
  }, [searchInput, setSearchParams, searchParams]);

  // Fetch traces on query/host/workspace filter change — no date filter; history is unbounded.
  useEffect(() => {
    fetchTracesPage(0);
  }, [query, hostFilter, workspaceFilter, fetchTracesPage]);

  // Fetch session summaries for cost/token stats in sidebar cards.
  // Use a very large window so the History list is independent of the
  // global date selector.
  useEffect(() => {
    fetchSummaries();
  }, [fetchSummaries]);

  const loadMore = () => {
    if (loadingTraces || !hasMore) return;
    fetchTracesPage((page + 1) * 50);
  };

  // Auto-refresh must replace the currently loaded range in place —
  // appending via fetchTracesPage's offset paging (meant for user-initiated
  // Load More) would duplicate the last page's items on every 30s tick.
  const refreshTraces = useCallback(() => {
    const requestSeq = ++tracesRequestSeq.current;
    const loadedCount = (page + 1) * 50;
    setErr(null);
    api
      .traces(loadedCount, 0, "all", hostFilter, workspaceFilter, query)
      .then((res) => {
        if (requestSeq !== tracesRequestSeq.current) return;
        setAvailableHosts(res.metrics?.hosts ?? []);
        setAvailableWorkspaces(res.metrics?.workspaces ?? []);
        const items = res.items ?? [];
        setTraces(items);
        setHasMore(items.length >= loadedCount);
      })
      .catch((e) => {
        if (requestSeq !== tracesRequestSeq.current) return;
        setErr(String(e));
      });
  }, [query, page, hostFilter, workspaceFilter]);

  const refresh = useCallback(() => {
    refreshTraces();
    fetchSummaries();
  }, [refreshTraces, fetchSummaries]);

  // Periodically refresh data every 30 seconds
  useEffect(() => {
    const interval = setInterval(refresh, 30000);
    return () => clearInterval(interval);
  }, [refresh]);

  return (
    <div className="flex flex-col md:flex-row h-[calc(100vh-180px)] overflow-hidden border border-neutral-800/80 bg-surface-sunken">
      {/* Sidebar — master list */}
      <aside
        className={cx(
          "flex-shrink-0 flex flex-col border-b md:border-b-0 md:border-r border-neutral-800 bg-surface transition-all duration-200 ease-in-out overflow-hidden",
          sidebarCollapsed ? "w-full md:w-12" : "w-full md:w-80"
        )}
      >
        {sidebarCollapsed ? (
          /* ── Collapsed: narrow strip ── */
          <div className="flex flex-col items-center py-3 gap-4 flex-shrink-0">
            <button
              type="button"
              onClick={() => setSidebarCollapsed(false)}
              className="w-6 h-6 flex items-center justify-center text-neutral-400 hover:text-neutral-300 transition-colors rounded-full hover:bg-neutral-800"
              title="Expand sidebar"
            >
              <ChevronRight size={14} />
            </button>
            <span className="text-[10px] font-bold uppercase tracking-[0.3em] text-neutral-400 [writing-mode:vertical-lr]">
              History
            </span>
          </div>
        ) : (
          /* ── Expanded: full sidebar ── */
          <>
            <div className="p-4 border-b border-neutral-800 space-y-4 bg-surface-raised">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <h2 className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 whitespace-nowrap">
                    History
                  </h2>
                  <span className="text-[10px] text-neutral-400 shrink-0">
                    All time
                  </span>
                  {loadingTraces && (
                    <span className="text-[10px] text-brand-400 shrink-0">
                      Scanning...
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={refresh}
                    className="w-5 h-5 flex items-center justify-center text-neutral-400 hover:text-neutral-300 transition-colors shrink-0 rounded hover:bg-neutral-800"
                    title="Refresh sessions"
                  >
                    <RefreshCw size={14} />
                  </button>{" "}
                  <button
                    type="button"
                    onClick={() => setSidebarCollapsed(true)}
                    className="w-5 h-5 flex items-center justify-center text-neutral-400 hover:text-neutral-300 transition-colors shrink-0 rounded hover:bg-neutral-800"
                    title="Collapse sidebar"
                  >
                    <ChevronLeft size={14} />
                  </button>
                </div>
              </div>
              <div className="flex gap-2">
                <input
                  type="search"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search sessions, tasks, models..."
                  className="w-full bg-surface-overlay border border-neutral-800 px-3 py-2.5 text-xs text-neutral-200 outline-none focus:border-brand-600 transition-all rounded-sm shadow-inner"
                />
                {searchInput && (
                  <button
                    type="button"
                    onClick={() => setSearchInput("")}
                    className="px-2 border border-neutral-800 text-neutral-400 hover:text-neutral-300 transition-colors"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
              <div className="flex gap-2">
                <select
                  aria-label="Filter by host"
                  value={hostFilter}
                  onChange={(e) => setHostFilter(e.target.value)}
                  className="flex-1 min-w-0 bg-surface-overlay border border-neutral-800 px-2 py-1.5 text-[10px] text-neutral-300 outline-none focus:border-brand-600"
                >
                  <option value="all">All hosts</option>
                  {availableHosts.map((h) => (
                    <option key={h} value={h}>
                      {h}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="Filter by workspace"
                  value={workspaceFilter}
                  onChange={(e) => setWorkspaceFilter(e.target.value)}
                  className="flex-1 min-w-0 bg-surface-overlay border border-neutral-800 px-2 py-1.5 text-[10px] text-neutral-300 outline-none focus:border-brand-600"
                >
                  <option value="all">All workspaces</option>
                  {availableWorkspaces.map((w) => (
                    <option key={w} value={w} title={w}>
                      {w.split("/").pop() || w}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {err && (
                <div className="p-4 text-xs text-red-300 font-mono">{err}</div>
              )}

              {displayTraces
                ?.slice()
                .sort((a, b) => {
                  const sa = sessionsMap?.get(a.session_id || a.id);
                  const sb = sessionsMap?.get(b.session_id || b.id);
                  return latestActivityMs(b, sb) - latestActivityMs(a, sa);
                })
                .map((t) => {
                  const sid = t.session_id || t.id;
                  const isActive = id === sid;
                  const summary = sessionsMap?.get(sid);
                  const sessionModel = resolveSessionModel(summary, t);
                  const host = extractHost(t);
                  const hostTextClass =
                    HOST_COLORS[host]?.split(" ")[1] || "text-neutral-400";
                  // _live sessions are still running in the RunLedger and
                  // haven't committed a final status to SQLite yet.
                  const displayStatus = t._live ? "running" : t.status;
                  const costPrefix =
                    summary?.cost_status === "estimated" ? "~" : "";

                  return (
                    <button
                      key={t.id}
                      onClick={() =>
                        navigate(
                          `/sessions/${sid}${query ? `?q=${encodeURIComponent(query)}` : ""}`
                        )
                      }
                      className={cx(
                        "w-full border-b border-neutral-800 p-3.5 text-left transition-all hover:bg-neutral-800/40 group/card",
                        isActive
                          ? "bg-brand-900/10 border-r-2 border-r-brand-500 shadow-[inset_0_0_28px_rgba(168,85,247,0.08)]"
                          : ""
                      )}
                    >
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <div className="flex min-w-0 items-center gap-2">
                          <StatusDot
                            status={displayStatus}
                            className="shrink-0"
                          />
                          <span
                            className={cx(
                              "shrink-0 text-[10px] font-mono uppercase tracking-[0.18em]",
                              hostTextClass
                            )}
                            title={host}
                          >
                            {host}
                          </span>
                          {sessionModel && (
                            <span
                              className="min-w-0 truncate text-[10px] font-mono text-sky-200"
                              title={sessionModel}
                            >
                              {sessionModel}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className="shrink-0 text-[10px] font-mono text-neutral-400"
                            title={fmtDate(t.created_at)}
                          >
                            {fmtRelativeTime(t.created_at)}
                          </span>
                          <span
                            className="shrink-0 text-[10px] font-mono text-neutral-400"
                            title={
                              summary
                                ? String(
                                    summary.active_duration_seconds ||
                                      summary.duration_seconds
                                  )
                                : undefined
                            }
                          >
                            {summary
                              ? fmtDuration(
                                  summary.active_duration_seconds ||
                                    summary.duration_seconds
                                )
                              : "—"}
                          </span>
                        </div>
                      </div>

                      <p
                        className={cx(
                          "mb-2 text-xs font-mono line-clamp-2 leading-relaxed",
                          isActive
                            ? "text-neutral-100 font-bold"
                            : "text-neutral-400 group-hover/card:text-neutral-300"
                        )}
                      >
                        {highlightSearchText(t.task || "Untitled Task", query)}
                      </p>

                      <div className="grid grid-cols-2 gap-1.5 rounded-sm border border-neutral-800/60 bg-surface-sunken/70 p-2">
                        {(
                          [
                            [
                              "Cost",
                              summary
                                ? `${costPrefix}${fmtUsd(summary.total_cost_usd)}`
                                : "—",
                              "text-red-300",
                            ],
                            [
                              "Saved",
                              summary
                                ? fmtUsd(summary.total_lemoncrow_savings_usd)
                                : "—",
                              "text-emerald-300",
                            ],
                          ] as [string, string, string][]
                        ).map(([label, value, valCls]) => (
                          <div
                            key={label}
                            className="flex items-center justify-between gap-2 rounded-sm border border-neutral-800/50 bg-neutral-950/40 px-2 py-1.5"
                          >
                            <div className="truncate text-[10px] font-mono tracking-[0.18em] uppercase text-neutral-400 leading-none">
                              {label}
                            </div>
                            <div
                              className={cx(
                                "shrink-0 text-[10px] font-black font-mono leading-none",
                                valCls
                              )}
                            >
                              {value}
                            </div>
                          </div>
                        ))}
                      </div>
                    </button>
                  );
                })}

              {!loadingTraces && hasMore && traces && (
                <button
                  onClick={loadMore}
                  className="w-full p-4 text-[10px] text-neutral-400 hover:text-neutral-200 uppercase tracking-widest font-bold"
                >
                  Load More
                </button>
              )}

              {!loadingTraces && traces?.length === 0 && (
                <div className="p-12 text-center text-xs text-neutral-400 italic font-mono">
                  No sessions found
                </div>
              )}
            </div>
          </>
        )}
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-hidden">
        {id ? (
          <SessionExplorerDetail sessionId={id} />
        ) : (
          <EmptyState summaries={summaries} />
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state — shown when no session is selected
// ---------------------------------------------------------------------------

function EmptyState({ summaries }: { summaries: SessionSummary[] | null }) {
  const totalCost = summaries?.reduce((s, i) => s + i.total_cost_usd, 0) ?? 0;
  const totalSaved =
    summaries?.reduce((s, i) => s + i.total_lemoncrow_savings_usd, 0) ?? 0;

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-12 space-y-12 animate-in fade-in slide-in-from-bottom-4 duration-700">
      <SectionHeader
        title="Session Explorer"
        description="Deep dive into agent execution, reasoning, and costs."
      />

      <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
        <MetricCard
          label="Sessions"
          value={summaries ? String(summaries.length) : "—"}
          tone="violet"
        />
        <MetricCard
          label="Total Cost"
          value={summaries ? fmtUsd(totalCost) : "—"}
          tone="amber"
        />
        <MetricCard
          label="Savings"
          value={summaries ? fmtUsd(totalSaved) : "—"}
          tone="emerald"
        />
        <MetricCard
          label="Efficiency"
          value={
            summaries
              ? `${Math.round((totalSaved / (totalCost || 1)) * 100)}%`
              : "—"
          }
          tone="neutral"
        />
      </div>

      <div className="border border-neutral-800 bg-surface-raised p-16 text-center rounded-sm">
        <Terminal size={48} className="mx-auto mb-6 text-neutral-400" />
        <h3 className="text-xs font-bold text-neutral-400 mb-2 uppercase tracking-[0.4em]">
          Select History
        </h3>
        <p className="text-xs text-neutral-400 max-w-sm mx-auto leading-relaxed">
          Explore the internal reasoning logs, tool executions, and file diffs
          for any past agent run.
        </p>
      </div>
    </div>
  );
}
