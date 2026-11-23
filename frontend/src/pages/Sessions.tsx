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
import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Plug,
  RefreshCw,
  Terminal,
  X,
} from "lucide-react";
import { api, type TraceSummary, type SessionSummary } from "../api";
import { MetricCard, cx } from "../components/WorkbenchUI";
import { EmptyState as UiEmptyState } from "../components/ui/empty-state";
import { ServerUnreachable, describeError, isUnreachable } from "../components/ServerUnreachable";
import { REDUCTION_LABEL, SAVED_LABEL, costReductionPct } from "../lib/savings";
import { fmtPct, fmtUsd, fmtDate, fmtDuration, fmtRelativeTime } from "../lib/format";
import { setVisibleInterval } from "../lib/visibleInterval";
import { extractHost } from "./sessions/helpers";
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
        className="bg-muted-foreground/30 text-foreground rounded-sm px-0.5 border border-input/20"
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

/** From md the list is a rail beside the detail, so the newest session opens on arrival; a phone shows the list first. */
function listBesideDetail(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(min-width: 768px)").matches;
}

export default function Sessions() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [traces, setTraces] = useState<TraceSummary[] | null>(null);
  const [loadingTraces, setLoadingTraces] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [errUnreachable, setErrUnreachable] = useState(false);
  const [searchInput, setSearchInput] = useState(searchParams.get("q") ?? "");
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const tracesRequestSeq = useRef(0);
  const [summaries, setSummaries] = useState<SessionSummary[] | null>(null);
  const [summariesSettled, setSummariesSettled] = useState(false);
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

  // Newest activity first: the summaries' updated_at beats the trace's created_at.
  const orderedTraces = useMemo(() => {
    if (!displayTraces) return null;
    return displayTraces.slice().sort((a, b) => {
      const sa = sessionsMap?.get(a.session_id || a.id);
      const sb = sessionsMap?.get(b.session_id || b.id);
      return latestActivityMs(b, sb) - latestActivityMs(a, sa);
    });
  }, [displayTraces, sessionsMap]);
  const newestId = orderedTraces?.[0] ? orderedTraces[0].session_id || orderedTraces[0].id : null;

  // Desktop opens the newest session beside the list (once the summaries can say which is newest).
  useEffect(() => {
    if (id || !newestId || !summariesSettled || !listBesideDetail()) return;
    const search = searchParams.toString();
    navigate({ pathname: `/activity/${newestId}`, search: search ? `?${search}` : "" }, { replace: true });
  }, [id, navigate, newestId, searchParams, summariesSettled]);

  const fetchTracesPage = useCallback(
    (offset: number) => {
      const requestSeq = ++tracesRequestSeq.current;
      setLoadingTraces(true);
      setErr(null);
      setErrUnreachable(false);
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
          setErrUnreachable(isUnreachable(e));
          setErr(describeError(e));
          setLoadingTraces(false);
        });
    },
    [query, hostFilter, workspaceFilter]
  );

  const fetchSummaries = useCallback(() => {
    api
      .sessions(SESSIONS_SINCE_ALL, (page + 1) * 50)
      .then(setSummaries)
      .catch(() => null)
      .finally(() => setSummariesSettled(true));
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
        setErrUnreachable(isUnreachable(e));
        setErr(describeError(e));
      });
  }, [query, page, hostFilter, workspaceFilter]);

  const refresh = useCallback(() => {
    refreshTraces();
    fetchSummaries();
  }, [refreshTraces, fetchSummaries]);

  // Periodically refresh data every 30 seconds while the page is visible
  useEffect(() => setVisibleInterval(refresh, 30000), [refresh]);
  return (
    // The page scrolls the window under the sticky shell header; the history list is a sticky rail beside it.
    <div className="flex min-h-[calc(100dvh-41px)] flex-col border-x border-border/80 bg-surface-sunken md:flex-row">
      {/* Sidebar — master list */}
      <aside
        className={cx(
          "flex-shrink-0 flex flex-col border-b md:border-b-0 md:border-r border-border bg-surface transition-all duration-200 ease-in-out md:sticky md:top-[41px] md:h-[calc(100dvh-41px)] md:self-start md:overflow-hidden",
          sidebarCollapsed ? "w-full md:w-12" : "w-full md:w-80",
          id ? "hidden md:flex" : "min-h-0 flex-1 md:flex-none"
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
            <span className="text-caption font-semibold uppercase tracking-tag text-muted-foreground [writing-mode:vertical-lr]">
              History
            </span>
          </div>
        ) : (
          /* ── Expanded: full sidebar ── */
          <>
            <div className="p-4 border-b border-border space-y-4 bg-surface-raised">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <h2 className="text-caption font-semibold uppercase tracking-widest text-muted-foreground whitespace-nowrap">
                    History
                  </h2>
                  <span className="text-caption text-muted-foreground shrink-0">
                    All time
                  </span>
                  {loadingTraces && (
                    <span className="text-caption text-muted-foreground shrink-0">
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
                    aria-label="Clear search"
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
                  className="flex-1 min-w-0 bg-surface-overlay border border-border px-2 py-1.5 text-meta text-subtle outline-none focus:border-input"
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
                  className="flex-1 min-w-0 bg-surface-overlay border border-border px-2 py-1.5 text-meta text-subtle outline-none focus:border-input"
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

            <div className="flex-1 md:overflow-y-auto custom-scrollbar">
              {err && errUnreachable && <ServerUnreachable className="m-3" onRetry={refresh} />}
              {err && !errUnreachable && (
                <div role="alert" className="p-4 text-body leading-6 text-muted-foreground">{err}</div>
              )}

              {orderedTraces?.map((t) => {
                  const sid = t.session_id || t.id;
                  const isActive = id === sid;
                  const summary = sessionsMap?.get(sid);
                  const sessionModel = resolveSessionModel(summary, t);
                  const host = extractHost(t);
                  // _live sessions are still running in the RunLedger and
                  // haven't committed a final status to SQLite yet.
                  const displayStatus = t._live ? "running" : t.status;
                  const costAvailable =
                    Boolean(summary) && summary?.cost_status !== "unavailable" &&
                    ((summary?.total_turns ?? 0) > 0 || (summary?.total_cost_usd ?? 0) > 0);
                  // "Priced" already says the host reported no cost; the value itself stays neutral.
                  const costTitle =
                    summary?.cost_status === "estimated"
                      ? "Priced from the recorded token usage: the host did not report a cost."
                      : undefined;
                  return (
                    <UiButton unstyled
                      key={t.id}
                      onClick={() =>
                        navigate(
                          `/activity/${sid}${query ? `?q=${encodeURIComponent(query)}` : ""}`
                        )
                      }
                      className={cx(
                        "w-full border-b border-border p-3.5 text-left transition-all hover:bg-accent/40 group/card",
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
                            className="shrink-0 text-caption font-mono uppercase tracking-tag text-foreground"
                            title={host}
                          >
                            {host}
                          </span>
                          {sessionModel && (
                            <span
                              className="min-w-0 truncate text-caption font-mono text-muted-foreground"
                              title={sessionModel}
                            >
                              {sessionModel}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className="shrink-0 text-caption font-mono text-muted-foreground"
                            title={fmtDate(t.created_at)}
                          >
                            {fmtRelativeTime(t.created_at)}
                          </span>
                          <span
                            className="shrink-0 text-caption font-mono text-muted-foreground"
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
                          // Titles in the sans font so a list of them scans; ids, times and amounts stay monospace.
                          "mb-2 text-body line-clamp-2 leading-relaxed",
                          isActive
                            ? "text-foreground font-semibold"
                            : "text-muted-foreground group-hover/card:text-subtle"
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
                                ? fmtUsd(summary.total_cost_usd)
                                : "—",
                              // Spend is not an error: neutral text, not red.
                              "text-foreground",
                              costTitle,
                            ],
                            [
                              SAVED_LABEL,
                              summary && summary.savings_status !== "unavailable"
                                ? fmtUsd(summary.total_lemoncrow_savings_usd)
                                : "—",
                              "text-ok",
                              undefined,
                            ],
                          ] as [string, string, string, string | undefined][]
                        ).map(([label, value, valCls, title]) => (
                          <div
                            key={label}
                            title={title}
                            className="flex items-center justify-between gap-2 rounded-md border border-border/50 bg-background/40 px-2 py-1.5"
                          >
                            <div className="truncate text-caption font-mono tracking-tag uppercase text-muted-foreground leading-none">
                              {label}
                            </div>
                            <div
                              className={cx(
                                "shrink-0 text-caption font-semibold font-mono leading-none",
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

              {!loadingTraces && !err && traces?.length === 0 && (
                <div className="p-4 text-center text-label text-muted-foreground">
                  {query || hostFilter !== "all" || workspaceFilter !== "all" ? "No sessions match" : "No sessions yet"}
                </div>
              )}
            </div>
          </>
        )}
      </aside>

      {/* Main content */}
      <main className={cx("min-w-0 flex-1", !id && "hidden md:block")}>
        {id ? (
          <div className="flex flex-col">
            {/* Below md the list and the detail take turns: the detail used to start ~2650px down the page. */}
            <div className="shrink-0 border-b border-border px-3 py-1.5 md:hidden">
              <UiButton type="button" size="xs" variant="ghost" onClick={() => navigate({ pathname: "/activity", search: searchParams.toString() })}>
                <ArrowLeft size={14} aria-hidden="true" /> All sessions
              </UiButton>
            </div>
            <div className="flex-1">
              <SessionExplorerDetail sessionId={id} />
            </div>
          </div>
        ) : (
          <EmptyState
            summaries={summaries}
            summaryLimit={(page + 1) * 50}
            failed={Boolean(err)}
            noSessions={!loadingTraces && !err && traces?.length === 0 && !summaries?.length && !query && hostFilter === "all" && workspaceFilter === "all"}
          />
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state — shown when no session is selected
// ---------------------------------------------------------------------------

function countSessions(count: number): string {
  return `${count} ${count === 1 ? "session" : "sessions"}`;
}

function EmptyState({
  summaries,
  summaryLimit,
  failed,
  noSessions,
}: {
  summaries: SessionSummary[] | null;
  /** How many summaries were asked for: a full page means older sessions exist past it. */
  summaryLimit: number;
  failed: boolean;
  noSessions: boolean;
}) {
  if (failed) {
    // The list on the left says what went wrong and offers Retry; totals of nothing would only add "—" cards.
    return (
      <div className="p-4 animate-in fade-in duration-300">
        <UiEmptyState
          icon={<Terminal aria-hidden="true" />}
          title="Sessions are unavailable"
          description="The session list could not load. Retry from the list."
        />
      </div>
    );
  }
  if (noSessions) {
    return (
      <div className="p-4 animate-in fade-in duration-300">
        <UiEmptyState
          icon={<Plug aria-hidden="true" />}
          title="No sessions yet"
          description="Sessions appear here once a connected coding agent records one. Connect a coding agent in Integrations."
          action={
            <UiButton asChild variant="outline">
              <Link to="/settings/integrations">Open Integrations</Link>
            </UiButton>
          }
        />
      </div>
    );
  }

  const usageSummaries = summaries;
  const pricedSummaries =
    summaries?.filter((summary) => summary.cost_status !== "unavailable" &&
      (summary.total_turns > 0 || summary.total_cost_usd > 0)) ?? null;
  const totalCost =
    pricedSummaries?.reduce((sum, item) => sum + item.total_cost_usd, 0) ?? 0;
  const measuredSavings =
    usageSummaries?.filter((summary) => summary.savings_status !== "unavailable") ?? [];
  const totalSaved = measuredSavings.reduce(
    (sum, item) => sum + item.total_lemoncrow_savings_usd,
    0
  );
  const unmeasuredCount = (usageSummaries?.length ?? 0) - measuredSavings.length;
  const unpricedCount =
    usageSummaries?.filter((summary) => summary.cost_status === "unavailable").length ?? 0;
  // The totals come from the summaries loaded with the list, so say which sessions they cover.
  const windowLabel = usageSummaries
    ? usageSummaries.length >= summaryLimit
      ? `Totals for the ${countSessions(usageSummaries.length)} loaded, newest first. Load more in the list to include older ones.`
      : `Totals for all ${countSessions(usageSummaries.length)}.`
    : null;

  return (
    <div className="space-y-4 p-4 animate-in fade-in duration-300">
      {windowLabel && <p className="text-label text-muted-foreground">{windowLabel}</p>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="Sessions"
          value={usageSummaries ? String(usageSummaries.length) : "—"}
          tone="neutral"
        />
        <MetricCard
          label="Cost"
          value={
            usageSummaries
              ? pricedSummaries && pricedSummaries.length > 0
                ? `${unpricedCount ? "≥" : ""}${fmtUsd(totalCost)}`
                : "—"
              : "—"
          }
          detail={unpricedCount ? `At least: ${countSessions(unpricedCount)} without a price.` : undefined}
          tone="neutral"
        />
        <MetricCard
          label={SAVED_LABEL}
          value={
            usageSummaries && measuredSavings.length > 0
              ? `${unmeasuredCount ? "≥" : ""}${fmtUsd(totalSaved)}`
              : "—"
          }
          detail={
            usageSummaries && measuredSavings.length > 0 && unmeasuredCount
              ? `At least: ${countSessions(unmeasuredCount)} without recorded savings.`
              : undefined
          }
          tone="success"
        />
        <MetricCard
          // The same label, formula and precision as Home and Usage (lib/savings.ts).
          label={REDUCTION_LABEL}
          value={
            usageSummaries && pricedSummaries && pricedSummaries.length > 0 && !unmeasuredCount
              ? fmtPct(costReductionPct(totalCost, totalSaved))
              : "—"
          }
          tone="neutral"
        />
      </div>

      <div className="rounded-lg border border-border/70 bg-background/60 p-4 text-center">
        <Terminal size={20} className="mx-auto mb-3 text-muted-foreground" />
        <h3 className="mb-1 text-body font-semibold text-subtle">
          Select a session
        </h3>
        <p className="mx-auto max-w-sm text-label leading-relaxed text-muted-foreground">
          Pick a session in the list to see its prompts, tool calls, cost and changed files.
        </p>
      </div>
    </div>
  );
}
