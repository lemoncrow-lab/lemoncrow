import { Select, SelectItem } from "../components/ui/select";
import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
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
import { api, type TraceSummary, type SessionSummary } from "../api";
import { MetricCard, cx } from "../components/WorkbenchUI";
import { fmtUsd, fmtDate, fmtDuration, fmtRelativeTime } from "../lib/format";
import { setVisibleInterval } from "../lib/visibleInterval";
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
  trace?: TraceSummary | null
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
  trace: TraceSummary,
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

  const [traces, setTraces] = useState<TraceSummary[] | null>(null);
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
    const seen = new Map<string, TraceSummary>();
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
          setErr(e instanceof Error ? e.message : String(e));
          setLoadingTraces(false);
        });
    },
    [query, hostFilter, workspaceFilter]
  );

  const fetchSummaries = useCallback(() => {
    api
      .sessions(SESSIONS_SINCE_ALL, (page + 1) * 50)
      .then(setSummaries)
      .catch(() => null);
  }, [page]);

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
          setErr(e instanceof Error ? e.message : String(e));
      });
  }, [query, page, hostFilter, workspaceFilter]);

  const refresh = useCallback(() => {
    refreshTraces();
    fetchSummaries();
  }, [refreshTraces, fetchSummaries]);

  // Periodically refresh data every 30 seconds while the page is visible
  useEffect(() => setVisibleInterval(refresh, 30000), [refresh]);
  return (
    <div className="flex h-[calc(100vh-41px)] flex-col overflow-hidden border border-border/80 bg-surface-sunken md:flex-row">
      {/* Sidebar — master list */}
      <aside
        className={cx(
          "flex-shrink-0 flex flex-col border-b md:border-b-0 md:border-r border-border bg-surface transition-all duration-200 ease-in-out overflow-hidden",
          sidebarCollapsed ? "w-full md:w-12" : "w-full md:w-80"
        )}
      >
        {sidebarCollapsed ? (
          /* ── Collapsed: narrow strip ── */
          <div className="flex flex-col items-center py-3 gap-4 flex-shrink-0">
            <UiButton
              type="button"
              onClick={() => setSidebarCollapsed(false)}
              size="icon"
              variant="ghost"
              aria-label="Expand sidebar"
              tooltip="Expand sidebar"
            >
              <ChevronRight size={14} />
            </UiButton>
            <span className="text-caption font-bold uppercase tracking-[0.3em] text-neutral-400 [writing-mode:vertical-lr]">
              History
            </span>
          </div>
        ) : (
          /* ── Expanded: full sidebar ── */
          <>
            <div className="p-4 border-b border-border space-y-4 bg-surface-raised">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <h2 className="text-caption font-bold uppercase tracking-widest text-neutral-400 whitespace-nowrap">
                    History
                  </h2>
                  <span className="text-caption text-neutral-400 shrink-0">
                    All time
                  </span>
                  {loadingTraces && (
                    <span className="text-caption text-brand-400 shrink-0">
                      Scanning...
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <UiButton
                    type="button"
                    onClick={refresh}
                    size="icon" variant="ghost" className="shrink-0"
                    aria-label="Refresh sessions"
                    tooltip="Refresh sessions"
                  >
                    <RefreshCw size={14} />
                  </UiButton>{" "}
                  <UiButton
                    type="button"
                    onClick={() => setSidebarCollapsed(true)}
                    size="icon" variant="ghost" className="shrink-0"
                    aria-label="Collapse sidebar"
                    tooltip="Collapse sidebar"
                  >
                    <ChevronLeft size={14} />
                  </UiButton>
                </div>
              </div>
              <div className="flex gap-2">
                <UiInput
                  type="search"
                  uiSize="xs"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search sessions, tasks, models..."
                  className="w-full"
                />
                {searchInput && (
                  <UiButton
                    type="button"
                    onClick={() => setSearchInput("")}
                    size="icon"
                    variant="ghost"
                  >
                    <X size={14} />
                  </UiButton>
                )}
              </div>
              <div className="flex gap-2">
                <Select
                  aria-label="Filter by host"
                  value={hostFilter}
                  onValueChange={setHostFilter}
                  className="flex-1 min-w-0 bg-surface-overlay border border-border px-2 py-1.5 text-caption text-neutral-300 outline-none focus:border-brand-600"
                >
                  <SelectItem value="all">All hosts</SelectItem>
                  {availableHosts.map((h) => (
                    <SelectItem key={h} value={h}>
                      {h}
                    </SelectItem>
                  ))}
                </Select>
                <Select
                  aria-label="Filter by workspace"
                  value={workspaceFilter}
                  onValueChange={setWorkspaceFilter}
                  className="flex-1 min-w-0 bg-surface-overlay border border-border px-2 py-1.5 text-caption text-neutral-300 outline-none focus:border-brand-600"
                >
                  <SelectItem value="all">All workspaces</SelectItem>
                  {availableWorkspaces.map((w) => (
                    <SelectItem key={w} value={w} title={w}>
                      {w.split("/").pop() || w}
                    </SelectItem>
                  ))}
                </Select>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {err && (
                <div role="alert" className="p-4 text-body leading-6 text-muted-foreground">{err}</div>
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
                  const costAvailable =
                    Boolean(summary) && summary?.cost_status !== "unavailable" &&
                    ((summary?.total_turns ?? 0) > 0 || (summary?.total_cost_usd ?? 0) > 0);
                  const costPrefix =
                    summary?.cost_status === "estimated" ? "~" : "";

                  return (
                    <UiButton unstyled
                      key={t.id}
                      onClick={() =>
                        navigate(
                          `/activity/${sid}${query ? `?q=${encodeURIComponent(query)}` : ""}`
                        )
                      }
                      className={cx(
                        "w-full border-b border-border p-3.5 text-left transition-all hover:bg-neutral-800/40 group/card",
                        isActive
                          ? "bg-muted/45 border-r-2 border-r-neutral-500"
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
                              "shrink-0 text-caption font-mono uppercase tracking-[0.18em]",
                              hostTextClass
                            )}
                            title={host}
                          >
                            {host}
                          </span>
                          {sessionModel && (
                            <span
                              className="min-w-0 truncate text-caption font-mono text-sky-200"
                              title={sessionModel}
                            >
                              {sessionModel}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className="shrink-0 text-caption font-mono text-neutral-400"
                            title={fmtDate(t.created_at)}
                          >
                            {fmtRelativeTime(t.created_at)}
                          </span>
                          <span
                            className="shrink-0 text-caption font-mono text-neutral-400"
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
                            ? "text-foreground font-bold"
                            : "text-neutral-400 group-hover/card:text-neutral-300"
                        )}
                      >
                        {highlightSearchText(t.task || "Untitled Task", query)}
                      </p>

                      <div className="grid grid-cols-2 gap-1.5 rounded-md border border-border/60 bg-surface-sunken/70 p-2">
                        {(
                          [
                            [
                              summary?.cost_status === "estimated" ? "Priced" : "Cost",
                              summary && costAvailable
                                ? `${costPrefix}${fmtUsd(summary.total_cost_usd)}`
                                : "—",
                              "text-red-300",
                            ],
                            [
                              "Est. Saved",
                              summary
                                ? fmtUsd(summary.total_lemoncrow_savings_usd)
                                : "—",
                              "text-emerald-300",
                            ],
                          ] as [string, string, string][]
                        ).map(([label, value, valCls]) => (
                          <div
                            key={label}
                            className="flex items-center justify-between gap-2 rounded-md border border-border/50 bg-background/40 px-2 py-1.5"
                          >
                            <div className="truncate text-caption font-mono tracking-[0.18em] uppercase text-neutral-400 leading-none">
                              {label}
                            </div>
                            <div
                              className={cx(
                                "shrink-0 text-caption font-black font-mono leading-none",
                                valCls
                              )}
                            >
                              {value}
                            </div>
                          </div>
                        ))}
                      </div>
                    </UiButton>
                  );
                })}

              {!loadingTraces && hasMore && traces && (
                <UiButton
                  onClick={loadMore}
                  variant="ghost"
                  size="xs"
                  className="w-full"
                >
                  Load More
                </UiButton>
              )}

              {!loadingTraces && traces?.length === 0 && (
                <div className="p-4 text-center text-label text-muted-foreground">
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
  const usageSummaries = summaries;
  const pricedSummaries =
    summaries?.filter((summary) => summary.cost_status !== "unavailable" &&
      (summary.total_turns > 0 || summary.total_cost_usd > 0)) ?? null;
  const totalCost =
    pricedSummaries?.reduce((sum, item) => sum + item.total_cost_usd, 0) ?? 0;
  const totalSaved =
    usageSummaries?.reduce(
      (sum, item) => sum + item.total_lemoncrow_savings_usd,
      0
    ) ?? 0;
  const hasUnpriced = Boolean(
    usageSummaries?.some((summary) => summary.cost_status === "unavailable")
  );

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4 custom-scrollbar animate-in fade-in duration-300">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="Sessions"
          value={usageSummaries ? String(usageSummaries.length) : "—"}
          tone="neutral"
        />
        <MetricCard
          label="Total Cost"
          value={
            usageSummaries
              ? pricedSummaries && pricedSummaries.length > 0
                ? `${hasUnpriced ? "≥" : ""}${fmtUsd(totalCost)}`
                : "—"
              : "—"
          }
          tone="warning"
        />
        <MetricCard
          label="Est. Savings"
          value={usageSummaries ? fmtUsd(totalSaved) : "—"}
          tone="success"
        />
        <MetricCard
          label="Efficiency"
          value={
            usageSummaries && pricedSummaries && pricedSummaries.length > 0
              ? `${Math.round((totalSaved / (totalCost + totalSaved || 1)) * 100)}%`
              : "—"
          }
          tone="neutral"
        />
      </div>

      <div className="rounded-lg border border-border/70 bg-background/60 p-4 text-center">
        <Terminal size={28} className="mx-auto mb-3 text-muted-foreground" />
        <h3 className="mb-1 text-xs font-semibold text-neutral-300">
          Select a session
        </h3>
        <p className="mx-auto max-w-sm text-label leading-relaxed text-muted-foreground">
          Inspect reasoning logs, tool executions, costs, and file changes from any past run.
        </p>
      </div>
    </div>
  );
}
