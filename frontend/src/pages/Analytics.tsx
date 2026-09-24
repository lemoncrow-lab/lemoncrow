import { useEffect, useState, useMemo } from "react";
import {
  api,
  type GranularToolUsage,
  type AnalyticsDashboard,
} from "../api";
import { EmptyState, MetricCard } from "../components/WorkbenchUI";
import { fmtPct, fmtTok, fmtUsd } from "../lib/format";
import { useTimeRange } from "../lib/TimeRangeContext";

const TIMELINE_BREAKDOWN_OPTIONS = [
  { value: "daily", label: "Daily" },
  { value: "hourly", label: "Hourly" },
] as const;
type TimelineBreakdownValue =
  (typeof TIMELINE_BREAKDOWN_OPTIONS)[number]["value"];
type TimelineBucket = AnalyticsDashboard["daily"][number];

// ---- Shared helpers --------------------------------------------------------

function defaultdict_int() {
  return new Proxy({} as Record<string, number>, {
    get: (target, name: string) => (name in target ? target[name] : 0),
  });
}

type CompactLeaderboardRow = {
  label: string;
  sublabel?: string;
  value: string;
  detail?: string;
  barValue: number;
};

function OverviewBar({
  value,
  max,
  color = "bg-emerald-500/50",
}: {
  value: number;
  max: number;
  color?: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="h-1.5 w-full bg-neutral-900 rounded-full overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function CompactLeaderboard({
  title,
  rows,
  color = "bg-cyan-500/60",
  emptyMessage = "No data.",
}: {
  title: string;
  rows: CompactLeaderboardRow[];
  color?: string;
  emptyMessage?: string;
}) {
  const maxValue = Math.max(...rows.map((row) => row.barValue), 0.0001);

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-4 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-400 font-bold">
          {title}
        </div>
        <div className="text-[10px] font-mono text-neutral-400">
          Top {rows.length}
        </div>
      </div>

      {rows.length ? (
        <div className="space-y-4">
          {rows.map((row, index) => (
            <div key={`${title}:${row.label}:${index}`} className="space-y-1.5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="w-5 shrink-0 font-mono text-[10px] text-neutral-400">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="truncate text-sm text-neutral-200">
                      {row.label}
                    </span>
                  </div>
                  {row.sublabel && (
                    <div className="pl-7 pt-0.5 text-[10px] text-neutral-400 truncate">
                      {row.sublabel}
                    </div>
                  )}
                </div>
                <div className="shrink-0 text-right">
                  <div className="font-mono text-sm text-neutral-100">
                    {row.value}
                  </div>
                  {row.detail && (
                    <div className="text-[10px] text-neutral-400">
                      {row.detail}
                    </div>
                  )}
                </div>
              </div>
              <div className="pl-7">
                <OverviewBar
                  value={row.barValue}
                  max={maxValue}
                  color={color}
                />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-neutral-400 italic text-xs">{emptyMessage}</div>
      )}
    </section>
  );
}

function utcDateKey(date: Date) {
  return date.toISOString().slice(0, 10);
}

function addUtcDays(date: Date, days: number) {
  const next = new Date(date);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

function addUtcHours(date: Date, hours: number) {
  const next = new Date(date);
  next.setUTCHours(next.getUTCHours() + hours);
  return next;
}

function utcHourKey(date: Date) {
  return `${utcDateKey(date)} ${String(date.getUTCHours()).padStart(2, "0")}:00`;
}

function timelineValueLabel(cost: number) {
  if (cost > 0 && cost < 0.01) return "<$0.01";
  return `$${cost.toFixed(2)}`;
}

function timelineBucketLabel(date: string, breakdown: TimelineBreakdownValue) {
  if (breakdown === "hourly") {
    const [, time = date] = date.split(" ");
    return time.slice(0, 5);
  }
  return date.slice(5);
}

function localDayAnchor(date = new Date()) {
  return new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
}

export function fillDailyBuckets(
  daily: AnalyticsDashboard["daily"],
  days: number,
  now = new Date()
): TimelineBucket[] {
  const byDate = new Map(daily.map((bucket) => [bucket.date, bucket]));
  const totalDays = Math.min(Math.max(days, 1), 365);
  // A selected window always ends today. Anchoring to the newest returned row
  // hid ingestion gaps by sliding "Last 7 days" backwards when recent data was
  // missing (for example Sep 17-23 rendered as Sep 12-18).
  const end = localDayAnchor(now);
  const start = addUtcDays(end, -(totalDays - 1));

  return Array.from({ length: totalDays }, (_, index) => {
    const date = utcDateKey(addUtcDays(start, index));
    return (
      byDate.get(date) ?? {
        date,
        sessions: 0,
        cost: 0,
        input_tokens: 0,
        output_tokens: 0,
      }
    );
  });
}

export function fillHourlyBuckets(
  hourly: AnalyticsDashboard["hourly"],
  days: number,
  now = new Date()
): TimelineBucket[] {
  const byHour = new Map(hourly.map((bucket) => [bucket.date, bucket]));
  const end = new Date(now);
  end.setUTCMinutes(0, 0, 0);
  const totalHours = Math.min(Math.max(days, 1), 365) * 24;
  const start = addUtcHours(end, -(totalHours - 1));

  return Array.from({ length: totalHours }, (_, index) => {
    const date = utcHourKey(addUtcHours(start, index));
    return (
      byHour.get(date) ?? {
        date,
        sessions: 0,
        cost: 0,
        input_tokens: 0,
        output_tokens: 0,
      }
    );
  });
}

function SpendTimelineChart({
  dashboard,
  breakdown,
  days,
  onBreakdownChange,
}: {
  dashboard: AnalyticsDashboard;
  breakdown: TimelineBreakdownValue;
  days: number;
  onBreakdownChange: (breakdown: TimelineBreakdownValue) => void;
}) {
  const buckets =
    breakdown === "hourly"
      ? fillHourlyBuckets(dashboard.hourly ?? [], days)
      : fillDailyBuckets(dashboard.daily, days);
  if (!buckets.length)
    return (
      <div className="text-neutral-400 italic text-xs p-4">
        No timeline data.
      </div>
    );

  const maxCost = Math.max(...buckets.map((d) => d.cost), 0.0001);
  const totalCost = buckets.reduce((a, d) => a + d.cost, 0);
  const unitLabel = breakdown === "hourly" ? "hourly" : "daily";
  const isHourly = breakdown === "hourly";

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-5 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-neutral-400 font-bold">
            {breakdown === "hourly" ? "Hourly" : "Daily"} Activity
          </div>
          <div className="mt-1 text-sm text-neutral-400">
            Last {buckets.length} {unitLabel} snapshots in the selected window.
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {TIMELINE_BREAKDOWN_OPTIONS.map((option) => {
            const selected = option.value === breakdown;
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => onBreakdownChange(option.value)}
                className={`border px-2 py-1 text-[10px] font-bold uppercase tracking-widest transition-colors ${
                  selected
                    ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-200"
                    : "border-neutral-800 bg-neutral-950 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300"
                }`}
                aria-pressed={selected}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      <div
        className={`flex items-end overflow-x-auto pb-2 ${
          isHourly ? "gap-px" : "gap-2"
        }`}
      >
        {buckets.map((d) => {
          const h = Math.max(6, (d.cost / maxCost) * (isHourly ? 72 : 112));
          return (
            <div
              key={d.date}
              className={`flex flex-col items-center ${
                isHourly
                  ? "min-w-[34px] flex-1 gap-1"
                  : "min-w-[52px] flex-1 gap-2"
              }`}
              title={`${d.date}: ${timelineValueLabel(d.cost)} · ${d.sessions} sessions`}
            >
              <div
                className={`max-w-full truncate font-mono ${
                  d.cost > 0 ? "text-emerald-300" : "text-neutral-400"
                } ${isHourly ? "text-[10px]" : "text-[10px]"}`}
              >
                {timelineValueLabel(d.cost)}
              </div>
              <div className={`flex items-end ${isHourly ? "h-20" : "h-28"}`}>
                <div
                  className={`rounded-t-sm transition-colors cursor-default ${
                    d.sessions > 0
                      ? "bg-emerald-500/60 hover:bg-emerald-400/80"
                      : "bg-neutral-800/70"
                  } ${isHourly ? "w-5" : "w-7"}`}
                  style={{ height: `${h}px` }}
                />
              </div>
              <div
                className={`max-w-full truncate font-mono ${
                  d.sessions > 0 ? "text-neutral-400" : "text-neutral-400"
                } ${isHourly ? "h-3 text-[10px]" : "text-[10px]"}`}
              >
                {timelineBucketLabel(d.date, breakdown)}
              </div>
            </div>
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-3 pt-2 border-t border-neutral-800/60">
        <div>
          <div className="text-[10px] uppercase text-neutral-400 mb-0.5">
            Total
          </div>
          <div className="text-sm font-mono text-emerald-300">
            ${totalCost.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-neutral-400 mb-0.5">
            Avg/Bucket
          </div>
          <div className="text-sm font-mono text-emerald-300">
            ${(totalCost / buckets.length).toFixed(2)}
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Analytics({
  onInspectSavings,
}: {
  onInspectSavings?: () => void;
} = {}) {
  const [data, setData] = useState<GranularToolUsage[]>([]);
  const [dashboard, setDashboard] = useState<AnalyticsDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [dashLoading, setDashLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const { days, range } = useTimeRange();
  const [timelineBreakdown, setTimelineBreakdown] =
    useState<TimelineBreakdownValue>("daily");

  useEffect(() => {
    setTimelineBreakdown(range === "1d" ? "hourly" : "daily");
  }, [range]);

  useEffect(() => {
    setLoading(true);
    api
      .granularAnalytics(undefined, undefined, 5000, days)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));

    setDashLoading(true);
    api
      .analyticsDashboard(days)
      .then(setDashboard)
      .catch(() => setDashboard(null))
      .finally(() => setDashLoading(false));
  }, [days]);

  const costDriversData = useMemo(() => {
    const toolCosts = defaultdict_int();
    const toolCalls = defaultdict_int();
    const toolTokens = defaultdict_int();
    data
      .filter((d) => d.event_type === "tool_call")
      .forEach((d) => {
        toolCosts[d.tool_name] += d.cost || 0;
        toolCalls[d.tool_name] += d.call_count ?? 1;
        toolTokens[d.tool_name] += d.output_tokens;
      });
    return Object.entries(toolCosts)
      .map(([tool, cost]) => ({
        tool,
        cost,
        calls: toolCalls[tool],
        tokens: toolTokens[tool],
        costPerCall: cost / (toolCalls[tool] || 1),
      }))
      .sort((a, b) => b.cost - a.cost)
      .slice(0, 10);
  }, [data]);

  const topHostRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_host ?? []).slice(0, 5).map((row) => ({
      label: row.host,
      sublabel: `${row.sessions.toLocaleString()} sessions`,
      value: fmtUsd(row.cost),
      detail: `${fmtPct(row.cache_pct)} cache`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topModelRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_model ?? []).slice(0, 5).map((row) => ({
      label: row.model || "—",
      sublabel: `${(row.input_tokens / 1_000_000).toFixed(2)}M in · ${(row.output_tokens / 1_000_000).toFixed(2)}M out`,
      value: fmtUsd(row.cost),
      detail: `${row.sessions.toLocaleString()} sessions · ${fmtPct(row.cache_pct)} cache`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topDomainRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_domain ?? []).slice(0, 5).map((row) => ({
      label: row.domain,
      sublabel: `${row.sessions.toLocaleString()} sessions`,
      value: fmtUsd(row.cost),
      detail: `${fmtUsd(row.avg_cost)}/session`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topToolRows = useMemo<CompactLeaderboardRow[]>(() => {
    return costDriversData.slice(0, 5).map((row) => ({
      label: row.tool,
      sublabel: `${row.calls.toLocaleString()} calls`,
      value: fmtUsd(row.cost),
      detail: `${fmtTok(row.tokens)} out · $${row.costPerCall.toFixed(4)}/call`,
      barValue: row.cost,
    }));
  }, [costDriversData]);

  if (err) return <div className="text-red-300 p-6">Error: {err}</div>;
  if (loading && data.length === 0)
    return <EmptyState title="Loading analytics…" className="m-6" />;

  return (
    <div className="space-y-4 text-neutral-200">
      <div className="space-y-6">
          {/* Summary metrics */}
          <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <MetricCard
              label="Sessions"
              value={dashboard ? dashboard.summary.total_sessions.toLocaleString() : "—"}
              tone="cyan"
            />
            <MetricCard
              label="Cost"
              value={dashboard ? fmtUsd(dashboard.summary.total_cost) : "—"}
              tone="amber"
            />
            <button
              type="button"
              onClick={onInspectSavings}
              disabled={!onInspectSavings}
              className="min-w-0 text-left disabled:cursor-default"
              aria-label={onInspectSavings ? "Open savings evidence" : undefined}
            >
              <MetricCard
                label="Saved"
                value={
                  dashboard
                    ? fmtUsd(dashboard.summary.total_lemoncrow_savings_usd ?? 0)
                    : "—"
                }
                detail={onInspectSavings ? "View evidence →" : undefined}
                tone="emerald"
              />
            </button>
            <button
              type="button"
              onClick={onInspectSavings}
              disabled={!onInspectSavings}
              className="min-w-0 text-left disabled:cursor-default"
              aria-label={onInspectSavings ? "Open reduction evidence" : undefined}
            >
              <MetricCard
                label="Reduction"
                value={
                  dashboard
                    ? (() => {
                        const cost = dashboard.summary.total_cost;
                        const saved =
                          dashboard.summary.total_lemoncrow_savings_usd ?? 0;
                        const wouldHaveCost = cost + saved;
                        return wouldHaveCost > 0
                          ? `${((saved / wouldHaveCost) * 100).toFixed(1)}%`
                          : "0.0%";
                      })()
                    : "—"
                }
                detail={onInspectSavings ? "How this is measured →" : undefined}
                tone="violet"
              />
            </button>
          </section>

          {/* Timeline */}
          <div className="space-y-6">
              {dashLoading ? (
                <EmptyState title="Loading…" className="p-4" />
              ) : dashboard ? (
                <>
                  <SpendTimelineChart
                    dashboard={dashboard}
                    breakdown={timelineBreakdown}
                    days={days}
                    onBreakdownChange={setTimelineBreakdown}
                  />
                </>
              ) : (
                <div className="text-neutral-400 italic text-sm">
                  Data unavailable.
                </div>
              )}
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <CompactLeaderboard
              title="Top Hosts"
              rows={topHostRows}
              color="bg-cyan-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No host data."
              }
            />
            <CompactLeaderboard
              title="Top Models"
              rows={topModelRows}
              color="bg-emerald-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No model data."
              }
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <CompactLeaderboard
              title="Top Domains"
              rows={topDomainRows}
              color="bg-neutral-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No domain data."
              }
            />
            <CompactLeaderboard
              title="Top Tool Drivers"
              rows={topToolRows}
              color="bg-amber-500/60"
              emptyMessage="No tool usage found."
            />
          </div>

      </div>



    </div>
  );
}
